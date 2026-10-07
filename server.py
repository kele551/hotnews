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
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timezone, timedelta
from collections import Counter
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

# 由 app.py 在启动时写入真实访问地址（端口可能被自动顺延），供通知点击跳转使用
APP_URL = "http://localhost:8000"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CST = timezone(timedelta(hours=8))
CACHE_TTL = 300          # RSS 缓存 5 分钟
IMG_CACHE_TTL = 86400    # 图片缓存 1 天
# 【加速】部分下载上限（2026-10-07）：
#   ART_PAGE_BYTES —— 抓文章页只要 <head> 里的 og:image，96KB 足够，不必下整页
#   IMG_HEAD_BYTES —— 测图片尺寸只要文件头，256KB 足够覆盖 JPEG 的 SOF 段
ART_PAGE_BYTES = 96 * 1024
IMG_HEAD_BYTES = 256 * 1024
# 用户要求（2026-10-07）：所有板块必须是「当天的」。
# 采集阶段先用 24 小时窗口（放得够宽，便于凌晨做「当天不足则回退」的兜底），
# 真正「只留当天」的收紧在 _collect 里由 _select_fresh 统一执行。
# 国内 24 小时（用户要求「当天」）；国际放宽到 48 小时 ——
# 实测国际源条目稀疏（CGTN 最新 32 小时前、UN News 23 小时前、France24 跨度可达两天），
# 按 24 小时收口会把好几个刚加的源直接滤空。
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
    "科技": 96,     # 4 天：IT之家/快科技日更，爱范儿/雷峰网/NotebookCheck 慢一些
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
        # 科技/硬件评测：notebookcheck-cn.com 是 TYPO3 站点、无 RSS，走 HTML 解析（parser=nbc）
        # 2026-10-07：改抓「新闻」列表页而不是首页——首页条目少且混着导航，
        # Notebookcheck-NBC.22034.0.html 才是站方的新闻归档列表（用户指定）。
        {"id": "cn-nbc",     "label": "NotebookCheck", "url": "https://www.notebookcheck-cn.com/Notebookcheck-NBC.22034.0.html", "base": "https://www.notebookcheck-cn.com", "region": "cn", "cls": "科技", "parser": "nbc"},
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
        # parser 内现取挑战 cookie 回放绕过
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
    ],
    "intl": [
        {"id": "f24-main",    "label": "France24", "url": "https://www.france24.com/en/rss",         "base": "https://www.france24.com", "region": "intl", "cls": "要闻"},
        {"id": "f24-sport",   "label": "France24", "url": "https://www.france24.com/en/sport/rss",   "base": "https://www.france24.com", "region": "intl", "cls": "体育"},
        # 【2026-10-07 补源】用户要求「国际板块新闻源太少，多加入一些」。
        # 以下 5 个都是**实测连通且带图**的（条目/带图/最新）：
        #   CGTN 50/50/10-06、UN News 30/30/10-06、ABC News 25/25/10-07、
        #   NPR 10/10/10-06、Sky News 6/6/10-07
        # 实测**连不上、未采用**：BBC、CNN、NYT、Guardian、Al Jazeera、DW、SCMP、
        #   纽约时报中文、联合早报（本机网络全部 ConnectTimeout）
        # 实测**无图、未采用**：CBS（30 条但 0 张图，会被「没图不要」的规则剔除）
        # 实测**停更、未采用**：人民网国际（最新 2025-06-05，停更一年多）
        {"id": "intl-npr",     "label": "NPR",      "url": "https://feeds.npr.org/1001/rss.xml",                     "base": "https://www.npr.org",    "region": "intl", "cls": "要闻"},
        {"id": "intl-sky",     "label": "Sky News", "url": "https://feeds.skynews.com/feeds/rss/world.xml",          "base": "https://news.sky.com",   "region": "intl", "cls": "要闻"},
        {"id": "intl-un",      "label": "UN News",  "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml",  "base": "https://news.un.org",    "region": "intl", "cls": "要闻"},
        {"id": "intl-cgtn",    "label": "CGTN",     "url": "https://www.cgtn.com/subscribe/rss/section/world.xml",    "base": "https://www.cgtn.com",   "region": "intl", "cls": "要闻"},
        {"id": "intl-abc",     "label": "ABC News", "url": "https://abcnews.go.com/abcnews/internationalheadlines",  "base": "https://abcnews.go.com", "region": "intl", "cls": "要闻"},
        # 娱乐栏目（国际）：f24-culture（文化）已按用户要求删除，改用国际娱乐八卦源。
        # Variety 实测 10 条全带图；Billboard 10 条无图（音乐明星向），国际不做无图过滤，占位显示。
        # 【2026-10-07 补源】实测连通且**全部带图**的国际娱乐源：
        # NME 10条/10图、Stereogum 40条/40图、Consequence 15条/15图。
        # 实测失败未采用：Deadline/Pitchfork/People/TheWrap/Vulture/Collider 连不上；
        # HollywoodReporter/RollingStone/IndieWire 能连通但 0 张图（会被无图规则剔除）。
        {"id": "intl-nme",       "label": "NME",        "url": "https://www.nme.com/feed",           "base": "https://www.nme.com",        "region": "intl", "cls": "娱乐", "max_age_hours": 72},
        {"id": "intl-stereogum", "label": "Stereogum",  "url": "https://www.stereogum.com/feed/",     "base": "https://www.stereogum.com",  "region": "intl", "cls": "娱乐", "max_age_hours": 72},
        {"id": "intl-consequence","label": "Consequence","url": "https://consequence.net/feed/",       "base": "https://consequence.net",    "region": "intl", "cls": "娱乐", "max_age_hours": 72},
        {"id": "intl-variety",   "label": "Variety",   "url": "https://variety.com/feed/",    "base": "https://variety.com",   "region": "intl", "cls": "娱乐", "max_age_hours": 72},
        {"id": "intl-billboard", "label": "Billboard", "url": "https://www.billboard.com/feed/", "base": "https://www.billboard.com", "region": "intl", "cls": "娱乐", "max_age_hours": 72},
    ],
    # 实时热点（今日头条热榜）走独立 _parse_hot，不走 RSS，这里仅占位以通过 region 校验
    "hot": [],
}

# 无题图的集合类源（国内软件类 RSS 无配图）：_collect 豁免无图过滤，前端走渐变块样式
# 用户要求（2026-10-07）：「每条新闻必须有高清大图，没图的就不要上」。
# 因此原来的 NOIMG_SOURCES 豁免已全部取消——开源中国 / 少数派这类不配图的源，
# 要么补上图，要么就从列表里消失（不再用渐变色块占位）。
NOIMG_SOURCES = set()

_CACHE = {}     # url -> (ts, payload)
_IMG_CACHE = {} # url -> (ts, bytes, ctype)
PRELOAD = {"cn": [], "intl": [], "hot": []}   # 启动时预热好的条目，API 直接取用
# 「当天」过滤之前的完整池子（按各源窗口，含最近几天）。
# 主列表只给当天（用户原则：常看常新、绝不看旧新闻），
# 但**搜索必须能搜到更早的新闻** —— 程序名叫「热点新闻检索」，这是它的本分。
PRELOAD_ALL = {"cn": [], "intl": [], "hot": []}


