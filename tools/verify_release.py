# -*- coding: utf-8 -*-
"""热点新闻 —— 线上升级链路验真（只读，什么都不发）

为什么要有这个文件
    v1.5.8 发完那天，"发出去了"和"用户点检查更新真的能升上来"之间隔着一串很容易断的环节：
    升级源在不在、签名对不对得上线上那**一份字节**、清单里的 sha256/size 等不等于发行附件
    的真实字节、附件名差一个字就 404、老用户那边的版本号比不比线上小。
    这些环节任何一条断了，用户看到的就是"没有新版本"或"下载失败"，而仓库里一切看起来都正常。
    所以把它固化成一条命令，发版前后都能跑；用户报"升不了级"时先跑它，别猜。

它按客户端 updater.py 的口径逐条复刻（口径直接从源码里读，不在这个脚本里二次硬编码）：
    ① 从 updater.py 解析内置公钥 UPDATE_PUBKEY 与两个升级源地址（GitCode 主 / GitHub 兜底）；
    ② 取每个源的**原始字节**，再按客户端同样的规则拼出 .sig 地址（?ref=main 时 .sig 要插在 ? 前）；
    ③ 用内置公钥对原始字节做 Ed25519 验签 —— 验不过就是 fail-closed，用户绝不会被提示升级；
    ④ 线上那份 version.json 与仓库里这份是否逐字节一致（顺手给一个"只差换行符"的判定）；
    ⑤ 按 launcher.url 把附件真下载回来，比对 sha256 与 size（清单说 15,249,859 就得是这么多字节）；
    ⑥ GitHub 镜像侧的同一个附件名也下一遍，两平台字节必须一致；
    ⑦ 版本比较：清单版本 > 已装版本，用户才会看到"有新版本"（用 --from 传用户手上那个版本号）。

用法
    python tools/verify_release.py                 # 验仓库 version.json 里写的那个版本
    python tools/verify_release.py --from 1.5.7    # 顺带回答"装着 1.5.7 的人升不升得动"
    python tools/verify_release.py --offline       # 只跑本地能对上的部分（不联网）

退出码：全过 = 0；任何一条硬失败 = 1（网络取不到也算失败，因为用户此刻就是取不到）。
"""
import argparse
import hashlib
import io
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# 本脚本可能在 <仓库>/tools/ 下（随仓库入库），也可能被复制到别处跑；两种都认出来。
_CANDIDATES = (os.path.dirname(HERE), HERE)
REPO = next(p for p in _CANDIDATES if os.path.exists(os.path.join(p, 'updater.py')))
UPDATER = os.path.join(REPO, 'updater.py')
UA = {'User-Agent': 'HotnewsVerifyRelease/1.0'}
# 本机到 GitHub 的路时通时断（这正是狐径存在的理由）。取不到就走本地代理重试一次，
# 代理只决定"这台机器怎么把字节拿回来"，比对的仍是字节本身，不影响结论。
PROXY = os.environ.get('HN_VERIFY_PROXY') or 'http://127.0.0.1:8787'

results = []


def say(name, ok, detail=''):
    results.append((name, bool(ok)))
    print('  [%s] %s%s' % ('PASS' if ok else 'FAIL', name, ('  ' + detail) if detail else ''))
    return ok


SOCK_TMO = int(os.environ.get('HOTNEWS_SOCK_TMO') or 60)        # 一次 recv 的上限：链路断了 60 s 内就该报错，不用干等十五分钟
DEADLINE_BIG = int(os.environ.get('HOTNEWS_DEADLINE_BIG') or 900)   # 大附件的**总时长**预算：本机走代理实测只有 ~48 KB/s，15 MB 要五六分钟


