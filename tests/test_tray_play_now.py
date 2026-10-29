# -*- coding: utf-8 -*-
# 作者: 海风（kele551）
# 用途: 托盘「立即轮播一轮」+「轮播间隔」设置的单测（2026-10-28 用户要求）。
#
# 用户选的口径（三条都是他点头的，别再自己发挥）：
#   · 入口 = 托盘右键菜单加一项「立即轮播一轮」，外加**双击托盘图标**同一个动作；
#   · 「轮播时间」指的是**多久自动弹一组**（原来写死 10 分钟），不是每条新闻停多久；
#   · 调法 = 程序里点一下就行（菜单里四档，存本机 config.local.json 的 notify 段）。
#
# 盯的三件事：
#   1) 坏值不能把气泡搞哑：手改配置文件改成 0 / 负数 / 字符串 / 7 这种中间值，
#      要回落默认或就近归档，不能"1 秒炸一次屏"或者永远不弹。
#   2) 手动那一轮放开的是**打扰闸**（节流、夜间免打扰、没新增、播过），
#      不放开**内容口径**（只播中文、按栏目配额挑）—— 后者是用户定过的死规矩。
#   3) 这两个动作都发生在 Win32 消息线程上，一律不许在那条线程里干重活/同步开网页
#      （2026-10-28 刚为这个修过"鼠标一直转"）。
#
# 为什么不 import server.py：八千多行，模块级要建 FastAPI 应用、读配置、起缓存目录。
# 这里用 ast 只把要验的函数摘出来 exec，配假依赖 —— 不建窗口、不碰网络、不看时钟。
import ast
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server.py")

WANT_FUNCS = ("_toast_min_sec", "_toast_min_set", "_tray_toast_label", "_balloon_play_now")
WANT_CONSTS = ("TOAST_MIN_CHOICES", "TOAST_MIN_DEFAULT", "_TRAY_CMD_OPEN", "_TRAY_CMD_PLAY",
               "_TRAY_CMD_QUIT", "_TRAY_CMD_TOAST_BASE")

FAILS = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("[ok]" if ok else "[!!]", name, ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name + (" — " + detail if detail else ""))


def load():
    src = open(SERVER, encoding="utf-8").read()
    return src, ast.parse(src)


def classes(tree, name):
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == name:
            return {m.name: m for m in n.body if isinstance(m, ast.FunctionDef)}
    return {}


def top(tree, name):
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def consts_of(tree, names):
    """把模块级常量按名字摘出来（值用 literal_eval，算不出来的就 unparse 原文）。"""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names:
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except Exception:
                out[node.targets[0].id] = ast.unparse(node.value)
    return out


# ────────────── 4b) 真跑 _notify_news：把它的依赖闭包摘出来 ──────────────
# 这个测试要的是"手动轮播不重播"这条**行为**，只验源码里有没有那几行字是糊弄不过去的
# （第一版就是这么写的，"force 不放开已播闸"的写法一改，源码闸还绿着，行为已经错了）。
# 所以这里按名字做一次依赖闭包：摘 _notify_news 以及它真正调到的那些纯函数（去重/配额/
# 已播名单/事件记忆），配几个记录桩真跑几轮。不建窗口、不联网、不看时钟。
CLOSURE_SEEDS = ("_notify_news", "_has_cjk", "_pick_balloon_items", "_dedup_same_event",
                 "_annotate_cross_hot", "_balloon_event_filter", "_balloon_event_remember",
                 "_balloon_event_sig", "_balloon_same_event", "_balloon_seen",
                 "_balloon_mark_shown", "_balloon_trace", "_toast_min_sec",
                 "_balloon_entry", "_balloon_head", "_is_intl_item", "_balloon_cls_key",
                 "_balloon_cls_tally")
