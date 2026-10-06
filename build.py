# -*- coding: utf-8 -*-
"""
hotnews 打包脚本 —— 产出两种包
  1) hotnews-vX.zip                    源码包（需系统 Python，手动 pip install）
  2) hotnews-vX-绿色版.zip             绿色包（自带 embeddable Python 运行时，解压即用）
"""
import os
import json
import shutil
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
EXCLUDE_DIRS = {"__pycache__", ".git", ".idea", ".vscode", "venv", ".venv", ".build"}
EXCLUDE_FILES = {"build.py", "uvicorn.log", "nohup.out"}
RUNTIME = "runtime"


def version():
    with open(os.path.join(BASE, "version.json"), "r", encoding="utf-8") as f:
        return json.load(f)["version"]


def walk(base, skip_runtime):
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs
                   if d not in EXCLUDE_DIRS and not (skip_runtime and d == RUNTIME)]
        for name in sorted(files):
            # 只排除「包根目录下的分发产物 zip」——切勿一刀切排除所有 .zip：
            # runtime/python313.zip 是 Python 标准库本体（encodings 等都在里面），
            # 排掉它会导致启动直接 Fatal: No module named 'encodings'
            if name in EXCLUDE_FILES:
                continue
            if root == base and name.lower().endswith(".zip"):
                continue
            full = os.path.join(root, name)
            yield full, os.path.relpath(full, base)


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
    ver = version()
    src = os.path.join(BASE, f"hotnews-v{ver}.zip")
    green = os.path.join(BASE, f"hotnews-v{ver}-绿色版.zip")

    n1, s1 = make_zip(src, skip_runtime=True)
    n2, s2 = make_zip(green, skip_runtime=False)

    print(f"源码包  {n1} files  {s1:6.2f} MB  {os.path.basename(src)}")
    print(f"绿色包  {n2} files  {s2:6.2f} MB  {os.path.basename(green)}")
