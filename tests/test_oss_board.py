# -*- coding: utf-8 -*-
# 作者: 海风（kele551）
# 用途: 「开源热榜」（GitHub 近 30 天新锐榜，2026-10-28 新增）的口径单测。
#
# 这一栏是新闻池之外单独一条链路（server.py「开源热榜」一节 + static/index.html
# 的 OSS_* 几处）。它碰不到 _collect / _select_fresh，所以本测不测新闻，只测三件事：
#   1) 榜单**口径**：窗口日期、排序、名次、条数上限、题图地址；
#   2) **闸门**：题材命中 / 地址不是 github.com / archived 的仓库，一条都不许出现；
#   3) **接线**：接口注册了、健康度登记了、预热与定时轮都 kick 了、前端栏目加对了，
#      并且它**没有**被写进 SOURCES（写进去就要动五处已经踩过坑的时效/分流口径）。
#
# 为什么不 import server.py：八千行的服务，模块级就建 FastAPI、读配置、碰数据目录。
# 照 tests/test_age_window.py 的办法用 ast 只摘要测的函数与常量。
#
# 确定性：today / translate 都是 _oss_items_from_payload 的入参，真实时钟、真实翻译、
# 真实网络一概不进断言（机器没有网络、端口怎么发、几点跑，结果都必须一样）。
import ast
import os
import re
import html
import hashlib
import ipaddress
import urllib.parse
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server.py")
INDEX = os.path.join(REPO, "static", "index.html")

WANT_FUNCS = {"_clean", "_oss_window_date", "_oss_search_url", "_oss_created_ts",
              "_oss_link", "_oss_desc", "_oss_items_from_payload"}
WANT_CONSTS = {"OSS_ENABLED", "OSS_CLS", "OSS_SOURCE_ID", "OSS_SOURCE_LABEL",
               "OSS_WINDOW_DAYS", "OSS_LIMIT", "OSS_TTL", "OSS_FAIL_TTL",
               "OSS_SEARCH_URL", "OSS_IMAGE_BASE", "OSS_NAME_RX", "_OSS_BLOCK_RX",
               "SOURCES"}

CST = timezone(timedelta(hours=8))
# 全程钉死这一天：跨天跑测试不许变绿变红（口径断言要的是"30 天前那个日期"，不是"今天"）
FIXED_TODAY = datetime(2026, 10, 28, 9, 30, tzinfo=CST)

FAILS = []


def check(name, got, want):
    if got == want:
        print("[ok] %s" % name)
    else:
        print("[X ] %s  得到=%r 应为=%r" % (name, got, want))
        FAILS.append(name)


def check_true(name, cond, why=""):
    if cond:
        print("[ok] %s" % name)
    else:
        print("[X ] %s  %s" % (name, why))
        FAILS.append(name)


def load():
    src = open(SERVER, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"re": re, "html": html, "hashlib": hashlib, "ipaddress": ipaddress,
          "urllib": urllib, "urllib.parse": urllib.parse,
          "datetime": datetime, "timedelta": timedelta, "timezone": timezone, "CST": CST}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS and not node.decorator_list:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in WANT_CONSTS:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
    for c in WANT_CONSTS | WANT_FUNCS:
        if c not in ns:
            FAILS.append("摘不到 " + c)
    return ns, tree, src


def fnodes(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == name]


def calls_inside(tree, where, want):
    """函数（含其内部嵌套函数）里有没有调用某个名字 —— 接线检查用，不被注释骗。"""
    for fn in fnodes(tree, where):
        for c in ast.walk(fn):
            if not isinstance(c, ast.Call):
                continue
            f = c.func
            if isinstance(f, ast.Name) and f.id == want:
                return True
            if isinstance(f, ast.Attribute) and f.attr == want:
                return True
    return False


