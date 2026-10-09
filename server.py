# -*- coding: utf-8 -*-
"""
hotnews —— 热点新闻检索（国内 / 国际）
FastAPI 后端：抓取 RSS、解析题图、图片代理、翻译、SSR 首屏
"""
import sys
import tempfile          # 【2026-10-07 修】第 3244 行用了 tempfile.gettempdir()，
                         # 但一直没 import —— Windows 上 LOCALAPPDATA 有值走短路不炸，Linux 上必崩。
import os
import re
import time
import json
import html
import base64
import hashlib
import hmac              # 【2026-10-09 修 高-4】本机令牌比对（标准库，不引入新依赖）
import ipaddress         # 【2026-10-09 修 高-1】判图片域名解析出来的是不是内网/回环地址
import socket            # 同上：只解析域名，不建连接
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timezone, timedelta
from collections import Counter, OrderedDict
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

if getattr(sys, "frozen", False):
    # 打包成 exe 后：同级目录 = exe 所在目录（放用户可改的 config.json）
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 无窗口打包（--noconsole）时没有控制台可写：sys.stdout 可能是 None，
# 也可能是被丢弃的无效流。打包态统一把标准输出接到 exe 同级的 hotnews.log，
# 既不会因 print 崩溃，也留下排查线索。
LOG_PATH = os.path.join(BASE_DIR, "hotnews.log")
if getattr(sys, "frozen", False):
    try:
        # 超过 1MB 就滚动一次，避免长期运行把盘占满
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1024 * 1024:
            os.replace(LOG_PATH, LOG_PATH + ".old")
    except Exception:
        pass
    try:
        _LOG_FP = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    except Exception:
        _LOG_FP = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = _LOG_FP
    sys.stderr = _LOG_FP

# 【2026-10-07 教训】这里曾经把 httpx / feedparser 做成"懒加载代理"想省启动时间，
# 结果 **RSS 全部抓不到**（feedparser 内部依赖模块级状态，代理包装后解析静默失败），
# 科技栏目整个消失；而实测启动只快了 0.07 秒（4.70s → 4.63s）。
# 收益为零、代价是整个栏目没了 —— 已回退，别再这么干。
import httpx  # noqa: E402
import feedparser  # noqa: E402
from fastapi import FastAPI, Query, Request, Response  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import updater  # noqa: E402


def _res(rel):
    """
    资源定位：兼容打包形态
      静态资源(static/) → PyInstaller 解包目录 sys._MEIPASS，回落 exe 同级目录
      可写配置(config/version) → exe 同级目录优先，不被临时解包目录吞掉
    """
    cands = []
    if getattr(sys, "_MEIPASS", None):
        cands.append(os.path.join(sys._MEIPASS, rel))
    cands.append(os.path.join(BASE_DIR, rel))
    for p in cands:
        if os.path.exists(p):
            return p
    return cands[-1]


STATIC_DIR = _res("static")

def _read_version():
    """版本号以 version.json 为唯一来源，避免和常量写两处走岔"""
    try:
        with open(_res("version.json"), "r", encoding="utf-8") as f:
            return json.load(f).get("version", "1.1.0")
    except Exception:
        return "1.1.0"


VERSION = _read_version()

# 【2026-10-09 修 中-5】进程启动时刻：/api/version 会把 uptime 带出去，
# app.py 二次启动时据此判断"对面是不是一个正常服务的实例"（见 app.py 的 find_running）。
_START_TS = time.time()

# 【2026-10-09 升级体验口径】本进程是不是「后台静默实例」（开机自启那种）。
# 决定发现新版本后"立刻装上重启"还是"退出时自动装"：静默实例没人看页面，立刻装最无感。
# 与 app.py 的 _silent 判断同源（argv 与环境变量都认）。
_SILENT_RUN = ("--silent" in sys.argv) or (os.environ.get("HOTNEWS_SILENT") == "1")

# 由 app.py 在启动时写入真实访问地址（端口可能被自动顺延），供通知点击跳转使用
APP_URL = "http://localhost:8000"

# 【2026-10-09 换源·抓取礼貌】自报身份：
#   站方要能在访问日志里认出「这是谁在抓、怎么联系、怎么让我停下」，所以 UA 必须带项目标识。
#   但**不把浏览器 UA 整个换掉** —— 实测部分源（如 mefcl）会按 UA 判断是不是
#   普通浏览器访问，纯 bot UA 会被挡在门外（等于把源打挂）。故采用「浏览器 UA + 追加标识」：
#   既保留了兼容性，又满足自报身份的要求。
BOT_ID = "HotnewsAggregator/1.5 (+https://gitee.com/kele551/hotnews)"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 " + BOT_ID)

CST = timezone(timedelta(hours=8))
CACHE_TTL = 300          # RSS 缓存 5 分钟
IMG_CACHE_TTL = 86400    # 图片缓存 1 天（**浏览器端** Cache-Control 用这个值）
# 【2026-10-09 修 高-2 / 高-3】
#   IMG_MEM_TTL  —— 进程内那份图片字节只留 6 小时。原来跟着浏览器缓存一起留 1 天，
#                   一天里跑几百上千条新闻，等于把图片本体都攒在内存里（详见 _TTLCache）。
#   FAIL_CACHE_TTL / IMG_FAIL_CACHE_TTL —— 抓取失败只写一个**很短**的负缓存：
#                   原来失败也按整段 TTL 记（文档 5 分钟、图片 404 一整天），一次网络抖动
#                   就让该源整轮消失、破图一天不自愈。留十几秒只是防同一瞬间的重复打点。
IMG_MEM_TTL = 6 * 3600
FAIL_CACHE_TTL = 15
IMG_FAIL_CACHE_TTL = 60
RETRY_BACKOFF = 0.6      # 失败重试前等这么久；只对超时/连接错误/5xx 重试，且只重试 1 次
IMG_CACHE_MAX_ITEMS = 256                # 图片缓存条数上限（LRU）
IMG_CACHE_MAX_BYTES = 32 * 1024 * 1024   # 图片缓存总字节预算（LRU）
IMG_ONE_MAX_BYTES = 1024 * 1024          # 单张超过 1MB 不进内存缓存，直接透传给浏览器
DOC_CACHE_MAX_ITEMS = 512                # RSS/文章页缓存条数上限
DOC_CACHE_MAX_BYTES = 16 * 1024 * 1024   # 同上，字节预算
# 【加速】部分下载上限（2026-10-07）：
#   ART_PAGE_BYTES —— 抓文章页只要 <head> 里的 og:image，96KB 足够，不必下整页
#   IMG_HEAD_BYTES —— 测图片尺寸只要文件头，256KB 足够覆盖 JPEG 的 SOF 段
ART_PAGE_BYTES = 96 * 1024
IMG_HEAD_BYTES = 256 * 1024
# 用户要求（2026-10-07）：所有板块必须是「当天的」。
# 采集阶段先用 24 小时窗口（放得够宽，便于凌晨做「当天不足则回退」的兜底），
# 真正「只留当天」的收紧在 _collect 里由 _select_fresh 统一执行。
# 国内 24 小时（用户要求「当天」）；国际放宽到 48 小时 ——
# 实测国际板块的条目跨度偏大：CGTN 世界频道从当天到两天前都有（实测最新 2.7 小时前、
# 最旧 30 多小时前），加上从国内板块转来的国际条目，按 24 小时收口会白丢一批。
# 【2026-10-09 换源】中新网·国际本身是分钟级更新的，48 小时这个窗口现在是
# 「给 CGTN 与转入条目留的余量」，不是「源太稀疏」的妥协了。
MAX_AGE_HOURS = {"cn": 24, "intl": 48}
SAME_DAY = True          # True = 只留当天（当天条目过少时自动回退到 24 小时）
FALLBACK_MIN = 8         # 当天条目少于这个数 → 放宽到最近 24 小时，避免凌晨打开是空页

# 【栏目分级时效 2026-10-07】用户要求「每个栏目必须有三个以上的网站存在，要不然太单一」。
# 但「当天」这条硬规则会把更新频率低的垂类源整个饿死 —— 实测 24 小时筛完：
#   小众软件 0 条 / 少数派 0 条 / 开源中国 0 条 / iplaysoft 1 条 / 极客公园 1 条，
#   结果「软件」栏目只剩 mefcl 一个源、「科技」只剩 IT之家 —— 正是用户反馈的现象。
# 所以：要闻/热榜等时效敏感栏目仍只留当天（走 MAX_AGE_HOURS=24h），
# 垂类（科技/软件/娱乐）各自放宽，保证栏目内至少 3 家不同网站。
CLS_MAX_AGE_HOURS = {
    "科技": 96,     # 4 天：IT之家/快科技日更，爱范儿/雷峰网 慢一些
    "软件": 120,    # 5 天：小众软件、开源中国、少数派这类本来就不是日更
    "娱乐": 120,    # 5 天：新浪娱乐首页混着深度稿，窗口太窄会只剩一两条
    "国际娱乐": 120,
    "热榜": 24,     # 用户反馈「热榜上居然还有两天前的新闻」→ 热榜不再豁免，同样按当天
}
# 「当天优先」的两个下限：某栏目当天条目低于这个数、或少于这么多家网站，
# 才允许补进较旧的条目（详见 _select_fresh）
FRESH_MIN_PER_CLS = 6
FRESH_MIN_SITES = 3

# ---------------- 数据源 ----------------
# 2026-10-06 全量实测后重构：人民网 / 新浪 / 网易 / 央视 / 环球网 / 澎湃 的 RSS
# 均已停更（人民网停在 2025-06-05，新浪停在 2025-09-23），仍返 200 但内容陈旧，
# 是最初「新闻太旧」的根因。现改用仍在实时更新的源。
#
# 【2026-10-09 换源·合规】按工作区章程 §9.1「默认只用境内权威媒体源，不采用境外来源」：
#   · 国际板块**删掉全部境外站点**，换成**境内媒体的国际版**
#     （CGTN + 中新网·国际，再由凤凰/澎湃/红星/界面报的国际新闻补足）；
#   · 国际热榜同步删掉那处境外热榜 API；
#   · 新增财经 / 体育 / 社会 / 娱乐源，全部是境内站点。
# 每个新源都做过真实联网实测（HTTP / 耗时 / 条数 / 带图数 / 最新时间），
# 逐条数据见 logs\换源-境内国际版-20261009.md；没实测通过的一律不写进来。
SOURCES = {
    "cn": [
        # 国内源只保留「稳定带图」的源；无图的中新网 6 频道已整体移除；
        # 中关村在线（数码）按用户要求于 1.3.0 移除。
        # （用户要求：不显示图片的新闻别上；若带图新闻过少则换更好的源）。
        # 界面 / IT之家 / 极客公园（科技）/ 小众软件（软件）实测有图且更新活跃；
        # 任一道退化无图，会被 _collect 的 no-image 过滤自动剔除。
        # 财经栏目按用户要求删除（界面 RSS 仅 1 条带图，达不到"出图"标准）。
        # 界面新闻本身保留，改归「要闻」，避免把你要求加的 jiemian 也一并砍掉。
        {"id": "cn-finance", "label": "界面",   "url": "https://a.jiemian.com/index.php?m=article&a=rss", "base": "https://www.jiemian.com", "region": "cn", "cls": "要闻"},
        # 澎湃新闻：官方 JSON 接口（parser=thepaper），自带标题/题图/直达链接/真实互动数。
        # 2026-10-06 用户点名要加；同批实测腾讯/网易/新浪的 RSS 全部 404、首页又是 JS 动态渲染
        # （新浪娱乐首页 45 条链接全是「站点地图/好莱坞/排行」这类导航），只有澎湃给出了干净的结构化数据。
        {"id": "cn-thepaper", "label": "澎湃新闻", "url": "https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar", "base": "https://www.thepaper.cn", "region": "cn", "cls": "要闻", "parser": "thepaper"},
        {"id": "cn-tech",    "label": "IT之家", "url": "https://www.ithome.com/rss/",                     "base": "https://www.ithome.com",   "region": "cn", "cls": "科技"},
        {"id": "cn-geek",    "label": "极客公园", "url": "https://www.geekpark.net/rss",                  "base": "https://www.geekpark.net", "region": "cn", "cls": "科技"},
        # 软件类：小众软件 RSS，条条带图（feed 内 media:content 含图），内容偏软件推荐/效率工具
        {"id": "cn-soft",    "label": "小众软件", "url": "https://www.appinn.com/feed/",                  "base": "https://www.appinn.com",   "region": "cn", "cls": "软件"},
        # 2026-10-07：改抓「新闻」列表页而不是首页——首页条目少且混着导航，
        # 【2026-10-07 补源】用户要求「每个栏目必须有三个以上的网站，要不然太单一」。
        # 以下三个都是**实测通过**（真连通 + 有题图 + 有当天/近日内容）：
        #   快科技 100 条 / 99 带图 / 当天 10:44 更新  ← 王牌
        #   爱范儿  20 条 / 20 带图 / 最新 3 天前
        #   雷峰网  20 条 / 17 带图 / 最新 3 天前
        # 实测失败、**未采用**：cnBeta（无条目）、品玩（无条目）
        {"id": "cn-mydrivers", "label": "快科技", "url": "http://rss.mydrivers.com/rss.aspx?Tid=1", "base": "https://news.mydrivers.com", "region": "cn", "cls": "科技"},
        {"id": "cn-ifanr",     "label": "爱范儿", "url": "https://www.ifanr.com/feed",   "base": "https://www.ifanr.com",   "region": "cn", "cls": "科技"},
        {"id": "cn-leiphone",  "label": "雷峰网", "url": "https://www.leiphone.com/feed", "base": "https://www.leiphone.com", "region": "cn", "cls": "科技"},
        # 软件类：iplaysoft（异次元软件世界）RSS 实测 30 条带图、更新活跃
        {"id": "cn-iplay",   "label": "iplaysoft", "url": "https://www.iplaysoft.com/feed/",              "base": "https://www.iplaysoft.com", "region": "cn", "cls": "软件"},
        # 【2026-10-07 补源】跟 mefcl 同类的软件分享站，实测通过：果核剥壳 10 条 / 10 带图。
        # 实测失败、**未采用**：423down（连不上）、微当下载（连不上）、
        #                       大眼仔旭（10 条但 0 张图，会被「没图不要」的规则剔除）
        {"id": "cn-ghpym",   "label": "果核剥壳", "url": "https://www.ghpym.com/feed", "base": "https://www.ghpym.com", "region": "cn", "cls": "软件"},
        # 软件类：mefcl（用户投稿站点），站方有 JS cookie 验证；抓首页（/feed/ 仍 403），
        # parser 内先按站方校验流程取到访问 cookie，再带 cookie 正常请求首页
        {"id": "cn-mefcl",   "label": "mefcl",     "url": "https://www.mefcl.com/",                       "base": "https://www.mefcl.com",     "region": "cn", "cls": "软件", "parser": "mefcl"},
        # 软件类：国内中文源（用户要求删除不易读的英文 GitHub 仓库，改用国内）。
        # 开源中国 RSS 软件发布动态（50 条，中文，带 180~210 字摘要）；少数派 RSS 工具/效率软件实践（10 条，中文）。
        # 两者无题图 → 加入 NOIMG_SOURCES 豁免无图过滤，前端走渐变块样式。
        {"id": "cn-oschina", "label": "开源中国", "url": "https://www.oschina.net/news/rss", "base": "https://www.oschina.net", "region": "cn", "cls": "软件"},
        {"id": "cn-sspai",   "label": "少数派",   "url": "https://sspai.com/feed",           "base": "https://sspai.com",       "region": "cn", "cls": "软件"},
        # 娱乐栏目：凤凰娱乐首页（明星/绯闻/八卦）。首页内联 JSON 带标题+题图+newsTime，量大。
        {"id": "cn-ent", "label": "凤凰娱乐", "url": "https://ent.ifeng.com/", "base": "https://ent.ifeng.com", "region": "cn", "cls": "娱乐", "parser": "ifeng"},
        # 【2026-10-07 补源】用户要求「娱乐栏目的新闻太少，增加新闻源头」。
        # 实测国内娱乐 RSS 全废（网易/中新网无条目、搜狐无图、人民网停更、时光网连不上），
        # 只能用新浪娱乐的 SSR HTML；它的列表页无图无时间，靠 _fill_missing_images 补。
        # 【已试过并放弃】网易娱乐 ent.163.com：首页 415 条带标题条目，图可改写 thumbnail参数放大到 660x440，但它是**网易号泛频道推荐流**（每次请求内容都不同），
        # 混着汽车/养生/育儿/社会；加娱乐关键词过滤后 40 条只剩 3 条且有 2 条不是娱乐。
        # 用户 2026-10-07 决定不做这个源，别再挖了。
        {"id": "cn-sina-ent", "label": "新浪娱乐", "url": "https://ent.sina.com.cn/", "base": "https://ent.sina.com.cn", "region": "cn", "cls": "娱乐", "parser": "sina_ent"},
        # ============ 【2026-10-09 换源】新增：财经 / 体育 / 社会 / 娱乐 ============
        # 目标：① 每个栏目至少 3 家网站；② 只用境内站点（合规口径见 COMPLIANCE.md 第 7 节）。
        # 下面每一条都做过真实联网实测（HTTP 状态 / 耗时 / 条数 / 带图数 / 最新时间），
        # 逐项数据记在 logs\换源-境内国际版-20261009.md；没实测通过的一律不写在这里。
        #
        # 华尔街见闻：实测 200 / 0.37s / 61 条 / 35 条带图 / 最新 1.7 小时前（财经垂类里更新最勤）。
        {"id": "cn-wscn", "label": "华尔街见闻", "url": "https://dedicated.wallstreetcn.com/rss.xml", "base": "https://wallstreetcn.com", "region": "cn", "cls": "财经"},
        # 中新网系（财经 / 体育 / 社会）：实测 200 / 约 20ms / 各 30 条 / 当天分钟级更新，
        # 但 **RSS 本身不带图** → 打 body_image 标记，走「正文页取首图」的图片策略
        # （限时 3 秒/条、命中 _ART_CACHE 缓存、取不到就跳过该条，详见 _fill_missing_images）。
        # 实测正文首图命中率 40%~60%，取到的多是 700x466 / 1080x783 这类达标大图。
        {"id": "cn-cnfin",   "label": "中新网·财经", "url": "https://www.chinanews.com.cn/rss/finance.xml", "base": "https://www.chinanews.com.cn", "region": "cn", "cls": "财经", "body_image": True},
        {"id": "cn-cnsport", "label": "中新网·体育", "url": "https://www.chinanews.com.cn/rss/sports.xml",  "base": "https://www.chinanews.com.cn", "region": "cn", "cls": "体育", "body_image": True},
        {"id": "cn-cnsoc",   "label": "中新网·社会", "url": "https://www.chinanews.com.cn/rss/society.xml", "base": "https://www.chinanews.com.cn", "region": "cn", "cls": "社会", "body_image": True},
        # 国际在线·娱乐（ent.cri.cn，中国国际广播电台 / 总台旗下）：影视综艺向。
        # 实测 200 / 0.023s / 卡片 40 张 / 24 小时内 19 条 / 当天分钟级更新。
        # ⚠ 卡片图是 **512x288 缩略图**（短边 288 差 12 像素过不了 300 的高清门槛，
        #   直接用会被后台低质图环节整条剔除、等于白加）——程序按站方 URL 规则改写成原图
        #   （实测 991x558 ~ 4052x2278，全部达标），见 _cri_image。
        # 它是娱乐栏目里第 3 家网站，也是「列表页自带标题+图+时间」的第二个源
        #（第一个是凤凰娱乐；新浪娱乐的列表页无图无时间，全靠逐条抓文章页）。
        {"id": "cn-cri-ent", "label": "国际在线娱乐", "url": "http://ent.cri.cn/", "base": "http://ent.cri.cn", "region": "cn", "cls": "娱乐", "parser": "cri_ent"},
    ],
    "intl": [
        # ============ 【2026-10-09 换源·合规】国际新闻只用境内媒体的国际版 ============
        # 原来这一栏 12 个源**全是境外站点**（境外站点 /
        # 境外站点），
        # 与章程 §9.1「默认只用境内权威媒体源，不采用境外来源」冲突，已整段删除。
        # 现在这一栏的构成（全部境内）：
        #   ① CGTN（中国国际电视台）世界频道 —— 实测 15~50 条、全部带图、当天更新；
        #   ② 中新网·国际 —— 实测 30 条 / 当天分钟级更新；RSS 不带图 → 正文页取首图；
        #   ③ 凤凰网 / 澎湃新闻 / 红星新闻 / 界面新闻 报的国际新闻 —— 由
        #      _split_cn_scope（国内要闻）与 _split_hot_by_scope（国内热榜）转过来，
        #      channel 标为 cn-intl-news / cn-intl-hot，本来就是中文，不依赖翻译。
        # 这样国际板块仍然「有要闻、有热榜、有娱乐/科技垂类」，只是**数据全部来自境内媒体**。
        {"id": "intl-cgtn",    "label": "CGTN",        "url": "https://www.cgtn.com/subscribe/rss/section/world.xml", "base": "https://www.cgtn.com",         "region": "intl", "cls": "要闻"},
        {"id": "intl-cnworld", "label": "中新网·国际", "url": "https://www.chinanews.com.cn/rss/world.xml",           "base": "https://www.chinanews.com.cn", "region": "intl", "cls": "要闻", "body_image": True},
    ],
    # 实时热点（今日头条热榜）走独立 _parse_hot，不走 RSS，这里仅占位以通过 region 校验
    "hot": [],
}

# 无题图的集合类源（国内软件类 RSS 无配图）：_collect 豁免无图过滤，前端走渐变块样式
# 用户要求（2026-10-07）：「每条新闻必须有高清大图，没图的就不要上」。
# 因此原来的 NOIMG_SOURCES 豁免已全部取消——开源中国 / 少数派这类不配图的源，
# 要么补上图，要么就从列表里消失（不再用渐变色块占位）。
NOIMG_SOURCES = set()

# 【2026-10-09 换源】「RSS 列表本身不带图、必须去正文页取首图」的源（SOURCES 里标 body_image）。
# 为什么单独登记：
#   · 图片策略与别家不同 —— 这些条目的图来自文章页正文，而中新网系**连 og:image 都没有**，
#     正文容器另有取法（见 _art_site_rule / _cn_body_image）；
#   · 要给它们**更短的超时**（BODY_IMG_TIMEOUT），别让一条慢页面把整轮补图拖住。
BODY_IMAGE_CHANNELS = {s["id"] for _rg in SOURCES.values() for s in _rg if s.get("body_image")}
BODY_IMG_TIMEOUT = 3.0     # 「去正文页取首图」的单条上限（秒）

_MISS = object()    # 缓存「没有这条」的哨兵：None 本身是「抓取失败」的合法缓存值


class _TTLCache:
    """带 TTL + 容量上限的 LRU 缓存（2026-10-09 新增，修 高-2）。

    原来 _CACHE / _IMG_CACHE 是普通 dict，只写不删：进程跑得越久内存越高，
    而 /img 不需要任何鉴权，别人换着 URL 请求就能把这个进程的内存灌满
    （无窗口运行时用户看不到任何提示，只会觉得"越用越卡"）。
    现在每条都记「过期时间 + 占用字节」，超条数或超字节预算就按 LRU 淘汰最久没用的那条；
    图片还额外限制单张体积（超过 IMG_ONE_MAX_BYTES 的直接透传，不进内存）。
    抓取是多线程的，所以内部加锁。
    """

    def __init__(self, ttl, max_items, max_bytes=0):
        self._ttl = ttl
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._d = OrderedDict()        # key -> (expire_at, value, weight)
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key, default=_MISS):
        with self._lock:
            e = self._d.get(key)
            if e is None:
                return default
            if time.time() >= e[0]:
                self._drop(key)
                return default
            self._d.move_to_end(key)
            return e[1]

    def put(self, key, value, weight=0, ttl=None):
        with self._lock:
            if key in self._d:
                self._drop(key)
            self._d[key] = (time.time() + (self._ttl if ttl is None else ttl), value, weight)
            self._bytes += weight
            while self._d and (len(self._d) > self._max_items
                               or (self._max_bytes and self._bytes > self._max_bytes)):
                self._drop(next(iter(self._d)))     # 淘汰最久未使用的一条

    def _drop(self, key):
        e = self._d.pop(key, None)
        if e:
            self._bytes -= e[2]

    def stats(self):
        with self._lock:
            return len(self._d), self._bytes


_CACHE = _TTLCache(CACHE_TTL, DOC_CACHE_MAX_ITEMS, DOC_CACHE_MAX_BYTES)   # 文档/RSS
_IMG_CACHE = _TTLCache(IMG_MEM_TTL, IMG_CACHE_MAX_ITEMS, IMG_CACHE_MAX_BYTES)   # 完整图片本体
# 【2026-10-09 修 阻断-2】测尺寸只看文件头，这类「探测用片段」单独一个缓存，
# 绝不与 _IMG_CACHE 里的完整图混放（键、缓存对象都分开）。
_PROBE_CACHE = _TTLCache(IMG_MEM_TTL, IMG_CACHE_MAX_ITEMS, 8 * 1024 * 1024)
PRELOAD = {"cn": [], "intl": [], "hot": []}   # 启动时预热好的条目，API 直接取用
# 【2026-10-09 修 中-7】「低质图后台剔除」的单飞闸：同一 region 同时只允许一轮，
# 并且写回 PRELOAD 前要比对「还是不是我开始时那一批」（防丢失更新，见 _enrich_quality_bg）。
_ENRICH_LOCK = threading.Lock()
_ENRICHING = set()
# 「当天」过滤之前的完整池子（按各源窗口，含最近几天）。
# 主列表只给当天（用户原则：常看常新、绝不看旧新闻），
# 但**搜索必须能搜到更早的新闻** —— 程序名叫「热点新闻检索」，这是它的本分。
PRELOAD_ALL = {"cn": [], "intl": [], "hot": []}


def _load_config():
    default = {
        # 【2026-10-09 升级体验口径】
        #   enabled     —— 总开关：关掉就完全不再检查更新
        #   auto_update —— **默认开**：发现新版本就静默下载 + 验签 + 自动替换（不弹确认框）。
        #                   关掉后回到"只提示、不自动升级"，由用户自己在页面点「检查更新」。
        "update": {"enabled": True, "auto_update": True},
        # contact：MyMemory 用邮箱标识身份，带上可把免费额度从 5k/天 提到 50k/天。
        # 【2026-10-07 改动】原来写死的是假邮箱（kele551@example.com），既拿不到提额，
        # 又会被一起打进 exe 分发出去。
        # 现在怎么填真实邮箱：**不要**改仓库里的 config.json（那会进版本库、公开仓库里
        # 就露出私人邮箱了），而是写本机私有覆盖文件（见下面 _LOCAL_CONFIG）。
        "translate": {"backend": "mymemory", "contact": "",
                      "baidu": {"appid": "", "secret": ""}, "deepl": {"auth_key": ""}, "enabled": True},
    }
    # 配置读取顺序（后者覆盖前者）：
    #   1) 程序自带/同级的 config.json（在版本库里，**不要往这里写私人信息**）
    #   2) %LOCALAPPDATA%\hotnews\config.local.json（本机私有，不进版本库、不随程序分发）
    # 有了第 2 个，提额度、换翻译后端都不必改代码、也不会把邮箱提交到公开仓库。
    data = {}
    for path in (_res("config.json"), _LOCAL_CONFIG):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
        except Exception:
            continue
        if not isinstance(cfg, dict):
            continue
        for k, v in cfg.items():
            if isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k].update(v)
            else:
                data[k] = v
    data.setdefault("update", dict(default["update"]))
    data["update"].setdefault("enabled", True)
    data["update"].setdefault("auto_update", True)
    data.setdefault("translate", dict(default["translate"]))
    for k, v in default["translate"].items():
        data["translate"].setdefault(k, v)
    return data


# 本机私有配置路径（不进版本库、不随程序分发）：
# 提额/换翻译后端写在这里，别改仓库里的 config.json。
_LOCAL_CONFIG = os.path.join(os.getenv("LOCALAPPDATA") or os.path.expanduser("~"),
                             "hotnews", "config.local.json")

CONFIG = _load_config()


# 共享 HTTP 连接池（2026-10-07 加速）
# 【为什么】原来 _fetch 每次调用都 `with httpx.Client(...)`，等于每个请求都重做
# 一次 TCP + TLS 握手。抓 30 条 HN、给热榜补 100 张图时，光握手就要几十秒。
# httpx.Client 官方明确支持跨线程复用，所以全局建一个即可（按 host 保活连接）。
# 【2026-10-09 修 中-6】上限从 48/24 提到 96/48：同时可能存在的抓取线程是
#   _hot_hn 30 + 完整/快速抓取 16~20 + 补图 14 + 后台测图 16，极端情况会超过 48 ——
#   httpx 拿不到连接就抛 PoolTimeout，被 _fetch 的 except 吞掉后**当成"该源抓取失败"**，
#   于是刷新高峰期偶发丢源、日志里只有一行"抓取失败"，很难定位。
#   现在两手一起做：① 上限与最大线程规模对齐；② _fetch 把 PoolTimeout 单独识别，
#   分开记日志、分开计数，并且给它多一次重试（它与"站点挂了"性质完全不同）。
_HTTP_LIMITS = httpx.Limits(max_connections=96, max_keepalive_connections=48,
                            keepalive_expiry=30.0)
_CLIENT = httpx.Client(follow_redirects=True, trust_env=False, timeout=10,
                       limits=_HTTP_LIMITS, headers={"User-Agent": UA})

# 【修 中-6】连接池排队超时的累计次数（与"源站真的失败"分开统计，便于事后定位）。
# 只用于诊断：/api/version 会带出去，日志里也会单独打一行。
_POOL_TIMEOUTS = [0]


# 文章页 → (题图, 发布时间, 站方栏目)。og:image 基本不会变，进程内记住，避免每轮刷新重抓一遍。
# 【2026-10-09 修 高-2】原来是个裸 dict，只增不减 —— 跑一天就是几千条，换成带容量上限的缓存。
_ART_CACHE = _TTLCache(7 * 86400, 4096)

