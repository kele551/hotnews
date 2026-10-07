# -*- coding: utf-8 -*-
"""数据源自检：跑一遍 SOURCES，报告条数 / 最新时间 / 是否有图"""
import io
import sys, os, time
from datetime import datetime
from collections import Counter

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
import server

now = time.time()
VERBOSE = os.environ.get("CN_VERBOSE") == "1"
for region in ("cn", "intl"):
    items = server._collect(region)
    print("=" * 66)
    print("%s 合计 %d 条" % (region, len(items)))
    if not items:
        print("  !! 空")
        continue
    ts = [i["published"] for i in items if i["published"]]
    if ts:
        newest = max(ts)
        print("  最新: %s  (%.1f 小时前)" % (
            datetime.fromtimestamp(newest, server.CST).strftime("%Y-%m-%d %H:%M:%S"),
            (now - newest) / 3600.0))
        print("  最旧: %s" % datetime.fromtimestamp(min(ts), server.CST).strftime("%Y-%m-%d %H:%M:%S"))
    else:
        print("  !! 全部条目无时间字段")
    withimg = sum(1 for i in items if i["image"])
    print("  带图: %d / %d" % (withimg, len(items)))
    # 按源统计（看哪些源是拖后腿的陈旧源）
    print("  -- 按源 --")
    groups = {}
    for it in items:
        groups.setdefault(it.get("channel") or it.get("label") or "?", []).append(it)
    for ch, group in sorted(groups.items(),
                            key=lambda kv: max([i["published"] for i in kv[1] if i["published"]] or [0]),
                            reverse=True):
        gts = [i["published"] for i in group if i["published"]]
        if gts:
            newest = max(gts)
            ages = [(now - t) / 3600 for t in gts]
            statues = "%s｜最新 %.1fh 前｜中位 %.1fh 前" % (
                datetime.fromtimestamp(newest, server.CST).strftime("%m-%d %H:%M"),
                (now - newest) / 3600, sorted(ages)[len(ages) // 2])
        else:
            statues = "全部无时间字段"
        gi = sum(1 for i in group if i["image"])
        if VERBOSE:
            statues += "｜%s" % "、".join(
                "%s(%.1fh)" % (i["title"][:18], (now - i["published"]) / 3600)
                for i in sorted(group, key=lambda x: x["published"], reverse=True)[:3])
        print("     %-22s %3d条 图%3d  %s" % (ch, len(group), gi, statues))
    clses = Counter(i["cls"] for i in items)
    print("  分类: %s" % dict(clses))
