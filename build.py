# -*- coding: utf-8 -*-
r"""
hotnews 打包脚本 —— 产出两种包
  1) hotnews-vX.zip                    源码包（需系统 Python，手动 pip install）
  2) hotnews-vX-绿色版.zip             绿色包（自带 embeddable Python 运行时，解压即用）

【2026-10-09 修】排除清单原来只有寥寥几个名字，**没有和 `.gitignore` 同步** ——
在维护者本机（仓库根目录堆着历史 exe、调试快照、PyInstaller 中间目录）跑一次
`python build.py`，会把 46 个不该对外分发的文件打进源码包，包括：
  · 含私人邮箱的历史首页快照 `_home.html` / `_home_new.html` / `_home_ifeng.html`；
  · 旧版 exe：`hotnews-v1.4.0.exe`、`热点新闻.exe`（各约 14 MB）、`dist\*.exe`、
    `hotnews_build_archive\*.exe`；
  · PyInstaller 中间目录 `build\` / `build_tmp\`；
  · `_probe*.py` / `_smoke_*.json` 等一次性调试脚本与抓取快照。
现在把 `.gitignore` 的那组模式同步过来（见下面的 EXCLUDE_* 三个常量）。

★ 两个坑，改这里之前务必先读：
  1) **不能一刀切排除 `*.zip`** —— `runtime\python313.zip` 是 Python 标准库本体
     （encodings 等都在里面），排掉它绿色包启动会直接
     `Fatal: No module named 'encodings'`。所以文件级排除只按「模式 + 位置」精确匹配。
  2) **不能一刀切排除 `*.exe`** —— `runtime\python.exe` / `runtime\pythonw.exe`
     是绿色包的内置解释器，排掉同样跑不起来。所以 `runtime\` 目录下的文件
     **一律不受文件级模式影响**（见 walk() 里的 _in_runtime 判断）。
     `runtime\` 本身仍然只在「绿色包」里出现，源码包始终跳过它。
"""
import os
import sys
import json
import shutil
import zipfile
import fnmatch

BASE = os.path.dirname(os.path.abspath(__file__))
# 与 .gitignore 同步（目录级）
EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "venv", ".venv", ".build",
                ".pytest_cache",
                # .gitignore：构建产物与历史归档目录
                "dist", "build", "build_tmp", "runtime_build",
                "hotnews_build_archive", "_shot", "_ico_png"}
# 与 .gitignore 同步（文件名级，仓库根目录里的固定名字）
EXCLUDE_FILES = {"build.py", "uvicorn.log", "nohup.out", "pyi.log"}
# 与 .gitignore 同步（模式级，用 fnmatch）—— 注意别把 *.zip / *.exe 放进来，
# 见文件头「两个坑」：runtime\ 里有必须保留的 python.exe / python313.zip。
EXCLUDE_GLOBS = ("_probe*.py", "_probe*.html", "_check*.py", "_dbg_*.py", "_retry_*.py",
                 "_mefcl_*.py", "_test_collect.py", "_home*.html",
                 "_ifeng_news_home.html", "_commitmsg.txt",
                 "_cn_check.json", "_smoke_*.json", "hotnews_cn_test.json",
                 "_news.opml", "*.log", "*.exe")
RUNTIME = "runtime"


def version():
    with open(os.path.join(BASE, "version.json"), "r", encoding="utf-8") as f:
        return json.load(f)["version"]


def walk(base, skip_runtime):
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs
                   if d not in EXCLUDE_DIRS and not (skip_runtime and d == RUNTIME)]
        _in_runtime = os.path.relpath(root, base).replace("\\", "/").split("/")[0] == RUNTIME
        for name in sorted(files):
            # 只排除「包根目录下的分发产物 zip」——切勿一刀切排除所有 .zip：
            # runtime/python313.zip 是 Python 标准库本体（encodings 等都在里面），
            # 排掉它会导致启动直接 Fatal: No module named 'encodings'
            if name in EXCLUDE_FILES:
                continue
            if root == base and name.lower().endswith(".zip"):
                continue
            # 文件级模式排除（与 .gitignore 同步）。runtime/ 里的文件一律豁免：
            # 那里有绿色包必须带上的 python.exe / pythonw.exe / python313.zip。
            if not _in_runtime and any(fnmatch.fnmatch(name, g) for g in EXCLUDE_GLOBS):
                continue
            full = os.path.join(root, name)
            yield full, os.path.relpath(full, base)


def list_files(skip_runtime=True):
    r"""只列清单、不写盘的「打包模拟」（`python build.py --list` 用它）。

    维护者本机跑打包前可以先核一遍：清单里不应该出现 dist\ / build\ / 历史 exe /
    `_home*.html` 这类不该分发的文件。
    """
    return [(full, arc) for full, arc in walk(BASE, skip_runtime)]


def make_zip(out, skip_runtime):
    if os.path.exists(out):
        os.remove(out)          # 先删旧包，避免被当成源文件重打
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for full, arc in walk(BASE, skip_runtime):
            z.write(full, arc)
            n += 1
    return n, os.path.getsize(out) / 1024 / 1024


if __name__ == "__main__":
    if "--list" in sys.argv:
        # 只打印清单、不写任何文件（打包前核对用，见文件头「两个坑」）
        for _tag, _skip in (("源码包", True), ("绿色包", False)):
            _rows = list_files(skip_runtime=_skip)
            print(f"=== {_tag}（模拟，未写盘）：{len(_rows)} 个文件 ===")
            for _full, _arc in _rows:
                print(f"  {_arc.replace(os.sep, '/')}")
        raise SystemExit(0)

    ver = version()
    src = os.path.join(BASE, f"hotnews-v{ver}.zip")
    green = os.path.join(BASE, f"hotnews-v{ver}-绿色版.zip")

    n1, s1 = make_zip(src, skip_runtime=True)
    n2, s2 = make_zip(green, skip_runtime=False)

    print(f"源码包  {n1} files  {s1:6.2f} MB  {os.path.basename(src)}")
    print(f"绿色包  {n2} files  {s2:6.2f} MB  {os.path.basename(green)}")
