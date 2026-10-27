# -*- coding: utf-8 -*-
r"""热点新闻 hotnews —— 一键发布流水线（GitCode 主 + GitHub 镜像）

与狐径、桌面壁纸的 `tools/publish.py` 同一套路，只是产物名、版本号来源、发布说明来源不同。
（2026-10-26 移植进仓库：v1.5.8 是照这套顺序**手工**发的，下一版别再靠人记流程。）

依赖: 一枚 GitCode 私人令牌（存一次长期复用）。HTTP 层只用 Python 标准库 ——
本机没有 third-party HTTP 库，以前那版依赖它，结果在这台机器上**整条流水线跑不起来**，
只能临时手搓脚本发版（2026-10-26 就是这个坑，见维护者工作区的发布记录）。
GitHub 侧走本机登录态的 gh.exe（它自己会挑线路，比手搓 REST 稳）；路径可用
环境变量 HOTNEWS_GH 或 ~/.hotnews-publish.json 的 "gh" 覆盖。

【2026-10-26 换目标】Gitee 那份老仓库已停更、客户端也不再去取（老地址一律不出现），
所以本脚本的发布目标改成：GitCode（国内主源）+ GitHub（国外镜像）。
AtomGit 不用推 —— 它按分钟级自动镜像 GitHub，连发行版附件一起镜像（当天实测过）。

用法:
    python tools/publish.py set-token <令牌>
    python tools/publish.py whoami
    python tools/publish.py release 1.5.9                # 只发 GitCode
    python tools/publish.py release 1.5.9 --github       # 双平台（用户要求两个都发）
    python tools/publish.py release 1.5.9 --skip-build   # 已打包，只做发布
    python tools/publish.py release 1.5.9 --notes 文件.md
    python tools/publish.py verify 1.5.8                 # 只读：线上验真，什么都不发

release 的自检默认**联网**跑（tools/preflight_release.py 会把链接与徽章、线上升级源对齐
两节都查）。本机网络不通时按同样方式传下去即可，它会把该节跳过并在日志里写明：
    --offline / --no-links / --e2e      透传给发版前自检
    --skip-preflight                    紧急跳过自检（必须在台账里写原因，不许默认用）
    --no-doc-check                      跳过"文档有没有跟上版本号"的核对（同上，留痕）

release 依次做:
    ① 改版本号：**version.json 是唯一的版本来源**（server.py 运行时就读它），
       文档（README / 说明.txt / docs/CHANGELOG.md）只**核对**提没提到这一版，
       跟不上就停下来 —— 脚本不拿正则替人写文案，避免出现"代码 v1.5.9、说明还是 v1.5.8"。
    ② 打包 (PyInstaller build_exe.spec) -> dist/热点新闻.exe
       → 复制成**发出去的附件名** dist/hotnews-vX.Y.Z.exe（ASCII + 带版本号）
    ③ 生成升级源 version.json：version + launcher{url,sha256(大写),size} + notes，
       然后**当场用客户端内置公钥自验一遍**（签错了绝不允许走到上传）
    ③+ 用 Ed25519 私钥给 version.json 的**原始字节**签名 -> version.json.sig
       私钥位置：环境变量 HOTNEWS_ED25519_KEY → ~/.hotnews-publish.json 的 "ed25519_key"
       → 找不到就中止。⚠ 四款软件**各用一把钥匙**，别拿狐径那把签热点的清单。
    ④ 提交 + 打 tag + **只推 tag**（GitCode 与 GitHub 两个远端）
    ⑤ GitCode 建发行版 + 上传 hotnews-vX.Y.Z.exe / version.json / version.json.sig
    ⑥ 同步 GitHub（--github：tag + Release + 附件；代码由 ④⑤ 的 main 推送完成）
    ⑦ 验真：两个平台各下载回来比对 SHA256；并**回读线上 version.json + .sig**，
       用客户端内置公钥真验一遍（验不过就算发布失败 —— 别让用户先发现升级通道是断的）

发版铁律（顺序不能换）：④ 里 **tag 先推、main 最后推**。升级源就是 main 上那份
version.json，main 一推上去线上立刻开始提示升级 —— 那时附件必须已经躺好。
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
from urllib import request as _rq
from pathlib import Path

quote = urllib.parse.quote          # 老代码里到处直接用 quote()，这里补个名字

# 2026-10-27 修（提交前试跑就复现了）：Windows 控制台默认 GBK，
# 这份文档里有 ⚠ 这类字符，一进 main() 打印 __doc__ 就 UnicodeEncodeError 退出，
# 而原来的 reconfigure 只写在 cmd_release / cmd_verify 里面 —— 连 help 都看不全。
# 与壁纸的 publish.py 一样，在模块顶上钉成 UTF-8。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

REPO_DIR = Path(__file__).resolve().parent.parent
OWNER, REPO = 'kele551', 'hotnews'
BRANCH = 'main'
GC_API = 'https://gitcode.com/api/v5'
GC_WEB = 'https://gitcode.com'
GH_REPO = '%s/%s' % (OWNER, REPO)
# 推送目标：GitCode（国内主源）+ GitHub（国外镜像）。
# ⚠ 本仓库的 remote **名字**是历史遗留：GitHub 那台叫 origin，另外还留着已停更的 gitee
# 和按分钟自动镜像 GitHub 的 atomgit。所以这里**按 URL 认远端、不按名字写死**
# （见 remotes_by_host()）—— 按名字写死会推错地方，而把升级源推回 gitee 就等于
# "用户点了个 404"。没被认出来的 remote 一律不推。
# ---- 本机私有配置（可选，永不进仓库）----------------------------------------
# 公开仓库里不写任何维护者本机路径。本机要用的解释器 / git / gh / 令牌位置，
# 一律按：① 环境变量 → ② 本机私有配置 ~/.hotnews-publish.json → ③ 与机器无关的默认值。
# 私有配置文件示例（放在用户主目录，文件名前面有个点）：
#   {"python": "C:\\\\Python313\\\\python.exe", "git_dir": "C:\\\\Program Files\\\\Git\\\\mingw64\\\\bin",
#    "gh": "C:\\\\tools\\\\gh.exe", "token_file": "D:\\\\private\\\\gitcode_token"}
_CFG_PATH = Path.home() / '.hotnews-publish.json'
_CFG = {}
if _CFG_PATH.is_file():
    try:
        _CFG = json.loads(_CFG_PATH.read_text(encoding='utf-8'))
    except Exception as _e:
        print('警告: %s 解析失败(%s)，按"没有本机配置"继续' % (_CFG_PATH, _e), file=sys.stderr)

_ENV_TOKEN_FILE = os.environ.get('GITCODE_TOKEN_FILE') or _CFG.get('token_file')
# 都没给就落到仓库内的 .gitcode_token（已 gitignore）；也可以直接用环境变量 GITCODE_TOKEN 传令牌本身
TOKEN_CANDIDATES = [_ENV_TOKEN_FILE] if _ENV_TOKEN_FILE else [str(REPO_DIR / '.gitcode_token')]
# 打包用的解释器必须能覆盖 —— 这个 PY 既用来跑 PyInstaller（打包），也用来跑发版前自检。
# 热点新闻的运行时是 **Python 3.13**（requirements.txt 那四个库都要装齐，见 README「打包」）。
# ⚠ **打包绝不能用 venv 混搭环境**：那种「一个解释器的 DLL 配另一个 venv 的库」组合，
# 打出来的 exe 一启动就弹
#   Security validation failure: unexpected name of application's home directory!
# （同一条坑在别的机器上真机复现过）。所以打包时用环境变量 HOTNEWS_PY 或
# ~/.hotnews-publish.json 里的 python 明确指一个独立解释器，别用 sys.executable 撞运气。
PY = os.environ.get('HOTNEWS_PY') or _CFG.get('python') or sys.executable


def _find_git_dir():
    """找 git.exe 所在目录：环境变量 → 本机配置 → PATH → 常见安装位置；找不到返回空串。"""
    for _c in (os.environ.get('HOTNEWS_GIT'), _CFG.get('git_dir')):
        if _c and Path(_c, 'git.exe').is_file():
            return str(_c).replace('\\', '/')
    _w = shutil.which('git')
    if _w:
        return str(Path(_w).parent).replace('\\', '/')
    for _c in (r'C:\Program Files\Git\cmd', r'C:\Program Files (x86)\Git\cmd'):
        if Path(_c, 'git.exe').is_file():
            return _c.replace('\\', '/')
    return ''


GIT_EXEC_PATH = _find_git_dir()
# 有绝对路径就用绝对路径: 有的执行环境按名字找不到 git(WinError 2)
GIT_EXE = (GIT_EXEC_PATH + '/git.exe') if GIT_EXEC_PATH else 'git'
GH = os.environ.get('HOTNEWS_GH') or _CFG.get('gh') or shutil.which('gh') or 'gh'
# GitHub 的 git 端口在这台机器上时通时断，失败时借本地代理重试（只在命令行 -c 上带，不改 config）
GIT_PROXY = os.environ.get('HOTNEWS_GIT_PROXY') or _CFG.get('git_proxy') or 'http://127.0.0.1:8787'
# 打包产物叫 dist/热点新闻.exe（build_exe.spec 里 name="热点新闻"，文件名不带版本号），
# 而**发出去的附件名必须 ASCII 且带版本号**：hotnews-vX.Y.Z.exe —— 清单 url 里写的、
# 用户书签与老版本升级逻辑按名字取的都是这个，差一个字就是 404
# （2026-10-26 三个平台上都是这个名字；中文文件名传上 GitCode 会被破坏）。
BUILD_EXE = REPO_DIR / 'dist' / '热点新闻.exe'


def asset_exe(ver):
    """把打包产物落成"要发出去的那一份"，返回 dist/hotnews-vX.Y.Z.exe。"""
    if not BUILD_EXE.is_file():
        sys.exit('找不到打包产物 %s —— 先打包（或去掉 --skip-build）' % BUILD_EXE)
    dst = BUILD_EXE.parent / ('hotnews-v%s.exe' % ver)
    if not dst.is_file() or dst.stat().st_size != BUILD_EXE.stat().st_size:
        shutil.copy2(str(BUILD_EXE), str(dst))
    return dst

# ---- Ed25519 签名私钥（升级通道加固，2026-10-16）------------------------------
# 取值顺序与上面那套完全一致：① 环境变量 HOTNEWS_ED25519_KEY →
# ② 本机私有配置 ~/.hotnews-publish.json 的 "ed25519_key" → ③ 找不到就**明确报错**。
# 私钥内容只在签名那一刻读一次；本文件里既不打印它、也不把路径写死成维护者机器上的样子。
ED25519_KEY = os.environ.get('HOTNEWS_ED25519_KEY') or _CFG.get('ed25519_key') or ''


def resolve_sign_key():
    """确定本次发版用哪把私钥；拿不到就报清楚"该怎么办"，绝不静默跳过签名。

    为什么宁可不发版：客户端**只认验过签的升级源**。要是这次漏了签名，线上就会多出
    一份"新版本号但签名为空/不匹配"的 version.json —— 已装用户一律被拒绝，等于升级通道断掉。
    """
    if ED25519_KEY:
        p = Path(ED25519_KEY)
        if p.is_file():
            return p
        sys.exit('配置里的 Ed25519 私钥文件不存在: %s\n'
                 '（环境变量 HOTNEWS_ED25519_KEY，或 ~/.hotnews-publish.json 的 "ed25519_key"）' % p)
    sys.exit('找不到 Ed25519 签名私钥 —— 拒绝发布（客户端只认验过签的升级源）。\n'
             '任选一种方式告诉本脚本私钥在哪（私钥本身**不要**放进仓库）：\n'
             '  ① 环境变量:  set HOTNEWS_ED25519_KEY=<私钥文件路径>\n'
             '  ② 本机私有配置 ~/.hotnews-publish.json 里加一行:  "ed25519_key": "<私钥文件路径>"\n'
             '私钥文件的内容是 64 个十六进制字符（32 字节种子）。')

# ---- 版本号：热点只有**一个**来源 —— version.json ----------------------------
# server.py 启动时读 version.json 的 version 当 APP_VERSION（_read_version()），
# 所以清单里的版本号就是程序自己报的版本号，不存在"代码里另一处忘了改"。
# 文档（README / 说明.txt / CHANGELOG）**不参与改写**，只做核对：见 check_docs_version()。
VERSION_JSON = REPO_DIR / 'version.json'
DOC_VERSION_FILES = ('README.md', '说明.txt', 'docs/CHANGELOG.md')


def _read_version_json():
    """读仓库里那份 version.json，顺手把"线上口径"的格式问题当场挡下来。

    客户端是**先对原始字节验签、再解析**的：BOM、CRLF、末尾多一个换行都会让签名对不上，
    于是全体已装用户被 fail-closed 拒掉。所以这里宁可现在报错，也不发一份"看着一样但字节不同"。
    """
    raw = VERSION_JSON.read_bytes()
    if raw.startswith(b'\xef\xbb\xbf'):
        sys.exit('version.json 带了 UTF-8 BOM —— 签名是对原始字节做的，客户端会验不过，拒绝发布')
    if b'\r\n' in raw:
        sys.exit('version.json 是 CRLF 行尾（应为 LF）—— 同上，字节不一致就等于验签失败')
    if raw.endswith(b'\n'):
        sys.exit('version.json 末尾多了换行 —— 线上那份是不带尾换行的 806 B 形状，签名会对不上')
    return json.loads(raw.decode('utf-8'))


def _write_version_json(d):
    """按线上那份的字节形状写：UTF-8 无 BOM、LF、2 空格缩进、**末尾不加换行**。"""
    body = json.dumps(d, ensure_ascii=False, indent=2)
    VERSION_JSON.write_bytes(body.encode('utf-8'))
    return VERSION_JSON


# ---------- 令牌 ----------
def save_token(tok):
    p = Path(TOKEN_CANDIDATES[0])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tok.strip(), encoding='utf-8')
    print('令牌已存到', p)


def load_token():
    t = os.environ.get('GITCODE_TOKEN')
    if t:
        return t.strip()
    for c in TOKEN_CANDIDATES:
        p = Path(c)
        if p.exists():
            return p.read_text(encoding='utf-8').strip()
    return None


def api(method, path, token, payload=None, timeout=120, raw_body=None, headers=None, extra=None):
    """GitCode v5 接口：令牌走 access_token 查询参数（本机没有第三方 HTTP 库，纯标准库）。

    payload 走 JSON；raw_body 直接发原始字节。extra 是额外的查询参数（如 file_name）。
    返回解析后的 dict/list；非 2xx 抛 RuntimeError（带上前 300 字符正文，便于现场判断）。
    """
    p = {'access_token': token}
    p.update(extra or {})
    url = GC_API + path + '?' + urllib.parse.urlencode(p)
    hd = {'User-Agent': 'hotnews-publish'}
    hd.update(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        hd['Content-Type'] = 'application/json'
    elif raw_body is not None:
        data = raw_body
    req = _rq.Request(url, data=data, headers=hd, method=method)
    try:
        with _rq.urlopen(req, timeout=timeout) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError('API %s %s -> %s %s'
                           % (method, path, e.code, e.read()[:300].decode('utf-8', 'replace')))
    if not body.strip():
        return {}
    return json.loads(body.decode('utf-8', 'replace'))


# ---------- 本地 ----------
def bump_version(ver):
    """把 version.json 的 version 字段改成本次版本（launcher 段稍后用真实 exe 重写）。"""
    if not re.fullmatch(r'\d+(\.\d+)+', ver):
        sys.exit('版本号形状不对: %r —— 应形如 1.5.9（不带 v 前缀：清单里存的就是它，'
                 'tag 是 v%s，附件名是 hotnews-%s.exe）' % (ver, ver, ver))
    d = _read_version_json()
    old = str(d.get('version') or '')
    d['version'] = ver
    _write_version_json(d)
    print('① 版本号: version.json %s -> %s（launcher 段等 ③ 用真实 exe 重写）' % (old, ver))
    return ver


def check_docs_version(ver):
    """核对对外文档有没有跟上本次版本；跟不上就**停下来**，不拿正则替人写文案。

    为什么不做成"自动改文档"：README 与 说明.txt 里那句是当前版本的一句人话，CHANGELOG
    更是整段说明 —— 拿正则硬塞会写出对不上号的假文案，发出去用户看到的就是错的更新内容
    （notes 是客户端**直接显示**的文本）。所以顺序反过来：人先写好，脚本只负责验收。
    """
    missing = []
    for name in DOC_VERSION_FILES:
        f = REPO_DIR / name
        if not f.is_file():
            missing.append('%s（文件不存在）' % name)
        elif ver not in f.read_text(encoding='utf-8-sig'):
            missing.append(name)
    if missing:
        sys.exit('①+ 文档还没提到 v%s: %s\n'
                 '   先写好 README.md / 说明.txt 的当前版本，以及 docs/CHANGELOG.md 里\n'
                 '   「## v%s（YYYY-MM-DD）」那一节，再重跑本脚本。\n'
                 '   （确认这几处本来就不该出现版本号时，可加 --no-doc-check 跳过，日志里会留痕）'
                 % (ver, '、'.join(missing), ver))
    print('①+ 文档版本核对: %s 都提到 v%s' % ('、'.join(DOC_VERSION_FILES), ver))


def _load_ed25519():
    """加载仓库里那份纯标准库 Ed25519 —— 客户端用的就是它，签/验同一套代码。"""
    if str(REPO_DIR) not in sys.path:
        sys.path.insert(0, str(REPO_DIR))
    try:
        import _ed25519
    except Exception as e:
        sys.exit('加载 _ed25519.py 失败(%s) —— 拒绝发布（没法自证签名可用）' % type(e).__name__)
    return _ed25519


def _load_seed(key_path):
    txt = Path(key_path).read_text(encoding='utf-8-sig').strip()
    if not re.fullmatch(r'[0-9a-fA-F]{64}', txt):
        sys.exit('私钥文件 %s 内容不对：应是 64 个十六进制字符（32 字节种子），实际 %d 个字符'
                 % (Path(key_path).name, len(txt)))
    return bytes.fromhex(txt)


def make_version_json(exe, notes='', sign_key=None):
    """用**真实产物**的字节重写 launcher 段 → 签名 → 当场用客户端内置公钥自验。

    为什么只能在这个位置：launcher.sha256/size 必须是发出去那个 exe 的真实值，
    而 exe 要到 ② 才存在。所以"先打包、后补清单、再签、再验"是硬顺序。

    自验这一步是今天最想加的东西（四款各用**一把不同的** Ed25519 钥匙：热点
    f6931bd2… / 狐径 a29ea38e… / 壁纸 cdb8bb7a…）。拿错钥匙签出来的签名**形状完全合法**
    （128 个十六进制字符），只有用客户端内置公钥验一遍才会露 —— 而线上露就意味着
    全体已装用户被 fail-closed 拒掉、升级通道当场断掉。所以这里验不过就直接中止。
    """
    if not sign_key:
        sys.exit('内部错误：make_version_json 没拿到私钥 —— 拒绝发布（客户端只认验过签的升级源）')
    data = exe.read_bytes()
    d = _read_version_json()
    ver = str(d.get('version') or '')
    d['launcher'] = {
        'url': '%s/%s/%s/releases/download/v%s/%s' % (GC_WEB, OWNER, REPO, ver, exe.name),
        'sha256': hashlib.sha256(data).hexdigest().upper(),
        'size': len(data),
    }
    if notes:
        d['notes'] = notes
    vj = _write_version_json(d)
    raw = vj.read_bytes()
    print('③ 升级源 version.json: %d B，launcher.url 指向 %s' % (len(raw), exe.name))

    ed = _load_ed25519()
    sig = ed.signature(raw, _load_seed(sign_key)).hex()
    if len(sig) != 128 or not re.fullmatch(r'[0-9a-fA-F]{128}', sig):
        sys.exit('签名结果形状不对（应 128 个十六进制字符，实际 %d）—— 拒绝发布' % len(sig))
    vsig = Path(str(vj) + '.sig')
    vsig.write_bytes(sig.encode('ascii'))      # 与线上同形状：无 BOM、无尾换行
    pub_hex = _client_pubkey()
    try:
        ed.checkvalid(bytes.fromhex(sig), raw, bytes.fromhex(pub_hex))
    except Exception:
        sys.exit('❌ 刚签好的 version.json **用客户端内置公钥验不过** —— 十有八九是私钥拿错了\n'
                 '   （四款软件各用一把钥匙，本仓库客户端的公钥前 16 位是 %s…）。\n'
                 '   已中止，什么都没往线上发。请核对 HOTNEWS_ED25519_KEY / '
                 '~/.hotnews-publish.json 的 "ed25519_key" 指的是热点那把。' % pub_hex[:16])
    print('③+ Ed25519 签名自验: 通过（sig %d 个十六进制字符，公钥 %s… 取自 updater.py）'
          % (len(sig), pub_hex[:16]))
    return vj, vsig


def run(cmd, cwd=REPO_DIR, check=True):
    e = os.environ.copy()
    if GIT_EXEC_PATH:                       # 没找到 git 目录就交给 PATH 上那个 git
        e['GIT_EXEC_PATH'] = GIT_EXEC_PATH
        e['PATH'] = GIT_EXEC_PATH.replace('/', os.sep) + os.pathsep + e.get('PATH', '')
    cmd = list(cmd)
    if cmd and cmd[0] == 'git':             # 不靠 PATH 找, 直接用绝对路径
        cmd[0] = GIT_EXE.replace('/', os.sep)
    print('   $', ' '.join(cmd))
    r = subprocess.run(cmd, cwd=str(cwd), env=e,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out = r.stdout.decode('utf-8', 'replace')
    if check and r.returncode != 0:
        print(out)
        raise RuntimeError('命令失败: %s' % ' '.join(cmd))
    return out


GIT_ID = ['-c', 'user.name=海风（kele551）',
          '-c', 'user.email=kele551@users.noreply.github.com']
# 全局 git config 里没有身份（用户要求不改 config），所以每次提交/打标签都显式带上。


def _remote_exists(name):
    return name in run(['git', 'remote'], check=False)


def _git_raw(args):
    """跑一条 git，返回 (退出码, 输出)。推送成败只信退出码 —— 管道会把真实状态吞掉（踩过）。"""
    e = os.environ.copy()
    if GIT_EXEC_PATH:
        e['GIT_EXEC_PATH'] = GIT_EXEC_PATH
        e['PATH'] = GIT_EXEC_PATH.replace('/', os.sep) + os.pathsep + e.get('PATH', '')
    args = list(args)
    if args and args[0] == 'git':
        args[0] = GIT_EXE.replace('/', os.sep)
    print('   $', ' '.join(args))
    r = subprocess.run(args, cwd=str(REPO_DIR), env=e,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return r.returncode, r.stdout.decode('utf-8', 'replace')


def remotes_by_host():
    """按 **URL** 挑出要推的两个远端（GitCode 主源 + GitHub 镜像），返回 remote 名字列表。

    本仓库的 remote 名字是历史遗留：GitHub 叫 `origin`，还留着停更的 `gitee` 和自动镜像的
    `atomgit`。按名字写死会推错地方；按 URL 认则无论谁将来改名、重指，都只会推到这两个平台，
    **gitee 永远不会被推到**（升级源要是回到那个 451 的死仓库，用户就升不上来了）。
    """
    got, other = {}, []
    for line in run(['git', 'remote', '-v']).splitlines():
        if '(fetch)' not in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        name, low = parts[0], parts[1].lower()
        if 'gitcode.com' in low:
            got.setdefault('gitcode', name)
        elif 'github.com' in low:
            got.setdefault('github', name)
        else:
            other.append('%s(%s)' % (name, low.split('//')[-1].split('/')[0]))
    missing = [k for k in ('gitcode', 'github') if k not in got]
    if missing:
        raise RuntimeError('本仓库没有 %s 的 remote —— 拒绝发布（升级源必须两平台都在）'
                           % '/'.join(missing))
    if other:
        print('   不推这些 remote（已停更或自动镜像）:', ', '.join(other))
    return [got['gitcode'], got['github']]


def _push(remote, ref):
    """推一个 ref；GitHub 那条线本机时不时裸连不上，失败就带一次本地代理重试。

    代理地址优先环境变量 HOTNEWS_GIT_PROXY / 本机配置 git_proxy（默认 127.0.0.1:8787，
    就是狐径自己的本地代理）。代理只在"这次推送"上用 -c 传进去，同样不落 git config。
    推完一律用 ls-remote 复核远端 sha：退出码只说明"这条命令没崩"，说明不了"远端变了"。
    """
    if not _remote_exists(remote):
        print('   跳过 %s：本仓库没有这个 remote' % remote)
        return False
    want = _git_raw(['git', 'rev-parse', 'HEAD' if ref == BRANCH else ref])[1].split()[-1]
    code, out = _git_raw(['git', 'push', remote, ref])
    if code != 0:
        print('   裸推失败(%s)，带本地代理 %s 再试一次' % (out.strip()[-160:], GIT_PROXY))
        code, out = _git_raw(['git', '-c', 'http.https://github.com/.proxy=' + GIT_PROXY,
                              'push', remote, ref])
        if code != 0:
            print('   带代理也失败:', out.strip()[-200:])
    remote_sha = (_git_raw(['git', 'ls-remote', remote, ref])[1].split() or ['', ''])[1:2]
    if remote_sha and remote_sha[0] == want:
        print('   pushed %s -> %s（远端 sha %s… 与本地一致）' % (ref, remote, want[:10]))
        return True
    raise RuntimeError('推送 %s -> %s 没生效：本地 %s，远端 %s'
                       % (ref, remote, want[:10], (remote_sha or ['(取不到)'])[0][:10]))


def git_commit_and_tag(ver):
    """提交 + 打 tag + **只推 tag**（升级源在 main 上，main 必须等附件就位后再推）。"""
    run(['git', 'add', '-A'])
    if run(['git', 'diff', '--cached', '--name-only']).strip():
        run(['git'] + GIT_ID + ['commit', '-m', 'release: v%s' % ver])
    else:
        print('   工作区没有新改动, 跳过本次提交(只补发版)')
    tag = 'v%s' % ver
    if not run(['git', 'tag', '-l', tag]).strip():
        run(['git'] + GIT_ID + ['tag', '-a', tag, '-m', tag])   # 打 tag 同样要显式带身份
    else:
        print('   tag %s 已存在, 不重复创建' % tag)
    targets = remotes_by_host()
    for remote in targets:
        _push(remote, tag)
    print('④ 代码已提交、tag %s 已推 %s（main 还没推，附件就位后再推 —— 顺序是铁律）'
          % (tag, '+'.join(targets)))


def git_push_branch():
    """⑤⑥ 都做完、附件已验真之后，才让升级源在两个平台上生效。"""
    targets = remotes_by_host()
    for remote in targets:
        _push(remote, BRANCH)
    print('④+ main 已推 %s（GitCode 主源 + GitHub 镜像）—— 此刻起线上才开始提示升级'
          % '+'.join(targets))


# ---------- 发行版 ----------
def _gc_get_release(tag, token):
    """按 tag 取发行版。注意 GitCode 的响应里**没有 id 字段**（Gitee 有），一切按 tag 寻址。"""
    try:
        return api('GET', '/repos/%s/%s/releases/tags/%s' % (OWNER, REPO, quote(tag)), token)
    except Exception:
        return None


def gitcode_release(ver, token, assets, notes):
    """GitCode：建发行版 + 逐个附件走"取上传地址 → PUT 原始字节"两步。

    2026-10-26 实测过的正解（Gitee 那套 attach_files 接口在 GitCode 一律 404/405）：
      ① POST /repos/{o}/{r}/releases                 body 里 target_commitish=main
      ② GET  /repos/{o}/{r}/releases/{tag}/upload_url?file_name=xxx   → {url, headers}
      ③ PUT  文件原始字节到那个 OBS url，平台回调把附件登记到发行版上
    同名附件：先比大小，大小一致就当已传好跳过；**大小不同必须人工到网页上删掉旧的**
    （GitCode 没有可靠的删除附件接口，宁可停下来也不能让线上留两份不同字节的同名包）。
    """
    tag = 'v%s' % ver
    rel = _gc_get_release(tag, token)
    if rel and rel.get('tag_name') == tag:
        print('⑤ GitCode 发行版已存在:', tag, '| 附件', len(rel.get('assets') or []), '个')
    else:
        rel = api('POST', '/repos/%s/%s/releases' % (OWNER, REPO), token, payload={
            'tag_name': tag, 'name': tag, 'body': notes or tag,
            'target_commitish': BRANCH, 'prerelease': False})
        if not rel or not rel.get('tag_name'):
            raise RuntimeError('GitCode 建发行版失败: %s' % json.dumps(rel, ensure_ascii=False)[:300])
        print('⑤ GitCode 发行版已创建:', rel.get('tag_name'))
    have = {a.get('name'): a for a in ((_gc_get_release(tag, token) or {}).get('assets') or [])}
    for path in assets:
        p = str(path)
        name = os.path.basename(p)
        size = os.path.getsize(p)
        old = have.get(name)
        if old and old.get('size') == size:
            print('   附件已在（同名同大小，跳过）:', name, size, 'B')
            continue
        if old:
            raise RuntimeError('GitCode 上同名附件字节不同（线上 %s B / 本地 %d B）：%s\n'
                               '请到 %s/%s/%s/releases/tag/%s 删掉旧附件再重发。'
                               % (old.get('size'), size, name, GC_WEB, OWNER, REPO, tag))
        j = api('GET', '/repos/%s/%s/releases/%s/upload_url' % (OWNER, REPO, quote(tag)),
                token, extra={'file_name': name}, timeout=120)
        up = (j or {}).get('url') or ((j or {}).get('data') or {}).get('url')
        hd = (j or {}).get('headers') or ((j or {}).get('data') or {}).get('headers') or {}
        if not up:
            raise RuntimeError('拿不到上传地址: %s' % json.dumps(j, ensure_ascii=False)[:200])
        data = io.open(p, 'rb').read()
        h = dict(hd)
        h['Content-Length'] = str(len(data))
        req = _rq.Request(up, data=data, headers=h, method='PUT')
        with _rq.urlopen(req, timeout=1800) as r:
            if r.status not in (200, 201, 204):
                raise RuntimeError('附件 PUT 失败 %s -> %s' % (name, r.status))
        print('   附件已上传:', name, size, 'B')
    chk = _gc_get_release(tag, token) or {}
    names = [a.get('name') for a in (chk.get('assets') or [])]
    missing = [os.path.basename(str(p)) for p in assets if os.path.basename(str(p)) not in names]
    if missing:
        raise RuntimeError('GitCode 回读缺少附件（平台没登记上）:', ', '.join(missing))
    print('   下载页:', '%s/%s/%s/releases/tag/%s' % (GC_WEB, OWNER, REPO, tag))


def sync_github(ver, assets, notes_file):
    """GitHub 侧走 gh.exe（本机登录态自己挑线路），不用 github.com 的 git 端口。

    两个写死的坑：
      · `release create` **不能带 --target**：tag 已经 push 上去了，带了会报 already exists；
      · 附件名必须 ASCII 且带版本号（hotnews-vX.Y.Z.exe），中文名传上去会被破坏，
        老用户按名字取就 404 —— 清单 url 里那一段与附件名必须一字不差。
    """
    tag = 'v%s' % ver
    code, out = _gh(['release', 'create', tag, '--title', tag,
                     '--notes-file', str(notes_file), '--repo', GH_REPO])
    if code != 0 and 'already exists' not in out.lower():
        raise RuntimeError('gh release create 失败: %s' % out.strip()[-400:])
    if code != 0:
        print('   GitHub 发行版已存在, 继续补附件')
    code, out = _gh(['release', 'upload', tag, '--clobber'] +
                    [str(a) for a in assets] + ['--repo', GH_REPO])
    if code != 0:
        raise RuntimeError('gh release upload 失败: %s' % out.strip()[-400:])
    print('⑥ GitHub 代码 / tag / Release / 附件 已同步')


def _gh(args):
    cmd = [GH] + args
    print('   $', ' '.join(cmd[:7]), '...' if len(cmd) > 7 else '')
    r = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=1800)
    return r.returncode, ((r.stdout or '') + (r.stderr or ''))


# ---------- 验真 ----------
VERIFY_PROXY = os.environ.get('HOTNEWS_VERIFY_PROXY') or GIT_PROXY


# 超时的两条口径要分开写，这是 2026-10-26 拿一脚坑换来的：
#   SOCK_TMO  一条连接上「一次 recv」的上限。链路真断了，60 s 就该报错换下一条，
#             而不是像以前那样写 300/600 s —— 那等于"卡死了也看不出来"。
#   DEADLINE  **总时长**预算。这台机器走代理从 GitHub 取字节的实测速率只有 ~48 KB/s
#             （2026-10-26 实测：220 s 拿了 10.5 MB 就断），15 MB 的附件本来就要五六分钟。
#             慢不等于错，所以预算给足；但到点必须收，不能让人分不清在跑还是卡住。
SOCK_TMO = int(os.environ.get('HOTNEWS_SOCK_TMO') or 60)
DEADLINE_SMALL = int(os.environ.get('HOTNEWS_DEADLINE_SMALL') or 180)
DEADLINE_BIG = int(os.environ.get('HOTNEWS_DEADLINE_BIG') or 900)


def _progress(label, got, expect, secs):
    """每 15 秒一行进度：大附件下载途中"没有输出"最容易被当成卡死。"""
    print('  … %s 已取回 %.1f MB / %.1f MB（%.0f KB/s，用了 %d s）'
          % (label, got / 1048576.0, expect / 1048576.0 if expect else 0,
             got / max(secs, 1) / 1024.0, int(secs)))
    sys.stdout.flush()


def _download(url, timeout=SOCK_TMO, tries=2, headers=None, deadline=None, expect=0, label=''):
    """取一个 URL 的原始字节；失败重试，github 那侧再借一次本地代理。

    维护机到 GitHub 的路**时通时断**（狐径自己就是为这个存在的），直连拿不到就走
    HOTNEWS_VERIFY_PROXY（默认 127.0.0.1:8787，即本机狐径的代理端口）。代理只决定这台
    机器"怎么把字节取回来"，比对的是字节本身，结论不受影响。

    边下边攒、每 15 秒报一次进度，到 deadline 就**明确放弃**（换下一条链路，不再重试同一条）。
    ⚠ 别把这里的默认值照抄到别的机器上：2026-10-26 一次只读验真因为
    "900 s × 3 次 × 两条链路"这种写法拖了二十分钟都没走到第二个平台。
    """
    ops = [_rq.build_opener(_rq.ProxyHandler({}))]
    if VERIFY_PROXY and 'github' in url:
        ops.append(_rq.build_opener(_rq.ProxyHandler(
            {'http': VERIFY_PROXY, 'https': VERIFY_PROXY})))
    hd = {'User-Agent': 'hotnews-verify', 'Accept-Encoding': 'identity'}
    hd.update(headers or {})
    for _ in range(tries):
        for op in ops:
            t0 = time.time()
            next_report = 15
            try:
                req = _rq.Request(url, headers=hd)
                with op.open(req, timeout=timeout) as r:
                    if r.status != 200:
                        continue
                    buf = bytearray()
                    over = False
                    while True:
                        # read1：有多少取多少。read(n) 会一直等到凑满 n 字节或连接关闭——那种慢链路下
                        # deadline 永远轮不到（2026-10-26 那 15 MB 就是这么空转到没人知道在跑还是卡死）。
                        chunk = (r.read1(262144) if hasattr(r, "read1") else r.read(262144))
                        if not chunk:
                            break
                        buf += chunk
                        spent = time.time() - t0
                        if spent >= next_report:
                            _progress(label or url.split('//')[-1][:52], len(buf), expect, spent)
                            next_report = spent + 15
                        if deadline and spent > deadline:
                            print('  x %s 总时长预算 %d s 用尽：只取回 %d B / 需要 %d B'
                                  '（这是链路慢，不代表包有问题；要等它就调大预算环境变量）'
                                  % (label or url.split('//')[-1][:52], deadline,
                                     len(buf), expect))
                            sys.stdout.flush()
                            over = True
                            break
                    if over:
                        return None          # 到预算就整条放弃，重试只会再把十分钟扔进去
                    if buf:
                        return bytes(buf)
            except Exception:
                continue
        time.sleep(3)
    return None


def _gh_token():
    """问 gh.exe 要本机登录态的令牌 —— 只是给验真抬一次 API 速率上限，拿不到照样能验。

    这里**不走 _gh()**：那个的超时是 1800 s（上传附件要那么久），拿个令牌要不了 30 s，
    给它一小时只会把整轮验真拖成"看不出是在跑还是卡死"（2026-10-26 就这么拖过二十分钟）。
    """
    try:
        r = subprocess.run([GH, 'auth', 'token'], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ''
    except Exception:
        return ''


def _gh_asset(tag, name, deadline=DEADLINE_BIG, expect=0):
    """走 api.github.com 取 GitHub 那个附件对象。返回 (原始字节 or None, 平台自报的 sha256 or '')。

    为什么要备一条路：`/releases/download/<tag>/<file>` 会 302 跳到
    objects.githubusercontent.com，那条 CDN 在这台机器上时通时断（2026-10-26 实测撞过：
    裸连 WinError 10060、走代理 502；同日再测：走代理**能通但只有 ~48 KB/s**）。
    API 口拿的是**同一个附件对象**的字节。
    顺带把平台自报的 digest 也带回来 —— 但注意：**digest 一致不等于通过**（那只是
    GitHub 自己说"我这儿是这个 sha256"），闸门要的始终是真把字节取回来比。
    """
    hd = {'Accept': 'application/vnd.github+json'}
    tok = _gh_token()
    if tok:
        hd['Authorization'] = 'token ' + tok
    # 元数据口本机实测 0.9 s 就回，给 60 s × 1 次足够；拿不到就直接放弃这条路
    raw = _download('https://api.github.com/repos/%s/releases/tags/%s' % (GH_REPO, tag),
                    60, 1, hd, deadline=60, label='GitHub 发行元数据')
    if not raw:
        return None, ''
    try:
        rel = json.loads(raw.decode('utf-8', 'replace'))
    except Exception:
        return None, ''
    for a in rel.get('assets') or []:
        if a.get('name') != name:
            continue
        digest = (a.get('digest') or '').replace('sha256:', '').strip().lower()
        if not a.get('url'):
            return None, digest
        h = dict(hd)
        h['Accept'] = 'application/octet-stream'
        return (_download(a['url'], SOCK_TMO, 1, h, deadline=deadline,
                          expect=a.get('size') or expect,
                          label='GitHub %s（API 附件口）' % name), digest)
    return None, ''


def gc_asset_url(ver, name):
    """GitCode 附件的下载口形状（2026-10-26 实测：与 Gitee 同名同形，不带令牌也能下）。"""
    return '%s/%s/%s/releases/download/%s/%s' % (GC_WEB, OWNER, REPO, 'v%s' % ver, quote(name))


def verify(ver, assets):
    """两个平台各把附件下回来比 sha256。GitHub 裸下载口不通时自动改走 API 附件口再试。"""
    ok = True
    tag = 'v%s' % ver
    for p in assets:
        p = str(p)
        name = os.path.basename(p)
        size = os.path.getsize(p)
        want = hashlib.sha256(io.open(p, 'rb').read()).hexdigest()
        big = size > 2 * 1024 * 1024
        dl = DEADLINE_BIG if big else DEADLINE_SMALL
        for label, url in (
                ('GitCode', gc_asset_url(ver, name)),
                ('GitHub', 'https://github.com/%s/%s/releases/download/%s/%s'
                 % (OWNER, REPO, tag, quote(name)))):
            # GitHub 那条浏览器下载口**只试一次**：它 302 到 objects.githubusercontent.com，
            # 在这台机器上要么几秒回来、要么就慢到没法看 —— 卡在那里重试没有意义。
            # （2026-10-26 一次只读验真就是被 600 s × 2 轮 × 2 条链路拖了二十分钟没出结果。）
            # 拿不到立刻换 api.github.com 的附件口 —— 拿的是同一个附件对象，字节一样才算数。
            data = _download(url, SOCK_TMO, 1 if label == 'GitHub' else 2,
                             deadline=dl, expect=size, label='%s %s' % (label, name))
            via = ''
            digest = ''
            if data is None and label == 'GitHub':
                data, digest = _gh_asset(tag, name, deadline=dl, expect=size)
                via = '（改走 api.github.com 附件口）'
            if data is None:
                extra = ''
                if digest and digest == want:
                    extra = '；GitHub 自报 digest 与本地一致（%s…）—— 但**自报不算通过**，' \
                            '闸门要的是真字节，换台网络正常的机器再跑一次' % want[:16]
                print('  [FAIL] %s %-22s 下载失败%s%s' % (label, name, via, extra))
                ok = False
                continue
            same = hashlib.sha256(data).hexdigest() == want
            ok = ok and same
            print('  [%s] %s %-22s %9d B  一致=%s%s'
                  % ('PASS' if same else 'FAIL', label, name, len(data), same, via))
            sys.stdout.flush()
    print('   下载页:', '%s/%s/%s/releases/tag/v%s' % (GC_WEB, OWNER, REPO, ver))
    return ok


def _client_pubkey():
    """从**客户端源码** updater.py 里解析出内置公钥（热点的升级验签就在 updater.py）。

    故意解析源码、而不是在这里另写一份常量：签名的公钥和客户端内置的那把必须是同一把，
    解析得到就天然一致；哪天换了钥匙却忘了改客户端，这里当场就对不上。
    """
    src = (REPO_DIR / 'updater.py').read_text(encoding='utf-8')
    m = re.search(r"""UPDATE_PUBKEY\s*=\s*['"]([0-9a-fA-F]{64})['"]""", src)
    if not m:
        sys.exit('从 updater.py 里解析不到内置公钥 UPDATE_PUBKEY —— 拒绝发布')
    return m.group(1)


