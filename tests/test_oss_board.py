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
              "_oss_link", "_oss_desc", "_oss_items_from_payload",
              # 【2026-11-12】周榜：只做定义与接线断言，不在这里调用（它们要碰网络 / 气泡）
              "_oss_week_load_disk", "_oss_week_announce", "_oss_week_refresh",
              "_kick_oss_week",
              # 【2026-11-12 三修】涨速榜：三个纯函数直接跑口径，其余只做接线断言
              "_oss_speed_fallback", "_oss_speed_measure", "_oss_speed_merge",
              "_oss_speed_load_disk", "_oss_speed_save_disk",
              "_oss_speed_snapshot", "_oss_speed_rebuild", "_oss_speed_seed",
              # 【2026-11-12 十六修】修「速榜恒空」：真值时间 / stale 标记出口
              "_oss_speed_meta",
              # 【2026-11-12 四修】旧缓存 speed_day 兼容（部署复核抓出来的真问题）
              "_oss_speed_normalize",
              # 【2026-11-12 五修】暴涨气泡：纯函数挑选项 + 播报函数只做接线断言
              "_oss_pick_boom", "_oss_boom_announce",
              # 【2026-11-12 七修】按速度依次挑 + 新闻轮播固定开源席 + 共用条目构造
              "_oss_boom_entry", "_oss_carousel_seat", "_fmt_num",
              # 【2026-11-12 八修】开源席中文简介（全中文说明项目干什么）
              # 【2026-11-12 十修】剥掉英文项目名前缀 + 背景换项目头像
              # 【2026-11-12 十二修】气泡大字必须有中文用途说明（不播标签式兜底）
              "_oss_cn_intro", "_oss_cn_usable", "_oss_avatar_url",
              # 【2026-11-12 十四修 / 十五修】网页卡片描述行：纯中文用途说明 + 出口补字段/过滤
              "_oss_card_intro", "_oss_with_intro", "_oss_display_rows",
              # 【2026-11-12 九修】气泡抬头拆分：周榜气泡抬头要正确取到「开源周榜」
              "_balloon_split_head"}
