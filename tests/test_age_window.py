# -*- coding: utf-8 -*-
# 作者: 海风（kele551）
# 用途: 时效窗口口径（_max_age_hours）的单测 —— 2026-10-26 修的那三条挡路项之一。
#
# 背景：mefcl / 凤凰娱乐 / 凤凰资讯 三个解析器各自写 `MAX_AGE_HOURS.get("cn", 72)`，
# 而 "cn" 键一直是 24 —— 那个 72/24 的"默认值"从没生效过，垂类源在解析阶段就被按
# 24 小时砍光。实测 2026-10-26：mefcl 首页 20 条全部因「站方只给日期、按当天 00:00 计，
# 昨天的稿子早于 now-24h」被丢光，软件栏目整个消失。
#
# 为什么不 import server.py：八千行的服务，模块级就建 FastAPI、读配置、碰数据目录。
# 照 tests/test_archive_backfill.py 的办法用 ast 只摘要测的函数与三张表。
import ast
import os
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server.py")

WANT_FUNCS = {"_max_age_hours"}
WANT_CONSTS = {"MAX_AGE_HOURS", "CLS_MAX_AGE_HOURS"}

FAILS = []


def check(name, got, want):
    if got == want:
        print("[ok] %s" % name)
    else:
        print("[X ] %s  得到=%r 应为=%r" % (name, got, want))
        FAILS.append(name)


def load():
    src = open(SERVER, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS and not node.decorator_list:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in WANT_CONSTS:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
    for c in WANT_CONSTS | WANT_FUNCS:
        if c not in ns:
            FAILS.append("摘不到 " + c)
    return ns, tree


def main():
    ns, tree_full = load()
    f = ns["_max_age_hours"]
    print("---- 1) 优先级：源自己声明 > 栏目表 > 分区默认")
    check("软件按栏目表", f({"cls": "软件", "region": "cn"}), ns["CLS_MAX_AGE_HOURS"]["软件"])
    check("娱乐按栏目表", f({"cls": "娱乐", "region": "cn"}), ns["CLS_MAX_AGE_HOURS"]["娱乐"])
    check("热榜不豁免", f({"cls": "热榜", "region": "cn"}), 24)
    check("无栏目时按分区 cn", f({"region": "cn"}), ns["MAX_AGE_HOURS"]["cn"])
    check("无栏目时按分区 intl", f({"region": "intl"}), ns["MAX_AGE_HOURS"]["intl"])
    check("src 自己声明优先", f({"cls": "软件", "region": "cn", "max_age_hours": 6}), 6)
    check("0 视为没声明(不能当合法窗口)", f({"cls": "软件", "region": "cn", "max_age_hours": 0}),
          ns["CLS_MAX_AGE_HOURS"]["软件"])

    print("---- 2) 老写法确实是坑：MAX_AGE_HOURS.get(\"cn\", 72) == 24")
    check("老写法给的是 24 不是 72", ns["MAX_AGE_HOURS"].get("cn", 72), 24)
    check("mefcl 现在拿到的窗口", f({"id": "cn-mefcl", "cls": "软件", "region": "cn"}), 120)

    print("---- 3) 用 2026-10-26 实测的那批日期回放过滤，看留几条")
    CST = timezone(timedelta(hours=8))
    now = datetime(2026, 10, 26, 9, 30, tzinfo=CST)
    dates = ["2026-10-22", "2026-10-11", "2026-09-28", "2026-09-25", "2026-09-23",
             "2026-09-21", "2026-09-15", "2026-09-12"] + ["2026-10-25"] * 12

    def keep(hours):
        cutoff = now.timestamp() - hours * 3600
        n = 0
        for d in dates:
            y, m, dd = [int(x) for x in d.split("-")]
            ts = datetime(y, m, dd, tzinfo=CST).timestamp()
            if ts >= cutoff:
                n += 1
        return n

    check("按老口径 24h 留几条", keep(24), 0)
    check("按新口径 120h 留几条", keep(120), 13)     # 10-22 一条 + 10-25 十二条（cutoff=10-21 09:30）

    print("---- 4) 坑写法与「三档表达式」都清零（用 ast 认，不被注释/文档字符串骗）")
    hits = []
    dup = []
    for node in ast.walk(tree_full):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "MAX_AGE_HOURS" and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == "cn"):
            hits.append((node.lineno, ast.unparse(node)))
    check('还在用 MAX_AGE_HOURS.get("cn", …) 的地方', hits, [])
    # 口径只此一处：那段「源声明 > 栏目表 > 分区」的三档表达式，除 _max_age_hours 本体
    # 之外不许再出现第二遍。2026-10-26 之前它被抄了 6 份，其中 3 份抄成了死默认值。
    for fn in tree_full.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name == "_max_age_hours":
            continue
        for c in ast.walk(fn):
            if (isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                    and c.func.attr == "get" and isinstance(c.func.value, ast.Name)
                    and c.func.value.id == "CLS_MAX_AGE_HOURS" and c.args
                    and "src" in ast.unparse(c.args[0])):
                dup.append((fn.name, c.lineno))
    check("把三档表达式抄在别处的函数", dup, [])

    print("\n%s  失败 %d 条" % ("全部通过" if not FAILS else "有失败", len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
