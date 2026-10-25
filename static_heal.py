# -*- coding: utf-8 -*-
# 作者: 海风（kele551）   https://gitcode.com/kele551/hotnews
"""静态界面文件自愈 —— 2026-10-18 事故固化。

事故（用户报「打开没有显示」）：
    程序在后台已经跑了几个小时，一切数据接口都正常；但界面打不开 ——
    因为 PyInstaller 单文件版把 static/ 解包在 %TEMP%\\_MEIxxxx\\ 下，
    系统/管家清理临时目录时把这个解包目录当成垃圾删掉了。
    现场证据：%TEMP%\\_MEI00004b702 里只剩 25 个"正在被内存映射、删不掉"的 dll 与 pyd，
    static/（html/css/js）与 base_library.zip 之外的普通文件全部消失；
    于是 `/` 只能回落到兜底文字，`/static/*` 全部 500，用户看到的就是"什么都没有"。

做法：
    打包态启动时把 static/ 复制到 %LOCALAPPDATA%\\hotnews\\static-<版本>\\，之后一律从这份
    稳定目录读页面与静态资源；%TEMP% 再被清也不会影响界面。
    源码态（未打包）直接用仓库里的 static/，不做复制（改模板立即生效）。

    2026-10-25 补：副本目录名带版本号，正常发版一定会换目录、不会撞；但**同一个版本号重新打包**
    （本机自测、改动还没发版）时目录名不变，老做法"目录里有 index.html 就直接用"会永远读上一次
    那一份 —— 新界面明明在 exe 里带着，打开却一点没变，看起来就是"改了没生效"。
    现在建好副本时在旁边盖一个 .bundle_sha（包内这套界面的内容签名），启动时比对：
      签名对得上 → 这份就是本包这套，原样保留（直接改这份 html 的手工补丁通道因此仍然有效）；
      对不上或没有戳 → 以包为准，被顶掉的那份先备份进 %LOCALAPPDATA%\\hotnews\\界面备份\\（每份留最近 5 个）。
"""
import os
import shutil
import hashlib
import time

LOCAL_DIR_NAME = "hotnews"
# 戳文件：记下"这份本地副本对应的是程序包里哪一套界面"。
# 点开头是避免被当成页面文件（/static 也就能读到它，但里面只有一个散列值）。
STAMP_NAME = ".bundle_sha"
# 只有这些后缀算界面内容；以前手工补丁留下的 index.html.换装前-2356 这类备份不算，
# 否则签名每次都对不上，等于把手工补丁通道顶死。
_SYNC_EXT = (".html", ".css", ".js")


