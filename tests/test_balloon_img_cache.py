# -*- coding: utf-8 -*-
# 作者: 海风（kele551）   https://gitcode.com/kele551/hotnews
"""气泡题图缓存清理 _balloon_img_prune 的单测 —— 2026-11-12「只涨不清」的修复。

背景（装机态实测，2026-11-12 21:4x）：
    %LOCALAPPDATA%\\hotnews\\balloon_img 攒了 3317 个文件 / 876 MB（最早 10-28），
    原实现只有「单张 ≤8MB」一道闸，没有任何按时间 / 总量清理，目录会一直涨。
修复：总量 300MB / 文件数 2000 条两条上限，超出按 mtime 从旧到新删；.part 残片超 1 小时清掉。

测试钉死：①超文件数按最旧删；②超总量按最旧删；③新文件不误删；④.part 残片清、新鲜的留；
⑤目录不存在不抛异常；⑥上限常量本身在合理区间；⑦_warm 里真的接了节流入口。

为什么不 import server.py：见 tests/test_age_window.py 开头。
"""
import ast
import os
import shutil
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server.py")

WANT_FUNCS = {"_balloon_img_prune", "_balloon_img_prune_tick", "_balloon_img_warm"}
WANT_CONSTS = {"BALLOON_IMG_MAX_BYTES", "BALLOON_IMG_MAX_FILES",
               "BALLOON_IMG_PRUNE_EVERY", "BALLOON_IMG_PART_TTL"}

FAILS = []


def check(name, got, want):
    if got == want:
        print("[ok] %s" % name)
    else:
        print("[X ] %s  得到=%r 应为=%r" % (name, got, want))
        FAILS.append(name)


def check_true(name, cond, note=""):
    if cond:
        print("[ok] %s" % name)
    else:
        print("[X ] %s  %s" % (name, note))
        FAILS.append(name)


def load():
    src = open(SERVER, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in WANT_CONSTS:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
    for c in WANT_CONSTS | WANT_FUNCS:
        if c not in ns:
            FAILS.append("摘不到 " + c)
    # 只摘要节点时，server.py 顶部的 import 不会跟着执行；被测函数要用到的模块补进去。
    ns.update({"os": os, "time": time, "threading": threading})
    return ns, tree


def make_dir(files):
    """files = [(名字, 字节数, mtime 偏移秒)]；mtime 越小越旧。"""
    d = tempfile.mkdtemp(prefix="hn_balloon_cache_")
    base = time.time()
    for name, size, age in files:
        p = os.path.join(d, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        os.utime(p, (base - age, base - age))
    return d


def names(d):
    return sorted(os.listdir(d))


def main():
    ns, tree = load()
    prune = ns["_balloon_img_prune"]

    print("---- 0) 上限常量本身：不能等于没设")
    check_true("总量上限 ≥150MB 且 ≤1GB", 150 * 1024 * 1024 <= ns["BALLOON_IMG_MAX_BYTES"] <= 1024 * 1024 * 1024,
               "实际=%r" % (ns["BALLOON_IMG_MAX_BYTES"],))
    check_true("文件数上限 ≥1000 且 ≤5000", 1000 <= ns["BALLOON_IMG_MAX_FILES"] <= 5000,
               "实际=%r" % (ns["BALLOON_IMG_MAX_FILES"],))
    check_true("两次清理间隔 ≥1 小时", ns["BALLOON_IMG_PRUNE_EVERY"] >= 3600)
    check_true(".part 残片有效期 ≥10 分钟", ns["BALLOON_IMG_PART_TTL"] >= 600)

    print("---- 1) 超文件数：按最旧删，新的留住")
    d = make_dir([("f%d.img" % i, 1000, 1000 - i) for i in range(10)])   # f0 最旧
    removed, files, total = prune(d, max_bytes=10 ** 9, max_files=5)
    check("删了 5 个", removed, 5)
    check("剩 5 个文件", files, 5)
    check("留在盘上的就是最新那 5 个", names(d), ["f5.img", "f6.img", "f7.img", "f8.img", "f9.img"])
    check("剩余字节数对得上", total, 5 * 1000)
    shutil.rmtree(d, ignore_errors=True)

    print("---- 2) 超总量：仍按最旧删，直到压到上限内")
    d = make_dir([("f%d.img" % i, 1000, 1000 - i) for i in range(10)])   # 共 10KB
    removed, files, total = prune(d, max_bytes=4000, max_files=100)
    check("删了 6 个", removed, 6)
    check_true("剩余总量 ≤ 上限", total <= 4000, "实际=%r" % (total,))
    check("留下的仍是最新的 4 个", names(d), ["f6.img", "f7.img", "f8.img", "f9.img"])
    shutil.rmtree(d, ignore_errors=True)

    print("---- 3) 本来就在上限内：一个都不删")
    d = make_dir([("a.img", 1000, 10), ("b.img", 1000, 5)])
    check("不删", prune(d, max_bytes=10 ** 9, max_files=100)[0], 0)
    check("文件都在", names(d), ["a.img", "b.img"])
    shutil.rmtree(d, ignore_errors=True)

    print("---- 4) 下载中断的 .part：超 1 小时清掉，新鲜的不动")
    d = make_dir([("old.img.part", 500, 7200), ("new.img.part", 500, 60), ("ok.img", 500, 30)])
    removed, files, total = prune(d, max_bytes=10 ** 9, max_files=100)
    check("只清掉那个超期的 .part", removed, 1)
    check("剩下的文件", names(d), ["new.img.part", "ok.img"])
    shutil.rmtree(d, ignore_errors=True)

    print("---- 5) 目录不存在 / 路径塞了个文件：不抛异常、回 0")
    check("不存在的目录", prune(os.path.join(tempfile.gettempdir(), "hn_no_such_dir_xyz"),
                                max_bytes=1, max_files=1), (0, 0, 0))
    fpath = os.path.join(tempfile.mkdtemp(prefix="hn_balloon_file_"), "x")
    open(fpath, "wb").write(b"y" * 10)
    check("路径是文件不是目录", prune(fpath, max_bytes=1, max_files=1), (0, 0, 0))
    shutil.rmtree(os.path.dirname(fpath), ignore_errors=True)

    print("---- 6) _balloon_img_warm 里真的接了节流入口（不能只加函数不调用）")
    warm_src = ast.unparse([n for n in tree.body
                            if isinstance(n, ast.FunctionDef) and n.name == "_balloon_img_warm"][0])
    check_true("warm 开头调用了 _balloon_img_prune_tick()", "_balloon_img_prune_tick()" in warm_src)
    tick_src = ast.unparse([n for n in tree.body
                            if isinstance(n, ast.FunctionDef) and n.name == "_balloon_img_prune_tick"][0])
    check_true("节流入口真的起后台线程（不卡绘制）", "Thread(" in tick_src and "daemon=True" in tick_src)
    check_true("节流入口用逐次放行的时间戳（不会每次都扫目录）",
               "_IMG_PRUNE_NEXT" in tick_src)

    print("\n%s  失败 %d 条" % ("全部通过" if not FAILS else "有失败", len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
