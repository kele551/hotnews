# -*- coding: utf-8 -*-
"""数据源自检：跑一遍 SOURCES，报告条数 / 最新时间 / 是否有图"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
from datetime import datetime
import server

for region in ("cn", "intl"):
    items = server._collect(region)
    print("=" * 60)
    print("%s 合计 %d 条" % (region, len(items)))
    if not items:
        print("  !! 空")
        continue
    ts = [i["published"] for i in items if i["published"]]
    if ts:
        print("  最新: %s" % datetime.fromtimestamp(max(ts), server.CST))
        print("  最旧: %s" % datetime.fromtimestamp(min(ts), server.CST))
    withimg = sum(1 for i in items if i["image"])
    print("  带图: %d / %d" % (withimg, len(items)))
    # 按分类统计
    from collections import Counter
    c = Counter(i["cls"] for i in items)
    print("  分类: %s" % dict(c))
    print("  前 8 条:")
    for it in items[:8]:
        t = datetime.fromtimestamp(it["published"], server.CST).strftime("%m-%d %H:%M") if it["published"] else "无时间"
        print("    %s  [%s] %s  img=%s" % (t, it["cls"], it["title"][:32], bool(it["image"])))