WANT_CONSTS = {"OSS_ENABLED", "OSS_CLS", "OSS_SOURCE_ID", "OSS_SOURCE_LABEL",
               "OSS_WINDOW_DAYS", "OSS_LIMIT", "OSS_POOL_EXTRA", "OSS_TTL", "OSS_FAIL_TTL",
               "OSS_SEARCH_URL", "OSS_IMAGE_BASE", "OSS_AVATAR_HOST", "OSS_NAME_RX", "_OSS_BLOCK_RX",
               "OSS_WEEK_DAYS", "OSS_WEEK_LIMIT", "OSS_WEEK_TTL",
               "OSS_WEEK_MIN_VELOCITY", "OSS_WEEK_SHOWN_TTL", "OSS_SPEED_LIMIT",
               "OSS_SPEED_MIN_HOURS", "OSS_SPEED_SEED_TTL", "OSS_SPEED_BASE_MAX_AGE",
               "OSS_TOAST_MS", "BALLOON_PER_ITEM_MS",
               "OSS_BOOM_TOAST_GAP", "OSS_BOOM_TOAST_PICK", "APP_URL",
               "_GH_BADGE_PNG_B64", "SOURCES"}

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
    check_true("URL per_page=要 20 条 + 20 条候选余量（筛完仍够 20）",
               "per_page=%d" % (20 + ns["OSS_POOL_EXTRA"]) in u, u)
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
            repo("mid/middle", 1200, desc="A mid thing"), repo("beta/second", 3000),
            repo("dup/one", 100), repo("dup/one", 100)]
    got = mk({"items": rows}, 30, 20, FIXED_TODAY, None)
    check("按累计星标降序", [x["title"] for x in got],
          ["alpha/top", "beta/second", "mid/middle", "zeta/late", "dup/one"])
    check("名次连续 1..N", [x["rank"] for x in got], [1, 2, 3, 4, 5])
    check("重复仓库只留一条", len([x for x in got if x["title"] == "dup/one"]), 1)
    # 【十五修】用户「没有介绍，怎么上的榜？用户为什么会选择？」：榜单原来只看
    # 星标 / 涨速，作者连一句简介都没填的仓库照样占位。源头把空简介挡掉，
    # 老缓存里残留的空简介由出口 _oss_display_rows 二次挡掉（双保险）。
    _no_desc = mk({"items": [repo("empty/one", 9000, desc=""),
                             repo("filled/two", 8000, desc="A useful tool")]},
                  30, 20, FIXED_TODAY, None)
    check("空简介仓库不进榜（源头准入）", [x["title"] for x in _no_desc], ["filled/two"])
    check_true("准入只认站方 description，空串 / None 都挡",
               "if not base:" in src
               and 'base = _clean(r.get("description") or "")' in src)
    # 【2026-11-13】用户要求「三榜都必须 20 条」：候选池按名次多抓 20 条，
    # 闸门筛掉空简介后仍要补满 20，且补位上来的不能是空简介。
    _pool = [repo("full/repo%02d" % i, 1000 - i, desc="Useful tool %d" % i) for i in range(30)]
    _pool += [repo("empty/repo%02d" % i, 999, desc="") for i in range(10)]
    _filled = mk({"items": _pool}, 30, 20, FIXED_TODAY, None)
    check("候选池筛完仍补满 20 条", len(_filled), 20)
    check("补位后名次连续 1..20", [x["rank"] for x in _filled], list(range(1, 21)))
    check_true("补位上来的全是有简介的",
               all(not x["title"].startswith("empty/") for x in _filled),
               str([x["title"] for x in _filled]))
    it = got[0]
    check("题图 = og 官方分享卡 + 仓库全名（页面卡片仍用它）",
          it["image"], ns["OSS_IMAGE_BASE"] + "alpha/top")
    # 【十修】owner.avatar_url 带 ?v=4 时拼 s=460 必须保留问号，否则拼成
    # /u/123v=4&s=460（404，气泡背景空白）。真缓存全是带 query 的形式。
    _r = repo("owner/avatared", 5)
    _r["owner"] = {"avatar_url": "https://avatars.githubusercontent.com/u/123?v=4"}
    _av = mk({"items": [_r]}, 30, 20, FIXED_TODAY, None)[0]["avatar"]
    check("头像 URL 保留原 query 且补 s=460（真缓存带 ?v=4）", _av,
          "https://avatars.githubusercontent.com/u/123?v=4&s=460")
    check_true("头像 URL 只认 GitHub 官方域",
               "avatars.githubusercontent.com" in _av and "opengraph" not in _av)
    check("heat 就是星标数（前端热度位复用同一字段）", it["heat"], 50000)
    check("stars 单独也带上", it["stars"], 50000)
    check("栏目名", it["cls"], OSS_CLS)
    check("来源标签", it["label"], ns["OSS_SOURCE_LABEL"])
    check("分区固定在 cn（这一栏不参与国内/国际分流）", it["region"], "cn")
    check("通道 id", it["channel"], ns["OSS_SOURCE_ID"])
    check("链接是过闸的那个", it["link"], "https://github.com/alpha/top")
    check("语言带上", it["lang"], "Go")
    check_true("摘要中文打头、按栏目叫「开源月榜」",
               it["desc"].startswith("GitHub 开源月榜"), it["desc"])
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
    check("仓库名命中 → 剔除", mk({"items": [repo("nsfw/demo", 1500, desc="demo")]},
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

    # 【2026-11-12 用户定】开源已归类到「软件」栏目：左栏不再单列「开源热榜」按钮。
    check_true("开源归类到软件：下拉不再 push OSS_CLS",
               'set.push(OSS_CLS)' not in idx and 'if(_ossAlive && !set.includes("软件")) set.push("软件")' in idx)
    check_true("开源归类到软件：左栏计数并进软件",
               'cnt.set("软件", (cnt.get("软件") || 0) + activeOss().items.length)' in idx)
    check_true("气泡深链落到软件栏（不再选独立开源栏）",
               '_sel.value = "软件"' in idx and 'setOssTab(_dq)' in idx)
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
    tab = line_of(idx, 'const _ossAlive =')
    check_true("软件栏（含开源）的显示条件只看这份数据、不看分区",
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

    print("---- 8b) 开源周榜（2026-11-12）：7 天窗口 / 日均涨星 / 桌面播报接线")
    # 为什么值得单开一节：30 天榜那套断言一个字不改，周榜必须证明自己用的是**另一把尺**，
    # 并且没有偷偷混进新闻链路（SOURCES / 时效表 / 气泡配额）。
    check("周榜窗口 = 7 天", ns["OSS_WEEK_DAYS"], 7)
    check("周榜 1 小时一批", ns["OSS_WEEK_TTL"], 3600)
    # 【十七修】积累期换批从 15 分钟收紧到 3 分钟：部署交接态基准与缓存同批时，
    # 必须靠下一批拉开时间差才出真值，15 分钟让用户空等太久；出真值后仍恢复 1 小时。
    check("积累期换批间隔缩到 3 分钟（首批真值尽快出）", ns["OSS_SPEED_SEED_TTL"], 180)
    check("周榜条数 40（廿三修扩池，减少重复播报）", ns["OSS_WEEK_LIMIT"], 40)
    check("30 天榜口径没动：窗口", ns["OSS_WINDOW_DAYS"], 30)
    check("30 天榜口径没动：间隔", ns["OSS_TTL"], 6 * 3600)
    # 钉死 2026-10-28：老项目星多但涨得慢，新项目星少但涨得快。
    # 生产里「7 天内」由 GitHub 查询串的 created:> 保证，映射函数只负责排序与闸门。
    wrows = [
        repo("old/big", 9000, created="2026-10-07T00:00:00Z"),   # 21.4 天 → 日均约 421
        repo("new/fast", 6000, created="2026-10-25T00:00:00Z"),  # 3.4 天 → 日均约 1765
    ]
    month = mk({"items": wrows}, 30, 20, FIXED_TODAY, None)
    week = mk({"items": wrows}, 7, 20, FIXED_TODAY, None, True)
    check("30 天榜仍按累计星标排（老口径不动）",
          [x["title"] for x in month], ["old/big", "new/fast"])
    check("周榜按日均涨星排（上星快的新项目在前）",
          [x["title"] for x in week], ["new/fast", "old/big"])
    check("周榜名次仍连续", [x["rank"] for x in week], [1, 2])
    check_true("日均涨星字段：新项目 > 老项目",
               week[0]["velocity"] > week[1]["velocity"], str([x["velocity"] for x in week]))
    check("30 天榜不带日均涨星字段", month[0].get("velocity", 0), 0)
    check_true("周榜摘要写明排序依据", "日均涨星" in week[0]["desc"], week[0]["desc"])
    check_true("周榜摘要按栏目叫「开源周榜」，不再写「近 7 天新建」",
               week[0]["desc"].startswith("GitHub 开源周榜")
               and "近 7 天新建" not in week[0]["desc"], week[0]["desc"])
    nz = mk({"items": [repo("x/nodate", 500, created="")]},
            7, 20, FIXED_TODAY, None, True)
    check_true("缺建库时间不除零、按窗口兜底",
               len(nz) == 1 and 0 < nz[0]["velocity"] <= 500, str(nz[:1]))
    check_true("题材闸门复用同一个映射函数",
               calls_inside(tree, "_oss_week_refresh", "_oss_items_from_payload"))
    check_true("/api/oss 回带周榜（只增字段，老调用方不变）",
               calls_inside(tree, "api_oss", "_oss_week_snapshot"))
    check_true("首屏 SSR 带周榜快照", calls_inside(tree, "index", "_oss_week_snapshot"))
    check_true("开机预热读周榜缓存", calls_inside(tree, "_warmup", "_oss_week_load_disk"))
    check_true("刷新轮跟着 kick 周榜", calls_inside(tree, "_kick_oss", "_kick_oss_week"))
    check_true("周榜换批会走桌面播报", calls_inside(tree, "_oss_week_refresh", "_oss_week_announce"))
    check_true("桌面播报走独立气泡出口（不占新闻 10 席）",
               calls_inside(tree, "_oss_week_announce", "_show_toast_list"))
    check_true("前端有 30 天 / 7 天 切换按钮",
               'data-osstab="week"' in idx and 'data-osstab="speed"' in idx
               and 'data-osstab="month"' in idx)
    # 【2026-11-12 用户定】GitHub 三榜归类进「软件」栏目，速榜空档时软件栏仍在。
    check_true("软件栏里画 GitHub 三榜（用户：开源归类到软件）",
               'cls === "软件" && _ossAliveTop()' in idx
               and 'html += ossBoardHTML()' in idx
               and 'html += ossSwitch' in idx)
    check_true("软件栏三档任一有数据就常驻（速榜空档不消失）",
               'const _ossAlive =' in idx and 'cnt.set("软件"' in idx)
    check_true("开源榜正文抽成共用函数（两处口径一致）",
               idx.count('function ossBoardHTML()') == 1
               and 'ossSwitch + ossBoardHTML()' in idx)
    # 【2026-11-12 用户实测再修】首页「全部」视图不再露开源预览：
    # 用户「首页第一次打开会出现周榜，点速榜就消失了」「喊你放进软件栏目，怎么还是在外面」。
    check_true("首页全部视图不再插开源预览段",
               'if(cls === "" && oss.length)' not in idx
               and '首页「全部」视图不再露开源榜那段预览' in idx)
    check_true("分母只在软件栏算开源条数",
               'const _tot = state.items.length + ((cls === "软件" && oss.length) ? oss.length : 0)' in idx)
    check_true("前端按服务端给的窗口天数显示", "state.ossWeekWin" in idx)
    check_true("页面轮询改成 2 分钟", "120000" in idx)
    check_true("切回前台立刻拉一次", "visibilitychange" in idx)
    check_true("周榜徽章样式已定义", ".tag.vel" in idx)

    print("---- 8c) 及时投送的三处修正（2026-11-12 实测后补）")
    # ① 首次抓到周榜也要弹：只建基线不弹的话，用户装完得等 1 小时才看见桌面投送。
    #    注入一个假的气泡出口，直接跑这段逻辑（不发网络、不碰 GUI、不写本机目录）。
    import time as _time
    import json as _json
    ann_nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == "_oss_week_announce"]
    ns2 = {"time": _time, "os": os, "json": _json, "APP_URL": "http://127.0.0.1:8000",
           "OSS_ENABLED": True, "OSS_WEEK_MIN_VELOCITY": ns["OSS_WEEK_MIN_VELOCITY"],
           "OSS_WEEK_TOAST_GAP": 600, "OSS_WEEK_SHOWN_TTL": 86400,
           "_OSS_WEEK_TOAST_AT": 0.0, "_OSS_WEEK_SHOWN": {},
           "_oss_week_shown_save": lambda: None}
    _calls = []
    ns2["_show_toast_list"] = lambda entries, replace=False: _calls.append(list(entries))
    # 九修：这一路改为复用 _oss_boom_entry，测试沙箱也要把纯函数依赖带进来。
    for _k in ("_oss_boom_entry", "_oss_cn_intro", "_oss_cn_usable", "_fmt_num",
               "_oss_link", "OSS_NAME_RX", "_balloon_split_head"):
        ns2[_k] = ns[_k]
    exec(compile(ast.Module(body=ann_nodes, type_ignores=[]), SERVER, "exec"), ns2)
    ns2["_oss_week_announce"]([], [{"title": "x/fast", "velocity": 2200,
                                    "stars": 6266, "rank": 1, "age_days": 2.8,
                                    "created_text": "2026-11-09", "image": "",
                                    "link": "https://github.com/x/fast",
                                    "desc": "GitHub 开源周榜 · Swift · 日均涨星 2200｜"
                                            "适用于 iOS 的 MobileGestalt 编辑器，具备区域代码调整功能。"}])
    check("首次抓到周榜也投一条桌面气泡", len(_calls), 1)
    _wk_entry = tuple(_calls[0][0])
    check_true("周榜抬头是「开源周榜」、GitHub 徽章生效",
               _wk_entry[0].startswith("【开源周榜】"), _wk_entry[0])
    check_true("正文只有中文用途说明，不含英文仓库名/星数/涨幅数字",
               "MobileGestalt 编辑器" in _wk_entry[1] and "x/fast" not in _wk_entry[1]
               and "6266" not in _wk_entry[1] and "2200" not in _wk_entry[1],
               _wk_entry[1])
    check_true("涨幅数字单独走第 7 元（右下角那一行）",
               _wk_entry[6] == "日均+2200星/天", str(_wk_entry[6]))
    check_true("点「打开」直接进 GitHub 项目页，不再跳本机榜单页",
               _wk_entry[3] == "https://github.com/x/fast", _wk_entry[3])
    check_true("meta 写「周榜」口径，不再写「7 天新建」",
               _wk_entry[5].startswith("周榜") and "7 天新建" not in _wk_entry[5],
               _wk_entry[5])
    check_true("气泡正文不带任何括号附加说明（用户 11-12 要求）",
               "（" not in str(_wk_entry[1]) and "）" not in str(_wk_entry[1]),
               str(_wk_entry[1]))
    check_true("气泡 meta 不带「每 N 小时一批」更新节奏（用户 11-12 要求）",
               "一批" not in str(_wk_entry[5]))
    # 【2026-11-12 用户定】「气泡里的文字要看全，不能有残缺」。
    _body = str(_wk_entry[1])
    check_true("周榜正文不带省略号（文字看全）", "…" not in _body and "..." not in _body, _body)
    check_true("周榜正文不带建库日期/引导语（这两段把正文挤出两行）",
               "建于" not in _body and "点「打开」" not in _body, _body)
    check_true("周榜正文足够短，两行字装得下（<=36 字）", len(_body) <= 36,
               "%d 字: %s" % (len(_body), _body))
    # 绘制端兜底：正文装不下时自动退字号，不往末尾收「…」（超长仓库名也不残缺）。
    check_true("正文自适应字号方法存在（_text_fits_lines + _body_font）",
               len(fnodes(tree, "_text_fits_lines")) == 1 and len(fnodes(tree, "_body_font")) == 1)
    _bl = "\n".join(ast.unparse(n) for n in fnodes(tree, "_paint_live")) + \
          "\n".join(ast.unparse(n) for n in fnodes(tree, "_paint_spot"))
    check_true("速递/聚光灯两档正文都走自适应字号",
               _bl.count("self._body_font(") >= 2)
    check_true("已播名单落盘（跨重启不重播）",
               calls_inside(tree, "_oss_week_announce", "_oss_week_shown_save"))
    check_true("开机读回已播名单", calls_inside(tree, "_warmup", "_oss_week_shown_load"))
    # ② 气泡左上角 GitHub 标志：三种播报版式 + 卡片兜底都要压上这枚徽章。
    check_true("气泡左上角 GitHub 徽章方法存在", len(fnodes(tree, "_draw_github_mark")) == 1)
    check_true("播报版式压上 GitHub 徽章",
               calls_inside(tree, "_paint_bcast", "_draw_github_mark"))
    check_true("卡片兜底版也压上 GitHub 徽章",
               calls_inside(tree, "_wndproc", "_draw_github_mark"))
    # 九修：此前把 GDI+ 函数错当成 gdi32 的 self.g 调，日志连续 AttributeError，
    # 徽章根本没画出来（用户截图只看到「热点新闻」）。必须钉死走 gdiplus。
    _badge_src = ast.unparse(fnodes(tree, "_draw_github_mark")[0])
    check_true("徽章绘制走 gdiplus，不再误用 gdi32 的 self.g",
               "gp = self.gdip or ct.windll.gdiplus" in _badge_src
               and "gp.GdipDrawImageRectI" in _badge_src
               and "g.GdipDrawImageRectI" not in _badge_src, _badge_src[:120])
    check_true("开源席不再并排画「热点新闻」遮挡 GitHub 标",
               len(fnodes(tree, "_is_oss_cur")) == 1
               and "if not _oss:" in ast.unparse(fnodes(tree, "_paint_spot")[0]))
    check_true("涨星数字由右下角绘制函数接管",
               len(fnodes(tree, "_draw_oss_growth")) == 1
               and calls_inside(tree, "_paint_spot", "_draw_oss_growth"))
    # ③ 页面：周榜在左、默认就落在周榜、空档不再干等 2 分钟。
    check_true("按钮文字按用户写法：速榜/周榜/月榜，没有多余的字",
               ">速榜</button>" in idx and ">周榜</button>" in idx
               and ">月榜</button>" in idx
               and "7 天周榜 ·" not in idx and "30 天榜 ·" not in idx
               and "涨速榜 · 星/天" not in idx)
    check_true("默认落在周榜", 'state.ossTab = "week"' in idx)
    check_true("开源栏标题按栏目叫周榜/速榜/月榜，不写「近 N 天新建」",
               "开源周榜 · 按日均涨星排" in idx and "开源速榜 · 按真实净增星/天排" in idx
               and "开源月榜 · 按累计星标排" in idx
               and "开源热榜 · GitHub 近" not in idx)
    check_true("空档按 8 秒快速补拉", "OSS_WEEK_RETRY < 10" in idx)
    check_true("空档提示写明正在抓取", "周榜正在抓取" in idx)

    print("---- 8d) 第三档「涨速榜」（2026-11-12 三修）：两榜并集 / 星每天 / 气泡提示下线")
    # 用户点破「和 7 天周榜会不会重复」：只把周榜 20 条重排等于白加一档。
    # 下面钉死：候选是两榜并集、每条按自己那一路的间隔算速度、首轮不冒充真值。
    check("涨速榜 40 条（廿三修扩池）", ns["OSS_SPEED_LIMIT"], 40)
    check("两批间隔下限 0.25 小时", ns["OSS_SPEED_MIN_HOURS"], 0.25)
    _now = 1770000000.0
    _pw = {"ts": _now - 3600, "stars": {"w/a": 100, "w/b": 100}}
    _week_now = [
        {"title": "w/a", "stars": 150, "published": _now - 3 * 86400,
         "desc": "GitHub 近 7 天新建｜原文 A", "velocity": 50},
        {"title": "w/b", "stars": 110, "published": _now - 3 * 86400,
         "desc": "GitHub 近 7 天新建｜原文 B", "velocity": 36},
    ]
    _wr = ns["_oss_speed_measure"](_week_now, _pw, _now, 7)
    check("周路按自己的 1 小时间隔算净增", [x["delta"] for x in _wr], [50, 10])
    check("周路速度 = 净增 ÷ 1 小时", [x["speed"] for x in _wr], [50.0, 10.0])
    check("周路展示口径 = 星/小时 × 24 = 星/天", [x["speed_day"] for x in _wr], [1200.0, 240.0])
    check_true("摘要写明星/天且保留真实间隔小时",
               "折合" in _wr[0]["desc"] and "星/天" in _wr[0]["desc"] and "小时净增" in _wr[0]["desc"],
               _wr[0]["desc"])
    check("周路标为有真实净增", [x["has_speed"] for x in _wr], [True, True])
    check("涨速榜不再显示周榜那颗日均涨星徽章", [x["velocity"] for x in _wr], [0, 0])
    check("周路窗口标成 7 天", _wr[0]["speed_window"], 7)
    # 30 天榜 6 小时前那一批：c 涨 120 星，按 6 小时折算 = 20 星/小时 = 480 星/天（不是 120）
    _pm = {"ts": _now - 6 * 3600, "stars": {"m/c": 600}}
    _month_now = [
        {"title": "m/c", "stars": 720, "published": _now - 20 * 86400,
         "desc": "GitHub 近 30 天新建｜原文 C", "velocity": 0},
    ]
    _mr = ns["_oss_speed_measure"](_month_now, _pm, _now, 30)
    check("月路按自己的 6 小时间隔折算", _mr[0]["speed"], 20.0)
    check("月路展示口径 480 星/天", _mr[0]["speed_day"], 480.0)
    check("月路窗口标成 30 天", _mr[0]["speed_window"], 30)
    _merged = ns["_oss_speed_merge"](_wr, _mr)
    check("涨速榜是两榜并集（3 个不同仓库，不是周榜那批重排）", len(_merged), 3)
    check("按星/天降序（与星/小时线性等价）：周榜 a > 月榜 c > 周榜 b",
          [x["title"] for x in _merged], ["w/a", "m/c", "w/b"])
    check("并集后名次连续", [x["rank"] for x in _merged], [1, 2, 3])
    _dup = ns["_oss_speed_merge"](
        _wr, [{"title": "w/a", "stars": 999, "has_speed": True, "speed": 999.0,
               "delta": 999, "speed_hours": 6.0, "speed_window": 30}])
    check("同一仓库两路都出现只留一条", len([x for x in _dup if x["title"] == "w/a"]), 1)
    check("同名时优先留 7 天周榜那条",
          [x for x in _dup if x["title"] == "w/a"][0]["speed_window"], 7)
    _first = ns["_oss_speed_measure"](
        [{"title": "n/new", "stars": 300, "published": _now - 86400,
          "desc": "GitHub 近 7 天新建｜原文 N", "velocity": 0}],
        {"ts": 0.0, "stars": {}}, _now, 7)
    check("首轮不冒充真实涨速", _first[0]["has_speed"], False)
    check("首轮净增为 0", _first[0]["delta"], 0)
    check("首轮按日均涨星兜底", _first[0]["fallback"], 300)
    check_true("首轮摘要写明「暂无对比」", "暂无对比" in _first[0]["desc"], _first[0]["desc"])
    _mix = ns["_oss_speed_merge"](_wr, _first)
    check("首轮待对比条目不进榜（不出平均数假榜）",
          [x["title"] for x in _mix], ["w/a", "w/b"])
    check_true("只有真值时也不掺假榜",
               all(x.get("has_speed") for x in _mix))
    _only_first = ns["_oss_speed_merge"]([], _first)
    # 【2026-11-12 十一修】用户「速榜和周榜内容又高度重叠」「都是待对比」重新拍板：
    # 速榜只认真实净增，一条真值都没有就出空榜，绝不拿日均涨星摆一屏「待对比」。
    check("一条真值都没有 → 空榜（不摆观察榜）", len(_only_first), 0)
    # 真实测过但这批没涨星（delta=0）也不能占榜，避免「净增 0 星/天」凑数。
    _zero = [{"title": "z/zero", "stars": 500, "has_speed": True, "speed": 0.0,
              "delta": 0, "speed_day": 0.0, "speed_hours": 1.0, "speed_window": 7}]
    check("测过但 delta=0 → 也不进速榜", len(ns["_oss_speed_merge"]([], _zero)), 0)
    _real = [{"title": "r/real", "stars": 900, "has_speed": True, "speed": 12.0,
              "delta": 12, "speed_day": 288.0, "speed_hours": 1.0, "speed_window": 7}]
    check("真涨了星 → 进速榜且名次连续",
          [x["rank"] for x in ns["_oss_speed_merge"]([], _real)], [1])
    check_true("速榜 merge 不再引用 fallback 平均数",
               "fallback" not in "".join(ast.unparse(n) for n in fnodes(tree, "_oss_speed_merge")))
    _seed_fn = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "_oss_speed_seed"][0]
    _measure_args = [c.args[1] for c in ast.walk(_seed_fn)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                     and c.func.id == "_oss_speed_measure" and len(c.args) > 1]
    check_true("冷启动 seed 不再用空基准覆盖磁盘基准（修重启归零）",
               len(_measure_args) == 2
               and all("_OSS_SPEED_PREV" in ast.unparse(a) for a in _measure_args)
               and not any(isinstance(a, ast.Dict) for a in _measure_args))
    check_true("读盘在无真值时仍恢复 prev 基准",
               "_OSS_SPEED_PREV" in "".join(ast.unparse(n) for n in fnodes(tree, "_oss_speed_load_disk")))
    check_true("积累期把周榜对比间隔临时缩到 3 分钟",
               "OSS_SPEED_SEED_TTL" in "".join(ast.unparse(n) for n in fnodes(tree, "_kick_oss_week")))
    check_true("30 天榜换批喂给涨速榜", calls_inside(tree, "_oss_refresh", "_oss_speed_rebuild"))
    check_true("周榜换批喂给涨速榜", calls_inside(tree, "_oss_week_refresh", "_oss_speed_rebuild"))
    check_true("/api/oss 回带涨速榜", calls_inside(tree, "api_oss", "_oss_speed_snapshot"))
    check_true("首屏 SSR 带涨速榜", calls_inside(tree, "index", "_oss_speed_snapshot"))
    check_true("开机预热读涨速榜缓存", calls_inside(tree, "_warmup", "_oss_speed_load_disk"))
    check_true("涨速榜不额外发请求（重建函数里没有 _fetch）",
               not calls_inside(tree, "_oss_speed_rebuild", "_fetch"))
    # 用户要求：气泡最后一条不再追加更新条数提示（会挤占新闻正文）
    _notify = "".join(ast.unparse(n) for n in fnodes(tree, "_notify_news"))
    check_true("气泡正文末尾不再挂更新条数提示",
               "共新增" not in _notify and "本次共" not in _notify)
    check_true("前端有第三档按钮", 'data-osstab="speed"' in idx)
    check_true("速榜在最左、周榜居中、月榜在最右",
               idx.index('data-osstab="speed"') < idx.index('data-osstab="week"'
               ) < idx.index('data-osstab="month"'))
    check_true("默认仍落在周榜", 'state.ossTab = "week"' in idx)
    check_true("卡片有涨速徽章", 'class="tag spd"' in idx)
    check_true("卡片写星/天、页面不再出现星/小时（按钮文字已简化）",
               "星/天" in idx and "星/小时" not in idx
               and "涨速榜 · 星/天" not in idx)
    check_true("卡片徽章用 speed_day 显示星/天", "it.speed_day" in idx and "星/天" in idx)
    check_true("涨速徽章 CSS 已定义", ".tag.spd" in idx)
    check_true("applyOss 读 speed_items", "meta.speed_items" in idx)
    # 【端到端实测抓出来的两处】不修的话冷启动涨速榜会退化成周榜子集。
    check_true("冷启动有补齐函数（不让涨速榜退化成周榜子集）",
               len(fnodes(tree, "_oss_speed_seed")) == 1)
    check_true("开机预热会用两榜本机缓存补齐并集",
               calls_inside(tree, "_warmup", "_oss_speed_seed"))
    # 2026-11-12 四修：官方 Octocat 徽章 / 开源气泡 12 秒 / 旧缓存兼容
    check("开源榜气泡单独停 12 秒", ns["OSS_TOAST_MS"], 12000)
    check("普通新闻轮播仍是 5.6 秒", ns["BALLOON_PER_ITEM_MS"], 5600)
    check_true("气泡时长按栏目分流函数已接线",
               "def _toast_per_item_ms" in src and src.count("_toast_per_item_ms(") >= 7)
    _gh = ns["_GH_BADGE_PNG_B64"]
    check_true("徽章内嵌的是 PNG base64",
               _gh.startswith("iVBORw0KGgo") and len(_gh) > 4000, "len=%d" % len(_gh))
    check_true("徽章改用官方矢量图贴位图、不再画白圆两点",
               "GdipDrawImageRectI" in src and "两个深灰小圆点" not in src)
    _oldrow = {"title": "old/x", "delta": 5, "speed": 5.0, "speed_hours": 1.0,
               "has_speed": True, "speed_window": 7,
               "desc": "GitHub 近 7 天新建 · 近 1.0 小时净增 5 星（5.0 星/小时）｜原文"}
    _newrow = ns["_oss_speed_normalize"]([_oldrow])[0]
    check("旧缓存条目补出 120 星/天", _newrow["speed_day"], 120.0)
    check_true("旧缓存摘要改写为星/天",
               "折合 120.0 星/天" in _newrow["desc"] and "星/小时" not in _newrow["desc"],
               _newrow["desc"])
    check_true("读回缓存时先过兼容函数",
               "_oss_speed_normalize(d.get(\"items\")" in src
               and "[\"week_rows\"] = _oss_speed_normalize" in src)

    def _first_call(fn, name):
        return min((n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == name),
                   default=10 ** 9)

    _ref = fnodes(tree, "_oss_refresh")[0]
    _wkref = fnodes(tree, "_oss_week_refresh")[0]
    check_true("30 天榜：派生排在题图预热之前（图片不拖住榜单数据）",
               _first_call(_ref, "_oss_speed_rebuild") < _first_call(_ref, "_oss_prewarm"))
    check_true("周榜：派生也排在题图预热之前",
               _first_call(_wkref, "_oss_speed_rebuild") < _first_call(_wkref, "_oss_prewarm"))

    print("---- 8e) 暴涨气泡（2026-11-12 七修：10分钟一组 / 按速度降序不随机 / 必出开源）")
    check("播报间隔 20 分钟（廿三修降频）", ns["OSS_BOOM_TOAST_GAP"], 1200)
    check("每组 1 条（廿三修降频）", ns["OSS_BOOM_TOAST_PICK"], 1)
    _boom_wk = [
        {"title": "b/x", "stars": 5000, "velocity": 900, "age_days": 4.0},
        {"title": "b/y", "stars": 3000, "velocity": 600, "age_days": 5.0}]
    _boom_sp = [
        {"title": "s/a", "stars": 8000, "has_speed": True, "speed_day": 1200.0},
        {"title": "s/b", "stars": 4000, "has_speed": True, "speed_day": 700.0}]
    _picked = ns["_oss_pick_boom"](_boom_wk, _boom_sp, {}, _now, 2)  # 真值正好 2 条，不多取周榜补位
    check_true("有真值时优先挑真实净增的项目",
               all(x.get("boom_real") for x in _picked))
    check("真值按星/天降序", [x["title"] for x in _picked], ["s/a", "s/b"])
    _p1 = ns["_oss_pick_boom"](_boom_wk, [], {}, _now, 3)
    check_true("首轮退用日均涨星但标非真实",
               len(_p1) == 2 and all(x["boom_real"] is False for x in _p1))
    check("首轮按日均降序", [x["title"] for x in _p1], ["b/x", "b/y"])
    _shown = {"s/a": _now}
    _p2 = ns["_oss_pick_boom"](_boom_wk, _boom_sp, _shown, _now, 3)
    check_true("24 小时内播过的仓库不再挑",
               all(x.get("title") != "s/a" for x in _p2))
    # 【2026-11-12 七修】不随机、按速度降序依次；必出开源（候选全被去重挡光也回退全池）
    _pick_node = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "_oss_pick_boom"][0]
    _pick_src = ast.unparse(_pick_node)
    # 只看有没有真的调用；docstring 里「不再 random.sample」这句说明不算
    _has_rand_call = any(isinstance(x, ast.Call) and isinstance(x.func, ast.Attribute)
                         and x.func.attr == "sample" for x in ast.walk(_pick_node))
    check_true("不再用 random.sample（按速度依次取）", not _has_rand_call, _pick_src[:80])
    check("取涨速最快的前 2 条、严格降序",
          [x["title"] for x in ns["_oss_pick_boom"](_boom_wk, _boom_sp, {}, _now, 2)],
          ["s/a", "s/b"])
    _all_shown = {"s/a": _now, "s/b": _now, "b/x": _now, "b/y": _now}
    _p3 = ns["_oss_pick_boom"](_boom_wk, _boom_sp, _all_shown, _now, 3)
    check("候选全被 24h 挡光时宁缺不重复（廿三修）", [x["title"] for x in _p3], [])

    check_true("暴涨播报由刷新循环每轮敲门",
               calls_inside(tree, "refresh_loop", "_oss_boom_announce"))
    # 【2026-11-12 用户定】暴涨气泡正文同样「看全、无残缺」：
    # 老文案「名字：建库N天，N星；真实日增N星/天。点「打开」看开源榜」真数据 18/19 被收「…」。
    _boom_src = ast.unparse([n for n in tree.body if isinstance(n, ast.FunctionDef)
                             and n.name == "_oss_boom_entry"][0])
    check_true("暴涨正文不带建库天数/星数/引导语（三段把正文挤出两行）",
               "建库" not in _boom_src and "星；" not in _boom_src)
    check_true("暴涨正文只留项目用途，涨幅数字单独走右下角（九修）",
               "_oss_cn_intro" in _boom_src and "growth =" in _boom_src
               and "· 日增%s星/天" not in _boom_src
               and "· 日均+%s星/天" not in _boom_src)
    check_true("开源条目点「打开」进 GitHub、不再回本机榜单页",
               "_oss_link(" in _boom_src and "https://github.com/" in _boom_src
               and '"oss="' not in _boom_src)
    check_true("开源速榜徽章也走 12 秒 + GitHub 标",
               '("开源周榜", "开源热榜", "开源速榜")' in src
               and src.count('"开源速榜"') >= 2)
    check_true("速榜空档提示改为「正在积累首批对比数据、只列真涨星的项目」",
               "正在积累首批对比数据" in idx and "速榜首批观察中" not in idx
               and "只列真实涨了星的项目" in idx)
    # 【十六修】「约 15 分钟后出榜」承诺了一直不兑现，改成如实说明「真值会一直保留」。
    check_true("空态不再写不兑现的「约 15 分钟后出榜」",
               "约 15 分钟后出榜" not in idx
               and "一旦出现真值就会一直保留" in idx)
    check_true("页面速榜口径写明「不是日均涨星、和周榜不是同一批」",
               "不是日均涨星" in idx)

    print("---- 8f) 新闻轮播固定开源席（2026-11-12 用户定：每个轮播时间段必出开源）")
    # 条目构造共用：10 分钟暴涨播报与新闻轮播开源席同一份格式（GitHub 徽章 + 短正文 + 深链）
    _e = ns["_oss_boom_entry"](dict(_boom_sp[0], boom_real=True, boom_val=1200.0,
                                    image="https://opengraph.githubassets.com/1/owner/repo",
                                    desc="GitHub 近 7 天新建 · Swift · 日均涨星 774｜"
                                         "适用于 iOS 的 MobileGestalt 编辑器，具备区域代码调整功能。"))
    check_true("开源条目带 GitHub 徽章（12 秒 + 图标生效）", _e[0].startswith("【开源速榜】"))
    check_true("开源条目走全中文正文（看全无残缺、说明项目干什么）",
               "MobileGestalt 编辑器" in _e[1] and "日增" not in _e[1]
               and "看开源榜" not in _e[1] and "（" not in _e[1])
    check_true("涨幅数字落在第 7 元、右下角那一行显示",
               _e[6] == "日增1200星/天", str(_e[6]))
    check_true("开源条目带项目图（整卡铺图背景）", bool(_e[4]))
    # 轮播开源席（读本机缓存的函数，不能在单测 ns 里裸调）：用 AST 钉死接线口径。
    # 它必须：三档快照兜底 → 复用 _oss_pick_boom（真值优先+速度降序）→ mark 记账换项目。
    _seat_src = ast.unparse([n for n in tree.body if isinstance(n, ast.FunctionDef)
                             and n.name == "_oss_carousel_seat"][0])
    check_true("开源席读三档快照兜底、必出开源",
               "_oss_speed_snapshot" in _seat_src and "_oss_week_snapshot" in _seat_src
               and "_oss_snapshot" in _seat_src and "_oss_pick_boom" in _seat_src)
    check_true("开源席按速度取、mark 记账下一轮换项目",
               "_oss_boom_entry" in _seat_src and "seen[name] = now" in _seat_src)
    # 注入接线：_notify_news 里把开源席置顶第 1 条（八修，原来在最后一条看不见）
    _notify_src = ast.unparse([n for n in tree.body if isinstance(n, ast.FunctionDef)
                               and n.name == "_notify_news"][0])
    check_true("新闻轮播把开源席置顶第1条（八修）",
               "_oss_carousel_seat(mark=True)" in _notify_src
               and "_news_shown[:BALLOON_PLAY_COUNT - 1]" in _notify_src
               and "entries[-1] = _seat" not in _notify_src)
    check_true("被开源席让位那条不记已播（下一轮还回来）",
               "_balloon_mark_shown([_it.get(\"id\") or _it.get(\"title\") for _it in _news_shown]"
               in src)

    print("---- 8g) 八修：置顶必出 + 全中文说明 + 项目图背景 + 同事件不重复轮播")
    # 中文简介纯函数
    _it_intro = {"desc": "GitHub 近 7 天新建 · Swift · 日均涨星 2164｜"
                         "适用于 iOS 27.x 的 MobileGestalt 编辑器，具备区域代码调整、备份功能。",
                 "lang": "Swift"}
    check("中文简介取｜后用途、去括号、不截半句",
          ns["_oss_cn_intro"](_it_intro), "适用于 iOS 27.x 的 MobileGestalt 编辑器")
    check("无中文说明的条目退回中文兜底、不塞英文仓库名",
          ns["_oss_cn_intro"]({"desc": "GitHub 近 7 天新建 · C · 日均涨星 693", "lang": "C"}),
          "C 开源项目")
    # 【十二修】用户「要新闻内容，而不是标题」：这类标签式兜底不能进气泡大字。
    check("无中文用途说明 → usable=False",
          ns["_oss_cn_usable"]({"desc": "GitHub 开源周榜 · JavaScript · 日均涨星 346"}), False)
    check("有中文用途说明 → usable=True", ns["_oss_cn_usable"](_it_intro), True)
    check("只有英文原文简介 → usable=False",
          ns["_oss_cn_usable"]({"desc": "GitHub 开源月榜｜简介原文：A tool for something"}), False)
    _cn_good = {"title": "a/good", "velocity": 900,
                "desc": "GitHub 开源周榜 · Swift｜这是一个全功能的桌面备份工具。"}
    _cn_bad = {"title": "b/bad", "velocity": 5000,
               "desc": "GitHub 开源周榜 · JavaScript · 日均涨星 346"}
    check("候选池优先挑有中文内容的（速度更快的标签条目排后面）",
          ns["_oss_pick_boom"]([_cn_bad, _cn_good], [], {}, 1e9, 1)[0]["title"], "a/good")
    check("全池都没中文内容时仍回退兜底、保证必出开源",
          len(ns["_oss_pick_boom"]([_cn_bad], [], {}, 1e9, 1)), 1)
    _ann_src = ast.unparse([n for n in tree.body if isinstance(n, ast.FunctionDef)
                            and n.name == "_oss_week_announce"][0])
    check_true("周榜换批播报也过滤无中文内容的条目", "_oss_cn_usable" in _ann_src)
    check_true("轮播月榜兜底同样优先挑有中文内容的",
               "_oss_cn_usable" in _seat_src)
    # 【十二修】用户「缩放，遮挡了整张画面」→ 背景改成完整不放大（contain）
    # 【十三修 2026-11-13】用户实拍截图「排列不整齐」：contain 在扁卡上左右留两条深灰带，
    # 看着像图断了 → 改回 cover 铺满（zoom 仍锁 1.0，不推进放大）。
    _spot_src = ast.unparse([n for n in tree.body if isinstance(n, ast.ClassDef)
                             and n.name == "_Balloon"][0])
    check_true("开源席背景铺满卡面（cover，不推进放大、不再左右留深灰带）",
               "contain=False" in _spot_src and "zoom = 1.0 if _oss_spot" in _spot_src)
    check_true("_draw_thumb 支持 contain 分支且小图不放大",
               "if contain:" in _spot_src and "min(1.0, dw / float(iw), dh / float(ih))" in _spot_src)
    check_true("中文简介预算 30 字（两行字数预算内）",
               "def _oss_cn_intro(it, limit=30)" in src)
    # 【2026-11-12 十修】用户截图「中文介绍！！！！！要说几遍」：
    # 原文案把「AOMEI Backupper 2026：」这段纯英文项目名当中文用途输出了。
    _en_prefix = {"desc": "GitHub 开源周榜 · TypeScript · 日均涨星 240｜"
                          "AOMEI Backupper 2026：全功能桌面备份和恢复套件", "lang": "TypeScript"}
    check("英文项目名前缀被剥掉、只留全中文用途（用户实拍那条）",
          ns["_oss_cn_intro"](_en_prefix), "全功能桌面备份和恢复套件")
    check("剥英规则覆盖 Adobe Acrobat 同类前缀",
          ns["_oss_cn_intro"]({"desc": "GitHub 开源周榜 · Python · 日均涨星 9｜"
                                       "Adobe Acrobat Pro 2026：用于台式机的安全独立 PDF 文档处理软件",
                               "lang": "Python"}), "用于台式机的安全独立 PDF 文档处理软件")
    check_true("剥英规则不漏 DeepInfer（纯英文段整段丢）",
               "DeepSeek" in ns["_oss_cn_intro"]({"desc": "GitHub 开源周榜 · Python · 日均涨星 9｜"
                                                   "DeepInfer ：DeepSeek模型的高性能、大规模推理框架",
                                                  "lang": "Python"}))
    check_true("中文简介函数按语义段丢掉无汉字前缀（防英文再冒出来）",
               "if not _has_cn(p):" in src and "cn_parts" in src)
    # 【2026-11-12 十三修】用户「气泡里的文字要看全，不能有残缺」：
    # 上一版把顿号「、」当切分点，「在 iPhone、iPad 和 Mac 上…」被切成「在 iPhone」；
    # 括号用 split(...)[0] 砍掉主句后半，只剩「中数学文」。全池 39 条体检后钉死三条。
    _half1 = {"desc": "GitHub 开源周榜 · C# · 日均涨星 655｜"
                      "在 iPhone、iPad 和 Mac 上原生运行您自己的 Switch 游戏："
                      "游戏代码重写为 ARM64，图形直接渲染到 Metal，一个应用程序即可运行整个游戏库。"
                      "自带游戏和密钥。把你自己的 Switch 游戏原生带到 iPhone/iPad/Mac。",
              "lang": "C#"}
    check("顿号并列结构不再被切半句（在 iPhone、iPad 和 Mac 上…）",
          ns["_oss_cn_intro"](_half1),
          "在 iPhone、iPad 和 Mac 上原生运行您自己的 Switch 游戏")
    _half2 = {"desc": "GitHub 开源月榜 · C｜"
                      "在Apple Silicon Mac、iPhone和iPad上玩自己的《王国之泪》和《荒野之息》。"
                      "不是模拟器：游戏的ARM64代码直接运行， NVN在Metal上重建。带上您自己的游戏。",
              "lang": "C"}
    check("英文名词开头的并列句不残缺（Apple Silicon Mac…）",
          ns["_oss_cn_intro"](_half2),
          "在Apple Silicon Mac、iPhone和iPad上玩自己的《王国之泪》和《荒野之息》")
    _half3 = {"desc": "GitHub 开源月榜 · Python｜"
                      "中数学文（、英语简体小学到高中三年级到高中） （繁体）、"
                      "初中物理的知识图谱 ： 1644 个知识点、2403 条先修边、6723 道题对齐课标，附自适应前置诊断。",
              "lang": "Python"}
    _out3 = ns["_oss_cn_intro"](_half3)
    check_true("括号只整段删除、主句后半不再被砍掉（不剩「中数学文」）",
               _out3 != "中数学文" and "初中物理的知识图谱" in _out3 and len(_out3) >= 12)
    check_true("三条残缺样本都不再以悬空虚词/半截标点结尾",
               all(not re.search(r"(?:的|和|与|在|把|被|将|对|为|是|了|：|，|、|；)$",
                                 ns["_oss_cn_intro"](x))
                   for x in (_half1, _half2, _half3)))
    check_true("剥英文产品名前缀只在整段开头做一次（不误删句内 iPad / NVN）",
               "intro = intro[m.end():].strip()" in src
               and "p = p[m.end():].strip()" not in src)
    check_true("括号是整段删除、不是 split(...)[0] 砍掉后文",
               're.sub(r"[（(][^）)]*[）)]", " ", intro)' in src
               and 're.split(r"[（(]", intro, 1)' not in src)
    check_true("切段后拼回完整文本再按句末标点分句（短片段不单独收口）",
               'text = "".join(cn_parts).strip()' in src
               and "sentences = [s.strip() for s in re.split" in src
               and "_cn_count(x) >= 6" in src)
    check("半句残句被判为无可用中文内容（挡在气泡候选池外）",
          ns["_oss_cn_usable"]({"desc": "GitHub 开源周榜｜在 iPhone"}), False)
    # 背景换项目头像：不再用印着英文仓库名的 opengraph 分享卡
    _avatar = ns["_oss_avatar_url"]({"title": "ebtsamalhakmy-sketch/desktop-backup-suite",
                                     "image": "https://opengraph.githubassets.com/1/x/y"})
    check("气泡背景 = 项目 owner 头像", _avatar,
          "https://avatars.githubusercontent.com/ebtsamalhakmy-sketch?s=460")
    check_true("气泡背景不再是 opengraph 分享卡（上面印着英文）",
               "opengraph.githubassets.com" not in _avatar
               and "_oss_avatar_url(it)" in src and "OSS_AVATAR_HOST" in src)
    check_true("抓取时从 GitHub owner.avatar_url 写入 avatar 字段",
               '"avatar": avatar' in src and 'avatar_url' in src)
    # 整卡项目图背景：开源徽章强制 spot
    check_true("开源徽章强制 spot 整卡铺项目图 + 暗蒙层",
               'self.style = "spot"' in src
               and '_b in ("开源周榜", "开源热榜", "开源速榜")' in src)
    # GitHub 标清晰不遮挡：徽章在左上 14,12；正文在 y=83 以下
    check_true("GitHub 官方标在左上角、不与下半部正文重叠",
               "gp.GdipDrawImageRectI(gfx, bmp, int(14 * s), int(12 * s)" in src
               and "83 + int((1.0 - e) * 10)" in src)
    # 开机这一轮也置顶开源 + 播前同事件过滤
    check_true("开机启动播报也置顶开源席",
               "_seat_boot" in src and "([_seat_boot] if _seat_boot else [])" in src)
    check_true("开机启动播报播前过滤同事件稿",
               "_novel_boot, _dupes_boot = _balloon_event_filter(_ordered)" in src)
    # 同一条新闻多家网站不重复：事件窗口 6h→26h、容量 400→1200、大事提醒也过滤
    check_true("事件记忆窗口源码 26 小时 / 容量 1200",
               "_BALLOON_EVENT_TTL = 26 * 3600" in src and "_BALLOON_EVENT_MAX = 1200" in src)
    check_true("大事提醒播前也过滤已播事件的别家稿",
               "fresh, _major_dupes = _balloon_event_filter(fresh)" in src)

    print("---- 8d) 十四修：网页卡片描述行只放完整中文用途说明")
    _card1 = {"desc": "GitHub 开源周榜 · Swift · 日均涨星 2111｜"
                      "适用于 iOS 27.x 的 MobileGestalt 编辑器，具备区域代码调整、备份功能。",
              "lang": "Swift"}
    _ci1 = ns["_oss_card_intro"](_card1)
    check("截图第 1 条：描述行只剩中文用途说明（整句、无截断）",
          _ci1, "适用于 iOS 27.x 的 MobileGestalt 编辑器，具备区域代码调整、备份功能")
    check_true("描述行不再叙述日均涨星 / 榜单名 / 语言等元信息",
               all(w not in _ci1 for w in ("日均涨星", "开源周榜", "Swift", "｜")))
    _card2 = {"desc": "GitHub 开源周榜 · Swift · 日均涨星 732｜"
                      "iOS 系统配置与 MobileGestalt 研究工具箱，可安全修改设备信息。",
              "lang": "Swift"}
    check("截图第 2 条：描述行是完整中文说明",
          ns["_oss_card_intro"](_card2),
          "iOS 系统配置与 MobileGestalt 研究工具箱，可安全修改设备信息")
    _card3 = {"desc": "GitHub 开源周榜 · C · 日均涨星 678", "lang": "C"}
    check("截图第 3 条：没有中文说明就不摆描述行（不拿标签凑数）",
          ns["_oss_card_intro"](_card3), "")
    check("英文原文未翻译的条目也不摆英文描述行",
          ns["_oss_card_intro"]({"desc": "GitHub 开源月榜｜简介原文：A tool for something",
                                 "lang": "Python"}), "")
    # 预算比气泡大：一句话说清楚，不截成气泡那 30 字
    _long = ns["_oss_card_intro"](_half1)
    check_true("卡片简介按更大预算给整句（不是气泡的 30 字截断）",
               len(_long) >= 40 and "游戏代码重写为 ARM64" in _long
               and "def _oss_card_intro(it, limit=120)" in src)
    check_true("卡片简介结尾不悬空虚词 / 不残半句",
               not re.search(r"(?:的|和|与|在|把|被|将|对|为|是|了|：|，|、|；)$", _long))
    _rows = [dict(_card1, title="a/one"), dict(_card3, title="b/two")]
    _out_rows = ns["_oss_with_intro"](_rows)
    check_true("出口逐条补 intro（浅拷贝不改缓存原 dict）",
               all("intro" in x for x in _out_rows)
               and all("intro" not in x for x in _rows)
               and all(a["desc"] == b["desc"] for a, b in zip(_rows, _out_rows)))
    check("没中文说明的条目 intro 为空串但字段仍在（前端可判空）",
          _out_rows[1]["intro"], "")
    check_true("_oss_with_intro 仍被展示出口调用（补 intro 的规则没丢）",
               "_oss_with_intro(rows or [])" in src)
    check_true("/api/oss 三份榜走展示出口", calls_inside(tree, "api_oss", "_oss_display_rows"))
    check_true("首屏 SSR 三份榜走同一展示出口（避免首屏先露旧描述 / 空白卡）",
               src.count('"oss": _oss_display_rows(') >= 1
               and src.count('"oss_week": _oss_display_rows(') >= 1
               and src.count('"oss_speed": _oss_display_rows(') >= 1)
    check_true("desc 内部字段不动（气泡仍按 ｜ 抽中文，口径不受影响）",
               'bits.append("日均涨星 %d" % int(round(velocity)))' in src)
    check_true("前端卡片优先用 intro，新闻卡片仍显示 desc",
               'hasOwnProperty.call(it, "intro")' in idx
               and 'const desc = (hasIntro ? (it.intro || "") : (it.desc || "")).trim();' in idx)
    check_true("前端描述行对开源卡片加 full 类、CSS 放开多行不裁切",
               'desc${hasIntro ? " full" : ""}' in idx
               and ".news .desc.full" in idx and "overflow:visible" in idx)

    print("---- 8e) 十五修：网页三榜只列有中文介绍的项目")
    _bank = [dict(_card1, title="a/one", rank=1),
             dict(_card3, title="b/two", rank=2),      # 无中文说明 → 不列
             dict(_card2, title="c/three", rank=3)]
    _shown = ns["_oss_display_rows"](_bank)
    check("空白描述行的条目不进网页榜", [x["title"] for x in _shown], ["a/one", "c/three"])
    check("过滤后名次连续重排", [x["rank"] for x in _shown], [1, 2])
    check_true("展示榜每条都有非空中文说明",
               all((x.get("intro") or "").strip() for x in _shown))
    check_true("过滤是浅拷贝，不改缓存原 dict",
               "intro" not in _bank[0] and "intro" not in _bank[2])
    _all_blank = [dict(_card3, title="x/one", rank=1), dict(_card3, title="y/two", rank=2)]
    _fallback = ns["_oss_display_rows"](_all_blank)
    check("整池无中文说明时兜底保留原榜（软件栏三档入口不整块消失）", len(_fallback), 2)
    check_true("兜底榜 intro 为空串但字段还在（前端可判空）",
               all("intro" in x and not x["intro"] for x in _fallback))
    check("空榜仍是空榜", ns["_oss_display_rows"]([]), [])
    check_true("气泡链路不经过展示过滤（轮播仍必出开源）",
               all("_oss_display_rows" not in (ast.get_source_segment(src, n) or "")
                   for n in fnodes(tree, "_oss_snapshot") + fnodes(tree, "_oss_pick_boom")))
    check_true("count 字段按过滤后的三份榜计算",
               '"count": len(items)' in src and '"week_count": len(week_items)' in src
               and '"speed_count": len(speed_items)' in src)

    print("---- 8f2) 十六修：速榜不再恒空（基准推进 / 冷启动重算 / 空批保留真值）")
    _rb = "".join(ast.unparse(n) for n in fnodes(tree, "_oss_speed_rebuild"))
    _sd = "".join(ast.unparse(n) for n in fnodes(tree, "_oss_speed_seed"))
    _ld = "".join(ast.unparse(n) for n in fnodes(tree, "_oss_speed_load_disk"))
    _sv = "".join(ast.unparse(n) for n in fnodes(tree, "_oss_speed_save_disk"))
    check("基准最长保留 24 小时（不退化成长期平均数）",
          ns["OSS_SPEED_BASE_MAX_AGE"], 24 * 3600)
    check_true("基准推进带「本路观测到涨星」判定，不再每批无条件覆盖",
               "gained" in _rb and "OSS_SPEED_BASE_MAX_AGE" in _rb
               and "gained or" not in _rb.replace(" ", ""))
    check_true("零净增批次不清空速榜、保留最近一次真值并标 stale",
               "_OSS_SPEED['stale'] = bool(_OSS_SPEED['items'])" in _rb
               and "最近一次真实净增" in _rb
               and "_OSS_SPEED['items'] = []" not in _rb)
    check_true("空批次不推进基准（只在涨星 / 首轮 / 满 24h 时推进）",
               "not pts" in _rb and "not (prev.get('stars') or {})" in _rb)
    check_true("冷启动总是用磁盘基准重算两路，不复用 has_speed=False 的旧行",
               "if not _OSS_SPEED['week_rows']" not in _sd
               and _sd.count("_oss_speed_measure(") == 2)
    check_true("冷启动接收两路缓存时刻，空基准不伪造 now",
               "_oss_speed_seed" in "".join(ast.unparse(n) for n in fnodes(tree, "_warmup"))
               and "month_ts" in _sd and "week_ts" in _sd
               and "float(cts or 0) or now" in _sd)
    check_true("读盘恢复 stale / real_at，并把旧「正在积累」文案按真值纠正",
               "real_at" in _ld and "stale" in _ld
               and "最近一次真实净增" in _ld and "正在积累" in _ld)
    check_true("写盘保存 real_at / stale（重启后不丢真值时间）",
               "real_at" in _sv and "stale" in _sv)
    check_true("/api/oss 回带 speed_stale / speed_real_at",
               '"speed_stale"' in src and '"speed_real_at"' in src
               and calls_inside(tree, "api_oss", "_oss_speed_meta"))
    check_true("首屏 SSR 也回带 stale / real_at（首屏不跳文案）",
               '"oss_speed_stale"' in src and '"oss_speed_real_at"' in src)
    check_true("前端速榜标注「本批暂无新增，显示最近一次真实净增」及时间",
               "state.ossSpeedStale" in idx and "state.ossSpeedRealAt" in idx
               and "本批暂无新增，显示最近一次真实净增" in idx
               and "meta.speed_stale" in idx and "meta.speed_real_at" in idx)
    check_true("速榜仍不掺平均数、不额外发请求",
               "fallback" not in "".join(ast.unparse(n) for n in fnodes(tree, "_oss_speed_merge"))
               and not calls_inside(tree, "_oss_speed_rebuild", "_fetch"))
    check_true("meta 出口只读快照，不碰网络 / 缓存文件",
               "def _oss_speed_meta" in src
               and "open(" not in "".join(ast.unparse(n) for n in fnodes(tree, "_oss_speed_meta")))

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
