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
    """探测 8000-8014 上是否已有本程序在跑；有则返回其端口（用于直接复用）"""
    import httpx
    for p in range(PORT_START, PORT_START + PORT_TRIES):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            if s.connect_ex(("127.0.0.1", p)) != 0:
                continue
        # 端口有人监听，还得确认是本程序（认 /api/version 的返回结构）
        try:
            r = httpx.get(f"http://127.0.0.1:{p}/api/version", timeout=1.5, trust_env=False)
            if r.status_code == 200 and "version" in r.json():
                return p
        except Exception:
            pass
    return 0


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
    # 1) 已有实例 → 直接把它调到前台（开浏览器），自己退出，避免起第二个
    # 【2026-10-07 修「升级后不自启」】升级时的时序是：
    #   旧进程把 exe 换掉 → 立刻拉起新进程 → 自己 0.6 秒后才退出。
    # 于是新进程起来时**旧进程还占着端口**，被这里判成"已有实例"，开个浏览器就退了
    # —— 用户看到的现象就是"升级完了程序没起来"。
    # 所以端口被占时先**等一等再重试**（最多 12 秒）：升级场景下旧实例马上就会退出，
    # 等到了就正常启动；真的已有实例（不是升级）等完仍占用，才走"打开浏览器"的老路。
    running = find_running()
    if running:
        for i in range(75):        # 最多等 30 秒（原来 12 秒，遇到慢退的旧进程不够）
            time.sleep(0.4)
            if not find_running():
                running = None
                break
        if running:
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

    threading.Thread(target=wait_and_open, args=(url, port), daemon=True).start()

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
