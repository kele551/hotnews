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
"""
import os
import shutil

LOCAL_DIR_NAME = "hotnews"


def target_dir(version, localappdata=None):
    """稳定副本的落地目录：%LOCALAPPDATA%\\hotnews\\static-<版本>"""
    base = localappdata or os.getenv("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".hotnews")
    return os.path.join(base, LOCAL_DIR_NAME, "static-" + str(version))


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
        return dst if _has_index(dst) else src
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
    got = sync_static(src, dst)
    if prune_old and got == dst:
        _prune_old(dst)
    return got