# 只重试这几种「对方临时不舒服」的状态码；404/403 之类不重试（重试也没用，还多打一次站方）
_RETRY_CODES = frozenset({408, 429, 500, 502, 503, 504})


def _ctype_of(resp):
    """响应里的真实 content-type（去掉 charset 参数）；拿不到才按 JPEG 兜底。"""
    try:
        return (resp.headers.get("content-type") or "").split(";")[0].strip() or "image/jpeg"
    except Exception:
        return "image/jpeg"


def _fetch_failed(out, key):
    """这次抓取算不算失败（失败只写短负缓存，也不占容量预算）。"""
    if key == "img":
        return not (isinstance(out, tuple) and out and out[0])
    return not out


def _fetch(url, timeout=10, max_bytes=None, extra_headers=None):
    # 按「是否图片」决定返回形态：图片返回 (bytes, ctype) 元组；文档/RSS 返回原始字节。
    # 旧逻辑靠 URL 含 "rss" 判断文档，但小众软件(/feed/) 这类源不含
    # "rss" 会被误判成图片 → feedparser 收到元组崩溃。改为按图片扩展名判定，覆盖所有源。
    _low = url.lower()
    _is_img = _low.startswith(("http://", "https://")) and (
        _low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))
        or ".jpg?" in _low or ".png?" in _low or ".webp?" in _low or ".gif?" in _low
        or "/img/" in _low
    )
    key = "img" if _is_img else "doc"
    # 带了额外请求头（如 mefcl 的访问 cookie）时缓存键要区分开，
    # 否则先抓到的「校验页」会把后面带 cookie 的正确结果顶掉。
    _ck = url
    if extra_headers:
        _ck = url + "|" + ",".join(f"{k}={v}" for k, v in sorted(extra_headers.items()))
    # 【2026-10-09 修 阻断-2】max_bytes 是「只读前 N 字节」的探测请求（测图片尺寸、
    # 只取文章页 <head>），拿到的是**片段**，绝不能和「要完整本体」的请求共用缓存：
    #   · 图片：完整本体进 _IMG_CACHE，探测片段进 _PROBE_CACHE（连缓存对象都分开）；
    #   · 文档：完整正文用原键，片段加 "|head" 后缀。
    # 以前两者同键同缓存，_measure_image 留下的「前 256KB」会把 /img 要的完整图顶掉 ——
    # 于是所有超过 256KB 的题图在 24 小时里只显示上半张，正是用户看到的"半张图"。
    head = max_bytes is not None
    if key == "img":
        cache = _PROBE_CACHE if head else _IMG_CACHE
    else:
        cache = _CACHE
        if head:
            _ck += "|head"
    hit = cache.get(_ck)
    if hit is not _MISS:
        return hit
    # 图片：先做地址层面的拦截（裸 IP / 内网 / 回环 / 非 80·443 端口一律不取），
    # 免得 /img 被当成内网探针、把本机当开放代理用（修 高-1）。
    if key == "img":
        _why = _img_url_block_reason(url)
        if _why:
            print(f"[warn] 图片地址被拒（{_why}）: {url[:110]}")
            return (None, "")
    out, transient, truncated = None, False, False
    # 【2026-10-09 修 中-6】pool_wait 单独标记「这次失败是连接池排队超时」：
    # 它不是源站故障，重试一次往往就好，也**不应该**写进负缓存（否则等于把
    # 自己的并发问题记成"这个源坏了"，后面十几秒都不再试）。
    pool_wait = False
    for attempt in (0, 1, 2):
        # 【2026-10-09】truncated 必须每次尝试都归零：否则上一次尝试的 True 会残留到重试，
        # 让「这次其实读完了整张图」的小图被误判成片段（不进 _IMG_CACHE，白丢一次缓存机会）。
        out, transient, truncated, pool_wait = None, False, False, False
        try:
            # trust_env=False：不走系统/沙箱代理，本机直连国内站点更快更稳
            hdrs = {}
            if key == "img":
                # 来源站点信息：部分图片 CDN 会校验它（站点访问策略），按站方要求带上
                try:
                    hdrs["Referer"] = urllib.parse.urlsplit(url).netloc
                except Exception:
                    pass
            if extra_headers:
                hdrs.update(extra_headers)
            if head:
                # 【加速】只读前 N 字节就断开：og:image 在 <head>、图片尺寸在头部几十字节里，
                # 整页下载纯属浪费（新闻页常有 100~500KB）。实测这是补图环节最大的一笔开销。
                buf, ctype = bytearray(), ""
                blocked = False
                with _CLIENT.stream("GET", url, timeout=timeout, headers=hdrs) as r:
                    if r.status_code in _RETRY_CODES:
                        transient = True
                    elif r.status_code == 200:
                        # 跟随重定向后落到内网地址 → 当抓取失败，绝不把内容带回去
                        if key == "img" and str(r.url) != url and _img_url_block_reason(str(r.url)):
                            blocked = True
                        ctype = _ctype_of(r)
                        for chunk in r.iter_bytes(16384):
                            buf.extend(chunk)
                            if len(buf) >= max_bytes:
                                break
                truncated = len(buf) >= max_bytes
                data = bytes(buf) if (buf and not blocked) else None
                # 真实 content-type 要留下：原来这里写死 image/jpeg，PNG/WebP 会被报成 JPEG
                out = (data, ctype or "image/jpeg") if data else (None, "")
                if key != "img":
                    out = data
            else:
                r = _CLIENT.get(url, timeout=timeout, headers=hdrs)
                if r.status_code in _RETRY_CODES:
                    transient = True
                if key == "img":
                    if r.status_code != 200 or (str(r.url) != url
                                                and _img_url_block_reason(str(r.url))):
                        out = (None, "")
                    else:
                        out = (r.content, _ctype_of(r))
                else:
                    # 必须返回原始字节 r.content，而不是 r.text：
                    # httpx 的 r.text 会用它猜的编码（常误判 GBK 源为 utf-8）解码成字符串，
                    # 一旦源字节不是合法 utf-8（如中关村在线 RSS 是 GBK），标题就被换成 U+FFFD 乱码。
                    # 交给 feedparser.parse(bytes) 按各源 XML 声明的编码自行解码，才能正确还原中文。
                    out = r.content if r.status_code == 200 else None
        except httpx.PoolTimeout:
            # 【修 中-6】连接池排队等不到连接。这是**我们自己的并发**问题，不是源站故障：
            # 单独计数 + 单独打日志（日志里出现这行说明该调池子，而不是"某源坏了"）。
            out, transient, pool_wait = None, True, True
            print(f"[warn] 连接池排队超时（非源站故障，第 {attempt + 1}/3 次尝试）: {url[:110]}")
        except Exception:
            out, transient = None, True
        # 【2026-10-09 修 高-3】偶发抖动（超时/连接错误/5xx）给一次带退避的重试：
        # 以前源抓取、_article_meta、_measure_image 全是单次尝试，抖一下这轮就没了。
        # 【修 中-6】池排队超时多给一次机会（共 3 次），它比网络抖动更容易靠等待恢复。
        _tries = 2 if pool_wait else 1
        if transient and attempt < _tries:
            time.sleep(RETRY_BACKOFF * (attempt + 1))
            continue
        break
    if pool_wait:
        # 只统计「真的因为排队超时而没抓到」的次数（重试成功的不算），
        # 这样 /api/version 里的 poolTimeouts 才是"丢了几次"而不是"试了几次"。
        _POOL_TIMEOUTS[0] += 1
    if _fetch_failed(out, key):
        if pool_wait:
            # 池超时不写负缓存：不然下一次调用会命中"失败缓存"直接返回 None，
            # 本来只是排队，结果被记成"这个源抓不到"（正是中-6 说的静默丢条目）。
            return out
        # 【修 高-3】失败只写很短的负缓存（防同一瞬间重复打点），不再长期负缓存：
        # 以前文档失败要哑 5 分钟、图片 404 要哑一整天，难怪"栏目偶尔少一块"。
        cache.put(_ck, out, 0, ttl=IMG_FAIL_CACHE_TTL if key == "img" else FAIL_CACHE_TTL)
        return out
    weight = len(out[0]) if key == "img" else len(out or b"")
    if key == "img" and weight > IMG_ONE_MAX_BYTES:
        return out          # 单张大图不进内存缓存（浏览器那份 Cache-Control 仍缓存一天）
    if key == "img" and head and not truncated:
        # 探测时就把整张图读完了（小图）→ 直接当完整图存，后面 /img 命中它，少下一次
        _IMG_CACHE.put(_ck, out, weight)
    else:
        cache.put(_ck, out, weight)
    return out


# ============ 图片代理的来源白名单（2026-10-09 新增，修 高-1）============
# 背景：/img 原来只校验「以 http(s):// 开头」就直连，等于给任意网页一个内网探针：
#   <img src="http://127.0.0.1:8000/img?url=http://192.168.1.1/">
# 靠 onload/onerror 就能扫内网端口；按 README 用 `python server.py` 启动时绑的是
# 0.0.0.0，同局域网的人还能把本机当开放代理读回响应体。
# 现在两道闸：
#   1) 地址层面：只允许 http/https、不带用户名密码、端口只能 80/443，域名不能是裸 IP，
#      且域名解析出来的地址必须全是公网地址（私网/回环/链路本地/保留段一律拒绝）；
#   2) 域名层面：只代理「登记源站的域名」和「当前正在服务的条目里出现过的图片域名」——
#      后者由 _note_item_hosts() 在每次下发新闻列表时登记，等于自动跟随源站换 CDN，
#      而外部页面塞进来的域名永远进不了这个集合。
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_DNS_CACHE = {}                 # host -> (ts, (ip, ...))，只给图片白名单用
_DNS_CACHE_TTL = 300
_IMG_HOSTS_SEEN = set()         # 见过（＝我们自己的条目里出现过）的图片域名
_IMG_HOSTS_LOCK = threading.Lock()


def _parent_domain(host):
    """取站点域名：img.ithome.com → ithome.com；www.news.cn → news.cn。
    带二级后缀的（com.cn / co.uk …）多留一段，免得把两个不同站点认成一家。"""
    parts = [x for x in (host or "").lower().split(".") if x]
    if len(parts) >= 3 and ".".join(parts[-2:]) in (
            "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "com.hk", "com.tw",
            "co.uk", "co.jp", "com.au", "co.kr"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else ".".join(parts)


# 登记源站的域名（SOURCES 里 base/url 的站点域名）——白名单的静态部分
_SRC_DOMAINS = set()
for _rg in SOURCES.values():
    for _s in _rg:
        for _u in (_s.get("base"), _s.get("url")):
            try:
                _h = urllib.parse.urlsplit(_u or "").hostname
            except Exception:
                _h = None
            if _h:
                _SRC_DOMAINS.add(_parent_domain(_h))


def _host_ips(host):
    """解析域名（结果缓存 5 分钟）；解析不出来返回 ()，调用方按 fail-closed 处理。"""
    now = time.time()
    hit = _DNS_CACHE.get(host)
    if hit and (now - hit[0]) < _DNS_CACHE_TTL:
        return hit[1]
    ips = ()
    try:
        ips = tuple({i[4][0] for i in socket.getaddrinfo(host, None)})
    except Exception:
        ips = ()
    if len(_DNS_CACHE) > 512:
        _DNS_CACHE.clear()
    _DNS_CACHE[host] = (now, ips)
    return ips


def _ip_is_public(s):
    """是不是公网地址：私网/回环/链路本地/组播/保留/未指定 一律不算。"""
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped          # ::ffff:127.0.0.1 这类要还原成 IPv4 再看
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    return bool(ip.is_global)


def _img_url_block_reason(url):
    """地址层面的拦截，返回拒绝原因；None 表示这个地址可以取。"""
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:
        return "地址解析失败"
    if p.scheme not in ("http", "https"):
        return "协议不允许"
    if p.username or p.password:
        return "地址里带凭据"
    try:
        port = p.port
    except ValueError:
        return "端口不合法"
    if port not in (None, 80, 443):
        return "端口不常规"
    host = (p.hostname or "").strip().lower().rstrip(".")
    if not host:
        return "缺少主机名"
    try:
        ipaddress.ip_address(host)
        return "不接受 IP 地址"          # 图片都在域名上，裸 IP 直接不代理
    except ValueError:
        pass
    ips = _host_ips(host)
    if not ips:
        return "域名解析失败"
    for s in ips:
        if not _ip_is_public(s):
            return "目标是内网地址"
    return None


def _note_item_hosts(items):
    """把「我们正在下发的条目里的图片域名」登记进白名单。
    /img 只代理这些域名 + SOURCES 登记源站的域名，别的域名一概 403。"""
    got = set()
    for it in items or []:
        try:
            h = urllib.parse.urlsplit(it.get("image") or "").hostname
        except Exception:
            h = None
        if h:
            got.add(h.lower().rstrip("."))
    if not got:
        return
    with _IMG_HOSTS_LOCK:
        _IMG_HOSTS_SEEN.update(got)


def _img_host_allowed(host):
    """域名是否在白名单里（登记源站域名，或我们自己的条目里出现过的图片域名）。"""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return False
    with _IMG_HOSTS_LOCK:
        if host in _IMG_HOSTS_SEEN:
            return True
    d = _parent_domain(host)
    return any(d == x or host.endswith("." + x) for x in _SRC_DOMAINS)


# 畸形协议前缀：https:https://x  或  https://https://x（RSS 里真实存在）
_URL_FIX = re.compile(r"^https?:/*(?=https?://)", re.I)
# URL 里的日期路径，用于识别站方写死的默认图
_IMG_DATE_PAT = (
    re.compile(r"/(20\d{2})[/\-]?(\d{2})[/\-]?(\d{2})/"),
    re.compile(r"/(20\d{2})[/\-](\d{1,2})/"),
)
STALE_IMG_DAYS = 180      # 图比文章旧半年以上 → 判定为站方占位图，宁缺毋滥
DUP_IMG_MIN = 3           # 同一源内被 3 条以上共用的图 → 判定为默认图
# 题图短边门槛（用户要求「每条新闻必须有高清大图」，2026-10-07 由 200 提到 300）。
# 定 300 的依据是实测：300 能保住界面新闻(580x330)、小众软件多数图；
# 提到 400 会把界面新闻整个板块砍掉（它源站上限就是 580x330），故取 300。
MIN_IMG_SHORT_SIDE = 300


def _norm_url(c, base):
    """归一化图片地址：剥畸形协议前缀、补全协议相对地址与相对路径"""
    c = html.unescape(c or "").strip().strip("\"'")
    if not c or c.startswith(("data:", "javascript:", "#")):
        return None
    while _URL_FIX.match(c):
        c = _URL_FIX.sub("", c, count=1)
    if c.startswith("//"):
        return "https:" + c
    if c.startswith(("http://", "https://")):
        return c
    if c.startswith("/"):
        return base.rstrip("/") + c
    return base.rstrip("/") + "/" + c.lstrip("/")


def _img_candidates(entry, base):
    """列出条目里所有候选题图（已归一化为绝对 URL，按可信度排序）"""
    cands = []

    def add(u):
        u = _norm_url(u, base)
        if u and u not in cands:
            cands.append(u)

    for m in entry.get("media_content") or []:
        if isinstance(m, dict):
            add(m.get("url"))
    t = entry.get("media_thumbnail")
    if isinstance(t, list) and t and isinstance(t[0], dict):
        add(t[0].get("url"))
    for e in entry.get("enclosures") or []:
        if isinstance(e, dict) and str(e.get("type", "")).startswith("image"):
            add(e.get("url"))
    im = entry.get("images")
    if isinstance(im, list) and im and isinstance(im[0], dict):
        add(im[0].get("url"))
    # summary / description 正文里的 img
    raw = entry.get("summary") or entry.get("description") or ""
    # 部分源（如小众软件）把题图放在 <content> 全文而非 summary，需一并扫描
    for c in (entry.get("content") or []):
        if isinstance(c, dict):
            raw = raw + "\n" + (c.get("value") or "")
    for u in re.findall(r"<img[^>]+src=[\"']([^\"']+)[\"']", raw, re.I):
        add(u)
    for u in re.findall(r'(?:src|href)=["\']([^"\']+\.(?:jpg|jpeg|png|webp|gif)(?:\?[^"\']*)?)["\']', raw, re.I):
        add(u)
    return cands


def _img_is_stale(url, pub_ts):
    """
    有些站的 RSS 把图片 URL 写死成站方默认图（日期是几年前的），配今天的新闻
    属于张冠李戴。按 URL 里的日期路径判断，比配错图强的是不配图。
    """
    if not url or not pub_ts:
        return False
    for i, pat in enumerate(_IMG_DATE_PAT):
        m = pat.search(url)
        if not m:
            continue
        g = m.groups()
        try:
            y, mo, d = int(g[0]), int(g[1]), (1 if i else int(g[2]))
            if not (2000 <= y <= 2100 and 1 <= mo <= 12):
                return False
            img_day = datetime(y, mo, d, tzinfo=CST).date()
        except ValueError:
            return False
        pub_day = datetime.fromtimestamp(pub_ts, CST).date()
        return abs((pub_day - img_day).days) > STALE_IMG_DAYS
    return False


def _image_dimensions(data):
    """
    不依赖 PIL，直接从图片字节头解析宽高（PNG/JPEG/GIF/BMP/WEBP）。
    返回 (w, h) 或 None（格式不可识别则保留条目，不去误杀好新闻）。
    打包时 PIL 被排除出 exe，这里纯解析头字节，零额外依赖。
    """
    if not data or len(data) < 24:
        return None
    sig = data[:8]
    if sig[:8] == b"\x89PNG\r\n\x1a\n":                 # PNG
        import struct
        return struct.unpack(">II", data[16:24])
    if sig[:6] in (b"GIF87a", b"GIF89a"):              # GIF
        import struct
        return struct.unpack("<HH", data[6:10])
    if sig[:2] == b"BM" and len(data) >= 26:          # BMP
        import struct
        w, h = struct.unpack("<ii", data[18:26])
        return (abs(w), abs(h))
    if sig[:4] == b"RIFF" and data[8:12] == b"WEBP":   # WEBP
        import struct
        fmt = data[12:16]
        if fmt == b"VP8 " and len(data) >= 30:
            w = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return (w, h)
        if fmt == b"VP8L" and len(data) >= 25:
            b = struct.unpack("<I", data[21:25])[0]
            return ((b & 0x3FFFFF) + 1, ((b >> 22) & 0x3FFF) + 1)
        if fmt == b"VP8X" and len(data) >= 30:
            w = struct.unpack("<I", data[24:27] + b"\x00")[0] + 1
            h = struct.unpack("<I", data[27:30] + b"\x00")[0] + 1
            return (w, h)
        return None
    if sig[:2] == b"\xff\xd8":                         # JPEG
        import struct
        i, n = 2, len(data)
        while i < n - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return (w, h)
            if i + 4 >= n:
                break
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if seg == 0:
                break
            i += 2 + seg
        return None
    return None


def _measure_image(url):
    """测题图真实尺寸；失败/非图返回 None（调用方据此保留条目，不因网络误杀好新闻）。

    【加速 2026-10-07】只读前 IMG_HEAD_BYTES 字节：JPEG/PNG/WEBP 的尺寸信息都在
    文件头部，整张图（常 100KB~1.5MB）根本不用下完。测不出则返回 None，_img_ok 保守放行。

    【2026-10-09 修 阻断-2】这里拿到的是**片段**，_fetch 会把它存进专门的 _PROBE_CACHE，
    绝不会再顶掉 /img 要的完整图（以前两者同一个缓存键，大图因此只显示上半张）。
    """
    try:
        res = _fetch(url, timeout=6, max_bytes=IMG_HEAD_BYTES)
    except Exception:
        return None
    if not res:
        return None
    data = res[0] if isinstance(res, tuple) else res
    if not data:
        return None
    return _image_dimensions(data)


def _img_ok(url):
    """题图是否达标：测不出（网络/格式）→ 保留；短边低于阈值 → 低质，剔除"""
    s = _measure_image(url)
    if s is None:
        return True
    return min(s) >= MIN_IMG_SHORT_SIDE


def _clean(text):
    if not text:
        return ""
    s = re.sub(r"<br\s*/?>|</p>|</div>", " ", text)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ============ 【2026-10-09 换源】中文源的编码兜底 ============
# 中新网系等中文源，正常情况都在 XML 声明里写对了 encoding="utf-8"，实测 24 个 feed
# 的字节也确实是合法 utf-8（见 logs\换源-境内国际版-20261009.md 的编码核对）。
# 但中文站历史上常有「声明 utf-8、实际 GBK/GD18030」的情况，一旦发生，
# feedparser 会按声明解出满屏 U+FFFD 乱码 —— 标题全废。这里做两层防护：
#   ① 我们自己按文本处理时（_decode_feed_bytes）：声明 → utf-8 → gb18030 依次回退；
#   ② 交给 feedparser 的场合（_parse_one 一律传**原始字节**，让它按各源声明解码），
#      解析完只在「确实出现 U+FFFD 且原始字节不是合法 utf-8」时按 gb18030 重解一次。
# 两条都只在异常时触发，正常源一次都不会走到。
_XML_DECL_ENC = re.compile(rb'(<\?xml[^>]*?encoding\s*=\s*["\'])([^"\']+)(["\'])', re.I)


def _decode_feed_bytes(raw):
    """把 feed / 页面的原始字节解成文本：按 XML 声明 → utf-8 → gb18030 依次回退。"""
    if isinstance(raw, str):
        return raw
    if not raw:
        return ""
    encs = []
    m = _XML_DECL_ENC.search(raw[:400])
    if m:
        encs.append(m.group(2).decode("ascii", "ignore"))
    encs += ["utf-8", "gb18030"]
    for e in encs:
        try:
            return raw.decode(e)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


# ============ 【2026-10-09 换源】条目发布时间：按字符串自己写的时区解析 ============
# feedparser 会把时间**换算成 UTC** 再放进 `*_parsed`，这带来一个坑（实测 16 个 RSS 源）：
#   · 写了时区的（`+0800` / `GMT`，实测 15 个源如此）→ 它换算得对，
#     但如果再按东八区解释一次，新闻时间就**早了 8 小时**；
#   · 没写时区的（实测快科技的 `2026-10-09 12:25:39`）→ 它只能当成 UTC，
#     这时按 UTC 解释反而**晚了 8 小时**（页面上显示成未来时间）。
# 两种源混在一起，统一按某一种解释必有一半是错的 → 以**原始字符串**为准：
# 写了时区就用它写的，没写就按东八区（本程序与所有源的口径都是北京时间）。
_DATE_FORMATS = (
    "%a, %d %b %Y %H:%M:%S", "%a, %d %b %Y %H:%M", "%d %b %Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
)
_TZ_OFF_RE = re.compile(r'([+-])(\d{2}):?(\d{2})$')
# ⚠ `Z` 必须单独一支：ISO-8601 的 `2026-10-09T12:25:39Z` 里 Z 紧贴数字，
# 两侧都是 \w，`\b…Z$` 反而匹配不上（复查发现并修掉）。
_TZ_UTC_RE = re.compile(r'(?:\b(?:UT|UTC|GMT)|Z)$', re.I)


def _ts_from_date_str(raw):
    """按字符串自己写的时区解析发布时间；写不出时区就按东八区。解析不了返回 0。"""
    s = (raw or "").strip()
    if not s:
        return 0
    # 尾巴上的时区名（"(CST)" / "[UTC]"）先去掉，免得挡住下面的格式匹配
    s = re.sub(r'[\(\[][^\)\]]{0,20}[\)\]]\s*$', '', s).strip()
    m = _TZ_OFF_RE.search(s)
    if m:
        mins = int(m.group(2)) * 60 + int(m.group(3))
        tz = timezone(timedelta(minutes=(-mins if m.group(1) == "-" else mins)))
        s = s[:m.start()].strip()
    elif _TZ_UTC_RE.search(s):
        tz = timezone.utc
        s = _TZ_UTC_RE.sub("", s).strip()
    else:
        tz = CST
    s = re.sub(r'\s+', ' ', s).strip().rstrip(",").strip()
    for fmt in _DATE_FORMATS:
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=tz).timestamp())
        except ValueError:
            continue
    return 0


def _entry_ts(e):
    """RSS / Atom 条目的发布时间 → 秒级时间戳（见上面那段说明）。"""
    ts = _ts_from_date_str(e.get("published") or e.get("updated") or "")
    if ts:
        return ts
    # 字符串实在解析不出来（少见格式）→ 退回 feedparser 的 UTC 语义
    p = e.get("published_parsed") or e.get("updated_parsed")
    if p:
        try:
            return int(datetime(*p[:6], tzinfo=timezone.utc).timestamp())
        except Exception:
            return 0
    return 0


def _refeed_if_mojibake(raw, doc):
    """feedparser 解出乱码时的兜底重解；没必要返回 None。

    触发与接受条件都刻意保守（正常源一次都不会走到）：
      · **触发**：原始字节不是合法 utf-8（说明声明的编码确实不对），
        并且解出来的标题要么带替换字符 U+FFFD、要么**一个汉字都没有**。
        为什么要看「有没有汉字」：feedparser 在声明解码失败时会退回 windows-1252，
        实测解出来是「´íÉùÃ÷±àÂëµÄÖÐÎÄÔ´」这种西欧重音字母，**根本没有 U+FFFD**，
        只按 U+FFFD 判断会漏掉真正的乱码。
      · **接受**：按 gb18030 重解后确实解出了汉字才替换 ——
        纯英文源（前后都没有汉字）永远不会被误改。
    回退顺序 gb18030 → utf-8；重解前把 XML 声明改写成 utf-8 再交给 feedparser。
    """
    if not raw or isinstance(raw, str):
        return None
    try:
        raw.decode("utf-8")
        return None                      # 本来就是合法 utf-8 → 不乱动
    except UnicodeDecodeError:
        pass
    _titles = [(e.get("title") or "") for e in (getattr(doc, "entries", None) or [])]
    _has_fffd = any("\ufffd" in t for t in _titles)
    if not (_has_fffd or not any(_has_cjk(t) for t in _titles)):
        return None                      # 已经解出中文了 → 不乱动
    txt = ""
    for e in ("gb18030", "utf-8"):
        try:
            txt = raw.decode(e)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    if not txt:
        return None
    fixed = txt.encode("utf-8")
    if _XML_DECL_ENC.search(fixed[:400]):
        fixed = _XML_DECL_ENC.sub(rb"\g<1>utf-8\g<3>", fixed, count=1)
    try:
        new = feedparser.parse(fixed)
    except Exception:
        return None
    _new_titles = [(e.get("title") or "") for e in (getattr(new, "entries", None) or [])]
    if any(_has_cjk(t) for t in _new_titles):
        return new
    return None


def _parse_one(src):
    # 凤凰娱乐：首页内联 JSON（newsstream），非 RSS，走专用解析
    if src.get("parser") == "ifeng":
        return _parse_ifeng(src)
    if src.get("parser") == "thepaper":
        return _parse_thepaper(src)
    if src.get("parser") == "mefcl":
        return _parse_mefcl(src)
    # 新浪娱乐：首页 HTML 列表（无 RSS 可用，实测网易/中新网/时光网的娱乐 RSS 全废）
    if src.get("parser") == "sina_ent":
        return _parse_sina_ent(src)
    # 【2026-10-09 换源】国际在线娱乐：列表页 SSR 卡片（标题+原图+时间都在 HTML 里）
    if src.get("parser") == "cri_ent":
        return _parse_cri_ent(src)
    # 【2026-10-09 修 低-6 删除死代码】这里原来还有一个 `parser == "github_readme"` 分支
    # （抓 GitHub 上 awesome 列表仓库的 README 做成软件卡片）。SOURCES 里没有任何源用它
    # —— 用户 2026-10-07 已明确要求「删除不易读的英文 GitHub 仓库，改用国内源」，
    # 所以这个分支和它专用的 `_parse_github_readme` / `_clean_md` 一起删掉了。
    # 逻辑留痕在 git 历史里，需要时按 commit 取回。
    raw = _fetch(src["url"])
    if not raw:
        print(f"[warn] RSS 抓取失败: {src['url']}")
        return []
    try:
        doc = feedparser.parse(raw)
    except Exception as ex:
        print(f"[warn] RSS 解析失败: {src['url']} ({type(ex).__name__})")
        return []
    # 【2026-10-09 换源】中文源编码兜底：声明与实际字节不符时按 gb18030 重解一次
    _fixed = _refeed_if_mojibake(raw, doc)
    if _fixed is not None:
        print(f"[ok] {src['id']}: XML 声明编码与实际不符，已按 gb18030 回退重解")
        doc = _fixed
    items = []
    max_age = (src.get("max_age_hours")
               or CLS_MAX_AGE_HOURS.get(src.get("cls", ""))
               or MAX_AGE_HOURS.get(src.get("region", "cn"), 24))
    cutoff = datetime.now(CST).timestamp() - max_age * 3600
    dropped = 0
    for e in doc.entries[:24]:
        title = _clean(e.get("title"))
        if not title:
            continue
        desc = _clean(e.get("summary") or e.get("description") or "")
        # 去 emoji 短码（:book: 这类）与「查看全文/阅读原文」尾巴，卡片摘要更干净
        desc = re.sub(r":[a-z_]{2,20}:", "", desc)
        desc = re.sub(r"(查看全文|阅读原文|查看原文)\s*$", "", desc).strip()
        desc = desc[:220]
        # 【2026-10-09 换源·自行发现的时区 bug】原来这里写成
        # `datetime(*pub[:6], tzinfo=CST)`：feedparser 的 *_parsed 已经是 **UTC**，
        # 再按东八区解释一次，等于把每条 RSS 新闻的时间**提前 8 小时**，后果很实在：
        #   · 当天 00:00~08:00 发布的新闻会被标成前一天 → 被「只留当天」整个滤掉
        #     （早上打开看到的是昨天的新闻）；
        #   · 工具栏「最新 时间」显示偏早 8 小时；与热榜/文章页取到的时间混排时顺序也不对。
        # 现在统一交给 _entry_ts：**按 feed 字符串自己写的时区**解析
        #（写了 +0800/GMT 就用它，没写时区按东八区），两种源都对得上。
        ts = _entry_ts(e)
        if ts and ts < cutoff:
            dropped += 1
            continue
        link = e.get("link") or ""
        # 多个候选中挑第一张"日期说得通"的，全不合格就留空（好过配错图）
        img = ""
        for c in _img_candidates(e, src["base"]):
            if not _img_is_stale(c, ts):
                img = c
                break
        items.append({
            "id": hashlib.md5((src["id"] + title).encode("utf-8")).hexdigest()[:12],
            "region": src["region"],
            "channel": src["id"],
            "label": src.get("label", src["id"]),
            "cls": src["cls"],
            "title": title,
            "desc": desc,
            "link": link,
            "image": img,
            "published": ts,
            "translated": False,
        })
    if dropped:
        print(f"[ok] {src['id']}: 保留 {len(items)} 条，滤除 {dropped} 条过期内容")
    return items


