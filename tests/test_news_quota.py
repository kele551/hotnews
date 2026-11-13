# -*- coding: utf-8 -*-
# 作者: 海风（kele551）   https://gitcode.com/kele551/hotnews
"""新闻流配额 _pick_news_with_quota 的单测 —— 2026-11-12「娱乐整栏消失」的修复。

背景（装机态实测，2026-11-12 21:4x）：
    国内池子 456 条（头条 43 / 娱乐 56 / 软件 47 / 科技 130 / 财经 37 / 热榜 143），
    `/api/news?region=cn&limit=200` 只回 200 条 = 热榜 143 + 科技 43 + 财经 10 + 头条 4，
    娱乐 0 / 软件 0。左栏计数与下拉都是拿这批数据现算的 → 用户连「娱乐」按钮都看不到。
根因：热榜 6 家后条目涨到 140+，全部排在新闻前面（items = hot + rest 再截 limit）。

修复口径：热榜单独限 HOT_RESPONSE_MAX 条，新闻按栏目保底 NEWS_MIN_PER_CLS 条。
本测试钉死四件事：①每栏都有货；②不重复；③总数不超名额；④小 limit 也不整栏消失。

为什么不 import server.py：八千行的服务，模块级就建 FastAPI、读配置、碰数据目录。
照 tests/test_age_window.py 的办法用 ast 只摘要测的函数与常量。
"""
import ast
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server.py")

WANT_FUNCS = {"_pick_news_with_quota", "api_news"}
WANT_CONSTS = {"NEWS_MIN_PER_CLS", "HOT_RESPONSE_MAX", "NEWS_LIMIT_MAX"}

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
    # 只摘要节点时 server.py 顶部的 import 不会跟着执行：api_news 的默认参数用了 FastAPI 的
    # Query(...)，给个透传桩就能把函数对象建出来（本测试只看它的接线，不调用它）。
    ns = {"Query": lambda default=None, **kw: default}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
            node.decorator_list = []          # api_news 挂着 @app.get，摘掉才好单独 exec
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in WANT_CONSTS:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
    for c in WANT_CONSTS | WANT_FUNCS:
        if c not in ns:
            FAILS.append("摘不到 " + c)
    return ns, tree


def pool(sizes):
    """造一批条目：sizes = [(cls, 条数)]，发布时间从新到旧依次递减 60 秒。"""
    items, ts = [], 1794500000
    for cls, n in sizes:
        for i in range(n):
            items.append({"cls": cls, "title": "%s-%d" % (cls, i),
                          "desc": "", "published": ts})
            ts -= 60
    items.sort(key=lambda x: x["published"], reverse=True)   # 与 api_news 的 rest 同口径
    return items


def cls_counts(rows):
    out = {}
    for it in rows:
        out[it["cls"]] = out.get(it["cls"], 0) + 1
    return out


def main():
    ns, tree = load()
    f = ns["_pick_news_with_quota"]
    HOT = ns["HOT_RESPONSE_MAX"]
    MIN = ns["NEWS_MIN_PER_CLS"]
    LIMIT = ns["NEWS_LIMIT_MAX"]

    print("---- 0) 常量本身：热榜限量与每栏保底都要在合理区间")
    check_true("HOT_RESPONSE_MAX 不超过页面热榜条数 20 + 余量", 20 <= HOT <= 40, "HOT=%r" % (HOT,))
    check_true("NEWS_MIN_PER_CLS 至少 10 条（少了等于没修）", 10 <= MIN <= 40, "MIN=%r" % (MIN,))
    check("limit 上限仍是 200（这次不动上限，只改分配）", LIMIT, 200)

    print("---- 1) 真实分布回放：5 栏 × 176 个新闻名额")
    rest = pool([("科技", 130), ("娱乐", 56), ("软件", 47), ("头条", 43), ("财经", 37)])
    got = f(rest, LIMIT - HOT)            # 200 - 24 = 176
    cnt = cls_counts(got)
    check("总条数等于名额", len(got), LIMIT - HOT)
    check("每一栏都出现了（这是修复的核心）", sorted(cnt.keys()),
          sorted(["科技", "娱乐", "软件", "头条", "财经"]))
    for c in ["科技", "娱乐", "软件", "头条", "财经"]:
        check_true("「%s」至少保底 %d 条" % (c, MIN), cnt.get(c, 0) >= MIN, "实际=%d" % cnt.get(c, 0))
    check("没有重复条目", len({id(x) for x in got}), len(got))
    check("仍按发布时间倒序", [x["published"] for x in got],
          sorted([x["published"] for x in got], reverse=True))
    check_true("没有改动原字典（cls 还是原样）", all(x["cls"] in ("科技", "娱乐", "软件", "头条", "财经")
                                                for x in got))

    print("---- 2) 小 limit（默认 48）：热榜 24 + 新闻 24 也必须每栏都有")
    got48 = f(rest, 48 - HOT)
    c48 = cls_counts(got48)
    check("48 上限下的新闻条数", len(got48), 48 - HOT)
    check("五栏仍都在", sorted(c48.keys()),
          sorted(["科技", "娱乐", "软件", "头条", "财经"]))
    for c in c48:
        check_true("「%s」至少 20//5=4 条" % c, c48[c] >= 4, "实际=%d" % c48[c])

    print("---- 3) 某栏库存不够：名额顺延给还有货的栏，不死等")
    thin = pool([("科技", 100), ("娱乐", 3), ("软件", 2)])
    got3 = f(thin, 50)
    check("总条数仍凑满 50", len(got3), 50)
    check("娱乐全给了", cls_counts(got3).get("娱乐"), 3)
    check("软件全给了", cls_counts(got3).get("软件"), 2)

    print("---- 4) 边界：budget 0 / 空池 / 名额大于库存")
    check("budget=0 回空", f(rest, 0), [])
    check("空池回空", f([], 100), [])
    got_all = f(pool([("科技", 5), ("娱乐", 4)]), 100)
    check("名额大于库存时全给", len(got_all), 9)

    print("---- 5) api_news 真的接线了（不能只加函数不调用）")
    src = ast.unparse([n for n in tree.body
                       if isinstance(n, ast.FunctionDef) and n.name == "api_news"][0]).replace('"', "'")
    check_true("api_news 里调用了 _pick_news_with_quota", "_pick_news_with_quota(rest" in src)
    check_true("热榜按 HOT_RESPONSE_MAX 截断", "hot[:HOT_RESPONSE_MAX]" in src)
    check_true("不再整批 hot+rest 直接截断", "items = hot + rest" not in src)
    check_true("搜索分支保留（不限额不配额）", "kw in i['title'].lower()" in src)
    check_true("热榜专区仍按 limit 截断", "region == 'hot'" in src and "items = hot[:limit]" in src)

    print("\n%s  失败 %d 条" % ("全部通过" if not FAILS else "有失败", len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
