# -*- coding: utf-8 -*-
"""
hotnews 单文件入口（无窗口版）
双击 exe → 若已有实例在跑就直接打开浏览器 → 否则选端口启动服务 → 自动开浏览器

不弹控制台窗口。运行日志写在 exe 同级的 hotnews.log。
退出方式：页面右上角「退出程序」，或任务管理器结束 hotnews.exe。
"""
import os
import sys
import time
import socket
import threading
import webbrowser

# 打包后 sys.path 不含 exe 同级目录，显式补上，保证 import server 有效
if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(sys.executable)))

import uvicorn  # noqa: E402
from server import app, VERSION, BASE_DIR, LOG_PATH  # noqa: E402
import server  # noqa: E402  供启动时写入真实访问地址（通知点击跳转用）

PORT_START = 8000
PORT_TRIES = 15


def alert(msg, title="热点新闻"):
    """没有控制台可看，致命错误用系统对话框告知"""
    print(f"[X] {msg}")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)
    except Exception:
        pass


def find_running():
    """探测 8000-8014 上是否已有本程序在跑；有则返回 (端口, 已运行秒数)。

    【2026-10-09 修 中-5】返回值从"端口"改成 (端口, uptime)：
    uptime 取自对方 /api/version，用来在日志里说清"对面这个实例已经服务了多久"。
    端口那道判断不变 —— 必须能返回带 version 的 JSON 才算"是本程序"。
    """
    import httpx
    for p in range(PORT_START, PORT_START + PORT_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", p)) != 0:
                continue
        # 端口有人监听，还得确认是本程序（认 /api/version 的返回结构）
        try:
            r = httpx.get(f"http://127.0.0.1:{p}/api/version", timeout=1.5, trust_env=False)
            if r.status_code == 200:
                js = r.json()
                if "version" in js:
                    return p, int(js.get("uptime") or 0)
        except Exception:
            pass
    return 0, 0


def pick_port(prefer=PORT_START, tries=PORT_TRIES):
    """8000 被占用时自动往后找可用端口"""
    for p in range(prefer, prefer + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return 0


def wait_and_open(url, port, tries=200):
    """等服务端口真正可连上再开浏览器，避免白屏。

    轮询间隔 0.4s → 0.12s：原来最坏要多等 0.4 秒才把浏览器叫起来，
    而用户对"双击之后多久有反应"非常敏感（见 2026-10-07 反馈）。
    """
    for _ in range(tries):
        time.sleep(0.12)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                webbrowser.open(url)
                return


def main():
    # 【2026-10-07 用户要求「设置成开机启动，自动加载，需要看时点右键打开」】
    # 带 --silent 启动时**不开浏览器**，只在后台把服务跑起来、把第一屏数据抓好。
    # 这样用户任何时候右键点「打开热点新闻」，页面都是现成的，秒开。
    _silent = ("--silent" in sys.argv) or (os.environ.get("HOTNEWS_SILENT") == "1")
    # 【2026-10-09 修 中-5】--handoff：本进程是**升级后被新版本拉起来的**（见 updater._fast_swap），
    # 这时旧进程还占着端口、马上就会退出，所以需要等它让位。
    # 普通"用户又双击了一次"不带这个标记 —— 那种情况应该**立刻开浏览器**，
    # 原来一律空转 75 次 × 0.4 秒（≈30 秒）才开页面，用户只会以为程序卡死了。
    _handoff = ("--handoff" in sys.argv) or (os.environ.get("HOTNEWS_HANDOFF") == "1")

    # 1) 已有实例 → 直接把它调到前台（开浏览器），自己退出，避免起第二个
    running, _uptime = find_running()
    if running:
        if not _handoff:
            # 正在正常服务的实例：立刻把页面调出来，一秒都不等
            if _silent:
                print(f"[ok] 已有实例在 {running} 端口运行（静默模式，不弹浏览器）")
                return
            print(f"[ok] 已有实例在 {running} 端口运行（已服务 {_uptime}s），直接打开浏览器")
            webbrowser.open(f"http://127.0.0.1:{running}")
            return
        # 【2026-10-07 修「升级后不自启」/ 中-5】升级交接：旧进程马上让出端口，
        # 等它退出再接管。上限从 30 秒降到 **10 秒**（25 × 0.4s）——
        # 就绪替换（_fast_swap）只要几十毫秒，慢退的旧进程也远用不到 10 秒。
        for i in range(25):
            time.sleep(0.4)
            running, _uptime = find_running()
            if not running:
                break
        if running:
            # 等了 10 秒还在：那就当"确实已有实例"，照样把页面打开（不再让用户干等）
            if _silent:
                print(f"[ok] 已有实例在 {running} 端口运行（静默模式，不弹浏览器）")
                return
            print(f"[ok] 已有实例在 {running} 端口运行，直接打开浏览器")
            webbrowser.open(f"http://127.0.0.1:{running}")
            return
        print("[ok] 上一实例正在退出（升级），本实例接管")

    # 2) 没有实例 → 启动
    port = pick_port()
    if not port:
        alert(f"{PORT_START}-{PORT_START + PORT_TRIES - 1} 端口全部被占用。\n"
              f"请先释放端口后重试。")
        return

    url = f"http://127.0.0.1:{port}"
    server.APP_URL = url   # 供右下角通知点击跳转
    print("=" * 52)
    print(f"  热点新闻检索  v{VERSION}")
    print(f"  访问地址:  {url}")
    print(f"  日志文件:  {LOG_PATH}")
    print("  退出方式:  页面右上角「退出程序」")
    print("=" * 52)
    print()

    if not _silent:
        threading.Thread(target=wait_and_open, args=(url, port), daemon=True).start()
    else:
        print("[ok] 静默模式：后台预热数据，不打开浏览器")

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass
    except Exception as ex:
        alert(f"服务启动失败：{type(ex).__name__}: {ex}")
        return
    print("\n服务已停止。")


if __name__ == "__main__":
    main()