# ============ 实时热榜：多平台聚合（澎湃 / 红星 / 凤凰）============
# 热榜要成立，前提是「多家 + 每条都能点进原文」。现役三家均由用户指定：
#   澎湃新闻  hotNews 数组，带站方真实互动数 interactionNum
#   红星新闻  官网首页 SSR 推荐流（该站无 RSS，/rss.xml 404）
#   凤凰网    官网首页 newsstream[] 内联 JSON（凤凰 RSS 全线 404）
# 三家的点击目标都是news 正文页，不存在「只有热词、点开是自己拼的搜索页」的问题。
# 前端置顶、按平台分组展示。各平台解析互相独立，任一失败不影响其它平台。

# 【已按用户要求下线的热榜源，勿再加回】
#   今日头条：接口只返回一个站内加权 HotValue，用户在卡片上看到的数字点进去对不上，
#             显示给用户就是个假数。用户两次明确提出去掉（2026-10-06 删除）。
#   百度     ：用户明确不要。
#   B站      ：内容全是视频，不是新闻列表。
#   微博     ：只有热搜词、没有正文，跳转链接是自己拼的搜索页；用户要求去掉（2026-10-06）。
#   腾讯新闻：原计划接入，但热榜接口已全部失效——i.news.qq.com 的 getSubList /
#             getHotRankList / getRankList 均 404， news.qq.com/rank/ 首页也跳到了 404
#             公益页。故由凤凰网顶替。


# 【2026-10-09 修 低-6 删除死代码】这里原来还有两个热榜解析器，**没有任何调用方**：
#   `_hot_baidu()`     —— 百度实时热点（上面已写"用户明确不要"）
#   `_hot_bilibili()`  —— B站热门（内容全是视频，不是新闻列表）
# 留着会让读代码的人以为"程序支持百度/B站热榜"，与 README 的数据源清单不符。
# 两个函数的逻辑已完整留痕在 git 历史（提交 1276afb 之前的那一版），需要时按 commit 取回。
# 上面那段"已下线、勿再加回"的说明**保留**，它才是真正有用的信息。


def _hot_cn_intl():
    """中新社·国际（chinanews.com.cn/rss/world.xml）——国际热榜的中文底座。

    为什么国际热榜必须有一家中文源：国际板块原来还有一堆英文外媒，
    要看得懂全靠后台机器翻译；而免费翻译额度一旦被限流，整个板块当场退回英文
    （实测某次 120 段只译出 2 段）。这条 RSS 本身就是中文国际新闻，
    不依赖任何翻译服务，保证「国际热榜」在任何情况下都看得懂。
    【2026-10-09 换源】从那以后国际板块**只剩境内媒体**（CGTN + 中新网 + 国产
    综合源转来的国际条目），这条更稳了。图片说明订正：中新网 RSS 不带图、文章页
    **也没有 og:image**，正文照片另有取法（见 _cn_body_image / _ART_SITE_IMG_RULES）；
    热榜是纯文本列表，本来也不需要图，所以这里保持 image=""。"""
    raw = _fetch("https://www.chinanews.com.cn/rss/world.xml")
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 中新社国际抓取失败")
        return []
    f = feedparser.parse(raw)      # 传原始字节，由 feedparser 按 XML 声明自解码
    out = []
    for idx, e in enumerate(f.entries[:30], 1):
        title = _clean(e.get("title") or "")
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        ts = _entry_ts(e)
        out.append({
            "id": hashlib.md5(("hot-cnworld-" + link).encode("utf-8")).hexdigest()[:12],
            "region": "intl", "channel": "cnworld-hot", "label": "中新社·国际",
            "cls": "热榜", "title": title, "desc": _clean(e.get("summary") or "")[:180],
            "link": link, "image": "", "published": ts,
            "heat": 0, "rank": idx, "score": (30 - idx + 1) / 30,
            # 本来就是中文 → 标记已翻译，后台翻译会跳过它（否则拿去「中译中」会被翻坏）
            "translated": True,
        })
    print(f"[ok] hot/中新社国际: {len(out)} 条")
    return out


# 澎湃 rightSidebar 的共享缓存（被 cn 要闻和热榜两个入口共用，见 _thepaper_data）
_THEPAPER_CACHE = {"ts": 0.0, "data": None}
_THEPAPER_LOCK = threading.Lock()


def _parse_hot_intl():
    """国际实时热榜 = 中新社·国际（中文，不依赖翻译，永远读得懂）
                   + 凤凰网 / 澎湃新闻 / 红星新闻 报的国际条目（中文）。

    【2026-10-09 换源·合规】原来这里还有一路境外热榜（其公开 API，
    逐条请求 Firebase 取分数与评论数）。按章程 §9.1「默认只用境内权威媒体源，
    不采用境外来源」，**连同 `_hot_hn()` 与它专用的 `_HN_CACHE` 一起整段删除**。
    删掉后国际热榜反而更快（少 30 次 Firebase 请求）也更好读（不再有英文条目）。

    两家来源互相独立，任一失败不影响另一家：
      - 中新社·国际每次都抓（RSS 轻快，实测 20~80ms）。中文条目已标记 translated=True，
        后台翻译会跳过，不会拿去「中译中」。
      - 国内综合源（凤凰/澎湃/红星）的国际条目排在后面，用来把热榜做厚。
    中新社排在前面，保证热榜靠前的部分一定是可读的中文。"""
    parts = []
    try:
        parts.extend(_hot_cn_intl())
    except Exception as ex:
        print(f"[warn] 国际热榜子源异常: {type(ex).__name__}")
    # 【2026-10-07 用户建议】「灵活一点，国际板块的热榜也可以用凤凰、澎湃、红星的
    # 国际新闻来体现」—— 国内热榜里被剔掉的国际新闻**不丢**，转来充实国际热榜。
    # 好处：删掉那处境外热榜之后，国际热榜仍由境内多家媒体撑起来，且全是中文，不用等翻译。
    try:
        # 优先用 _collect("hot") 里已经抓好文章页、按站方栏目分好类的那批；
        # 没有时退回关键词判定（首次启动国内还没抓完的情况）。
        _intl_from_cn = list(_HOT_INTL_SPILL) or _split_hot_by_scope(_parse_hot())[1]
        if _intl_from_cn:
            _seen_p = {p.get("title") for p in parts}
            _add = [x for x in _intl_from_cn if x.get("title") not in _seen_p]
            if _add:
                print(f"[ok] hot-intl: 并入国内综合源的国际新闻 {len(_add)} 条")
                parts.extend(_add)
    except Exception as ex:
        print(f"[warn] 并入国内源国际新闻失败: {type(ex).__name__}")
    if not parts:
        return parts
    # 与国内热榜一致：按平台(label)内归一化百分位跨平台混合排序
    groups = {}
    for it in parts:
        groups.setdefault(it.get("label") or "", []).append(it)
    for group in groups.values():
        if any(x.get("heat") for x in group):
            group.sort(key=lambda x: x.get("heat") or 0, reverse=True)
        else:
            group.sort(key=lambda x: x.get("rank") or 999)
            for it in group:
                it["heat"] = 0
        n = max(1, len(group))
        for i, it in enumerate(group, 1):
            it["score"] = (n - i + 1) / n
    parts.sort(key=lambda x: x.get("score") or 0, reverse=True)
    print(f"[ok] hot-intl: 多平台聚合共 {len(parts)} 条（百分位混排）")
    return parts


def _thepaper_data(ttl=120):
    """澎湃首页侧栏 JSON（要闻 / 财经投資 / 编辑精选 / hotNews 全在这一个响应里）。

    做成缓存是因为它**同时被两个入口消费**：国内栏目的 cn-thepaper（要闻）和热榜的
    hot-thepaper。两者由 ThreadPoolExecutor 并发抓取，若各自发一次请求，实测第二发会被
    站方限速到 12s+（连着请求两次必有一发慢到超时边缘），整个 cn 刷新会被拖住。
    故这里用锁 + 短 TTL 保证「一次刷新只抓一次」，两个入口共享同一份数据。"""
    now = time.time()
    if _THEPAPER_CACHE["data"] and now - _THEPAPER_CACHE["ts"] < ttl:
        return _THEPAPER_CACHE["data"]
    with _THEPAPER_LOCK:
        now = time.time()
        if _THEPAPER_CACHE["data"] and now - _THEPAPER_CACHE["ts"] < ttl:
            return _THEPAPER_CACHE["data"]
        try:
            with httpx.Client(timeout=10, trust_env=False, headers={"User-Agent": UA}) as c:
                r = c.get("https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar")
            data = (r.json() if r.status_code == 200 else {}).get("data") or {}
        except Exception as ex:
            print(f"[warn] 澎湃抓取异常: {type(ex).__name__}")
            return _THEPAPER_CACHE["data"] or {}
        if data:
            _THEPAPER_CACHE["ts"], _THEPAPER_CACHE["data"] = now, data
        return _THEPAPER_CACHE["data"] or {}


def _ms_to_ts(v):
    """把毫秒时间戳（字符串/数字）转成秒级时间戳；不合法返回 0。"""
    try:
        n = int(str(v).strip())
    except Exception:
        return 0
    if n > 10 ** 12:            # 毫秒 → 秒
        n //= 1000
    # 合理性检查：只接受 2010-01-01 ~ 2100-01-01 之间
    return n if 1262304000 < n < 4102444800 else 0