def fetch(url, timeout=SOCK_TMO, tries=3, deadline=None, expect=0, label=''):
    """取一个 URL 的原始字节；直连不通时借本地代理。返回 (bytes, 错误说明)。

    ⚠ 两条口径要分开：`timeout` 只管**一次 recv**，`deadline` 才是**总时长**。
    以前这里只写 timeout=900 然后 `r.read()` 一把梭：链路上只要有零碎字节在动，
    socket 超时永远不触发，一次验真能拖到没人知道它是在跑还是卡死了
    （2026-10-26 就是这么拖掉二十分钟的）。现在边读边报进度，到预算就明确放弃。
    """
    ops = [urllib.request.build_opener(urllib.request.ProxyHandler({}))]
    if 'github' in url:
        ops.append(urllib.request.build_opener(
            urllib.request.ProxyHandler({'http': PROXY, 'https': PROXY})))
    err = ''
    for i in range(tries):
        for op in ops:
            t0 = time.time()
            nxt = 15
            try:
                req = urllib.request.Request(url, headers=UA)
                with op.open(req, timeout=timeout) as r:
                    if getattr(r, 'status', 200) != 200:
                        err = 'HTTP %s' % r.status
                        continue
                    buf = bytearray()
                    over = False
                    while True:
                        # read1：有多少取多少。read(n) 会一直等到凑满 n 字节或连接关闭 ——
                        # 那种慢链路下 deadline 永远轮不到（2026-10-26 那 15 MB 就是这么空转到没人知道在跑还是卡死）。
                        chunk = (r.read1(262144) if hasattr(r, "read1") else r.read(262144))
                        if not chunk:
                            break
                        buf += chunk
                        spent = time.time() - t0
                        if spent >= nxt:
                            print('    … %s 已取回 %.1f MB / %.1f MB（%.0f KB/s，%d s）'
                                  % (label or url.split('//')[-1][:52], buf.__len__() / 1048576.0,
                                     expect / 1048576.0 if expect else 0,
                                     buf.__len__() / max(spent, 1) / 1024.0, int(spent)))
                            sys.stdout.flush()
                            nxt = spent + 15
                        if deadline and spent > deadline:
                            err = '总时长预算 %d s 用尽（只取回 %d B / 需 %d B）' % (
                                deadline, len(buf), expect)
                            over = True
                            break
                    if over:
                        return None, err
                    if buf:
                        return bytes(buf), ''
            except Exception as e:
                err = '%s: %s' % (type(e).__name__, str(e)[:110])
        time.sleep(2)
    return None, err


def sig_url_for(url):
    """复刻 updater.py 的拼法：带查询串的源（GitCode 的 ?ref=main），.sig 要插在 ? 之前。"""
    p, _, q = url.partition('?')
    return p + '.sig' + ('?' + q if q else '')


def ver_tuple(v):
    return tuple(int(x) for x in re.findall(r'\d+', str(v) or '')[:3])