# 这几支是"故意给假货"的叶子：配置文件、夜间时段、弹窗本体
#（_balloon_play_hint 走真的 —— 它只是把回执转给 _show_toast，闸的就是那层转发）
# _interest_weights / _notify_hold_reason 也必须给假货：
#   · 前者会真去读 %LOCALAPPDATA%\hotnews\interest.json —— 换台机器、或者用户刚好点过
#     几条科技，配额就变了，下面"12 条挑 10 条"的期望会随风飘（单测不许看真实文件）；
#   · 后者会去问 Windows 当前是不是全屏/勿扰，同样不是本测试要验的东西
#     （而且这几轮都是 force=True，那道闸压根不走）。
STUBBED = ("_notify_cfg", "_show_toast", "_show_toast_list", "_in_quiet_hours",
           "_interest_weights", "_notify_hold_reason")


def _force_assigns(fn, targets):
    """函数里有没有 `if force:` 直接改候选 —— 旧写法就是 force 把播过的旧的凑进来。

    只认**正向** `if force:`；`if not force: return` 那种"只有自动才走的闸"不算。
    """
    for n in ast.walk(fn):
        if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "force":
            for st in ast.walk(n):
                if isinstance(st, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id in targets for t in st.targets):
                    return True
    return False


def _top_maps(tree):
    """模块顶层的函数表 / 赋值表。同名后定义的覆盖前面的 —— 和 Python 运行时一致。"""
    fns, asg = {}, {}
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and not n.decorator_list:
            fns[n.name] = n
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    asg[t.id] = n
    return fns, asg


def _closure(tree, seeds, stubbed):
    fns, asg = _top_maps(tree)
    picked, stack = {}, list(seeds)
    while stack:
        name = stack.pop()
        if name in picked or name in stubbed:
            continue
        node = fns.get(name) or asg.get(name)
        if node is None:
            continue                     # 内建/标准库/我们注入的桩，跳过
        picked[name] = node
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) \
                    and (sub.id in fns or sub.id in asg):
                stack.append(sub.id)
    return picked