def _load_config():
    default = {
        "update": {"enabled": True},
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
_HTTP_LIMITS = httpx.Limits(max_connections=48, max_keepalive_connections=24,
                            keepalive_expiry=30.0)
_CLIENT = httpx.Client(follow_redirects=True, trust_env=False, timeout=10,
                       limits=_HTTP_LIMITS, headers={"User-Agent": UA})

# 文章页 → 题图 URL。og:image 基本不会变，进程内永久记住，避免每轮刷新重抓一遍。
_ART_CACHE = {}


def _fetch(url, timeout=10, max_bytes=None, extra_headers=None):
    # 按「是否图片」决定返回形态：图片返回 (bytes, ctype) 元组；文档/RSS 返回原始字节。
    # 旧逻辑靠 URL 含 "rss" 判断文档，但小众软件(/feed/)、notebookcheck(/) 等源不含
    # "rss" 会被误判成图片 → feedparser 收到元组崩溃。改为按图片扩展名判定，覆盖所有源。
    _low = url.lower()
    _is_img = _low.startswith(("http://", "https://")) and (
        _low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"))
        or ".jpg?" in _low or ".png?" in _low or ".webp?" in _low or ".gif?" in _low
        or "/img/" in _low
    )
    key = ("img", url) if _is_img else ("doc", url)
    # 带了额外请求头（如 mefcl 的挑战 cookie）时缓存键要区分开，
    # 否则先抓到的「挑战页」会把后面带 cookie 的正确结果顶掉。
    _ck = url
    if extra_headers:
        _ck = url + "|" + ",".join(f"{k}={v}" for k, v in sorted(extra_headers.items()))
    now = datetime.now().timestamp()
    cache = _IMG_CACHE if key[0] == "img" else _CACHE
    hit = cache.get(_ck)
    if hit and (now - hit[0]) < (IMG_CACHE_TTL if key[0] == "img" else CACHE_TTL):
        return hit[1]
    try:
        # trust_env=False：不走系统/沙箱代理，本机直连国内站点更快更稳
        hdrs = {}
        if key[0] == "img":
            # 部分图片 CDN 靠 Referer 防盗链，带上来源站点域名
            try:
                hdrs["Referer"] = urllib.parse.urlsplit(url).netloc
            except Exception:
                pass
        if extra_headers:
            hdrs.update(extra_headers)
        if max_bytes:
            # 【加速】只读前 N 字节就断开：og:image 在 <head>、图片尺寸在头部几十字节里，
            # 整页下载纯属浪费（新闻页常有 100~500KB）。实测这是补图环节最大的一笔开销。
            buf = bytearray()
            with _CLIENT.stream("GET", url, timeout=timeout, headers=hdrs) as r:
                if r.status_code == 200:
                    for chunk in r.iter_bytes(16384):
                        buf.extend(chunk)
                        if len(buf) >= max_bytes:
                            break
            data = bytes(buf) if buf else None
            out = ((data, "image/jpeg") if data else (None, "")) if key[0] == "img" else data
        else:
            r = _CLIENT.get(url, timeout=timeout, headers=hdrs)
            if key[0] == "img":
                if r.status_code == 200:
                    out = (r.content, r.headers.get("content-type", "image/jpeg"))
                else:
                    out = (None, "")
            else:
                # 必须返回原始字节 r.content，而不是 r.text：
                # httpx 的 r.text 会用它猜的编码（常误判 GBK 源为 utf-8）解码成字符串，
                # 一旦源字节不是合法 utf-8（如中关村在线 RSS 是 GBK），标题就被换成 U+FFFD 乱码。
                # 交给 feedparser.parse(bytes) 按各源 XML 声明的编码自行解码，才能正确还原中文。
                out = r.content if r.status_code == 200 else None
    except Exception:
        out = None
    cache[_ck] = (now, out)
    return out


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
# 定 300 的依据是实测：300 能保住界面新闻(580x330)、NotebookCheck(672x504)、小众软件多数图；
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


def _parse_one(src):
    # notebookcheck 走 HTML 解析分支（无 RSS）
    if src.get("parser") == "nbc":
        return _parse_nbc(src)
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
    # GitHub curated 软件集合仓库（awesome 列表）：抓 README raw 解析软件条目
    if src.get("parser") == "github_readme":
        return _parse_github_readme(src)
    raw = _fetch(src["url"])
    if not raw:
        print(f"[warn] RSS 抓取失败: {src['url']}")
        return []
    try:
        doc = feedparser.parse(raw)
    except Exception as ex:
        print(f"[warn] RSS 解析失败: {src['url']} ({type(ex).__name__})")
        return []
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
        pub = e.get("published_parsed") or e.get("updated_parsed")
        ts = int(datetime(*pub[:6], tzinfo=CST).timestamp()) if pub else 0
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


def _hot_baidu():
    """百度实时热点：HTML 内嵌 <!--s-data:...--> JSON，解析 cards[].content[]。"""
    try:
        with httpx.Client(follow_redirects=True, timeout=8, trust_env=False, headers={"User-Agent": UA}) as c:
            r = c.get("https://top.baidu.com/board?tab=realtime")
        t = r.text if r.status_code == 200 else ""
    except Exception as ex:
        print(f"[warn] 百度热榜抓取异常: {type(ex).__name__}")
        return []
    m = re.search(r"<!--s-data:([\s\S]*?)-->", t)
    if not m:
        return []
    try:
        d = json.loads(m.group(1))
    except Exception:
        return []
    out, rank = [], 0
    for card in (d.get("data", {}).get("cards") or []):
        for it in (card.get("content") or []):
            word = (it.get("word") or "").strip()
            if not word:
                continue
            rank += 1
            u = it.get("rawUrl") or it.get("url") or ""
            if u and u.startswith("/"):
                u = "https://top.baidu.com" + u
            if not u:
                u = "https://www.baidu.com/s?wd=" + urllib.parse.quote(word)
            heat = it.get("hotScore") or 0
            out.append({
                "id": hashlib.md5(("hot-baidu-" + word).encode("utf-8")).hexdigest()[:12],
                "region": "hot", "channel": "baidu-hot", "label": "百度",
                "cls": "热榜", "title": word, "desc": "", "link": u, "image": "",
                "published": 0, "heat": int(heat) if str(heat).isdigit() else 0,
                "rank": rank, "translated": False,
            })
    print(f"[ok] hot/百度: {len(out)} 条")
    return out


def _hot_bilibili():
    """B站热门：api.bilibili.com/x/web-interface/popular JSON。"""
    try:
        with httpx.Client(follow_redirects=True, timeout=10, trust_env=False,
                          headers={"User-Agent": UA, "Referer": "https://www.bilibili.com"}) as c:
            r = c.get("https://api.bilibili.com/x/web-interface/popular?ps=20&pn=1")
        j = r.json() if r.status_code == 200 else {}
    except Exception as ex:
        print(f"[warn] B站热榜抓取异常: {type(ex).__name__}")
        return []
    out = []
    for idx, it in enumerate((j.get("data", {}).get("list") or [])[:20], 1):
        title = (it.get("title") or "").strip()
        if not title:
            continue
        bvid = it.get("bvid") or ""
        u = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
        view = (it.get("stat") or {}).get("view") or 0
        out.append({
            "id": hashlib.md5(("hot-bili-" + title).encode("utf-8")).hexdigest()[:12],
            "region": "hot", "channel": "bili-hot", "label": "B站",
            "cls": "热榜", "title": title, "desc": "", "link": u, "image": "",
            "published": 0, "heat": int(view) if str(view).isdigit() else 0,
            "rank": idx, "translated": False,
        })
    print(f"[ok] hot/B站: {len(out)} 条")
    return out


def _hot_cn_intl():
    """中新社·国际（chinanews.com.cn/rss/world.xml）——国际热榜的中文底座。

    为什么国际热榜必须有一家中文源：国际板块其余源（France24 / Variety / Billboard /
    Hacker News）全是英文，要看得懂全靠后台机器翻译；而免费翻译额度一旦被限流，
    整个板块当场退回英文（实测某次 120 段只译出 2 段）。这条 RSS 本身就是中文国际新闻，
    不依赖任何翻译服务，保证「国际热榜」在任何情况下都看得懂。
    中新网 RSS 和文章页都没有可用题图，但热榜是纯文本列表，本来也不需要图。"""
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
        ts = 0
        tp = e.get("published_parsed") or e.get("updated_parsed")
        if tp:
            try:
                ts = int(datetime(*tp[:6], tzinfo=timezone.utc).timestamp())
            except Exception:
                ts = 0
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


def _hot_hn():
    """Hacker News 实时热门榜（国际热榜数据源）。

    为什么选它：Reddit r/worldnews、BBC most-popular、Guardian、Al Jazeera、Google News
    RSS、DW、EuroNews 在国内网络实测全部 ConnectTimeout，唯一既带「真实热度数值」又能连通的
    国际榜单只剩 HN 的 Firebase 官方 API（免 key）。这里 score 是用户真实投票点数、
    descendants 是评论条数，都能在点开的页面上对得上 —— 不像头条那种站内加权值。
    内容偏科技/创投，但也覆盖国际重大事件，符合「国际上现在最热」的语义。"""
    # 【加速】4 分钟内直接复用上次结果。原来 _HN_CACHE 定义了却从没被用过，
    # 导致每次刷新都把这 30 条重新抓一遍。
    if _HN_CACHE["items"] and (time.time() - _HN_CACHE["ts"]) < 240:
        return _HN_CACHE["items"]
    try:
        # 复用全局连接池（同域长连接），不要再 new 一个 Client
        r = _CLIENT.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=8)
        ids = r.json() if r.status_code == 200 else []
    except Exception as ex:
        print(f"[warn] HN 榜单抓取异常: {type(ex).__name__}")
        return _HN_CACHE["items"]
    ids = [i for i in (ids or []) if isinstance(i, int)][:30]

    def one(i):
        # 只试一次：原来失败要 5s + sleep + 5s = 10s，30 条一起就把整轮拖长一倍。
        # HN 少一条对榜单毫无影响，速度优先。
        try:
            r = _CLIENT.get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json", timeout=5)
            return r.json() if r.status_code == 200 else None
        except Exception:
            return None

    out = []
    # 30 条一次性全放出去：16 线程会分 2 批，等于把超时时间乘 2（实测 19s）。
    with ThreadPoolExecutor(max_workers=30) as ex:
        for it in ex.map(one, ids):
            if not it:
                continue
            title = _clean(it.get("title") or "")
            if not title:
                continue
            iid = it.get("id")
            url = it.get("url") or f"https://news.ycombinator.com/item?id={iid}"
            out.append({
                "id": hashlib.md5(("hot-hn-" + str(iid)).encode("utf-8")).hexdigest()[:12],
                "region": "intl", "channel": "hn-hot", "label": "Hacker News",
                "cls": "热榜", "title": title, "desc": "", "link": url, "image": "",
                "published": int(it.get("time") or 0),
                "heat": int(it.get("score") or 0),
                "comments": int(it.get("descendants") or 0),
                "rank": len(out) + 1, "translated": False,
            })
    print(f"[ok] hot/HN: {len(out)} 条")
    _HN_CACHE["ts"] = time.time()
    _HN_CACHE["items"] = out
    return out


