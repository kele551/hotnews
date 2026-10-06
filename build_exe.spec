# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件打包配置：产出 dist/hotnews.exe"""
import os

# spec 被 exec 执行，没有 __file__；PyInstaller 会注入 SPECPATH
BASE = SPECPATH

# (源, 目标目录) —— static 必须打进去；config.json 也带上作为默认配置模板
datas = [
    (os.path.join(BASE, "static"), "static"),
    (os.path.join(BASE, "config.json"), "."),
    (os.path.join(BASE, "version.json"), "."),
]

a = Analysis(
    [os.path.join(BASE, "app.py")],
    pathex=[BASE],
    binaries=[],
    datas=datas,
    hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                   "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PIL", "numpy", "pandas", "matplotlib", "PyInstaller",
              "setuptools", "pip", "distutils", "pydoc", "doctest", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="hotnews",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,         # 不弹黑窗口；日志走 hotnews.log，退出走页面按钮
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(BASE, "hotnews.ico"),
)