def verify_signature(vj, vsig):
    """回读两平台线上的 version.json + version.json.sig，用客户端内置公钥**真验一遍**。

    这一步验的是"用户点检查更新时到底能不能过"：
      · 线上 version.json 的原始字节 → 与本地发布的那份一致；
      · 同目录的 version.json.sig 拿得到、且用客户端内置公钥验得过。
    任何一条不过就返回 False（发版算失败，别让用户自己发现升级通道是断的）。
    """
    import urllib.request          # noqa: F401  (老代码留个记号，实际取数走 _download)
    print('⑦+ 线上升级源验签（用客户端内置公钥，两个平台各一次）:')
    if str(REPO_DIR) not in sys.path:
        sys.path.insert(0, str(REPO_DIR))
    try:
        import _ed25519
    except Exception as e:
        sys.exit('加载 _ed25519.py 失败(%s) —— 拒绝发布（没法自证签名可用）' % type(e).__name__)
    pub = bytes.fromhex(_client_pubkey())
    local_raw = open(str(vj), 'rb').read()
    ok = True
    # 客户端读的就是这两个地址：GitCode 的 raw 走 API（它没有 Gitee 那种 /raw/分支 直链），
    # GitHub 用 raw.githubusercontent.com。带一个时间戳参数是防 CDN 给回半新半旧的缓存。
    bust = int(time.time())
    gc_base = GC_API + '/repos/%s/%s/raw' % (OWNER, REPO)
    gh_base = 'https://raw.githubusercontent.com/%s/%s/%s' % (OWNER, REPO, BRANCH)
    for label, mk in (
            ('GitCode', lambda f: '%s/%s?ref=%s&t=%d' % (gc_base, f, BRANCH, bust)),
            ('GitHub', lambda f: '%s/%s?v=%d' % (gh_base, f, bust))):
        # 806 B 与 128 B 两个小文件：单次 recv 60 s、总预算 120 s、最多 3 轮，
        # 最坏也就三分钟出头，不会再出现"一条链路把整轮验真拖没影"的写法。
        raw = _download(mk('version.json'), SOCK_TMO, 3, deadline=120,
                        label='%s version.json' % label)
        sig_raw = _download(mk('version.json.sig'), SOCK_TMO, 3, deadline=120,
                            label='%s version.json.sig' % label)
        if raw is None or sig_raw is None:
            print('  [FAIL] %s 线上 version.json / .sig 取不到（GitCode 与 GitHub 都必须能取到）' % label)
            ok = False
            continue
        same_bytes = raw == local_raw
        sig_txt = sig_raw.decode('utf-8', 'replace').strip()
        sig_ok = False
        if re.fullmatch(r'[0-9a-fA-F]{128}', sig_txt):
            try:
                sig_ok = bool(_ed25519.checkvalid(bytes.fromhex(sig_txt), raw, pub))
            except Exception:
                sig_ok = False
        ok = ok and same_bytes and sig_ok
        print('  [%s] %s 线上 version.json 与本地一致=%s，Ed25519 验签=%s（公钥 %s…）'
              % ('PASS' if (same_bytes and sig_ok) else 'FAIL', label, same_bytes, sig_ok,
                 pub.hex()[:16]))
    return ok