def run_notify_news():
    """真跑几轮 _notify_news(force=True)，把每轮播了什么记下来。"""
    src, tree = load()
    picked = _closure(tree, CLOSURE_SEEDS, STUBBED)
    shown, hints, reps = [], [], []
    import datetime as _dt
    # CST = timezone(timedelta(hours=8)) 是模块级赋值，闭包会把它一起摘出来 exec，
    # 所以 timezone / timedelta 这两个裸名字必须在场，不然摘到 CST 那一行就 NameError。
    ns = {"time": time, "threading": threading, "print": lambda *a, **k: None,
          "re": __import__("re"), "json": __import__("json"), "os": os, "sys": sys,
          "datetime": _dt.datetime, "timezone": _dt.timezone, "timedelta": _dt.timedelta,
          "CST": _dt.timezone(_dt.timedelta(hours=8)),
          "PRELOAD": {"cn": [], "hot": [], "intl": []},
          "NEWS_BASELINE": set(), "NEWS_LAST_TOAST": 0.0,
          "NEWS_LOCK": threading.Lock(),
          "_notify_cfg": lambda: {},
          "_in_quiet_hours": lambda ts=None: False,
          # 兴趣权重给空表 = 冷启动配额（和没点开过任何新闻的新用户一样），
          # 这样每轮挑几条只取决于喂进去的 batch，不取决于这台机器上的 interest.json。
          "_interest_weights": lambda: {},
          "_notify_hold_reason": lambda: "",
          "_show_toast": lambda title, body: hints.append((title, body)),
          # replace 是 2026-10-28 加的那道「人开口就掐掉正在播的那一组」的开关，桩必须收下它
          # 并原样记一笔 —— 不记就等于测不出「手动那一轮到底有没有喊掐」（漏了就会像以前那样
          # 排在正在播的 56 秒那一组后面干等，用户口径：「切换样式模式，播报暂停了一分钟」）。
          "_show_toast_list": lambda entries, replace=False: (
              shown.append(list(entries)), reps.append(bool(replace)))}
    # 赋值先 exec（默认参数/闭包可能用到常量），再 exec 函数
    order = sorted(picked.items(), key=lambda kv: (0 if isinstance(kv[1], ast.Assign) else 1,
                                                   kv[1].lineno))
    bad = []
    for name, node in order:
        try:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
        except Exception as ex:
            bad.append("%s: %s: %s" % (name, type(ex).__name__, ex))
    if bad:
        print("[!!] 依赖闭包 exec 失败：" + "; ".join(bad[:3]))
        FAILS.extend(bad)
        return None

    # 【2026-10-29】「播过名单」现在会落盘（常看常新 ③：重启不该把"播过"这件事忘掉），
    # 于是闭包把 `_balloon_shown_save` 和 `_BALLOON_SHOWN_PATH` 一起摘了进来 —— 不管这一句
    # 就会把下面那些 n1..n10 假条目写进用户机器上那份**真名单**（今天真写过：
    # %LOCALAPPDATA%\hotnews\balloon_shown.json 里躺着 10 条 n 开头的键）。
    # 单测不许碰本机数据目录：摘完立刻把路径改到临时目录，跑完删掉，并验一笔"确实写到这儿了"。
    shown_path = os.path.join(tempfile.gettempdir(), "hn-test-tray-shown-%d.json" % os.getpid())
    if "_BALLOON_SHOWN_PATH" not in ns:
        FAILS.append("闭包里没摘到 _BALLOON_SHOWN_PATH（落盘改名这步失效了，会写进本机数据目录）")
    ns["_BALLOON_SHOWN_PATH"] = shown_path
    for p in (shown_path, shown_path + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass

    def mk(i, cls, region="cn", extra=None):
        it = {"id": "n%d" % i, "title": "第%d条：%s新闻的标题各不相同" % (i, cls),
              "cls": cls, "region": region, "label": "站%d" % i, "source": "站%d" % i,
              "link": "https://example.com/n%d" % i, "published": 0}
        it.update(extra or {})
        return it

    batch = (                                    # 12 条：配额刚好占 10，剩 2 条给第二轮
        [mk(1, "头条"), mk(2, "头条")]
        + [mk(3 + i, "热榜", extra={"rank": i + 1}) for i in range(3)]
        + [mk(6 + i, "要闻", region="intl") for i in range(2)]
        + [mk(8, "科技"), mk(9, "财经"), mk(10, "娱乐")]
        + [mk(11, "软件"), mk(12, "软件")])
    out = {"r1_toast_at": 0.0, "r2_toast_at": 0.0}

    def round_(items, tag):
        del shown[:]
        del hints[:]
        del reps[:]
        ns["_notify_news"](items, force=True)
        entries = shown[-1] if shown else []
        out[tag + "_count"] = len(entries)
        out[tag + "_ids"] = [e[3] for e in entries]
        out[tag + "_hint"] = hints[-1] if hints else ("", "")
        out[tag + "_toast_at"] = ns["NEWS_LAST_TOAST"]
        out[tag + "_replace"] = list(reps)
        return entries

    round_(batch, "r1")
    round_(batch, "r2")
    round_(batch, "r3")
    # 第四轮：换了 id、跟第一轮某条讲的是同一件事（标题一样）→ 事件记忆要挡住
    round_([mk(21, "头条", extra={"title": batch[0]["title"]})], "r4")
    # 第五轮：只有英文标题 → 内容口径（只播中文）要挡住
    en = mk(31, "科技", extra={"title": "Tesla announces a new factory in Berlin"})
    en["title_en"] = en["title"]
    round_([en], "en")
    out["en_ids"] = [e[1] for e in shown[-1]] if shown else []

    # 第六、七轮（2026-10-29 用户：「自动这个样式，还在播报一天前的旧新闻，一定要定规则：
    # 常看常新」）：规则 ① 是"发布时间 > 24 小时一律不进播报候选"。
    # 上面那些假条目 published 全是 0（热榜那种拿不到时间），走的是"按新放行"那条路，
    # 所以这两轮才是真验到这条规则的 —— 一轮全旧（该一条不播），一轮新旧混着（旧的必须缺席）。
    old_batch = [mk(40 + i, cls, extra={"published": time.time() - 30 * 3600})
                 for i, cls in enumerate(("头条", "头条", "热榜", "科技", "财经", "娱乐"))]
    round_(old_batch, "old")
    out["old_ids"] = [e[3] for e in (shown[-1] if shown else [])]
    # 新旧混着的一轮：6 条 1 小时内的（头条2 + 热榜3 + 科技1，刚好占满这几档配额）
    # + 6 条 30 小时前的。旧的要是没被规则 ① 挡住，这一轮会播到 8 条（财经/娱乐各 1 席）。
    mix = ([mk(50 + i, cls, extra={"published": time.time() - 1 * 3600})
            for i, cls in enumerate(("头条", "头条", "热榜", "热榜", "热榜", "科技"))]
           + [mk(60 + i, cls, extra={"published": time.time() - 30 * 3600})
              for i, cls in enumerate(("财经", "财经", "娱乐", "娱乐", "软件", "软件"))])
    round_(mix, "mix")
    out["mix_ids"] = [e[3] for e in (shown[-1] if shown else [])]
    out["mix_old"] = [i for i in out["mix_ids"] if i in {x["id"] for x in mix[6:]}]
    # 落盘这一路也要真走通一遍（而且走的是临时目录）：第一轮播出去 10 条就该有一份，
    # 后面几轮落在 60 秒节流窗口里，不该再反复磨盘。
    try:
        _disk = len((__import__("json").load(open(shown_path, encoding="utf-8")) or {})
                    .get("shown") or {})
    except Exception as ex:
        _disk = -1
    check("几轮之后名单确实落了盘，且落在临时目录（不碰本机 %LOCALAPPDATA%\\hotnews）",
          os.path.exists(shown_path), shown_path)
    check("落的那份就是真播出去的 10 条（不是空表，也不是把整份历史清掉重写）",
          _disk >= 10, "%d 条" % _disk)
    for p in (shown_path, shown_path + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass
    return out


def build_namespace():
    ns = {"threading": threading, "time": time, "print": print}
    tree = load()[1]
    for name in WANT_FUNCS:
        fn = top(tree, name)
        if fn is None:
            FAILS.append("server.py 里找不到函数 " + name)
            return None
        exec(compile(ast.Module(body=[fn], type_ignores=[]), SERVER, "exec"), ns)
    ns.update(consts_of(tree, WANT_CONSTS))
    for k in WANT_CONSTS:
        if k not in ns:
            FAILS.append("server.py 里找不到常量 " + k)
            return None
    return ns


def main():
    src, tree = load()
    ns = build_namespace()
    if ns is None:
        return finish()

    print("---- 0) ctypes.wintypes 里没有的名字一律不许用 ----")
    # 真机踩过的坑（2026-10-28）：AppendMenuW 的原型写成 wintypes.UINT_PTR —— ctypes 压根
    # 没有这个名字，_Tray.__init__ 当场 AttributeError，托盘整个降级，右键菜单和双击全没。
    # 单测只用 ast 摘函数、从不跑 __init__，所以这条必须在纯静态这一层就闸住。
    import ctypes as _ct
    import ctypes.wintypes as _wt
    _have = set(dir(_wt))
    _used = {}
    for _n in ast.walk(tree):
        if isinstance(_n, ast.Attribute) and isinstance(_n.value, ast.Name) \
                and _n.value.id == "wintypes":
            _used.setdefault(_n.attr, _n.lineno)
    _bad = {k: v for k, v in _used.items() if k not in _have}
    check("server.py 引用的 wintypes 名字全都真实存在（用到 %d 个）" % len(_used),
          _bad == {}, "不存在的是: %r" % (_bad,))
    check("指针宽度用 ctypes.c_size_t 自己搭，不去借 wintypes.UINT_PTR",
          "_UINT_PTR = ctypes.c_size_t" in src and "wintypes.UINT_PTR" not in src)

    print("---- 1) 间隔取值：坏值不许把气泡搞哑 ----")
    # 假配置：想让它返回什么就返回什么，只看 _toast_min_sec 自己的归档。
    box = {"cfg": {}}
    ns["_notify_cfg"] = lambda: dict(box["cfg"])
    tms = ns["_toast_min_sec"]
    for give, want, why in (
            (5, 300, "最小档"),
            (10, 600, "默认档"),
            (30, 1800, "半小时"),
            (60, 3600, "1 小时"),
            (7, 300, "中间值就近归到 5（|7-5|=2 < |7-10|=3）"),
            (45, 30 * 60, "45 就近归到 30"),
            (0, 600, "0 档等于静音 → 回落默认"),
            (-3, 600, "负数 → 回落默认"),
            ("abc", 600, "字符串 → 回落默认"),
            (None, 600, "空值 → 回落默认"),
    ):
        box["cfg"] = {"toast_min": give}
        check("toast_min=%r → %d 秒（%s）" % (give, want, why), tms() == want, "实得 %s" % tms())
    box["cfg"] = {}
    check("配置里根本没这一项 → 默认 10 分钟", tms() == 600, "实得 %s" % tms())
    box["cfg"] = {"toast_min": 9999}
    check("手改出一个 9999 → 就近归到最大档，不许变成几天不弹", tms() == 3600, "实得 %s" % tms())

    print("---- 2) 写回去：只认这四档，且只 patch toast_min 一个键 ----")
    saved = []
    ns["_save_notify_cfg"] = lambda patch: saved.append(dict(patch)) or True
    tset = ns["_toast_min_set"]
    check("_toast_min_set(30) 写的是 {'toast_min': 30}",
          tset(30) == 30 and saved[-1] == {"toast_min": 30}, "saved=%r" % saved[-1])
    check("写 7 会被归成 5（菜单里没有 7 分钟这一档）",
          tset(7) == 5 and saved[-1] == {"toast_min": 5}, "got=%s saved=%r" % (tset(7), saved[-1]))
    check("写坏了也不许把别的键一起 patch 掉（夜间免打扰等不能跟着丢）",
          all(list(p.keys()) == ["toast_min"] for p in saved), "saved=%r" % saved)
    _nd = src[src.index("NOTIFY_DEFAULTS = {"):src.index("\n}", src.index("NOTIFY_DEFAULTS = {"))]
    check("默认表里挂了 toast_min 这一项 —— 没挂号的键 _save_notify_cfg 落不了盘",
          '"toast_min": 10' in _nd, _nd.replace("\n", " ")[:120])

    print("---- 3) 立即轮播：空池 / 有池 两条路 ----")
    calls = {"toast": [], "kick": [], "notify": []}
    ns["_show_toast"] = lambda *a, **k: calls["toast"].append(a)
    ns["_kick_collect"] = lambda r: calls["kick"].append(r)

    def fake_notify(items, force=False):
        calls["notify"].append((len(items), force))
    ns["_notify_news"] = fake_notify
    pool = {"cn": [], "hot": [], "intl": []}
    ns["PRELOAD"] = pool
    play = ns["_balloon_play_now"]

    play("单测")
    check("池子空着时给一句明白话（不许双击完什么都不发生）",
          len(calls["toast"]) == 1, "toast=%r" % (calls["toast"],))
    check("池子空着还顺手把三区抓取踢起来", calls["kick"] == ["hot", "cn", "intl"],
          "kick=%r" % calls["kick"])
    check("池子空着不该去调 _notify_news", calls["notify"] == [], "notify=%r" % calls["notify"])

    pool["cn"] = [{"id": "a%d" % i, "title": "标题%d" % i, "cls": "科技"} for i in range(30)]
    pool["intl"] = [{"id": "b%d" % i, "title": "国际%d" % i, "cls": "要闻"} for i in range(20)]
    calls["notify"] = []
    where = []

    def fake_notify2(items, force=False):
        where.append(threading.current_thread().name)
        calls["notify"].append((len(items), force))
    ns["_notify_news"] = fake_notify2
    here = threading.current_thread().name
    play("单测")
    t = time.time()
    while not calls["notify"] and time.time() - t < 3:
        time.sleep(0.01)
    check("有池子 → 走 _notify_news(force=True)",
          calls["notify"] and calls["notify"][-1][1] is True, "notify=%r" % calls["notify"])
    check("候选 = cn+hot+intl 合起来的池子（不是只拿一个分区）",
          calls["notify"] and calls["notify"][-1][0] == 50, "拿了几条=%r" % (calls["notify"],))
    check("挑池子这活跑在别的线程上（托盘那条消息线程不能被占 —— 占了就是鼠标转圈）",
          bool(where) and where[0] != here, "跑在 %r，调用线程是 %r" % (where, here))

    print("---- 4) _notify_news 的 force：放开打扰闸，不放开内容口径 ----")
    fn = top(tree, "_notify_news")
    seg = ast.get_source_segment(src, fn) or ""
    # 注释里出现 force（"手动那一轮同样不放开"）是正常的，别拿它当"口径松了"的证据；
    # 断言一律对着去注释后的代码，注释删光也不会变绿/变红。
    code_src = ast.unparse(fn)
    check("_notify_news 收了 force 参数",
          any(a.arg == "force" for a in fn.args.args), ast.unparse(fn.args))
    for needle, why in (
            ("if not force and now - NEWS_LAST_TOAST < _toast_min_sec()", "节流闸"),
            ("if not force and _in_quiet_hours(now)", "夜间免打扰闸"),
    ):
        check("手动那一轮放开%s" % why, needle in seg, needle[:40])
    check("自动那一套仍然走 _toast_min_sec（间隔可设，不再有写死的 10*60）",
          "NEWS_TOAST_MIN" not in seg and "_toast_min_sec()" in seg)
    check("手动放开「没有新增」这道闸（added 回落整池）",
          "added = new_ids" in code_src)
    check("「播过就不再播」这道闸手动也**不许**放开（旧写法是 force 拿旧的凑数）",
          "_seen = _balloon_seen()" in code_src and "not in _seen" in code_src
          and not _force_assigns(fn, ("_unseen", "_novel", "fresh")),
          "有 force 分支直接改候选 = 手动那一轮会回锅旧条目")
    check("「同一件事别的网站也在报」那道闸手动也照样过",
          "_balloon_event_filter(fresh)" in code_src)
    check("每一处播不出都有回执（手动那一轮点了必须有反应）",
          code_src.count("_balloon_play_hint(") >= 4 and "if force:" in code_src)
    check("手动这一轮没播出东西要把 NEWS_LAST_TOAST 退回去（不能顺手压掉后面 10 分钟的自动播报）",
          "_prev_toast = NEWS_LAST_TOAST" in code_src
          and code_src.count("NEWS_LAST_TOAST = _prev_toast") >= 4)
    check("内容口径没被 force 松动：只播中文这条对所有路径生效",
          "fresh = [x for x in fresh if _has_cjk(x.get('title'))]" in code_src)
    check("内容口径没被 force 松动：栏目配额挑选照旧（不因为手动就一个栏目占满）",
          "picked = _pick_balloon_items(fresh)" in code_src)
    check("手动那一轮不许报「本次共新增 N 条」（N 会是整个池子，看着像一分钟冒出来的）",
          "if not force and len(added) > len(entries)" in code_src)

    print("---- 4b) 真跑 _notify_news：手动轮播不许重播（用户原话「不能重复，反复」）----")
    played = run_notify_news()
    if played is None:
        return finish()
    check("第一轮 12 条里挑 10 条播出去", played["r1_count"] == 10,
          "实播 %s 条" % played["r1_count"])
    check("第二轮同样这 12 条：只补剩下的 2 条没播过的，绝不回锅那 10 条",
          played["r2_count"] == 2 and not set(played["r2_ids"]) & set(played["r1_ids"]),
          "第二轮=%r" % played["r2_ids"])
    check("第三轮：全部播过 → 一条都不播（宁可不弹也不重播）",
          played["r3_count"] == 0, "第三轮还播了 %s 条" % played["r3_count"])
    check("第三轮给了「没有新的可播」回执，用户不至于以为双击没反应",
          played["r3_hint"][0] == "没有新的可播", "回执=%r" % (played["r3_hint"],))
    check("播不出来的那一轮没把节流时刻顶到当下（自动播报不受牵连）",
          played["r3_toast_at"] == played["r2_toast_at"],
          "r2=%s r3=%s" % (played["r2_toast_at"], played["r3_toast_at"]))
    check("第二轮真播出东西时，节流时刻是往前走的（不是原样不动）",
          played["r2_toast_at"] >= played["r1_toast_at"], "")
    check("同事件的别家稿也挡得住：换了 id、事情播过 → 手动照样不播",
          played["r4_count"] == 0 and played["r4_hint"][0] == "没有新的可播",
          "r4 播了 %s 条 回执=%r" % (played["r4_count"], played["r4_hint"]))
    check("英文标题仍旧一律不播（force 不许松动内容口径）",
          played["en_count"] == 0, "英文那条播出去了=%r" % (played["en_ids"],))
    # 【2026-10-29 常看常新 ①】上面那些假条目 published 全是 0（热榜那种拿不到发布时间，
    # 按"新"放行），所以只有这两轮真碰到这条规则：一轮全旧、一轮新旧混着。
    # 这是**行为**闸门，不是源码闸门 —— 摘真函数跑，把 _notify_news 里那条裁刀整个走一遍。
    check("全是 30 小时前的那一批：一条都不播（用户：「还在播报一天前的旧新闻」）",
          played["old_count"] == 0 and played["old_ids"] == [],
          "播了 %s 条 %r" % (played["old_count"], played["old_ids"]))
    check("全旧那一轮也明说「没有新的可播」（不拿旧的凑数）",
          played["old_hint"][0] == "没有新的可播", "回执=%r" % (played["old_hint"],))
    check("新旧混着的一轮：播出去的一条旧的都没有",
          played["mix_old"] == [], "混进来了 %r" % (played["mix_old"],))
    check("新旧混着的一轮：6 条 1 小时内的照常播（旧闻不许把额度占干）",
          played["mix_count"] == 6, "实播 %s 条" % played["mix_count"])
    check("旧闻不进出播过名单（不然它熬到 24 小时内也永远播不到）",
          played["old_count"] == 0 and played["mix_count"] == 6, "")
    # 「切换样式模式，播报暂停了一分钟」—— 根因是手动那一组排在正在播的 56 秒那一组后面干等。
    # 修法是把 replace=True 一路传到 _show_toast_list，让它给气泡留一个「掐」的标记。
    # 静态那条闸门在 test_balloon_styles.py 第 6 组，这里补的是**真跑一遍确实传到了**：
    # 光看代码有 replace 这个参数不够，得看 force 那一轮真的把它带下去。
    check("手动每一轮真播出去时都喊了「掐掉正在播的那一组」（replace=True）",
          played["r1_replace"] == [True] and played["r2_replace"] == [True],
          "r1=%r r2=%r" % (played["r1_replace"], played["r2_replace"]))
    check("一条没播出去的那两轮不喊掐（没东西可换，别把人家正在播的掐了）",
          played["r3_replace"] == [] and played["r4_replace"] == [],
          "r3=%r r4=%r" % (played["r3_replace"], played["r4_replace"]))

    print("---- 5) 托盘接线：菜单项、命令号、双击 ----")
    tray = classes(tree, "_Tray")
    for m in ("_build_menu", "_run_cmd", "_popup_menu", "_wndproc"):
        check("_Tray 有 %s" % m, m in tray)
    bm = ast.get_source_segment(src, tray["_build_menu"]) or ""
    rc = ast.get_source_segment(src, tray["_run_cmd"]) or ""
    wp = ast.get_source_segment(src, tray["_wndproc"]) or ""
    all_ids = [ns["_TRAY_CMD_OPEN"], ns["_TRAY_CMD_PLAY"], ns["_TRAY_CMD_QUIT"]] + \
              [ns["_TRAY_CMD_TOAST_BASE"] + i for i in range(len(ns["TOAST_MIN_CHOICES"]))]
    check("菜单文字：打开热点新闻 / 立即轮播一轮 / 轮播间隔 / 退出程序",
          all(s in bm for s in ("打开热点新闻", "立即轮播一轮", "轮播间隔", "退出程序")),
          "缺一项就没接上")
    check("命令号不撞车（三个动作 + 间隔四档，七个号各不相同）",
          len(set(all_ids)) == len(all_ids) == 7, "ids=%r" % all_ids)
    check("间隔四档做成子菜单并带上当前档（MF_POPUP + 单选 + CHECKED）",
          "0x00000010" in bm and "0x00000200" in bm and "CheckMenuItem" in bm)
    # CheckMenuItem(hMenu, uID, uCheckState, uFlags)：真机第一次弹菜单就发现这两个参数被
    # 记反过（ctypes 不报错，勾就是不亮），所以这里按**调用现场**闸死顺序。
    check("CheckMenuItem 的参数顺序是「uCheckState=MF_CHECKED 在前、uFlags=MF_BYCOMMAND 在后」",
          "CheckMenuItem(hsub, cid, 0x00000008, 0x00000000)" in bm,
          "记反 = 菜单没有勾")
    check("双击托盘图标 = 立即轮播（认 WM_LBUTTONDBLCLK 0x0203）",
          "_low == 0x0203" in wp and "_balloon_play_now" in wp)
    check("单击 0x0202 仍旧什么都不做（不然点一下图标就跳出一屏新闻，成了误触）",
          "== 0x0202" not in wp)
    check("改了间隔要给一条回执气泡（这设置没有别的界面能看见）",
          "_toast_min_set" in rc and "_show_toast" in rc)
    check("退出程序挪到 9 号，让 2 号给「立即轮播」",
          ns["_TRAY_CMD_QUIT"] == 9 and ns["_TRAY_CMD_PLAY"] == 2)
    check("菜单句柄相关的几个 API 都声明了原型（句柄被截成 32 位 = 空白菜单）",
          "AppendMenuW.argtypes" in src and "CreatePopupMenu.argtypes" in src
          and "CreatePopupMenu.restype" in src and "_UINT_PTR = ctypes.c_size_t" in src)
    _ini = [m for m in classes(tree, "_Tray").values() if m.name == "__init__"]
    _flat = " ".join((ast.unparse(_ini[0]) if _ini else "").split())
    check("CheckMenuItem 声明了 4 个参数（Win32 就是 4 个：句柄/ID/检查状态/标志）",
          "CheckMenuItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT, wintypes.UINT]"
          in _flat, "声明=%s" % (_flat.split("CheckMenuItem.argtypes = ")[1][:60]
                                 if "CheckMenuItem.argtypes = " in _flat else "没有"))
    check("原型声明用 ctypes.c_size_t 顶替 UINT_PTR，且 wintypes.UINT_PTR 一次都没出现",
          "wintypes.UINT_PTR" not in src)
    tip = [ln for ln in src.splitlines() if "nid.szTip" in ln]
    check("悬停提示写了「双击=立即轮播」（用户不会去翻菜单）",
          bool(tip) and "双击" in tip[0], tip[0].strip() if tip else "")
    check("_popup_menu 只做弹菜单，动作收在 _run_cmd（能单测）",
          "self._run_cmd(cmd)" in ast.get_source_segment(src, tray["_popup_menu"]) or "")

    print("---- 6) 对外文案红线词 ----")
    zone = src[src.index("def _toast_min_sec"):src.index("def _has_cjk")]
    hit = [w for w in ("绕过", "翻墙", "梯子", "科学上网", "破解", "上传", "上报") if w in zone]
    check("新增这一节的代码与注释里没有红线词", hit == [], "hit=%r" % hit)

    print("")
    return finish()


def finish():
    if FAILS:
        print("失败 %d 条:" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
