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
import shutil
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
# 【2026-10-09 修 低-4】GitHub 侧作为**第三候选**兜底：Gitee 万一取不到
# （网络不通 / 内容审查 451 / 仓库临时维护），还能落到 GitHub 的 raw。
# 三个候选都拿到 **原始字节** 后统一走同一套 Ed25519 验签，验不过就换下一个 ——
# 所以加这个候选不会削弱升级安全（没有私钥照样签不出来）。
# ⚠ GitHub 侧必须也放**完整版** version.json（带 launcher）+ 与之配套的 version.json.sig，
#   否则验签必失败。现在 GitHub 仓库里那份是精简版（只有 version 字段），
#   也就是说这个兜底**要等维护者把完整版同步到 GitHub 之后才真正生效**（见 README）。
REMOTE_VERSION_URL_GH = "https://raw.githubusercontent.com/kele551/hotnews/main/version.json"

# 【2026-10-07 升级通道加固】离线生成的 Ed25519 公钥；私钥只存在作者机器的
# F:\Harness\secrets 里，绝不进仓库。客户端用它验证 version.json 的签名，
# 这样即便 Gitee 账号被盗也推不了恶意版本（详见 fetch_remote）。
UPDATE_PUBKEY = "f6931bd24749559c36fc14aa8fcdedd638502fec39bb010ef505a120f9a2dfbd"

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    EXE_PATH = os.path.abspath(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    EXE_PATH = None  # 源码模式不支持自替换，只做版本提示

_lock = threading.Lock()
_state = {"status": "idle", "message": "", "remote": None, "latest": None}
# 【2026-10-09 修 中-4】替换/校验失败时的**给用户看的原因**（server 会原样回给页面）。
# 升级包名与「替换回执」文件路径也提上来当常量，VBS / BAT / Python 三处引用同一份。
UPDATE_PKG = os.path.join(tempfile.gettempdir(), "hotnews_update.exe")
RESULT_FILE = os.path.join(tempfile.gettempdir(), "hotnews_update.result")


def last_message():
    """最近一次操作给用户看的原因（server.py 的 /api/update/install 用它回前端）。"""
    return (_state.get("message") or "").strip()


def state():
    """当前更新状态的一份快照（只读）。server 用它判断"有没有待安装的新版本"。"""
    return dict(_state)


def _silent_run():
    """本进程是不是「后台静默实例」。替换后拉起新版本时要**把 --silent 一起带过去**，
    否则开机自启的后台实例升级完会莫名其妙弹出一个浏览器窗口。
    与 app.py / server.py 的判断同源（argv 与环境变量都认）。"""
    try:
        return ("--silent" in sys.argv) or (os.environ.get("HOTNEWS_SILENT") == "1")
    except Exception:
        return False


def _relaunch_args():
    """替换脚本/新进程要带的命令行参数（保持静默属性 + 告诉新实例这是升级交接）。"""
    args = []
    if _silent_run():
        args.append("--silent")
    args.append("--handoff")
    return args


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
    """拉远端 version.json，**验签通过**才返回 dict；失败/被篡改返回 None。

    【2026-10-07 升级通道加固】原来只校验 exe 的 sha256，而 sha256 和文件
    放在同一个地方 —— Gitee 账号一旦被盗，攻击者可以同时替换 exe 和 sha256，
    客户端会乖乖下载并执行恶意程序（供应链投毒）。
    现在 version.json 必须带一份用**离线私钥**做的 Ed25519 签名
    （同目录的 version.json.sig），客户端用内置公钥验签：
    没有私钥就签不出合法签名，账号被盗也推不了恶意版本。
    """
    if httpx is None:
        return None
    # 【2026-10-09 修 低-4】三个候选按顺序试：Gitee 主地址 → Gitee 备用地址 → GitHub 兜底。
    # 每个都要求「HTTP 200 + Ed25519 验签通过」才采用，任一环节不过就换下一个。
    for url in (REMOTE_VERSION_URL, REMOTE_VERSION_URL_ALT, REMOTE_VERSION_URL_GH):
        try:
            # trust_env=False：不走系统/环境代理，直连 Gitee 更快更稳（同 server._fetch）
            r = httpx.get(url, timeout=5, follow_redirects=True, trust_env=False,
                          headers={"User-Agent": "hotnews-updater"})
            if r.status_code != 200:
                continue
            raw = r.content                      # 验签必须用**原始字节**，不能用重新序列化的
            ok, why = _verify_sig(raw, url + ".sig")
            if not ok:
                print(f"[warn] 升级源验签失败（{why}），已忽略这次更新信息")
                continue
            return json.loads(raw.decode("utf-8"))
        except Exception:
            continue
    return None


def _verify_sig(raw, sig_url):
    """校验 version.json 的 Ed25519 签名。返回 (是否通过, 原因)。"""
    try:
        import _ed25519
    except Exception:
        return False, "验签模块缺失"    # fail-closed：宁可不让升级，也绝不放行未验签的更新
    try:
        s = httpx.get(sig_url, timeout=5, follow_redirects=True, trust_env=False,
                      headers={"User-Agent": "hotnews-updater"})
        if s.status_code != 200:
            return False, f"拿不到签名 HTTP {s.status_code}"
        sig = bytes.fromhex(s.text.strip())
        if _ed25519.checkvalid(sig, raw, bytes.fromhex(UPDATE_PUBKEY)):
            return True, ""
        return False, "签名不匹配"
    except Exception as ex:
        return False, f"{type(ex).__name__}"


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

    tmp_exe = UPDATE_PKG
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
            _write_replace_scripts(tmp_exe)
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
    # 【2026-10-09 修 低-5】.bat 与 .vbs **一起写出**：原来只写 .vbs，
    # 而 trigger_replace() 的兜底分支一直在找 %TEMP%\hotnews_update.bat —— 那条兜底永远不可达。
    _write_replace_scripts(tmp_exe)
    _state.update(status="ready", message="新版本已下载，退出程序时自动安装",
                  latest=remote)
    return True, "新版本已就绪"


def _write_replace_scripts(new_exe):
    """同时写好 VBS 与 BAT 两个替换脚本。

    【2026-10-09 修 低-5】`_write_replace_bat` 以前没有任何调用方（死代码），
    而 `trigger_replace()` 的 VBS 兜底分支又依赖 %TEMP%\\hotnews_update.bat 存在 ——
    两边对不上，导致"VBS 走不通时改用 BAT"这条承诺在代码里根本不成立。
    这个函数把两个脚本一次写好，并返回 (vbs路径, bat路径)（写不出来的那个为空串）。
    """
    vbs = bat = ""
    try:
        vbs = _write_replace_vbs(new_exe)
    except Exception as ex:
        print(f"[warn] 写替换脚本(.vbs)失败: {type(ex).__name__}")
    try:
        bat = _write_replace_bat(new_exe)
    except Exception as ex:
        print(f"[warn] 写替换脚本(.bat)失败: {type(ex).__name__}")
    return vbs, bat


def _write_replace_vbs(new_exe):
    """
    生成替换脚本（VBScript），由 wscript.exe 以 GUI 方式运行 —— 绝不分配控制台窗口。

    弃用 powershell.exe 的原因：powershell 是控制台型程序，-File 模式下
    -WindowStyle Hidden 在本机仍会闪出黑窗，加上脚本末尾 Start-Process 拉起的新进程
    又是一个控制台，于是「两个小黑窗」。wscript.exe 属于 GUI 子系统，天生无控制台，
    从根上杜绝黑窗；末尾 sh.Run 拉起的也是 console=False 的 exe，同样无控制台。

    【2026-10-09 修 中-4】补上**失败回执**与**失败时拉起旧版本**：
    这条链路的问题在于"主程序已经把控制权交出去、随后就退出了"，
    一旦 30 次复制全部失败，以前是静默什么都不做 → 用户看到的是"程序消失"。
    现在无论成功失败都会：
      · 把结果写进 %TEMP%\\hotnews_update.result（下次启动时由 cleanup_leftovers 读出来报给用户）；
      · **失败时把原来的 exe 重新拉起来** —— 旧文件一直没被改动，程序原样还在，绝不"消失"。
    """
    vbs = os.path.join(tempfile.gettempdir(), "hotnews_update.vbs")
    target = EXE_PATH
    _args = " ".join(_relaunch_args())
    _run = 'sh.Run Chr(34) & "' + target + '" & Chr(34)' + (
        (' & " ' + _args + '"') if _args else '')
    # VBS 字符串用双引号；Windows 路径是反斜杠，与 VBS 字符串不冲突，直接拼入即可。
    script = (
        'On Error Resume Next\n'
        'Set fso = CreateObject("Scripting.FileSystemObject")\n'
        'Set sh = CreateObject("WScript.Shell")\n'
        'result = "' + RESULT_FILE + '"\n'
        'WScript.Sleep 2000\n'
        'ok = False\n'
        'errNo = 0\n'
        # 【2026-10-09 升级体验口径】覆盖前先备份一份 exe.old（失败可回退的底气）。
        # 旧文件在下次启动时由 cleanup_leftovers 清掉，不会长期占地方。
        'fso.CopyFile "' + target + '", "' + target + '.old", True\n'
        'Err.Clear\n'
        'For i = 1 To 30\n'
        '  fso.CopyFile "' + new_exe + '", "' + target + '", True\n'
        '  If Err.Number = 0 Then ok = True: Exit For\n'
        '  errNo = Err.Number\n'
        '  Err.Clear\n'
        '  WScript.Sleep 1000\n'
        'Next\n'
        'If ok Then\n'
        '  ok2 = fso.CreateTextFile(result, True)\n'
        '  If Err.Number = 0 Then ok2.WriteLine "OK 新版本已覆盖到位"' + '\n'
        '  Err.Clear\n'
        '  ' + _run + '\n'
        'Else\n'
        '  ok2 = fso.CreateTextFile(result, True)\n'
        '  If Err.Number = 0 Then ok2.WriteLine "FAIL 替换失败（错误号 " & errNo & "），已拉起原版本"' + '\n'
        '  Err.Clear\n'
        # 失败时：原 exe 没被改动过（或已经备份在 .old），把旧版本拉起来，绝不让程序消失
        '  ' + _run + '\n'
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

    【2026-10-09 修 低-5】这个函数以前**从来没有被调用过** —— 而 trigger_replace()
    的兜底分支又一直在找 %TEMP%\\hotnews_update.bat，等于那条兜底永远不可达。
    现在它在 download_and_prepare() 里和 .vbs 一起写出（见那里的调用）。

    【2026-10-09 修 中-4】与 .vbs 一样补上失败回执 + 失败时拉起旧版本，
    保证「替换没成功」也不会出现"程序消失"。
    """
    bat = os.path.join(tempfile.gettempdir(), "hotnews_update.bat")
    target = EXE_PATH
    _args = " ".join(_relaunch_args())
    script = (
        "@echo off\r\n"
        "ping -n 3 127.0.0.1 >nul\r\n"
        "set ok=0\r\n"
        # 【2026-10-09 升级体验口径】覆盖前先备份一份 exe.old（失败可回退）
        'copy /y "' + target + '" "' + target + '.old" >nul 2>&1\r\n'
        "for /l %%i in (1,1,30) do (\r\n"
        '  copy /y "' + new_exe + '" "' + target + '" >nul 2>&1 && set ok=1 && goto done\r\n'
        "  ping -n 2 127.0.0.1 >nul\r\n"
        ")\r\n"
        ":done\r\n"
        "if \"%ok%\"==\"1\" (\r\n"
        '  >"' + RESULT_FILE + '" echo OK 新版本已覆盖到位\r\n'
        ") else (\r\n"
        '  >"' + RESULT_FILE + '" echo FAIL 替换失败（文件被占用或目录不可写），已拉起原版本\r\n'
        ")\r\n"
        'start "" "' + target + '"' + ((" " + _args) if _args else "") + "\r\n"
    )
    enc = "mbcs" if os.name == "nt" else "utf-8"
    with open(bat, "w", encoding=enc, newline="") as f:
        f.write(script)
    return bat


def cleanup_leftovers():
    """启动时清理上次升级留下的 .old / .new 文件（以及过期的升级包）。

    快速替换会把原 exe 改名成 .old 留在原地（运行中的文件删不掉），
    所以要等下次启动、旧文件不再被占用时再删。

    【2026-10-09 修 中-4】现在**返回上一次替换脚本留下的回执文本**（没有则空串）：
    脚本方式替换成功/失败都会写 %TEMP%\\hotnews_update.result，
    启动时读出来（读完即删），server 会把它打进日志、失败时还弹一条气泡。
    这样"替换没成功"不再是一个无声事件。
    """
    receipt = ""
    try:
        exe = EXE_PATH
        if exe:
            for suf in (".old", ".new"):
                p = exe + suf
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
    except Exception:
        pass
    try:
        if os.path.exists(RESULT_FILE):
            with open(RESULT_FILE, "r", encoding="utf-8", errors="ignore") as f:
                receipt = (f.read() or "").strip()
            os.remove(RESULT_FILE)
    except Exception:
        receipt = ""
    return receipt


def _fast_swap(pkg):
    """就地替换并重启：**不依赖任何脚本宿主**，实测约 20 毫秒。

    原理：Windows 允许**重命名**正在运行的 exe（只是不允许覆盖/删除它）。所以：
      1. 升级包先复制到 exe 同目录的 .new（同盘复制 14MB 约 11 毫秒）
      2. 把正在运行的 exe 改名成 .old（约 8 毫秒）
      3. 把 .new 改名成正式名字（同盘改名，原子）
      4. 拉起新 exe，然后本进程退出
    旧做法（写 .vbs 让 wscript 等主程序退出后再复制）要**先睡 2 秒**、再每秒重试，
    慢且依赖脚本宿主 —— 用户反馈「那么小的程序，更新安装太慢」就是它。
    返回 (ok, 说明)。
    """
    exe = EXE_PATH
    if not exe or not os.path.exists(exe):
        return False, "拿不到自身路径"
    folder = os.path.dirname(exe)
    new_p = exe + ".new"
    old_p = exe + ".old"
    try:
        for p in (new_p, old_p):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        shutil.copy2(pkg, new_p)          # 同盘复制，很快
        os.rename(exe, old_p)             # 运行中也能改名
        os.rename(new_p, exe)             # 原子改名到位
    except Exception as ex:
        # 尽力回滚，别把程序弄丢
        try:
            if (not os.path.exists(exe)) and os.path.exists(old_p):
                os.rename(old_p, exe)
        except Exception:
            pass
        return False, f"就地替换失败: {type(ex).__name__}"
    # 拉起新版本（分离进程：本进程随后退出不会带走它）
    #
    # 【2026-10-07 关键修复】必须**剥掉 PyInstaller 自己的环境变量**。
    # 单文件版靠 _MEIPASS2 / _PYI_* 让子进程复用父进程已经解包好的 _MEIxxxx 临时目录。
    # 我们从"正在运行的程序里"拉起新版本时，这些变量会被继承过去 ——
    # 新进程于是**复用老进程马上要删掉的那个临时目录**，证书等数据文件被删掉，
    # 新进程在 import 阶段就崩：ssl.create_default_context → FileNotFoundError。
    # 现象就是"替换成功、程序却不重启"（老进程退了、新的没起来）。
    # 手动双击没问题，是因为那时环境里没有这些变量。
    env = {k: v for k, v in os.environ.items()
           if k != "_MEIPASS2" and not k.startswith("_PYI")}
    try:
        DETACHED = 0x00000008 | 0x08000000        # DETACHED_PROCESS | CREATE_NO_WINDOW
        # 【2026-10-09 修 中-5 / 升级体验口径】带 --handoff 拉起新版本：告诉它"你是升级交接
        # 起来的，旧进程还占着端口、马上会退出，需要等一下再接管"，而不是像"用户又双击了一次"
        # 那样直接开个浏览器就退出（那正是 2026-10-07「升级后程序没起来」的根因）。
        # 静默实例升级时还会把 --silent 一起带上，免得后台升级弹出一个浏览器窗口。
        subprocess.Popen([exe] + _relaunch_args(), cwd=folder, env=env, creationflags=DETACHED,
                         shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    except Exception as ex:
        return False, f"新版本已就位但启动失败: {type(ex).__name__}"
    print("[ok] 就地替换完成：已改名旧文件、写入新文件、并拉起新进程")
    return True, "已就地替换并重启"

def _exe_dir_writable():
    """替换可行性预检：在 exe 同目录真的走一遍「建文件 → 改名 → 删除」。

    为什么要这一步（修 中-4）：`_fast_swap` 覆盖程序用的不是"写文件"而是
    「复制成 .new → 把 exe 改名成 .old → 把 .new 改名成 exe」，
    所以只判断"目录可写"还不够，**改名也必须能成功**（Program Files 下普通用户、
    被安全软件锁住、只读盘符都会在这里失败）。
    返回 (可否就地替换, 原因)。预检不过时调用方**不退出程序**，只把原因回给用户。
    """
    exe = EXE_PATH
    if not exe or not os.path.exists(exe):
        return False, "拿不到自身路径（源码模式下不支持自替换）"
    folder = os.path.dirname(exe)
    a = os.path.join(folder, ".hotnews_wtest")
    b = os.path.join(folder, ".hotnews_wtest2")
    try:
        for p in (a, b):
            try:
                os.remove(p)
            except Exception:
                pass
        with open(a, "wb") as f:
            f.write(b"hotnews")
        os.replace(a, b)          # 覆盖替换靠的就是改名，必须一起试
        os.remove(b)
        return True, ""
    except Exception as ex:
        for p in (a, b):
            try:
                os.remove(p)
            except Exception:
                pass
        return False, (f"程序所在目录无法写入或改名（{folder}，{type(ex).__name__}）")


def trigger_replace():
    """立即执行替换（主程序退出前调用）。

    三种落地方式，按优先级：
      1) `_fast_swap`  —— 就地改名替换，同步返回结果，实测约 20 毫秒；
      2) `hotnews_update.vbs` —— wscript.exe 跑，GUI 宿主无黑窗（替换前会等主程序退出）；
      3) `hotnews_update.bat` —— cmd 兜底（【2026-10-09 修 低-5】现在真的会被写出，见
         `_write_replace_scripts`）。

    【2026-10-07】加了 .bat 兜底：万一 .vbs 那条路走不通（脚本缺失/被占用/wscript 不可用），
    就用 cmd /c + CREATE_NO_WINDOW 跑 .bat，同样不会闪黑窗，保证升级一定能落地。

    【2026-10-09 修 中-4：失败分支闭环】**返回 True 才允许主程序退出**。
    以前 `_fast_swap` 失败后就 spawn 一个脚本并返回 True，主程序随即 os._exit ——
    而脚本里的 CopyFile 带 On Error Resume Next，30 次全失败也静默什么都不做，
    于是用户看到的是"程序直接消失且不再起来"（exe 目录不可写时必现）。
    现在：
      · 先做 `_exe_dir_writable()` 预检，不过就 `return False`（**不退出**）+ 明确原因；
      · 脚本方式写出的 VBS/BAT 都会在失败时写回执并**把原版程序重新拉起来**（见两个 _write_replace_*）；
      · 连脚本都拉不起来时同样 `return False`，程序继续跑。
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
    pkg = UPDATE_PKG
    # 注意：_state["remote"] 存的是**版本号字符串**，远端字典在 latest 里
    _rm = _state.get("latest") or {}
    _lau = _rm.get("launcher") or {}
    exp_sha = (_lau.get("sha256") or "").upper()
    exp_size = int(_lau.get("size", 0) or 0)

    # 【2026-10-09 修 高-5：fail-closed】原来这里靠 `if exp_size and ...` / `if exp_sha and ...`
    # 把关 —— 拿不到期望值就**整段跳过**，直接 _fast_swap 覆盖 exe。而 _state["latest"] 在
    # 「已是最新版」时被显式置为 None（check() 行 148），%TEMP%\hotnews_update.exe 又是**固定文件名**，
    # 上一次升级/旧版本留下的包会一直躺在那儿，于是会出现：
    #   已经是最新 → latest=None → 期望值全空 → 校验整段跳过 → 把 %TEMP% 里的旧包静默覆盖上去（降级）。
    # 现在改成：**先确认「确实有一个待安装的新版本」，再逐项校验 sha256 与尺寸，缺一项就不替换。**
    if not _rm or not _rm.get("version"):
        _state.update(status="error", message="没有待安装的新版本，已放弃替换")
        return False
    if not _lau.get("url"):
        _state.update(status="error", message="远端未提供下载地址，已放弃替换")
        return False
    if not exp_sha:
        # 远端没给 sha256 就没法确认这个包是谁 —— 宁可不升级（设计承诺：升级必须验 sha）
        _state.update(status="error", message="远端未提供 sha256，已放弃替换")
        return False
    if not os.path.exists(pkg):
        _state.update(status="error", message="升级包不在本地，已放弃替换")
        return False
    if exp_size and os.path.getsize(pkg) != exp_size:
        _state.update(status="error", message="升级包不完整，已放弃替换")
        return False
    if _sha256(pkg) != exp_sha:
        _state.update(status="error", message="升级包校验失败，已放弃替换")
        return False

    # 【2026-10-09 修 中-4】可写性 / 改名可行性预检。不过就**不退出程序**：
    # 与其"程序消失且不再起来"，不如维持现状 + 说清楚让用户手动处理。
    _okw, _why = _exe_dir_writable()
    if not _okw:
        _state.update(status="error", message=(
            f"无法自动替换：{_why}。程序已保持原样、没有退出；"
            f"请手动把升级包（%TEMP%\\{os.path.basename(UPDATE_PKG)}）复制到程序目录覆盖，"
            f"或把程序换到有写入权限的目录后再点「立即更新」。"))
        print(f"[warn] 替换预检未通过：{_why}（程序未退出）")
        return False

    # 【2026-10-07 用户反馈「更新安装太慢」】先用**就地替换**：实测约 20 毫秒，
    # 不需要等主程序退出、不需要脚本宿主、也不会出现"替换成功但没重启"的情况。
    ok, msg = _fast_swap(pkg)
    if ok:
        _state.update(status="done", message=msg)
        return True
    print(f"[warn] 就地替换失败（{msg}），回退到脚本方式")

    vbs = os.path.join(tempfile.gettempdir(), "hotnews_update.vbs")
    if os.path.exists(vbs):
        try:
            _spawn(["wscript.exe", vbs])
            _state.update(status="ready", message="已改用替换脚本，程序退出后自动完成")
            return True
        except Exception:
            pass
    bat = os.path.join(tempfile.gettempdir(), "hotnews_update.bat")
    if os.path.exists(bat):
        try:
            _spawn(["cmd.exe", "/c", bat])
            _state.update(status="ready", message="已改用替换脚本(.bat)，程序退出后自动完成")
            return True
        except Exception:
            pass
    # 【2026-10-09 修 中-4】连脚本都拉不起来 → **不返回 True**，
    # 主程序于是不会退出，用户看到的是"更新没装上"而不是"程序不见了"。
    _state.update(status="error", message=(
        "自动替换不可用（就地替换失败，且替换脚本无法启动）。"
        "程序已保持原样、没有退出；请手动把升级包复制到程序目录覆盖后重新运行。"))
    print("[warn] 自动替换不可用，程序保持原样（未退出）")
    return False