def _changelog_section(ver):
    """从 docs/CHANGELOG.md 里抠出「## v<ver>（日期）」那一节，清成给用户看的纯文本。

    只清 markdown 记号，**不改一个字的内容**：`**粗**`/反引号去掉、`[文字](链接)` 只留文字、
    行首 `- ` 换成客户端里一贯用的 `· `、引用行与分隔线丢掉。notes 会被**原样显示在升级弹窗
    里**，所以这里不代写、不总结、不补内容 —— 那一节写成什么样就是什么样。
    """
    f = REPO_DIR / 'docs' / 'CHANGELOG.md'
    if not f.is_file():
        return ''
    lines = f.read_text(encoding='utf-8-sig').splitlines()
    start = None
    head = re.compile(r'^##\s+v?%s\b' % re.escape(ver))
    for i, l in enumerate(lines):
        if head.match(l):
            start = i + 1
            break
    if start is None:
        return ''
    out = []
    for l in lines[start:]:
        if l.startswith('## '):
            break                                   # 下一版的小节，到此为止
        t = l.replace('**', '').replace('`', '')
        t = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', t)
        t = t.strip()
        if not t or t.startswith('>') or t.startswith('---'):
            continue
        out.append(re.sub(r'^[-*]\s+', '· ', t))
    if not out:
        return ''
    return '更新内容\n' + '\n'.join(out)


