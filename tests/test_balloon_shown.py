# -*- coding: utf-8 -*-
# 作者: 海风（kele551）
# 用途: 气泡「常看常新」的判据单测 —— 播过名单（含跨重启）+ 时效闸门（2026-10-29）。
#
# 两件事分开盯：
#
# A. 「播过名单」（2026-10-25 待办 #65 的那次真 bug）：`_BALLOON_SHOWN` 原来是 set，
#    "本轮新增的全播过"时**整份 clear()**，于是几千条历史一起被忘掉，隔几天又冒回
#    新增列表的老新闻立刻能再弹一次。现在改成按条过期 + 超上限从最久没播那头删 +
#    写盘跨重启（用户 2026-10-28：「轮播轮播，就是不能重复，反复」）。
#
# B. 「常看常新」（2026-10-29 用户点名要一条规则：「自动这个样式，还在播报一天前的
#    旧新闻，一定要定规则」）四条：① 发布时间 > 24h 一律不进播报候选；② 6h 内够一轮
#    就只从 6h 里挑，凑不满才放宽到 ①；③ 播过名单记 26h 且跨重启保留 —— 必须比 ① 长，
#    否则早上播过的一条到夜里名单先把它忘了、它却还在 24h 窗口里 → 同一件事播两遍；
#    ④ 挑不出新鲜的正文明说「没有新的可播」。
#    实测依据（2026-10-29 本机池子 200 条）：>24h 的 46 条（23%）、最旧 45.7 小时，
#    旧代码一条都不挡。
#
# 为什么不 import server.py：那是八千行、模块级要建 FastAPI 应用、读配置、起缓存目录。
# 这里用 ast 只把 `_balloon_seen` / `_balloon_mark_shown` / `_balloon_trace` /
# `_balloon_age_h` / `_balloon_fresh_cut` / `_balloon_shown_load` / `_balloon_shown_save`
# 和它们的常量摘出来单独 exec。全程不碰网络；落盘路径在摘完之后立刻改写到系统临时目录
# （测试绝不往 %LOCALAPPDATA%\hotnews 里写东西），跑完删掉。
import ast
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server.py")

WANT_FUNCS = ("_balloon_seen", "_balloon_mark_shown", "_balloon_trace",
              "_balloon_age_h", "_balloon_fresh_cut",
              "_balloon_shown_load", "_balloon_shown_save", "_balloon_shown_flush")
WANT_CONSTS = ("_BALLOON_SHOWN", "_BALLOON_SHOWN_TTL", "_BALLOON_SHOWN_MAX",
               "_BALLOON_SHOWN_LOCK", "_BALLOON_SHOWN_IO_LOCK", "_BALLOON_SHOWN_PATH",
               "_BALLOON_SHOWN_SAVE_GAP",
               "_BALLOON_SHOWN_SAVED_AT", "_BALLOON_TRACE", "_BALLOON_TRACE_MAX",
               "BALLOON_FRESH_HOURS", "BALLOON_FRESH_PREFER_HOURS", "BALLOON_PLAY_COUNT")

FAILS = []


def load_fragment():
    tree = ast.parse(open(SERVER, encoding="utf-8").read())
    ns = {"time": time, "json": json, "os": os, "threading": threading,
          "tempfile": tempfile, "datetime": datetime,
          "timedelta": timedelta, "timezone": timezone,
          "CST": timezone(timedelta(hours=8))}
    for node in tree.body:
        hit = None
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS \
                and not node.decorator_list:
            hit = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in WANT_CONSTS:
            hit = node.targets[0].id
        if hit:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
            print("  摘到 %s" % hit)
    miss = [n for n in (WANT_FUNCS + WANT_CONSTS) if n not in ns]
    if miss:
        FAILS.append("摘取失败，缺: " + ", ".join(miss))
    return ns