def _hot_thepaper():
    """澎湃热榜：首页侧栏 hotNews 数组。

    选它的理由是数据最「实」：interactionNum 是站方自己在页面上显示的真实互动数
    （不是今日头条那种点进去对不上的站内加权 HotValue），且能用 contId 拼出
    newsDetail_forward_<contId> 直达正文，列表看到的和点进去看到的是同一篇。

    【2026-10-07 修】原来 published 写死 0，而接口其实给了 pubTimeLong / publishTime ——
    结果这 20 条全都算「无时间」，时效过滤被整段跳过
    （用户反馈「热榜上居然还有两天前的新闻」，这里是元凶之一）。
    现在按 pubTimeLong → trackPublishTime → publishTime 依次取值。"""
    out = []
    for idx, it in enumerate((_thepaper_data().get("hotNews") or [])[:30], 1):
        if not isinstance(it, dict):
            continue
        cid = it.get("contId")
        title = _clean(it.get("name") or "")
        if not cid or not title:
            continue
        heat = it.get("interactionNum") or it.get("praiseTimes") or 0
        try:
            heat = int(heat)
        except Exception:
            heat = 0
        pub = _ms_to_ts(it.get("pubTimeLong")) or _ms_to_ts(it.get("trackPublishTime"))
        if not pub:
            try:
                pub = int(datetime.strptime((it.get("publishTime") or "").strip(),
                                            "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).timestamp())
            except Exception:
                pub = 0
        # 【2026-10-07 修】图片直接取自接口的 pic 字段。
        # 原来 image 写死 ""，只能靠 _fill_missing_images 去抓文章页 —— 但澎湃的
        # newsDetail_forward_<contId> 是个 **17KB 的空壳页**（正文由 JS/接口加载），
        # 里面根本没有 og:image，于是 20 条全被「没图不要」剔除，澎湃从国内热榜
        # 整个消失。而接口的 pic 就是原图（smallPic 才是 332px 缩略图）。
        _pic = (it.get("pic") or it.get("sharePic") or it.get("smallPic") or "").strip()
        if _pic.startswith("//"):
            _pic = "https:" + _pic
        out.append({
            "id": hashlib.md5(("hot-thepaper-" + str(cid)).encode("utf-8")).hexdigest()[:12],
            "region": "hot", "channel": "thepaper-hot", "label": "澎湃新闻",
            "cls": "热榜", "title": title, "desc": "",
            "link": f"https://www.thepaper.cn/newsDetail_forward_{cid}", "image": _pic,
            "published": pub, "heat": heat, "rank": idx, "translated": False,
        })
    print("[ok] hot/澎湃新闻: %d 条（有时间的 %d 条，带图的 %d 条）"
          % (len(out), sum(1 for x in out if x["published"]),
             sum(1 for x in out if x["image"])))
    return out


def _cdsb_rel_time(tail):
    """解析红星新闻锚文本里夹的「来源 + 时间」尾巴，转成 UTC+8 秒级时间戳。

    常见形态：
      "45分钟前" / "2小时前" / "2天前"
      "今天 12:30" / "昨天 13:20"
      "10-06 20:58"（当年） / "2026-10-06 20:58"
    解析不出返回 0（交给调用方按 0 处理：排在后面）。"""
    now = datetime.now(CST)
    s = (tail or "").strip()
    if not s:
        return 0
    m = re.search(r"(\d+)\s*分钟前", s)
    if m:
        return now.timestamp() - int(m.group(1)) * 60
    m = re.search(r"(\d+)\s*小时前", s)
    if m:
        return now.timestamp() - int(m.group(1)) * 3600
    m = re.search(r"(\d+)\s*天前", s)
    if m:
        return now.timestamp() - int(m.group(1)) * 86400
    m = re.search(r"今天\s*(\d{1,2})[:：](\d{1,2})", s)
    if m:
        d = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return d.timestamp()
    m = re.search(r"昨天\s*(\d{1,2})[:：](\d{1,2})", s)
    if m:
        d = (now - timedelta(days=1)).replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return d.timestamp()
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+(\d{1,2})[:：](\d{1,2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)), tzinfo=CST).timestamp()
        except Exception:
            pass
    m = re.search(r"(\d{1,2})[-/](\d{1,2})\s+(\d{1,2})[:：](\d{1,2})", s)
    if m:
        try:
            return datetime(now.year, int(m.group(1)), int(m.group(2)),
                            int(m.group(3)), int(m.group(4)), tzinfo=CST).timestamp()
        except Exception:
            pass
    return 0


def _hot_cdsb():
    """红星新闻（成都商报）：官网首页推荐流，站方按热度排的当天热点。

    无 RSS（/rss.xml 返 404），也没有专用热榜 API；但首页是服务端直出 HTML，
    正文都挂在 static.cdsb.com/micropub/Articles/YYYYMM/<hash>.html 上，
    锚文本就是标题（里面夹 &ldquo; 这类实体，交给 _clean 还原）。"""
    raw = _fetch("https://www.cdsb.com/")
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 红星新闻抓取失败")
        return []
    page = raw.decode("utf-8", "ignore")
    out, seen = [], set()
    pat = r'<a\s[^>]*href="((?:https?:)?//[^"]*static\.cdsb\.com/micropub/Articles/[^"]+)"[^>]*>(.*?)</a>'
    for m in re.finditer(pat, page, re.S):
        title = _clean(m.group(2))
        # 锚文本 append 了站方的「来源 + 时间」尾巴，例如
        #   "xxx（标题） 红星新闻 10-06 20:58" / "xxx（标题） 红星新闻 45分钟前"
        # 直接把它连同后面的时间一起切掉（保留足够的标题长度，避免误伤正文里含来源的标题）
        cut = title.rfind(" 红星新闻 ")
        pub = 0
        if cut > 8:
            rest = title[cut:]          # " 红星新闻 45分钟前" / " 红星新闻 10-06 20:58"
            title = title[:cut].strip()
            pub = _cdsb_rel_time(rest)
        if not pub:
            # 【2026-10-07 修 严重 bug】这里原来是
            #     pub = int(time.time()) - len(seen) * 120
            # 也就是**解析不出时间就凭空造一个「刚刚」**。后果：
            #   红星新闻首页有一批锚文本没有时间尾巴 → 全被标成「几分钟前」→
            #   两天前的旧闻既通过了「只留当天」的过滤，又排到了热榜第一条。
            #   用户连着反馈两次「热榜第一条就是两天前的」，实测原文页确认
            #   （薛之谦那条真实发布于 2026-10-05 11:24，我们却记成 10-07 11:23）。
            # 现在的规矩：**绝不允许伪造时间**。解析不出就留 0（未知），
            # 由 _fill_missing_images 去文章页读真实发布时间补上；
            # 补不到就按「未知时间」被当天过滤剔除 —— 宁可少一条，不可骗用户。
            pub = 0
        # 锚文本里混着纯图标 / 空 div，短于 6 字的不是标题
        if not title or len(title) < 6 or title in seen:
            continue
        seen.add(title)
        out.append({
            "id": hashlib.md5(("hot-cdsb-" + title).encode("utf-8")).hexdigest()[:12],
            "region": "hot", "channel": "cdsb-hot", "label": "红星新闻",
            "cls": "热榜", "title": title, "desc": "",
            "link": _norm_url(m.group(1), "https://www.cdsb.com/"), "image": "",
            "published": int(pub), "heat": 0, "rank": len(out) + 1, "translated": False,
        })
        if len(out) >= 30:
            break
    print(f"[ok] hot/红星新闻: {len(out)} 条")
    return out


def _hot_ifeng():
    """凤凰资讯：官网首页要闻流 newsstream[]（约 28 条，含 url / title / newsTime / 题图）。

    凤凰的 RSS 全线 404（news.ifeng.com/rss/*.xml 都指向同一个 404 页），但首页是
    SSR 内联 JSON，条目写作 {"id":..,"title":..,"url":..,"commentUrl":..,"skey":..,
    "newsTime":..,"source":..,"type":"article","thumbnails":{"image":[{"url":..}]}}。
    注意字段顺序和娱乐频道（cn-ent）不一样：这里的 "type" 排在最后而不是最前，
    所以锚点必须用 "id","title","url" 这个顺序，照抄 ent 的写法会一条都抓不到。
    该源带 newsTime，故额外做 72h 时效过滤，防止首页混进旧推广稿。"""
    raw = _fetch("https://news.ifeng.com/")
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 凤凰资讯抓取失败")
        return []
    page = raw.decode("utf-8", "ignore")
    cutoff = datetime.now(CST).timestamp() - MAX_AGE_HOURS.get("cn", 72) * 3600
    out, seen = [], set()
    pat = r'"id":"(\d+)","title":"([^"]*)","url":"(https?://[^"]*ifeng\.com/c/[^"]+)"'
    for m in re.finditer(pat, page):
        url = m.group(3)
        title = _clean(m.group(2))
        if not title or url in seen:
            continue
        # 时间和题图都在 url 之后，往后截取一小段取最近的那个
        tail = page[m.end():m.end() + 420]
        tms = re.search(r'"newsTime":"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"', tail)
        ts = 0
        if tms:
            try:
                ts = int(datetime.strptime(tms.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).timestamp())
            except Exception:
                ts = 0
        if ts and ts < cutoff:
            continue
        seen.add(url)
        ims = re.findall(r'"url":"(https://[^"]*ifengimg\.com[^"]*)"', tail[:320])
        out.append({
            "id": hashlib.md5(("hot-ifeng-" + m.group(1)).encode("utf-8")).hexdigest()[:12],
            "region": "hot", "channel": "ifeng-hot", "label": "凤凰网",
            "cls": "热榜", "title": title, "desc": "",
            "link": url, "image": _norm_url(ims[0], "https://news.ifeng.com/") if ims else "",
            "published": ts, "heat": 0, "rank": len(out) + 1, "translated": False,
        })
        if len(out) >= 30:
            break
    print(f"[ok] hot/凤凰网: {len(out)} 条")
    return out


def _parse_hot():
    """实时热榜：聚合 {澎湃新闻, 红星新闻, 凤凰网} 三家（用户指定）。
    每条带平台标签（label），按「平台内归一化热度」跨平台混合排序。任一平台失败不影响其它。"""
    parts = []
    for fn in (_hot_thepaper, _hot_cdsb, _hot_ifeng):
        try:
            parts.extend(fn())
        except Exception as ex:
            print(f"[warn] 热榜子源异常: {type(ex).__name__}")
    print(f"[ok] hot: 多平台聚合共 {len(parts)} 条")
    if not parts:
        return parts
    # 跨平台统一排序依据：先在每个平台内部排名次（有真实热度就按热度，没有就按站方名次），
    # 再统一换算成「平台内百分位」，最后才跨平台混排。
    #
    # 【为什么不用热度绝对值】各家口径完全不同：澎湃给的是互动数，凤凰/红星压根没有热度值
    # 只有名次。按绝对值排，凤凰/红星会用名次生成一个 0~1 的分数压过澎湃中下游的真实
    # 热度条目 —— 实测表现为 2~31 名被红星连续占据，澎湃除榜首外全沉到 30 名开外，
    # 等于一家垄断，「多平台聚合」名存实亡。
    # 【为什么不用 min-max 归一化】头部一条互动数会把同平台其余条目全压到 0.1 以下，
    # 跨无法与其它平台中下游竞争，同样是垄断，只是换成被另一个平台垄断。
    # 百分位只表达「你在你自己的榜上排第几」，天然跨平台可比。
    groups = {}
    for it in parts:
        groups.setdefault(it.get("label") or "", []).append(it)
    for group in groups.values():
        if any(x.get("heat") for x in group):
            group.sort(key=lambda x: x.get("heat") or 0, reverse=True)
        else:
            group.sort(key=lambda x: x.get("rank") or 999)
            for it in group:
                it["heat"] = 0          # 数字不可信，前端不展示，避免误导
        n = max(1, len(group))
        for i, it in enumerate(group, 1):
            it["score"] = (n - i + 1) / n
    parts.sort(key=lambda x: x.get("score") or 0, reverse=True)
    return parts
def _parse_mefcl(src):
    """mefcl.com（用户投稿站点）：站方用 ge_js_validator 的 JS cookie 校验来访者身份。

    访问流程（已实测，按站方校验流程走）：
      1. 先抓首页 → 若拿到 278B 的校验页，正则取到站方动态生成的合法 cookie 值
         （形如 1791287062@63@<32位hex>，max-age=1800，30 分钟内有效）；
      2. 带合法 Cookie: ge_js_validator_63=<值> 重新请求首页 → 返回 102KB 真内容；
      3. 解析 <article class="excerpt ..."> 块：标题取 <h2><a> 文本，链接取 <a href>，
         题图取 <img data-src>（src 只是占位 thumbnail.png），摘要取 <p class="note">。

    /feed/ 仍返回 403，故改抓首页 HTML。解析失败 / 拿不到内容时返回 []，不影响其它源。"""
    import time as _t
    now = _t.time()
    # 先直接抓首页：站方若已不再返回校验页，正文（含 article.excerpt）直接返回，
    # 这时无需 cookie，直接解析即可——否则会误判「没有校验字段」而整源消失。
    try:
        with httpx.Client(follow_redirects=True, timeout=10, trust_env=False,
                          headers={"User-Agent": UA}) as c:
            r0 = c.get(src["url"])
        raw0 = r0.content if r0.status_code == 200 else b""
    except Exception as ex:
        print(f"[warn] mefcl 首页抓取异常: {type(ex).__name__}（回退上次结果）")
        return _MEFCL_LAST
    html0 = raw0.decode("utf-8", "ignore")
    if "excerpt" in html0:
        # 正常返回正文，直接解析
        html = html0
    else:
        # 拿到站方校验页 → 取合法 cookie 后重新请求（按站方校验流程访问）
        cookie = _MEFCL_COOKIE.get("value")
        if not cookie or (now - _MEFCL_COOKIE.get("ts", 0)) > 1500:
            m = re.search(r'ge_js_validator_63=([^";\s]+)', html0)
            if not m:
                print("[warn] mefcl 无文章块也无校验 cookie 字段，可能被改版（回退上次结果）")
                return _MEFCL_LAST
            cookie = m.group(1)
            _MEFCL_COOKIE["value"] = cookie
            _MEFCL_COOKIE["ts"] = now
        try:
            with httpx.Client(follow_redirects=True, timeout=10, trust_env=False,
                              headers={"User-Agent": UA, "Cookie": "ge_js_validator_63=" + cookie}) as c:
                r = c.get(src["url"])
            raw = r.content if r.status_code == 200 else None
        except Exception as ex:
            print(f"[warn] mefcl 抓取异常: {type(ex).__name__}（回退上次结果）")
            return _MEFCL_LAST
        if not raw:
            print("[warn] mefcl 抓取失败（可能未通过站方校验，回退上次结果）")
            return _MEFCL_LAST
        html = raw.decode("utf-8", "ignore")
        # 仍是校验页（有 cookie 字段但无文章块）→ cookie 失效，下次重取
        if "ge_js_validator" in html and "excerpt" not in html:
            _MEFCL_COOKIE["ts"] = 0
            print("[warn] mefcl 仍返回校验页，cookie 失效需重试（回退上次结果）")
            return _MEFCL_LAST
    items = []
    cutoff = datetime.now(CST).timestamp() - MAX_AGE_HOURS.get("cn", 72) * 3600
    for m in re.finditer(r'<article\s+class="excerpt[^"]*">(.*?)</article>', html, re.S):
        blk = m.group(1)
        # 链接：优先 <a class="focus" href>（文章主链接）；回落 <h2><a href>
        linkm = re.search(r'<a[^>]*class="focus"[^>]*href="([^"]+)"', blk) \
            or re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"', blk, re.S)
        link = _norm_url(linkm.group(1), src["base"]) if linkm else ""
        # 标题：<h2><a> 可见文本；空则取该 <a> 的 title 属性；再空取 focus a 的 title 属性
        hm = re.search(r'<h2[^>]*>\s*<a[^>]*>(.*?)</a>', blk, re.S)
        title = _clean(hm.group(1)) if hm else ""
        if not title and hm:
            tam = re.search(r'<h2[^>]*>\s*<a[^>]*title="([^"]+)"', blk, re.S)
            if tam:
                title = _clean(tam.group(1))
        if not title:
            fm = re.search(r'<a[^>]*class="focus"[^>]*title="([^"]+)"', blk)
            if fm:
                title = _clean(fm.group(1))
        if not title:
            continue
        # 题图：优先 <img data-src>（真实图），回落 src（占位 thumbnail.png）
        img = ""
        dm = re.search(r'<img[^>]*\bdata-src="([^"]+)"', blk)
        if dm:
            img = _norm_url(dm.group(1), src["base"])
        if not img:
            sm = re.search(r'<img[^>]*\bsrc="([^"]+)"', blk)
            if sm:
                img = _norm_url(sm.group(1), src["base"])
        # 摘要
        dn = re.search(r'<p[^>]*class="note"[^>]*>(.*?)</p>', blk, re.S)
        desc = _clean(dn.group(1))[:200] if dn else ""
        # 时间：优先 <time datetime="YYYY-MM-DD HH:MM:SS">；
        # 【2026-10-07 修】mefcl 实际用的是 <time>2026-09-28</time>（**只有日期、
        # 没有 datetime 属性**），原来的正则匹配不到 → 20 条全部 published=0 →
        # 被「只留当天」的规则整批滤掉，结果用户最重要的软件站 mefcl 在列表里
        # 整个消失。这里补一个纯日期格式的回落（按当天 00:00 计）。
        ts = 0
        tsm = re.search(r'datetime="(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"', blk)
        if tsm:
            try:
                ts = int(datetime.strptime(tsm.group(1).replace("T", " "),
                                            "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).timestamp())
            except Exception:
                ts = 0
        if not ts:
            dm2 = re.search(r'<time[^>]*>\s*(\d{4})-(\d{2})-(\d{2})\s*</time>', blk)
            if dm2:
                try:
                    ts = int(datetime(int(dm2.group(1)), int(dm2.group(2)),
                                      int(dm2.group(3)), tzinfo=CST).timestamp())
                except Exception:
                    ts = 0
        if ts and ts < cutoff:
            continue
        items.append({
            "id": hashlib.md5((src["id"] + title).encode("utf-8")).hexdigest()[:12],
            "region": src["region"], "channel": src["id"],
            "label": src.get("label", src["id"]), "cls": src["cls"],
            "title": title, "desc": desc, "link": link,
            "image": img or "", "published": ts, "translated": False,
        })
    if items:
        _MEFCL_LAST = items
    print(f"[ok] {src['id']}: mefcl 解析 {len(items)} 条")
    return items


def _parse_thepaper(src):
    """澎湃新闻：官方 contentapi（cache.thepaper.cn），直接给 JSON，不用逆解析 HTML。

    这是本轮实测里少数「既权威又结构化」的国内源：干货字段一应俱全
      name            标题
      pic             题图（完整 http 地址）
      interactionNum  互动数 / praiseTimes 点赞数 → 真实热度，可以显示给用户
      publishTime     毫秒时间戳
      contId          文章 id，拼 https://www.thepaper.cn/newsDetail_forward_<contId> 直达原文
    同一个响应里还有 morningEveningNews（早晚报，只有封面图、没有正文链接）之类，
    统一挑带 contId 的条目，避免把「封面」这种没法点击的东西塞进列表。
    data 经 _thepaper_data() 取，与热榜共用同一次抓取（详见该函数的说明）。"""
    data = _thepaper_data()
    out = []
    seen = set()
    for key in ("hotNews", "editorHandpicked", "financialInformationNews"):
        for it in (data.get(key) or []):
            if not isinstance(it, dict):
                continue
            cid = it.get("contId")
            title = _clean(it.get("name") or "")
            if not cid or not title or cid in seen:
                continue
            seen.add(cid)
            # 注意字段名：澎湃给的毫秒时间戳是 trackPublishTime（publishTime 这个字段实测为空），
            # 拿不到时间条目就会被统一当成 0，在「最新优先」里全沉底，所以必须取到。
            ts = it.get("trackPublishTime") or it.get("publishTime") or it.get("pubTimeLong") or 0
            try:
                ts = int(ts)
                ts = ts // 1000 if ts > 1e11 else ts      # 毫秒 → 秒
            except Exception:
                ts = 0
            # 互动数优先，没有就用点赞数；都没有就 0（前端不显示）
            heat = it.get("interactionNum") or it.get("praiseTimes") or 0
            try:
                heat = int(heat)
            except Exception:
                heat = 0
            pic = it.get("pic") or it.get("smallPic") or ""
            if pic and str(pic).startswith("//"):
                pic = "https:" + pic
            out.append({
                "id": hashlib.md5(("thepaper-" + str(cid)).encode("utf-8")).hexdigest()[:12],
                "region": "cn", "channel": "cn-thepaper", "label": "澎湃新闻",
                "cls": "要闻", "title": title, "desc": _clean(it.get("subTitle") or "")[:200],
                "link": f"https://www.thepaper.cn/newsDetail_forward_{cid}",
                "image": pic, "published": ts, "heat": heat, "translated": False,
            })
    print(f"[ok] 澎湃新闻: {len(out)} 条")
    return out


def _parse_ifeng(src):
    """凤凰娱乐首页（娱乐栏目：明星/绯闻/八卦）。
    首页内嵌 JSON 数组，每条含：newsTime(YYYY-MM-DD HH:MM:SS) / thumbnails.image[0].url
    / title / url(ent.ifeng.com/c/xxx)。首页会混入少量旧推广稿，故按 newsTime 做时效过滤
    （cn-ent 放宽到 720h=30 天）。抓取失败返回 []，不影响其它源。"""
    raw = _fetch(src["url"])
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 凤凰娱乐抓取失败")
        return []
    html = raw.decode("utf-8", "ignore")
    items, seen = [], set()
    no_ts = 0            # 【2026-10-09】源站没给时间的条数（只用于日志，不伪造时间）
    max_age = src.get("max_age_hours") or MAX_AGE_HOURS.get("cn", 24)
    cutoff = datetime.now(CST).timestamp() - max_age * 3600
    for m in re.finditer(r'"type":"article","url":"(https://ent\.ifeng\.com/c/[^"]+)"', html):
        url = m.group(1)
        back = html[max(0, m.start() - 900):m.start()]
        tms = re.findall(r'"newsTime":"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"', back)
        ttl = re.findall(r'"title":"([^"]*)"', back)
        ims = re.findall(r'"url":"(https://[^"]*ifengimg\.com[^"]*)"', back)
        title = _clean(ttl[-1]) if ttl else ""
        if not title or title in seen:
            continue
        ts = 0
        if tms:
            try:
                ts = int(datetime.strptime(tms[-1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).timestamp())
            except Exception:
                ts = 0
        if ts and ts < cutoff:
            continue
        if not ts:
            no_ts += 1      # 源站未给时间：published 写 0，当天列表里由 _select_fresh 统一收口
        img = _norm_url(ims[-1], src["base"]) if ims else ""
        seen.add(title)
        items.append({
            "id": hashlib.md5((src["id"] + title).encode("utf-8")).hexdigest()[:12],
            "region": src["region"], "channel": src["id"],
            "label": src.get("label", src["id"]), "cls": src["cls"],
            "title": title, "desc": "", "link": url,
            # 【2026-10-09 修 中-1】解析不出时间就写 0（＝源站未给时间），**绝不伪造成"刚刚"**：
            # 以前这里是 `ts or now()`，与 README「绝不伪造时间」的承诺直接矛盾，
            # 而且旧稿会被标成"1 分钟前"顶到娱乐栏目第一位。0 值由 _select_fresh
            # 按「未知时间不参与当天」统一过滤（但仍留在搜索池里，搜索照样搜得到）。
            "image": img, "published": ts,
            "translated": False,
        })
        if len(items) >= 30:
            break
    print(f"[ok] {src['id']}: 凤凰娱乐解析 {len(items)} 条"
          f"（另有 {no_ts} 条源站未给时间，不计入当天列表）")
    return items


app = FastAPI(title="hotnews", version=VERSION)


def _drop_shared_images(items):
    """
    一个源若把同一张图挂到 N 条不同新闻上，那是站方默认图而不是配图
    （实测界面新闻 30 条里只有 12 张唯一图，两张各重复 10 次 / 9 次）。
    这种图清掉，卡片走无图样式反而干净。
    """
    per_ch = {}
    for it in items:
        if it.get("image"):
            per_ch.setdefault(it["channel"], []).append(it)
    for ch, group in per_ch.items():
        cnt = Counter(it["image"] for it in group)
        bad = {u for u, n in cnt.items() if n >= DUP_IMG_MIN}
        if not bad:
            continue
        for it in group:
            if it["image"] in bad:
                it["image"] = ""
        print(f"[ok] {ch}: 剔除 {len(bad)} 张被多条新闻共用的默认图")
    return items


# ==================== 当天时效 / 文章页补图（2026-10-07 新增） ====================
# 用户要求两条新规矩：
#   1) 所有板块必须是「当天的」
#   2) 每条新闻必须有高清大图，没图的就不要上
# 这两条和「红星/凤凰必须进要闻」是冲突的——热榜那三家本身一条图都没有。
# 解决办法就是下面的 _fill_missing_images：去文章页把图补回来，补不到的才丢。

# 列表页只给缩略图、必须去正文页取大图的源（mefcl 的列表图固定 220x150，必糊）
BIG_IMAGE_CHANNELS = {"cn-mefcl"}
# 低质图豁免：这些源的题图**站方本身就小**，去正文页也换不出更大的，
# 只能开豁免，否则整个栏目会消失。目前只有 mefcl（站方图固定 220x150）。
# 注意：豁免只免「短边门槛」，**仍必须有图**（没图的照样剔除）。
MIN_IMG_EXEMPT_CHANNELS = {"cn-mefcl"}
# 需要去文章页「取发布时间」的源（它们的图已经有了，只是为了时间才抓一次）
# 只为「取发布时间」才抓文章页的源。目前**为空** ——
# 曾经放过网易娱乐，但它的首页是网易号泛频道推荐流（每次请求内容都不同，
# 混着汽车/养生/育儿/社会），加娱乐关键词过滤后 40 条只剩 3 条、还有 2 条不是娱乐，
# 得不偿失，用户 2026-10-07 决定移除。机制保留，以后有需要再往里加。
TIME_FIX_CHANNELS = set()


def _day_start_cutoff():
    """今天 00:00（东八区）的时间戳。"""
    now = datetime.now(CST)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _select_fresh(items, region):
    """按栏目收口时效，**一律按时间倒序**（最新鲜的永远在最上面）。

    用户的几条要求互相冲突，这是权衡后的方案（每条都在代码里标了出处）：
      · 「常看常新、绝不看旧新闻」→ **新闻类（要闻/热榜）严格当天**；
        热榜给 24 小时（否则澎湃这类更新慢的榜单会整个消失）
      · 「每个栏目必须有三个以上的网站，要不然太单一」+「软件栏目去哪里了」
        → **垂类（科技/软件/娱乐）按 CLS_MAX_AGE_HOURS 放几天**。
        实测：只在当天里筛，小众软件/少数派/开源中国/果核剥壳今天全都没发东西，
        「软件」只剩 mefcl 一家 —— 正是用户两次反馈的现象。
      · 每条都带**真实发布时间**（红星那种解析不出就伪造「刚刚」的逻辑已彻底删除），
        界面工具栏会显示「最新 <时间>」，不会拿旧闻冒充新新闻。
    """
    if not SAME_DAY or not items:
        return items
    today = _day_start_cutoff()
    now = datetime.now(CST).timestamp()

    def cutoff(it):
        cls = it.get("cls") or ""
        h = CLS_MAX_AGE_HOURS.get(cls)
        if h:
            return now - h * 3600
        if region == "intl":
            # 国际源条目稀疏（实测跨度可达两天），按「今天 00:00」会把它们全滤空
            return now - MAX_AGE_HOURS.get("intl", 48) * 3600
        return today

    kept = [it for it in items if (it.get("published") or 0) >= cutoff(it)]
    dropped = len(items) - len(kept)
    kept.sort(key=lambda x: x.get("published") or 0, reverse=True)
    # 各栏目的条数与来源数，方便一眼看出哪个栏目被饿死了
    _stat = {}
    for it in kept:
        c = it.get("cls") or "?"
        _stat.setdefault(c, [0, set()])
        _stat[c][0] += 1
        _stat[c][1].add(it.get("label") or it.get("channel"))
    _txt = " ".join(f"{c}{n}条/{len(s)}家" for c, (n, s) in sorted(_stat.items()))
    print(f"[ok] {region}: 时效收口 {len(kept)} 条（丢掉 {dropped} 条）| {_txt}")
    return kept


_OG_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\']og:image(?::url)?["\'][^>]*content=["\']([^"\']+)', re.I)
_OG_RE_B = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:image', re.I)
_BODY_IMG_RE = re.compile(
    r'<img[^>]+(?:data-src|data-original|src)=["\']([^"\']+\.(?:jpe?g|png|webp)[^"\']*)', re.I)
_IMG_JUNK = ("logo", "icon", "avatar", "qrcode", "erweima", "blank", "/ad", "sprite", "share")


# 文章页里的发布时间，按可信度从高到低。
# 红星新闻的首页尾巴**实测不可信**（首页写 10-06 23:23，文章自己写 2026-10-05 11:24），
# 所以以文章页自己的元数据为准。
_ART_TIME_PATS = (
    re.compile(r'"datePublished"\s*:\s*"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})', re.I),
    re.compile(r'property=["\']article:published_time["\'][^>]*content=["\']'
               r'(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})', re.I),
    re.compile(r'content=["\'](\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})["\'][^>]*'
               r'property=["\']article:published_time', re.I),
    re.compile(r'name=["\'](?:publishdate|pubdate|weibo:article:create_at|'
               r'og:release_date)["\'][^>]*content=["\'](\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})', re.I),
    re.compile(r'class=["\'][^"\']*(?:time|date)[^"\']*["\'][^>]*>\s*'
               r'(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})', re.I),
    re.compile(r'(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[:：](\d{2})'),
    re.compile(r'(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})'),
)


def _parse_article_time(txt):
    """从文章页 HTML 抽真实发布时间，抽不到返回 0。

    带合理性检查：只接受 2015 年之后、且不超过明天的时间，
    避免页面里其它地方的年份串（版权年、评论时间）被误命中。
    """
    for rx in _ART_TIME_PATS:
        m = rx.search(txt)
        if not m:
            continue
        try:
            y, mo, d, h, mi = (int(x) for x in m.groups()[:5])
            ts = int(datetime(y, mo, d, h, mi, tzinfo=CST).timestamp())
        except Exception:
            continue
        if 1420070400 < ts < time.time() + 2 * 86400:
            return ts
    return 0


# ============ 【2026-10-09 换源】按站点登记的「正文首图」取法 ============
# 背景：中新网系 RSS **不带图**，连文章页也没有 og:image，正文首图在固定的正文容器里；
# 而通用正文 img 规则会先命中站头 logo（中新网页面里第一张 .png 是 164x50 的导航图），
# 那属于张冠李戴 —— 所以给这类站点登记专门的取法，**命中登记就不再退回通用规则**。
_CN_ZW_START = re.compile(r'class=["\']left_zw["\']', re.I)
# 正文窗口的**结束标记**（按可靠性排序）：`<!--正文end-->` 是站方自己标的正文结尾，
# 退而求其次才是「相关新闻」注释与图片频道容器。
# ⚠ 这里不能用「固定长度窗口」了事：中新网正文之后紧跟着「相关新闻」卡片
# （bigpic_list / intermoren_box / news_title），那里的缩略图与本条新闻毫无关系，
# 取它就是张冠李戴 —— 实测 https://…/sh/2026/10-09/10709549.shtml 的窗口里
# 唯一一张图就是另一条新闻的缩略图，必须靠这个结束标记把它挡在外面。
_CN_ZW_STOP = re.compile(
    r'<!--\s*正文\s*end\s*-->|<!--\s*相关新闻|id=["\']zhengwenpic["\']', re.I)
# 中新网的正文图床有**两个**域名，别只写一个（实测漏写 i2.chinanews.com 会让大半条目
# 明明有图却取不到：「//i2.chinanews.com/simg/hnhd/...」不含 "i2.chinanews.com.cn" 这个子串）。
_CN_IMG_HOSTS = ("i2.chinanews.com", "image.chinanews.com", "poss-videocloud.cns.com.cn")


def _cn_body_image(txt, url):
    """中新网文章页的正文首图：<div class="left_zw"> 容器里的第一张站内图床图。

    只认站内图床（i2.chinanews.com / i2.chinanews.com.cn / image.chinanews.com /
    poss-videocloud.cns.com.cn），导航、页脚、App 推广图都在
    www.chinanews.com.cn/fileftp/ 或 image.cns.com.cn/default/ 下，天然被排除。
    找不到就返回 ""，调用方按「这条没图」处理（宁缺毋滥）。

    优先取**带图说**的那张：站方给正文照片配了 `<div class="pictext">图说</div>`，
    而相关新闻缩略图没有图说。一篇文章里若第一张是边栏图、第二张才是正文照片，
    这个优先级就能挑对。
    """
    m = _CN_ZW_START.search(txt)
    if not m:
        return ""
    seg = txt[m.end(): m.end() + 40000]
    stop = _CN_ZW_STOP.search(seg)
    if stop:
        seg = seg[:stop.start()]
    first = ""
    for mm in re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', seg, re.I):
        u = _norm_url(mm.group(1), url)
        if not u or any(j in u.lower() for j in _IMG_JUNK):
            continue
        if not any(h in u for h in _CN_IMG_HOSTS):
            continue
        if not first:
            first = u
        # 站方给正文照片配了 <div class="pictext">图说</div>，紧跟在图后面（中间隔着
        # `alt="" />` 这类标签尾巴）。两点都踩过坑，别再改回去：
        #   ① 窗口要从**整个 <img …> 标签结束处**起算，不能从 src="…" 结束处起算；
        #   ② 窗口要在**下一张图之前**截断 —— 否则前一张图会「借走」后一张图的图说，
        #      把边栏图当成正文照片（自测里专门有一条断言盯着这个）。
        _after = seg[mm.end(): mm.end() + 400]
        _nxt = _after.lower().find("<img")
        if _nxt >= 0:
            _after = _after[:_nxt]
        if re.search(r'<div[^>]+class=["\']pictext', _after, re.I):
            return u
    return first


# 域名后缀 → 取图函数（取不到返回 ""）
_ART_SITE_IMG_RULES = (
    ("chinanews.com.cn", _cn_body_image),
)


def _art_site_rule(url):
    """这个地址有没有登记「正文首图」的站点规则；没有返回 None。"""
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return None
    for suffix, fn in _ART_SITE_IMG_RULES:
        if host == suffix or host.endswith("." + suffix):
            return fn
    return None


def _article_meta(url, timeout=8):
    """抓一次文章页，同时取回「题图」和「真实发布时间」。

    【为什么合并】补图和校准时间都需要文章页，分两次抓纯属浪费。
    返回 (image, published)；都取不到就是 ("", 0)。

    加速要点（2026-10-07）：
      1) 只要 <head> 里的 og:image 和时间元数据，所以**只读前 ART_PAGE_BYTES 字节**
      2) 结果放进 _ART_CACHE 永久记住（文章元数据不会变）

    【2026-10-09 换源】参数 timeout：单条抓取上限。RSS 不带图的源
    （BODY_IMAGE_CHANNELS，中新网系）传 BODY_IMG_TIMEOUT=3 秒 —— 它们条目多
    （一个源 30 条），不能让个别慢页面把整轮补图拖住；失败就跳过该条，不重试。
    """
    if not url or not url.startswith("http"):
        return "", 0, ""
    cached = _ART_CACHE.get(url, None)
    if cached is not None:
        return cached          # (图, 发布时间, 栏目分类)
    # mefcl 的文章页和首页一样有 ge_js_validator 校验，**必须带上同一个 cookie**，
    # 否则抓回来的是校验页（几百字节），og:image 取不到 → mefcl 只能留在列表页那张
    # 220x150 缩略图上 → 被高清门槛剔除 → 整个「软件」栏目消失（用户实际反馈）。
    _eh = None
    if "mefcl.com" in url:
        _cv = _MEFCL_COOKIE.get("value")
        if _cv:
            _eh = {"Cookie": "ge_js_validator_63=" + _cv}
    try:
        raw = _fetch(url, timeout=timeout, max_bytes=ART_PAGE_BYTES, extra_headers=_eh)
    except Exception:
        return "", 0, ""
    if not raw:
        return "", 0, ""
    data = raw[0] if isinstance(raw, tuple) else raw
    if not data:
        return "", 0, ""
    try:
        txt = data.decode("utf-8", "ignore")
    except Exception:
        return "", 0, ""
    img = ""
    for rx in (_OG_RE, _OG_RE_B):
        m = rx.search(txt)
        if m:
            u = _norm_url(m.group(1), url)
            if not any(x in u.lower() for x in _IMG_JUNK):
                img = u
                break
    # 【2026-10-09 换源】og:image 没有时，先按**站点登记规则**取正文首图（中新网系）；
    # 命中登记的站点不再退回下面的通用正文 img 规则（那里的第一张往往是站头 logo）。
    _site_fn = _art_site_rule(url)
    if not img and _site_fn:
        try:
            img = _site_fn(txt, url) or ""
        except Exception:
            img = ""
    if not img and not _site_fn:
        m = _BODY_IMG_RE.search(txt)
        if m:
            u = _norm_url(m.group(1), url)
            if not any(x in u.lower() for x in _IMG_JUNK):
                img = u
    ts = _parse_article_time(txt)
    # 【2026-10-07】再顺手读**站方自己的栏目分类**：凤凰文章页 JSON-LD 里有
    # "articleSection":"国际" / "社会" / "军事"…，用来判定国内/国际比关键词黑名单可靠得多
    #（黑名单必漏：实测「乌征兵人员将1岁幼儿父亲沿地拖行」标题里没有"乌克兰"三个字）。
    sec = ""
    _sm = re.search(r'"articleSection"\s*:\s*"([^"]{1,12})"', txt)
    if _sm:
        sec = _sm.group(1).strip()
    if img or ts or sec:
        _ART_CACHE.put(url, (img, ts, sec), 256)   # 只缓存有结果；失败不缓存，下次还有机会重试
    return img, ts, sec


def _round_robin_by_channel(pool):
    """按来源（channel）轮流取，保证补图名额不会被某一家全吃掉。

    【2026-10-09 换源】为什么需要：补图名额（max_fetch）原来是按列表顺序切的，
    而列表顺序 = SOURCES 的登记顺序。新增的 4 个「RSS 不带图」的中新网系源条目多
    （每源 30 条），不轮流取的话排在前面的源会把名额占满，排在后面的源整轮补不到图、
    在页面上直接消失 —— 正是用户反复反馈过的「某个栏目只剩一家网站」。
    """
    buckets = {}
    for it in pool:
        buckets.setdefault(it.get("channel") or "", []).append(it)
    out, i = [], 0
    while buckets:
        for k in list(buckets):
            b = buckets[k]
            if i < len(b):
                out.append(b[i])
            else:
                buckets.pop(k, None)
        i += 1
    return out


def _fill_missing_images(items, region, max_fetch=24, workers=14, force_all=False):
    """给缺图的条目去文章页补一张图。补不到的由调用方丢弃。

    热榜（澎湃/红星/凤凰）是纯文字榜，mefcl 列表页只给 220x150 缩略图，
    「RSS 不带图」的源（中新网系 / 新浪娱乐）列表页也没有图 ——
    这几类都必须靠这一步续命，否则按「没图不要」的新规矩会整块消失。
    """
    if not items:
        return items

    def needs(it):
        # force_all：热榜必须**每条都抓一次文章页** —— 不是为图，是为了读站方
        # JSON-LD 里的 articleSection（"国际"/"社会"/"军事"…），用来判定国内/国际。
        # 凤凰热榜自带缩略图，不强制抓的话这些条目永远不会被访问到，也就拿不到
        # 栏目分类（实测 section 全空 → 只能退回关键词黑名单 → 必漏，比如
        # 「乌征兵人员将1岁幼儿父亲沿地拖行」标题里没有"乌克兰"三个字）。
        if force_all:
            return True
        # 网易娱乐的图由首页缩略图改写尺寸得到、不必抓文章页，
        # 但它的**发布时间**必须去文章页取，所以这类条目仍然要抓一次。
        return (it.get("channel") in BIG_IMAGE_CHANNELS
                or not it.get("image")
                or (it.get("channel") in TIME_FIX_CHANNELS and not it.get("published")))

    # 【2026-10-09 换源】名额按来源轮流分配（见 _round_robin_by_channel），
    # 免得新建的中新网系源把补图名额占满、其它源整轮补不到图。
    need = _round_robin_by_channel([it for it in items if needs(it)])[:max_fetch]
    if not need:
        return items
    print(f"[ok] {region}: {len(need)} 条需要补图，去文章页取")
    # 【2026-10-09 换源】「RSS 不带图」的源单条限时 3 秒（用户要求），
    # 其余源保持原来的 8 秒；失败就跳过该条，由调用方按「没图不要」处理。
    def _tmo(it):
        return BODY_IMG_TIMEOUT if it.get("channel") in BODY_IMAGE_CHANNELS else 8
    with ThreadPoolExecutor(max_workers=workers) as ex:
        got = list(ex.map(lambda it: _article_meta(it.get("link") or "", timeout=_tmo(it)), need))
    filled = fixed_time = 0
    for it, _meta in zip(need, got):
        img, ts = _meta[0], _meta[1]
        if len(_meta) > 2 and _meta[2]:
            it["section"] = _meta[2]      # 站方栏目分类，用于国内/国际判定
        # 【2026-10-07】只为「取发布时间」才抓文章页的源（网易娱乐），
        # **不要用文章页的图覆盖已有的图** —— 网易号文章页的 og:image 是站方
        # 自己的「下载App」横幅（common_nav/topapp.jpg，150x178，每篇都一样），
        # 会把首页那张改写放大得到的 660x440 好图顶掉。
        _time_only = it.get("channel") in TIME_FIX_CHANNELS and it.get("image")
        if img and img != it.get("image") and not _time_only:
            it["image"] = img
            filled += 1
        # 【时间校准 2026-10-07】文章页自己的发布时间才是权威的：
        #   · 没有时间（红星首页那批没有尾巴的锚）→ 补上
        #   · 热榜条目 → 一律以文章页为准（实测红星首页尾巴会把 10-05 写成 10-06，
        #     不覆盖的话旧闻还是会漏进「只留当天」的列表）
        if ts:
            old = it.get("published") or 0
            if not old or it.get("cls") == "热榜":
                if old != ts:
                    it["published"] = ts
                    fixed_time += 1
    print(f"[ok] {region}: 补图 {filled}/{len(need)} 条，校准发布时间 {fixed_time} 条")
    return items


def _collect_fast(region):
    """快速版抓取：**只跑 RSS / HTML 源**（并发，实测国内约 4 秒、国际约 10 秒）。

    为什么需要：用户反馈「抓取时间让我等的焦虑」—— 完整 _collect 要 30~50 秒，
    因为热榜要逐个抓文章页补图、无图源也要逐个抓文章页补图。首屏一直空着等很难受。
    所以先把这个快的结果发布出去，慢活交给完整 _collect 在后台补完再覆盖发布。

    代价：这一版里「RSS 本身不带图」的源（澎湃/界面/新浪娱乐/中新网系）暂时不出现，
    等后台补图补时间完成后自动补上（前端每 15 秒轮询会自动重绘）。
    """
    out = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        for r in ex.map(_parse_one, SOURCES[region]):
            out.extend(r)
    seen, items = set(), []
    for it in out:
        title = it.get("title")
        if not title or title in seen:
            continue
        seen.add(title)
        items.append(it)
    items = _drop_shared_images(items)
    # 【2026-10-09 换源】国内/国际**都**只留现成有图的（无图的等后台补图那一轮）。
    # 为什么国际也要加：换源后国际通道多了「RSS 不带图」的中新网·国际，
    # 不过滤的话预热阶段会先把这一批无图条目发到页面上（前端渲染成占位色块），
    # 与「没图的就不要上」的口径不一致；过滤后首屏只有自带图的 CGTN，
    # 几十秒后完整 _collect 补好图再覆盖发布。
    items = [it for it in items if it.get("image")]
    _note_item_hosts(items)       # 【2026-10-09 修 高-1】发布前登记图片域名（/img 白名单）
    return _select_fresh(items, region)

def _collect(region):
    """并发抓取一个 Region 的全部源，去重后按时间倒序。
    国内源在此剔除无图新闻（快，不下载图）；低质图由后台 _enrich_quality_bg 异步剔除，
    避免首屏因下载测尺寸而变慢。"""
    if region == "hot":
        # 【2026-10-07 重排】顺序改成：按时间筛 → 抓文章页（补图+校准时间+读站方栏目）
        #   → 才按栏目分流。「先分流」是不行的：凤凰首页 JSON 里**没有栏目信息**
        #   （channel 字段是空的），必须抓到文章页才能看到 JSON-LD 的 articleSection。
        #   关键词黑名单必漏——实测「乌征兵人员将1岁幼儿父亲沿地拖行」是国际新闻，
        #   标题里却没有"乌克兰"三个字（用户反馈「国内热榜第二条还是国际新闻」）。
        _cut = datetime.now(CST).timestamp() - CLS_MAX_AGE_HOURS.get("热榜", 24) * 3600
        fresh_hot = [it for it in _parse_hot()
                     if not it.get("published") or it["published"] >= _cut]
        hot_items = _fill_missing_images(fresh_hot, "hot", max_fetch=80, force_all=True)
        keep = [it for it in hot_items if it.get("image")]
        dom, _intl = _split_hot_by_scope(keep)
        _HOT_INTL_SPILL[:] = _intl
        print(f"[ok] hot: 时间筛后 {len(fresh_hot)} 条，补图带图 {len(keep)} 条；"
              f"按站方栏目分流 国内 {len(dom)} / 国际 {len(_intl)}")
        PRELOAD_ALL["hot"] = list(dom)
        _note_item_hosts(dom)     # 【2026-10-09 修 高-1】发布前登记图片域名（/img 白名单）
        return _select_fresh(dom, "hot")
    out = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for r in ex.map(_parse_one, SOURCES[region]):
            out.extend(r)
    seen, items = set(), []
    for it in out:
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        items.append(it)
    items = _drop_shared_images(items)
    if region != "cn":
        # 【2026-10-07】接收国内板块剔出来的国际新闻（国内「要闻」里的国际内容）。
        # 国内先抓（见 _warmup 顺序调整），所以这里能拿到；拿不到就下一轮补。
        _spill = _spill_from_cn()
        if _spill:
            _seen_t = {it["title"] for it in items}
            _add = [s for s in _spill if s["title"] not in _seen_t]
            if _add:
                print(f"[ok] {region}: 并入国内板块转来的国际新闻 {len(_add)} 条")
                items.extend(_add)
                items = _drop_shared_images(items)
    if region == "cn":
        before = len(items)
        # 用户要求「没图的就不要上」：豁免源已取消，无图一律剔除
        cn_only = []
        # 【2026-10-07】先给「RSS 本身不带图」的条目去文章页补图，补不到才剔除。
        # 为什么必须做：少数派(4条)、开源中国的 RSS **完全没有图**，直接按「没图不要」
        # 剔除会让「软件」栏目只剩 mefcl 一家 —— 正是用户反馈的
        # 「软件栏目怎么只有一个网站的」。它们的文章页其实都有 og:image，抓得到。
        # 【2026-10-07 修 bug】这里原来只挑「没有图的」条目（`not it.get("image")`），
        # 结果**网易娱乐永远进不来** —— 它的图是首页缩略图改写尺寸得到的、本身就有图，
        # 于是不会被传进补图函数，发布时间就永远补不上（published=0）→ 被时效规则丢掉。
        # 正确的条件是「缺图的 + 需要补时间的（TIME_FIX_CHANNELS）都要过一遍」。
        _needmeta = [it for it in items
                     if not it.get("image")
                     or it.get("channel") in BIG_IMAGE_CHANNELS
                     or (it.get("channel") in TIME_FIX_CHANNELS and not it.get("published"))]
        if _needmeta:
            # 【2026-10-09 换源】名额 80 → 96：新增的中新网·财经/体育/社会三个源
            # 每源最多 24 条且都不带图，加上界面/华尔街见闻/少数派/开源中国原有的缺图条目，
            # 80 个名额会被占满。名额内部按来源轮流分配（_round_robin_by_channel），
            # 所以这个数字只是上限，不会让某一家独占。
            _fill_missing_images(_needmeta, "cn-noimg", max_fetch=96)
        before = len(items)
        cn_only = [it for it in items if it.get("image")]
        print(f"[ok] {region}: 补图后仍无图 {before - len(cn_only)} 条已剔除，剩 {len(cn_only)} 条（低质图后台异步剔除）")
        # 【2026-10-07 修 死代码】BIG_IMAGE_CHANNELS 本意是让 mefcl 去文章页换大图，
        # 但 _fill_missing_images 一直只对热榜调用过，**国内源从来没调** →
        # mefcl 永远停在列表页那张 220x150 缩略图上 → 被高清门槛剔除 →
        # 整个「软件」栏目消失（用户反馈「软件栏目去哪里了，我重点关注的网站去哪里了」）。
        _big = [it for it in cn_only if it.get("channel") in BIG_IMAGE_CHANNELS]
        if _big:
            _fill_missing_images(_big, "cn-big", max_fetch=24)
        # 合并实时热榜（澎湃/红星/凤凰）：文本榜无图，必须在无图过滤之后并入，否则被剔掉。
        # 热榜整体置顶，各平台条目已混合排序。
        try:
            hot = PRELOAD.get("hot") or _collect("hot")
            hot = [h for h in hot if h.get("image")]   # 没图的不并入，否则违反「没图不要」
            seen_t = {it["title"] for it in cn_only}
            hot = [h for h in hot if h["title"] not in seen_t]
            if hot:
                print(f"[ok] {region}: 合并实时热点 {len(hot)} 条")
            # 去「割裂」：把 红星/凤凰 的实时热点也并入「要闻」栏目（与热榜共享同一批内容），
            # 否则要闻只有界面+澎湃两家，红星/凤凰被锁死在热榜、永不进要闻。
            # 各取前 10 条（按热榜百分位排名），避免淹没要闻里界面/澎湃的常驻源。
            for lbl in ("红星新闻", "凤凰网"):
                cnt = 0
                for h in hot:
                    if h.get("label") == lbl:
                        d = dict(h)
                        d["cls"] = "要闻"
                        cn_only.append(d)
                        cnt += 1
                        if cnt >= 10:
                            break
        except Exception as ex:
            hot = []
            print(f"[warn] 合并实时热点失败: {type(ex).__name__}")
        # 注意：hot 不能再按 rank 重排——_parse_hot 里已按「平台内归一化 score」做过跨平台混合排序，
        # 直接沿用它的顺序即可（rank 是平台内名次，跨平台无可比性）。
        cn_only.sort(key=lambda x: x["published"], reverse=True)
        _merged_all = list(hot) + cn_only
        PRELOAD_ALL[region] = _merged_all
        # 用户要求：国内「要闻」里的国际新闻全部移到国际板块
        _dom, _intl = _split_cn_scope(_merged_all)
        if _intl:
            print(f"[ok] {region}: 要闻里剔出 {len(_intl)} 条国际新闻 → 转给国际板块")
        return _select_fresh(_dom, region)
    items.sort(key=lambda x: x["published"], reverse=True)

    # 国际实时热榜置顶，其后国际 RSS 卡片按时间倒序
    try:
        hot = _parse_hot_intl()
        # 【加速 2026-10-07】同样先按时间筛掉旧闻再补图（理由见上面 hot 分支）。
        _icut = datetime.now(CST).timestamp() - MAX_AGE_HOURS.get("intl", 48) * 3600
        hot = [h for h in hot if not h.get("published") or h["published"] >= _icut]
        # 2026-10-07：国际热榜同样是纯文字榜（当时那一路是境外热榜），
        # 按「每条新闻必须有高清大图」的规矩，必须先补图，补不到的丢掉。
        hot = _fill_missing_images(hot, f"{region}-hot", max_fetch=80, force_all=True)
        hot = [h for h in hot if h.get("image")]
        seen_t = {it["title"] for it in items}
        hot = [h for h in hot if h["title"] not in seen_t]
        if hot:
            print(f"[ok] {region}: 合并国际热榜 {len(hot)} 条（均已带图）")
    except Exception as ex:
        hot = []
        print(f"[warn] 合并国际热榜失败: {type(ex).__name__}")

    if region == "intl":
        # 【2026-10-09 换源】国际板块改用境内媒体的国际版后，中新网系 RSS **本身不带图**，
        # 与国内板块同一套做法：先给缺图的条目去正文页取首图
        # （限时 3 秒/条、结果进 _ART_CACHE 缓存、取不到就跳过该条），
        # 补完仍然没图的才按「没图不要」剔除。
        _needmeta = [it for it in items if not it.get("image")]
        if _needmeta:
            _fill_missing_images(_needmeta, "intl-noimg", max_fetch=48)
        before = len(items)
        items = [it for it in items if it.get("image")]
        print(f"[ok] {region}: 补图后剔除无图 {before - len(items)} 条，剩 {len(items)} 条"
              f"（低质图后台异步剔除）")
    PRELOAD_ALL[region] = hot + items
    _note_item_hosts(hot + items)  # 【2026-10-09 修 高-1】发布前登记图片域名（/img 白名单）
    return _select_fresh(hot + items, region)


def _enrich_quality_bg(region):
    """后台异步：下载题图测真实尺寸，剔除低质图（短边过小）。测不出则保留，不因网络误杀。"""
    # 【2026-10-09 修 中-7】同一 region 同一时刻只允许一轮剔除：
    # 刷新循环与按需抓取都可能各起一个线程，两轮同时下载同一批图纯属浪费。
    with _ENRICH_LOCK:
        if region in _ENRICHING:
            print(f"[ok] {region}: 已有一轮低质图剔除在进行，本轮跳过")
            return
        _ENRICHING.add(region)
    try:
        items = PRELOAD.get(region) or []
        if not items:
            return
        with ThreadPoolExecutor(max_workers=16) as ex:
            # 【2026-10-07 定论】mefcl 的题图**站方自己就是 220x150** ——
            # 实测列表页的 <img data-src> 和文章页 og:image 是同一个文件（220x150），
            # 去正文页换大图也换不出更大的。所以它**永远达不到 300 的高清线**。
            # 要么给这个站开豁免（图偏小但能用），要么整个「软件」栏目消失。
            # 用户明确说「我重点关注的网站」就是 mefcl，所以选择开豁免（仍必须有图）。
            # 其余源一个都不放过，照旧按 MIN_IMG_SHORT_SIDE 剔除。
            keep = list(ex.map(
                lambda it: (_img_ok(it["image"]) if it.get("image") else False)
                or (it.get("channel") in MIN_IMG_EXEMPT_CHANNELS and bool(it.get("image"))),
                items))
        pruned = [it for it, k in zip(items, keep) if k]
        dropped = len(items) - len(pruned)
        if dropped:
            print(f"[ok] {region}: 后台剔除低质图 {dropped} 条，剩 {len(pruned)} 条")
        # 【2026-10-09 修 高-1】发布前把这批图的域名登记进 /img 白名单：
        # 白名单要在浏览器来取图**之前**就已经登记好，否则我们自己的图也会被 403 挡掉。
        _note_item_hosts(pruned)
        # 【2026-10-09 修 中-7 丢失更新竞态】上面下载测尺寸要花好几秒，这期间
        # refresh_loop（或按需抓取）完全可能已经把 PRELOAD[region] 换成**新的一批**。
        # 原来无条件 `PRELOAD[region] = pruned` 会把新数据盖回旧的这一批（页面数据回退一轮）。
        # 现在：只有「还是我开始时的那一批」才整体写回；否则只把**这一批里被剔除的 id**
        # 从新数据里摘掉（等于只同步交集，绝不覆盖）。
        cur = PRELOAD.get(region)
        if cur is items:
            PRELOAD[region] = pruned
        else:
            drop_ids = {it.get("id") for it in items if it.get("id")} - \
                       {it.get("id") for it in pruned if it.get("id")}
            if drop_ids:
                PRELOAD[region] = [it for it in (cur or []) if it.get("id") not in drop_ids]
            print(f"[ok] {region}: 剔除完成时数据已换新，只同步被剔除的 {len(drop_ids)} 条"
                  f"（没有覆盖新数据）")
    finally:
        with _ENRICH_LOCK:
            _ENRICHING.discard(region)


# ===================== Windows 右下角气泡通知（纯 ctypes，无第三方依赖） =====================
# 非 Windows / 创建失败均静默降级，不影响主程序。点击气泡打开 APP_URL。
_AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_NAME = "hotnews"


def _autostart_enabled():
    """开机自启是否已开（读当前用户的 Run 项，不需要管理员权限）。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as k:
            v, _ = winreg.QueryValueEx(k, _AUTOSTART_NAME)
            return bool(v)
    except Exception:
        return False


def _autostart_set(enable):
    """开/关开机自启。返回 (是否成功, 说明)。

    【2026-10-07 用户要求「第一次使用时界面给个开关让用户选」】
    开启时用 --silent 启动：开机只在后台把服务和第一屏数据准备好，不弹浏览器；
    用户想看时点托盘右键「打开热点新闻」就是秒开。
    """
    try:
        import winreg
        import sys as _sys
        exe = _sys.executable if getattr(_sys, "frozen", False) else ""
        if enable and not exe:
            return False, "非打包环境，无法设置开机启动"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_KEY) as k:
            if enable:
                winreg.SetValueEx(k, _AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  f'"{exe}" --silent')
            else:
                try:
                    winreg.DeleteValue(k, _AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        print(f"[ok] 开机自启已{'开启' if enable else '关闭'}")
        return True, "已开启开机启动" if enable else "已关闭开机启动"
    except Exception as ex:
        return False, f"设置失败: {type(ex).__name__}"


def _open_app_window():
    """把程序页面打开（托盘菜单、气泡点击都用它）。
    优先 ShellExecuteW —— Windows 上打开 URL 最可靠；失败退回 webbrowser。"""
    url = APP_URL or "http://127.0.0.1:8000"
    try:
        import ctypes as _ct
        if _ct.windll.shell32.ShellExecuteW(None, "open", url, None, None, 1) > 32:
            return
    except Exception:
        pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def _install_ready_update():
    """退出前调用：若已有一个「下载完 + 验签验 sha 都通过」的新版本，现在就把它装上。

    【2026-10-09 升级体验口径】自动升级的落地时机之一就是这个 ——
    用户退出程序时**自动**完成替换，不需要点任何按钮、不弹任何确认框。
    （另一个时机见 _warmup 的 check_update：后台静默实例发现新版会立刻装。）
    返回 True 表示已经交给替换流程（调用方可以放心退出了）。
    注意：这里只做「取回已就绪的替换」，验证与 fail-closed 闸门全在 updater.trigger_replace 里，
    所以"没有待安装的新版本"或"校验没过"时它会老实返回 False，退出的还是原来那份程序。
    """
    try:
        if not (updater.state().get("latest") or {}).get("version"):
            return False            # 没有待安装的新版本：静默退出，什么都不做
        ok = updater.trigger_replace()
        if ok:
            print("[update] 退出前已自动完成替换（无确认框）")
        else:
            print(f"[warn] 退出前安装未执行：{updater.last_message()}")
        return bool(ok)
    except Exception as ex:
        print(f"[warn] 退出前安装异常: {type(ex).__name__}")
        return False


def _quit_app_now():
    """退出程序（跟网页上「退出程序」按钮同一条路）。

    【2026-10-09 升级体验口径】退出前先把「已就绪的新版本」自动装上 ——
    这是自动升级真正落地的时刻（页面上的「退出程序」与托盘菜单的「退出程序」都走这里）。
    """
    _install_ready_update()
    def _die():
        time.sleep(0.2)
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()

class _Tray:
    def __init__(self):
        self.ok = False
        self.hwnd = None
        self.nid = None
        self.shell32 = None
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            self.ctypes = ctypes
            self.wintypes = wintypes
            self.WM_TRAY = 0x8000 + 100
            self.NIM_ADD, self.NIM_MODIFY, self.NIM_DELETE = 0x0, 0x1, 0x2
            self.NIF_MESSAGE, self.NIF_ICON, self.NIF_INFO, self.NIF_TIP = 0x1, 0x2, 0x10, 0x4
            self.NIN_BALLOONUSERCLICK = 0x405

            # 【2026-10-07 修致命 bug】原来这里用了三个 ctypes.wintypes 里根本不存在的名字：
            #     wintypes.wchar    → 不存在（正确是 WCHAR）
            #     wintypes.LRESULT  → 不存在（正确是 c_ssize_t）
            #     wintypes.WNDCLASS → 不存在（得自己定义 WNDCLASSW）
            # 任一个都会让 __init__ 抛 AttributeError，self.ok 永远 False，
            # 而失败是「静默降级」，日志里只留一行异常类型名 —— 所以这个程序的
            # 右下角托盘和气泡「从来就没工作过」，用户一直以为是自己没设置对。
            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                         wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            self.WNDPROC = WNDPROC

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            self.WNDCLASSW = WNDCLASSW

            class NOTIFYICONDATA(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT),
                    ("hIcon", wintypes.HANDLE),
                    ("szTip", wintypes.WCHAR * 128),
                    ("dwState", wintypes.DWORD),
                    ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256),
                    ("uVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64),
                    ("dwInfoFlags", wintypes.DWORD),
                    ("guidItem", ctypes.c_byte * 16),
                    ("hBalloonIcon", wintypes.HANDLE),
                ]
            self.NOTIFYICONDATA = NOTIFYICONDATA
            self.user32 = ctypes.windll.user32
            self.shell32 = ctypes.windll.shell32
            self.kernel32 = ctypes.windll.kernel32

            # 【2026-10-07 修】ctypes 对没声明原型的函数一律按 32 位 int 传参，
            # 而 LPARAM/WPARAM 和句柄在 x64 上都是 64 位 —— 后果有两个：
            #   1) 窗口消息里稍大的 lp 会报 "int too long to convert"，窗口过程直接失效
            #   2) 返回句柄的函数会被截断成 32 位，句柄可能失真
            # 所以下面这些必须显式声明 argtypes / restype。
            u, s, k = self.user32, self.shell32, self.kernel32
            u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                         wintypes.WPARAM, wintypes.LPARAM]
            u.DefWindowProcW.restype = ctypes.c_ssize_t
            u.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
            u.CreateWindowExW.restype = wintypes.HWND
            u.RegisterClassW.argtypes = [wintypes.LPVOID]
            u.RegisterClassW.restype = wintypes.ATOM
            u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT]
            u.LoadImageW.restype = wintypes.HANDLE
            u.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
            u.LoadIconW.restype = wintypes.HICON
            s.Shell_NotifyIconW.argtypes = [wintypes.DWORD, wintypes.LPVOID]
            s.Shell_NotifyIconW.restype = wintypes.BOOL
            k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            k.GetModuleHandleW.restype = wintypes.HMODULE
            self.ok = True
        except Exception as ex:
            # 同上：打完整原因，别再让托盘静默失效
            import traceback
            print(f"[warn] 托盘初始化失败（通知降级）: {type(ex).__name__}: {ex}")
            traceback.print_exc()

    def _wndproc(self, hwnd, msg, wp, lp):
        user32 = self.user32
        if msg == self.WM_TRAY:
            # lp 低字为通知码；用户点击气泡 → 打开应用（APP_URL 由 app.py 写入真实端口）
            _low = lp & 0xFFFF
            if _low == self.NIN_BALLOONUSERCLICK:
                _open_app_window()
            elif _low == 0x0205:
                # 【2026-10-07 用户要求「右下角图标右键弹出退出和打开软件的菜单」】
                # WM_RBUTTONUP：在托盘图标上点右键 → 弹出菜单
                self._popup_menu(hwnd)
        if msg == 0x2:  # WM_DESTROY
            user32.PostQuitMessage(0)
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _popup_menu(self, hwnd):
        """托盘图标右键菜单：打开热点新闻 / 退出程序。

        两个坑：
        1) 弹菜单前必须先 SetForegroundWindow，否则菜单点完不消失（Windows 的老规矩）；
        2) 用 TPM_RETURNCMD 让 TrackPopupMenu 直接返回选中的命令号，比等 WM_COMMAND 简单可靠。
        """
        u = self.user32
        wintypes = self.wintypes
        try:
            hmenu = u.CreatePopupMenu()
            u.AppendMenuW(hmenu, 0x00000000, 1, "打开热点新闻")
            u.AppendMenuW(hmenu, 0x00000800, 0, None)          # 分隔线
            u.AppendMenuW(hmenu, 0x00000000, 2, "退出程序")
            pt = wintypes.POINT()
            u.GetCursorPos(self.ctypes.byref(pt))   # ctypes 在这个类里是 self.ctypes
            u.SetForegroundWindow(hwnd)
            cmd = u.TrackPopupMenu(hmenu, 0x0002 | 0x0100,      # RIGHTALIGN | RETURNCMD
                                   pt.x, pt.y, 0, hwnd, None)
            u.PostMessageW(hwnd, 0, 0, 0)                       # 让菜单正常收起
            u.DestroyMenu(hmenu)
            if cmd == 1:
                print("[ok] 托盘菜单：打开热点新闻")
                _open_app_window()
            elif cmd == 2:
                print("[ok] 托盘菜单：退出程序")
                _quit_app_now()
        except Exception as ex:
            print(f"[warn] 托盘菜单失败: {type(ex).__name__}: {ex}")

    def _load_icon(self):
        """取一个能显示的通知区图标。

        2026-10-07 新增：原来 uFlags 里根本没有 NIF_ICON、hIcon 也是空的，
        按 Win32 规范这种通知区图标是空白的（用户反馈看不到右下角图标）。
        顺序：同目录 hotnews.ico → 从 exe 自身资源里取（打包态） → 系统默认图标兜底。
        """
        user32 = self.user32
        IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
        try:
            p = _res("hotnews.ico")
            if os.path.exists(p):
                h = user32.LoadImageW(None, p, IMAGE_ICON, 0, 0,
                                      LR_LOADFROMFILE | LR_DEFAULTSIZE)
                if h:
                    return h
        except Exception:
            pass
        try:
            # 打包成 exe 后，图标就在 exe 自己的资源里，资源 ID 通常是 1。
            # GetModuleHandleW 属于 kernel32（不是 user32）。
            hinst = self.ctypes.windll.kernel32.GetModuleHandleW(None)
            h = user32.LoadIconW(hinst, 1)
            if h:
                return h
        except Exception:
            pass
        try:
            return user32.LoadIconW(None, 32512)      # IDI_APPLICATION
        except Exception:
            return None

    def start(self):
        if not self.ok:
            return
        threading.Thread(target=self._thread, daemon=True).start()

    def _thread(self):
        try:
            ctypes = self.ctypes
            wintypes = self.wintypes
            user32 = self.user32
            WNDPROC = self.WNDPROC
            # 必须把回调对象挂在 self 上：只塞进结构体的话会被 GC 回收，
            # 之后窗口过程变成野指针，进程直接崩。
            self._wndproc_ref = WNDPROC(self._wndproc)
            wc = self.WNDCLASSW()
            # 注意：GetModuleHandleW 在 kernel32 里，不在 user32 ——
            # 写成 user32.GetModuleHandleW 会抛 AttributeError: function not found
            wc.hInstance = ctypes.windll.kernel32.GetModuleHandleW(None)
            wc.lpszClassName = "HotNewsTrayWnd"
            wc.lpfnWndProc = self._wndproc_ref
            user32.RegisterClassW(ctypes.byref(wc))
            hwnd = user32.CreateWindowExW(0, "HotNewsTrayWnd", "hotnews_tray", 0,
                                          0, 0, 0, 0, None, None, wc.hInstance, None)
            self.hwnd = hwnd
            nid = self.NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(self.NOTIFYICONDATA)
            nid.hWnd = hwnd
            nid.uID = 1
            # 2026-10-07 修复：原来只设 NIF_MESSAGE|NIF_TIP，没设 NIF_ICON、hIcon 也没赋值，
            # 通知区图标是空白的。现在补上图标。
            nid.uFlags = self.NIF_MESSAGE | self.NIF_TIP | self.NIF_ICON
            nid.uCallbackMessage = self.WM_TRAY
            nid.szTip = "热点新闻"
            nid.hIcon = self._load_icon()
            self.nid = nid
            self.shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid))
            print("[ok] 托盘就绪（图标已设，点击气泡可打开应用）")
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as ex:
            # 原来只打异常类型名（如 "AttributeError"），把真正原因吞掉了 ——
            # 托盘因此静默失效很久都没人发现。现在打完整堆栈。
            import traceback
            print(f"[warn] 托盘线程异常（通知降级）: {type(ex).__name__}: {ex}")
            traceback.print_exc()
            self.ok = False


TRAY = _Tray()


# ================= 右下角自绘气泡（2026-10-07 新增，替代系统气泡） =================
# 【为什么必须自己画】
#   本机实测：Windows 11 (build 26300) 且 ToastEnabled=0（系统通知总开关关闭）。
#   老式 Shell_NotifyIcon 气泡在 Win11 上支持不可靠，加上通知关闭，会被系统静默丢弃 ——
#   表现就是"代码返回成功，但用户什么都看不见"（用户实际反馈就是这个）。
#   所以这里自绘一个置顶小窗：不经过通知中心，关掉通知也一定看得见。
#   点击气泡 → 打开应用；9 秒后自动消失；多条通知排队依次显示。
BALLOON_W, BALLOON_H = 400, 118
# 【2026-10-07 用户要求「三行不过瘾，显示六行，间隔延长两秒」】
# 停留 5200 → 7200 毫秒；一组 3 条 → 6 条。
BALLOON_PER_ITEM_MS = 7200   # 每条新闻停留多久（毫秒）
BALLOON_PLAY_COUNT = 6       # 一条气泡里滚动播放几条


class _Balloon:
    def __init__(self):
        self.ok = False
        self.q = []                 # 队列里每一项是「一组要滚动播放的条目」[(title,msg,warn), ...]
        self.lock = threading.Lock()
        self.hwnd = None
        self.cur = ("", "", False, "")
        self.showing = False
        self.playlist = []
        self.hover = False            # 鼠标是否停在气泡上（悬停才显示「打开/退出」)
        self.paused = False           # 悬停期间暂停自动关闭（否则来不及点按钮）
        self._btn_open = None         # 两个按钮的位置，点击时做命中判断
        self._btn_close = None
        self._tracking = False        # 是否已登记 WM_MOUSELEAVE
        self.idx = 0                # 播到第几条
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            self.ctypes, self.wintypes = ctypes, wintypes
            u, g, k = (ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.kernel32)
            self.u, self.g, self.k = u, g, k

            # 【2026-10-07 修「毛刺感」】显示器是 125% 缩放，但进程没声明 DPI 感知时，
            # Windows 会把窗口按 100% 画好再整块放大 —— 文字因此发虚、边缘起毛刺。
            # 声明 DPI 感知后按物理像素绘制，再把尺寸/字号按缩放比放大，字就实了。
            try:
                u.SetProcessDPIAware()
            except Exception:
                pass
            try:
                u.GetDC.argtypes = [wintypes.HWND]
                u.GetDC.restype = wintypes.HDC
                u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
                g.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
                g.GetDeviceCaps.restype = ctypes.c_int
                _hdc = u.GetDC(None)
                _dpi = g.GetDeviceCaps(_hdc, 88)      # LOGPIXELSX
                u.ReleaseDC(None, _hdc)
                self.scale = max(1.0, min(3.0, (_dpi or 96) / 96.0))
            except Exception:
                self.scale = 1.0
            s = self.scale
            self.W = int(BALLOON_W * s)
            self.H = int(BALLOON_H * s)
            self.pad = int(16 * s)
            print(f"[ok] 气泡缩放比 = {s:.2f}（{int(s * 100)}%），尺寸 {self.W}x{self.H}")

            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                         wintypes.WPARAM, wintypes.LPARAM)
            self.WNDPROC = WNDPROC

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                            ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            class PAINTSTRUCT(ctypes.Structure):
                _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
                            ("rcPaint", RECT), ("fRestore", wintypes.BOOL),
                            ("fIncUpdate", wintypes.BOOL),
                            ("rgbReserved", ctypes.c_byte * 32)]

            self.WNDCLASSW, self.RECT, self.PAINTSTRUCT = WNDCLASSW, RECT, PAINTSTRUCT

            # --- 原型：x64 下句柄/LPARAM 是 64 位，不声明会被截断或报 too long to convert ---
            u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                         wintypes.WPARAM, wintypes.LPARAM]
            u.DefWindowProcW.restype = ctypes.c_ssize_t
            u.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
            u.CreateWindowExW.restype = wintypes.HWND
            u.RegisterClassW.argtypes = [wintypes.LPVOID]
            u.RegisterClassW.restype = wintypes.ATOM
            u.BeginPaint.argtypes = [wintypes.HWND, wintypes.LPVOID]
            u.BeginPaint.restype = wintypes.HDC
            u.EndPaint.argtypes = [wintypes.HWND, wintypes.LPVOID]
            u.GetClientRect.argtypes = [wintypes.HWND, wintypes.LPVOID]
            u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            u.SetTimer.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.LPVOID]
            u.SetTimer.restype = wintypes.UINT
            u.KillTimer.argtypes = [wintypes.HWND, wintypes.UINT]
            u.DestroyWindow.argtypes = [wintypes.HWND]
            u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            u.InvalidateRect.argtypes = [wintypes.HWND, wintypes.LPVOID, wintypes.BOOL]
            u.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                                    wintypes.LPVOID, wintypes.UINT]
            u.DrawTextW.restype = ctypes.c_int
            u.FillRect.argtypes = [wintypes.HDC, wintypes.LPVOID, wintypes.HBRUSH]
            u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                                wintypes.LPVOID, wintypes.UINT]
            u.LoadImageW.restype = wintypes.HANDLE
            u.LoadIconW.restype = wintypes.HICON
            g.CreateSolidBrush.argtypes = [wintypes.COLORREF]
            g.CreateSolidBrush.restype = wintypes.HBRUSH
            g.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPCWSTR]
            g.CreateFontW.restype = wintypes.HANDLE
            g.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
            g.SelectObject.restype = wintypes.HANDLE
            g.DeleteObject.argtypes = [wintypes.HANDLE]
            g.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
            g.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
            k.GetModuleHandleW.restype = wintypes.HMODULE
            # 浅色卡片要用的 GDI 函数（圆角、描边、画小图标）
            g.CreatePen.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]
            g.CreatePen.restype = wintypes.HANDLE
            g.RoundRect.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
            g.CreateRoundRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_int, ctypes.c_int, ctypes.c_int]
            g.CreateRoundRectRgn.restype = wintypes.HANDLE
            u.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HANDLE, wintypes.BOOL]
            u.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HICON,
                                     ctypes.c_int, ctypes.c_int, wintypes.UINT,
                                     wintypes.HBRUSH, wintypes.UINT]
            # 注意：GetStockObject 属于 gdi32（不是 user32），写错会 AttributeError
            g.GetStockObject.argtypes = [ctypes.c_int]
            g.GetStockObject.restype = wintypes.HANDLE
            self.ok = True
        except Exception as ex:
            import traceback
            print(f"[warn] 自绘气泡初始化失败: {type(ex).__name__}: {ex}")
            traceback.print_exc()

    def _icon(self):
        u = self.u
        for cand in (_res("hotnews.ico"), None):
            try:
                if cand and os.path.exists(cand):
                    h = u.LoadImageW(None, cand, 1, 0, 0, 0x10 | 0x40)
                    if h:
                        return h
            except Exception:
                pass
        try:
            return u.LoadIconW(self.k.GetModuleHandleW(None), 1)
        except Exception:
            return None

    def _wndproc(self, hwnd, msg, wp, lp):
        ctypes, wintypes = self.ctypes, self.wintypes
        u, g = self.u, self.g
        try:
            if msg == 0x000F:                      # WM_PAINT
                ps = self.PAINTSTRUCT()
                hdc = u.BeginPaint(hwnd, ctypes.byref(ps))
                rc = self.RECT()
                u.GetClientRect(hwnd, ctypes.byref(rc))
                title, text, warn = self.cur[0], self.cur[1], self.cur[2]
                s = self.scale
                # 卡片底色：明快浅色（跟程序网页风格一致，不用黑底）。
                # 大事样式用暖白底 + 橙条区分，一眼能看出"这条不一样"。
                u.FillRect(hdc, ctypes.byref(rc), self.br_bg_warn if warn else self.br_bg)
                # 左侧彩色强调条
                bar = self.RECT(0, 0, int(5 * s), rc.bottom)
                u.FillRect(hdc, ctypes.byref(bar), self.br_warn if warn else self.br_info)
                # 1px 浅灰圆角描边，让卡片在浅色桌面上有边界感
                op = g.SelectObject(hdc, self.pen_border)
                ob = g.SelectObject(hdc, self.null_brush)
                g.RoundRect(hdc, 0, 0, rc.right, rc.bottom, int(16 * s), int(16 * s))
                g.SelectObject(hdc, op)
                g.SelectObject(hdc, ob)
                g.SetBkMode(hdc, 1)                # TRANSPARENT
                # 左上角小图标（有就用，没有就把标题左移）
                x0 = int(20 * s)
                if self.hicon16:
                    _ic = int(18 * s)
                    u.DrawIconEx(hdc, int(19 * s), int(17 * s), self.hicon16, _ic, _ic, 0, None, 3)
                    x0 = int(46 * s)
                right = rc.right - int(16 * s)
                # 标题（深色、加粗）
                g.SelectObject(hdc, self.font_title)
                g.SetTextColor(hdc, 0x2C201A)      # #1A202C
                t = self.RECT(x0, int(13 * s), right, int(41 * s))
                u.DrawTextW(hdc, title, -1, ctypes.byref(t),
                            0x20 | 0x4 | 0x8000 | 0x800)     # SINGLELINE|VCENTER|ELLIPSIS|NOPREFIX
                # 正文（中灰、加粗）
                g.SelectObject(hdc, self.font_msg)
                g.SetTextColor(hdc, 0x68554A)      # #4A5568
                m = self.RECT(x0, int(47 * s), right, rc.bottom - int(10 * s))
                u.DrawTextW(hdc, text, -1, ctypes.byref(m),
                            0x10 | 0x800)                    # WORDBREAK|NOPREFIX

                # 【2026-10-07 用户要求「鼠标指向，显示退出和打开的字样」】
                # 鼠标悬停在气泡上时，右下角浮出两个胶囊按钮；移开就消失。
                if self.hover:
                    bw, bh = int(56 * s), int(26 * s)
                    gap = int(8 * s)
                    by = rc.bottom - int(36 * s)
                    ex = rc.right - int(14 * s)
                    self._btn_close = (ex - bw, by, ex, by + bh)
                    self._btn_open = (ex - bw - gap - bw, by, ex - bw - gap, by + bh)
                    for _r, _fill, _txt, _fg in (
                            (self._btn_open, self.br_btn_open, "打开", 0xFFFFFF),
                            (self._btn_close, self.br_btn_close, "退出", 0x2C201A)):
                        _br = g.SelectObject(hdc, _fill)
                        _pn = g.SelectObject(hdc, self.pen_border)
                        g.RoundRect(hdc, _r[0], _r[1], _r[2], _r[3], bh, bh)
                        g.SelectObject(hdc, _br)
                        g.SelectObject(hdc, _pn)
                        g.SelectObject(hdc, self.font_btn)
                        g.SetTextColor(hdc, _fg)
                        _tr = self.RECT(_r[0], _r[1], _r[2], _r[3])
                        u.DrawTextW(hdc, _txt, -1, ctypes.byref(_tr),
                                    0x1 | 0x4 | 0x800)       # CENTER|VCENTER|NOPREFIX
                u.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if msg == 0x0113:                      # WM_TIMER
                if wp == 1:                        # 轮询队列：取下一组来播
                    pl = None
                    with self.lock:
                        if self.q and not self.showing:
                            pl = self.q.pop(0)
                    if pl:
                        self.playlist = pl
                        self.idx = 0
                        self._apply_current()
                        self._place()
                        u.ShowWindow(hwnd, 4)       # SW_SHOWNOACTIVATE
                        u.SetWindowPos(hwnd, -1, self.x, self.y, self.W, self.H,
                                       0x0010 | 0x0040)          # NOACTIVATE|SHOWWINDOW
                        u.InvalidateRect(hwnd, None, True)
                        self.showing = True
                        # 整组停留时间 = 每条停留 × 条数
                        u.SetTimer(hwnd, 2, BALLOON_PER_ITEM_MS * len(pl), None)
                        if len(pl) > 1:
                            u.SetTimer(hwnd, 3, BALLOON_PER_ITEM_MS, None)   # 轮换计时器
                elif wp == 2:                      # 整组播完 → 隐藏
                    u.KillTimer(hwnd, 2)
                    u.KillTimer(hwnd, 3)
                    u.ShowWindow(hwnd, 0)           # SW_HIDE
                    self.showing = False
                    self.playlist = []
                elif wp == 3:                      # 滚动到下一条
                    self.idx += 1
                    if self.idx < len(self.playlist):
                        self._apply_current()
                        u.InvalidateRect(hwnd, None, True)
                    else:
                        u.KillTimer(hwnd, 3)
                return 0
            if msg == 0x0200:                      # WM_MOUSEMOVE —— 悬停
                # 【2026-10-07 用户要求「鼠标指向，显示退出和打开的字样」】
                if not self.hover:
                    self.hover = True
                    u.InvalidateRect(hwnd, None, True)
                    # 【2026-10-07 用户反馈「没有过」】关键修复：鼠标移上来就**暂停自动关闭**。
                    # 原来气泡每条只停 5.2 秒，用户根本来不及把鼠标移过去点按钮。
                    try:
                        u.KillTimer(hwnd, 2)      # 整组停留计时器
                        u.KillTimer(hwnd, 3)      # 轮换计时器
                        self.paused = True
                    except Exception:
                        pass
                if not self._tracking:
                    # 登记一次 WM_MOUSELEAVE，鼠标移开时把按钮收起来。
                    # （Windows 不会主动发 leave，必须先 TrackMouseEvent 登记，且每次移入都要重登记。）
                    try:
                        class _TME(ctypes.Structure):
                            _fields_ = [("cbSize", wintypes.DWORD),
                                        ("dwFlags", wintypes.DWORD),
                                        ("hwndTrack", wintypes.HWND),
                                        ("dwHoverTime", wintypes.DWORD)]
                        _t = _TME()
                        _t.cbSize = ctypes.sizeof(_TME)
                        _t.dwFlags = 0x00000002          # TME_LEAVE
                        _t.hwndTrack = hwnd
                        _t.dwHoverTime = 0
                        if u.TrackMouseEvent(ctypes.byref(_t)):
                            self._tracking = True
                    except Exception:
                        pass
                return 0
            if msg == 0x02A3:                      # WM_MOUSELEAVE —— 鼠标移开
                self.hover = False
                if getattr(self, "paused", False):
                    # 移开就恢复计时，重新给一整组的时间，别立刻消失
                    try:
                        u.SetTimer(hwnd, 2, BALLOON_PER_ITEM_MS * max(1, len(self.playlist)), None)
                        if len(self.playlist) > 1:
                            u.SetTimer(hwnd, 3, BALLOON_PER_ITEM_MS, None)
                        self.paused = False
                    except Exception:
                        pass
                self._tracking = False
                self._btn_open = None
                self._btn_close = None
                u.InvalidateRect(hwnd, None, True)
                return 0
            if msg == 0x0201:                      # WM_LBUTTONDOWN
                # 【2026-10-07 用户反馈「气泡的新闻无法点开」】原来点哪里都只打开程序首页，
                # 看不到那一条新闻。现在优先打开**当前正在播放的这条**的原文链接；
                # 没有链接（比如"我的软件发布"是纯提示、没有跳转目标）才回落到首页。
                # 先看是不是点在两个按钮上（坐标来自上一次绘制）
                def _in(r):
                    return bool(r) and r[0] <= wp <= r[2] and r[1] <= lp <= r[3]
                if _in(self._btn_close):
                    # 「退出」= 收起这条气泡，不打开任何东西
                    u.ShowWindow(hwnd, 0)
                    self.showing = False
                    self.hover = False
                    self._tracking = False
                    u.KillTimer(hwnd, 2)
                    u.KillTimer(hwnd, 3)
                    return 0
                _link = ""
                try:
                    if isinstance(self.cur, (tuple, list)) and len(self.cur) > 3:
                        _link = self.cur[3] or ""
                except Exception:
                    _link = ""
                # 【2026-10-07】原来只用 webbrowser.open，在打包环境里偶尔不生效
                # （用户反馈"气泡点不开"）。改用 ShellExecuteW —— 这是 Windows 上
                # 打开 URL 最直接、最可靠的方式；失败再退回 webbrowser。
                _url = _link or APP_URL
                _done = False
                try:
                    if ctypes.windll.shell32.ShellExecuteW(
                            None, "open", _url, None, None, 1) > 32:
                        _done = True
                except Exception:
                    _done = False
                if not _done:
                    try:
                        webbrowser.open(_url)
                    except Exception:
                        pass
                u.ShowWindow(hwnd, 0)
                self.showing = False
                self.hover = False
                self._tracking = False
                return 0
            if msg == 0x0002:                      # WM_DESTROY
                u.PostQuitMessage(0)
                return 0
        except Exception as ex:
            print(f"[warn] 气泡窗口消息处理异常: {type(ex).__name__}: {ex}")
        return u.DefWindowProcW(hwnd, msg, wp, lp)

    def _apply_current(self):
        """把当前播放到的那一条装进 self.cur，并在标题尾巴标出 (第几条/共几条)。"""
        pl = self.playlist
        if not pl:
            self.cur = ("", "", False, "")
            return
        i = max(0, min(self.idx, len(pl) - 1))
        title, text, warn = pl[i][0], pl[i][1], pl[i][2]
        link = pl[i][3] if len(pl[i]) > 3 else ""
        if len(pl) > 1:
            title = f"{title}    ({i + 1}/{len(pl)})"
        self.cur = (title, text, warn, link)

    def _place(self):
        """放到工作区右下角（避开任务栏）。"""
        rc = self.RECT()
        try:
            self.u.SystemParametersInfoW(0x0030, 0, self.ctypes.byref(rc), 0)   # SPI_GETWORKAREA
        except Exception:
            rc = self.RECT(0, 0, 1920, 1080)
        self.x = max(0, rc.right - self.W - self.pad)
        self.y = max(0, rc.bottom - self.H - self.pad)

    def start(self):
        if not self.ok:
            return
        threading.Thread(target=self._thread, daemon=True).start()

    def _thread(self):
        try:
            ctypes, wintypes = self.ctypes, self.wintypes
            u, g = self.u, self.g
            self._ref = self.WNDPROC(self._wndproc)
            wc = self.WNDCLASSW()
            wc.hInstance = self.k.GetModuleHandleW(None)
            wc.lpszClassName = "HotNewsBalloonWnd"
            wc.lpfnWndProc = self._ref
            u.RegisterClassW(ctypes.byref(wc))
            self.x, self.y = 0, 0
            self._place()
            hwnd = u.CreateWindowExW(
                0x00000008 | 0x00000080 | 0x08000000,     # TOPMOST|TOOLWINDOW|NOACTIVATE
                "HotNewsBalloonWnd", "热点新闻提醒",
                0x80000000,                               # WS_POPUP
                self.x, self.y, self.W, self.H,
                None, None, wc.hInstance, None)
            self.hwnd = hwnd
            s = self.scale
            # GDI 资源建一次就复用，避免每次弹都泄漏。
            # 配色跟程序网页的「明快浅色」一致：白卡片 + 浅灰描边 + 彩色强调条。
            # （原来是 #1F2430 黑底，用户反馈"好丑"，已改掉。）
            self.br_bg = g.CreateSolidBrush(0xFFFFFF)        # 纯白      #FFFFFF
            self.br_bg_warn = g.CreateSolidBrush(0xF1F8FF)   # 暖白      #FFF8F1
            self.br_info = g.CreateSolidBrush(0xB06C2B)      # 蓝        #2B6CB0
            self.br_warn = g.CreateSolidBrush(0x206BDD)      # 橙        #DD6B20
            # 【2026-10-07 用户要求「鼠标指向显示打开/退出」】两个胶囊按钮的底色
            self.br_btn_open = g.CreateSolidBrush(0xB06C2B)   # 蓝 #2B6CB0
            self.br_btn_close = g.CreateSolidBrush(0xF0E8E2)  # 浅灰 #E2E8F0
            self.pen_border = g.CreatePen(0, max(1, int(s)), 0xF0E8E2)   # 浅灰描边 #E2E8F0
            self.null_brush = g.GetStockObject(5)            # NULL_BRUSH（只描边不填充）
            # 字体：字体加粗（weight 700 走真粗体，不用 GDI 合成，避免发糊），
            # 并用 CLEARTYPE_QUALITY(5) + OUT_TT_PRECIS(5) 开抗锯齿。
            # 原来的 iQuality=0(DEFAULT) 在缩放屏上会出现锯齿/毛刺。
            self.font_title = g.CreateFontW(-int(19 * s), 0, 0, 0, 700, 0, 0, 0,
                                            134, 5, 0, 5, 0, "Microsoft YaHei UI")
            self.font_btn = g.CreateFontW(-int(14 * s), 0, 0, 0, 700, 0, 0, 0,
                                          134, 5, 0, 5, 0, "Microsoft YaHei UI")
            self.font_msg = g.CreateFontW(-int(16 * s), 0, 0, 0, 700, 0, 0, 0,
                                          134, 5, 0, 5, 0, "Microsoft YaHei UI")
            # 小图标（取不到就算了，标题会自动左移）
            self.hicon16 = None
            try:
                _p = _res("hotnews.ico")
                if os.path.exists(_p):
                    self.hicon16 = u.LoadImageW(None, _p, 1, int(18 * s), int(18 * s), 0x10)
            except Exception:
                pass
            # 圆角：GDI 画出来是直角，用 window region 切成圆角
            try:
                _hrgn = g.CreateRoundRectRgn(0, 0, self.W + 1, self.H + 1,
                                             int(16 * s), int(16 * s))
                if _hrgn:
                    u.SetWindowRgn(hwnd, _hrgn, True)
            except Exception:
                pass
            u.SetTimer(hwnd, 1, 300, None)                 # 每 300ms 看一次队列
            print("[ok] 自绘气泡就绪（右下角，不依赖系统通知）")
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        except Exception as ex:
            import traceback
            print(f"[warn] 自绘气泡线程异常: {type(ex).__name__}: {ex}")
            traceback.print_exc()
            self.ok = False


BALLOON = _Balloon()


def _show_toast_list(entries):
    """一条气泡里滚动播放多条：entries = [(title, msg, warn), ...]。

    用户要求「滚动播放气泡新闻」：一组最多 BALLOON_PLAY_COUNT 条（现为 6 条），
    每条停留 BALLOON_PER_ITEM_MS，标题尾部带 (1/3) 这样的进度。
    """
    # 统一成 4 元组 (title, msg, warn, link)；调用方可能只给 3 个元素
    norm = []
    for e in (entries or []):
        if not e or not (e[0] or e[1]):
            continue
        norm.append((e[0], e[1], bool(e[2]) if len(e) > 2 else False,
                     e[3] if len(e) > 3 else ""))
    entries = norm[:BALLOON_PLAY_COUNT]
    if not entries:
        return
    if BALLOON.ok:
        with BALLOON.lock:
            BALLOON.q.append(list(entries))
            # 队列最多压 2 组，避免刷新风暴时排队排到天边
            if len(BALLOON.q) > 2:
                del BALLOON.q[:-2]
        return
    # 自绘不可用时的兜底：系统气泡只能显示第一条
    title, msg, warn = entries[0][0], entries[0][1], entries[0][2]
    if not TRAY.ok or TRAY.nid is None:
        return
    try:
        nid = TRAY.nid
        nid.uFlags = TRAY.NIF_INFO | TRAY.NIF_TIP
        nid.szInfoTitle = (title or "")[:63]
        nid.szInfo = (msg or "")[:255]
        nid.dwInfoFlags = 0x2 if warn else 0x1
        TRAY.shell32.Shell_NotifyIconW(TRAY.NIM_MODIFY, TRAY.ctypes.byref(nid))
    except Exception as ex:
        print(f"[warn] 气泡通知失败: {type(ex).__name__}")


def _show_toast(title, msg, warn=False):
    """右下角弹单条提示（滚动播放的简化封装）。"""
    _show_toast_list([(title, msg, warn)])


# 跨栏目合并的新条目基线 + 节流（两次通知最小间隔）
NEWS_BASELINE = set()
NEWS_LAST_TOAST = 0.0
# 【节流 2026-10-07】刷新仍是每 10 分钟一轮（保证「常看常新」），
# 但普通「更新提醒」气泡没必要每轮都弹 —— 那一天最多 144 次，太吵。
# 改成 30 分钟最多一次（3 轮里最多打扰 1 次，内容照样是最新的）。
# 注意：「新闻大事」和「我的软件发布」两条**不受这个节流限制**，仍然即时。
# 【2026-10-07 用户要求「气泡提示缩短到10分钟轮播，轮播内容不能重复」】
NEWS_TOAST_MIN = 10 * 60        # 原来 30 分钟，改成跟刷新周期一致
NEWS_LOCK = threading.Lock()


def _has_cjk(s):
    """标题里有没有汉字 —— 用来保证气泡只播中文（用户看不懂英文）。"""
    for ch in (s or ""):
        if "\u4e00" <= ch <= "\u9fff":
            return True
    return False

def _notify_news(items):
    """刷新后调用：对比基线找出新增条目，若有则弹一条「新增 N 条」右下角通知。
    首次调用只建立基线不弹；节流窗口内不重复弹。"""
    global NEWS_BASELINE, NEWS_LAST_TOAST
    if not items:
        return
    new_ids = {it.get("id") for it in items if it.get("id")}
    if not new_ids:
        return
    with NEWS_LOCK:
        if not NEWS_BASELINE:
            NEWS_BASELINE.update(new_ids)
            return
        added = new_ids - NEWS_BASELINE
        NEWS_BASELINE.update(new_ids)
        if len(NEWS_BASELINE) > 4000:
            NEWS_BASELINE = set(list(NEWS_BASELINE)[-2000:])
    if not added:
        return
    now = time.time()
    if now - NEWS_LAST_TOAST < NEWS_TOAST_MIN:
        return
    NEWS_LAST_TOAST = now
    fresh = [it for it in items if it.get("id") in added]
    # 【2026-10-07 用户反馈「还是有英文新闻播报」】气泡里**只留中文条目**。
    # 国际热榜里曾有境外英文源，之前"优先中文"只是排序、挡不住它
    # 从热榜那 2 个名额挤进来。这里直接按"标题有没有汉字"过滤，最干脆。
    fresh = [x for x in fresh if _has_cjk(x.get("title"))]
    if not fresh:
        return
    # 【2026-10-07 用户要求「轮播内容不能重复」】优先播没播过的条目；
    # 全都播过一遍了才清空记录、开新一轮。这样连续几轮不会反复看到同几条。
    try:
        _seen = _BALLOON_SHOWN
        _unseen = [x for x in fresh if (x.get("id") or x.get("title")) not in _seen]
        if _unseen:
            fresh = _unseen + [x for x in fresh if x not in _unseen]
        else:
            _seen.clear()
            print("[ok] 气泡轮播：上一轮已播完，开始新一轮")
    except Exception:
        pass
    if not fresh:
        return
    # 【2026-10-07 用户要求：气泡提醒还要包含国际版块】
    # 不能简单「按时间取最新 3 条」—— 国内更新密集，会把 3 个位置全占掉，国际永远轮不到。
    # 改成按版块分配名额：国际先占 1 席、国内占 1 席、热榜占 1 席，
    # 还有空位才按时间补。这样一轮气泡里必定能看见国际。
    picked, used = [], set()

    _labels = []          # 已经选中的来源，用来避免"同一家刷屏"

    def take(cands, maxn=1, no_repeat_src=True):
        """从 cands 里取最多 maxn 条。

        【2026-10-07 用户反馈「快科技不停推送」】默认**同一来源只取一条**：
        快科技这类更新极勤的站点原来会把 6 个位置全占掉，别的源永远轮不到。
        实在没有别的来源可选时才允许重复（第二遍 no_repeat_src=False）。
        """
        for it in cands:
            if len(picked) >= BALLOON_PLAY_COUNT or maxn <= 0:
                return
            if id(it) in used:
                continue
            _lb = it.get("label") or ""
            if no_repeat_src and _lb and _lb in _labels:
                continue
            used.add(id(it))
            if _lb:
                _labels.append(_lb)
            picked.append(it)
            maxn -= 1

    sort_key = lambda x: x.get("published", 0)          # noqa: E731
    # 【2026-10-07 用户反馈「气泡轮播国际新闻都是英文的，我怎么看得懂」】
    # 国际板块里混着两类：①国内媒体报的国际新闻（澎湃/凤凰/红星，**中文**）；
    # ②CGTN 这类对外报道（英文，翻译额度用完时就是英文原文）。
    # 【2026-10-09 换源】境外源已全部删除，②只剩 CGTN 一家，英文条目大幅减少。
    # 气泡优先推第①类 —— 既是国际大事，又是中文，用户能直接看懂。
    _intl_all = sorted([it for it in fresh if it.get("region") == "intl"],
                       key=sort_key, reverse=True)
    _is_cn_src = lambda x: str(x.get("channel") or "").startswith("cn-intl") or bool(x.get("translated"))  # noqa: E731
    intl = [x for x in _intl_all if _is_cn_src(x)] + [x for x in _intl_all if not _is_cn_src(x)]
    cn = sorted([it for it in fresh if it.get("region") == "cn"
                 and it.get("cls") != "热榜"], key=sort_key, reverse=True)
    hot = sorted([it for it in fresh if it.get("cls") == "热榜"], key=sort_key, reverse=True)
    # 【2026-10-07 用户要求「气泡也要有国际板块的新闻播报」+「快科技不停推送」】
    # 原来国际只保证 1 席、剩下 5 席按时间补 —— 结果被更新最勤的国内源刷屏。
    # 现在**按板块平分**（6 席 → 国际 2 / 国内 2 / 热榜 2），并且同来源只取一条。
    _quota = max(1, BALLOON_PLAY_COUNT // 3)
    take(intl, _quota)     # 国际 2 席
    take(cn, _quota)       # 国内 2 席
    take(hot, _quota)      # 热榜 2 席
    # 还有空位：先补没露过面的板块/来源，最后才允许同来源重复
    if len(picked) < BALLOON_PLAY_COUNT:
        take(sorted(fresh, key=sort_key, reverse=True),
             BALLOON_PLAY_COUNT - len(picked), no_repeat_src=True)
    if len(picked) < BALLOON_PLAY_COUNT:
        take(sorted(fresh, key=sort_key, reverse=True),
             BALLOON_PLAY_COUNT - len(picked), no_repeat_src=False)
    # 用户要求「滚动播放气泡新闻，三条左右」：一条气泡里依次滚这几条
    entries = []
    for it in picked[:BALLOON_PLAY_COUNT]:
        tag = "国际" if it.get("region") == "intl" else "国内"
        label = it.get("label") or it.get("cls") or "热点"
        # 国际条目优先用译文（_translate_items 是就地写回 title 的，英文留在 title_en）
        title = it.get("title") or ""
        if it.get("region") == "intl" and not it.get("translated") and it.get("title_en"):
            title = it.get("title_en") or title
        entries.append((f"热点新闻更新 · {tag} · {label}", title[:72], False, it.get("link") or ""))
    if len(added) > len(entries):
        _e = entries[-1]
        entries[-1] = (_e[0], _e[1] + f"　（本次共新增 {len(added)} 条）") + tuple(_e[2:])
    # 记录已播，保证"轮播内容不能重复"
    try:
        for _it in picked:
            _BALLOON_SHOWN.add(_it.get("id") or _it.get("title"))
        if len(_BALLOON_SHOWN) > 3000:
            _BALLOON_SHOWN.clear()
    except Exception:
        pass
    print("[ok] 更新提醒：滚动播放 %d 条（国际 %d / 国内 %d / 热榜 %d）"
          % (len(entries),
             sum(1 for x in picked if x.get("region") == "intl"),
             sum(1 for x in picked if x.get("region") == "cn" and x.get("cls") != "热榜"),
             sum(1 for x in picked if x.get("cls") == "热榜")))
    _show_toast_list(entries)


# ============ 国内热榜的「国际新闻」过滤（2026-10-07 用户反馈） ============
# 用户原话：「国内实时热榜里的新闻全部是外国新闻，和两大板块对不上」
# 根因：凤凰网的热榜抓的是 news.ifeng.com **综合首页**，它的头条被国际新闻占满
# （实测 49 条里绝大多数是特朗普/伊朗/C罗/柬埔寨这类）。
# 而凤凰没有可抓的国内频道（mainland/ 和 society/ 页面结构不同、抓到 0 条），
# 所以只能在内容层面把国际新闻挑出去，国内热榜只留国内条目。
# 说明：这是**关键词黑名单**，做不到 100% 准确；但配上澎湃/红星两个纯国内源，
# 国内热榜的观感就正常了。宁可漏掉一两条国内的，也不要满屏外国新闻。
# 【2026-10-07 回退】这里曾把大厂简称（苹果/微软/谷歌/英伟达/三星/索尼…）也登记成
# 国际特征词，想拦住「苹果一号电脑拍卖」这类。结果**过头了**：中国科技媒体几乎每条
# 都会提到这些公司，国内科技被整个划走、栏目清空（实测 0 条）。
# 教训：判断"是不是国际"要看**事情发生在哪 / 主体是谁**，不能看"提到了哪家公司"。
# 所以 data/keywords.txt 的 [foreign] 组里只留外国机构/奖项，公司名一律不放
#（华为报苹果、小米报高通都是国内科技新闻）。
# ============ 关键词表：数据文件 data/keywords.txt（2026-10-09 从代码正文挪出）============
# 【为什么要挪】下面这几张表原来是**整段写在 .py 正文里**的字符串常量：连续几百个
# 国家 / 地区 / 机构 / 赛事名。Gitee 侧对 server.py 的 raw 取回曾返回 HTTP 451
# （平台内容审查），最可能命中的就是这种「成串专有名词」的密度。
# 现在只把**纯数据**放进 data/keywords.txt，代码里只剩加载逻辑；
# 判定逻辑、阈值、分流顺序一个字没改（分组与原来的常量一一对应）。
#   · `[组名]` 单独一行表示分组；一行一个词；`#` 开头是注释；空行忽略；
#   · 读不到文件时各组退回空集合（fail-safe：不崩，只是不做这一层筛选）；
#   · 打包：build_exe.spec 的 datas 会带上 data/（见那里的注释）。
_KEYWORDS_PATH = _res(os.path.join("data", "keywords.txt"))


def _load_keywords():
    """读 data/keywords.txt → {组名: frozenset(词)}。任何异常都退回空表，绝不因此启动失败。"""
    groups = {}
    try:
        with open(_KEYWORDS_PATH, "r", encoding="utf-8") as f:
            cur = None
            for line in f:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                if s.startswith("[") and s.endswith("]"):
                    cur = s[1:-1].strip()
                    groups.setdefault(cur, set())
                    continue
                if cur:
                    groups[cur].add(s)
    except Exception as ex:
        print(f"[warn] 关键词表读取失败（{type(ex).__name__}: {_KEYWORDS_PATH}），"
              f"相关筛选本轮按空表处理")
    return {k: frozenset(v) for k, v in groups.items()}


_KW = _load_keywords()


def _kw(name):
    """取一组关键词；缺组时返回空集合（等价于"这层筛选不生效"，不抛异常）。"""
    return _KW.get(name) or frozenset()


FOREIGN_MARKERS = _kw("foreign")
FOREIGN_SECTIONS = _kw("foreign_sections")
DOMESTIC_SECTIONS = _kw("domestic_sections")


# 【2026-10-07 用户反馈「国际版要闻还是充次着国内新闻」】
# 光判断"像不像国际"不够 —— 「外交部：美方应慎重处理台湾问题」「法德要求欧盟…商务部回应」
# 这类标题里有外国名，但其实是**中国官方表态/国内新闻**，被误判成国际后塞进了国际要闻。
# 所以再加一层「强国内特征」排除：命中这些词就是国内新闻，不给国际板块。
DOMESTIC_MARKERS = _kw("domestic")


# 【2026-10-07 用户反馈「国际版娱乐还是有国内明星的新闻」】
# 娱乐栏目不能用通用规则：中国明星出国（「蔡少芬和张晋现身韩国」）会因为"韩国"
# 两个字被误判成国际娱乐。娱乐栏目要求标题里出现**外国的具体对象**
# （外国艺人 / 球队 / 奖项 / 作品），光有外国地名不算。
ENT_FOREIGN_MARKERS = _kw("ent_foreign")


def _looks_ent_foreign(title):
    """娱乐栏目专用的「是不是国际」判定：必须有外国具体对象，光有外国地名不算。"""
    t = title or ""
    return any(k in t for k in ENT_FOREIGN_MARKERS)


# 【2026-10-07 用户反馈「气泡里快科技播的是国际新闻」】
# 科技栏目必须按**品牌归属**判，不能只看国家名：
#   「DLSS 5 体验：英伟达怎么让它以假乱真」没有国家名，但讲的是美国公司 → 国际
#   「华为徐直军：昇腾950超节点」讲的是中国公司 → 国内
# 先看是不是中国品牌（是就留国内），再看是不是外国品牌（是就走国际），
# 两者都没有才退回原来的关键词判定。
CN_TECH_BRANDS = _kw("cn_tech")
FOREIGN_TECH_BRANDS = _kw("foreign_tech")


def _tech_scope(title):
    """科技栏目的归属判定：返回 'cn' / 'intl' / None（判不出来）。"""
    t = title or ""
    if any(k in t for k in CN_TECH_BRANDS):
        return "cn"
    if any(k in t for k in FOREIGN_TECH_BRANDS):
        return "intl"
    return None


# 【2026-10-07】国际大奖 / 国际赛事类关键词 —— 这类**优先判国际**，
# 即使标题里同时出现「中国科学家」「中国影片」也不改判。
# 起因：把「中国」加进国内特征词后，「诺贝尔物理学奖将揭晓，中国科学家薛其坤受关注」
# 被锁在国内要闻/热榜里，用户反馈"国内还是有国际新闻"。
STRONG_INTL_MARKERS = _kw("strong_intl")


def _strong_intl(title):
    """国际大奖/赛事 → 一律算国际，不受国内特征词影响。"""
    t = title or ""
    return any(k in t for k in STRONG_INTL_MARKERS)


def _looks_domestic(title):
    """粗判一条新闻是不是「国内新闻」（含中国官方表态、国内事务）。

    用途：国内板块往国际板块转条目时，先过这一关 —— 只要有强国内特征就不转。
    """
    t = title or ""
    for k in DOMESTIC_MARKERS:
        if k in t:
            return True
    return False

def _looks_foreign(title):
    """粗判一条新闻是不是国际新闻（用于国内热榜过滤）。"""
    t = title or ""
    for k in FOREIGN_MARKERS:
        if k in t:
            return True
    return False


# 国内热榜里允许保留的来源（澎湃/红星本身就是国内源，无需过滤；
# 凤凰是综合源，必须过滤掉国际条目）
HOT_FILTER_FOREIGN_LABELS = {"凤凰网"}


def _parse_sina_ent(src):
    """新浪娱乐：ent.sina.com.cn 首页的 <p class="item"><a href title="标题">。

    【为什么用它】国内娱乐的 RSS 实测**全废**：
      网易娱乐/中新网娱乐 → 无条目；搜狐娱乐 → 30 条但 0 图、无时间；
      人民网娱乐 → 停更一年多（最新 2025-06-05）；时光网 → 连不上。
    新浪娱乐首页是 SSR HTML，条目链接形如 https://k.sina.com.cn/article_<...>.html。

    注意：这个列表页**既没有发布时间、也没有题图**，两者都靠后续的
    _fill_missing_images 去文章页取（_article_meta 一次抓取同时返回 og:image
    和真实发布时间），所以这里 published / image 先留空。
    """
    raw = _fetch(src["url"])
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 新浪娱乐抓取失败")
        return []
    page = raw.decode("utf-8", "ignore")
    pat = (r'<p class="item"><a href="(https://k\.sina\.com\.cn/article_[^"]+)"'
           r'[^>]*title="([^"]*)"')
    out, seen = [], set()
    for m in re.finditer(pat, page):
        url = _norm_url(m.group(1), src["base"])
        title = _clean(m.group(2))
        if not title or len(title) < 6 or url in seen:
            continue
        seen.add(url)
        out.append({
            "id": hashlib.md5(("sina-ent-" + url).encode("utf-8")).hexdigest()[:12],
            "region": "cn", "channel": src["id"], "label": src["label"],
            "cls": src["cls"], "title": title, "desc": "",
            "link": url, "image": "", "published": 0,
            "heat": 0, "translated": False,
        })
        if len(out) >= 30:
            break
    print(f"[ok] {src['id']}: 新浪娱乐解析 {len(out)} 条（题图与时间待文章页补）")
    return out


# 【2026-10-09 换源】国际在线·娱乐（ent.cri.cn）的列表卡片：
#   标题链 + <div class="eff-img"> 里的图 + <div class="eff-mes"><i>时间</i> 三件套
# 都在服务端渲染好的 HTML 里，正则一把抓；页面是 GB 系老站的写法，但声明正确、实测 utf-8。
_CRI_CARD_RE = re.compile(
    r'<a[^>]+href="(/20\d{6}/[0-9a-f\-]{20,}\.html)"[^>]*>\s*([^<>]{6,90}?)\s*</a>'
    r'(.*?)<div class="eff-mes"><i>([\d\-: ]{10,19})</i>', re.S)
# ⚠ 卡片里的图是**缩略图**：URL 形如 `…/image/<hash>.<原图W>x<原图H>.<下发W>x<下发H>.jpg`，
# 实测列表给的是 512x288（短边 288 < 高清门槛 300）—— 直接用会被后台低质图环节
# **整条剔除**，等于这个源白加。站方同一路径去掉最后一节尺寸就是原图
# （实测改写后 2496x1404 / 991x558 / 960x540 / 4052x2278，全部达标）。
_CRI_THUMB_RE = re.compile(
    r'^(.*?)\.(\d{2,5})x(\d{2,5})\.(\d{2,5})x(\d{2,5})(\.(?:jpe?g|png|webp))$', re.I)


def _cri_image(url):
    """把国际在线卡片的缩略图地址改写成原图地址；改写不了就原样返回。"""
    m = _CRI_THUMB_RE.match(url or "")
    return (m.group(1) + "." + m.group(2) + "x" + m.group(3) + m.group(6)) if m else (url or "")


def _parse_cri_ent(src):
    """国际在线·娱乐（ent.cri.cn）：列表页 SSR 卡片，标题 / 原图 / 时间一次拿全。

    【为什么加它】2026-10-09 换源时娱乐栏目只剩凤凰娱乐 + 新浪娱乐两家：
    新浪娱乐的列表页**既无图也无时间**、全靠逐条抓文章页，栏目内站点数不达标。
    逐家实测后，只有这家同时满足「当天分钟级更新 + 列表页自带题图 + 境内权威媒体
    （中国国际广播电台 / 总台旗下）」。实测数据见
    logs\\换源-境内国际版-20261009.md；实测失败的仍不采用（见 README 那张表）。

    图片取自卡片（并按 _cri_image 改写成原图）→ 不需要再去文章页，
    也不占 BODY_IMAGE_CHANNELS 的名额。
    """
    raw = _fetch(src["url"])
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 国际在线娱乐抓取失败")
        return []
    page = _decode_feed_bytes(raw)      # 中文站：按声明 → utf-8 → gb18030 依次回退解码
    max_age = (src.get("max_age_hours")
               or CLS_MAX_AGE_HOURS.get(src.get("cls", ""))
               or MAX_AGE_HOURS.get(src.get("region", "cn"), 24))
    cutoff = datetime.now(CST).timestamp() - max_age * 3600
    out, seen, dropped = [], set(), 0
    for m in _CRI_CARD_RE.finditer(page):
        path, title, mid, ttxt = m.group(1), _clean(m.group(2)), m.group(3), m.group(4)
        link = _norm_url(path, src["base"])
        if not title or len(title) < 6 or not link or link in seen:
            continue
        try:
            ts = int(datetime.strptime(ttxt.strip(), "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=CST).timestamp())
        except Exception:
            ts = 0
        if ts and ts < cutoff:
            dropped += 1
            continue
        img = ""
        mi = re.search(r'<img[^>]+src="([^"]+\.(?:jpe?g|png|webp)[^"]*)"', mid, re.I)
        if mi:
            u = _norm_url(mi.group(1), src["base"])
            if u and not any(x in u.lower() for x in _IMG_JUNK):
                img = _cri_image(u)      # 缩略图 → 原图（见 _CRI_THUMB_RE 的说明）
        seen.add(link)
        out.append({
            "id": hashlib.md5((src["id"] + title).encode("utf-8")).hexdigest()[:12],
            "region": src["region"], "channel": src["id"], "label": src["label"],
            "cls": src["cls"], "title": title, "desc": "",
            "link": link, "image": img, "published": ts, "translated": False,
        })
        if len(out) >= 30:
            break
    print(f"[ok] {src['id']}: 国际在线娱乐解析 {len(out)} 条"
          f"（带图 {sum(1 for x in out if x['image'])} 条，滤除过期 {dropped} 条）")
    return out


# 国内板块里要做「国内 / 国际」内容分流的栏目（用户 2026-10-07 要求）：
#   · 热榜：原来就分流了
#   · 要闻：「国内板块里的要闻充次着国际新闻，全部移到国际板块」
# 科技 / 软件 / 娱乐**不做分流** —— 这两栏里的国际内容（AMD、马斯克、欧美乐坛…）
# 本来就属于该栏目本身，分流会把它们搬空。
# 【2026-10-07 用户再次强调】「区分国内和国际，娱乐栏目也不例外」→ 娱乐也纳入分流。
# 科技/软件仍不分流：那两栏里的国际内容（AMD、马斯克、欧美软件）本来就属于该栏目。
# 【2026-10-07 用户反馈「快科技里有国际新闻混在里面」】
# 科技栏目原来不参与国内/国际分流，于是「洛杉矶机场两架宽体机相撞」「比利时天才少年」
# 「谷歌/OpenAI/马斯克/韩国散户」这些国际科技新闻全留在国内科技里。
# 加进来之后：国际科技条目会带着 cls="科技" 转到国际板块，
# **国际板块自动多出一个"科技"栏目**（栏目是跟着数据走的，不用另外写死）。
CN_SPLIT_CLS = {"要闻", "热榜", "娱乐", "科技"}

# 国内热榜里被判为国际的那批（由 _collect("hot") 填），供国际热榜并入
_HOT_INTL_SPILL = []


def _split_cn_scope(items):
    """按内容把国内板块的条目分成「国内」「国际」两拨，国际的那拨转给国际板块。"""
    # 站方自己的栏目分类优先（凤凰文章页 JSON-LD 的 articleSection）；
    # 没有分类信息时才退回关键词黑名单。
    # 只有**已登记的栏目名**才当分类用；认不出来就退回关键词判定 ——
    # 凤凰会把 "独家原创" 也塞进 articleSection，那是内容类型不是栏目，
    # 若一律信它，「特朗普称可让伊朗摧毁洛杉矶」「冲绳县知事：驻日美军…」
    # 这些会被当成国内新闻留在国内榜里。
    # 【2026-10-09】FOREIGN_SECTIONS / DOMESTIC_SECTIONS 已挪到 data/keywords.txt
    #（模块级常量，见上面 _load_keywords），这里直接用，判定顺序不变。
    dom, intl = [], []
    for it in items:
        _sec = (it.get("section") or "").strip()
        _title = it.get("title") or ""
        # 【2026-10-07 用户反馈「国内热榜还是混进国际新闻」】关键改动：
        # **关键词很明确时以关键词为准，不再被站方栏目压住**。
        # 原因是凤凰把外交新闻归在「大陆」栏目里，而「大陆」在我的国内白名单中，
        # 于是「俄罗斯总统助理…将访华」这种标题被判成国内 —— 用户一眼就看出是国际。
        # 现在顺序是：站方说国际 → 国际；关键词明显是国际（且无强国内特征）→ 国际；
        # 站方说国内 → 国内；其余按关键词兜底。
        if _sec in FOREIGN_SECTIONS:
            _is_intl = True
        elif _strong_intl(_title):
            # 国际大奖/赛事优先判国际（诺贝尔、奥斯卡、奥运会…），
            # 哪怕标题里有「中国科学家」「中国影片」也不改判。
            _is_intl = True
        elif _looks_foreign(_title) and not _looks_domestic(_title):
            _is_intl = True
        elif _sec in DOMESTIC_SECTIONS:
            _is_intl = False
        elif it.get("cls") == "科技":
            # 科技栏按品牌归属判（见 _tech_scope）：先看中国品牌，再看外国品牌，
            # 都判不出来才退回通用关键词规则。
            _ts = _tech_scope(_title)
            if _ts == "cn":
                _is_intl = False
            elif _ts == "intl":
                _is_intl = True
            else:
                _is_intl = _looks_foreign(_title) and not _looks_domestic(_title)
        elif it.get("cls") == "娱乐":
            # 娱乐栏目单独判定：中国明星出国不算国际（见 _looks_ent_foreign）
            _is_intl = _looks_ent_foreign(_title) and not _looks_domestic(_title)
        else:
            # 像国际 **且** 不像国内，才算国际 —— 只判前者会把
            # 「外交部：美方应慎重处理台湾问题」这类中国官方表态误转过去。
            _is_intl = _looks_foreign(_title) and not _looks_domestic(_title)
        if it.get("cls") in CN_SPLIT_CLS and _is_intl:
            d = dict(it)
            d["region"] = "intl"
            d["channel"] = "cn-intl-news"     # 标明「国内媒体报的国际新闻」
            d["translated"] = True            # 国内媒体发的，本来就是中文
            intl.append(d)
        else:
            dom.append(it)
    return dom, intl


def _spill_from_cn():
    """国内板块剔出来的国际新闻（供国际板块并入）。

    读的是国内刚抓好的完整池子 PRELOAD_ALL['cn']，**不再发任何网络请求**。
    国内没抓好时返回空（下一轮补上），绝不因此阻塞或报错。
    """
    try:
        return _split_cn_scope(PRELOAD_ALL.get("cn") or [])[1]
    except Exception:
        return []


def _split_hot_by_scope(items):
    """把热榜条目分成「国内」「国际」两拨（用户建议的灵活做法）。

    凤凰网/澎湃 这类综合源的热榜里混着国际新闻。国内热榜要纯国内，
    但这些国际条目**不该丢掉** —— 转手拿去充实国际热榜（那边原来只有
    中新社国际 + CGTN 两家）。
    返回 (国内条目, 国际条目)；国际条目的 region 改成 intl。
    """
    # 【2026-10-07】优先用**站方自己的栏目分类**（凤凰文章页 JSON-LD 的 articleSection，
    # 实测取到 "国际"/"社会"/"军事"/"台湾"…）；只有拿不到分类时才退回关键词黑名单。
    # 黑名单必漏：实测「乌征兵人员将1岁幼儿父亲沿地拖行」是国际新闻，标题里却没有
    # "乌克兰"三个字 —— 用户反馈的「国内热榜第二条还是国际新闻」就是它。
    # 只有**已登记的栏目名**才当分类用；认不出来就退回关键词判定 ——
    # 凤凰会把 "独家原创" 也塞进 articleSection，那是内容类型不是栏目，
    # 若一律信它，「特朗普称可让伊朗摧毁洛杉矶」「冲绳县知事：驻日美军…」
    # 这些会被当成国内新闻留在国内榜里。
    # 【2026-10-09】FOREIGN_SECTIONS / DOMESTIC_SECTIONS 已挪到 data/keywords.txt
    #（模块级常量，见上面 _load_keywords），这里直接用，判定顺序不变。
    dom, intl = [], []
    for it in items:
        _sec = (it.get("section") or "").strip()
        _title = it.get("title") or ""
        # 【2026-10-07 用户反馈「国内热榜还是混进国际新闻」】关键改动：
        # **关键词很明确时以关键词为准，不再被站方栏目压住**。
        # 原因是凤凰把外交新闻归在「大陆」栏目里，而「大陆」在我的国内白名单中，
        # 于是「俄罗斯总统助理…将访华」这种标题被判成国内 —— 用户一眼就看出是国际。
        # 现在顺序是：站方说国际 → 国际；关键词明显是国际（且无强国内特征）→ 国际；
        # 站方说国内 → 国内；其余按关键词兜底。
        if _sec in FOREIGN_SECTIONS:
            _is_intl = True
        elif _strong_intl(_title):
            # 国际大奖/赛事优先判国际（诺贝尔、奥斯卡、奥运会…），
            # 哪怕标题里有「中国科学家」「中国影片」也不改判。
            _is_intl = True
        elif _looks_foreign(_title) and not _looks_domestic(_title):
            _is_intl = True
        elif _sec in DOMESTIC_SECTIONS:
            _is_intl = False
        elif it.get("cls") == "科技":
            # 科技栏按品牌归属判（见 _tech_scope）：先看中国品牌，再看外国品牌，
            # 都判不出来才退回通用关键词规则。
            _ts = _tech_scope(_title)
            if _ts == "cn":
                _is_intl = False
            elif _ts == "intl":
                _is_intl = True
            else:
                _is_intl = _looks_foreign(_title) and not _looks_domestic(_title)
        elif it.get("cls") == "娱乐":
            # 娱乐栏目单独判定：中国明星出国不算国际（见 _looks_ent_foreign）
            _is_intl = _looks_ent_foreign(_title) and not _looks_domestic(_title)
        else:
            # 像国际 **且** 不像国内，才算国际 —— 只判前者会把
            # 「外交部：美方应慎重处理台湾问题」这类中国官方表态误转过去。
            _is_intl = _looks_foreign(_title) and not _looks_domestic(_title)
        if it.get("cls") in CN_SPLIT_CLS and _is_intl:
            d = dict(it)
            d["region"] = "intl"
            d["channel"] = "cn-intl-hot"      # 标明「来自国内综合源的国际条目」
            # 【2026-10-07 修】这些条目本来就是中文（凤凰/澎湃/红星是国内媒体），
            # 标成「待翻译」既白占 MyMemory 额度，界面也会把它算进「未中文化」。
            d["translated"] = True
            intl.append(d)
        else:
            dom.append(it)
    return dom, intl


# 【2026-10-09 修 低-6 删除死代码】这里原来还有一个 `_filter_hot_domestic()`：
# 它只是把 `_split_hot_by_scope()` 的「国内」那一半包一层日志，**没有任何调用方**
# （_collect("hot") 直接调 _split_hot_by_scope）。删掉后不影响分流结果。


# ============ 「我发布的软件」专属提醒（2026-10-07 用户要求） ============
# 用户原话：「气泡提醒必须有我发表的软件提醒，让我及时知道发布情况」。
# 用户会把作品投稿到 mefcl 这类软件站，所以要盯着这些站，一旦出现作品名立刻弹提醒。
# 注意：这里只放**强标识**，不放「海风」这种常见词（会被天气新闻误命中）。
MY_SOFTWARE_KEYWORDS = (
    "壁纸助手", "微软壁纸助手", "ms-wallpaper-assistant",
    "FoxPath", "狐径", "kele551",
)
MY_SOFTWARE_CHANNELS = {"cn-mefcl", "cn-ghpym", "cn-iplay", "cn-soft"}   # 软件发布类站点
_MY_SOFT_SEEN = set()
_MY_SOFT_READY = [False]
# 已经播过气泡的条目 id —— 用户要求「轮播内容不能重复」。
# 每轮优先挑没播过的；全都播过了才清空重来（保证一直有新内容可播）。
_BALLOON_SHOWN = set()


def _notify_my_software(items):
    """发现自己的作品被发布 → 弹一条专属提醒。

    这条不做时间节流：用户的诉求就是「及时知道发布情况」，漏报比多报严重。
    """
    if not items:
        return
    hits = []
    for it in items:
        blob = ("%s %s" % (it.get("title") or "", it.get("desc") or "")).lower()
        if any(k.lower() in blob for k in MY_SOFTWARE_KEYWORDS):
            hits.append(it)
    if not hits:
        return
    fresh = [it for it in hits if it.get("id") and it["id"] not in _MY_SOFT_SEEN]
    for it in hits:
        if it.get("id"):
            _MY_SOFT_SEEN.add(it["id"])
    if len(_MY_SOFT_SEEN) > 2000:
        _MY_SOFT_SEEN.clear()
    if not _MY_SOFT_READY[0]:
        _MY_SOFT_READY[0] = True
        print(f"[ok] 「我的软件」提醒基线已建立（命中 {len(hits)} 条）")
        return
    if not fresh:
        return
    entries = []
    for it in fresh[:BALLOON_PLAY_COUNT]:
        entries.append((
            "🎉 你的软件被发布了",
            "[%s] %s" % (it.get("label") or it.get("channel") or "", (it.get("title") or "")[:60]),
            True, it.get("link") or ""))
    print(f"[ok] ★ 我的软件提醒：{len(fresh)} 条")
    _show_toast_list(entries)


# ============ 新闻大事提醒（2026-10-07 新增，用户要求的两类气泡之一） ============
# 用户要两类提醒：「新闻更新提醒」（上面的 _notify_news）和「新闻大事提醒」（这里）。
# 判定取两条规则的并集：
#   ① 关键词命中：标题里出现地震/爆炸/战争/坠机/暴跌 这类重大事件词
#   ② 多家同时报道：同一条新闻被 3 家以上不同来源同时报（标题 2-gram 重合度 >= 0.6 归并）
MAJOR_KEYWORDS = _kw("major")
MAJOR_MIN_SOURCES = 3        # 同一条新闻被这么多家同时报 → 判为大事
MAJOR_TOAST_MIN = 30 * 60    # 大事提醒最小间隔（秒），免得刷屏
_MAJOR_SEEN = set()
_MAJOR_LAST_TOAST = 0.0
_MAJOR_READY = [False]       # 首次只建基线不弹，避免启动瞬间炸出一堆
_MAJOR_LOCK = threading.Lock()


def _title_grams(s, n=2):
    s = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s or "")
    return {s[i:i + n] for i in range(len(s) - n + 1)} if len(s) >= n else ({s} if s else set())


def _notify_major(items):
    """刷新后调用：挑出「大事」弹一条警示气泡。首次调用只建基线不弹。"""
    global _MAJOR_LAST_TOAST, _MAJOR_READY
    if not items:
        return
    items = items[:150]
    hit = [it for it in items if any(k in (it.get("title") or "") for k in MAJOR_KEYWORDS)]
    # 规则②：多家同时报道同一条 → 也是大事
    groups = []
    for it in items:
        g = _title_grams(it.get("title"))
        if not g:
            continue
        for grp in groups:
            inter = len(g & grp["g"])
            if inter / max(1, min(len(g), len(grp["g"]))) >= 0.6:
                grp["labels"].add(it.get("label") or it.get("channel") or "")
                grp["g"] |= g
                break
        else:
            groups.append({"g": set(g),
                           "labels": {it.get("label") or it.get("channel") or ""},
                           "item": it})
    for grp in groups:
        if len([x for x in grp["labels"] if x]) >= MAJOR_MIN_SOURCES:
            hit.append(grp["item"])
    if not hit:
        return
    with _MAJOR_LOCK:
        fresh = [it for it in hit if it.get("id") and it["id"] not in _MAJOR_SEEN]
        for it in hit:
            if it.get("id"):
                _MAJOR_SEEN.add(it["id"])
        if len(_MAJOR_SEEN) > 4000:
            _MAJOR_SEEN = set(list(_MAJOR_SEEN)[-2000:])
        if not _MAJOR_READY[0]:
            _MAJOR_READY[0] = True
            print(f"[ok] 大事提醒基线已建立（{len(_MAJOR_SEEN)} 条）")
            return
    if not fresh:
        return
    now = time.time()
    if now - _MAJOR_LAST_TOAST < MAJOR_TOAST_MIN:
        return
    _MAJOR_LAST_TOAST = now
    sample = fresh[:BALLOON_PLAY_COUNT]
    entries = []
    for it in sample:
        label = it.get("label") or it.get("cls") or "要闻"
        entries.append((f"⚠ 新闻大事 · {label}", (it.get("title") or "")[:72], True, it.get("link") or ""))
    if len(fresh) > len(entries):
        _e = entries[-1]
        entries[-1] = (_e[0], _e[1] + f"　（另有 {len(fresh) - len(entries)} 条同类）") + tuple(_e[2:])
    print(f"[ok] 大事提醒：滚动播放 {len(entries)} 条")
    _show_toast_list(entries)



def _check_update_once():
    """启动后的静默检查 + **默认自动升级**（2026-10-09 升级体验口径）。

    行为（与产品口径一一对应，故意抽成模块级函数以便单测）：
      · 静默检查 → 有新版 → 静默下载 → Ed25519 验签 + sha256/尺寸校验 → 自动替换运行；
      · **全程不弹任何确认框、不要求用户点"确定"**；
      · 只有**失败**才提示一次（人话 + 一句怎么办），且旧版本保持可用；
      · `config.update.auto_update=false` → 回到"只提示、不自动升级"；
      · `config.update.enabled=false` → 连检查都不做。
    检查本身失败（连不上更新源）只写日志，不打扰用户。
    """
    try:
        cfg = CONFIG.get("update", {})
        if cfg.get("enabled", True) is False:
            print("[ok] 自动更新已关闭（config.update.enabled=false）")
            return
        st = updater.check()
        if st["status"] == "latest":
            print(f"[ok] 已是最新 v{st['remote']}")
            return
        if st["status"] != "update":
            # 连不上更新源不是用户的错、也不影响使用 —— 只写日志，不打扰
            print(f"[warn] 更新检查: {st['message']}")
            return

        rv = st["remote"]
        if cfg.get("auto_update", True) is False:
            # 自动升级被关掉 → 只提示一次，由用户自己在页面点「检查更新」
            print(f"[update] 发现新版本 v{rv}（auto_update=false，仅提示不自动升级）")
            _show_toast(f"发现新版本 v{rv}",
                        "自动升级已关闭：可在页面点「检查更新」手动升级")
            return

        # —— 自动升级：静默下载 + 校验，**不弹任何确认框** ——
        print(f"[update] 发现新版本 v{rv}，静默下载并校验")
        ok, msg = updater.download_and_prepare(st["latest"])
        print(f"[update] {msg}")
        if not ok:
            # 失败才提示一次：说人话 + 一句怎么处理；旧版本照常可用
            _show_toast("升级没成功，已继续使用当前版本",
                        f"{msg}。可以稍后再点「检查更新」，或到发布页手动下载覆盖。")
            return

        if _SILENT_RUN:
            # 后台静默实例（开机自启那种，没有页面在看）→ 立刻替换并重启，用户完全无感
            print("[update] 当前是后台静默运行，立即替换并重启")
            if updater.trigger_replace():
                threading.Timer(0.2, lambda: os._exit(0)).start()
            else:
                _show_toast("升级没装成，已继续使用当前版本",
                            f"{updater.last_message()}。可以稍后再点「检查更新」。")
        else:
            # 前台实例（用户可能正在看页面）→ 不打断：退出程序时自动安装（见 _install_ready_update）
            print("[update] 新版已就绪，将在退出程序时自动替换（无需确认）")
    except Exception as ex:
        print(f"[warn] 更新检查异常: {type(ex).__name__}")


@app.on_event("startup")
def _warmup():
    """后台预热两个 Region + 静默检查更新"""
    _load_trans_cache()          # 载入历史译文缓存，避免重复消耗免费翻译额度
    # 【2026-10-09 修 中-4】cleanup_leftovers 现在会**返回上一次替换脚本留下的回执**
    # （成功 / 失败 / 失败原因）。失败说明"上次升级没落地"，要在日志与气泡里说清楚，
    # 不能再像以前那样静默什么都不做。
    _last_swap = ""
    try:
        _last_swap = updater.cleanup_leftovers() or ""
        if _last_swap:
            print(f"[update] 上次替换回执: {_last_swap}")
    except Exception:
        _last_swap = ""
    TRAY.start()                 # 右下角托盘图标（非 Windows 静默降级）
    BALLOON.start()              # 右下角自绘气泡（系统通知被关也照样弹）
    def run():
        # 【2026-10-09 修 中-4】上次升级失败过 → 开机就说清楚，并给出可执行的动作
        if _last_swap.upper().startswith("FAIL"):
            try:
                _show_toast("上次升级没有完成", "程序仍是原版本，可以照常使用；"
                                               "请重新点「检查更新」或手动双击程序图标")
            except Exception:
                pass
        # 【2026-10-07 分两阶段】用户反馈「抓取时间让我等的焦虑」。
        # 完整抓一轮要 30~50 秒（热榜/无图源都要逐个抓文章页），首屏一直空着等。
        # 阶段一：只跑 RSS/HTML 源，几秒内先发布一次，让页面立刻有东西看。
        for _rg in ("cn", "intl"):
            try:
                PRELOAD[_rg] = _collect_fast(_rg)
                print(f"[ok] 快速就绪 {_rg}: {len(PRELOAD[_rg])} 条")
            except Exception as _ex:
                print(f"[warn] 快速就绪 {_rg} 失败: {type(_ex).__name__}")

        # 阶段二：完整抓取（含热榜合并、逐个文章页补图补时间），做完覆盖发布。
        # 顺序上国内先做，这样「国内要闻剔出的国际新闻」能顺手并进国际板块。
        def do_intl():
            try:
                # 【2026-10-09 修 中-7】与 refresh_loop / 页面按需抓取共用单飞闸：
                # 预热期间用户就把页面打开了也不会并发抓同一个 region 两轮。
                items = _collect_exclusive("intl")
            except Exception as ex:
                print(f"[warn] 预热 intl 失败: {type(ex).__name__}")
                return
            if items is None:
                print("[ok] 预热 intl: 已有一轮在跑，本轮跳过（单飞）")
                return
            print(f"[ok] 预热 intl: {len(items)} 条")
            try:
                # 低质图后台异步剔除（与国内一致）
                if items:
                    threading.Thread(target=_enrich_quality_bg, args=("intl",), daemon=True).start()
            except Exception:
                pass
            try:
                # 翻译慢且常被限流：不阻塞抓取，后台补齐译文
                if not _TRANS_BUSY[0]:
                    threading.Thread(target=_translate_intl_bg, args=(items,), daemon=True).start()
            except Exception:
                pass
            try:
                # 【2026-10-07】气泡要按板块分配名额（国际/国内/热榜各 2 席），
                # 所以**不能按板块分别调用** —— 那样每次都只看到半边，国际永远是 0。
                # 这里先只管"我的软件"提醒，新闻提醒等两个板块都抓好后合并调用。
                _notify_my_software(items)
            except Exception:
                pass

        for region in ("hot", "cn"):
            try:
                # 【2026-10-09 修 中-7】同上，走单飞闸，避免与页面按需抓取重复跑一轮
                items = _collect_exclusive(region)
                if items is None:
                    print(f"[ok] 预热 {region}: 已有一轮在跑，本轮跳过（单飞）")
                    continue
                print(f"[ok] 预热 {region}: {len(items)} 条")
                # 国内源：后台异步剔除低质图（下载测尺寸），不阻塞首屏
                if region == "cn" and items:
                    threading.Thread(target=_enrich_quality_bg, args=(region,), daemon=True).start()
                # 【2026-10-07】气泡要按板块分配名额（国际/国内/热榜各 2 席），
                # 所以**不能按板块分别调用** —— 那样每次都只看到半边，国际永远是 0。
                # 这里先只管"我的软件"提醒，新闻提醒等两个板块都抓好后合并调用。
                _notify_my_software(items)
                # 【2026-10-07 用户反馈「小气泡没有弹出」】开机后**主动弹一次今日热点**。
                # 原来只有"有新增条目"才弹，而启动时只建基线不弹 —— 于是刚开机那 10 分钟
                # 里用户什么都看不见，以为气泡坏了。这里补一次播报，也顺便验证气泡通路。
                if region == "cn":
                    # 【2026-10-07 用户要求「气泡也要有国际板块的新闻播报」】
                    # 原来只播国内热榜（而且这时候国际还没抓好，想播也没有）。
                    # 现在起个线程等国际抓完，再拼一条**中外混编**的启动播报：
                    # 热榜 2 条 + 国际 2 条 + 国内 2 条。
                    def _boot_balloon():
                        # 【2026-10-07】不能只等"国际有数据"就弹 —— 快速阶段抓的是外媒（英文），
                        # 中文的"国内媒体报的国际新闻"要等完整抓取才进来。
                        # 所以等到**出现中文国际条目**再弹，最多等 120 秒。
                        for _ in range(120):
                            time.sleep(1)
                            _it = PRELOAD.get("intl") or []
                            if any(_has_cjk(x.get("title"))
                                   and str(x.get("channel") or "").startswith("cn-intl")
                                   for x in _it):
                                break
                        try:
                            _all = [x for x in (list(PRELOAD.get("cn") or [])
                                                + list(PRELOAD.get("intl") or []))
                                    if _has_cjk(x.get("title"))]   # 只播中文，英文条目不要
                            _hot = [x for x in _all if x.get("cls") == "热榜"]
                            # 国际优先取**国内媒体报道的**（中文，用户看得懂）
                            _intl_raw = [x for x in _all if x.get("region") == "intl" and x.get("cls") != "热榜"]
                            _intl = ([x for x in _intl_raw
                                      if str(x.get("channel") or "").startswith("cn-intl")
                                      or x.get("translated")]
                                     + [x for x in _intl_raw
                                        if not (str(x.get("channel") or "").startswith("cn-intl")
                                                or x.get("translated"))])
                            _cn = [x for x in _all if x.get("region") == "cn" and x.get("cls") != "热榜"]
                            _pick = (_hot[:2] + _intl[:2] + _cn[:2])[:BALLOON_PLAY_COUNT]
                            if not _pick:
                                print("[warn] 启动气泡：没有可用条目，跳过")
                                return
                            _ents = []
                            for x in _pick:
                                _tag = "国际 · " if x.get("region") == "intl" else "国内 · "
                                _ents.append(("今日热点 · " + _tag + (x.get("label") or ""),
                                              (x.get("title") or "")[:72], False, x.get("link") or ""))
                            _n_intl = sum(1 for x in _pick if x.get("region") == "intl")
                            print(f"[ok] 启动气泡：播报 {len(_ents)} 条（含国际 {_n_intl} 条）")
                            _show_toast_list(_ents)
                        except Exception as _ex:
                            print(f"[warn] 启动气泡失败: {type(_ex).__name__}")
                    threading.Thread(target=_boot_balloon, daemon=True).start()
            except Exception as ex:
                print(f"[warn] 预热 {region} 失败: {type(ex).__name__}: {ex}")
                import traceback as _tb
                print(_tb.format_exc())

        # 国内抓完再抓国际 —— 这样「国内要闻里剔出的国际新闻」已经就绪，
        # 能顺手并进国际板块（见 _spill_from_cn）。
        do_intl()

    def refresh_loop():
        # 用户要求：所有栏目统一每 10 分钟自动刷新（不分国内/国际），无需手动。
        # 一轮内重建 热榜 + 国内源 + 国际源，刷新后弹气泡（更新提醒 / 大事提醒）。
        # 【2026-10-09 修 中-7】改走 _collect_exclusive：与页面按需抓取（_kick_collect）
        # 共用同一把「正在抓取」的闸，保证同一 region 同一时刻只有一轮。
        # 已被占着就跳过本轮（下一轮 10 分钟后再来），绝不并发跑两轮。
        while True:
            time.sleep(600)          # 10 分钟一轮
            try:
                _collect_exclusive("hot")
            except Exception:
                pass
            try:
                cn_items = _collect_exclusive("cn")
                # 2026-10-07 修复：以前刷新时漏掉了国内的「低质图异步剔除」，
                # 导致跑久了低质图不会被清，跟刚启动时的表现不一致。
                if cn_items:
                    threading.Thread(target=_enrich_quality_bg, args=("cn",), daemon=True).start()
            except Exception:
                pass
            try:
                items = _collect_exclusive("intl")
                if items:
                    threading.Thread(target=_enrich_quality_bg, args=("intl",), daemon=True).start()
                    if not _TRANS_BUSY[0]:
                        threading.Thread(target=_translate_intl_bg, args=(items,), daemon=True).start()
            except Exception:
                pass
            # 一轮刷新结束：合并所有栏目，弹「更新提醒」（内部已节流）+「大事提醒」+「我的软件」
            try:
                merged = (PRELOAD.get("cn", []) + PRELOAD.get("hot", [])
                          + PRELOAD.get("intl", []))
                _notify_news(merged)
                _notify_major(merged)
                _notify_my_software(merged)      # 用户要求：作品被发布必须及时知道
            except Exception:
                pass

    def check_update():
        # 延迟几秒再查，避免和预热抢网络/CPU
        time.sleep(5)
        _check_update_once()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    u = threading.Thread(target=check_update, daemon=True)
    u.start()
    threading.Thread(target=refresh_loop, daemon=True).start()


# ============ 接口绝不阻塞（2026-10-07 修「切换分区时栏目没切换」） ============
# 用户反馈：点「国际」之后栏目和内容都没变。
# 根因：api_news 在没有预热数据时会**同步** _collect(region)，而国际板块冷启动实测
# 要 40~49 秒（国内约 30 秒）—— 请求一直挂着，页面自然停在旧数据上。
# 现在：没数据就立刻返回 loading，后台线程去抓，前端每 15 秒轮询自动接上。
_COLLECTING = {}
_COLLECT_LOCK = threading.Lock()


# ============ 单飞（single-flight）：同一个 region 同时只允许一轮抓取 ============
# 【2026-10-09 修 中-7】原来只有 `_kick_collect`（页面按需抓取）会登记 _COLLECTING，
# `refresh_loop`（每 10 分钟一轮的定时刷新）**不登记** —— 于是两者可以对同一个 region
# 同时抓两轮：白耗网络与源站配额，而且两条链路交替写 PRELOAD，页面数据会来回跳。
# 现在两条链路共用下面这一对函数，保证「同一时刻只有一轮」：
#   _collect_exclusive(region) —— 抢到闸就抓并发布，抢不到就直接跳过本轮（不排队、不阻塞）。
def _try_begin_collect(region):
    """抢「正在抓取」的闸。抢到返回 True，否则 False。"""
    with _COLLECT_LOCK:
        if _COLLECTING.get(region):
            return False
        _COLLECTING[region] = True
        return True


def _end_collect(region):
    with _COLLECT_LOCK:
        _COLLECTING[region] = False


def _collect_exclusive(region):
    """单飞抓取：抢到闸才抓，抓到就发布。已被别人占着则返回 None（本轮跳过）。"""
    if not _try_begin_collect(region):
        print(f"[ok] {region}: 已有一轮抓取在进行，本轮跳过（单飞）")
        return None
    try:
        items = _collect(region)
        PRELOAD[region] = items
        return items
    except Exception as ex:
        print(f"[warn] 抓取 {region} 失败: {type(ex).__name__}")
        return None
    finally:
        _end_collect(region)


def _kick_collect(region):
    """后台抓一轮并写进 PRELOAD。已在抓就不重复起。返回是否新起了线程。"""
    if not _try_begin_collect(region):
        return False

    def _run():
        try:
            PRELOAD[region] = _collect(region)
        except Exception as ex:
            print(f"[warn] 后台抓取 {region} 失败: {type(ex).__name__}")
        finally:
            _end_collect(region)

    threading.Thread(target=_run, daemon=True).start()
    return True


# ============ 本机令牌 + 同源校验（2026-10-09 新增，修 高-4）============
# 背景：/api/quit 直接 os._exit、/api/update/install 会替换 exe，两个接口原来什么都不校验。
# 任意网页只要写一个 <form method=POST action="http://127.0.0.1:8000/api/quit"> 就能把程序关掉
# （表单是"简单请求"，不需要 CORS 预检；响应读不到，但副作用已经发生了）。
# 现在有副作用的接口要过两道闸：
#   ① 令牌：首屏 SSR 会把令牌写进页面（window.__SSR_DATA__.token），请求要带 X-HN-Token 头。
#      跨站页面拿不到这个头（跨域读不到我们的响应体，我们也不给任何 CORS 头）；
#   ② 同源 + 本机：Origin/Referer 若带了，必须是本机地址；客户端地址必须是回环地址。
# 令牌落在 %LOCALAPPDATA%\hotnews\local_token：升级重启后旧页面不用刷新也还能用，
# 而且是按 Windows 用户隔离的目录，别的用户与网页都读不到。
_LOCAL_TOKEN_FILE = os.path.join(
    os.getenv("LOCALAPPDATA") or tempfile.gettempdir(), "hotnews", "local_token")
_TOKEN_HEADER = "x-hn-token"


def _load_local_token():
    try:
        with open(_LOCAL_TOKEN_FILE, "r", encoding="utf-8") as f:
            t = (f.read() or "").strip()
        if len(t) >= 32:
            return t
    except Exception:
        pass
    t = os.urandom(24).hex()
    try:
        os.makedirs(os.path.dirname(_LOCAL_TOKEN_FILE), exist_ok=True)
        with open(_LOCAL_TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(t)
    except Exception:
        pass      # 写不进去（只读盘）也不影响：本次进程内用随机令牌，重开换一个
    return t


TOKEN = _load_local_token()


def _same_site(request):
    """Origin/Referer 若带了，必须是本机地址（同源校验）。"""
    for name in ("origin", "referer"):
        v = (request.headers.get(name) or "").strip()
        if not v:
            continue
        try:
            host = (urllib.parse.urlsplit(v).hostname or "").lower()
        except Exception:
            return False
        if host not in _LOOPBACK_HOSTS:
            return False
    return True


def _is_local_client(request):
    """客户端地址必须是回环（127.0.0.1 / ::1），别的地方来的请求一律拒绝。"""
    h = (getattr(request.client, "host", "") or "").strip().lower()
    if not h:
        return False
    if h in _LOOPBACK_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _guard_mutating(request):
    """有副作用接口的统一闸门：通过返回 None，否则返回 403 响应。"""
    if not _same_site(request):
        return JSONResponse({"ok": False, "message": "请求来源不是本机页面，已拒绝"},
                            status_code=403)
    if not _is_local_client(request):
        return JSONResponse({"ok": False, "message": "只允许本机发起该操作，已拒绝"},
                            status_code=403)
    tok = request.headers.get(_TOKEN_HEADER) or ""
    if not (tok and hmac.compare_digest(tok, TOKEN)):
        return JSONResponse({"ok": False, "message": "缺少本机访问令牌，已拒绝"},
                            status_code=403)
    return None


@app.get("/api/news")
def api_news(region: str = Query("cn", enum=["cn", "intl", "hot"]), q: str = Query(""), limit: int = Query(48, ge=1, le=200)):
    if region not in SOURCES:
        return JSONResponse({"error": "unknown region"}, status_code=400)
    # 【搜索走更宽的池子】用户原则：主列表常看常新、绝不看旧新闻；
    # 但程序叫「热点新闻检索」，搜索时必须能搜到更早的新闻。
    # 所以带关键词时改搜 PRELOAD_ALL（当天过滤之前的完整池子，含最近几天）。
    _kw = (q or "").strip()
    if _kw and PRELOAD_ALL.get(region):
        all_items = PRELOAD_ALL[region]
    elif PRELOAD.get(region):
        all_items = PRELOAD[region]
    else:
        # 没有预热数据 → **绝不在请求里同步抓取**，立刻返回并让后台去抓。
        _kick_collect(region)
        return JSONResponse(
            {"version": VERSION, "count": 0, "items": [], "loading": True,
             "message": "正在抓取，请稍候…"},
            media_type="application/json; charset=utf-8")
    # 去重（同标题）
    seen, items = set(), []
    for it in all_items:
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        items.append(it)
    # 缺图的补一个占位，保证网格整齐
    for it in items:
        if not it["image"]:
            it["image"] = ""
    # 排序要分两段：热榜条目由 _parse_hot 按「平台内归一化热度」排好，且它们的 published 为 0
    #（热榜接口不提供时间），一旦统一按 published 排就会被甩到最末，"混合榜"就没意义了。
    hot = [it for it in items if it.get("cls") == "热榜"]
    rest = [it for it in items if it.get("cls") != "热榜"]
    rest.sort(key=lambda x: x["published"], reverse=True)
    items = hot + rest
    if q.strip():
        kw = q.strip().lower()
        items = [i for i in items if kw in i["title"].lower() or kw in i["desc"].lower()]
    # 【修 高-1】把这一批图里出现过的域名登记进 /img 的来源白名单
    _note_item_hosts(items)
    # 显式声明 charset=utf-8，避免个别客户端把 JSON 误判为 GBK 导致乱码
    return JSONResponse({"version": VERSION, "count": len(items), "items": items[:limit]},
                        media_type="application/json; charset=utf-8")


@app.get("/api/version")
def api_version():
    # 【2026-10-09 修 中-5 / 中-6】多带三个诊断字段（只增不减，不影响老调用方）：
    #   pid / uptime    —— 二次启动时 app.py 用它判断"对面是不是一个正常服务的实例"
    #   poolTimeouts    —— 连接池排队超时的累计次数，排查"偶发丢源"时一眼就能看到
    return JSONResponse({"version": VERSION,
                         "pid": os.getpid(),
                         "uptime": int(time.time() - _START_TS),
                         "poolTimeouts": _POOL_TIMEOUTS[0],
                         "updatedAt": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")},
                        media_type="application/json; charset=utf-8")


@app.get("/api/update/check")
def api_update_check():
    """手动检查更新：返回最新版本信息；有新版则顺带触发后台下载"""
    st = updater.check()
    if st["status"] == "update":
        # 后台下载，不阻塞响应
        def _dl():
            ok, msg = updater.download_and_prepare(st["latest"])
            print(f"[update] {msg}")
        threading.Thread(target=_dl, daemon=True).start()
    return {
        "status": st["status"],
        "current": VERSION,
        "remote": st.get("remote"),
        "message": st.get("message"),
        "notes": (st.get("latest") or {}).get("notes", ""),
    }


@app.post("/api/update/install")
def api_update_install(request: Request):
    """已下载就绪则触发替换脚本，随后退出服务让脚本完成覆盖+重启"""
    # 【2026-10-09 修 高-4】这个接口会替换正在运行的程序，必须先过本机令牌 + 同源校验
    _deny = _guard_mutating(request)
    if _deny:
        print("[warn] /api/update/install 被拒绝：来源未通过本机校验")
        return _deny
    print("[ok] 收到安装请求，开始替换")
    if not updater.trigger_replace():
        # 【2026-10-09 修 中-4】替换没有真的落地时**绝不能退出** —— 以前这里无条件返回 True
        # 之后主进程就 os._exit 了，于是"替换失败 + 程序已经消失"同时发生（详见 updater 的
        # 可写性预检）。现在把 updater 给出的具体原因原样回给页面，程序继续运行。
        _why = updater.last_message() or "没有待安装的更新或校验没通过"
        print(f"[warn] 替换未执行：{_why}（程序未退出，可继续使用）")
        return {"ok": False, "message": _why}
    print("[ok] 替换已触发，准备退出让新版本接管端口")

    # 【2026-10-07 修「升级后不自动重启」】
    # 原来延迟 0.6 秒才退，而新进程这时已经起来抢端口了，被判成"已有实例"→ 开个浏览器就退，
    # 用户看到的现象就是"装完了程序没起来"。现在改成**立刻退出**（0.15 秒留给 HTTP 响应写完），
    # 端口马上释放，新进程等待重试后就能接管。
    def _die():
        print("[ok] 进程即将退出（为升级让出端口）")
        sys.stdout.flush()
        os._exit(0)
    threading.Timer(0.15, _die).start()
    threading.Thread(target=_die, daemon=True).start()
    return {"ok": True, "message": "正在更新并重启"}


@app.get("/api/autostart")
def api_autostart_get():
    """读开机自启状态（界面上的开关用它回显）。"""
    return {"enabled": _autostart_enabled()}


@app.post("/api/autostart")
async def api_autostart_set(request: Request, payload: dict = None):
    """开关开机自启（写 HKCU 的 Run 项，不需要管理员权限）。

    【2026-10-07 用户要求「第一次使用时界面给个开关让用户选」】
    开机自启时用 --silent 启动：只后台把服务跑起来、把第一屏数据抓好，不弹浏览器。
    这样用户想看的时候点托盘右键「打开热点新闻」就是秒开（数据早准备好了）。
    """
    # 【2026-10-09 修 高-4】写注册表也是有副作用的操作，一起挂到本机令牌校验上
    _deny = _guard_mutating(request)
    if _deny:
        print("[warn] /api/autostart 被拒绝：来源未通过本机校验")
        return _deny
    want = bool((payload or {}).get("enabled"))
    ok, msg = _autostart_set(want)
    return {"enabled": _autostart_enabled(), "ok": ok, "message": msg}

@app.post("/api/quit")
def api_quit(request: Request):
    """
    退出服务。打包成无窗口 exe 后没有控制台可按 Ctrl+C，只能由页面按钮触发。
    延迟 0.6s 再退，先让这次 HTTP 响应回到浏览器。
    """
    # 【2026-10-09 修 高-4】原来谁都能 POST 一下就关掉程序（本地 CSRF），现在要带本机令牌
    _deny = _guard_mutating(request)
    if _deny:
        print("[warn] /api/quit 被拒绝：来源未通过本机校验")
        return _deny

    # 【2026-10-09 升级体验口径】退出即自动安装已就绪的新版本（不弹确认框、不要求用户点确定）
    _installing = _install_ready_update()

    def _die():
        time.sleep(0.6)
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
    return {"ok": True, "message": "正在更新并重启" if _installing else "服务正在退出"}


# ---------------- 翻译（国际栏目 英 → 中） ----------------
# MyMemory 免费免 key 但有每日额度，故译文永久缓存到本地磁盘：
# 同一条原文一辈子只翻译一次，后续刷新与重启都直接命中缓存。
_TRANS_CACHE_FILE = os.path.join(
    os.getenv("LOCALAPPDATA") or tempfile.gettempdir(), "hotnews", "translate_cache.json")
_TRANS_CACHE = {}
_TRANS_DIRTY = [False]
_TRANS_LOCK = threading.Lock()
# 翻译熔断：本批一旦撞上限流/额度用尽就置位，后续条目直接放原文。
# 额度按天重置，所以每批任务开始时复位一次（给后端一次机会），而不是永久性熔断。
_TRANS_BREAK = [False]
_TRANS_BUSY = [False]       # 是否有后台翻译任务正在进行（避免重复启动）


def _load_trans_cache():
    global _TRANS_CACHE
    try:
        if os.path.exists(_TRANS_CACHE_FILE):
            with open(_TRANS_CACHE_FILE, "r", encoding="utf-8") as f:
                _TRANS_CACHE = json.load(f)
            print(f"[ok] 翻译缓存载入 {len(_TRANS_CACHE)} 条")
    except Exception:
        _TRANS_CACHE = {}


def _save_trans_cache():
    try:
        os.makedirs(os.path.dirname(_TRANS_CACHE_FILE), exist_ok=True)
        with open(_TRANS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(_TRANS_CACHE, f, ensure_ascii=False)
    except Exception:
        pass


def _translate_backend(text):
    """调用配置的翻译后端（baidu / deepl / mymemory）。

    返回 (译文, 状态)：状态为 ok / quota / err。
      quota = 当日免费额度用尽或被限流（429 / MYMEMORY WARNING），再试也没用 → 调用方熔断；
      err   = 网络抖动等可重试错误。
    早期版本失败一律返回空串并盲目重试，实测 MyMemory 额度耗尽时会让 45 段各重试 3 次、
    每段白等数秒，单次抓取被拖到 90s 以上。区分出 quota 才能第一时间停手。"""
    cfg = CONFIG.get("translate", {})
    backend = cfg.get("backend", "mymemory")
    try:
        if backend == "baidu":
            bd = cfg.get("baidu", {})
            if not bd.get("appid") or not bd.get("secret"):
                return "", "err"
            salt = str(int(time.time()))
            sign = hashlib.md5((bd["appid"] + text + salt + bd["secret"])
                               .encode("utf-8")).hexdigest()
            with httpx.Client(timeout=15, trust_env=False) as c:
                r = c.get("https://fanyi-api.baidu.com/api/trans/vip/translate", params={
                    "q": text, "from": "en", "to": "zh",
                    "appid": bd["appid"], "salt": salt, "sign": sign})
                js = r.json()
            if js.get("trans_result"):
                return js["trans_result"][0]["dst"], "ok"
            return "", "quota" if r.status_code == 429 else "err"
        if backend == "deepl":
            dk = cfg.get("deepl", {})
            if not dk.get("auth_key"):
                return "", "err"
            with httpx.Client(timeout=15, trust_env=False) as c:
                r = c.post("https://api-free.deepl.com/v2/translate", data={
                    "text": text, "target_lang": "ZH", "auth_key": dk["auth_key"]})
                js = r.json()
            if js.get("translations"):
                return js["translations"][0]["text"], "ok"
            return "", "quota" if r.status_code == 429 else "err"
        # mymemory：免费免 key。配了 contact(邮箱) 可把额度从 5k/天 提到 50k/天
        params = {"q": text[:480], "langpair": "en|zh-CN"}
        if cfg.get("contact"):
            params["de"] = cfg["contact"]
        with httpx.Client(timeout=15, trust_env=False) as c:
            r = c.get("https://api.mymemory.translated.net/get", params=params)
            if r.status_code == 429:
                return "", "quota"
            js = r.json()
        t = (js.get("responseData") or {}).get("translatedText", "")
        if t and "MYMEMORY WARNING" not in t.upper():
            return html.unescape(t), "ok"          # 接口返回的是 HTML 实体
        # 返回体里带 MYMEMORY WARNING 就是那句「今日免费额度已用完」
        return "", ("quota" if "MYMEMORY WARNING" in t.upper() else "err")
    except Exception:
        return "", "err"


def _translate_text(text):
    """英→中，带磁盘持久缓存。失败返回空串（调用方回退原文，绝不因翻译失败丢内容）。"""
    if not text:
        return ""
    text = text.strip()
    hit = _TRANS_CACHE.get(text)
    if hit:
        return hit
    if not CONFIG.get("translate", {}).get("enabled", True):
        return ""
    if _TRANS_BREAK[0]:
        return ""                      # 本批已熔断（额度用尽），直接放原文，不再发无效请求
    # MyMemory 并发稍高会间歇性拒绝（并非额度耗尽），失败后降速重试两次
    out = ""
    for attempt in range(3):
        out, st = _translate_backend(text)
        if out:
            break
        if st == "quota":
            # 额度是按天重置的，同一批剩下的几十段再试也必然失败：立刻熔断，
            # 把整批的耗时从「每段各等 3 次重试」压到「只失败一次」。
            _TRANS_BREAK[0] = True
            print("[warn] 翻译额度用尽/被限流 → 本批熔断，其余条目保留英文原文")
            return ""
        time.sleep(0.6 * (attempt + 1))
    if out and out != text:
        with _TRANS_LOCK:
            _TRANS_CACHE[text] = out
            _TRANS_DIRTY[0] = True
    return out or ""


def _translate_items(items, max_items=200):
    """把国际条目的标题/摘要翻成中文；英文原文保留到 title_en / desc_en（前端悬停可见）。"""
    dirty = False
    todo = [it for it in items if not it.get("translated")][:max_items]
    if not todo:
        return items
    _TRANS_BREAK[0] = False       # 新一批从干净状态开始（额度可能已跨天恢复）
    # 【2026-10-07 修「国际娱乐一部分变英文」】原来 title 和 desc 交替入队，
    # 摘要（200 字≈40 词）是标题的 4 倍长，161 条一起译要 8000+ 词，
    # 直接把 MyMemory 免费额度（匿名 5k 词/天）打爆 → 连标题都没译出来。
    # 改成**两轮：先把所有标题译完，再译摘要**。额度不够时，最显眼的标题一定全中文。
    jobs = []
    for it in todo:
        it["title_en"] = it.get("title") or ""
        if it.get("title"):
            jobs.append((it, "title", (it["title"] or "")[:300]))
    for it in todo:
        if it.get("desc"):
            it["desc_en"] = it.get("desc") or ""
            jobs.append((it, "desc", (it["desc"] or "")[:300]))
    # 缓存命中的立即回填
    pending = []
    for it, field, src in jobs:
        cached = _TRANS_CACHE.get(src)
        if cached:
            it[field] = cached
            it["translated"] = True
        else:
            pending.append((it, field, src))
    if pending:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=4) as ex:
            outs = list(ex.map(lambda x: _translate_text(x[2]), pending))
        ok = 0
        for (it, field, src), out in zip(pending, outs):
            if out:
                it[field] = out
                it["translated"] = True
                ok += 1
        print(f"[ok] 翻译 {ok}/{len(pending)} 段，耗时 {time.time() - t0:.1f}s")
        if ok < len(pending):
            # 多半是 MyMemory 当日免费额度用尽（返回 MYMEMORY WARNING）。译不出的保留英文，
            # 且**不写缓存**，下一轮会重试，额度恢复后自动补齐。
            print(f"[warn] 有 {len(pending) - ok} 段未译出（额度用尽或被限流），保留英文原文，稍后重试")
        dirty = True
    if dirty or _TRANS_DIRTY[0]:
        with _TRANS_LOCK:
            _TRANS_DIRTY[0] = False
            _save_trans_cache()
    return items


def _translate_intl_bg(items):
    """后台把国际条目译成中文。就地修改这批 item（与 PRELOAD 里是同一批对象引用），
    所以译文补上后，前端下一次拉数据自然就是中文，用户无需任何操作。"""
    _TRANS_BUSY[0] = True
    try:
        t0 = time.time()
        _translate_items(items)
        n = sum(1 for it in items if it.get("translated"))
        print(f"[ok] 国际后台翻译：{n}/{len(items)} 条已中文化，耗时 {time.time() - t0:.1f}s")
    except Exception as ex:
        print(f"[warn] 后台翻译异常: {type(ex).__name__}（保留英文原文）")
    finally:
        _TRANS_BUSY[0] = False


# 【2026-10-09 修 低-8】/api/translate 的单次长度上限。
# 这个接口没有鉴权（本机页面用），不设限的话任何本机脚本都能拿它把
# MyMemory 的免费额度刷光（额度用完国际板块就整体退回英文）。
TRANS_MAX_CHARS = 2000


@app.get("/api/translate")
def api_translate(text: str = Query(""), target: str = "zh-CN"):
    cfg = CONFIG.get("translate", {})
    if not cfg.get("enabled", True) or not text:
        return {"ok": False, "reason": "disabled"}
    # 【2026-10-09 修 低-8】限长（不截断，直接拒绝）：让调用方知道超了，而不是悄悄翻一半
    if len(text) > TRANS_MAX_CHARS:
        return {"ok": False,
                "reason": f"文本过长：{len(text)} 字，单次上限 {TRANS_MAX_CHARS} 字"}
    out = _translate_text(text)
    if out:
        with _TRANS_LOCK:
            _TRANS_DIRTY[0] = False
            _save_trans_cache()
        return {"ok": True, "backend": cfg.get("backend", "mymemory"), "text": out}
    return {"ok": False, "reason": "翻译失败或额度耗尽"}


@app.get("/img")
def proxy_img(url: str = Query(...)):
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "bad url"}, status_code=400)
    # 【2026-10-09 修 高-1】来源白名单 + 内网地址拦截。原来只校验协议前缀就直连，
    # 任意网页都能拿它探内网（见上面 _img_url_block_reason 的说明）。
    _why = _img_url_block_reason(url)
    if not _why and not _img_host_allowed(urllib.parse.urlsplit(url).hostname):
        _why = "非本项目登记来源"
    if _why:
        print(f"[warn] /img 拒绝代理（{_why}）: {url[:110]}")
        return JSONResponse({"error": f"refused: {_why}"}, status_code=403)
    # 这里**不带 max_bytes**：要的就是完整图。_fetch 会走 _IMG_CACHE（完整本体那条缓存），
    # 不会命中 _measure_image 留下的探测片段（修 阻断-2）。
    res = _fetch(url)
    # _fetch 有两种返回：图片扩展名 → (bytes, content_type)；其余（如无扩展名的图床链接）→ bytes。
    # 以前一律按 tuple 解包，遇到后者 res[0] 是 int，触发 AttributeError 并让整个请求 500。
    if isinstance(res, tuple):
        blob, ctype = res[0], res[1]
    else:
        blob, ctype = res, "image/jpeg"
    if not blob or not isinstance(blob, (bytes, bytearray)):
        return Response(status_code=404)
    # ctype 一律用源站给的真实值（_fetch 已按响应头取好），不再写死 image/jpeg
    return Response(content=blob, media_type=ctype or "image/jpeg",
                    headers={"Cache-Control": f"public, max-age={IMG_CACHE_TTL}"})


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    v = _load_version()
    ssr = ""
    tpl = _html_template("index.html")      # 【修 低-8】启动时读过一次就不再读盘
    if tpl:
        payload = {
            "version": VERSION,
            "region": "cn",
            # 【修 高-4】有副作用的接口（退出/更新/开机自启）要带这个令牌
            "token": TOKEN,
            # 把预热好的条目直接塞进首屏，打开页面立刻有内容，不用等 JS 再拉一次
            "items": PRELOAD.get("cn", [])[:60],
        }
        # 【修 高-1】首屏这几条的图片域名也登记进 /img 白名单
        _note_item_hosts(payload["items"])
        blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        ssr = tpl.replace("__SSR_DATA__", blob)
        ssr = ssr.replace('__SSR_REGION__', "cn")
        ssr = ssr.replace("__SSR_VERSION__", VERSION)
        return HTMLResponse(ssr, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    return HTMLResponse("<h1>hotnews</h1>")


# 【2026-10-09 修 低-8】静态 HTML 模板缓存：`/` 与 `/world` 原来**每个请求都读一次盘**
# （首页每次刷新都重新读 30KB 的 index.html）。模板是随程序分发的静态资源，
# 启动时读一次就够；改了模板需要重启程序（对单文件 exe 而言本来也是这样）。
_HTML_TPL = {}


def _html_template(name):
    """读并缓存 static/<name>；读不到返回空串（调用方各自兜底）。"""
    if name not in _HTML_TPL:
        try:
            with open(os.path.join(STATIC_DIR, name), "r", encoding="utf-8") as f:
                _HTML_TPL[name] = f.read()
        except Exception:
            _HTML_TPL[name] = ""
    return _HTML_TPL[name]


def _load_version():
    p = _res("version.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"version": VERSION}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/world", response_class=HTMLResponse)
def world_page():
    """国际板的「纯英文版」页面。

    背景：国际源原文是英文，机翻接口（MyMemory）不限流时才可用，慢的时候一组要 90s+，
    所以抓取链路已经不等翻译。这一页把内容以真正的英文文档返回（<html lang="en">），
    交给浏览器自带的整页翻译（Edge/Chrome）接管 —— 用的是浏览器自己的翻译服务，
    速度快、没有额度限制，也不需要我们的服务器去做任何请求。
    顶部也写了中文操作提示，用户进来就能看懂怎么一键变中文。"""
    # 【2026-10-09 修 低-8】与首页同样走模板缓存，不再每次请求读盘
    tpl = _html_template("intl.html")
    if tpl:
        return HTMLResponse(tpl,
                            headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    return HTMLResponse("<h1>world page missing</h1>")


if __name__ == "__main__":
    import argparse
    import uvicorn
    # 【2026-10-09 修 高-1】默认只监听本机：这里原来写死 host="0.0.0.0"，
    # 直接 `python server.py` 起服务等于把本机暴露给同网段（/img 会被当开放代理用）。
    # 确实要让别的设备访问时，显式传 --host 0.0.0.0（那时有副作用的接口仍要求本机令牌）。
    _ap = argparse.ArgumentParser(description="hotnews 服务（默认仅本机可访问）")
    _ap.add_argument("--host", default="127.0.0.1",
                     help="监听地址，默认 127.0.0.1；要对局域网开放时显式写 0.0.0.0")
    _ap.add_argument("--port", type=int, default=8000, help="监听端口，默认 8000")
    _args = _ap.parse_args()
    uvicorn.run(app, host=_args.host, port=_args.port, log_level="info")
