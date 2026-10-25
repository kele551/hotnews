# -*- coding: utf-8 -*-
# 作者: 海风（kele551）
# 用途: 断网首屏回落归档（2026-10-25 待办 #62）的判据单测。
#
# 为什么不 import server.py：那是八千行的服务，模块级要建 FastAPI 应用、读配置、
# 起缓存路径，单测一 import 就把这些副作用全走一遍（还可能碰到本机真实数据目录）。
# 这里用 ast 只把要测的那几个函数 + 它们的依赖摘出来单独 exec，
# 归档连接指向**临时目录里自己建的 sqlite**，本机 %LOCALAPPDATA% 一个字节都不碰。
import ast
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server.py")

# 要测的入口（依赖靠闭包递归自动带上，不用手工列全）
# _hist_rows 是写库那半：灌测试数据要走真实映射，不能自己拼 tuple（列顺序一改就测不出来）
ROOTS = ("_news_from_archive", "_hist_rows")
# 不搬的函数：归档连接由测试自己换成临时库（真 _hist_conn 会去建本机数据目录）
EXCLUDE = {"_hist_conn"}

FAILS = []
SKIPPED_CONSTS = []
SKIPPED_IMPORTS = []


def _is_const_target(node):
    """只挑模块级「大写/下划线开头」的简单赋值当常量搬过去。
    小写的那些（app、client、logger）多半有真实副作用，不搬。"""
    if not isinstance(node, ast.Assign) or len(node.targets) != 1:
        return False
    t = node.targets[0]
    return (isinstance(t, ast.Name) and t.id[:1] in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
            and t.id[:2] != "__")