def load_notes(ver, notes_file=None):
    """取本次的发布说明（notes）—— 三条来源按优先级，且**绝不拿上一版的文案凑数**。

    ① --notes <文件>        ：整份照用（临时写一份的时候）。
    ② docs/release-notes.md ：存在才用，且首行版本号必须等于本次；不等就是"忘了换文案"，
                              直接中止 —— 以前会把上一版说明发到新发行页上（张冠李戴）。
    ③ docs/CHANGELOG.md     ：取本次那一节（热点一直按版本逐节维护 CHANGELOG，**这是主路径**）。
                              找不到本节也中止：宁可停下来，也不发一份"版本号是新的、说明是旧的
                              或空的"清单 —— 那份文本是用户唯一能看到的"为什么要升级"。
    """
    if notes_file:
        p = Path(notes_file)
        if not p.exists():
            sys.exit('--notes 指定的文件不存在: %s' % p)
        print('   发布说明: --notes %s' % p.name)
        return p.read_text(encoding='utf-8')

    rn = REPO_DIR / 'docs' / 'release-notes.md'
    if rn.exists():
        text = rn.read_text(encoding='utf-8')
        first = next((l for l in text.splitlines() if l.strip()), '')
        m = re.search(r'v?(\d+(?:\.\d+)+)', first)
        if not m:
            sys.exit('release-notes.md 首行没有版本号（应形如「热点新闻 v%s — 标题」）:\n  %s'
                     % (ver, first))
        if m.group(1) != ver:
            sys.exit('release-notes.md 还是 v%s 的文案，本次要发的是 v%s —— 直接发会让发行页挂着旧说明。\n'
                     '请把它改成 v%s 的内容，或删掉本文件改用 docs/CHANGELOG.md 的那一节。'
                     % (m.group(1), ver, ver))
        print('   发布说明: release-notes.md (首行版本 v%s, 与本次一致)' % m.group(1))
        return text

    notes = _changelog_section(ver)
    if not notes:
        sys.exit('拿不到 v%s 的发布说明：docs/release-notes.md 不存在，'
                 'docs/CHANGELOG.md 里也没有「## v%s（…）」这一节。\n'
                 '先把本次的更新内容写进 CHANGELOG 再发 —— 这份文本会原样显示在用户的升级弹窗里。'
                 % (ver, ver))
    print('   发布说明: docs/CHANGELOG.md 的 v%s 一节（%d 行）' % (ver, notes.count('\n') + 1))
    return notes


