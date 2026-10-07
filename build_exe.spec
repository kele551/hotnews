# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 单文件打包配置：产出 dist/热点新闻.exe（文件名不含版本号）

版本资源：从 version.json 读版本号，署名常量在下面，自动生成 version_info.txt 写进 exe，
这样「鼠标指向 exe 显示版本号」，文件属性里也带署名与仓库地址，且版本号与 version.json 永远一致。

【2026-10-07 署名合规修正】
  1) 原来把私人邮箱写进了 exe 属性 —— 违反「不要在对外文档/文件里写私人邮箱」
     （会被爬虫抓去发垃圾邮件），已删除，改为写仓库地址。
  2) CompanyName / LegalCopyright 按署名规矩统一成 ASCII 的 HaiFeng (kele551)，
     与「微软壁纸助手」「狐径」两个程序保持一致。
  3) StringTable 的语言 ID 原来是 0409(en-US)，但 Translation 写的是 0804(zh-CN)，
     两者不一致会导致部分系统上版本信息显示异常，已统一为 080404B0。
"""
import os
import json

BASE = SPECPATH

APP_AUTHOR = "海风（kele551）"
APP_COMPANY = "HaiFeng (kele551)"
APP_COPYRIGHT = "Copyright (C) 2026 HaiFeng (kele551)"
APP_REPO = "gitee.com/kele551/hotnews"
APP_DESC = "热点新闻检索"
APP_NAME = "热点新闻检索"

# utf-8-sig：用 PowerShell 的 Set-Content -Encoding UTF8 写出来的 version.json 会带 BOM，
# 按 utf-8 读会直接 JSONDecodeError（打包当场失败）。读的时候容忍 BOM。
_v = json.load(open(os.path.join(BASE, "version.json"), encoding="utf-8-sig"))["version"]
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
        u'080404B0',
        [StringStruct(u'FileDescription', u'{APP_DESC}'),
         StringStruct(u'FileVersion', u'{_v}.0'),
         StringStruct(u'InternalName', u'hotnews'),
         StringStruct(u'CompanyName', u'{APP_COMPANY}'),
         StringStruct(u'LegalCopyright', u'{APP_COPYRIGHT}'),
         StringStruct(u'OriginalFilename', u'热点新闻.exe'),
         StringStruct(u'ProductName', u'{APP_NAME}'),
         StringStruct(u'ProductVersion', u'{_v}.0'),
         StringStruct(u'Comments', u'{APP_REPO}')]
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
    # 通知区图标：托盘用它做首选来源（取不到就退回 exe 自身资源），
    # 否则打包后 _MEIPASS 与 exe 同级都找不到 hotnews.ico
    (os.path.join(BASE, "hotnews.ico"), "."),
]

a = Analysis(
    [os.path.join(BASE, "app.py")],
    pathex=[BASE],
    binaries=[],
    datas=datas,
    # 【必须列在这里】_ed25519 是在函数里 import 的，PyInstaller 静态分析扫不到，
    # 不列进来打包后就会 ModuleNotFoundError，验签会失败（fail-closed 会直接拒绝升级）。
    # 同理 feedparser 必须保持模块级 import，别再改成懒加载。
    hiddenimports=["_ed25519",
                   "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
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