def load_fragment():
    """把 server.py 里归档读路径那几个函数摘出来 exec，返回它们的命名空间。"""
    src = open(SERVER, encoding="utf-8").read()
    tree = ast.parse(src)

    funcs, order = {}, []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and not node.decorator_list and node.name not in EXCLUDE:
            funcs[node.name] = node
        order.append(node)

    # 从入口出发按引用关系把用到的函数捞全（少列一个就 NameError，不如自动闭包）
    need = set(ROOTS)
    queue = list(ROOTS)
    while queue:
        fn = funcs.get(queue.pop())
        if fn is None:
            continue
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Name) and sub.id in funcs and sub.id not in need:
                need.add(sub.id)
                queue.append(sub.id)

    chosen = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            chosen.append(node)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            continue                                    # 模块 docstring 不需要
        elif _is_const_target(node):
            chosen.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name in need:
            chosen.append(node)

    ns = {"__name__": "hotnews_archive_fragment", "_FRAGMENT": True}
    # 逐条 exec：某条常量要调没搬进来的函数（_load_config 之类）就跳过它，
    # 不影响其余 —— 反正被测路径用不到。
    for node in chosen:
        mod = ast.Module(body=[node], type_ignores=[])
        try:
            exec(compile(mod, SERVER, "exec"), ns)
        except Exception as ex:
            if _is_const_target(node):
                if node.targets[0].id not in SKIPPED_CONSTS:
                    SKIPPED_CONSTS.append(node.targets[0].id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                # httpx / feedparser / fastapi 这些第三方包本机 python 里没装
                # （它们只在打包时被收进 exe），被测的那几个函数一个都不用到 ——
                # 跳过是常态，不算失败。
                nm = getattr(node, "names", [None])[0]
                SKIPPED_IMPORTS.append(getattr(nm, "name", "?") if nm else "?")
            else:
                FAILS.append("摘取节点失败 %s: %s" % (ast.dump(node)[:60], ex))
    return ns, need


def make_db(ns, path):
    conn = sqlite3.connect(path)
    conn.executescript(ns["_HIST_SCHEMA"])
    conn.commit()
    return conn


def seed(ns, conn, region, items):
    """走**真实写库路径**灌数据（_hist_rows + _HIST_UPSERT），字段映射一起验。"""
    rows = ns["_hist_rows"](region, items)
    conn.executemany(ns["_HIST_UPSERT"], rows)
    conn.commit()
    return len(rows)


def mk(title, link, pub, channel="测试源", label="测试源", cls="头条",
       image="", heat=0, key=None):
    return {"id": key or "", "title": title, "link": link, "desc": "",
            "image": image, "published": pub, "heat": heat, "rank": 0,
            "translated": False, "channel": channel, "label": label, "cls": cls}


def check(name, ok, detail=""):
    print("%s %s%s" % ("[ok]" if ok else "[!!]", name, ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name + (" — " + detail if detail else ""))


def main():
    ns, need = load_fragment()
    check("依赖闭包摘到 %d 个函数" % len(need),
          {"_news_from_archive", "_hist_query", "_hist_where", "_hist_item",
           "_hist_rows", "_dedup_same_event"} <= need,
          "拿到: " + ", ".join(sorted(need)))

    tmp = tempfile.mkdtemp(prefix="hotnews_test_")
    conn = make_db(ns, os.path.join(tmp, "history.db"))
    # 被测函数里的 _hist_conn / _HIST_LOCK 换成临时库 + 同一把锁，读写不打架
    ns["_hist_conn"] = lambda: conn
    ns["_HIST_LOCK"] = threading.Lock()

    days = 86400
    now = int(time.time())
    d1 = now - 1 * days
    d10 = now - 10 * days

    # ── 1. 空归档：一条都没有 → 不硬凑，调用方该回"正在抓取" ──
    items, note = ns["_news_from_archive"]("cn", 48)
    check("空归档回空列表", items == [], "%r" % (note,))

    # ── 2. 近 7 天有货：原样垫首屏，note 说清是哪天 ──
    seed(ns, conn, "cn", [mk("测试新闻 %02d" % i, "https://example.com/a%d" % i,
                             now - i * 600) for i in range(1, 21)])
    items, note = ns["_news_from_archive"]("cn", 48)
    check("近 7 天归档垫首屏 20 条", len(items) == 20, note)
    check("note 报了归档日期", bool(re.match(r"^本机归档 \d{4}-\d{2}-\d{2} 的 20 条$", note)), note)

    # ── 3. 字段形状与 /api/news 一致（卡片渲染直接复用，少一个键就是空卡片）──
    need_keys = {"id", "region", "channel", "label", "cls", "title", "desc",
                 "link", "image", "published", "heat", "rank", "translated",
                 "day", "times"}
    missing = need_keys - set(items[0].keys())
    check("归档条目字段齐（可直接喂卡片渲染）", not missing, "缺: " + ",".join(sorted(missing)))
    check("published 原样带着（页面「最新」说的是实话）",
          all(isinstance(it["published"], int) and it["published"] > 0 for it in items),
          "样例 %d" % items[0]["published"])

    # ── 4. 分区隔离：intl 的归档不能顶 cn 的首屏 ──
    seed(ns, conn, "intl", [mk("Intl headline %02d" % i, "https://example.com/i%d" % i,
                               d1, label="境外源") for i in range(1, 9)])
    _, cn_note = ns["_news_from_archive"]("cn", 48)
    intl_items, _ = ns["_news_from_archive"]("intl", 48)
    check("只灌 intl 不动 cn 的池子（cn 仍是它那 20 条）", len(intl_items) == 8)
    check("cn 归档条数没被 intl 污染", "的 20 条" in cn_note, cn_note)

    # ── 5. 超过 7 天的存货：第一档凑不满 → 放宽到保留期，不再空屏 ──
    conn2 = make_db(ns, os.path.join(tmp, "old.db"))
    ns["_hist_conn"] = lambda: conn2
    seed(ns, conn2, "hot", [mk("很久以前的热榜 %02d" % i, "https://example.com/old%d" % i,
                               d10, cls="热榜") for i in range(1, 5)])
    old_items, old_note = ns["_news_from_archive"]("hot", 48)
    check("10 天前的 4 条也能垫（比空屏强）", len(old_items) == 4, old_note)
    conn2.close()

    # ── 6. 热榜条目 published=0（源站不给时间）也得能回落 ──
    conn3 = make_db(ns, os.path.join(tmp, "zeropub.db"))
    ns["_hist_conn"] = lambda: conn3
    seed(ns, conn3, "hot", [mk("热榜第 %d 名" % i, "https://example.com/h%d" % i, 0,
                               cls="热榜", heat=i * 100) for i in range(1, 8)])
    z_items, z_note = ns["_news_from_archive"]("hot", 48)
    check("published=0 的热榜条目照样回落", len(z_items) == 7, z_note)
    check("热榜条目 day 落在当天（归档窗口才捞得到）",
          z_items and z_items[0]["day"] == ns["_hist_day"](now), str(z_items[:1]))
    conn3.close()

    # ── 7. 折叠：同一链接的两行只留一条（归档是 upsert，重复来自不同 key）──
    conn4 = make_db(ns, os.path.join(tmp, "dup.db"))
    ns["_hist_conn"] = lambda: conn4
    seed(ns, conn4, "cn", [mk("同一篇稿子", "https://example.com/same", now - 60, key="k1"),
                           mk("同一篇稿子", "https://example.com/same", now - 61, key="k2")])
    dup_items, _ = ns["_news_from_archive"]("cn", 48)
    check("同链接的两行折叠成一条", len(dup_items) == 1, "%d 条" % len(dup_items))
    conn4.close()

    # ── 8. 库读不到（连接抛异常）：不炸接口，回空 + 一句实话 ──
    def boom():
        raise sqlite3.OperationalError("database is locked")
    ns["_hist_conn"] = boom
    bad_items, bad_note = ns["_news_from_archive"]("cn", 48)
    check("归档读不到时不抛异常", bad_items == [], "%r" % (bad_items,))
    check("归档读不到时 note 说清原因", "读不到" in (bad_note or ""), bad_note)

    # ── 9. 取回条数按 limit 放大，保证首屏折叠后还够数 ──
    ns["_hist_conn"] = lambda: conn
    for probe, expect_min in ((20, 20), (200, 20)):
        got, _ = ns["_news_from_archive"]("cn", probe)
        check("limit=%d 时至少给回 %d 条" % (probe, expect_min),
              len(got) >= expect_min, "%d 条" % len(got))

    conn.close()
    print("")
    if SKIPPED_CONSTS:
        print("（摘取时跳过的无关常量 %d 个：%s）"
              % (len(SKIPPED_CONSTS), ", ".join(SKIPPED_CONSTS[:8])))
    if SKIPPED_IMPORTS:
        print("（本机没装、被测路径也用不到的第三方包：%s）"
              % ", ".join(sorted(set(SKIPPED_IMPORTS))))
    if FAILS:
        print("未通过 %d 项:" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