_HN_CACHE = {"ts": 0, "items": []}

# 澎湃 rightSidebar 的共享缓存（被 cn 要闻和热榜两个入口共用，见 _thepaper_data）
_THEPAPER_CACHE = {"ts": 0.0, "data": None}
_THEPAPER_LOCK = threading.Lock()


def _parse_hot_intl():
    """国际实时热榜 = 中新社·国际（中文，不依赖翻译，永远读得懂）
                   + Hacker News（英文科技/创投，能译则译）。

    两家独立，任一失败不影响另一家：
      - 中新社每次都抓（RSS 轻快，实测 0.1~1.4s）。中文条目已标记 translated=True，
        后台翻译会跳过，不会拿去「中译中」。
      - HN 要逐个 item 请求 Firebase，慢得多，故带 3 分钟本地缓存（榜单变动慢，不必每次请求）。
    中新社排在前面，保证热榜靠前的部分一定是可读的中文。"""
    parts = []
    try:
        parts.extend(_hot_cn_intl())
    except Exception as ex:
        print(f"[warn] 国际热榜子源异常: {type(ex).__name__}")
    now = time.time()
    hn = None
    if _HN_CACHE["items"] and now - _HN_CACHE["ts"] < 180:
        hn = _HN_CACHE["items"]
    else:
        hn = _hot_hn()
        _HN_CACHE["ts"], _HN_CACHE["items"] = now, hn
    if hn:
        parts.extend(hn)
    # 【2026-10-07 用户建议】「灵活一点，国际板块的热榜也可以用凤凰、澎湃、红星的
    # 国际新闻来体现」—— 国内热榜里被剔掉的国际新闻**不丢**，转来充实国际热榜。
    # 好处：国际热榜从 2 家（中新社/HN）变成最多 5 家，且都是中文，不用等翻译。
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
    结果这 20 条全都算「无时间」，把时效过滤整个绕过去了
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