def target_dir(version, localappdata=None):
    """稳定副本的落地目录：%LOCALAPPDATA%\\hotnews\\static-<版本>"""
    base = localappdata or os.getenv("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".hotnews")
    return os.path.join(base, LOCAL_DIR_NAME, "static-" + str(version))


def bundle_sig(d):
    """一套 static/ 的内容签名（文件名 + 字节）。读不到就返回空串。"""
    if not d or not os.path.isdir(d):
        return ""
    h = hashlib.sha256()
    found = 0
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if not os.path.isfile(p) or name == STAMP_NAME:
            continue
        if os.path.splitext(name)[1].lower() not in _SYNC_EXT:
            continue
        try:
            with open(p, "rb") as f:
                b = f.read()
        except Exception:
            return ""
        found += 1
        h.update(name.encode("utf-8"))
        h.update(b"\0")
        h.update(b)
    return h.hexdigest() if found else ""


def _stamp_path(dst):
    return os.path.join(dst, STAMP_NAME)


def read_stamp(dst):
    try:
        with open(_stamp_path(dst), "r", encoding="utf-8") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""


def write_stamp(dst, sig):
    """写戳。失败不影响任何功能（最坏情况就是下次启动多拷一次）。"""
    try:
        tmp = _stamp_path(dst) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(sig)
        os.replace(tmp, _stamp_path(dst))
    except Exception:
        pass


def backup_dir(localappdata=None):
    """被顶掉的本地副本备份到哪儿：%LOCALAPPDATA%\\hotnews\\界面备份\\
    （故意不叫 static-*，否则会被 _prune_old 当成旧版本副本一起清掉）"""
    base = localappdata or os.getenv("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".hotnews")
    return os.path.join(base, LOCAL_DIR_NAME, "界面备份")


def backup_stale(dst, keep=5):
    """把要被替换掉的界面文件挪去备份目录（文件名后面加时间戳），每份最多留 keep 个。
    失败静默 —— 备份写不进去也不能拦住启动（该用包里那套还是要用）。"""
    try:
        bd = backup_dir(os.path.dirname(os.path.dirname(dst)))
        os.makedirs(bd, exist_ok=True)
        # 时间戳带微秒：同一秒里被顶掉两次（自测会这样）也不会把前一份备份覆盖掉
        _now = time.time()
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(_now)) + "%06d" % int((_now % 1) * 1000000)
        for name in sorted(os.listdir(dst)):
            p = os.path.join(dst, name)
            if not os.path.isfile(p) or name == STAMP_NAME:
                continue
            if os.path.splitext(name)[1].lower() not in _SYNC_EXT:
                continue
            try:
                shutil.copy2(p, os.path.join(bd, "%s.%s" % (name, ts)))
            except Exception:
                pass
        # 每个原文件名下按时间戳分组，只留最近 keep 份（时间戳在名字末尾，字典序=时间序）
        groups = {}
        for fn in sorted(os.listdir(bd)):
            if not os.path.isfile(os.path.join(bd, fn)) or "." not in fn:
                continue
            head = fn.rsplit(".", 1)[0]
            groups.setdefault(head, []).append(fn)
        for head, names in groups.items():
            for extra in names[:-keep] if keep > 0 else []:
                try:
                    os.remove(os.path.join(bd, extra))
                except Exception:
                    pass
    except Exception:
        pass


def needs_refresh(src, dst):
    """本地副本存在时，要不要用包里的这套覆盖它。

    【为什么必须有这一步】稳定副本的目录名带着版本号，正常发版一定会换目录，不会撞；
    但**同一个版本号重新打包**（本机自测、改动还没发版）时目录名不变，
    老做法"目录里有 index.html 就直接用"会永远读上一次那一份 ——
    新界面明明在 exe 里带着，打开页面却一点没变，看起来就是"改了没生效"。

    手工补丁通道（直接改 %LOCALAPPDATA% 里那份 html，不用重打包）不能因此失效：
      · 戳 == 本包签名 → 这份副本就是本包这套（可能还带手工改动）→ 原样保留，不动它；
      · 戳不一样或没有戳 → 包里的界面更新了（或这份是老版本留下的）→ 以包为准，
        被顶掉的那份先备份进「界面备份」目录，随时能换回去。
    """
    sig = bundle_sig(src)
    if not sig:
        return False                    # 包里的读不到（例如 %TEMP% 被清）：绝不动本地副本
    cur = read_stamp(dst)
    if cur and cur == sig:
        return False                    # 已经是本包这套（含手工补丁）：保留
    return True


def _has_index(d):
    return bool(d) and os.path.isfile(os.path.join(d, "index.html"))


def _prune_old(dst):
    """清掉旧版本留下的 static-* 副本（只留当前这份），失败静默。"""
    try:
        root = os.path.dirname(dst)
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if name.startswith("static-") and p != dst and os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def sync_static(src, dst):
    """把 src 整棵目录同步到 dst（先写 .tmp 再整体改名，避免复制一半被当成完整）。

    返回实际可用的目录：成功 → dst；失败或缺 index.html → src
    （宁可退回解包目录，也不能让页面彻底没了）。
    """
    try:
        if _has_index(dst):
            return dst
        if not _has_index(src):
            return src
        tmp = dst + ".tmp"
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(src, tmp)
        if os.path.isdir(dst):
            shutil.rmtree(dst, ignore_errors=True)
        os.replace(tmp, dst)
        if _has_index(dst):
            # 副本刚建好：盖上"我对应包里哪一套界面"的戳（见 needs_refresh）
            sig = bundle_sig(src)
            if sig:
                write_stamp(dst, sig)
            return dst
        return src
    except Exception as e:  # 复制失败不影响启动，只是回到旧行为
        print(f"[warn] 静态目录复制失败，继续用解包目录：{type(e).__name__}: {e}")
        return src


def ensure_static(static_dir, base_dir, version, frozen,
                  localappdata=None, prune_old=True):
    """返回本次运行该用的静态目录。

    frozen=False（源码态）→ 原样返回 static_dir。
    frozen=True（打包态）→ 优先用 %LOCALAPPDATA% 的稳定副本，没有再复制一份，
                           复制不了就退回 static_dir / <exe 同级>\\static。
    """
    if not frozen:
        return static_dir
    src = static_dir
    if not _has_index(src):
        alt = os.path.join(base_dir or "", "static")
        if _has_index(alt):
            src = alt
    dst = target_dir(version, localappdata)
    # 【2026-10-25 修】包里这套界面和本地副本对不上（同一版本号重新打包 / 老目录没戳）
    # → 以包为准，被顶掉的那份先进「界面备份」目录。判据与保留手工补丁的规矩见 needs_refresh。
    if _has_index(dst) and needs_refresh(src, dst):
        backup_stale(dst)
        shutil.rmtree(dst, ignore_errors=True)
    got = sync_static(src, dst)
    if got == dst and not read_stamp(dst):
        # 以前建好的目录没有戳（这次没覆盖它，例如包里的 static 读不到）：
        # 只有确实能拿到本包签名时才补盖，否则宁可下次再判一遍，别乱写一个空戳。
        sig = bundle_sig(src)
        if sig:
            write_stamp(dst, sig)
    if prune_old and got == dst:
        _prune_old(dst)
    return got