# ---------- 入口 ----------
def cmd_release(ver, skip_build=False, notes_file=None, with_github=False):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    token = load_token()
    if not token:
        sys.exit('没有令牌, 先跑: python tools/publish.py set-token <令牌>')
    t0 = time.time()

    notes = load_notes(ver, notes_file)

    bump_version(ver)
    if '--no-doc-check' in sys.argv:
        print('   ⚠ 已显式跳过文档版本核对（--no-doc-check）')
    else:
        check_docs_version(ver)
    if not skip_build:
        out = run([PY, '-m', 'PyInstaller', 'build_exe.spec', '--clean', '--noconfirm'])
        print('② 打包完成:', out.strip().splitlines()[-1] if out.strip() else '')

    exe = asset_exe(ver)              # dist/热点新闻.exe -> dist/hotnews-vX.Y.Z.exe
    print('   exe =', exe, exe.stat().st_size, 'B')

    # 私钥在这一步就定下来（找不到直接中止，不会走到"已经打包/已改版本号"才发现发不了）
    sign_key = resolve_sign_key()
    vj, vsig = make_version_json(exe, notes, sign_key)

    # ── 发版铁律：先自检、后发布（2026-10-08 定，四款一样）────────────────
    # 起因：发完版才发现"升级后程序可能起不来"(杀软拦第一次启动)。
    # 位置很关键：必须在 make_version_json 之后 —— 自检要校验"即将发出去的
    # version.json 与 exe 是否一致"，放前面会永远报不一致。
    # ⚠ 热点的自检脚本在**仓库里**（tools/preflight_release.py），而且只认
    #   -h / --offline / --no-links / --e2e：给它塞一个 exe 路径会直接 exit(2)。
    #   联网的两节（链接与徽章、线上升级源对齐）在本机网络不通时可用 --offline 跳，
    #   但那等于没跑完 —— 发完还得单独跑一次 tools/verify_release.py 把链路补齐。
    if '--skip-preflight' in sys.argv:
        print('   ⚠ 已显式跳过发版前自检（--skip-preflight）—— 请在台账里写明原因')
    else:
        pf = REPO_DIR / 'tools' / 'preflight_release.py'
        if not pf.exists():
            sys.exit('找不到发版前自检脚本 %s —— 不许发布（铁律）' % pf)
        print('   ── 发版前自检（必须全绿）──')
        extra = [a for a in ('--offline', '--no-links', '--e2e') if a in sys.argv]
        r = subprocess.run([PY, str(pf)] + extra, cwd=str(REPO_DIR))
        if r.returncode != 0:
            sys.exit('❌ 发版前自检未通过，已中止发布。修好再发（或 --skip-preflight 紧急跳过）')
        print('   ✅ 自检全绿，继续发布')

    # ⚠ version.json.sig 必须与 version.json **一起**发出去：客户端拿到 version.json
    # 会立刻去取同目录的 .sig 验签，取不到就拒绝这次升级（fail-closed）。
    # 老客户端只读 version.json（多一个附件对它无害）—— 这就是"兼容老版本"的做法。
    assets = [exe, vj, vsig]

    # ── 发版顺序是铁律（2026-10-26 定）：tag 先推 → 两平台建发行版传附件 →
    #    附件验真 → **main 最后推**（升级源就是 main 上那份 version.json，它一上线
    #    全体老用户立刻开始提示升级；那时附件必须已经躺好，否则用户点了个 404）。
    git_commit_and_tag(ver)
    gitcode_release(ver, token, assets, notes)
    if with_github:
        nf = notes_file
        if not nf:
            nf = REPO_DIR / '_notes.md'
            nf.write_text(notes or ('v%s' % ver), encoding='utf-8')
        sync_github(ver, assets, nf)
        if not notes_file and Path(nf).exists():
            Path(nf).unlink()
    else:
        print('⑥ 跳过 GitHub (加 --github 才同步)')
    print('⑦ 验真（附件字节，两个平台各一次）:')
    ok = verify(ver, assets)
    if not ok:
        sys.exit('❌ 附件验真没通过 —— main 先不推，线上仍是上一版；修好再重跑本脚本')
    git_push_branch()
    # 验签必须放在 main 推完之后：推之前线上那份 version.json 还是上一版的字节，
    # 拿它跟本次发布的本地文件比必然"不一致"，那是假警报。
    ok = verify_signature(vj, vsig) and ok
    print('全部完成, 用时 %.0f 秒, 验真=%s' % (time.time() - t0, ok))
    return 0 if ok else 1


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    a = sys.argv[1]
    if a == 'set-token' and len(sys.argv) > 2:
        save_token(sys.argv[2])
        u = api('GET', '/user', sys.argv[2].strip())
        print('令牌有效, 用户 =', u.get('login') or u.get('name'))
    elif a == 'whoami':
        t = load_token()
        if not t:
            sys.exit('没有存过令牌')
        u = api('GET', '/user', t)
        print('用户 =', u.get('login') or u.get('name'))
    elif a == 'verify' and len(sys.argv) > 2:
        sys.exit(cmd_verify(sys.argv[2]))
    elif a == 'release' and len(sys.argv) > 2:
        sys.exit(cmd_release(sys.argv[2],
                             skip_build='--skip-build' in sys.argv,
                             with_github='--github' in sys.argv,
                             notes_file=(sys.argv[sys.argv.index('--notes') + 1]
                                         if '--notes' in sys.argv else None)))
    else:
        print(__doc__)