def _parse_nbc(src):
    """NotebookCheck 中文版（notebookcheck-cn.com）：TYPO3 站点无 RSS，抓首页新闻列表解析。
    HTML 块结构稳定：<a class="introa_*" href="...html"> 内含
      <picture><img src="fileadmin/_processed_/.../csm_*.jpg">          （列表缩略图，仅 ~126px，低质）
      <source srcset=".../_nc5/xxx-q82-w672-h.webp ...">                （同图更大尺寸，用这个保高清）
      <h2 class="introa_title">标题</h2>
      <div class="introa_rm_abstract">摘要</div>
      <span class="itemdate itemdate_<ts>" data-crdate="<ts>">日期</span>
    图片优先取 <picture> 里最大的 webp（w672，约 672px 宽），回落 csm_ jpg；均经 _norm_url 补全为绝对地址。
    仅保留 72h 内条目（与国内源时效一致）。站点退化时返回 []，不影响其它源。"""
    raw = _fetch(src["url"])
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print(f"[warn] notebookcheck 抓取失败: {src['url']}")
        return []
    html = raw.decode("utf-8", "ignore")
    items = []
    cutoff = datetime.now(CST).timestamp() - MAX_AGE_HOURS.get("cn", 72) * 3600
    for m in re.finditer(r'<a\s+class="introa_[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        link, blk = m.group(1), m.group(2)
        if "notebookcheck-cn.com" not in link:
            continue
        t = re.search(r'introa_title"[^>]*>([^<]+)<', blk)
        if not t:
            continue
        title = _clean(t.group(1))
        if not title:
            continue
        d = re.search(r'introa_rm_abstract"[^>]*>(.*?)</div>', blk, re.S)
        desc = _clean(d.group(1))[:200] if d else ""
        # 图片：<picture> 里挑含 w672 的 webp（最大尺寸），否则取第一条 webp，再回落 csm_ jpg
        img = ""
        ss = re.search(r'srcset="([^"]+_nc5/[^"]+\.webp[^"]*)"', blk)
        if ss:
            cands = [s.strip().split()[0] for s in ss.group(1).split(",") if "w672" in s]
            pick = cands[0] if cands else re.search(r'(/fileadmin/_processed_/webp/[^" ]+\.webp)', ss.group(1))
            if isinstance(pick, str):
                img = pick
        if not img:
            csm = re.search(r'src="([^"]*csm_[^"]*\.(?:jpg|jpeg|png|webp))"', blk)
            if csm:
                img = csm.group(1)
        if img:
            img = _norm_url(img, src["base"])
        ts_m = re.search(r'itemdate_(\d+)', blk) or re.search(r'data-crdate="(\d+)"', blk)
        ts = int(ts_m.group(1)) if ts_m else 0
        if ts and ts < cutoff:
            continue
        items.append({
            "id": hashlib.md5((src["id"] + title).encode("utf-8")).hexdigest()[:12],
            "region": "cn",
            "channel": src["id"],
            "label": src.get("label", src["id"]),
            "cls": src["cls"],
            "title": title,
            "desc": desc,
            "link": link if link.startswith("http") else src["base"] + link,
            "image": img or "",
            "published": ts,
            "translated": False,
        })
    print(f"[ok] {src['id']}: notebookcheck 解析 {len(items)} 条（72h 内）")
    return items


# mefcl 挑战 cookie 缓存：站方每次生成的 cookie 30 分钟内有效，有效期内复用，
# 避免每次刷新都打一次挑战页。失效（被挑战页挡回）时把 ts 置 0 强制重新取。
_MEFCL_COOKIE = {"value": None, "ts": 0}
# 最近一次成功解析的结果：抓取瞬时失败（超时/被拦）时回退，避免实时刷新偶发
# 网络抖动把整个 mefcl 板块清空（用户明确要这个源）。
_MEFCL_LAST = []


def _parse_mefcl(src):
    """mefcl.com（用户投稿站点）：站方用 ge_js_validator JS cookie 验证拦截爬虫。

    绕过流程（已实测）：
      1. 先抓首页 → 若命中 278B 挑战页，正则抠出动态生成的合法 cookie 值
         （形如 1791287062@63@<32位hex>，max-age=1800，30 分钟内有效）；
      2. 带合法 Cookie: ge_js_validator_63=<值> 回放首页 → 返回 102KB 真内容；
      3. 解析 <article class="excerpt ..."> 块：标题取 <h2><a> 文本，链接取 <a href>，
         题图取 <img data-src>（src 只是占位 thumbnail.png），摘要取 <p class="note">。

    /feed/ 仍返回 403，故改抓首页 HTML。解析失败 / 被拦返回 []，不影响其它源。"""
    import time as _t
    now = _t.time()
    # 先直接抓首页：站方若已不再弹 JS 挑战，正文（含 article.excerpt）直接返回，
    # 这时无需 cookie，直接解析即可——否则会误判「无挑战字段」而整源消失。
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
        # 命中 JS 验证挑战页 → 抠合法 cookie 回放
        cookie = _MEFCL_COOKIE.get("value")
        if not cookie or (now - _MEFCL_COOKIE.get("ts", 0)) > 1500:
            m = re.search(r'ge_js_validator_63=([^";\s]+)', html0)
            if not m:
                print("[warn] mefcl 无文章块也无挑战 cookie 字段，可能被改版（回退上次结果）")
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
            print("[warn] mefcl 抓取失败（可能被 JS 验证拦截，回退上次结果）")
            return _MEFCL_LAST
        html = raw.decode("utf-8", "ignore")
        # 仍被挑战页挡住（有 cookie 字段但无文章块）→ cookie 失效，下次重取
        if "ge_js_validator" in html and "excerpt" not in html:
            _MEFCL_COOKIE["ts"] = 0
            print("[warn] mefcl 仍返回 JS 验证页，cookie 失效需重试（回退上次结果）")
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


def _clean_md(s):
    """清理 GitHub README 描述里的 markdown 垃圾：图片徽章、引用式/内联链接、行内标记。
    例：[![Open-Source Software][oss]](https://...) ![star] → 全部去掉，只留人话。
    循环剥离以处理嵌套（外层链接的文本本身又是一张引用式图片）。"""
    if not s:
        return ""
    for _ in range(5):
        before = s
        s = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', s)      # 内联图片 ![x](y)
        s = re.sub(r'!\[[^\]]*\]\[[^\]]*\]', '', s)     # 引用式图片 ![x][ref]
        s = re.sub(r'!\[[^\]]*\]', '', s)               # 裸图片 ![x]
        s = re.sub(r'\[[^\]]*\]\[[^\]]*\]', '', s)      # 引用式链接 [x][ref]
        s = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', s)  # 内联链接 [x](y) → x
        if s == before:
            break
    s = re.sub(r'<[^>]+>', '', s)                       # HTML 标签
    s = re.sub(r'[`*_~]+', '', s)                       # 行内标记符
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r'^[\s\-–—:：.,，、]+', '', s).strip()     # 开头残留符号
    return s