def main():
    ap = argparse.ArgumentParser(add_help=True, description='热点新闻线上升级链路验真（只读）')
    ap.add_argument('--from', dest='installed', default='',
                    help='用户已装的版本号，用来判断"老用户看得见这个新版吗"')
    ap.add_argument('--offline', action='store_true', help='不联网，只跑本地一致性')
    a = ap.parse_args()

    src = io.open(UPDATER, encoding='utf-8').read()

    def _const(name):
        m = re.search(r'^%s\s*=\s*"([^"]+)"' % name, src, re.M)
        return m.group(1) if m else None

    pub_hex = _const('UPDATE_PUBKEY')
    url_gc = _const('REMOTE_VERSION_URL_GC')
    url_gh = _const('REMOTE_VERSION_URL_GH')
    print('升级源（直接取 updater.py 里的常量，不另行硬编码）:')
    print('   主源 GitCode : %s' % url_gc)
    print('   兜底 GitHub : %s' % url_gh)
    if not (pub_hex and url_gc and url_gh):
        say('updater.py 里能解析出公钥与两个升级源地址', False,
            '缺 %s' % [n for n, v in (('UPDATE_PUBKEY', pub_hex),
                                      ('REMOTE_VERSION_URL_GC', url_gc),
                                      ('REMOTE_VERSION_URL_GH', url_gh)) if not v])
        return 1
    pub = bytes.fromhex(pub_hex)
    say('内置公钥格式正确（64 个十六进制字符）', len(pub_hex) == 64, '公钥 %s…' % pub_hex[:16])

    local_raw = io.open(os.path.join(REPO, 'version.json'), 'rb').read()
    local = json.loads(local_raw.decode('utf-8-sig'))
    ver = local.get('version')
    print('仓库 version.json: version=%s  launcher=%s' % (ver, '有' if local.get('launcher') else '无（精简版）'))
    if a.offline:
        say('（--offline）跳过联网部分', True, '结论只代表本地字节自洽，不代表线上')
        return 0 if all(ok for _, ok in results) else 1

    sys.path.insert(0, REPO)
    try:
        import _ed25519
    except Exception as e:
        say('加载仓库里的 _ed25519.py', False, '%s' % type(e).__name__)
        return 1

    ok_any = False
    for label, url in (('GitCode', url_gc), ('GitHub', url_gh)):
        print()
        print('② %s 侧：取原始字节 → 验签 → 比本地 → 下载附件' % label)
        raw, err = fetch(url)
        if raw is None:
            say('%s 线上 version.json 取得到' % label, False, err)
            continue
        sig_raw, err2 = fetch(sig_url_for(url))
        if sig_raw is None:
            say('%s 线上 version.json.sig 取得到' % label, False, err2)
            continue
        txt = ''.join(chr(b) for b in sig_raw if 32 <= b < 127).strip()
        sig_ok = False
        if re.fullmatch(r'[0-9a-fA-F]{128}', txt):
            try:
                sig_ok = bool(_ed25519.checkvalid(bytes.fromhex(txt), raw, pub))
            except Exception:
                sig_ok = False
        elif len(sig_raw) == 64:
            try:
                sig_ok = bool(_ed25519.checkvalid(sig_raw, raw, pub))
            except Exception:
                sig_ok = False
        say('%s 线上 version.json 用内置公钥验签通过' % label, sig_ok,
            '.sig %d 字节（%s）' % (len(sig_raw), '128 位十六进制文本' if re.fullmatch(r'[0-9a-fA-F]{128}', txt) else '64 字节裸签名'))
        if not sig_ok:
            continue
        d = json.loads(raw.decode('utf-8-sig'))
        same_raw = raw == local_raw
        same_norm = raw.replace(b'\r\n', b'\n') == local_raw.replace(b'\r\n', b'\n')
        say('%s 线上 version.json 与仓库这份一致' % label, same_raw or same_norm,
            'raw=%s 仅换行符不同=%s  线上 %d B / 本地 %d B' % (same_raw, (not same_raw) and same_norm, len(raw), len(local_raw)))
        lch = d.get('launcher') or {}
        au, ash, asz = lch.get('url'), (lch.get('sha256') or '').upper(), lch.get('size')
        print('   清单指向: %s' % au)
        if not (au and ash):
            say('%s 清单里有 launcher.url/sha256（完整版，不是精简版）' % label, False,
                '缺字段 = 客户端拿不到下载地址')
            continue
        blob, err3 = fetch(au, deadline=DEADLINE_BIG, expect=asz or 0,
                           label='%s 附件' % label)
        if blob is None:
            say('%s 附件能下载回来' % label, False, err3)
            continue
        real = hashlib.sha256(blob).hexdigest().upper()
        say('%s 附件真实 sha256 == 清单写的' % label, real == ash,
            '%s… vs %s…' % (real[:16], ash[:16]))
        say('%s 附件真实大小 == 清单写的' % label, len(blob) == asz,
            '%d B vs %s B' % (len(blob), asz))
        ok_any = True
        # GitHub 镜像侧：文件名与主源一致，路径换成 GitHub 的发行下载口
        if label == 'GitCode':
            ghu = 'https://github.com/kele551/hotnews/releases/download/v%s/%s' % (
                ver, urllib.parse.quote(os.path.basename(urllib.parse.urlsplit(au).path)))
            gb, err4 = fetch(ghu, deadline=DEADLINE_BIG, expect=asz or 0,
                             label='GitHub 镜像附件')
            if gb is None:
                say('GitHub 镜像同名附件也下得回来（用户可能从那边下）', False, err4)
            else:
                say('GitHub 镜像附件与主源逐字节一致', hashlib.sha256(gb).hexdigest().upper() == real,
                    '%d B' % len(gb))

    print()
    if a.installed:
        bigger = ver_tuple(ver) > ver_tuple(a.installed)
        say('已装 %s 的用户看得见 %s（版本比较用元组，不按字符串）' % (a.installed, ver), bigger,
            '%s vs %s' % (ver_tuple(a.installed), ver_tuple(ver)))

    if not ok_any:
        say('至少一个升级源完整走通（取清单 → 验签 → 附件字节对得上）', False,
            '两源都没走通：用户此刻必然升不了级')
    print()
    bad = [n for n, o in results if not o]
    print('验真结果: %s（%d 项，失败 %d 项）' % ('PASS' if not bad else 'FAIL', len(results), len(bad)))
    for n in bad:
        print('   FAIL -> %s' % n)
    return 0 if not bad else 1


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    sys.exit(main())
