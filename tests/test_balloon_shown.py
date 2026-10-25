# -*- coding: utf-8 -*-
# 作者: 海风（kele551）
# 用途: 气泡"已播名单"与投放记录的判据单测（2026-10-25 待办 #65）。
#
# 盯的是那次真 bug：`_BALLOON_SHOWN` 原来是 set，"本轮新增的全播过"时**整份 clear()**，
# 于是几千条历史一起被忘掉，隔几天又冒回新增列表的老新闻立刻能再弹一次。
# 现在改成按条记 12 小时 + 超上限从最久没播那头删 —— 这几条都得到处有闸。
#
# 为什么不 import server.py：那是八千行、模块级要建 FastAPI 应用、读配置、起缓存目录。
# 这里用 ast 只把 `_balloon_seen` / `_balloon_mark_shown` / `_balloon_trace`
# 和它们的常量摘出来单独 exec，全程不碰网络、不碰本机数据目录。
import ast
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server.py")

WANT_FUNCS = ("_balloon_seen", "_balloon_mark_shown", "_balloon_trace")
WANT_CONSTS = ("_BALLOON_SHOWN", "_BALLOON_SHOWN_TTL", "_BALLOON_SHOWN_MAX",
               "_BALLOON_SHOWN_LOCK", "_BALLOON_TRACE", "_BALLOON_TRACE_MAX")

FAILS = []


def load_fragment():
    tree = ast.parse(open(SERVER, encoding="utf-8").read())
    ns = {"time": time, "threading": threading, "datetime": datetime,
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


def main():
    print("摘取 server.py 里的气泡判据：")
    ns = load_fragment()
    seen, mark, trace = ns["_balloon_seen"], ns["_balloon_mark_shown"], ns["_balloon_trace"]
    shown, ttl, cap = ns["_BALLOON_SHOWN"], ns["_BALLOON_SHOWN_TTL"], ns["_BALLOON_SHOWN_MAX"]
    now = time.time()
    print("")

    # ── 1. 播过的要记住 ──
    shown.clear()
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

    # ── 4. 断网几天后回来：12 小时内的老新闻重冒回新增列表也不该再弹 ──
    shown.clear()
    mark(["三天前播过"])
    shown["三天前播过"] = now - 3 * 86400
    s4 = seen()
    check("超过 12 小时的条目允许重新播（不然永远没新内容）", "三天前播过" not in s4, "")

    # ── 5. 投放记录：留最近若干条，新的挤掉旧的 ──
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
    sys.exit(main())