def _parse_github_readme(src):
    """GitHub curated 软件集合仓库（README 维护的 awesome 列表）：
    抓 README.md raw，解析列表项「- [名称](链接) - 描述」做成软件推荐卡片。
    无题图，_collect 对该源豁免无图过滤（前端走渐变块样式）。
    抓取失败 / 被拦返回 []，不影响其它源。"""
    repo = src.get("repo") or ""
    if not repo:
        m = re.search(r"github\.com/([^/#?]+/[^/#?]+)", src.get("url", ""))
        repo = m.group(1) if m else ""
    if not repo:
        print(f"[warn] {src['id']} 无法解析仓库地址")
        return []
    raw = None
    for b in (src.get("branch") or "master", "main", "master"):
        u = f"https://raw.githubusercontent.com/{repo}/{b}/README.md"
        r = _fetch(u)
        if r:
            raw = r
            break
    if not raw:
        print(f"[warn] {src['id']} README 抓取失败（可能被拦 / 分支名不符）")
        return []
    text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw
    # 软件条目行：- [名称](url) - 描述（锚点 # 链接、图片链接不匹配 https?://，自然排除）
    rows = re.findall(
        r'^\s*[-*]\s*\[([^\]]+)\]\((https?://[^)\s]+)\)\s*[-–—:]\s*(.+)$',
        text, re.M)
    items, limit = [], src.get("limit", 24)
    ts = int(datetime.now(CST).timestamp())
    for name, link, desc in rows:
        if len(items) >= limit:
            break
        title = _clean(name)
        if not title or len(title) > 60:
            continue
        if "!" in link or link.lower().endswith((".png", ".svg", ".jpg", ".jpeg", ".md", ".gif")):
            continue
        d = _clean(_clean_md(desc))
        if len(d) > 100:                                  # 描述截短，避免卡片太密
            cut = d[:100].rsplit(" ", 1)[0] or d[:100]
            d = cut + "…"
        if not d:
            continue
        items.append({
            "id": hashlib.md5((src["id"] + title).encode("utf-8")).hexdigest()[:12],
            "region": src["region"], "channel": src["id"],
            "label": src.get("label", src["id"]), "cls": src["cls"],
            "title": title, "desc": d,
            "link": _norm_url(link, src.get("base") or "https://github.com"),
            "image": "", "published": ts, "translated": False,
        })
    print(f"[ok] {src['id']}: 解析 {len(items)} 条软件（README {len(text)} 字节，候选 {len(rows)}）")
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
        img = _norm_url(ims[-1], src["base"]) if ims else ""
        seen.add(title)
        items.append({
            "id": hashlib.md5((src["id"] + title).encode("utf-8")).hexdigest()[:12],
            "region": src["region"], "channel": src["id"],
            "label": src.get("label", src["id"]), "cls": src["cls"],
            "title": title, "desc": "", "link": url,
            "image": img, "published": ts or int(datetime.now(CST).timestamp()),
            "translated": False,
        })
        if len(items) >= 30:
            break
    print(f"[ok] {src['id']}: 凤凰娱乐解析 {len(items)} 条")
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


def _article_meta(url):
    """抓一次文章页，同时取回「题图」和「真实发布时间」。

    【为什么合并】补图和校准时间都需要文章页，分两次抓纯属浪费。
    返回 (image, published)；都取不到就是 ("", 0)。

    加速要点（2026-10-07）：
      1) 只要 <head> 里的 og:image 和时间元数据，所以**只读前 ART_PAGE_BYTES 字节**
      2) 结果放进 _ART_CACHE 永久记住（文章元数据不会变）
    """
    if not url or not url.startswith("http"):
        return "", 0, ""
    cached = _ART_CACHE.get(url)
    if cached is not None:
        return cached          # (图, 发布时间, 栏目分类)
    # mefcl 的文章页和首页一样有 ge_js_validator JS 挑战，**必须带上同一个 cookie**，
    # 否则抓回来的是挑战页（几百字节），og:image 取不到 → mefcl 只能留在列表页那张
    # 220x150 缩略图上 → 被高清门槛剔除 → 整个「软件」栏目消失（用户实际反馈）。
    _eh = None
    if "mefcl.com" in url:
        _cv = _MEFCL_COOKIE.get("value")
        if _cv:
            _eh = {"Cookie": "ge_js_validator_63=" + _cv}
    try:
        raw = _fetch(url, timeout=8, max_bytes=ART_PAGE_BYTES, extra_headers=_eh)
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
    if not img:
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
        _ART_CACHE[url] = (img, ts, sec)   # 只缓存有结果；失败不缓存，下次还有机会重试
    return img, ts, sec


def _article_image(url):
    """只要题图（保留旧接口，内部走 _article_meta）。"""
    return _article_meta(url)[0]   # (图, 时间, 栏目)[0]