def check(name, ok, detail=""):
    print("%s %s%s" % ("[ok]" if ok else "[!!]", name, ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name + (" — " + detail if detail else ""))


def func_seg(src, name):
    """取某个顶层函数的完整源码（只要静态闸，不 exec 它 —— 那两个函数会起窗口、动网络）。"""
    i = src.find("\ndef " + name + "(")
    if i < 0:
        return ""
    j = src.find("\ndef ", i + 1)
    k = src.find("\nclass ", i + 1)
    ends = [x for x in (j, k) if x > 0]
    return src[i:min(ends) if ends else len(src)]


def main():
    print("摘取 server.py 里的播报判据：")
    ns = load_fragment()
    server_src = open(SERVER, encoding="utf-8").read()
    seen, mark, trace = ns["_balloon_seen"], ns["_balloon_mark_shown"], ns["_balloon_trace"]
    shown, ttl, cap = ns["_BALLOON_SHOWN"], ns["_BALLOON_SHOWN_TTL"], ns["_BALLOON_SHOWN_MAX"]
    now = time.time()
    # 落盘路径改写到临时目录：单测不许碰用户机器上那份真名单，也不许依赖它的现状。
    TEST_DIR = os.path.join(tempfile.gettempdir(), "hn-test-balloon-%d" % os.getpid())
    TEST_PATH = os.path.join(TEST_DIR, "balloon_shown.json")
    ns["_BALLOON_SHOWN_PATH"] = TEST_PATH
    os.makedirs(TEST_DIR, exist_ok=True)
    for p in (TEST_PATH, TEST_PATH + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass
    print("  （名单落盘改写到 %s）" % os.path.basename(TEST_PATH))
    print("")

    # ── 1. 播过的要记住 ──
    shown.clear()
    ns["_BALLOON_SHOWN_SAVED_AT"][0] = now + 1e6      # 这一组不许动盘（省得把后面的落盘测带偏）
    mark(["a", "b", "", None])
    s1 = seen()
    check("记过的两条都算播过", s1 == {"a", "b"}, str(sorted(s1)))
    check("空键不入表（空标题不该把整类挡掉）", "" not in s1 and None not in s1, str(sorted(s1)))

    # ── 2. 到点自然忘（不是整份清空）──
    shown.clear()
    mark(["old", "new"])
    shown["old"] = now - ttl - 1
    s2 = seen()
    check("过了 TTL 的那条被忘掉", "old" not in s2, str(sorted(s2)))
    check("没过期的一条还记着", s2 == {"new"}, str(sorted(s2)))

    # ── 3. 关键回归：超上限只删最久的，不许一把清光 ──
    shown.clear()
    for i in range(cap + 500):
        shown["k%05d" % i] = now
    mark(["新来的"])
    s3 = seen()
    check("超上限后仍留着绝大部分历史(不是被 clear 掉)",
          len(s3) > cap * 0.9, "%d 条" % len(s3))
    check("最久没播的那批被删掉了", "k00000" not in s3, str(len(s3)))
    check("刚播的这条一定在名单里", "新来的" in s3, "")

    # ── 4. 断网几天后回来：记的时长内不许重播，过期了才允许 ──
    shown.clear()
    mark(["三天前播过"])
    shown["三天前播过"] = now - 3 * 86400
    s4 = seen()
    check("超过名单时长的条目允许重新播（不然永远没新内容）", "三天前播过" not in s4, "")
    shown["半天前播过"] = now - 20 * 3600
    check("20 小时的还在名单里（那天没播完的晚上回来不该再播一遍）", "半天前播过" in seen(), "")

    # ── 5. 常看常新 ③：记的时长必须压在 ① 的硬上限之上 ──
    fresh_h, prefer_h = ns["BALLOON_FRESH_HOURS"], ns["BALLOON_FRESH_PREFER_HOURS"]
    check("硬上限就是 24 小时（用户说的「一天前的旧新闻」）", fresh_h == 24, str(fresh_h))
    check("优先档比硬上限紧，且是真小时数", 0 < prefer_h < fresh_h, str(prefer_h))
    check("播过名单记 (硬上限 + 2) 小时 = 26h，不留「先忘掉却还在窗口里」的缝",
          ttl == (fresh_h + 2) * 3600, "%d 秒" % ttl)
    check("名单时长确实长于时效窗口（① 和 ③ 绑死，改一处另一处跟着变）",
          ttl > fresh_h * 3600, "%d vs %d" % (ttl, fresh_h * 3600))

    # ── 6. 常看常新 ①：发布时间超过 24 小时的一律不进候选 ──
    age, cut = ns["_balloon_age_h"], ns["_balloon_fresh_cut"]

    def _it(hours):
        """published 是 unix 秒（和 server 里那批解析器一致）。hours=None → 0（热榜那种）。"""
        return {"title": "%d 小时" % hours if hours is not None else "热榜",
                "published": 0 if hours is None else now - hours * 3600}

    check("算得出一条 3 小时前的稿子几小时了", abs(age(_it(3), now) - 3) < 0.01,
          str(age(_it(3), now)))
    check("没发布时间 → None（调用方按「新」放行）", age(_it(None), now) is None, "")
    check("published 是脏值也不炸", age({"published": "abc"}, now) is None
          and age({"published": None}, now) is None
          and age({}, now) is None, "")
    check("时间戳在未来（服务器时钟快了）算 0 小时，不返回负数",
          age(_it(-5), now) == 0.0, str(age(_it(-5), now)))

    one = cut([_it(25), _it(30)], now)
    check("两条都超 24 小时 → 一条不剩，且报出挡了 2 条",
          one[0] == [] and one[1] == 2, repr(one))
    two = cut([_it(24), _it(23.5)], now)
    check("24 小时整按「没超」放行（口径是 >24，不是 >=）",
          len(two[0]) == 2 and two[1] == 0, repr(two))
    three = cut([_it(2), _it(45)], now)
    check("旧的被摘走、新的留下", [i["title"] for i in three[0]] == ["2 小时"]
          and three[1] == 1, repr(three))
    four = cut([_it(None), _it(None)], now)
    check("热榜那种没有发布时间的照常播（不一起清空）",
          len(four[0]) == 2 and four[1] == 0, repr(four))

    # ── 7. 常看常新 ②：6 小时内够一轮就只从 6 小时里挑 ──
    need = ns["BALLOON_PLAY_COUNT"]
    plenty = [_it(1)] * need + [_it(20)] * 10
    got, stale, held = cut(plenty, now, prefer_hours=prefer_h, prefer_need=need)
    check("6 小时内够 %d 条 → 只播这 %d 条，20 小时那批自动让位" % (need, need),
          len(got) == need and stale == 0 and held == 10,
          "留 %d 挡 %d 让 %d" % (len(got), stale, held))
    check("让位的条数按「留下几条」算，不是按挡掉的算", held == 10 and stale == 0, "")
    notenough = [_it(1)] * (need - 1) + [_it(20)] * 10
    got2, stale2, held2 = cut(notenough, now, prefer_hours=prefer_h, prefer_need=need)
    check("6 小时内凑不满一轮 → 放宽到 24 小时那一档，不许空着",
          len(got2) == need - 1 + 10 and held2 == 0, "留 %d 让 %d" % (len(got2), held2))
    got3, _s3, _h3 = cut([_it(20)] * 12, now, prefer_hours=prefer_h, prefer_need=need)
    check("一条 6 小时内的都没有 → 整天内的照播（② 只是优先，不是第二道硬闸）",
          len(got3) == 12, "留 %d" % len(got3))
    got4, _s4, _h4 = cut([_it(6), _it(2)], prefer_hours=prefer_h, prefer_need=1, now=now)
    check("6 小时整算「优先档内」（<=，跟 ① 的 > 对齐不打架）", len(got4) == 2, repr(got4))
    check("prefer_hours 不给 → 只做 ①（日报那一套：要的是这一天，不是最近 6 小时）",
          cut([_it(2), _it(20)], now)[0:1] == ([_it(2), _it(20)],),
          repr(cut([_it(2), _it(20)], now)[2]))

    # ── 8. 静态闸：这两条路真的接上了同一个尺子 ──
    _notify = func_seg(server_src, "_notify_news")
    check("播报入口用的是 prefer_hours=BALLOON_FRESH_PREFER_HOURS（不另抄一份数字）",
          "_balloon_fresh_cut(fresh, prefer_hours=BALLOON_FRESH_PREFER_HOURS" in _notify
          and "prefer_need=BALLOON_PLAY_COUNT" in _notify, "")
    check("挡掉的旧闻在日志里说清「页面照常留着，只是不播」（页面口径没动）",
          "页面照常留着，只是不播" in _notify, "")
    check("一条新鲜的都没有 → 明说「没有新的可播」，不拿旧的凑数（规则 ④）",
          "没有新的可播" in _notify, "")
    _digest = func_seg(server_src, "_daily_digest")
    check("日报那一路也走同一个函数（只 24 小时、不套 6 小时优先档）",
          "_balloon_fresh_cut(pool, prefer_hours=None)" in _digest,
          "没找到" if not _digest else "%d 字符" % len(_digest))
    check("时效闸门写在播报入口的第一道（旧闻不该占掉后面那些闸的额度）",
          0 <= _notify.find("_balloon_fresh_cut(") < _notify.find("_balloon_seen()"), "")
    # 【2026-10-29 实机验证揪出来的第三条路】开机那一轮不在 _notify_news 里，
    # 它自己拼池子、自己挑，以前两道闸都没接：关机一天再开机，播的就是昨天的新闻，
    # 而且开机播完那 10 条几分钟后还能被「更新提醒」再播一遍。
    _bi = server_src.find("def _boot_balloon(")
    _boot = server_src[_bi:server_src.find("target=_boot_balloon", _bi)] if _bi >= 0 else ""
    check("启动播报也裁时效（①②：>24h 一律不进候选，6h 内够一轮就只取 6h）",
          "_balloon_fresh_cut(" in _boot
          and "prefer_hours=BALLOON_FRESH_PREFER_HOURS" in _boot
          and "prefer_need=BALLOON_PLAY_COUNT" in _boot,
          "没摘到" if not _boot else "%d 字符" % len(_boot))
    check("启动播报挑不出新鲜的就不弹（④：开机那一下不拿旧闻凑数）",
          "不拿旧闻凑数" in _boot and "return" in _boot, "")
    check("启动播报把这 10 条记进播过名单（③：否则下一轮刷新重播）",
          "_balloon_mark_shown(" in _boot and "force=True" in _boot, "")
    check("记名单的顺序在播出去之前/之后都算，但必须在 _pick 之后（记的是真播的那几条）",
          0 <= _boot.find("_pick = _pick_balloon_items(") < _boot.find("_balloon_mark_shown("),
          "")
    # 【2026-10-29 补】只记不查 = 重启一次原样重播一遍：开机挑之前得先扣掉 26 小时内播过的。
    check("启动播报挑之前先扣掉播过名单（重启不重播上午那 10 条）",
          "_balloon_seen()" in _boot
          and 0 <= _boot.find("_balloon_seen()") < _boot.find("_pick = _pick_balloon_items("),
          "")
    check("开机与自动轮播的「挑选+记账」在同一把锁里（不撞同一批）",
          "with _BALLOON_PICK_LOCK:" in _notify and "with _BALLOON_PICK_LOCK:" in _boot, "")
    _i = server_src.find("为升级让出端口")
    _die_src = server_src[_i:_i + 400] if _i >= 0 else ""
    check("升级的两条退出路（静默更新 / 安装替换）退出前也 flush 名单",
          "_balloon_shown_flush" in _die_src
          and "_balloon_shown_flush" in server_src[server_src.find("if _SILENT_RUN:"):][:800],
          "die %d 字符" % len(_die_src))

    # ── 9. 常看常新 ③：名单跨重启 ──
    load, save = ns["_balloon_shown_load"], ns["_balloon_shown_save"]
    shown.clear()
    ns["_BALLOON_SHOWN_SAVED_AT"][0] = 0.0
    mark(["上午播过的头条"])
    check("播过之后真的写了盘（重启不该把「播过」这件事忘掉）",
          os.path.exists(TEST_PATH), TEST_PATH)
    shown.clear()
    n = load()
    check("读回来以后又是「播过」（重启后第一轮不能重播上午那 10 条）",
          "上午播过的头条" in seen() and n == 1, "%d 条" % n)
    shown.clear()
    ns["_BALLOON_SHOWN_SAVED_AT"][0] = 0.0
    mark(["太旧了"])
    shown["太旧了"] = now - ttl - 10
    ns["_BALLOON_SHOWN_SAVED_AT"][0] = 0.0
    save()
    shown.clear()
    load()
    check("读回时顺手把已过期的丢掉（不无限膨胀）", "太旧了" not in seen(), str(sorted(seen())))
    # 节流：一分钟最多写一次，别让「立即轮播」连点把盘磨坏
    shown.clear()
    ns["_BALLOON_SHOWN_SAVED_AT"][0] = 0.0
    mark(["第一条"])
    t1 = os.path.getmtime(TEST_PATH)
    ns["_BALLOON_SHOWN_SAVED_AT"][0] = time.time()          # 假装刚写过
    mark(["第二条"])
    check("节流窗口内不重复写盘", os.path.getmtime(TEST_PATH) == t1, "")
    _save_src = func_seg(server_src, "_balloon_shown_save")
    check("临时文件名带 pid+线程号（三条线程同时 force 也不会把内容写串）",
          'tmp = "%s.%d.%d.tmp" % (_BALLOON_SHOWN_PATH, os.getpid(), threading.get_ident())'
          in _save_src,
          "没摘到" if not _save_src else "%d 字符" % len(_save_src))
    check("写盘有专用锁，且写完 os.replace（断电也不会留半份名单）",
          "with _BALLOON_SHOWN_IO_LOCK:" in _save_src
          and "os.replace(tmp, _BALLOON_SHOWN_PATH)" in _save_src
          and "except Exception" in _save_src,
          "没摘到" if not _save_src else "%d 字符" % len(_save_src))
    # 真并发：几条线程同时 force 落盘，落下来的必须是能读的完整 JSON、且没有残留 tmp
    shown.clear()
    for i in range(40):
        shown["并发%d" % i] = time.time()
    ns["_BALLOON_SHOWN_SAVED_AT"][0] = 0.0
    ths = [threading.Thread(target=save, kwargs={"force": True}) for _ in range(6)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(5)
    ok_json = False
    try:
        with open(TEST_PATH, encoding="utf-8") as f:
            ok_json = len((json.load(f) or {}).get("shown") or {}) == 40
    except Exception:
        ok_json = False
    check("6 条线程同时 force 落盘，文件仍是完整 JSON", ok_json, "")
    leftovers = [n for n in os.listdir(TEST_DIR) if n.startswith("balloon_shown.json.") and n.endswith(".tmp")]
    check("并发落盘不给目录留临时文件", leftovers == [], str(leftovers))
    # 【2026-10-29 实机验证揪出来的缝】节流是"一分钟最多写一次"，可 os._exit 不走 atexit：
    # 用户双击托盘看完这一轮就退出程序，这一轮的名单正好卡在节流窗口里没落盘，
    # 下次开机又会把同一批播第二遍（规则 ③ 破功）。手动那一轮 + 退出前那一次都强制写。
    shown.clear()
    ns["_BALLOON_SHOWN_SAVED_AT"][0] = 0.0
    mark(["节流内的第一条"])
    t0 = os.path.getmtime(TEST_PATH)
    time.sleep(0.02)
    ns["_BALLOON_SHOWN_SAVED_AT"][0] = time.time()
    mark(["手动那一轮"], force=True)
    check("手动（双击托盘）那一轮不等节流，当场写盘", os.path.getmtime(TEST_PATH) > t0, "")
    shown.clear()
    load()
    check("强制写完之后，重启读回来的正是手动那一轮播过的条目",
          "手动那一轮" in seen() and "节流内的第一条" in seen(), str(sorted(seen())))
    ns["_BALLOON_SHOWN_SAVED_AT"][0] = time.time()
    shown["退出前最后一条"] = time.time()
    t1b = os.path.getmtime(TEST_PATH)
    time.sleep(0.02)
    ns["_balloon_shown_flush"]()
    check("退出前 flush 也越过节流（os._exit 不走 atexit，只能自己写）",
          os.path.getmtime(TEST_PATH) > t1b, "")
    shown.clear()
    load()
    check("flush 写进去的那条同样读得回来", "退出前最后一条" in seen(), str(sorted(seen())))
    check("force 只透传给写盘，不改名单本身（记的时长、上限那条逻辑没动）",
          "def _balloon_mark_shown(keys, force=False)" in server_src
          and "_balloon_shown_save(force=force)" in server_src
          and "_balloon_shown_save(force=True)" in func_seg(server_src, "_balloon_shown_flush"),
          "")
    _quit_src = func_seg(server_src, "_quit_app_now")
    _apiq = server_src[server_src.find("def api_quit("):]
    _apiq = _apiq[:_apiq.find("\n\n\n")] if "\n\n\n" in _apiq else _apiq[:3000]
    check("两条退出路（托盘菜单「退出」和网页「退出程序」）退出前都 flush 一次",
          "_balloon_shown_flush()" in _quit_src and "_balloon_shown_flush()" in _apiq,
          "quit %d / api_quit %d 字符" % (len(_quit_src), len(_apiq)))
    # 坏文件 / 没文件：宁可多播一条，也不许因为一个缓存文件起不来
    try:
        os.remove(TEST_PATH)
    except OSError:
        pass
    shown.clear()
    check("文件不存在 → 返回 0，不抛（首启机器上没有这份名单）", load() == 0, "")
    with open(TEST_PATH, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")
    shown.clear()
    check("名单文件读坏了 → 当没播过，绝不抛异常", load() == 0 and seen() == set(), "")
    try:
        os.remove(TEST_PATH)
    except OSError:
        pass

    # ── 10. 投放记录：留最近若干条，新的挤掉旧的 ──
    tr, trmax = ns["_BALLOON_TRACE"], ns["_BALLOON_TRACE_MAX"]
    tr.clear()
    for i in range(trmax + 6):
        trace("第 %d 轮" % i)
    check("记录条数不超过上限", len(tr) == trmax, "%d 条" % len(tr))
    check("留下的是最近的几轮", tr[-1].endswith("第 %d 轮" % (trmax + 5)), tr[-1])
    check("最早那几轮已经挤掉了", not any(x.endswith("第 0 轮") for x in tr), "")
    check("每条都带时间(排查对得上时刻)", tr[-1][:2].isdigit(), tr[-1])

    print("")
    if FAILS:
        print("未通过 %d 项:" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        sys.exit(main())
    finally:
        _p = os.path.join(tempfile.gettempdir(),
                          "hn-test-balloon-shown-%d.json" % os.getpid())
        for _f in (_p, _p + ".tmp"):
            try:
                os.remove(_f)
            except OSError:
                pass