def repo(name, stars, created="2026-10-05T09:12:33Z", desc="A tiny thing", lang="Rust",
         **kw):
    r = {"full_name": name, "html_url": "https://github.com/" + name,
         "stargazers_count": stars, "forks_count": kw.get("forks", stars // 10),
         "created_at": created, "description": desc, "language": lang,
         "topics": kw.get("topics", []), "archived": kw.get("archived", False)}
    return r


def main():
    ns, tree, src = load()
    OSS_CLS = ns["OSS_CLS"]
    mk = ns["_oss_items_from_payload"]

    print("---- 1) 榜单口径：窗口 / 排序参数 / 时间解析")
    check("近 30 天 → 窗口日期", ns["_oss_window_date"](30, FIXED_TODAY), "2026-09-28")
    check("days 给 0 也至少回退 1 天", ns["_oss_window_date"](0, FIXED_TODAY), "2026-10-27")
    u = ns["_oss_search_url"](30, 20, FIXED_TODAY)
    check_true("URL 里 > 转义成 %3E", "%3E" in u and ">" not in u, u)
    check_true("URL 带 created:><日期>", "created%3A%3E2026-09-28" in u, u)
    check_true("URL 按星标降序", "sort=stars" in u and "order=desc" in u, u)
    check_true("URL per_page=20", "per_page=20" in u, u)
    check_true("per_page 上限夹到 100",
               "per_page=100" in ns["_oss_search_url"](30, 500, FIXED_TODAY), u)
    utc = timezone.utc
    check("created_at(Z) 解成 epoch 秒",
          ns["_oss_created_ts"]("2026-10-05T09:12:33Z"),
          int(datetime(2026, 10, 5, 9, 12, 33, tzinfo=utc).timestamp()))
    check("解不出给 0（不抛）", ns["_oss_created_ts"]("昨天下午"), 0)

    print("---- 2) 跳转地址只认 github.com（把跳转权留在自己手里）")
    check("正常仓库地址放行", ns["_oss_link"]("https://github.com/a/b"), "https://github.com/a/b")
    for bad in ("javascript:alert(1)", "data:text/html,x", "https://evil.com/a/b",
                "https://github.com@evil.com/a/b", "https://1.2.3.4/a/b",
                "https://github.com:8443/a/b", "https://user:pw@github.com/a/b", ""):
        check_true("拒: %r" % (bad or "(空)",), ns["_oss_link"](bad) == "", ns["_oss_link"](bad))

    print("---- 3) 条目映射：排序 / 名次 / 上限 / 题图 / 徽章字段")
    rows = [repo("zeta/late", 900), repo("alpha/top", 50000, lang="Go"),
            repo("mid/middle", 1200, desc=""), repo("beta/second", 3000),
            repo("dup/one", 100), repo("dup/one", 100)]
    got = mk({"items": rows}, 30, 20, FIXED_TODAY, None)
    check("按累计星标降序", [x["title"] for x in got],
          ["alpha/top", "beta/second", "mid/middle", "zeta/late", "dup/one"])
    check("名次连续 1..N", [x["rank"] for x in got], [1, 2, 3, 4, 5])
    check("重复仓库只留一条", len([x for x in got if x["title"] == "dup/one"]), 1)
    it = got[0]
    check("题图 = og 官方分享卡 + 仓库全名", it["image"], ns["OSS_IMAGE_BASE"] + "alpha/top")
    check("heat 就是星标数（前端热度位复用同一字段）", it["heat"], 50000)
    check("stars 单独也带上", it["stars"], 50000)
    check("栏目名", it["cls"], OSS_CLS)
    check("来源标签", it["label"], ns["OSS_SOURCE_LABEL"])
    check("分区固定在 cn（这一栏不参与国内/国际分流）", it["region"], "cn")
    check("通道 id", it["channel"], ns["OSS_SOURCE_ID"])
    check("链接是过闸的那个", it["link"], "https://github.com/alpha/top")
    check("语言带上", it["lang"], "Go")
    check_true("摘要中文打头", it["desc"].startswith("GitHub 近 30 天新建"), it["desc"])
    check_true("没开翻译时标明「简介原文」", "简介原文：A tiny thing" in it["desc"], it["desc"])
    check("翻译没开就不冒充中文", it["translated"], False)
    # created_text 按东八区算日子：UTC 20:00 已是北京时间第二天凌晨 4 点
    z = mk({"items": [repo("x/y", 10, created="2026-10-05T20:00:00Z")]},
           30, 20, FIXED_TODAY, None)[0]
    check("建于 X 用北京时间落到哪一天", z["created_text"], "2026-10-06")
    check("published 是创建时刻的 epoch", z["published"],
          int(datetime(2026, 10, 5, 20, 0, 0, tzinfo=utc).timestamp()))

    print("---- 4) 条数上限（榜单只留前 N 名，全量留在服务端）")
    many = [repo("m/r%d" % i, 1000 - i) for i in range(35)]
    big = mk({"items": many}, 30, ns["OSS_LIMIT"], FIXED_TODAY, None)
    check("截到 OSS_LIMIT", len(big), ns["OSS_LIMIT"])
    check("上限就是 20（用户定的『一屏能扫完』）", ns["OSS_LIMIT"], 20)
    check("截完名次仍从 1 连续", [x["rank"] for x in big], list(range(1, ns["OSS_LIMIT"] + 1)))
    check("留的是星最高的那批", big[-1]["stars"], 981)

    print("---- 5) 闸门：题材 / archived / 地址 —— 宁可少几条，不放过一条")
    rx = ns["_OSS_BLOCK_RX"]
    for w in ("malware", "keylogger", "phishing", "色情", "情报", "政治", "军事", "舆情"):
        check_true("词表命中 %s" % w, bool(rx.search("anything " + w + " anything")))
    one = lambda **kw: mk({"items": [repo("bad/one", 5000, **kw)]}, 30, 20, FIXED_TODAY, None)
    check("干净仓库不误杀（名字里带 bad 不算敏感词）", len(one(desc="A tiny thing")), 1)
    check("简介命中敏感词 → 剔除", one(desc="舆情监控与政治施压工具"), [])
    check("topics 命中 → 剔除", one(topics=["malware", "keylogger"]), [])
    check("语言分类是成人向 → 剔除", one(lang="Porn"), [])
    check("archived（站方自己都不维护了）→ 剔除",
          mk({"items": [dict(repo("old/five", 1500), archived=True)]},
             30, 20, FIXED_TODAY, None), [])
    # 名字里带敏感词也要拦住：上面那条 "bad/one" 是干净名，这里换成会命中的名
    check("仓库名命中 → 剔除", mk({"items": [repo("nsfw/demo", 1500, desc="")]},
                                30, 20, FIXED_TODAY, None), [])
    bads = mk({"items": [repo("a/ok", 100),
                         dict(repo("b/js", 90), html_url="javascript:alert(1)"),
                         dict(repo("c evil", 80), html_url="https://evil.example/b/c"),
                         {"full_name": "", "html_url": "https://github.com/x/y",
                          "stargazers_count": 9}]}, 30, 20, FIXED_TODAY, None)
    check("坏地址/怪名字一律剔除", [x["title"] for x in bads], ["a/ok"])
    check("payload 缺 items 也不炸（给空榜，不是报错）", mk({}, 30, 20, FIXED_TODAY, None), [])
    check("payload 是 None 也不炸", mk(None, 30, 20, FIXED_TODAY, None), [])

    print("---- 6) 翻译器是注入的：开了才出中文，坏了也不影响出榜")
    dic = {"A tiny thing": "一件小工具"}
    tr = mk({"items": [repo("t/one", 700, desc="A tiny thing")]},
            30, 20, FIXED_TODAY, lambda s: dic.get(s, ""))
    check_true("有译文时直接用译文", "｜一件小工具" in tr[0]["desc"], tr[0]["desc"])
    check("有译文才标 translated", tr[0]["translated"], True)

    def boom(s):
        raise RuntimeError("翻译挂了")
    br = mk({"items": [repo("t/two", 700, desc="A tiny thing")]}, 30, 20, FIXED_TODAY, boom)
    check("翻译抛异常不影响这一条出榜", len(br), 1)
    check_true("退回原文标注", "简介原文：A tiny thing" in br[0]["desc"], br[0]["desc"])

    print("---- 7) 接线：接口 / 健康度 / 预热 / 定时轮 / 前端栏目")
    api = [n for n in fnodes(tree, "api_oss")
           if n.decorator_list and "api/oss" in ast.dump(n.decorator_list[0])]
    check("/api/oss 已注册", len(api), 1)
    check_true("健康度面板登记了这一栏",
               calls_inside(tree, "_all_sources_meta", "OSS_ENABLED")
               or "OSS_SOURCE_ID" in "".join(ast.dump(n) for n in fnodes(tree, "_all_sources_meta")))
    check_true("开机预热 kick 了一次", calls_inside(tree, "_warmup", "_kick_oss"))
    check_true("定时轮也跟着 kick", calls_inside(tree, "refresh_loop", "_kick_oss"))
    check_true("首屏 SSR 带了这一栏", calls_inside(tree, "index", "_oss_snapshot"))
    idx = open(INDEX, encoding="utf-8").read()

    def line_of(text, needle):
        for ln in text.splitlines():
            if needle in ln:
                return ln
        return ""

    check_true("左栏栏目顺序里有「开源热榜」",
               "开源热榜" in line_of(idx, "const CLS_ORDER"), line_of(idx, "const CLS_ORDER"))
    check_true("首屏从 SSR 拿这一栏", "SSR.oss" in idx or "SSR && SSR.oss" in idx)
    check_true("前端拉的是 /api/oss", 'fetch("/api/oss")' in idx)
    check_true("卡片有星标徽章", 'class="tag star"' in idx)
    check_true("星标徽章的 CSS 定义了", ".tag.star" in idx)
    check_true("窗口天数不写死在前端", "SSR.oss_win" in idx or "SSR && SSR.oss_win" in idx)

    # 【2026-10-28 实机验证抓到的坑】左栏在国内 / 国际两个分区都摆这个 tab，
    # 但渲染那行原先写成 `(isCn && Array.isArray(state.oss))` —— 国际分区点「开源热榜」
    # 进去是个空栏（tab 上有 20 条、页面 0 条）。tab 显示条件与渲染条件必须同源。
    gate = line_of(idx, "const oss = ")
    check_true("渲染侧取这一栏的数据不看分区（国际点进去也得有内容）",
               "Array.isArray(state.oss)" in gate and "isCn" not in gate, gate)
    tab = line_of(idx, "set.push(OSS_CLS)")
    check_true("左栏 tab 的显示条件只看这份数据、不看分区",
               "state.oss" in tab and "region" not in tab and "isCn" not in tab, tab)

    print("---- 7b) 题图那条路：og 卡没扩展名，得按图片判，还要预热")
    fetch_src = "".join(ast.unparse(n) for n in fnodes(tree, "_fetch"))
    check_true("「GitHub 现画图没有扩展名」这个特判还在",
               "opengraph.githubassets.com" in fetch_src, fetch_src[:120])
    check_true("抓到榜之后预热题图", calls_inside(tree, "_oss_refresh", "_oss_prewarm"))
    check_true("冷启动只读到本机缓存时也预热题图（否则首屏 6 张每张等 1~10 秒）",
               calls_inside(tree, "_warmup", "_oss_prewarm"))
    check_true("开机读盘也把这一栏记进源健康度（不然面板里永远 idle）",
               calls_inside(tree, "_warmup", "_note_src_health"))
    check("健康度那一行的身份只有一个出处", len(fnodes(tree, "_oss_meta")), 1)
    check_true("预热走的是同一条 _fetch（进 _IMG_CACHE，不是文档表）",
               calls_inside(tree, "_oss_prewarm", "_fetch"))

    print("---- 8) 这一栏**没有**混进新闻链路（写进去就要动五处已定口径）")
    reg = [s.get("id") for _rg in ("cn", "intl", "hot")
           for s in ns["SOURCES"].get(_rg, []) if s.get("cls") == OSS_CLS]
    check("SOURCES 里没有 cls=开源热榜 的源", reg, [])
    check_true("开源热榜一节不碰新闻时效表",
               "MAX_AGE_HOURS" not in src[src.index("# ============ 开源热榜"):
                                          src.index('@app.get("/api/version")')])
    check("窗口天数 = 30（用户选的『新锐榜』口径）", ns["OSS_WINDOW_DAYS"], 30)
    check("抓取间隔 6 小时（榜单一天挪不了几次）", ns["OSS_TTL"], 6 * 3600)
    check("失败后 10 分钟内不重复撞", ns["OSS_FAIL_TTL"], 600)

    print("---- 9) 对外文案红线词（新增这一节的代码与注释一起扫）")
    sec = src[src.index("# ============ 开源热榜"):src.index('@app.get("/api/version")')]
    hit = [w for w in ("绕过", "翻墙", "梯子", "科学上网", "破解", "屏蔽", "墙",
                       "参赛", "评委", "组委会") if w in sec]
    check("服务端「开源热榜」一节无红线词", hit, [])
    hit2 = [w for w in ("绕过", "翻墙", "梯子", "科学上网", "破解", "屏蔽", "墙") if w in idx]
    check("页面全文无红线词", hit2, [])

    print("\n%s  失败 %d 条" % ("全部通过" if not FAILS else "有失败", len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
