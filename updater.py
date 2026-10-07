# -*- coding: utf-8 -*-
"""
hotnews 自动更新模块
--------------------------------------------------------------
版本来源：Gitee 仓库的 version.json（国内下载稳定，GitHub 仅作镜像）
  https://raw.giteeusercontent.com/kele551/hotnews/raw/master/version.json

version.json 结构（含 launcher 字段才支持自动下载）：
  {
    "version": "1.2.0",
    "launcher": {
      "url":    "https://gitee.com/kele551/hotnews/releases/download/v1.2.0/hotnews-v1.2.0.exe",
      "sha256": "大写十六进制，64 位",
      "size":   14234524
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

# 国内下载源：Gitee（raw.giteeusercontent.com 是 Gitee raw 的实际存储域名，国内直连稳定）
REMOTE_VERSION_URL = "https://raw.giteeusercontent.com/kele551/hotnews/raw/master/version.json"
REMOTE_VERSION_URL_ALT = "https://gitee.com/kele551/hotnews/raw/master/version.json"

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
            # trust_env=False：不走系统/环境代理，直连 Gitee 更快更稳（同 server._fetch）
            r = httpx.get(url, timeout=5, follow_redirects=True, trust_env=False,
                          headers={"User-Agent": "hotnews-updater"})
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    return None


def check(silent=True):
    """
    检查是否有更新。返回状态：
      {"status": "latest"|"update"|"error"|"checking", "remote": <ver>, "message": <str>}
    非阻塞：若已有检查在进行，直接返回 checking，避免手动点按钮被后台检查卡住。
    """
    if not _lock.acquire(blocking=False):
        return {"status": "checking", "remote": _state.get("remote"),
                "message": "检查进行中，请稍候"}
    try:
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
    finally:
        _lock.release()


def _res(rel):
    """资源定位：兼容 PyInstaller 单文件（_MEIPASS 解包目录）与源码目录。
    单文件 exe 运行时 version.json 在 _MEIPASS，不在 exe 同级目录，必须用这个定位。"""
    cands = []
    if getattr(sys, "_MEIPASS", None):
        cands.append(os.path.join(sys._MEIPASS, rel))
    cands.append(os.path.join(BASE_DIR, rel))
    for p in cands:
        if os.path.exists(p):
            return p
    return cands[-1]


def _read_local_version():
    """本地版本，跟 server.VERSION 同源（读 version.json，兜底 1.1.0）。
    必须用 _res 定位：单文件 exe 的 version.json 在 _MEIPASS，直接按 BASE_DIR 读会找不到
    → 误判 1.1.0 → 远端 1.2.1 永远大于它 → 已是最新也提示有更新（2026-10-06 修复）。"""
    try:
        with open(_res("version.json"), "r", encoding="utf-8") as f:
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
    total = int(launcher.get("size", 0) or 0)
    expect = (launcher.get("sha256") or "").upper()

    # 【2026-10-07 修 严重 bug】本地已有且校验通过的包，**直接复用，绝不重下**。
    # 原实现每次 check 都用 "wb" 打开目标文件重新下载 —— "wb" 会**先把已下好的包清空**，
    # 一旦这次下载中途被打断（或用户在下载途中点了安装），留下的是一个残缺文件，
    # 而替换脚本会把它原样覆盖到正在用的 exe 上，**整个程序当场报废**（真实发生过：
    # 14.4MB 的程序被 1.2MB 的半个包覆盖，版本号还显示新的，但根本启动不了）。
    if os.path.exists(tmp_exe):
        same_size = (not total) or os.path.getsize(tmp_exe) == total
        same_sha = (not expect) or _sha256(tmp_exe) == expect
        if same_size and same_sha:
            _state.update(status="ready", message="新版本已下载，退出程序时自动安装",
                          latest=remote)
            _write_replace_vbs(tmp_exe)
            return True, "新版本已就绪"

    # 下载到 .part，**校验全部通过后才原子改名** —— 中途任何失败都不会碰已有的包
    part = tmp_exe + ".part"
    try:
        try:
            os.remove(part)
        except Exception:
            pass
        with httpx.stream("GET", url, timeout=60, follow_redirects=True,
                          headers={"User-Agent": "hotnews-updater"}) as r:
            if r.status_code != 200:
                return False, f"下载失败 HTTP {r.status_code}"
            got = 0
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(65536):
                    f.write(chunk)
                    got += len(chunk)
            if total and got != total:
                os.remove(part)
                return False, f"下载不完整 {got}/{total} 字节"
        if expect:
            actual = _sha256(part)
            if actual != expect:
                os.remove(part)
                return False, "校验失败，文件可能被篡改"
        os.replace(part, tmp_exe)          # 原子替换，Windows 上是同盘改名
    except Exception as ex:
        try:
            os.remove(part)
        except Exception:
            pass
        return False, f"下载失败: {type(ex).__name__}"

    # 写替换脚本：等待主程序退出 → 覆盖 → 重启（VBS 由 wscript 跑，无黑窗）
    _write_replace_vbs(tmp_exe)
    _state.update(status="ready", message="新版本已下载，退出程序时自动安装",
                  latest=remote)
    return True, "新版本已就绪"


def _write_replace_vbs(new_exe):
    """
    生成替换脚本（VBScript），由 wscript.exe 以 GUI 方式运行 —— 绝不分配控制台窗口。

    弃用 powershell.exe 的原因：powershell 是控制台型程序，-File 模式下
    -WindowStyle Hidden 在本机仍会闪出黑窗，加上脚本末尾 Start-Process 拉起的新进程
    又是一个控制台，于是「两个小黑窗」。wscript.exe 属于 GUI 子系统，天生无控制台，
    从根上杜绝黑窗；末尾 sh.Run 拉起的也是 console=False 的 exe，同样无控制台。
    """
    vbs = os.path.join(tempfile.gettempdir(), "hotnews_update.vbs")
    target = EXE_PATH
    # VBS 字符串用双引号；Windows 路径是反斜杠，与 VBS 字符串不冲突，直接拼入即可。
    script = (
        'On Error Resume Next\n'
        'Set fso = CreateObject("Scripting.FileSystemObject")\n'
        'Set sh = CreateObject("WScript.Shell")\n'
        'WScript.Sleep 2000\n'
        'ok = False\n'
        'For i = 1 To 30\n'
        '  fso.CopyFile "' + new_exe + '", "' + target + '", True\n'
        '  If Err.Number = 0 Then ok = True: Exit For\n'
        '  Err.Clear\n'
        '  WScript.Sleep 1000\n'
        'Next\n'
        'If ok Then\n'
        '  sh.Run Chr(34) & "' + target + '" & Chr(34), 1, False\n'
        'End If\n'
        'On Error GoTo 0\n'
    )
    # 【2026-10-07 修 严重 bug】原来这里写的是 utf-8-sig（带 BOM），注释还写着
    # 「wscript 才能正确识别」—— 恰好写反了：**Windows Script Host 不认带 BOM 的 UTF-8**，
    # 会直接报「无效字符（1,1）800A0408」，脚本根本不执行 → 在线升级从来没成功过，
    # 用户每次都会看到那个 Windows Script Host 错误弹窗。
    # 实测（cscript 逐种编码跑）：utf-8-sig ✗ / utf-16 ✓ / ansi(gbk) ✓ / utf-8无BOM 中文会乱。
    # WSH 原生支持 UTF-16 的 .vbs，且中文路径也正确，所以用 utf-16。
    with open(vbs, "w", encoding="utf-16", newline="\r\n") as f:
        f.write(script)
    return vbs


def _write_replace_bat(new_exe):
    """兜底方案：.bat（GBK 编码，cmd 按 OEM 代码页读，中文路径不会乱）。

    只在 .vbs 那条路走不通时使用。用 CREATE_NO_WINDOW 拉起，不会闪黑窗。
    """
    bat = os.path.join(tempfile.gettempdir(), "hotnews_update.bat")
    target = EXE_PATH
    script = (
        "@echo off\r\n"
        "ping -n 3 127.0.0.1 >nul\r\n"
        "for /l %%i in (1,1,30) do (\r\n"
        '  copy /y "' + new_exe + '" "' + target + '" >nul 2>&1 && goto done\r\n'
        "  ping -n 2 127.0.0.1 >nul\r\n"
        ")\r\n"
        "goto :eof\r\n"
        ":done\r\n"
        'start "" "' + target + '"\r\n'
    )
    enc = "mbcs" if os.name == "nt" else "utf-8"
    with open(bat, "w", encoding=enc, newline="") as f:
        f.write(script)
    return bat


def trigger_replace():
    """立即执行替换脚本（主程序退出前调用）。wscript.exe 跑 VBS，GUI 宿主无黑窗。

    【2026-10-07】加了 .bat 兜底：万一 .vbs 那条路走不通（脚本缺失/被占用/wscript 不可用），
    就用 cmd /c + CREATE_NO_WINDOW 跑 .bat，同样不会闪黑窗，保证升级一定能落地。
    """
    CREATE_NO_WINDOW = 0x08000000
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0                # SW_HIDE

    def _spawn(argv):
        subprocess.Popen(
            argv, creationflags=CREATE_NO_WINDOW, startupinfo=si, shell=False,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # 【2026-10-07】替换前**再校验一次升级包**：尺寸对不上或 sha256 不符就拒绝替换。
    # 这是最后一道闸 —— 宁可不升级，也绝不能把残缺文件覆盖到正在用的程序上。
    pkg = os.path.join(tempfile.gettempdir(), "hotnews_update.exe")
    # 注意：_state["remote"] 存的是**版本号字符串**，远端字典在 latest 里
    _rm = _state.get("latest") or {}
    _lau = _rm.get("launcher") or {}
    exp_sha = (_lau.get("sha256") or "").upper()
    exp_size = int(_lau.get("size", 0) or 0)
    if not os.path.exists(pkg):
        return False
    if exp_size and os.path.getsize(pkg) != exp_size:
        _state.update(status="error", message="升级包不完整，已放弃替换")
        return False
    if exp_sha and _sha256(pkg) != exp_sha:
        _state.update(status="error", message="升级包校验失败，已放弃替换")
        return False

    vbs = os.path.join(tempfile.gettempdir(), "hotnews_update.vbs")
    if os.path.exists(vbs):
        try:
            _spawn(["wscript.exe", vbs])
            return True
        except Exception:
            pass
    bat = os.path.join(tempfile.gettempdir(), "hotnews_update.bat")
    if os.path.exists(bat):
        try:
            _spawn(["cmd.exe", "/c", bat])
            return True
        except Exception:
            pass
    return False