def cmd_verify(ver):
    """只读验真：不发任何东西，把线上那一版按用户的口径重跑一遍。

    什么时候用它：① 发完版想确认"用户点检查更新真的能过"；② 用户报"升级失败"时，
    先在这里判断是通道断了还是包坏了，别一上来就改代码发新版。
    附件比的是本地仓库里那份 version.json 指向的字节 —— 所以它同时回答
    "线上清单说的 size/sha256，跟两个平台上真实能被下载到的文件对不对得上"。
    """
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    vj = REPO_DIR / 'version.json'
    vsig = REPO_DIR / 'version.json.sig'
    if not vj.is_file() or not vsig.is_file():
        sys.exit('找不到本地 %s / %s —— 先确认仓库里的升级源是不是这一版' % (vj, vsig))
    d = _read_version_json()                     # 顺带把 BOM/CRLF/尾换行这些"验签杀手"查一遍
    if str(d.get('version')) != str(ver):
        print('⚠ 本地 version.json 是 v%s，你要验的是 v%s —— 清单可能对不上号'
              % (d.get('version'), ver))
    lau = d.get('launcher') or {}
    url = lau.get('url') or ''
    exe_name = os.path.basename(url) or ('hotnews-v%s.exe' % ver)
    exe = REPO_DIR / 'dist' / exe_name
    assets = [a for a in (exe, vj, vsig) if Path(a).exists()]
    print('① 清单: version=%s  附件=%s  size=%s  sha256=%s…'
          % (d.get('version'), exe_name, lau.get('size'), (lau.get('sha256') or '')[:16]))
    if url and ('/v%s/%s' % (d.get('version'), exe_name)) not in url:
        print('  [FAIL] 清单 url 不是「…/releases/download/v<版本>/<附件名>」的形状: %s' % url)
        return 1
    ok = True
    if exe.is_file() and lau.get('sha256'):
        real = hashlib.sha256(exe.read_bytes()).hexdigest()
        same = (real.lower() == str(lau['sha256']).lower()
                and str(lau.get('size')) == str(exe.stat().st_size))
        ok = same
        print('  [%s] 本地这份 %s 与清单说的 sha256/size 一致=%s（清单说 %s B / 文件 %d B）'
              % ('PASS' if same else 'FAIL', exe_name, same,
                 lau.get('size'), exe.stat().st_size))
    elif not exe.is_file():
        print('  [WARN] 本地没有 %s —— 只能验线上两平台的字节，比不了"清单说的对不对"' % exe)
    print('② 附件验真（两个平台各一次）:')
    ok = verify(ver, assets) and ok
    ok = verify_signature(vj, vsig) and ok
    print('验真结果:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    main()