def _fill_missing_images(items, region, max_fetch=24, workers=14, force_all=False):
    """给缺图的条目去文章页补一张图。补不到的由调用方丢弃。

    热榜（澎湃/红星/凤凰）是纯文字榜，mefcl 列表页只给 220x150 缩略图，
    这两类都必须靠这一步续命，否则按「没图不要」的新规矩会整块消失。
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

    need = [it for it in items if needs(it)][:max_fetch]
    if not need:
        return items
    print(f"[ok] {region}: {len(need)} 条需要补图，去文章页取")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        got = list(ex.map(lambda it: _article_meta(it.get("link") or ""), need))
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

    代价：这一版里「RSS 本身不带图」的源（澎湃/界面/新浪娱乐/网易娱乐）暂时不出现，
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
    if region == "cn":
        # 只留现成有图的（无图的等后台补图那一轮）
        items = [it for it in items if it.get("image")]
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
            _fill_missing_images(_needmeta, "cn-noimg", max_fetch=80)
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
        # 2026-10-07：国际热榜同样是纯文字榜（中新社国际 / Hacker News），
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
        # 国际 RSS（France24 等）均带题图，与国内保持一致：先剔除无图条目，
        # 再合并（无图的）国际热榜（Hacker News），保证国际栏目的图片展示
        # 与国内栏目体验一致（用户要求国内/国际一致）。翻译在后台补，见 _warmup。
        before = len(items)
        items = [it for it in items if it.get("image")]
        print(f"[ok] {region}: 剔除无图 {before - len(items)} 条，剩 {len(items)} 条（低质图后台异步剔除）")
    PRELOAD_ALL[region] = hot + items
    return _select_fresh(hot + items, region)


def _enrich_quality_bg(region):
    """后台异步：下载题图测真实尺寸，剔除低质图（短边过小）。测不出则保留，不因网络误杀。"""
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
    PRELOAD[region] = pruned


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


def _quit_app_now():
    """退出程序（跟网页上「退出程序」按钮同一条路）。"""
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

    用户要求「滚动播放气泡新闻，三条左右」：一组最多 BALLOON_PLAY_COUNT 条，
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
    # 国际热榜里有 Hacker News 这种英文源，之前"优先中文"只是排序、挡不住它
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
    # ②France24/NPR/Sky 这些外媒（英文，翻译额度用完时就是英文原文）。
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
FOREIGN_MARKERS = (
    # 国家 / 地区
    "美国", "日本", "韩国", "朝鲜", "俄罗斯", "俄国", "乌克兰", "伊朗", "以色列",
    "巴勒斯坦", "加沙", "印度", "巴基斯坦", "英国", "法国", "德国", "意大利", "西班牙",
    "加拿大", "澳大利亚", "巴西", "阿根廷", "墨西哥", "土耳其", "埃及", "南非", "泰国",
    "柬埔寨", "缅甸", "越南", "菲律宾", "马来西亚", "新加坡", "印尼", "阿富汗", "叙利亚",
    "伊拉克", "黎巴嫩", "也门", "沙特", "卡塔尔", "阿联酋", "波兰", "荷兰", "瑞士",
    "瑞典", "挪威", "丹麦", "希腊", "葡萄牙", "奥地利", "匈牙利", "捷克", "塞尔维亚",
    "尼泊尔", "孟加拉", "斯里兰卡", "蒙古", "哈萨克", "乌兹别克", "格鲁吉亚", "亚美尼亚",
    "委内瑞拉", "古巴", "智利", "秘鲁", "哥伦比亚", "尼日利亚", "肯尼亚", "埃塞俄比亚",
    "苏丹", "利比亚", "索马里", "刚果", "卢旺达", "新西兰", "爱尔兰", "芬兰", "比利时",
    # 城市 / 地标
    "白宫", "五角大楼", "国会山", "克里姆林宫", "唐宁街", "爱丽舍宫", "青瓦台", "首相官邸",
    "洛杉矶", "旧金山", "纽约", "华盛顿", "芝加哥", "西雅图", "波士顿", "底特律",
    "伦敦", "巴黎", "柏林", "莫斯科", "东京", "大阪", "首尔", "平壤", "新德里",
    "迪拜", "悉尼", "多伦多", "温哥华", "罗马", "马德里", "阿姆斯特丹", "日内瓦",
    "冲绳", "关岛", "夏威夷", "阿拉斯加", "硅谷", "华尔街", "妙瓦底", "缅北",
    # 国际组织 / 军事
    "北约", "欧盟", "联合国", "世卫", "国际货币基金", "世界银行", "欧佩克",
    "驻日美军", "驻韩美军", "美军", "俄军", "乌军", "以军", "哈马斯", "真主党",
    "塔利班", "胡塞", "伊斯兰国", "基地组织",
    # 外国政要 / 名人
    "特朗普", "拜登", "哈里斯", "普京", "泽连斯基", "马克龙", "朔尔茨", "默克尔",
    "岸田", "石破", "尹锡悦", "李在明", "金正恩", "莫迪", "内塔尼亚胡", "哈梅内伊",
    "冯德莱恩", "苏纳克", "斯塔默", "特鲁多", "米莱", "马杜罗", "埃尔多安",
    "马斯克", "C罗", "梅西", "姆巴佩", "贝克汉姆", "泰勒·斯威夫特",
    # 常见国际赛事 / 事件
    "世界杯", "欧洲杯", "欧冠", "奥运会", "北约峰会", "G7", "G20", "APEC",
    "美国大选", "中期选举", "弹劾", "国情咨文",
    # 【补漏】实测漏网的两类：缩写式（美伊/俄乌/巴以）和人名简称
    "中东", "万斯", "美伊", "俄乌", "巴以", "以巴", "美方", "日方", "韩方", "俄方", "乌方",
    "德黑兰", "特拉维夫", "耶路撒冷", "海外", "境外", "半岛电视台", "路透社", "法新社",
    "美联社", "彭博社", "外媒", "白宫发言人", "五角大楼发言人",
    # 【2026-10-07 娱乐/体育向】用户强调「娱乐栏目也不例外」。
    # 娱乐标题常常**不带国家名**，光靠国家词抓不住：
    #   "孔蒂暗示愿执教曼联" / "罗马诺：前皇马后卫卡瓦哈尔加盟赫塔费" / "拉塞尔遭到罚退"
    "曼联", "皇马", "巴萨", "切尔西", "阿森纳", "利物浦", "曼城", "热刺", "拜仁",
    "尤文", "国米", "AC米兰", "巴黎圣日耳曼", "多特", "英超", "西甲", "意甲", "德甲",
    "法甲", "欧冠", "欧联", "NBA", "F1", "罗马诺", "孔蒂", "卡瓦哈尔", "赫塔费",
    "拉塞尔", "汉密尔顿", "维斯塔潘", "好莱坞", "格莱美", "奥斯卡", "艾美", "戛纳",
    "泰勒·斯威夫特", "比伯", "卡戴珊", "贝克汉姆", "姆巴佩", "内马尔", "本泽马",
    "迪拜", "韩娱", "日娱", "欧美", "韩团", "日漫", "美剧", "英剧", "韩剧",
    # 【2026-10-07 再补】用户反馈国内要闻里还有国际新闻，实测漏的是这几类：
    # 外国奖项（诺奖/奥斯卡）、外国球员（德约）、外国科技公司（Claude/GPT/英伟达）
    "诺贝尔", "诺奖", "奥斯卡", "格莱美", "金球奖", "艾美奖", "普利策",
    "德约", "纳达尔", "费德勒", "穆雷", "小威廉姆斯", "阿尔卡拉斯", "辛纳",
    "温网", "美网", "法网", "澳网", "大师赛", "大满贯", "世界杯", "欧洲杯",
    "GPT", "ChatGPT", "Claude", "Gemini", "OpenAI", "Copilot", "Llama",
    "英伟达", "特斯拉", "SpaceX", "谷歌", "OpenAI", "Anthropic", "Meta",
    "奈飞", "Netflix", "迪士尼",
    # 【2026-10-07 回退】这里曾把大厂简称（苹果/微软/谷歌/英伟达/三星/索尼…）也登记成
    # 国际特征词，想拦住「苹果一号电脑拍卖」这类。结果**过头了**：中国科技媒体几乎每条
    # 都会提到这些公司，国内科技被整个划走、栏目清空（实测 0 条）。
    # 教训：判断"是不是国际"要看**事情发生在哪 / 主体是谁**，不能看"提到了哪家公司"。
    # 所以这里只留外国机构/奖项，公司名一律不放（华为报苹果、小米报高通都是国内科技新闻）。
    "推特", "X平台", "脸书", "Instagram", "TikTok", "YouTube",
    "纳斯达克", "道琼斯", "标普", "华尔街", "美联储", "欧洲央行", "日本央行",
    "哈佛", "耶鲁", "斯坦福", "麻省理工", "牛津", "剑桥",
)


# 【2026-10-07 用户反馈「国际版要闻还是充次着国内新闻」】
# 光判断"像不像国际"不够 —— 「外交部：美方应慎重处理台湾问题」「法德要求欧盟…商务部回应」
# 这类标题里有外国名，但其实是**中国官方表态/国内新闻**，被误判成国际后塞进了国际要闻。
# 所以再加一层「强国内特征」排除：命中这些词就是国内新闻，不给国际板块。
DOMESTIC_MARKERS = (
    "我国", "中方", "中国内地", "外交部", "商务部", "国防部", "国台办", "国务院",
    "发改委", "教育部", "公安部", "文旅部", "财政部", "工信部", "住建部", "农业农村部",
    "全国人大", "全国政协", "中央", "省委", "市委", "县委", "区政府", "两岸", "台海",
    "解放军", "东部战区", "南部战区", "火箭军", "驻华", "中国队", "国足", "中超", "CBA",
    "春晚", "央视", "人民日报", "新华社", "光明日报", "中国影片", "华语片", "国产片",
    "申报奥斯卡", "国内", "全省", "全市", "我县", "村民", "社区",
    # 【2026-10-07 补】标题里出现「中国」的，对国内热榜来说就是国内新闻
    #（「中国拟推候选人角逐世卫总干事」这类不该跑到国际去）
    "中国", "中方", "我国", "国产",
)


# 【2026-10-07 用户反馈「国际版娱乐还是有国内明星的新闻」】
# 娱乐栏目不能用通用规则：中国明星出国（「蔡少芬和张晋现身韩国」）会因为"韩国"
# 两个字被误判成国际娱乐。娱乐栏目要求标题里出现**外国的具体对象**
# （外国艺人 / 球队 / 奖项 / 作品），光有外国地名不算。
ENT_FOREIGN_MARKERS = (
    "好莱坞", "格莱美", "奥斯卡", "艾美", "戛纳", "柏林电影节", "威尼斯", "金球奖",
    "公告牌", "Billboard", "Netflix", "奈飞", "迪士尼", "漫威", "DC",
    "C罗", "梅西", "姆巴佩", "内马尔", "本泽马", "贝克汉姆", "泰勒·斯威夫特",
    "比伯", "卡戴珊", "德约", "纳达尔", "费德勒", "穆雷",
    "曼联", "皇马", "巴萨", "切尔西", "阿森纳", "利物浦", "曼城", "热刺", "拜仁",
    "尤文", "国米", "英超", "西甲", "意甲", "德甲", "法甲", "欧冠", "NBA", "F1",
    "韩娱", "日娱", "韩团", "日漫", "美剧", "英剧", "韩剧", "欧美", "宝莱坞",
)


def _looks_ent_foreign(title):
    """娱乐栏目专用的「是不是国际」判定：必须有外国具体对象，光有外国地名不算。"""
    t = title or ""
    return any(k in t for k in ENT_FOREIGN_MARKERS)

# 【2026-10-07】国际大奖 / 国际赛事类关键词 —— 这类**优先判国际**，
# 即使标题里同时出现「中国科学家」「中国影片」也不改判。
# 起因：把「中国」加进国内特征词后，「诺贝尔物理学奖将揭晓，中国科学家薛其坤受关注」
# 被锁在国内要闻/热榜里，用户反馈"国内还是有国际新闻"。
# 【2026-10-07 用户反馈「气泡里快科技播的是国际新闻」】
# 科技栏目必须按**品牌归属**判，不能只看国家名：
#   「DLSS 5 体验：英伟达怎么让它以假乱真」没有国家名，但讲的是美国公司 → 国际
#   「华为徐直军：昇腾950超节点」讲的是中国公司 → 国内
# 先看是不是中国品牌（是就留国内），再看是不是外国品牌（是就走国际），
# 两者都没有才退回原来的关键词判定。
CN_TECH_BRANDS = (
    "华为", "小米", "OPPO", "oppo", "vivo", "荣耀", "一加", "真我", "realme", "红米",
    "比亚迪", "蔚来", "小鹏", "理想", "吉利", "长安", "奇瑞", "五菱", "极氪", "问界",
    "中兴", "联想", "大疆", "紫光", "京东方", "中芯国际", "长江存储", "寒武纪", "地平线",
    "摩尔线程", "龙芯", "飞腾", "统信", "麒麟", "鸿蒙", "澎湃", "玄戒",
    "阿里", "阿里巴巴", "腾讯", "百度", "字节", "抖音", "京东", "美团", "拼多多",
    "宁德时代", "海康", "科大讯飞", "商汤", "旷视", "月之暗面", "智谱", "DeepSeek",
    "宇树", "大模型", "国产", "我国", "中国", "国内",
)

FOREIGN_TECH_BRANDS = (
    "谷歌", "苹果", "微软", "英伟达", "OpenAI", "ChatGPT", "Claude", "Gemini", "Copilot",
    "三星", "索尼", "任天堂", "特斯拉", "马斯克", "高通", "英特尔", "AMD", "Meta",
    "亚马逊", "台积电", "ASML", "诺基亚", "爱立信", "波音", "空客", "NASA", "SpaceX",
    "Netflix", "奈飞", "迪士尼", "丰田", "大众", "宝马", "奔驰", "保时捷", "现代",
    "DLSS", "GeForce", "Radeon", "Ryzen", "骁龙", "Exynos", "iOS", "macOS", "Windows",
)


def _tech_scope(title):
    """科技栏目的归属判定：返回 'cn' / 'intl' / None（判不出来）。"""
    t = title or ""
    if any(k in t for k in CN_TECH_BRANDS):
        return "cn"
    if any(k in t for k in FOREIGN_TECH_BRANDS):
        return "intl"
    return None


STRONG_INTL_MARKERS = (
    "诺贝尔", "诺奖", "奥斯卡", "格莱美", "艾美奖", "金球奖", "戛纳", "柏林电影节",
    "威尼斯电影节", "普利策", "图灵奖", "菲尔兹奖",
    "奥运会", "冬奥会", "世界杯", "欧洲杯", "亚洲杯", "世乒赛", "世锦赛",
    "欧冠", "英超", "西甲", "意甲", "德甲", "法甲", "NBA", "F1",
)


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
    FOREIGN_SECTIONS = ("国际", "军事", "全球", "海外", "国际新闻", "环球", "国际时局")
    DOMESTIC_SECTIONS = ("社会", "台湾", "大陆", "国内", "时政", "地方", "法治", "教育",
                         "健康", "体育", "娱乐", "科技", "财经", "文化", "评论", "要闻",
                         "新时代", "港澳", "舆论场", "直击现场", "一号专案", "运动家",
                         "科学湃", "澎湃号", "公益", "智库", "中国政库", "浦江头条")
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
    中新社国际 + Hacker News 两家）。
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
    FOREIGN_SECTIONS = ("国际", "军事", "全球", "海外", "国际新闻", "环球", "国际时局")
    DOMESTIC_SECTIONS = ("社会", "台湾", "大陆", "国内", "时政", "地方", "法治", "教育",
                         "健康", "体育", "娱乐", "科技", "财经", "文化", "评论", "要闻",
                         "新时代", "港澳", "舆论场", "直击现场", "一号专案", "运动家",
                         "科学湃", "澎湃号", "公益", "智库", "中国政库", "浦江头条")
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


def _filter_hot_domestic(items):
    """国内热榜专用：只留国内条目（国际的那些由 _split_hot_by_scope 转给国际热榜）。"""
    kept, dropped = _split_hot_by_scope(items)
    if dropped:
        print(f"[ok] hot: 国内热榜剔出 {len(dropped)} 条国际新闻（转给国际热榜）")
    return kept


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
MAJOR_KEYWORDS = (
    "地震", "海啸", "爆炸", "袭击", "战争", "开战", "坠机", "空难", "沉船",
    "遇难", "身亡", "死亡", "失联", "坍塌", "火灾", "山洪", "台风", "红色预警",
    "暴跌", "熔断", "停牌", "崩盘", "破产", "降息", "加息", "制裁", "断交",
    "重大事故", "紧急", "突发", "疫情", "核泄漏",
)
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



@app.on_event("startup")
def _warmup():
    """后台预热两个 Region + 静默检查更新"""
    _load_trans_cache()
    try:
        updater.cleanup_leftovers()   # 清理上次升级留下的 .old/.new
    except Exception:
        pass          # 载入历史译文缓存，避免重复消耗免费翻译额度
    TRAY.start()                 # 右下角托盘图标（非 Windows 静默降级）
    BALLOON.start()              # 右下角自绘气泡（系统通知被关也照样弹）
    def run():
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
                items = _collect("intl")
                PRELOAD["intl"] = items
                print(f"[ok] 预热 intl: {len(items)} 条")
            except Exception as ex:
                print(f"[warn] 预热 intl 失败: {type(ex).__name__}")
                return
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
                items = _collect(region)
                PRELOAD[region] = items
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
        while True:
            time.sleep(600)          # 10 分钟一轮
            try:
                PRELOAD["hot"] = _collect("hot")
            except Exception:
                pass
            try:
                cn_items = _collect("cn")
                PRELOAD["cn"] = cn_items
                # 2026-10-07 修复：以前刷新时漏掉了国内的「低质图异步剔除」，
                # 导致跑久了低质图不会被清，跟刚启动时的表现不一致。
                if cn_items:
                    threading.Thread(target=_enrich_quality_bg, args=("cn",), daemon=True).start()
            except Exception:
                pass
            try:
                items = _collect("intl")
                PRELOAD["intl"] = items
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
        # 延迟几秒再查，避免和预热抢网络/CPU；失败静默不影响使用
        time.sleep(5)
        try:
            cfg = CONFIG.get("update", {})
            if cfg.get("enabled", True) is False:
                print("[ok] 自动更新已关闭（config.update.enabled=false）")
                return
            st = updater.check()
            if st["status"] == "update":
                print(f"[update] 发现新版本 v{st['remote']}，开始后台下载")
                # 用户要求「更新提醒」气泡：发现新版本必须让你看见，不能只写日志
                _show_toast(f"发现新版本 v{st['remote']}", "正在后台下载，退出程序时自动替换重启")
                ok, msg = updater.download_and_prepare(st["latest"])
                print(f"[update] {msg}")
                if ok:
                    _show_toast(f"新版本 v{st['remote']} 已就绪", "退出程序时自动替换并重启")
            elif st["status"] == "latest":
                print(f"[ok] 已是最新 v{st['remote']}")
            else:
                print(f"[warn] 更新检查: {st['message']}")
        except Exception as ex:
            print(f"[warn] 更新检查异常: {type(ex).__name__}")

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


def _kick_collect(region):
    """后台抓一轮并写进 PRELOAD。已在抓就不重复起。返回是否新起了线程。"""
    with _COLLECT_LOCK:
        if _COLLECTING.get(region):
            return False
        _COLLECTING[region] = True

    def _run():
        try:
            PRELOAD[region] = _collect(region)
        except Exception as ex:
            print(f"[warn] 后台抓取 {region} 失败: {type(ex).__name__}")
        finally:
            with _COLLECT_LOCK:
                _COLLECTING[region] = False

    threading.Thread(target=_run, daemon=True).start()
    return True


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
    # 显式声明 charset=utf-8，避免个别客户端把 JSON 误判为 GBK 导致乱码
    return JSONResponse({"version": VERSION, "count": len(items), "items": items[:limit]},
                        media_type="application/json; charset=utf-8")


@app.get("/api/version")
def api_version():
    return JSONResponse({"version": VERSION, "updatedAt": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")},
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
def api_update_install():
    """已下载就绪则触发替换脚本，随后退出服务让脚本完成覆盖+重启"""
    print("[ok] 收到安装请求，开始替换")
    if not updater.trigger_replace():
        print("[warn] 替换未执行（没有待安装的更新或校验没过）")
        return {"ok": False, "message": "没有待安装的更新"}
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
async def api_autostart_set(payload: dict = None):
    """开关开机自启（写 HKCU 的 Run 项，不需要管理员权限）。

    【2026-10-07 用户要求「第一次使用时界面给个开关让用户选」】
    开机自启时用 --silent 启动：只后台把服务跑起来、把第一屏数据抓好，不弹浏览器。
    这样用户想看的时候点托盘右键「打开热点新闻」就是秒开（数据早准备好了）。
    """
    want = bool((payload or {}).get("enabled"))
    ok, msg = _autostart_set(want)
    return {"enabled": _autostart_enabled(), "ok": ok, "message": msg}

@app.post("/api/quit")
def api_quit():
    """
    退出服务。打包成无窗口 exe 后没有控制台可按 Ctrl+C，只能由页面按钮触发。
    延迟 0.6s 再退，先让这次 HTTP 响应回到浏览器。
    """
    def _die():
        time.sleep(0.6)
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
    return {"ok": True, "message": "服务正在退出"}


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


@app.get("/api/translate")
def api_translate(text: str = Query(""), target: str = "zh-CN"):
    cfg = CONFIG.get("translate", {})
    if not cfg.get("enabled", True) or not text:
        return {"ok": False, "reason": "disabled"}
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
    res = _fetch(url)
    # _fetch 有两种返回：图片扩展名 → (bytes, content_type)；其余（如无扩展名的图床链接）→ bytes。
    # 以前一律按 tuple 解包，遇到后者 res[0] 是 int，触发 AttributeError 并让整个请求 500。
    if isinstance(res, tuple):
        blob, ctype = res[0], res[1]
    else:
        blob, ctype = res, "image/jpeg"
    if not blob or not isinstance(blob, (bytes, bytearray)):
        return Response(status_code=404)
    return Response(content=blob, media_type=ctype or "image/jpeg",
                    headers={"Cache-Control": f"public, max-age={IMG_CACHE_TTL}"})


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    v = _load_version()
    ssr = ""
    if os.path.exists(os.path.join(STATIC_DIR, "index.html")):
        with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as f:
            tpl = f.read()
        payload = {
            "version": VERSION,
            "region": "cn",
            # 把预热好的条目直接塞进首屏，打开页面立刻有内容，不用等 JS 再拉一次
            "items": PRELOAD.get("cn", [])[:60],
        }
        blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        ssr = tpl.replace("__SSR_DATA__", blob)
        ssr = ssr.replace('__SSR_REGION__', "cn")
        ssr = ssr.replace("__SSR_VERSION__", VERSION)
        return HTMLResponse(ssr, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    return HTMLResponse("<h1>hotnews</h1>")


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
    for name in ("intl.html",):
        p = os.path.join(STATIC_DIR, name)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return HTMLResponse(f.read(),
                                    headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    return HTMLResponse("<h1>world page missing</h1>")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
