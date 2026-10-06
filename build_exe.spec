# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件打包配置：产出 dist/热点新闻.exe（文件名不含版本号）

版本资源：从 version.json 读版本号，作者/邮箱为常量，自动生成 version_info.txt 写进 exe，
这样「鼠标指向 exe 显示版本号」，文件属性里也注明作者与联系邮箱，且版本号与 version.json 永远一致。
"""
import os
import json

BASE = SPECPATH

APP_AUTHOR = "海风"
APP_EMAIL = "75219857@qq.com"
APP_DESC = "热点新闻检索"
APP_NAME = "热点新闻检索"

_v = json.load(open(os.path.join(BASE, "version.json"), encoding="utf-8"))["version"]
_parts = _v.split(".")
while len(_parts) < 4:
    _parts.append("0")
_filevers = tuple(int(x) for x in _parts[:4])
_prodvers = _filevers

version_info_text = f'''# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={_filevers},
    prodvers={_prodvers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        u'040904B0',
        [StringStruct(u'FileDescription', u'{APP_DESC}'),
         StringStruct(u'FileVersion', u'{_v}.0'),
         StringStruct(u'InternalName', u'hotnews'),
         StringStruct(u'CompanyName', u'{APP_AUTHOR}'),
         StringStruct(u'LegalCopyright', u'Copyright (C) 2026 {APP_AUTHOR}'),
         StringStruct(u'OriginalFilename', u'热点新闻.exe'),
         StringStruct(u'ProductName', u'{APP_NAME}'),
         StringStruct(u'ProductVersion', u'{_v}.0'),
         StringStruct(u'Comments', u'作者：{APP_AUTHOR}  联系邮箱：{APP_EMAIL}')]
      )
      ]
    ),
    VarFileInfo([VarStruct(u'Translation', [0x804, 0x4B0])])
  ]
)
'''
_version_info_path = os.path.join(BASE, "build_tmp", "version_info.txt")
os.makedirs(os.path.dirname(_version_info_path), exist_ok=True)
with open(_version_info_path, "w", encoding="utf-8") as f:
    f.write(version_info_text)

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
    name="热点新闻",               # 产出 热点新闻.exe，文件名不带版本号
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,                # 不弹黑窗口；日志走 hotnews.log，退出走页面按钮
    version=_version_info_path,   # 嵌入版本资源：悬停显示版本号 + 作者/邮箱
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(BASE, "hotnews.ico"),
)
