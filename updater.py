# -*- coding: utf-8 -*-
"""
hotnews 自动更新模块
--------------------------------------------------------------
版本来源：GitHub raw 上的 version.json
  https://raw.githubusercontent.com/kele551/hotnews/main/version.json

version.json 结构（含 launcher 字段才支持自动下载）：
  {
    "version": "1.2.0",
    "launcher": {
      "url":    "https://github.com/kele551/hotnews/releases/download/v1.2.0/热点新闻.exe",
      "sha256": "大写十六进制，64 位",
      "size":   14226182
    },
    "notes": "更新说明（可选）"
  }

流程：
  1) 拉远端 version.json（超时短，失败静默，不影响正常使用）
  2) 对比本地 VERSION，若远端 > 本地 且带 launcher → 进入下载
  3) 下载 exe 到临时文件 → 校验 sha256（不符则丢弃）
  4) exe 正在运行不能覆盖自己 → 写一个 .bat 到临时目录，
     等进程退出后由它执行「替换 + 重启」，或用户点「立即重启」时触发
"""
import os
import sys
import json
import hashlib
import subprocess
import tempfile
import threading
import urllib.parse

try:
    import httpx
except ImportError:
    httpx = None

REMOTE_VERSION_URL = "https://raw.githubusercontent.com/kele551/hotnews/main/version.json"
# 国内直连 raw.githubusercontent 可能被墙，回落到 jsDelivr CDN 镜像
REMOTE_VERSION_URL_ALT = "https://cdn.jsdelivr.net/gh/kele551/hotnews@main/version.json"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    EXE_PATH = os.path.abspath(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    EXE_PATH = None  # 源码模式不支持自替换，只做版本提示

_lock = threading.Lock()
_state = {"status": "idle", "message": "", "remote": None, "latest": None}


def _version_key(v):
    """'1.2.0' -> (1, 2, 0)，用于可比较的版本号"""
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except Exception:
        return (0, 0, 0)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def fetch_remote():
    """拉远端 version.json，返回 dict 或 None（失败静默）"""
    if httpx is None:
        return None
    for url in (REMOTE_VERSION_URL, REMOTE_VERSION_URL_ALT):
        try:
            r = httpx.get(url, timeout=8, follow_redirects=True,
                          headers={"User-Agent": "hotnews-updater"})
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    return None


def check(silent=True):
    """
    检查是否有更新。返回状态：
      {"status": "latest"|"update"|"error", "remote": <ver>, "message": <str>}
    """
    with _lock:
        try:
            remote = fetch_remote()
            if not remote or not remote.get("version"):
                _state.update(status="error", message="无法连接更新源", remote=None)
                return _state.copy()
            rv = remote["version"]
            local = _read_local_version()
            _state["remote"] = rv
            if _version_key(rv) > _version_key(local):
                _state.update(status="update", message=f"发现新版本 v{rv}",
                              latest=remote)
            else:
                _state.update(status="latest", message=f"已是最新 v{local}", latest=None)
            return _state.copy()
        except Exception as ex:
            _state.update(status="error", message=f"检查失败: {type(ex).__name__}", remote=None)
            return _state.copy()


def _read_local_version():
    """本地版本，跟 server.VERSION 同源（读 version.json，兜底 1.1.0）"""
    try:
        with open(os.path.join(BASE_DIR, "version.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("version", "1.1.0")
    except Exception:
        return "1.1.0"


def download_and_prepare(remote):
    """
    下载新版 exe 到临时目录并校验，成功后生成替换脚本。
    返回 (ok, message)。不会立即替换——exe 运行中覆盖不了自己。
    """
    if httpx is None or not EXE_PATH:
        return False, "当前非打包环境，无法自动更新"
    launcher = remote.get("launcher") or {}
    url = launcher.get("url")
    if not url:
        return False, "远端未提供下载地址"

    tmp_exe = os.path.join(tempfile.gettempdir(), "hotnews_update.exe")
    try:
        with httpx.stream("GET", url, timeout=60, follow_redirects=True,
                          headers={"User-Agent": "hotnews-updater"}) as r:
            if r.status_code != 200:
                return False, f"下载失败 HTTP {r.status_code}"
            total = int(launcher.get("size", 0) or 0)
            got = 0
            with open(tmp_exe, "wb") as f:
                for chunk in r.iter_bytes(65536):
                    f.write(chunk)
                    got += len(chunk)
            if total and got != total:
                os.remove(tmp_exe)
                return False, f"下载不完整 {got}/{total} 字节"
    except Exception as ex:
        try:
            os.remove(tmp_exe)
        except Exception:
            pass
        return False, f"下载失败: {type(ex).__name__}"

    # 校验 sha256
    expect = (launcher.get("sha256") or "").upper()
    if expect:
        actual = _sha256(tmp_exe)
        if actual != expect:
            os.remove(tmp_exe)
            return False, "校验失败，文件可能被篡改"

    # 写替换脚本：杀掉旧进程 → 覆盖 → 重启
    _write_replace_bat(tmp_exe)
    _state.update(status="ready", message="新版本已下载，退出程序时自动安装",
                  latest=remote)
    return True, "新版本已就绪"


def _write_replace_bat(new_exe):
    """生成替换脚本。脚本会在下次退出/重启时执行。"""
    bat = os.path.join(tempfile.gettempdir(), "hotnews_update.bat")
    target = EXE_PATH
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "title hotnews 更新",
        # 等待旧进程退出
        ":waitloop",
        "tasklist /FI \"IMAGENAME eq hotnews.exe\" 2>nul | find /I \"hotnews.exe\" >nul",
        "if not errorlevel 1 (",
        "  timeout /t 1 /nobreak >nul",
        "  goto waitloop",
        ")",
        f'copy /Y "{new_exe}" "{target}" >nul',
        "if errorlevel 1 (",
        "  echo 替换失败，可能文件被占用",
        "  timeout /t 5 >nul",
        "  exit /b 1",
        ")",
        # 清理临时文件与脚本自身
        f'del /F /Q "{new_exe}" >nul 2>nul',
        'start "" "{target}"',
        "del /F /Q \"%~f0\" >nul 2>nul",
        "exit /b 0",
    ]
    with open(bat, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\n".join(lines))
    return bat


def trigger_replace():
    """立即执行替换脚本（在退出前调用，用 CREATE_NEW_PROCESS_GROUP 不弹窗）"""
    bat = os.path.join(tempfile.gettempdir(), "hotnews_update.bat")
    if not os.path.exists(bat):
        return False
    try:
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen(
            ["cmd.exe", "/c", bat],
            creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
            shell=False,
        )
        return True
    except Exception:
        return False
