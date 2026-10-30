# -*- coding: utf-8 -*-
# 作者: 海风（kele551）   https://gitcode.com/kele551/hotnews
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
import math              # 【2026-10-28 第五轮】播报版式的呼吸点/声波动画要算正弦
import json
import html
import base64
import hashlib
import hmac              # 【2026-10-09 修 高-4】本机令牌比对（标准库，不引入新依赖）
import ipaddress         # 【2026-10-09 修 高-1】判图片域名解析出来的是不是内网/回环地址
import socket            # 同上：只解析域名，不建连接
import sqlite3           # 【2026-10-25 新增】跨天归档（见下面「跨天归档与历史检索」一节）
                        #   标准库自带，不新增第三方依赖；打包时 PyInstaller 会自动收进去。
import threading
import urllib.parse
import webbrowser
import ctypes            # 【2026-10-28 修 NameError】气泡的 GDI+/DIB 绘制散在几个方法里，
                        # 每个方法都写 self.ctypes 太啰嗦；顶层导入一次（跨平台安全，Linux 也有）。
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
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,  # noqa: E402
                               PlainTextResponse)
from fastapi.staticfiles import StaticFiles  # noqa: E402

import updater  # noqa: E402
import static_heal  # noqa: E402  【2026-10-18 修】静态界面文件自愈（见 static_heal.py 文件头）


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

# 【2026-10-18 修 高-1】静态界面文件自愈 —— 用户报「打开没有显示」的真实原因。
# 事发：程序在跑，但 %TEMP%\_MEIxxxx\static 被清理临时目录的动作当成垃圾删掉了
# （现场只剩 25 个内存映射中的 dll/pyd），于是 `/` 只剩兜底文字、`/static/*` 全 500。
# 现在打包态一律改从 %LOCALAPPDATA%\hotnews\static-<版本>\ 读界面文件，细节见 static_heal.py。
STATIC_DIR = static_heal.ensure_static(
    STATIC_DIR, BASE_DIR, VERSION, bool(getattr(sys, "frozen", False)))

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
BOT_ID = "HotnewsAggregator/1.5 (+https://gitcode.com/kele551/hotnews)"
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
# 所以：头条 / 热榜等时效敏感栏目仍只留当天（走 MAX_AGE_HOURS=24h），
# 垂类（科技/软件/娱乐）各自放宽，保证栏目内至少 3 家不同网站。
CLS_MAX_AGE_HOURS = {
    "科技": 96,     # 4 天：IT之家/快科技日更，爱范儿/雷峰网 慢一些
    "软件": 120,    # 5 天：小众软件、开源中国、少数派这类本来就不是日更
    # 【2026-10-11 六轮·用户「娱乐新闻少的可怜」】娱乐 120h → 168h（7 天）。
    # 娱乐是所有栏目里**更新最慢**的：国际娱乐唯一的直供源 CGTN 文化平均三四天才换一批
    # （实测 50 条 RSS 里只有 17 条落在 5 天内），5 天窗口直接把栏目饿到只剩 4 条。
    # 娱乐不是快讯，用户对"三天前的电影节报道"没有违和感，放宽是安全的；
    # 而"当天优先"的规则没动 —— 当天够 6 条/3 家时，旧条目根本不会补进来
    # （见 FRESH_MIN_PER_CLS / _select_fresh）。
    "娱乐": 168,    # 7 天
    "国际娱乐": 168,
    "热榜": 24,     # 用户反馈「热榜上居然还有两天前的新闻」→ 热榜不再豁免，同样按当天
}
# 「当天优先」的两个下限：某栏目当天条目低于这个数、或少于这么多家网站，
# 才允许补进较旧的条目（详见 _select_fresh）
FRESH_MIN_PER_CLS = 6
FRESH_MIN_SITES = 3


def _max_age_hours(src):
    """时效窗口只此一处口径：源自己声明 > 栏目级豁免表 > 分区默认。

    【2026-10-26 修】三个 HTML 解析器（mefcl / 凤凰娱乐 / 凤凰资讯）各自写成
    `MAX_AGE_HOURS.get("cn", 72)`，而 "cn" 这个键**一直都在**（值 24）—— 那个 72
    从没生效过。后果是「软件」「娱乐」这类按 CLS_MAX_AGE_HOURS 放宽到 120/168 小时的
    垂类源，在解析阶段就按 24 小时砍光：2026-10-26 实测 mefcl 首页 20 条**全部**因
    「昨天 00:00 早于 now-24h」被丢，栏目里整个消失，右栏标成 warn。
    （站方只给日期不给时刻，解析器按当天 00:00 计，见 _parse_mefcl 里 2026-10-07 那段注释。）
    解析阶段用这个函数，与抓取主路径（见 RSS 分支的 max_age）同一口径；
    "当天优先" 的下限仍在 _select_fresh 那里把关，不会因为窗口放宽就把旧稿顶到前面。
    """
    return (src.get("max_age_hours")
            or CLS_MAX_AGE_HOURS.get(src.get("cls", ""))
            or MAX_AGE_HOURS.get(src.get("region", "cn"), 24))

# ---------------- 数据源 ----------------
# 2026-10-06 全量实测后重构：人民网 / 新浪 / 网易 / 央视 / 环球网 / 澎湃 的 RSS
# 均已停更（人民网停在 2025-06-05，新浪停在 2025-09-23），仍返 200 但内容陈旧，
# 是最初「新闻太旧」的根因。现改用仍在实时更新的源。
#
# 【2026-10-09 换源·合规】按工作区章程 §9.1「默认只用境内权威媒体源，不采用境外来源」：
#   · 国际板块**删掉全部境外站点**，换成**境内媒体的国际版**
#     （CGTN + 中新网·国际，再由凤凰/澎湃/红星/界面报的国际新闻补足）；
#   · 国际热榜同步删掉那处境外热榜 API；
#   · 国内新增财经 / 娱乐源，全部是境内站点。
#     （有两个栏目后来按用户口径**整栏下线**：国内体育整栏不要（国际频道不受影响）、
#      民生类那一栏整栏去掉 —— 详见下面 SOURCES["cn"] 里的两段说明。）
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
        # 界面新闻本身保留，改归「头条」，避免把你要求加的 jiemian 也一并砍掉。
        # 【2026-10-09 栏目定稿】该栏目按用户口径定名为「头条」：
        # 界面 / 澎湃这两家是头条的主力源；标题命中财经特征词的条目会被
        # _fix_finance_cls 改判到「财经」（用户点名「股市/ETF/A股…必须落财经」）。
        {"id": "cn-finance", "label": "界面",   "url": "https://a.jiemian.com/index.php?m=article&a=rss", "base": "https://www.jiemian.com", "region": "cn", "cls": "头条"},
        # 澎湃新闻：官方 JSON 接口（parser=thepaper），自带标题/题图/直达链接/真实互动数。
        # 2026-10-06 用户点名要加；同批实测腾讯/网易/新浪的 RSS 全部 404、首页又是 JS 动态渲染
        # （新浪娱乐首页 45 条链接全是「站点地图/好莱坞/排行」这类导航），只有澎湃给出了干净的结构化数据。
        {"id": "cn-thepaper", "label": "澎湃新闻", "url": "https://cache.thepaper.cn/contentapi/wwwIndex/rightSidebar", "base": "https://www.thepaper.cn", "region": "cn", "cls": "头条", "parser": "thepaper"},
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
        # 混着汽车/养生/育儿等泛频道内容；加娱乐关键词过滤后 40 条只剩 3 条且有 2 条不是娱乐。
        # 用户 2026-10-07 决定不做这个源，别再挖了。
        {"id": "cn-sina-ent", "label": "新浪娱乐", "url": "https://ent.sina.com.cn/", "base": "https://ent.sina.com.cn", "region": "cn", "cls": "娱乐", "parser": "sina_ent"},
        # ============ 【2026-10-09 换源】新增：财经 / 娱乐 ============
        # 目标：① 每个栏目至少 3 家网站；② 只用境内站点（合规口径见 docs/COMPLIANCE.md 第 7 节）。
        # 下面每一条都做过真实联网实测（HTTP 状态 / 耗时 / 条数 / 带图数 / 最新时间），
        # 逐项数据记在 logs\换源-境内国际版-20261009.md；没实测通过的一律不写在这里。
        #
        # 华尔街见闻：实测 200 / 0.37s / 61 条 / 35 条带图 / 最新 1.7 小时前（财经垂类里更新最勤）。
        {"id": "cn-wscn", "label": "华尔街见闻", "url": "https://dedicated.wallstreetcn.com/rss.xml", "base": "https://wallstreetcn.com", "region": "cn", "cls": "财经"},
        # 中新网系（财经）：实测 200 / 约 20ms / 30 条 / 当天分钟级更新，
        # 但 **RSS 本身不带图** → 打 body_image 标记，走「正文页取首图」的图片策略
        # （限时 3 秒/条、命中 _ART_CACHE 缓存、取不到就跳过该条，详见 _fill_missing_images）。
        # 实测正文首图命中率 40%~60%，取到的多是 700x466 / 1080x783 这类达标大图。
        {"id": "cn-cnfin",   "label": "中新网·财经", "url": "https://www.chinanews.com.cn/rss/finance.xml", "base": "https://www.chinanews.com.cn", "region": "cn", "cls": "财经", "body_image": True},
        # 【2026-10-09 用户口径：国内不要体育，国际频道保留】原 `cn-cnsport`（中新网那一路体育 RSS）已整条移除 ——
        # 用户原话「国际板块，留体育，国内的体育不值得看」。它的 30 条**不带图**、要靠正文页补图
        # （实测命中率低），本来也过不了「自带图」这一关，所以直接删掉、**没有**改挂到财经或别的栏目。
        # 国内这一栏随之消失（栏目跟着数据走）；国际频道的体育由下面的 intl-xinhua-sport /
        # intl-cgtn-sport 两家境内源供给，实测数据见 logs\国际栏增补带图源-20261009.md。
        # 【2026-10-09 栏目定稿：民生类那一栏整栏下线】原 `cn-cnsoc`（中新网那一路民生 RSS）
        # 已删除，国内不再有这一栏（用户口径：整栏去掉）。它原来那 4 条（实测）是
        # 地方事故/天气类，**按内容归入「头条」，没有一条被塞进财经** —— 用户点名「绝不塞进财经」。
        # 分类规则里也一并删掉这个栏目名：`data/keywords.txt` 的 domestic_sections 里那一条
        # 站方栏目名提示已移除（它只影响国内/国际判定，不影响任何栏目命名）。
        # 国际在线·娱乐（ent.cri.cn，中国国际广播电台 / 总台旗下）：影视综艺向。
        # 实测 200 / 0.023s / 卡片 40 张 / 24 小时内 19 条 / 当天分钟级更新。
        # ⚠ 卡片图是 **512x288 缩略图**（短边 288 差 12 像素过不了 300 的高清门槛，
        #   直接用会被后台低质图环节整条剔除、等于白加）——程序按站方 URL 规则改写成原图
        #   （实测 991x558 ~ 4052x2278，全部达标），见 _cri_image。
        # 它是娱乐栏目里第 3 家网站，也是「列表页自带标题+图+时间」的第二个源
        #（第一个是凤凰娱乐；新浪娱乐的列表页无图无时间，全靠逐条抓文章页）。
        {"id": "cn-cri-ent", "label": "国际在线娱乐", "url": "http://ent.cri.cn/", "base": "http://ent.cri.cn", "region": "cn", "cls": "娱乐", "parser": "cri_ent"},
        # ============ 【2026-10-09 补源·娱乐】实测结论：**没有可新增的境内娱乐频道** ============
        # 用户再次点名「娱乐栏目新闻太少」，逐家实测了 20 多个境内娱乐入口，结论如下（详细表格见
        # logs\国际栏增补带图源-20261009.md）：
        #   · 唯一「自带图 + 当天更新 + 图≥300」达标的，是国际在线（ent.cri.cn）自己的垂直子频道
        #     （/movie 电影 20 卡·24h 内 5 条、/tv 电视剧 20 卡·8 条、/focus 焦点 20 卡·11 条）。
        #   · 但**实测它们相对 ent.cri.cn 首页新增 0 条**（四个源并集 = 首页 22 条，标题去重后完全重合）——
        #     同一个编辑部的同一批稿子。按用户「不要凑数」「每个栏目必须有三家以上网站」的要求，
        #     **没有接入**这三个子频道：看着多三个源，实际还是同一家、同一条新闻。
        #   · 其它家全部不达标（图片过小/停更/403/JS 空壳）：新华网娱乐（图 184~225）、人民网娱乐
        #     （9 张图卡里只有 2 张是当天的）、光明网娱乐（当周只有 1 条）、北青网娱乐（图固定 292x390）、
        #     中国日报娱乐（停在 2022）、央视网娱乐（JS 空壳）、1905（403）、环球网娱乐（JS 空壳）、
        #     中新网娱乐（403/404）、网易娱乐（用户 2026-10-07 已否决）、央广网娱乐（内容停在 2023-01）、
        #     芒果TV/湖南娱乐/腾讯娱乐（JS 空壳）、搜狐娱乐（停在 2017）、南都/上游/大众网/潇湘晨报/扬子晚报
        #     （当天无图或响应 15 秒以上）、观察者网/界面（无娱乐频道或卡片无图）。
        # 所以国内「娱乐」这一轮**维持原样**（凤凰娱乐 + 新浪娱乐 + 国际在线娱乐）；
        # 国际板块的「娱乐」栏目改由 CGTN 文化分区直供（见 SOURCES["intl"]），实测 2 条 → 8 条。
    ],
    "intl": [
        # ============ 【2026-10-09 换源·合规】国际新闻只用境内媒体的国际版 ============
        # 原来这一栏 12 个源**全是境外站点**（境外站点 /
        # 境外站点），
        # 与章程 §9.1「默认只用境内权威媒体源，不采用境外来源」冲突，已整段删除。
        # 现在这一栏的构成（全部境内）：
        #   ① CGTN（中国国际电视台）世界/商业/文化/体育/科技分区 —— 实测各 50 条、全部带图、当天更新；
        #   ② 中新网·国际 / 国际在线·国际 / 人民网·国际 / 光明网·国际 —— 中文国际新闻，
        #      除中新网外**列表页自带图**（2026-10-09 补源，见下面那一段注释）；
        #   ③ 新华网·体育 —— 中文国际体育（用户 2026-10-09 要求国际板块保留体育栏目）；
        #   ④ 凤凰网 / 澎湃新闻 / 红星新闻 / 界面新闻 报的国际新闻 —— 由
        #      _split_cn_scope（国内头条）与 _split_hot_by_scope（国内热榜）转过来，
        #      channel 标为 cn-intl-news / cn-intl-hot，本来就是中文，不依赖翻译。
        # 这样国际板块「要闻 / 科技 / 财经 / 娱乐 / 体育 / 热榜」六栏都有直供源，
        # 不再只靠国内分流；数据全部来自境内媒体。
        {"id": "intl-cgtn",    "label": "CGTN",        "url": "https://www.cgtn.com/subscribe/rss/section/world.xml", "base": "https://www.cgtn.com",         "region": "intl", "cls": "要闻"},
        {"id": "intl-cnworld", "label": "中新网·国际", "url": "https://www.chinanews.com.cn/rss/world.xml",           "base": "https://www.chinanews.com.cn", "region": "intl", "cls": "要闻", "body_image": True},
        # ============ 【2026-10-09 补源】用户：「国际栏目新闻太少！！给国际栏多接几个自带图的源」 ============
        # 硬口径（本次逐家实测）：① 只用境内媒体；② **列表页/RSS 自己就带图**（不靠正文页补图）；
        # ③ 当天/近日更新；④ 图片短边 ≥300（过得了后台低质图门槛）。四条全过的才写在这里，
        # 逐条实测数据（HTTP/耗时/条数/带图数/最新时间/图片来源字段）与「试过没用上」清单见
        # logs\国际栏增补带图源-20261009.md —— 没实测通过的一律不写。
        #
        # —— ① 要闻：三家中文国际频道，列表页自带图 ——
        # 国际在线·国际（news.cri.cn/world，总台旗下）：实测 200 / 0.03s / 卡片 20 张 / 全部带图 /
        #   当天分钟级更新（抽 8 张原图 1920x1080、5472x3079、1440x810…全部达标）。
        #   ⚠ 卡片图是 256x144 缩略图（过不了 300 门槛），程序按站方 URL 规则改写成原图，见 _cri_image；
        #   时间在列表页里就是精确到分的（2026年10月09日 14:39），不用猜。
        {"id": "intl-cri-world", "label": "国际在线·国际", "url": "http://news.cri.cn/world/", "base": "http://news.cri.cn", "region": "intl", "cls": "要闻", "parser": "cri_world"},
        # 人民网·国际（world.people.com.cn）：实测 200 / 0.09s / 带图卡 6 张 / 660x370~996x558 全部达标。
        #   列表页只给到**日期**（URL 里的 /2026/1009/），所以发布时间按当天 00:00 记 ——
        #   是站方 URL 上写死的日期，不是猜的时点；排序上会排在有具体时分的条目之后。
        {"id": "intl-people-world", "label": "人民网·国际", "url": "http://world.people.com.cn/", "base": "http://world.people.com.cn/", "region": "intl", "cls": "要闻", "parser": "people_list"},
        # 光明网·国际（world.gmw.cn，光明日报社）：实测 200 / 0.17s / 带图卡 4 张 / 图 580x320 达标；
        #   同样只给到日期（URL 里的 2026-10/09/），按当天 00:00 记。
        {"id": "intl-gmw-world", "label": "光明网·国际", "url": "https://world.gmw.cn/", "base": "https://world.gmw.cn/", "region": "intl", "cls": "要闻", "parser": "gmw_list"},
        #
        # —— ② 垂类四栏：国际板块按用户口径保留「要闻/科技/财经/娱乐/体育/热榜」——
        # 这四栏原来只能等国内板块按关键词分流过来的条目（_split_cn_scope），冷启动时是空的；
        # 现在给每一栏都接上**自带图**的直供源，栏目不再依赖分流是否命中。
        # CGTN（中国国际电视台 / 总台）分区 RSS：实测每个分区 50 条、**50 条全部带 media:content 图**、
        # 图片来源字段就是 media_content（不需要正文页），时间精确到分、都是当天/前一两天的稿子。
        # 4 个分区实测（条目/带图/最新/24h 内/48h 内）：
        #   world（原有）50/50/当天 10:42；business 50/50/10-08 21:25（6/7）；
        #   culture 50/50/10-08 15:44（1/5）；sports 50/50/当天 14:30（3/3）；tech-sci 50/50/10-08 16:58（1/7）。
        # CGTN 原文是英文，与原有 intl-cgtn 一样交给后台翻译线程（translated=False）。
        {"id": "intl-cgtn-biz",     "label": "CGTN商业", "url": "https://www.cgtn.com/subscribe/rss/section/business.xml", "base": "https://www.cgtn.com", "region": "intl", "cls": "财经"},
        {"id": "intl-cgtn-culture", "label": "CGTN文化", "url": "https://www.cgtn.com/subscribe/rss/section/culture.xml",  "base": "https://www.cgtn.com", "region": "intl", "cls": "娱乐"},
        # —— 【2026-10-11 七轮·用户拍板】两家境外权威娱乐媒体（只补国际娱乐这一栏）——
        # 为什么敢接、为什么只接这两家、怎么一键关掉，见 INTL_ENT_OVERSEAS 上面那段长注释。
        # 逐家实测（2026-10-11）：
        #   Variety（好莱坞产业权威，variety.com/feed/）：200 / 10 条 / **10 条全部带
        #     media:content 图** / 当天 14:23~17:38 更新 / 图 1200x~（过 300 短边门槛）。
        #   Consequence（独立音乐媒体，consequence.net/feed/）：200 / 15 条 / **15 条全部带图** /
        #     当天 00:04~12:28 更新。
        # 两家都是英文，走后台翻译线程（translated=False），与 CGTN 英文源同一条链路。
        # ⚠ 这两家的条目在 `_is_domestic_for_intl` 里走**境外娱乐专用前置层**
        #   （不做"证明自己是国际"那套判据 —— 那是给境内源防中国题材用的），
        #   但仍过主权红线 `_INTL_ENT_BLOCK_RX`。
        {"id": "intl-variety",     "label": "Variety",     "url": "https://variety.com/feed/",     "base": "https://variety.com",     "region": "intl", "cls": "娱乐"},
        {"id": "intl-consequence", "label": "Consequence", "url": "https://consequence.net/feed/", "base": "https://consequence.net", "region": "intl", "cls": "娱乐"},
        {"id": "intl-cgtn-sport",   "label": "CGTN体育", "url": "https://www.cgtn.com/subscribe/rss/section/sports.xml",   "base": "https://www.cgtn.com", "region": "intl", "cls": "体育"},
        {"id": "intl-cgtn-tech",    "label": "CGTN科技", "url": "https://www.cgtn.com/subscribe/rss/section/tech-sci.xml", "base": "https://www.cgtn.com", "region": "intl", "cls": "科技"},
        # 新华网·体育（www.news.cn/sports，新华社）：用户 2026-10-09 明确「国际板块留体育」，
        # 这是实测里唯一「中文 + 列表页自带图 + 自带精确时间 + 当天更新」的国际体育源：
        #   实测 200 / 0.06~0.1s / coxe 卡 21 张 / 24h 内 7 条、48h 内 20 条 / 图 981x552~1010x568 达标 /
        #   时间形如 2026-10-09 09:46:58（列表页里直接给到秒）。
        #   内容以国际赛事为主（奥运申办、中网、世界羽联…），与用户「国内体育不值得看」的口径一致。
        {"id": "intl-xinhua-sport", "label": "新华网·体育", "url": "http://www.news.cn/sports/", "base": "http://www.news.cn/sports/", "region": "intl", "cls": "体育", "parser": "xinhua_coxe"},
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

# 【2026-10-25 新增】来源 → 色相表（界面把每家媒体的名字染成固定颜色，见 static/index.html 的 srcHue）。
# 为什么在服务端按"配置里的第几家"发牌，而不是前端直接按名字散列：
#   拿当天真机池子试过 —— 纯散列（连乘黄金角都试了）18 个名字两两最小只差 0~3°，
#   IT之家 168°、快科技 173° 几乎同色，而这两家恰好是条数最多的（各 41 条），
#   最该分开的两家反而分不开。按固定顺序发牌、色相走黄金角递推，
#   33 家在 360° 上近似均匀铺开（最小间隔约 10°），而且颜色只跟"第几家"有关，
#   刷新、换排序、重开程序都不变色。
# 表外的名字（以后新上的热榜媒体）退回前端按名字散列，不会没颜色，只是可能和别家挨得近。
HOT_SOURCE_LABELS = ("凤凰网", "红星新闻")   # 热榜条目转发的媒体名，不在 SOURCES 里，实测 2026-10-25 见过

# 用户点名的色相，优先于上面那套自动发牌（2026-10-25）。
#   红星新闻 → 0°（正红）：自动发到 303°（品红），用户看了真机截图说"太难看了，还是红色吧"。
#     正好和 10-24 那条「热榜来源站点用红字」同一个红，热榜那行的徽章也不用换颜色。
#   界面 → 25°（橙红）：原本占着 0° 的是界面（配置里第一个源）。红星要来正红，界面就得让位，
#     不然头条栏里两家顶着一模一样的红。挪 25° 后和最近的 中新网·财经(40°) 差 15°，不撞。
#   GitHub → 215°（蓝）：2026-10-28 新增的「开源热榜」来源。它不在 SOURCES 里
#     （单独一条链路，见下面「开源热榜」一节），靠这段手工点名拿到固定色，
#     和 215° 附近没有别的家；卡片上的站名徽章、左栏计数跟新闻栏用的是同一套色。
SRC_HUE_PICKS = {"红星新闻": 0, "界面": 25, "GitHub": 215}


def _build_src_hues():
    out, i = {}, 0
    for _rg in ("cn", "intl", "hot"):
        for s in SOURCES.get(_rg, []):
            lb = (s.get("label") or "").strip()
            if not lb or lb in out:
                continue
            hue = SRC_HUE_PICKS.get(lb, int(round((i * 137.508) % 360)))   # 黄金角：相邻两家不会挨成同一种颜色
            i += 1
            out[lb] = hue
            # 条目上有时写的是源 id（归档里的老数据、热榜的通道名），指到同一个颜色
            sid = (s.get("id") or "").strip()
            if sid:
                out.setdefault(sid, hue)
    for lb in HOT_SOURCE_LABELS:
        if lb and lb not in out:
            out[lb] = SRC_HUE_PICKS.get(lb, int(round((i * 137.508) % 360)))
            i += 1
    # 手工点了名、但源表里还没有的（以后新加的源先定色再上线），也补进去
    for lb, hv in SRC_HUE_PICKS.items():
        out.setdefault(lb, hv)
    return out


SRC_HUES = _build_src_hues()


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
# 2026-10-09 补回（真机事故）：mefcl（用户投稿站）的站点校验 cookie 缓存 + "上次成功结果"兜底。
# 这两个模块级变量在"删除死代码"那一批里被误删，而 _parse_mefcl 仍在用它们 ——
# 于是它一抛 NameError，就把**整个「国内」栏**的抓取带崩（ex.map 会把异常抛到外层），
# 用户看到的就是"国内新闻半天抓不出来"。
_MEFCL_COOKIE = {"value": "", "ts": 0}
_MEFCL_LAST = []

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
        # 【2026-10-28】GitHub 的仓库分享卡没有扩展名（…/1/<owner>/<repo>），按老启发式
        # 会被当文档：进错缓存（5 分钟的文档表，不是图片表）、Content-Type 兜成 image/jpeg。
        # 只点名这一家、且只在「开源热榜」这一条链路上出现，别的源一律按原规则判。
        or _low.startswith("https://opengraph.githubassets.com/")
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
                        # 跟随重定向后落到内网地址 / 非登记来源 → 当抓取失败，绝不把内容带回去
                        # 【2026-10-10 修】原来这里**只查内网、不查白名单**，见
                        # _img_redirect_blocked 的说明（开放重定向能变成任意公网代理）。
                        if key == "img" and _img_redirect_blocked(url, r.url):
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
                    # 【2026-10-10 修】重定向落地后要**重新过白名单**，不只是内网拦截 ——
                    # 见上面 _img_redirect_blocked 的说明。
                    if r.status_code != 200 or _img_redirect_blocked(url, r.url):
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


def _img_redirect_blocked(orig, final):
    """跟随重定向之后，**最终落地的地址**是不是已经不合法了。

    【2026-10-10 修】/img 原来只在**请求前**校验一次来源白名单，而 httpx 是
    `follow_redirects=True`：只要白名单里任一站存在一个开放重定向（站内跳转、
    `?url=` 跳转、登录跳转都算），就能让 `/img?url=<白名单域名上的跳转地址>`
    变成访问**任意公网地址**的代理 —— 与 docs/SECURITY.md
    「只保证不去请求未登记域名」的说法对不上。
    （内网/裸 IP/非标准端口那一层原来就查了，缺的是**白名单**这一层。）

    返回 True = 必须当抓取失败处理，绝不把内容带回去。
    """
    f = "" if final is None else str(final)
    if not f or f == orig:
        return False
    if _img_url_block_reason(f):
        return True
    try:
        h = urllib.parse.urlsplit(f).hostname
    except Exception:
        return True
    return not _img_host_allowed(h)


# 畸形协议前缀：https:https://x  或  https://https://x（RSS 里真实存在）
_URL_FIX = re.compile(r"^https?:/*(?=https?://)", re.I)
# 【2026-10-15 修】「两个协议黏在一起」的畸形地址（真机日志实测 8 条图片因此被拒）：
#     https://img-kp.mydrivers.comhttps://img1.mydrivers.com/img/20261014/xxx.png
#   —— 前半段是个**只有主机名、没有路径**的占位（快科技文章页里那个 `img-kp` 图床），
#   后面那半段才是真正的图。整串交给 urlsplit 会得到主机名 "img-kp.mydrivers.comhttps"
#   → 域名解析失败 → 图片被拒（用户看到的就是"这条没图"）。
#   ⚠ 只在「scheme://host 后面**紧跟着**又一个 scheme://」时裁掉前半段：
#   正常地址即使查询串里带 http://（如 ?u=http://…），前半段也一定带路径或问号，不会误伤。
_URL_GLUED = re.compile(r"^https?://[^/\s?]+(?=https?://)", re.I)
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
    """归一化图片地址：剥畸形协议前缀、剥"两个协议黏一起"、补全协议相对地址与相对路径"""
    c = html.unescape(c or "").strip().strip("\"'")
    if not c or c.startswith(("data:", "javascript:", "#")):
        return None
    c = _URL_GLUED.sub("", c, count=1)      # 见上面 _URL_GLUED 的说明
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


# ============ 源健康度（只读观测，2026-10-24 UI 整改新增）============
# 右栏要显示「每个源最近抓到没有 / 抓了几条 / 连续失败几次」。做法是**被动记录**：
# 在抓取链路（_parse_one / _parse_hot）里顺手写一笔内存表，
# 绝不为它单独发请求；`GET /api/sources` 只读这份表 ——
# 所以页面上「打开/刷新源健康度」不会触发任何抓取（这是用户明确要求的硬约束）。
#
# 为什么不用日志或磁盘：气泡/页面都要能立刻读到；而且这些数字只在本次进程内有效，
# 重启后重新积累（不落盘 = 不会把上次的陈旧状态当成本轮的健康度）。
_SRC_HEALTH = {}          # id -> {id,label,region,cls,status,lastTs,lastCount,failStreak,okCount,failCount,error}
_SRC_HEALTH_LOCK = threading.Lock()
# 热榜不是 RSS 源，走 _parse_hot 里的三个平台函数；这里给它们起稳定的伪源 id。
HOT_SRC_LIST = (("hot-thepaper", "澎湃新闻热榜", "_hot_thepaper"),
                ("hot-cdsb", "红星新闻热榜", "_hot_cdsb"),
                ("hot-ifeng", "凤凰网热榜", "_hot_ifeng"))


def _note_src_health(src, ok, count=0, error=""):
    """记一笔某个源的抓取结果。src 传 SOURCES 里的那一项（热榜用伪源字典）。

    ⚠ 这段是**纯观测**：任何异常都在内部吞掉，绝不允许影响抓取/气泡主链路。
    """
    try:
        sid = src.get("id") or src.get("label") or "?"
        now = time.time()
        with _SRC_HEALTH_LOCK:
            ent = _SRC_HEALTH.get(sid)
            if ent is None:
                ent = {"id": sid,
                       "label": src.get("label") or sid,
                       "region": src.get("region") or "",
                       "cls": src.get("cls") or "",
                       "status": "idle",
                       "lastTs": 0, "lastCount": 0, "failStreak": 0,
                       "okCount": 0, "failCount": 0, "error": ""}
                _SRC_HEALTH[sid] = ent
            if ok:
                # 抓通但 0 条：不报「失败」，只标「降级」——站方改版/当期没更新都能看出苗头
                ent["status"] = "ok" if int(count or 0) > 0 else "warn"
                ent["lastTs"] = now
                ent["lastCount"] = int(count or 0)
                ent["failStreak"] = 0
                ent["okCount"] = int(ent.get("okCount") or 0) + 1
                ent["error"] = ""
            else:
                ent["status"] = "fail"
                ent["lastTs"] = now
                ent["lastCount"] = 0
                ent["failStreak"] = int(ent.get("failStreak") or 0) + 1
                ent["failCount"] = int(ent.get("failCount") or 0) + 1
                ent["error"] = str(error or "")[:80]
    except Exception:
        pass


def _all_sources_meta():
    """所有「应该出现」的源（用于把还没抓过的源以 idle 状态也列出来）。"""
    out = []
    for _rg in ("cn", "intl"):
        for s in (SOURCES.get(_rg) or []):
            out.append({"id": s.get("id"), "label": s.get("label") or s.get("id"),
                        "region": _rg, "cls": s.get("cls") or ""})
    for _sid, _label, _fn in HOT_SRC_LIST:
        out.append({"id": _sid, "label": _label, "region": "hot", "cls": "热榜"})
    # 【2026-10-28 新增】开源热榜：它不在 SOURCES 里（单独一条链路 /api/oss），
    # 但右栏「源健康度」该看得见它 —— 抓不到时用户能在角标里看出是这一栏没抓到，
    # 而不是"程序坏了"。
    if OSS_ENABLED:
        out.append({"id": OSS_SOURCE_ID, "label": OSS_SOURCE_LABEL,
                    "region": "cn", "cls": OSS_CLS})
    return out


def _parse_one(src):
    """带**单源隔离**的解析入口。

    2026-10-09 真机教训：mefcl 解析里一个 NameError 把整个「国内」栏带崩了 ——
    `_collect` 用 `ex.map(_parse_one, ...)` 迭代结果，任何一个源抛异常都会中断整栏。
    现在这里兜一层：**某个源坏了只丢它自己**，日志写清是哪个源、什么异常。
    """
    try:
        _items = _parse_one_raw(src)
    except Exception as ex:
        print(f"[warn] 源解析异常，已跳过该源: {src.get('id') or src.get('url')} - "
              f"{type(ex).__name__}: {ex}")
        # 【2026-10-24】健康度：这个源本轮失败（连续失败次数 +1，右栏会标红）
        _note_src_health(src, False, 0, f"{type(ex).__name__}: {ex}")
        return []
    # 【2026-10-09 用户要求「国际，娱乐栏目还是有国内新闻，你怎么不归纳到国内版里」】
    # 国际频道的娱乐/文化条目里，凡属**国内题材**（上海时装周、毛泽东故居、中国国庆…）
    # 一律不进国际栏 —— 挂在这里是因为它是**所有源的必经包装**，一处生效、不会漏出口。
    # 注意：CGTN 文化是**英文源**，判据必须中英文都有（只写中文会一条都拦不住）。
    if (src.get("region") or "") == "intl":
        _keep = [x for x in _items if not _drop_domestic_ent(x)]
        if len(_keep) != len(_items):
            print("[ok] %s: 国际栏剔除国内题材的娱乐/文化 %d 条"
                  % (src.get("id") or "?", len(_items) - len(_keep)))
        _items = _keep
    # 【2026-10-24】健康度：这个源本轮抓到了多少条（0 条会在右栏标成「降级」，可能改版）
    _note_src_health(src, True, len(_items))
    return _items


RSS_ENTRIES_CAP = 50      # 单个 RSS 最多解析多少条（见 _parse_one_raw 里的说明）


def _parse_one_raw(src):
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
    # 【2026-10-09 补源·国际栏】下面四个都是「列表页自带图」的站点专用解析
    # （共同的取舍：宁可少抓几条，也不去正文页补图 —— 用户要的就是自带图的源）
    if src.get("parser") == "cri_world":      # 国际在线·国际（news.cri.cn/world）
        return _parse_cri_world(src)
    if src.get("parser") == "xinhua_coxe":    # 新华网·体育（国际频道，www.news.cn/sports）
        return _parse_xinhua_coxe(src)
    if src.get("parser") == "people_list":    # 人民网·国际 / 娱乐（列表页 <li> 图卡）
        return _parse_people_list(src)
    if src.get("parser") == "gmw_list":       # 光明网·国际（world.gmw.cn 图卡）
        return _parse_gmw_list(src)
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
    max_age = _max_age_hours(src)
    cutoff = datetime.now(CST).timestamp() - max_age * 3600
    dropped = 0
    # 【2026-10-11】原来写死只取前 24 条。RSS 是按时间倒序的，取 24 条对「一天几十条」
    # 的通讯社源够用，但**更新慢的分区源**就被砍掉一半以上 —— 实测 CGTN 文化分区
    # 一次给 50 条（全部带图），只取前 24 条时用户看到的是"国际娱乐只剩一两条"。
    # 现在取满 RSS_ENTRIES_CAP 条，过期的一样被下面 `ts < cutoff` 滤掉，
    # 不会因此多显示旧新闻，只是让慢更新源在时效窗口内的稿子一条都不漏。
    for e in doc.entries[:RSS_ENTRIES_CAP]:
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


# 澎湃 rightSidebar 的共享缓存（被 cn 头条和热榜两个入口共用，见 _thepaper_data）
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
    # 【2026-10-09 用户要求「热榜必须有来源出处」】与 _parse_hot 同一套：这里也要补，
    # 因为**中新社·国际**那一路是直接进来的，没经过 _parse_hot 的统一出口。
    for it in parts:
        if not it.get("source"):
            it["source"] = _hot_source_of(it.get("label"))
    print(f"[ok] hot-intl: 多平台聚合共 {len(parts)} 条（百分位混排）")
    return parts


def _thepaper_data(ttl=120):
    """澎湃首页侧栏 JSON（头条 / 财经投資 / 编辑精选 / hotNews 全在这一个响应里）。

    做成缓存是因为它**同时被两个入口消费**：国内栏目的 cn-thepaper（头条）和热榜的
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
    """凤凰资讯：官网首页头条流 newsstream[]（约 28 条，含 url / title / newsTime / 题图）。

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
    # 【2026-10-26 更正注释】这里原来写 `MAX_AGE_HOURS.get("cn", 72)`，但 "cn" 键一直是 24，
    # 所以实际生效的一直是 24 小时 —— 对本源(热榜)反而是**对的**（热榜不豁免，见 CLS_MAX_AGE_HOURS），
    # 只是写法骗人。改成明说：热榜按热榜的窗口。
    cutoff = datetime.now(CST).timestamp() - CLS_MAX_AGE_HOURS.get("热榜", 24) * 3600
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


def _hot_source_of(label):
    """热榜条目的「来源出处」文案（2026-10-09 用户点名：「热榜必须有来源出处」）。

    用户要求的可读形式是「来源：凤凰网热榜 · 第4名」，所以字段值取「平台名 + 热榜」：
      凤凰网 → 凤凰网热榜 / 红星新闻 → 红星新闻热榜 / 澎湃新闻 → 澎湃新闻热榜 /
      中新社·国际 → 中新社·国际热榜。
    为什么单独加一个 `source` 字段而不是让前端拼 label+rank：
      接口里 label 原本只表示"哪家平台"，语义上跟"来源出处"不完全等价（红星/凤凰的
      热榜条目其实是本站首页推荐流，不是一张正式榜单），前端各自拼字符串迟早会不一致；
      统一在后端给一个明确字段，界面、气泡、自检三处用的是同一个值。
    """
    lb = (label or "").strip()
    if not lb:
        return ""
    return lb if lb.endswith("热榜") else lb + "热榜"


def _parse_hot():
    """实时热榜：聚合 {澎湃新闻, 红星新闻, 凤凰网} 三家（用户指定）。
    每条带平台标签（label），按「平台内归一化热度」跨平台混合排序。任一平台失败不影响其它。"""
    parts = []
    # 【2026-10-24 UI 整改】顺手把每个热榜平台的健康度记一笔（纯观测，不影响取数）。
    for sid, label, fn_name in HOT_SRC_LIST:
        fn = globals().get(fn_name)
        if fn is None:
            continue
        try:
            _got = fn() or []
        except Exception as ex:
            print(f"[warn] 热榜子源异常: {type(ex).__name__}")
            _note_src_health({"id": sid, "label": label, "region": "hot", "cls": "热榜"},
                             False, 0, type(ex).__name__)
            continue
        _note_src_health({"id": sid, "label": label, "region": "hot", "cls": "热榜"},
                         True, len(_got))
        parts.extend(_got)
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
    # 【2026-10-09 用户要求「热榜必须有来源出处」】在**唯一的出口**统一补 source 字段，
    # 国内热榜 / 国际热榜（国际热榜里来自国内综合源的那批也走这里）一次覆盖。
    for it in parts:
        if not it.get("source"):
            it["source"] = _hot_source_of(it.get("label"))
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
    global _MEFCL_LAST          # 2026-10-26 修: 下面要写这个模块级台账, 少了 global
    # 就会让 :1894 :1906 :1917 :1920 :1926 五条「回退上次结果」全抛 UnboundLocalError
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
    # 【2026-10-26 修】原来写 `MAX_AGE_HOURS.get("cn", 72)` = 24 小时，把「软件」栏目
    # 本该按 CLS_MAX_AGE_HOURS["软件"]=120 小时的窗口砍到一天；站方又只给日期(按当天 00:00 计)，
    # 于是"昨天发的"整批落在 now-24h 之前 —— 实测 2026-10-26 首页 20 条**全丢**，mefcl 在列表里消失。
    # 改走 _max_age_hours(src) 这一处口径，与抓取主路径一致。
    cutoff = datetime.now(CST).timestamp() - _max_age_hours(src) * 3600
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
                "cls": "头条", "title": title, "desc": _clean(it.get("subTitle") or "")[:200],
                "link": f"https://www.thepaper.cn/newsDetail_forward_{cid}",
                "image": pic, "published": ts, "heat": heat, "translated": False,
            })
    print(f"[ok] 澎湃新闻: {len(out)} 条")
    return out


def _parse_ifeng(src):
    """凤凰娱乐首页（娱乐栏目：明星/绯闻/八卦）。
    首页内嵌 JSON 数组，每条含：newsTime(YYYY-MM-DD HH:MM:SS) / thumbnails.image[0].url
    / title / url(ent.ifeng.com/c/xxx)。首页会混入少量旧推广稿，故按 newsTime 做时效过滤
    （窗口按 _max_age_hours：cn-ent 属「娱乐」= CLS_MAX_AGE_HOURS 168 小时；2026-10-26 之前
    这里写着"720h"但代码走的是 24h，两个数都没按声明生效过）。抓取失败返回 []，不影响其它源。"""
    raw = _fetch(src["url"])
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 凤凰娱乐抓取失败")
        return []
    html = raw.decode("utf-8", "ignore")
    items, seen = [], set()
    no_ts = 0            # 【2026-10-09】源站没给时间的条数（只用于日志，不伪造时间）
    # 【2026-10-26 修】同 mefcl：原来 `MAX_AGE_HOURS.get("cn", 24)` 让「娱乐」这个
    # 按栏目放宽到 168 小时的垂类源，在解析阶段就按 24 小时收口（docstring 里写的
    # "cn-ent 放宽到 720h" 更是从没生效过）。统一走 _max_age_hours(src)。
    max_age = _max_age_hours(src)
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
# 这两条和「红星/凤凰必须进头条」是冲突的——热榜那三家本身一条图都没有。
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
# 混着汽车/养生/育儿等泛频道内容），加娱乐关键词过滤后 40 条只剩 3 条、还有 2 条不是娱乐，
# 得不偿失，用户 2026-10-07 决定移除。机制保留，以后有需要再往里加。
TIME_FIX_CHANNELS = set()


def _day_start_cutoff():
    """今天 00:00（东八区）的时间戳。"""
    now = datetime.now(CST)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _select_fresh(items, region):
    """按栏目收口时效，**一律按时间倒序**（最新鲜的永远在最上面）。

    用户的几条要求互相冲突，这是权衡后的方案（每条都在代码里标了出处）：
      · 「常看常新、绝不看旧新闻」→ **新闻类（头条/热榜）严格当天**；
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


# ============ 【2026-10-09 用户点名的「财经不能错栏」】 ============
# 用户原话：「财经类新闻一定要区别化类，不能错放栏目」，并逐条点名了必须落「财经」的词：
#   股市 / 基金 / ETF / 央行 / 汇率 / 利率 / 楼市 / 财报 / IPO / 关税 / 贸易 / 大宗商品 / 黄金 / 油价。
#
# 改前实测（2026-10-09 15:4x，logs\气泡与栏目定稿-20261009.md 有完整清单）：
#   国内栏 4 条**该进财经却被判成「头条」**——界面 2 条（标题带 ETF 代码）、
#   澎湃 2 条（标题带 A股/沪指）。根因是「来源自带 cls」这一处说了算：
#   界面 / 澎湃 这两家是「头条」栏目源，它们的财经稿自然全落到头条，
#   而**站方栏目映射（articleSection）只用于国内/国际分流、压根不参与栏目命名**，
#   「时效规则」（CLS_MAX_AGE_HOURS）只管存留时长 —— 三处各管一段、谁也不管"内容该属于哪一栏"，
#   这就是错栏的真正来源。
#
# 现在的修法（只加一条"内容兜底"，不动另外两处）：
#   · 来源自带 cls 仍然是**第一权威**，不被本函数改动（华尔街见闻/中新网·财经/CGTN商业的条目原样保留）；
#   · 只有落在**话题栏目**里、而标题明确是财经的条目才改判成「财经」；
#   · 「热榜」**不改**：它是"平台榜单"这一维度，不是话题栏目，改了反而破坏榜单完整性，
#     而且热榜条目在气泡/界面上都带平台出处（source），不会被误读成财经栏目；
#   · 反向保护：只有命中**强财经特征词**才改判，避免把泛新闻硬塞进财经
#     （用户明确要求"不能反向把泛新闻塞进财经"）。实测改判后财经条目标题 100% 命中财经词。
#   · 时效一致性：财经在 CLS_MAX_AGE_HOURS 里**没有条目**（跟头条一样按"当天/24h"收口），
#     所以本函数放在 _select_fresh **之前**调用 —— 科技(96h)条目若因改判变成财经，
#     会按财经的"当天"窗口重新收口，不会出现"前天的股市稿按科技窗口混进来"。
FINANCE_TITLE_KEYWORDS = (
    # —— 用户逐条点名的 ——
    "股市", "股票", "基金", "ETF", "央行", "汇率", "利率", "房贷利率", "降息", "加息", "降准",
    "楼市", "房价", "房地产", "财报", "IPO", "关税", "贸易", "大宗商品", "金价", "黄金价格",
    "油价", "原油",
    # —— 同类强特征（用户口径的等价扩展，都是"只可能出现在财经稿里"的词）——
    "A股", "港股", "美股", "沪指", "深证成指", "创业板指", "科创50", "北证50", "涨停", "跌停",
    "券商", "债券", "国债", "美债", "收益率", "期货", "期货市场", "证监会", "交易所", "上市公司",
    "市值", "股价", "回购", "增持", "减持", "北向资金", "市盈率", "美联储", "彭博", "债券市场",
    "收涨", "收跌", "涨超", "跌超", "高开", "低开", "收盘", "开盘", "盘中",
)
# 命中上面这些词、但明显不是财经的用法（防止"黄金周""新机上市"这类误判）。
FINANCE_TITLE_SKIP = (
    "黄金周", "黄金档", "黄金时段", "黄金比例", "黄金分割", "黄金联赛", "黄金周假期",
    "新车上市", "上市新车", "手机上市", "游戏上市", "影片上映", "新机上市",
)
_FINANCE_RX = re.compile("|".join(re.escape(k) for k in FINANCE_TITLE_KEYWORDS))
# 允许被改判的栏目：话题栏目。热榜（榜单维度）与体育（国际）不在内，财经本身也不用改。
FINANCE_FIXABLE_CLS = {"头条", "科技", "软件", "娱乐", "体育"}   # 「体育」只在国际频道存在


def _finance_title_hit(title):
    """标题是不是"强财经"：命中强特征词，且不属于已知的非财经用法。"""
    t = title or ""
    if not t or not _FINANCE_RX.search(t):
        return ""
    for bad in FINANCE_TITLE_SKIP:
        if bad in t:
            return ""
    return _FINANCE_RX.search(t).group(0)


# ============ 【2026-10-09 用户要求】国际·体育只留"国际赛事" ============
# 用户原话：「国际，体育栏目里混着国内体育新闻」（说的是国际频道，不是国内频道）。
# 根因：国际频道用的 `新华网·体育`（news.cn/sports）本身是**国内站**，源里中超/CBA/全运会/人物稿与
# 国际赛事混着；CGTN 体育是国际台（中国国际电视台），不做过滤。
#
# 【2026-10-10 修正 —— 用户实测「体育也是堵不住漏洞」】
# 这一版原来的两道判据（`_INTL_SPORT_DOM_RX` / `_INTL_SPORT_INT_RX`）**判反了**：
#   ① 国内赛事词表里**没有登记中国自己的主力赛事**（中网 / WTT / 大冬会 一个都没有）；
#   ② "国际信号"表里却把「中网 / WTT / 大冬会 / 世界 / 国际 / 全球」甚至**一串外国国名**
#      都当成了国际信号 —— 于是「中网综合：郑钦文重返四强」「WTT 中国大满贯：王曼昱…进四强」
#      「U18 女篮亚洲杯：中国队负于澳大利亚队」全部靠"外国对手名 / 中国赛事名"过关，
#      实测 11 条里放行了 9 条中国赛事。
# 现在改成"**先看是不是国内赛事/国内主体**（命中即丢），再看有没有**体育意义上的外国信号**"，
# 两张表都换掉了（见下面的 `_INTL_SPORT_CN_EVENT_RX` 等）。
# 宁可少几条，也不让非国际赛事混进国际栏（用户明确说"国内的体育不值得看"）。


# 【2026-10-09】国际频道的娱乐/文化只留国际题材：标题是国内地标/国产/卫视这类，
# 又没有国际信号（奥斯卡/戛纳/海外/全球…）的，不进国际栏（用户：国际娱乐里还有国内新闻）。
# 中英文双语：CGTN 这类**英文源**的标题是英文（翻译在后面才做），只写中文词会漏判。
_INTL_ENT_DOM_RX = re.compile(
    r"上海|北京|广州|深圳|成都|杭州|南京|武汉|西安|重庆|天津|瑞金|延安|井冈山|"
    r"毛泽东|习近平|故宫|长城|央视|卫视|国产|华语|内地|中国|国庆|春节|中秋|"
    r"\bChina\b|\bChinese\b|Shanghai|Beijing|Ruijin|\bMao\b|Hainan|Guangzhou|Shenzhen|"
    r"Chengdu|Hangzhou|Nanjing|Xi'?an|Chongqing|Tianjin|National Day|domestic|\bCPC\b",
    re.I)
_INTL_ENT_INT_RX = re.compile(
    r"奥斯卡|戛纳|柏林|威尼斯|好莱坞|海外|全球|世界|国际|欧洲|美洲|非洲|亚洲|"
    r"希腊|法国|英国|美国|日本|韩国|意大利|西班牙|德国|俄罗斯|米兰|巴黎|纽约|伦敦|东京|"
    r"时装周|巡演|首映|票房|"
    r"Oscar|Cannes|Berlin|Venice|Hollywood|overseas|global|world|Europe|Asia|Africa|"
    r"America|Paris|Milan|New York|London|Tokyo|Greece|France|Italy|Spain|Germany|Russia",
    re.I)
_INTL_ENT_CLS = ("娱乐", "文化")


def _drop_domestic_ent(it):
    """国际栏里这条娱乐/文化是不是"国内题材"？是就丢掉（返回 True）。

    【2026-10-11 与出口统一口径】原来这里用的是 `_INTL_ENT_DOM_RX`/`_INTL_ENT_INT_RX`
    这对正则，而下面 2026-10-10 那段注释自己就写明它的**方向不对**（白名单里的
    「全球/世界/国际/亚洲」太泛、国内黑名单又是有限枚举，连「彭丽媛」「习近平」
    都不在里面）—— 也就是说出口早已换成新判据，**源头却还挂着这套旧闸门**，
    两处口径不一致，CGTN 文化的稿子被砍两遍（真机：8 条 → 源头砍 2 → 出口再砍）。

    现在两处都用同一张表 `INTL_CHANNEL_LOCAL_ONLY`（纯国内地方事务），判据只有一处，
    不会再出现"源头说国内、出口说国际"这种自相矛盾。

    影响面：**只有 `intl-cgtn-culture` 这一个源**会走这里（SOURCES 里 region=intl
    且 cls=娱乐 的就它一家；凤凰娱乐/新浪娱乐/国际在线娱乐都是 region=cn），
    所以放开不会把国内明星八卦放进国际栏 —— CGTN 不发八卦。
    """
    if (it.get("cls") or "") not in _INTL_ENT_CLS:
        return False
    ti = it.get("title") or ""
    return any(k in ti for k in INTL_CHANNEL_LOCAL_ONLY)


# ============ 【2026-10-10 用户实测报障】直连国际源的"国内内容"漏筛 ============
# 用户原话：「国际板块的娱乐有混进国内消息了，怎么回事？堵不住这个漏洞」「体育也是堵不住漏洞」。
#
# 【根因】国际栏的条目有两个来源，两边的国别判定**强度不一样**：
#   ① 国内板块按关键词分流过来的（_split_cn_scope / _split_hot_by_scope）——
#      必须过 `_looks_foreign`/`_looks_ent_foreign` + `not _looks_domestic` 两道关；
#   ② SOURCES 里**直连的国际源**（CGTN 文化 / 新华网·体育 …）—— 一个过滤器都不过。
#      源头那两道老闸门都太松，拦不住真数据：
#        · `_drop_domestic_ent`（娱乐）要求"像国内 且 完全不像国际"，而它的国际白名单里
#          有「全球/世界/国际」这种泛词 —— 「中国如何利用数字技术为全球女性赋权」
#          命中「中国」也命中「全球」→ 放行；
#        · `_INTL_SPORT_INT_RX`（体育）把**中国自己的赛事名**当成了国际信号
#          （中网 / WTT / 大冬会 / 世界 / 国际），反而 `_INTL_SPORT_DOM_RX` 里
#          一个都没登记 → 11 条里放行了 9 条中国赛事。
#      新华网是国内通讯社、CGTN 是中国国际电视台，这两个源里**天然含国内内容**，
#      所以"直连源"这一路必须补上和第①路同一口径的判定。
#
# 【本次修法】只作用于 `region == "intl"` 的条目，两条口径：
#   · 娱乐：必须有**外国具体对象**（复用 `ENT_FOREIGN_MARKERS`，与国内分流那条路同一个表），
#     且不得 `_looks_domestic`；
#   · 体育：必须有**体育专用的外国信号**，且不得命中"国内赛事/国内主体"表。
#
# 为什么体育不复用 `FOREIGN_MARKERS`：那张表是"通用地名/人名表"，体育里几乎每条中国赛事
#   都带外国对手名（澳大利亚队 / 意大利都灵 / 韩国组合 / 匈牙利），按它判会**一条都拦不住**
#   —— 这正是体育"堵不住"的原因。所以体育用一张**只看赛事与主体**的表，见下。
# 娱乐为什么不再用 `_INTL_ENT_DOM_RX/_INTL_ENT_INT_RX` 这对正则：它的国际白名单里
#   「全球/世界/国际/亚洲」太泛（「…为全球女性赋权」就是这么漏进来的），
#   而国内黑名单又是有限枚举（「彭丽媛」「习近平」都不在里面）。改成**白名单要具体对象**
#   之后，判据方向才是对的（和用户 2026-10-07 定娱乐口径时说的"光有外国地名不算"一致）。

# 体育·国内赛事 / 国内主体（命中即丢，不管有没有外国名字）。
_INTL_SPORT_CN_EVENT_RX = re.compile(
    r"中网|中国大满贯|WTT\s*中国|大冬会|全运会|全国锦标赛|全国冠军赛|全国联赛|"
    r"中超|中甲|中乙|中冠|足协杯|CBA|WCBA|乒超|排超|羽超|象甲|省运会|市运会|"
    r"国足|中国队|中国女篮|中国男篮|中国女排|中国男排|亚洲杯")

# 体育·非国际赛事的人物稿/专栏/国内单位（"朱思冰：第134『削』翻世界第四"这类）。
# ⚠ 这里**只放"国内单位 + 国内联赛 + 世界排名"**，绝不放奥运/世界杯/世锦赛这类
#   **真国际赛事的名字** —— 第一版把「奥运会/世界杯/世锦赛/全运会」写进了这张表，
#   结果「超六成匈牙利民众支持布达佩斯申办2036年奥运会」被自己的"国内信号"拦掉了
#   （实测抓到，见测试报告第 1 组）。国际赛事名归下面的 `_INTL_SPORT_INT_RX` 管。
_INTL_SPORT_CN_ACTOR_RX = re.compile(
    r"新华社|新华网|央视|中国台协|中国足协|中国篮协|"
    r"国内联赛|青训|世界杯预选赛|世预赛国足|"
    r"世界第[一二三四五六七八九十百\d]|队史|总决赛")

# 体育·外国信号（**具体赛事 / 外国主体**，不是泛地名）。
# 刻意**不含**「世界 / 国际 / 全球」这类泛词：原来就是它们把「朱思冰…世界第四」
# 「国际举联…举重世界纪录」当成了国际赛事。要"世界"就必须是「世界纪录」这种具体说法。
# 保留了一小串**外国名将**（原表的 `焦科维奇/莫雷加德/凯恩`，原本就登记过的）——
# 「焦科维奇晋级上海大师赛四强」这类标题没有赛事英文缩写、但有实打实的外国主角。
_INTL_SPORT_INT_RX = re.compile(
    r"奥运|冬奥|残奥|世锦赛|世乒赛|世羽赛|世界纪录|世界杯|欧洲杯|欧冠|欧联|欧协联|"
    r"亚冠|世预赛|温网|美网|法网|澳网|四大满贯|大师赛|白金赛|钻石联赛|"
    r"NBA|WNBA|NFL|MLB|NHL|ATP|WTA|F1|UFC|国际足联|国际奥委会|国际举联|国际乒联|"
    r"英超|西甲|意甲|德甲|法甲|欧洲联赛|焦科维奇|德约科维奇|莫雷加德|凯恩")


def _looks_sport_foreign(title):
    """体育条目有没有**体育意义上的外国信号**（国际赛事 / 世界纪录 / 外国联赛）。

    和 `_looks_foreign` 的区别：这里不认泛地名。"超六成匈牙利民众支持布达佩斯申办
    2036年奥运会"靠「奥运」过，"U18 女篮亚洲杯…澳大利亚队"里有「澳大利亚」也进不来。
    """
    t = title or ""
    if _INTL_SPORT_CN_EVENT_RX.search(t):
        return False
    if _INTL_SPORT_CN_ACTOR_RX.search(t):
        return False
    return bool(_INTL_SPORT_INT_RX.search(t))


# 娱乐·**本次补的外国具体对象**（外国大制片厂 / 流媒体 / 电视台）。
# 为什么要补：CGTN 文化这类直连源里有真国际的娱乐稿，标题里一个常见"外国对象词"都没有
#   ——「关于派拉蒙-华纳兄弟合并的须知事项」（英文原题 What to know about the
#   Paramount-Warner Brothers merger）在 `ent_foreign` 表里**一个词都不命中**，
#   沿用旧表会把它一起丢掉（用户要的是"堵国内"，不是"把国际也砍掉"）。
# 为什么写在代码里而不是只写 data/keywords.txt：keywords.txt 读不到时各组会退回空表
#   （fail-safe 设计），那时娱乐栏就会**一条都留不下**；把这张小表做成代码常量，
#   即使数据文件丢了，判据也不会反向失效。
# 为什么不用「环球 / 华纳」这种短名：会撞上中文用法（环球时报 / 环球网），
#   所以只登记**含义唯一**的全名（环球影业 / 索尼影业 / 20世纪影业 / 华纳兄弟）。
#
# 【必须中英文都登记】过滤发生在 `_collect` 出口，那时候 CGTN 这类**英文源**
# 的标题还是英文原题（译文由后台翻译线程稍后才写回），所以只写中文词等于对英文源无效。
# 这条是实测抓到的：只写「派拉蒙/华纳兄弟」时，真机上
# 「What to know about the Paramount-Warner Brothers merger」照样被当成"没有外国对象"丢掉
# —— 用户要的是堵国内，不是把国际娱乐也砍掉。
_ENT_FOREIGN_EXTRA = frozenset({
    "派拉蒙", "华纳兄弟", "环球影业", "索尼影业", "20世纪影业", "梦工厂", "皮克斯",
    "HBO", "BBC", "NHK", "宝莱坞", "宝冢",
    "Paramount", "Warner", "Universal Pictures", "Sony Pictures", "Columbia Pictures",
    "20th Century", "DreamWorks", "Pixar", "Hollywood", "Netflix", "Disney", "Marvel",
    "Bollywood", "Emmy", "Grammy", "Oscar", "Cannes", "Venice", "Berlin",
    # 【2026-10-11 补】用户「国际娱乐太少」。这里只放**最常用、且不会撞中文用法**的一批，
    # 完整词表仍在 data/keywords.txt 的 [ent_foreign]（那边丢了也不会一条都判不出）。
    "奥斯卡", "好莱坞", "格莱美", "戛纳", "金球奖", "迪士尼", "漫威", "韩娱", "日娱",
    "圣丹斯", "BAFTA", "安妮奖", "托尼奖", "宫崎骏", "吉卜力", "BTS", "BLACKPINK",
    "Taylor Swift", "Beyonce", "Rihanna", "Adele", "Spider-Man", "Star Wars",
    "Harry Potter", "Game of Thrones", "Squid Game", "Hulu", "Lionsgate", "MGM",
})
# 注意：与 `ENT_FOREIGN_MARKERS` 合并的那一步（`_ENT_FOREIGN_ALL`）**必须写在
# `_load_keywords()` 之后**（见下面关键词表那段）—— 关键词表在本文件后半段才载入，
# 在这里合并会 NameError。判据函数在**调用时**才读这个全局名，所以顺序没问题。


# ============ 【2026-10-10 第二批】要闻 / 财经 / 科技 三栏补同一道闸门 ============
# 上级复核后拍板扩到其余国际栏（用户原话：「三款软件改到合格为止，别问我了」）。
# 实测这三栏也在漏同一类内容（都是直连源里的国内题材）：
#   · 国际·财经：`intl-cgtn-biz` 的「BizDataDive：中国央行如何看待人民币汇率」
#     「中国，欧盟在"务实"谈判后达成贸易，投资成果」「BizDataDive：中国国庆假期活动热闹非凡」；
#   · 国际·科技：`intl-cgtn-tech` 的「中国发布发展新型优质生产力的指导方针」
#     「中国-老挝电力线路超过10亿千瓦时」「中国的北极考察带回了气候模型的数据」
#     「IAC 2026：中国航天公司的目标是扩大全球市场份额」；
#   · 国际·要闻：`intl-cgtn` 的「王毅会见太平洋岛国领导人代表团」
#     「中国向肯尼亚提供200万$埃博拉和干旱援助」「博茨瓦纳总统将对中国进行国事访问」
#     （热榜那路另有来源，见下）。
#
# ⚠ 要闻的口径与其它栏**不一样**，这是本条最要紧的地方：
#   上级明确要求「外国主体 + 中国相关」的稿子（某国总统**访华**、**中美会谈**、**中欧**…）
#   属于国际新闻，**必须保留**。所以判据是「**主体是不是中国**」，不是「标题里有没有中国」。
#   直接套 `DOMESTIC_MARKERS`（里面有「中方」「驻华」）会把
#     「中方代表：应在中东整体大局势下解决也门冲突」（中国的外交表态，属国际）
#     「驻华大使谈…」一起误杀 —— 所以要有 ①② 两条豁免。
#
# ① 中国在**客体位置**（被访问、被指向）→ 不是"中国主体"，豁免。
#    注意不能只写 `(?<!对)中国`：里层仍是"中国"三个字，那样等于没豁免。
#    【2026-10-10 修正】英文分支原来写成 `\bChina\b(?=…visit|market|economy…)`，方向反了 ——
#      它把 `China's central bank`（`China's` → 命中 `'s`）和 `China, EU reach…`（`China` 后是逗号，
#      全不匹配）**排除了**，却把 `visit China`（`China` 在句尾）也算成"客体"。
#      真机后果：财经/科技/要闻三栏的英文国内稿**一条都拦不住**（实测 79 条判出 0 条）。
#    现在改成**只认真正的"被访问"句式**」：`(state )?visit(s|ed)? to China` / `trip to China` …
#    即 `China` 前面必须是 to / in / of / from 这类介词性引导词。
_NEWS_CN_OBJECT_RX = re.compile(
    r"访华|对中国|向中国|在华|驻华|赴华|来华|中国行|谈中国|涉中国|"
    # 英文客体式：`(state )?visits? to China` / `visit China` / `trip to China` / `tour China` /
    # `to China` / `in China` / `of China` …（China 前面必须是"去/在/属于"这类引导词）
    r"\b(?:visits?|visited|visit|trips?|tour|tours|travels?|returns?|"
    r"to|in|into|of|from|towards?|on|across|around)\s+China\b",
    re.I)
# ② 中国/中方是**表态方**（后面直接跟冒号）→ 谈的是国际事务，豁免。
_NEWS_CN_SPEAKER_RX = re.compile(r"(中国|中方|我国)(政府|外交部|代表|常驻代表)?\s*[:：]")
# ③ 中国在**主体位置**做了什么 → 国内主体。
#    中文源（新华社体育/CGTN 译文）与英文源（CGTN 原文）**都要认**。
#
#    ⚠ 英文这一支是 2026-10-10 的**真机验收失败**换来的教训，务必看清：
#      第一批补英文词只补了娱乐栏，财经/科技/要闻三栏当时只写了中文判据。
#      而过滤发生在 `_collect` 出口，CGTN 这类**英文源的标题还是英文原题**
#      （中文是后台翻译线程稍后才写回的）—— 于是三栏的英文国内稿**一条都拦不住**，
#      真机 79 条判出 0 条（上级实测：China's central bank / China, EU reach /
#      China issues guidelines / China-Laos power line 全在）。
#      所以英文主体式必须写全：China / Chinese / Beijing + 任何**小写英文词**，
#      以及 China's / China, / China-<某国> 这几种形态。
#
#    ⚠ 中文那一支踩过的坑也要记住：第一版把「中国+感受/发布/表示」这类**裸动词**放进来，
#      结果「走进泰国东北部社区 感受伊桑文化的古朴与活力」被误判成国内 —— 标题里根本没有
#      "中国"两字，是"中国"（来自别的字）+裸动词（来自别的词）**跨词拼**出来的。
#      所以「中国+动作」一律要求**紧跟具体宾语或句式助词**，不做模糊匹配。
#
#    顺序有讲究：**先长后短**、先具体后笼统 —— 正则取第一个能匹配的分支。
_NEWS_CN_SUBJECT_RX = re.compile(
    # 中国 + 职务/机构 + 人名 + 动作（"中国外长王毅会见…"）
    r"中国\S{0,6}(?:外长|防长|部长|主席|总理|发言人|领导人|代表)"
    r"[\u4e00-\u9fa5]{2,4}(?:会见|会晤|出席|访问|致电|致信|贺信|签署|宣布|发布|"
    r"调研|考察|视察|批示|主持|召开|讲话|致辞|磋商|交涉|表态|回应)"
    # 中国 + 动作 + 明确宾语/助词（只认这些句式，不做模糊匹配）
    # ⚠ 末尾那个分支 `(?![^\s])` 是"标题到这儿就结束了"的意思。
    #   不能写成 `\s|$` —— 多选分支里的 `$` 会被当成**字面量美元符**，
    #   于是「中国向肯尼亚提供200万$…」这种带 `$` 的标题反而匹配上（真机实测踩到）。
    r"|中国(?:发布|宣布|推出|启动|批准|通过|签署|修订|印发|出台|部署|强调|要求|"
    r"表示|称|将|已|正|向|对|为|在)(?:了|将|已|正|在|与|同|和|对|就|这|该|"
    r"[\u4e00-\u9fa5]|(?![^\s]))"
    # 中国 + 机构/领域名词（这一类本身就是国内主体）
    r"|中国(?:央行|政府|官方|商务部|财政部|外交部|共产党|航天|军方|海军|空军|陆军|"
    r"科学院|工程院|科协|企业|公司|品牌|制造|经济|市场|社会|消费者|游客|民众|"
    r"高校|大学|城市|农村|农业|工业|能源|电力|铁路|公路|航空|港口|医疗|教育|"
    r"最高领导人|领导人|主席|总理|外长|防长|部长|省长|市长|发言人|代表团|国产|"
    # 【2026-10-10 第三批】"中国 + 某类专家/机构" 的职业/部门词——
    # 真机实测漏网：摘要「中国**气象**专家呼吁…」（用户报的厄尔尼诺那条）。
    r"气象|疾控|统计|地震|海洋|地质|测绘|水利|环保|生态|林业|畜牧|渔业|农业|"
    r"医学|医药|卫生|防疫|疫苗|科学|科研|工程|技术|通信|电子|计算机|软件|半导体|"
    r"汽车|钢铁|煤炭|石油|化工|纺织|建筑|交通|民航|邮政|粮食|食品|体育|文化|旅游|"
    # 中国 + 驻外机构 / 使领馆 / 主办活动（上级点名：「中国驻约翰内斯堡总领馆举办国庆招待会」）
    r"驻\S{2,12}(?:使领馆|总领馆|领事馆|大使馆|领馆)|"
    r"驻\S{2,12}(?:举办|举行|主办|承办|召开|出席|向\S{0,8}捐赠)|"
    # 中国 + 使领馆/官方机构作主语
    r"总领馆|大使馆|领事馆|"
    # 中国 + 官方活动动词（招待会/国庆/新春这类，主语是中国机构）
    r"举办|举行|主办|承办)"
    # 中国 + 人名 + 动作（动词用**窄表**）
    r"|中国[^\s，。：:、]{2,4}(?:会见|会晤|出席|访问|致电|致信|贺信|签署|宣布|"
    r"发布|调研|考察|视察|批示|主持|召开|讲话|致辞|磋商|交涉|表态|回应|"
    r"任命|免去|辞去|当选|连任|访俄|访美|访欧|访非)|"
    # 【2026-10-10 真机补】"中国 + 表态动词" 紧邻式：真机漏网「中国抨击日本右翼势力…」。
    # 这里**要求紧邻**（中间不许有别的内容）—— 与中文那支踩过的坑正好相反：
    # 那个坑是"中国"与动词**隔着别的词**才误命中（"…社区 感**受**…"），紧邻式是安全的。
    r"中国(?:抨击|谴责|警告|敦促|呼吁|批评|指责|回应|反驳|抗议|祝贺|"
    r"发布|宣布|表示|强调|要求|批评)"
    # 我国
    r"|我国|"
    # 英文：China / Chinese / Beijing + **任何小写英文词**（China issues / Chinese space /
    #       Beijing slams / China's central bank / China, EU reach / China-Laos power line）
    #   为什么是"任意小写词"而不是动词表：CGTN 的用词太活（issues/unveils/slams/extends/
    #   passes/aims/brings…），列动词必然漏。改判断方向后**不会**误伤外国主体稿 ——
    #   "Trump…"/"NASA's…"/"Brazil sues…" 里根本没有 China/Chinese/Beijing。
    #   注意：`to visit China` 这种客体式已由 ① 先行豁免，`China's` 也算主体式
    #   （China's central bank / China's Arctic expedition 都是国内主体）；
    #   带年份的也要认（China's 2026 National Day holiday），所以允许 `['’]s` 与
    #   数字/冠词出现在真正的主语名词之前。
    r"\b(?:China|Chinese|Beijing)(?:['’]s)?\s*[-,]?\s*(?:the\s+)?(?:\d{1,4}\s+)?[a-z]+|"
    # 英文：China/Chinese + 职务（中国总理主持国务院会议这类）
    r"\b(?:China|Chinese)\s+(?:premier|president|foreign|defence|defense|commerce|"
    r"central|state|top|senior|vice|deputy|leader|leaders|government|officials|"
    r"official|firms|companies|carmakers|regulator|authorities|military|navy|"
    r"coast|space|aerospace|scientists|students|tourists|consumers|market|"
    r"economy|banks|chipmakers)\b|"
    # 英文：`China <动词> (with) …`（"中国会见/会谈/访问/援助…"这类动作句）。
    # 【2026-10-10 真机补】`\bChina\s+[a-z]` 那一支要求 China 后面**紧跟**小写词，
    # 于是 `China extends $2 million…` 能中，但 "China's Arctic…" 与
    # "China-Laos…" 要靠 `['’]s` / `[-,]` 那一支；而 `China meets with…`
    # （动词在 China 之后、宾语之前）也漏 —— 这里补上**动词**这一路。
    r"\bChina\s+(?:meets?|met|holds?|held|extends?|extended|delivers?|delivered|"
    r"hands?|handed|grants?|granted|donates?|donated|sends?|sent|signs?|signed|"
    r"reaches?|reached|agrees?|agreed|joins?|joined|attends?|attended|"
    r"visits?|visited|opens?|opened|completes?|completed|passes?|passed|"
    r"issues?|issued|unveils?|unveiled|launches?|launched|releases?|released|"
    r"plans?|planned|aims?|aimed|says?|said|announces?|announced|"
    r"urges?|urged|calls?|called|warns?|warned|slams?|slammed|"
    r"condemns?|condemned|opposes?|opposed|pushes?|pushed|expands?|expanded)\b", re.I)


# ④ 中国党政一把手/外事口径的**人名**：标题常常不带「中国」两个字
#    （真机实测「王毅会见太平洋岛国领导人代表团」—— 主体是中国外长，标题里没有"中国"）。
#    只列**当下在任、且在国际新闻里高频**的名字，避免把语料撑大后误命中。
#    代价：人事更替后要手动更新（这是已知的取舍，写在这里免得以后忘）。
_NEWS_CN_LEADERS = (
    "习近平", "李强", "王毅", "赵乐际", "王沪宁", "丁薛祥", "李希", "韩正",
    "彭丽媛", "张又侠", "何立峰", "王小洪", "吴政隆", "谌贻琴",
    # 【2026-10-10 补】英文源（CGTN）在 `_collect` 出口的标题**还是英文**，
    # 所以人名表必须**中英文都有** —— 真机实测漏网的就是
    # "Wang Yi meets with delegation of leaders from Pacific Island countries"。
    "Wang Yi", "Xi Jinping", "Li Qiang", "Zhao Leji", "Wang Huning",
    "Ding Xuexiang", "Li Xi", "Han Zheng", "Peng Liyuan", "He Lifeng", "Wang Xiaohong",
)

# ⑥ 【2026-10-15 用户口径】"中国主体"要认得出来的两个补充维度
# 起因（真机 2026-10-15 实测）：国际栏 97 条里有 9 条明明是中国的稿子，判据却返回"不知道"
#   （`_news_subject_verdict` → None → 当成不是国内 → 留在国际栏）：
#     「"中国绿"风行河内」「中国联合展台亮相法国戛纳电视节」
#     「"中国鄂温克驯鹿社区的气候与社会变迁"边会在冰岛举行」
#     「北京12345论坛如何重塑全球城市治理谈判」「欧洲议会议员：北京12345热线…」
#     「全球最大的工业博览会在上海开幕」「亚冠精英联赛：上海海港主场战平…」
#     「市场驱动型人民币与全球再平衡之路」「澳前总理陆克文现身南锣鼓巷…北京变化」
#   根因：老判据只认「中国 + 特定机构/动词」的白名单，上面这些（中国地名、人民币、
#   "中国+普通名词"）一条都不命中。
#   用户 2026-10-15 选定口径：**中国主体的稿子一律不进国际栏**（含外交经贸类）。
#   ① 中国地名 / 中国标识出现在标题里 → 中国主体；
#   ② 标题**开头**就是 中国/我国/中方/China/Chinese/Beijing（主语位置）→ 中国主体。
#   ⚠ 仍然保留"中国在客体/表态位置"的豁免（见 `_NEWS_CN_OBJECT_RX` / `_NEWS_CN_SPEAKER_RX`）：
#     「某国总统访华」「外媒评价中国」这类**外国主体**的稿子照样留在国际栏。
_NEWS_CN_PLACE_RX = re.compile(
    r"北京|上海|广州|深圳|天津|重庆|成都|武汉|西安|南京|杭州|苏州|青岛|大连|厦门|长沙|郑州|"
    r"合肥|济南|福州|昆明|贵阳|南宁|哈尔滨|长春|沈阳|石家庄|太原|兰州|银川|西宁|乌鲁木齐|拉萨|"
    r"呼和浩特|海口|三亚|无锡|宁波|温州|东莞|佛山|珠海|中山|泉州|南通|徐州|常州|绍兴|嘉兴|"
    r"洛阳|烟台|潍坊|唐山|保定|香港|澳门|人民币|亚冠|中超|CBA")
_NEWS_CN_HEAD_RX = re.compile(
    r"^[\s【\[（(「『《\"'“”‘’]*"
    r"(?:(?:视频|图集|图解|直播|快讯|独家|组图|海报|动画|H5)\s*[丨|｜:：\-—]?\s*)?"
    r"[\s【\[（(「『《\"'“”‘’]*\s*(?:中国|我国|中方|China|Chinese|Beijing)", re.I)

# 【2026-10-15 修误判】强词表（[cn_subjects]）里有 4 个词**别的国家也在用**：
#   外交部 / 商务部 / 国防部 / 财政部。真机实测两条真国际稿就是被它们判成国内的：
#     「莫斯科警告说，东京的信息收集尝试将使两国关系紧张」（摘要：**俄罗斯外交部**发言人…）
#     「伊朗就空军基地事件指控传唤英国特使」（摘要：**伊朗外交部**周二传唤…）
#   所以这 4 个词只有在**前面不是外国国名**时才算"中国主体"：
#     「外交部发言人表示…」→ 算（默认就是中国口径）；
#     「俄罗斯外交部…」→ 不算。
_NEWS_CN_GENERIC_MIN = ("外交部", "商务部", "国防部", "财政部")
_FOREIGN_MIN_PREFIX = (
    "美国", "俄罗斯", "苏联", "英国", "法国", "德国", "日本", "韩国", "朝鲜", "印度", "伊朗",
    "以色列", "土耳其", "乌克兰", "欧盟", "澳大利亚", "加拿大", "巴西", "墨西哥", "阿根廷",
    "埃及", "南非", "沙特", "阿联酋", "卡塔尔", "泰国", "越南", "菲律宾", "印尼", "马来西亚",
    "新加坡", "巴基斯坦", "阿富汗", "叙利亚", "伊拉克", "黎巴嫩", "巴勒斯坦", "波兰", "荷兰",
    "比利时", "西班牙", "意大利", "葡萄牙", "希腊", "瑞典", "挪威", "芬兰", "丹麦", "瑞士",
    "奥地利", "匈牙利", "捷克", "罗马尼亚", "塞尔维亚", "白俄罗斯", "哈萨克斯坦", "古巴",
    "委内瑞拉", "哥伦比亚", "智利", "秘鲁", "新西兰", "爱尔兰", "冰岛", "卢森堡")


def _generic_min_is_cn(text, word):
    """通用部委词在 text 里出现时，是不是"中国口径"（前面没有外国国名）。"""
    for m in re.finditer(re.escape(word), text):
        head = text[max(0, m.start() - 6):m.start()]
        if not any(p in head for p in _FOREIGN_MIN_PREFIX):
            return True
    return False


def _has_cn_marker_title(title):
    """标题里有没有「中国标识」（中国地名 / 人民币 / 标题开头就是中国…含义的）。

    ⚠ **只在标题上用，摘要上不许用**（2026-10-15 实测教训）：
      摘要里随口提一句中国城市（「……对中国的影响」）就会被误杀 —— 真机里
      「莫斯科警告说，东京的信息收集尝试将使两国关系紧张」与
      「伊朗就空军基地事件指控传唤英国特使」两条**真国际稿**就是这么被判成国内的。
      标题里出现中国地名/人民币基本就是中国的事；摘要不行。
    """
    t = title or ""
    if not t:
        return False
    return bool(_NEWS_CN_PLACE_RX.search(t) or _NEWS_CN_HEAD_RX.match(t))

# ⑤ 要闻专用的**强国内机构**（第一层：只放中国党政机构/官方口径标识）。
#    为什么不用 `DOMESTIC_MARKERS`：那张表是为**中文源**设计的，里面有「社区」「村民」
#    「全县/全市」这类**普通名词**。真机实测踩到：「走进泰国东北部社区 感受伊桑文化的
#    古朴与活力」被 `DOMESTIC_MARKERS` 里的「社区」误判成国内 —— 而它是国际在线的境外
#    见闻稿，必须留在国际要闻。要闻这一路是中文源，用那张表的误伤面太大。
#    所以这里只留**外国标题里不会出现**的机构名与国内政治概念。
_NEWS_CN_ORG_MARKERS = (
    "外交部", "商务部", "国防部", "国台办", "国务院", "发改委", "教育部", "公安部",
    "文旅部", "财政部", "工信部", "住建部", "农业农村部",
    "全国人大", "全国政协", "中央", "省委", "市委", "县委", "区政府",
    "两岸", "台海", "解放军", "东部战区", "南部战区", "火箭军",
    "人民日报", "新华社", "光明日报", "央视", "春晚",
    "中国队", "国足", "中超", "CBA",
)
# 第二层：`DOMESTIC_MARKERS` 里排掉那些**会出现在外国/国际标题里的普通名词**之后剩下的。
# 排掉的原因逐条写清（每条都有真机或构造的误伤场景）：
#   · 社区 / 村民 / 国内 / 全县 / 全省 / 全市 / 我县 —— 普通名词，境外稿里也常出现；
#   · 中方 / 驻华 —— 外国主体 + 中国相关的稿子（访华、驻华大使），**上级明确要求保留**；
#   · 中国 / 我国 / 国产 / 中国内地 —— 太泛，主体/客体位置由 ①②③④ 精判，不在这里一刀切；
#   · 中国影片 / 华语片 / 国产片 —— 「中国影片入围戛纳」是**在外国的国际赛事**（属国际），
#     不该因为出现"中国影片"就砍掉。
_NEWS_CN_MARKER_SKIP = frozenset({
    "社区", "村民", "国内", "全省", "全市", "我县", "全县",
    "中方", "驻华", "中国", "我国", "国产", "中国内地",
    "中国影片", "华语片", "国产片",
})
_NEWS_CN_STRONG_CACHE = []


def _news_cn_strong():
    """真正用到的"要闻强国内特征词"表（首次调用时算好并缓存）。

    为什么做成函数而不是模块级常量：`DOMESTIC_MARKERS` 在**本文件后半段**才载入
    （关键词表那段），在这里直接引用会 NameError —— 同 `_ENT_FOREIGN_ALL` 那个坑。
    """
    if not _NEWS_CN_STRONG_CACHE:
        _NEWS_CN_STRONG_CACHE.append(
            tuple(_NEWS_CN_ORG_MARKERS)
            + tuple(sorted(k for k in DOMESTIC_MARKERS
                           if k not in _NEWS_CN_MARKER_SKIP)))
    return _NEWS_CN_STRONG_CACHE[0]


def _news_subject_verdict(text):
    """"主体是不是中国"的三态判定：`True` 是 / `False` 不是 / `None` **判不出来**。

    为什么要有 `None` 这一态（而不是直接给 True/False）：摘要兜底（见
    `_looks_domestic_news_item`）**只在标题判不出来时才允许生效**。
    如果标题已经明确"不是中国主体"（例如 "Trump strikes deal with Putin on oil"），
    摘要里就算出现"中国"二字也不该改判 —— 否则会大面积误杀外国稿。
    """
    t = text or ""
    if not t:
        return None
    # 豁免优先：中国在客体位置 / 中国是表态方 → 明确"不是国内主体"
    if _NEWS_CN_OBJECT_RX.search(t):
        return False
    if _NEWS_CN_SPEAKER_RX.search(t):
        return False
    if _NEWS_CN_SUBJECT_RX.search(t):
        return True
    if any(k in t for k in _NEWS_CN_LEADERS):
        return True
    _strong_hit = [k for k in _news_cn_strong() if k in t]
    if _strong_hit:
        # 【2026-10-15】通用的 4 个部委词要区分"谁的部委"（见 _NEWS_CN_GENERIC_MIN 注释）
        if any(k not in _NEWS_CN_GENERIC_MIN for k in _strong_hit):
            return True
        if any(_generic_min_is_cn(t, k) for k in _strong_hit
               if k in _NEWS_CN_GENERIC_MIN):
            return True
    return None


def _looks_domestic_news(title):
    """「要闻」专用（**只看标题**）：主体是不是中国，或标题里带中国标识。判不出来当"不是"。

    【2026-10-15 用户口径】加了 `_has_cn_marker_title`：只有真机那 9 条
    （中国地名/人民币/标题开头是中国）以前判不出来，全留在国际栏。
    """
    return _news_subject_verdict(title) is True or _has_cn_marker_title(title)


def _item_texts(it):
    """一条新闻里可以拿来做国别判定的所有文本（标题 + 摘要，中英都算）。

    ⚠ 为什么必须带上摘要：用户 2026-10-10 在界面上抓到
      「专家敦促为超级厄尔尼诺现象中的极端天气做好准备」——
      **英文标题完全看不出中国主体**（Experts urge preparedness for extreme weather
      amid super El Nino），可摘要里明写「**中国气象专家**呼吁采取更强有力的措施…」，
      英文摘要同样是 `Chinese meteorological experts called for…`。
      只看标题就是漏。
    ⚠ 为什么要带上 `*_en` / `*_cn`：过滤跑在 `_collect` 出口，那一刻
      英文源的 `title` 还是英文、译文可能还没写回；而写成 `title` 的又可能是中文。
      两个方向的字段都收进来，才不会因为"这次是哪一种"而漏判。
      （2026-10-10 真机实测：`Wang Yi meets with…` 就是英文形态漏的。）
    """
    out = []
    for k in ("title", "title_en", "trans_cn",
              "desc", "desc_en", "desc_cn", "summary"):
        v = it.get(k)
        if v:
            out.append(str(v))
    return out


def _looks_domestic_news_item(it):
    """标题+摘要合起来判"主体是不是中国"（要闻口径）。

    取舍（为什么这样设计，不是拍脑袋）：
      · **标题优先**：标题能判出来（True 或 False）就以标题为准 —— 摘要常常只是正文开头，
        里面出现"中国"未必是主体（「……对中国的影响」「……与中国的贸易」），
        一律拿摘要判会把大量真国际稿误杀；
      · **只在标题三态为 None 时才拿摘要兜底**：这正是厄尔尼诺那条的情形，
        标题中性、摘要明写主体；
      · 摘要兜底用**同一套主体判据**（`_news_subject_verdict`），同样享受
        客体/表态豁免 —— 所以「Botswana's president to pay state visit to China」
        这种"外国主体 + 到中国"的稿子，摘要里出现 China 也不会被误伤。
    """
    texts = _item_texts(it)
    if not texts:
        return False
    v = _news_subject_verdict(texts[0])      # title 永远排第一
    if v is True:
        return True
    # 【2026-10-15】标题级的「中国标识」（中国地名/人民币/标题开头是中国）→ 直接判国内。
    # 与 `_looks_domestic_news` 同一口径；**只在标题上做**，摘要不做（见 _has_cn_marker_title 注释）。
    if _has_cn_marker_title(texts[0]):
        return True
    if v is not None:
        return v
    for t in texts[1:]:                      # 摘要兜底
        if _news_subject_verdict(t) is True:
            return True
    return False


# ⑤ 科技栏的**英文外国品牌**表（代码常量，与 `_ENT_FOREIGN_EXTRA` 同一个理由）。
# 为什么必需：过滤跑在 `_collect` 出口，CGTN 科技源的标题/摘要**还是英文**，
# 而 `data/keywords.txt` 的 `[foreign_tech]` 里只有中文品牌名（谷歌/苹果/英伟达…）
# → 英文稿一条品牌都认不出来。真机实测踩到：摘要兜底上线后，
# 「Apple unveils new iPhone…（摘要：will ship to markets including China）」
# 因为认不出 Apple 是外国品牌、摘要里又出现 China，被误判成国内。
# 真机数据里的标定样本：`Trump administration is suspending Microsoft from a
# green card program`（要保留）、`NASA's SpaceX Crew-12 mission…`（要保留）。
_FOREIGN_TECH_EN = frozenset({
    "Apple", "Google", "Microsoft", "Nvidia", "Intel", "AMD", "Qualcomm", "Samsung",
    "Sony", "Nintendo", "Meta", "Amazon", "Tesla", "SpaceX", "NASA", "OpenAI",
    "Anthropic", "ChatGPT", "Claude", "Gemini", "Copilot", "Netflix", "Disney",
    "IBM", "Oracle", "TSMC", "ASML", "ARM", "Boeing", "Airbus", "Toyota",
    "Volkswagen", "BMW", "Mercedes", "Porsche", "Hyundai", "Nokia", "Ericsson",
    "Windows", "iOS", "macOS", "Android", "PlayStation", "Xbox",
})


def _tech_scope_en(title):
    """英文标题的品牌归属（只用于国际栏过滤，不改 `_tech_scope` 的既有行为）。"""
    t = title or ""
    if any(k in t for k in CN_TECH_BRANDS):        # 中文品牌名（华为/小米…）
        return "cn"
    if any(k in t for k in FOREIGN_TECH_BRANDS):   # 中文外国品牌名（谷歌/苹果…）
        return "intl"
    if any(re.search(r"\b%s\b" % re.escape(k), t) for k in _FOREIGN_TECH_EN):
        return "intl"
    return None


def _is_domestic_for_intl(it):
    """直连国际源的条目里，这条是不是"国内内容"（是就不进国际栏）。

    只用在国际栏（`region == "intl"`）；**国内板块（region == "cn"）一律不受影响**。

    ⚠ 2026-10-11 四轮起：**直连国际频道（channel 以 `intl-` 开头）回到下面这些逐栏判据**。
      原因：CGTN 是**中国的国际台**，科技/文化/商业分区大量报中国题材，一律放行就是
      「国际栏鱼龙混杂」（用户 2026-10-11 截图）。只有 `cn-intl-news`（国内媒体转来的、
      已在 `_split_cn_scope` 判过一遍的）免二次判定，避免两套分类器互相打架。

    五栏各有口径，热榜不动（它由 `_split_hot_by_scope` 在别处判）：
      · 娱乐：必须有外国具体对象（`ENT_FOREIGN_MARKERS` + `_ENT_FOREIGN_EXTRA`），
        且不得命中 `DOMESTIC_MARKERS` —— 与国内分流那条路同一方向；
      · 体育：体育专用的外国信号（`_looks_sport_foreign`）；
      · 要闻：主体是不是中国（`_looks_domestic_news`，含访华/中美这类豁免）；
      · 科技：先按**品牌归属**判（复用 `_tech_scope`：华为/小米/比亚迪…是中国品牌 → 国内；
        谷歌/苹果/英伟达…是外国品牌 → 国际），品牌都判不出来才退回 `DOMESTIC_MARKERS`。
        为什么用品牌表：「小鹏上线 Robotaxi 小程序」这类稿标题里没有"中国"两个字，
        只有 `[cn_tech]` 品牌表能认出来。
        这里**不**做"必须有外国对象"的反向要求 —— 那会把「SpaceX Crew-12 返回地球」
        这类没有中国词的真国际科技稿误杀，也会让整个科技栏塌掉。
      · 财经：命中 `DOMESTIC_MARKERS` 即为国内（标题里没有强国内特征词的就不动）。
    """
    cls = (it.get("cls") or "").strip()
    ti = it.get("title") or ""
    # 【2026-10-11 四轮·用户反馈「国际栏还是鱼龙混杂」】这道闸原来把**所有**直连国际频道
    # （channel 以 intl- 开头）整段放行，只挡一张 14 词小表（省委/市委/春晚/中超…）。
    # 后果：CGTN 是**中国的国际台**，它的科技/文化/商业分区里大量中国题材稿
    # （「中国的北极考察带回了气候模型的数据」「IAC 2026：中国航天公司的目标是扩大全球
    #   市场份额」「彭丽媛敦促发展女性数字化教育」「中国 2026 年国庆假期票房突破11.6亿元」
    #   「音乐、舞蹈和五颜六色的服装…」）全部留在国际栏 —— 就是用户看到的"鱼龙混杂"。
    # 现在只对 `cn-intl-news`（国内媒体转来的国际新闻，**已经过 `_split_cn_scope` 判过一遍**）
    # 免二次判定，避免两套分类器互相打架；直连 intl-* 源回到下面**逐栏判据**，由每个栏目
    # 对应的主体判据把关（娱乐要求"有外国具体对象" / 科技看品牌归属 / 要闻看中国主体）。
    _ch = (it.get("channel") or "").strip()
    # 体育栏**不走**这道闸：它有自己那套「赛事/主体」专用判据（见下面 `_INTL_SPORT_*`），
    # 是 2026-10-10 专门按「体育堵不住漏洞」修的，且源解析阶段已经先滤过一遍
    # （真机日志里的"滤除非国际赛事"）。保持原判据，不动它。
    if cls != "体育" and _ch == "cn-intl-news":
        return any(k in ti for k in INTL_CHANNEL_LOCAL_ONLY)
    if cls == "娱乐":
        # 【2026-10-11 七轮·境外娱乐前置层】Variety / Consequence 这两家是**境外娱乐媒体**，
        # 内容 100% 是国际文娱，不该走下面那套「要正面证明自己是国际」的判据 ——
        # 那套是给 CGTN 这种境内源防中国题材用的，套到境外源上会把
        # 「Shane Gillis Drops F-Bomb on SNL」「Barry Manilow's Vegas Comeback」这类
        # 纯欧美娱乐稿全判成国内（实测 10 条里误杀 6 条），等于接了白接。
        # 所以这里只做两件事：① 主权红线（`_INTL_ENT_BLOCK_RX`）命中即拦；
        # ② 其余一律放行。关掉总开关 INTL_ENT_OVERSEAS 后本层整体失效。
        if INTL_ENT_OVERSEAS and (it.get("channel") or "") in INTL_ENT_OVERSEAS_CHANNELS:
            return bool(_INTL_ENT_BLOCK_RX.search(ti))
        if not ti or _looks_domestic(ti):
            return True
        # 娱乐栏的语义是**必须有外国具体对象**，所以：
        #   · 先看"外国具体对象"（中英文都在 `_ENT_FOREIGN_ALL` 里）；
        #   · 再看"主体是不是中国"（标题 + 摘要，中英文都认）—— 命中即拦；
        #   · 两者都不命中 → **默认拦**（宁可少一条，也不让国内内容混进来）。
        # ⚠ 两个坑都踩过（真机实测）：
        #   ① 摘要里也可能暴露主体：「Symposium held to study Xi's works on culture」
        #      英文标题看不出来，摘要/译文里是"习近平"；
        #   ② 别写成 `_looks_domestic_news_item(it)` —— 那个函数"判不出来"时返回 False，
        #      拿来当"要不要拦"会让「音乐、舞蹈和五颜六色的服装…」这类没有外国对象的
        #      稿子全部漏进国际娱乐。这里必须是"没外国对象就拦"。
        _txt = ti + " " + " ".join(_item_texts(it)[1:])
        _cn_abroad = False      # 「主体是中国、但事件发生在国外」
        if _news_subject_verdict(_txt) is True:
            # 主体是中国 —— 但如果**标题里有明确的国外地点**，那事件发生在国外，
            # 是国际文化交流（中国电影周在北马其顿 / 中国高定亮相巴黎时装周），放行。
            # 开关见 INTL_ENT_CN_ABROAD_PASS：改 False 就回到"凡中国主体一律拦"。
            if INTL_ENT_CN_ABROAD_PASS and _ENT_INTL_SIGNAL_RX.search(ti):
                _cn_abroad = True
            else:
                return True
        # 下面两张是中国题材黑名单；"中国主体 + 国外场域"这一路要**跳过**它们
        #（否则 "Chinese couture makes its Paris Fashion Week debut" 会被
        #  `_INTL_ENT_DOM_RX` 里的 "Chinese" 拦回去，等于开关白加）。
        if not _cn_abroad:
            if _ENT_CN_SUBJECT_RX.search(ti):
                return True
            # 【2026-10-11 六轮·补】中国地名/地标黑名单（中英文都认）。
            # 只要上面的主体判据**判不出来**就会漏 —— 真机抓到：
            #   「Spring/Summer 2027 Shanghai Fashion Week kicks off」（上海时装周）
            #   「The Drum and Bell Towers and the rhythms of Old Beijing」（北京钟鼓楼）
            # 这两条 `_news_subject_verdict` 都返回 None（判不出主体），只靠外国信号兜不住。
            # 这张表是 2026-10-09 就写好的国内题材黑名单（`_INTL_ENT_DOM_RX`），
            # 中英文地标/节日/朝代/机构都在里面，这里直接拿来当**拦截面**用。
            if _INTL_ENT_DOM_RX.search(ti):
                return True
        # 【2026-10-11 六轮】最后一步从「必须有外国具体对象」改成「**没查出中国主体就放行**」。
        # 为什么改：白名单法误杀太狠 —— 真机 CGTN 文化 17 条里被砍的
        #   「Canadian Anne Carson wins 2026 Nobel Prize in Literature」（加拿大作家获诺贝尔
        #   文学奖）、「Atlanta zoo to debut new pandas and habitat」（亚特兰大动物园迎新熊猫）
        #   都是**实打实的国际文化新闻**，标题里却一个"外国文娱对象词"都没有。
        #   拦国内靠的是**主体判据**（`_news_subject_verdict` 中英文都认），
        #   白名单只是它判不出来时的补刀，不该反过来压过主体判据。
        # 保险：如果标题是**中文**（说明是境内源转来的），仍要求有外国具体对象 ——
        #   中文娱乐标题里一个外国对象都没有，基本就是国内明星八卦
        #   （「闫妮自曝离婚后还去前夫家打掼蛋」这类绝不能进国际栏）。
        if re.search(r"[\u4e00-\u9fa5]", ti):
            # 中文稿：仍要求有外国文娱具体对象
            return not any(k in ti for k in _ENT_FOREIGN_ALL)
        # 英文稿：要正面证明自己是国际的（外国地名 / 国籍 / 国际奖项 / 文娱对象）。
        # 一条都不占 → 拦（"数字智能让城市治理更聪明"这类无中国词的中国稿就是这么挡掉的）。
        if any(k in ti for k in _ENT_FOREIGN_ALL):
            return False
        return not _ENT_INTL_SIGNAL_RX.search(ti)
    if cls == "体育":
        # 【2026-10-15】体育原来只走"有没有体育专用的外国信号"，于是
        # 「亚冠精英联赛：上海海港主场战平大田韩亚市民」（中国球队的比赛）留在了国际体育栏。
        # 用户口径：中国主体一律不进国际栏 —— 所以先看中国标识/中国主体，再退回体育专用判据。
        if _has_cn_marker_title(ti) or _looks_domestic_news_item(it):
            return True
        return not _looks_sport_foreign(ti)
    if cls == "要闻":
        return _looks_domestic_news_item(it)
    if cls == "科技":
        if not ti:
            return True
        _ts = _tech_scope(ti)          # 先品牌归属（中国品牌优先）
        if _ts == "cn":
            return True
        if _ts == "intl":
            # 外国品牌 → 明确国际，**不再看摘要**（否则「苹果发布会」摘要提到中国就被误杀）
            return False
        # 中文品牌表没认出来 → 再试**英文品牌表**（CGTN 科技源的标题/摘要是英文，
        # 关键词表里只有中文品牌名，认不出来就会被下面的摘要兜底误伤）
        _ts2 = _tech_scope_en(ti)
        if _ts2 == "cn":
            return True
        if _ts2 == "intl":
            return False
        # 品牌都判不出来 → 退回"主体是不是中国"（标题优先、摘要兜底）。
        # ⚠ 这里**必须**用中英文都认的判据，不能只用 `_looks_domestic`
        #   —— 后者只认中文词，而 CGTN 科技源的标题在 `_collect` 出口**还是英文原题**。
        #   2026-10-10 真机验收失败就是这个原因：科技栏 4 条英文国内稿（China issues
        #   guidelines / China-Laos power line / China's Arctic expedition /
        #   IAC 2026: Chinese space companies）一条都没拦住。
        return _looks_domestic_news_item(it) or _looks_domestic(ti)
    if cls == "财经":
        if not ti:
            return True
        # 同理：财经栏也必须中英文都认，且摘要兜底
        return _looks_domestic_news_item(it) or _looks_domestic(ti)
    if cls == "热榜":
        # 【2026-10-10 上级点名】热榜原来一律不动，但真机实测它也有国内内容：
        #   「《人民日报》真把诺奖得主的国籍给"咔嚓"了？」「中国科协之声：诺贝尔奖值得尊重…」
        # 这类是**中文源**（凤凰/红星/澎湃转的国内媒体稿），所以用中文能认的同一套判据。
        # 实测这一条**只剔掉那 1~2 条国内稿**，其余 12 条真国际（中欧"各退一步"、
        # 特朗普与普京、白宫新闻秘书、秘鲁市长选举…）一条不少。
        return _looks_domestic_news_item(it)
    # 其余栏目（理论上不会有）：也用同一套兜底，宁严不漏
    return _looks_domestic_news_item(it)


def _split_domestic_items(items, region):
    """把国际栏里被判为"国内内容"的直连源条目剔掉，返回 (保留, 剔除) 两拨。

    只在 `region == "intl"` 时动手 —— 国内板块的条目一律原样返回（不误伤）。
    剔掉的条目**直接丢弃**：用户口径里国内板块没有"体育"栏（已下线），娱乐也不重复收，
    硬塞回去反而会造出多余的栏目。
    """
    if region != "intl":
        return list(items or []), []
    keep, drop = [], []
    for it in (items or []):
        (drop if _is_domestic_for_intl(it) else keep).append(it)
    return keep, drop


def _fix_finance_cls(items, region=""):
    """把"内容明显是财经、栏目却挂在头条/科技/娱乐…"的条目改判到「财经」。

    就地修改 items（dict 是共享引用，PRELOAD / PRELOAD_ALL 会同时看到修正后的结果），
    返回改判条数；日志里逐条打印「原栏目 → 财经 + 标题」，方便发版前自检核对。
    """
    fixed = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        cls = (it.get("cls") or "").strip()
        if cls not in FINANCE_FIXABLE_CLS:
            continue
        hit = _finance_title_hit(it.get("title"))
        if not hit:
            continue
        it["cls"] = "财经"
        it["cls_fixed_from"] = cls          # 留痕：前端/自检都能看出这条被改判过
        fixed.append((cls, hit, it.get("title") or ""))
    if fixed:
        print("[ok] %s: 财经错栏纠正 %d 条（%s）"
              % (region or "-", len(fixed),
                 "、".join("%s→财经(%s)" % (c, h) for c, h, _ in fixed[:6])))
        for c, h, t in fixed:
            print("     · [%s→财经]（命中「%s」）%s" % (c, h, t[:56]))
    return len(fixed)


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
    # "articleSection":"国际" / "军事" / "台湾"…，用来判定国内/国际比关键词黑名单可靠得多
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
        # JSON-LD 里的 articleSection（"国际"/"军事"/"台湾"…），用来判定国内/国际。
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
    # 【2026-10-10】国际栏的首屏（快速版）同样要剔掉直连源里的国内内容 ——
    # 否则预热阶段先把国内娱乐/体育发到页面上，几十秒后完整 _collect 才抹掉，
    # 用户会看到"闪一下国内新闻又没了"。判据与完整版同一个函数。
    items, _ = _split_domestic_items(items, region)
    if region != "cn":
        # 【2026-10-11 五轮】与完整版 `_collect` 对齐：首屏也要接收**国内板块转来的国际新闻**。
        # 原来这里不并，启动后头几十秒国际栏是没有 cn-intl-news 的。
        _spill = _spill_from_cn()
        if _spill:
            _seen_t = {it["title"] for it in items}
            _add = [s for s in _spill if s["title"] not in _seen_t]
            if _add:
                items.extend(_add)
                items = _drop_shared_images(items)
    if region == "cn":
        # 【2026-10-11 五轮 · 本轮主修】首屏（快速版）原来**不做「国内→国际」分流** ——
        # `_split_cn_scope` 只写在完整 `_collect` 里。后果：**每次启动后头几十秒**，
        # 国内栏会把国际新闻原样播出（用户截图里的「从选票到权力：德法意极右翼政党的
        # 不同制度路径」「人物｜白宫新任发言人」「泽连斯基」「英国一男子闯入动物园虎区」
        # 就是这一版，日志里的 `快速就绪 cn: 168 条 | 头条16条`），等完整 `_collect`
        # 跑完（约 20~50 秒）才抹掉。用户重启后立刻打开页面，看到的就是它。
        # 现在：先把**全量**登记进搜索池 / 国际栏转入池（与完整版一致），再按同一套
        # 判据把国际新闻真移走 —— 首屏第一帧就是干净的国内稿。
        PRELOAD_ALL["cn"] = list(items)
        _note_item_hosts(items)   # 图片域名按全量登记（含即将转去国际的那批）
        # 【2026-10-25 归档】记**分流前的全量**（与搜索池同一份），四个采集出口口径一致
        _hist_record("cn", items)
        items, _ = _split_cn_scope(items)
    else:
        _note_item_hosts(items)   # 【2026-10-09 修 高-1】发布前登记图片域名（/img 白名单）
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
        # 【2026-10-28 修】热榜卡（hotlistHTML）不渲染图片，只画排名/标题/来源；
        # 所以无图平台（如红星新闻，站内推荐流本就无图）也应保留，而不是被「没图不要」整条剔除。
        # 否则只剩有图的凤凰一家，多平台聚合名存实亡。普通新闻卡仍需图，那一路不在此豁免。
        keep = list(hot_items)
        dom, _intl = _split_hot_by_scope(keep)
        _HOT_INTL_SPILL[:] = _intl
        print(f"[ok] hot: 时间筛后 {len(fresh_hot)} 条，补图带图 {len(keep)} 条；"
              f"按站方栏目分流 国内 {len(dom)} / 国际 {len(_intl)}")
        PRELOAD_ALL["hot"] = list(dom)
        _note_item_hosts(dom)     # 【2026-10-09 修 高-1】发布前登记图片域名（/img 白名单）
        _hist_record("hot", dom)  # 【2026-10-25 归档】见 _hist_record
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
        # 【2026-10-07】接收国内板块剔出来的国际新闻（国内「头条」里的国际内容）。
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
            # 【2026-10-09 换源】名额 80 → 96：新增的中新网·财经这类不带图的源
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
        # 合并实时热榜（澎湃/红星/凤凰）：热榜置顶块（hotlistHTML）不渲染图片，
        # 只画排名/标题/来源，所以无图平台（如红星新闻）也应并入，避免只剩有图的凤凰一家。
        # 普通头条卡需要图，故「并入头条」这一路仍只取有图，不破坏「没图不要」。
        try:
            _hot_all = PRELOAD.get("hot") or _collect("hot")
            seen_t = {it["title"] for it in cn_only}
            # 置顶热榜：热榜卡不渲染图片，无图平台也保留（豁免「没图不要」）
            # 【2026-10-28 修 热榜退回单源】原来这里还套了 `h["title"] not in seen_t`，
            # 拿 cn_only 的**全部标题**去卡热榜 —— 有 RSS 的平台（澎湃）热榜条目与自家
            # RSS 同标题，被整批剔掉，最后只剩「没有 RSS 源」的凤凰一家。
            # 实测证据：`hot: 时效收口 26 条 | 热榜26条/2家` → `cn: 合并实时热点 11 条`
            # → `cn: 时效收口 ... 热榜11条/1家`，澎湃 15 条全折在这一步。
            # 热榜是**独立榜单块**（前端 hotlistHTML 单独渲染），与新闻流不是一个区域，
            # 不该拿列表标题去卡它。改为**只在热榜内部按标题去重**（同一条不重复进榜）。
            _seen_hot, hot_top = set(), []
            for h in _hot_all:
                if not (h.get("image") or _is_hot_board(h)):
                    continue
                if h["title"] in _seen_hot:
                    continue
                _seen_hot.add(h["title"])
                hot_top.append(h)
            # 并入头条：只取有图（头条卡需图），并避开列表已有的（头条仍不许重复）
            hot_headline = [h for h in _hot_all
                            if h.get("image") and h["title"] not in seen_t]
            if hot_top:
                print(f"[ok] {region}: 合并实时热点（置顶）{len(hot_top)} 条")
            # 去「割裂」：把 红星/凤凰 中**有图**的实时热点也并入「头条」栏目（与热榜共享同一批内容），
            # 否则「头条」只有界面+澎湃两家，凤凰被锁死在热榜、永不进头条。
            # 只并入有图的（头条卡需图），各取前 10 条，避免淹没头条里界面/澎湃的常驻源。
            for lbl in ("红星新闻", "凤凰网"):
                cnt = 0
                for h in hot_headline:
                    if h.get("label") == lbl:
                        d = dict(h)
                        d["cls"] = "头条"
                        cn_only.append(d)
                        cnt += 1
                        if cnt >= 10:
                            break
        except Exception as ex:
            hot_top = []
            print(f"[warn] 合并实时热点失败: {type(ex).__name__}")
        # 注意：hot 不能再按 rank 重排——_parse_hot 里已按「平台内归一化 score」做过跨平台混合排序，
        # 直接沿用它的顺序即可（rank 是平台内名次，跨平台无可比性）。
        cn_only.sort(key=lambda x: x["published"], reverse=True)
        _merged_all = list(hot_top) + cn_only
        # 【2026-10-09 用户点名「财经类新闻一定要区别化类，不能错放栏目」】
        # **放在 PRELOAD_ALL 之前**：搜索池与列表用同一份已纠正的分类，不会出现
        # 「列表里是财经、搜出来变头条」这种自相矛盾。见 _fix_finance_cls 的三处一致性说明。
        _fix_finance_cls(_merged_all, region)
        PRELOAD_ALL[region] = _merged_all
        _hist_record(region, _merged_all)   # 【2026-10-25 归档】国内：热榜+RSS 的完整池子
        # 用户要求：国内「头条」里的国际新闻全部移到国际板块
        _dom, _intl = _split_cn_scope(_merged_all)
        if _intl:
            print(f"[ok] {region}: 头条里剔出 {len(_intl)} 条国际新闻 → 转给国际板块")
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
        # 【2026-10-28 修】国际热榜置顶块（hotlistHTML）不渲染图片，无图平台也应保留，
        # 避免只剩有图的源、多平台聚合退化。国际热榜条目只进置顶块（cls=热榜），不进普通要闻卡。
        hot = [h for h in hot if h.get("image") or _is_hot_board(h)]
        seen_t = {it["title"] for it in items}
        hot = [h for h in hot if h["title"] not in seen_t]
        # 【2026-10-10 修 真机验收失败】**热榜这条链也必须过国别判定**。
        # 原来只对 `items` 过滤，而国际热榜是**另一个列表**（hot），直接 hot + items 返回 ——
        # 于是「《人民日报》真把诺奖得主的国籍给"咔嚓"了？」「中国科协之声：诺贝尔奖值得尊重…」
        # 这两条一直留在国际热榜上（上级真机验收点名了它们）。
        # 放在这里（补图之后、与 items 合并之前）而不是和 items 一起判：
        # 热榜条目没有 cls="要闻" 之类的语义，就地用 `_looks_domestic_news_item`
        # 判"主体是不是中国"（标题优先、摘要兜底 —— 与六栏同一套判据）。
        if region == "intl":
            _hot_keep = [h for h in hot if not _looks_domestic_news_item(h)]
            if len(_hot_keep) != len(hot):
                print(f"[ok] {region}: 热榜剔除中国主体的 {len(hot) - len(_hot_keep)} 条：")
                for _h in hot:
                    if _looks_domestic_news_item(_h):
                        print(f"     · [{_h.get('label') or '?'}] "
                              f"{(_h.get('title') or '')[:56]}")
            hot = _hot_keep
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
        # 【2026-10-10 用户实测报障「国际的娱乐/体育堵不住漏洞」】
        # 直连国际源（CGTN 文化 / 新华网·体育…）的条目原来一个国别过滤器都不过。
        # 在这里补上（娱乐/体育两栏，判据见 _is_domestic_for_intl）—— 放在这一处是因为
        # 它是**国际栏所有出口的必经点**：国内板块转来的（_spill_from_cn）与热榜（hot）
        # 在更前面就已经并进 items / hot 了，不会漏过；而它们本身已经过同一套判定，
        # 再判一次是幂等的（娱乐那路对 `cls == "娱乐"` 本来就不转国际）。
        items, _dom_items = _split_domestic_items(items, region)
        if _dom_items:
            print(f"[ok] {region}: 剔除直连源里的国内内容 {len(_dom_items)} 条"
                  f"（娱乐/体育/要闻/科技/财经，判据见 _is_domestic_for_intl）：")
            for _d in _dom_items:
                print(f"     · [{_d.get('cls')}·{_d.get('channel')}] {(_d.get('title') or '')[:56]}")
    _fix_finance_cls(hot + items, region)
    PRELOAD_ALL[region] = hot + items
    _hist_record(region, hot + items)       # 【2026-10-25 归档】国际：热榜+RSS 的完整池子
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
            # 【2026-10-28 修 热榜退回单源】最后一关就在下面这个 lambda：
            # 它要求「必须有图且达标」，无图一律丢。而热榜卡（前端 hotlistHTML）
            # 只画排名/标题/来源、**不渲染图片**，有图没图对展示毫无影响。
            # 澎湃热榜多数不带图 → 整批被这一关剔掉，页面上热榜就只剩有图的凤凰一家。
            # （日志铁证：`_collect_exclusive cn 赋值后 热榜 25 条/2家 {凤凰11,澎湃14}`
            #   → `[ok] cn: 后台剔除低质图 39 条` → `/api/news` 只剩热榜 9 条/1家）
            # 与 mefcl 同一条规矩：**这个板块不渲染图就豁免图要求**，是热榜条目就留。
            keep = list(ex.map(
                lambda it: (_img_ok(it["image"]) if it.get("image") else False)
                or (it.get("channel") in MIN_IMG_EXEMPT_CHANNELS and bool(it.get("image")))
                or _is_hot_board(it),
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


def _open_app_window(url=None):
    """把程序页面打开（托盘菜单、气泡点击都用它）。

    url 传了就开那一页（气泡正播到哪条，点「打开」就开那条的原文），
    不传 / 传空回落到首页 APP_URL。
    优先 ShellExecuteW —— Windows 上打开 URL 最可靠；失败退回 webbrowser。
    """
    url = url or APP_URL or "http://127.0.0.1:8000"
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


def _open_app_later(url=None):
    """把 _open_app_window 扔到后台线程去做 —— 专供 Win32 消息回调里用。

    为什么必须这样：ShellExecuteW 是同步的，它要等外壳把浏览器拉起来才返回，
    浏览器冷启动、默认浏览器异常、或本机此刻正叠着代理抖动，就是好几秒。
    这几秒发生在托盘/气泡自己的消息线程里，那条线程就没在 GetMessage 循环里等着，
    Windows 判定"这个窗口点下去没响应"，于是给鼠标挂上转圈（2026-10-28 用户反馈
    「点退出鼠标一直转」的直接原因之一）。消息回调只做「收起窗口」这种本地动作。
    """
    threading.Thread(target=_open_app_window, args=(url,), daemon=True).start()


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
    _balloon_shown_flush()      # 常看常新 ③：os._exit 不走 atexit，名单得在退出前自己写盘
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
            # 2026-10-28 加「轮播间隔」子菜单时补的原型声明。这几个原来一个都没声明，
            # 全靠 ctypes 默认按 32 位 int 传：菜单句柄今天恰好没超出 32 位所以没出事，
            # 但 AppendMenuW 的第三个参数在 MF_POPUP 下装的是**子菜单句柄**，
            # 一旦被截成 32 位，弹出来的就是一张空白菜单 —— 这种坑要提前堵。
            u.CreatePopupMenu.argtypes = []
            u.CreatePopupMenu.restype = wintypes.HMENU
            # 注意：ctypes.wintypes 里**没有** UINT_PTR（3.13 也没有），写它会 AttributeError
            # 把整个托盘初始化打断、通知和右键菜单一起静默失效。指针宽度自己搭。
            _UINT_PTR = ctypes.c_size_t
            u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                      _UINT_PTR, wintypes.LPCWSTR]
            u.AppendMenuW.restype = wintypes.BOOL
            u.CheckMenuItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT,
                                        wintypes.UINT]
            u.CheckMenuItem.restype = wintypes.BOOL
            u.GetMenuStringW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR,
                                         ctypes.c_int, wintypes.UINT]
            u.GetMenuStringW.restype = ctypes.c_int
            u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                         wintypes.HWND]
            u.TrackPopupMenu.restype = ctypes.c_int
            u.DestroyMenu.argtypes = [wintypes.HMENU]
            u.DestroyMenu.restype = wintypes.BOOL
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
                _open_app_later()
            elif _low == 0x0203:
                # 【2026-10-28 用户选的入口】双击托盘图标 = 立即轮播一轮。
                # 只认 WM_LBUTTONDBLCLK(0x0203)：单击 0x0202 一直是什么都不做，
                # 保持原样，免得把"点一下图标就跳出一屏新闻"变成误触。
                # _balloon_play_now 自己会另起线程，消息线程不在这儿干活（鼠标不转）。
                _balloon_play_now("双击托盘")
            elif _low == 0x0205:
                # 【2026-10-07 用户要求「右下角图标右键弹出退出和打开软件的菜单」】
                # WM_RBUTTONUP：在托盘图标上点右键 → 弹出菜单
                self._popup_menu(hwnd)
        if msg == 0x2:  # WM_DESTROY
            user32.PostQuitMessage(0)
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _build_menu(self):
        """搭托盘右键菜单，返回 (父菜单句柄, {命令号: 文字})。

        单独拆出来是为了能验：不真把菜单弹到屏幕上，也能用 GetMenuStringW 把标签读回来
        （本机脚本 E:\\Qdoer\\tmp\\ossbuild\\verify_tray_menu.py 就这么做的）。
        """
        u = self.user32
        cur_min = _toast_min_sec() // 60
        hmenu = u.CreatePopupMenu()
        items = {}
        u.AppendMenuW(hmenu, 0x00000000, _TRAY_CMD_OPEN, "打开热点新闻")
        u.AppendMenuW(hmenu, 0x00000000, _TRAY_CMD_PLAY, "立即轮播一轮")
        items[_TRAY_CMD_OPEN] = "打开热点新闻"
        items[_TRAY_CMD_PLAY] = "立即轮播一轮"
        u.AppendMenuW(hmenu, 0x00000800, 0, None)              # 分隔线
        # 「轮播间隔」做成子菜单：四档放主菜单里太长，而且这一组是互斥的一件事。
        hsub = u.CreatePopupMenu()
        for i, m in enumerate(TOAST_MIN_CHOICES):
            cid = _TRAY_CMD_TOAST_BASE + i
            lab = _tray_toast_label(m)
            u.AppendMenuW(hsub, 0x00000000 | 0x00000200, cid, lab)   # STRING|RADIOCHECK
            if m == cur_min:
                # CheckMenuItem(hMenu, uID, uCheckState, uFlags) —— 后两个别记反：
                # uCheckState 放 MF_CHECKED(0x8)，uFlags 放 MF_BYCOMMAND(0)。
                # 记反了 ctypes 不报错、菜单也不带勾，只能靠真机把 GetMenuState 读回来才看得出来。
                u.CheckMenuItem(hsub, cid, 0x00000008, 0x00000000)
            items[cid] = lab
        u.AppendMenuW(hmenu, 0x00000010, hsub, "轮播间隔（自动弹一组）")   # MF_POPUP
        # 【2026-10-28 用户：「都好看，不行就让用户可以自行选择吧」】
        # 播报样式也做成子菜单。选完立刻播一条给用户看 —— 样式这种东西，不看见就等于没选。
        hsty = u.CreatePopupMenu()
        cur_style = _balloon_style()
        for i, st in enumerate(BALLOON_STYLES):
            cid = _TRAY_CMD_STYLE_BASE + i
            lab = BALLOON_STYLE_LABEL.get(st, st)
            u.AppendMenuW(hsty, 0x00000000 | 0x00000200, cid, lab)
            if st == cur_style:
                u.CheckMenuItem(hsty, cid, 0x00000008, 0x00000000)
            items[cid] = lab
        u.AppendMenuW(hmenu, 0x00000010, hsty, "播报样式")
        # 【2026-10-30】「工作时不打扰」做成一个可勾的条目：这一条闸门以前只有代码知道，
        # 用户看到的现象是「气泡一整天不响」。给它一个看得见、点得动的地方，就在样式下面。
        _hold_on = bool(_notify_cfg().get("hold_on", True))
        u.AppendMenuW(hmenu, 0x00000000 | (0x00000008 if _hold_on else 0),
                      _TRAY_CMD_HOLD, "工作时不打扰（锁屏 / 投影演示时让路）")
        items[_TRAY_CMD_HOLD] = "工作时不打扰（锁屏 / 投影演示时让路）"
        u.AppendMenuW(hmenu, 0x00000800, 0, None)
        u.AppendMenuW(hmenu, 0x00000000, _TRAY_CMD_QUIT, "退出程序")
        items[_TRAY_CMD_QUIT] = "退出程序"
        # 子菜单挂进父菜单之后就归父菜单所有了：DestroyMenu(hmenu) 会连着它一起销毁，
        # 所以这里不需要（也不能）再单独 DestroyMenu(hsub)。
        self._hsub = hsub
        self._hsub_style = hsty
        return hmenu, items

    def _run_cmd(self, cmd):
        """菜单返回的命令号落到动作上。拆开是为了单测能直接喂数字，不用真弹菜单。"""
        if cmd == _TRAY_CMD_OPEN:
            print("[ok] 托盘菜单：打开热点新闻")
            _open_app_later()
        elif cmd == _TRAY_CMD_PLAY:
            print("[ok] 托盘菜单：立即轮播一轮")
            _balloon_play_now("托盘菜单")
        elif _TRAY_CMD_TOAST_BASE <= cmd < _TRAY_CMD_TOAST_BASE + len(TOAST_MIN_CHOICES):
            m = _toast_min_set(TOAST_MIN_CHOICES[cmd - _TRAY_CMD_TOAST_BASE])
            print("[ok] 托盘菜单：轮播间隔 = %d 分钟" % m)
            # 改完给一条气泡回执：这个设置没有别的界面能看见，不回执就等于没改成。
            _show_toast("轮播间隔已设为 %d 分钟" % m,
                        "自动弹一组的间隔；想现在就看一轮，右键托盘点「立即轮播一轮」。")
        elif _TRAY_CMD_STYLE_BASE <= cmd < _TRAY_CMD_STYLE_BASE + len(BALLOON_STYLES):
            st = BALLOON_STYLES[cmd - _TRAY_CMD_STYLE_BASE]
            _save_ui_cfg({"balloon": st})
            print("[ok] 托盘菜单：播报样式 = %s" % st)
            # 直接播一条新样式的：样式是给人看的，不看见就等于没选。
            # 这里另起线程（_balloon_play_now 自己会），菜单消息线程不干重活。
            _balloon_play_now("播报样式")
        elif cmd == _TRAY_CMD_HOLD:
            # 勾选态就是当前值，点一下取反 —— 和页面那个开关写的是同一个键（notify.hold_on）。
            new = not bool(_notify_cfg().get("hold_on", True))
            _save_notify_cfg({"hold_on": new})
            print("[ok] 托盘菜单：工作时不打扰 = %s" % ("开" if new else "关"))
            _show_toast("工作时不打扰已%s" % ("开启" if new else "关闭"),
                        ("锁屏、投影演示时自动让路；全屏看片/游戏照播。"
                         if new else "任何时候都照播，包括锁屏和投影演示。"))
        elif cmd == _TRAY_CMD_QUIT:
            print("[ok] 托盘菜单：退出程序")
            _quit_app_now()

    def _popup_menu(self, hwnd):
        """托盘图标右键菜单：打开热点新闻 / 立即轮播一轮 / 轮播间隔 / 退出程序。

        两个坑：
        1) 弹菜单前必须先 SetForegroundWindow，否则菜单点完不消失（Windows 的老规矩）；
        2) 用 TPM_RETURNCMD 让 TrackPopupMenu 直接返回选中的命令号，比等 WM_COMMAND 简单可靠
           —— 子菜单里的条目也一样返回它的命令号。
        """
        u = self.user32
        wintypes = self.wintypes
        try:
            hmenu, _items = self._build_menu()
            pt = wintypes.POINT()
            u.GetCursorPos(self.ctypes.byref(pt))   # ctypes 在这个类里是 self.ctypes
            u.SetForegroundWindow(hwnd)
            cmd = u.TrackPopupMenu(hmenu, 0x0002 | 0x0100,      # RIGHTALIGN | RETURNCMD
                                   pt.x, pt.y, 0, hwnd, None)
            u.PostMessageW(hwnd, 0, 0, 0)                       # 让菜单正常收起
            u.DestroyMenu(hmenu)
            self._run_cmd(cmd)
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
            # 【2026-10-28】把两个新入口写在鼠标悬停提示里 —— 这程序的用户不会去翻菜单，
            # 提示条是"双击能干什么"唯一的自解释处（szTip 上限 127 字，这里 24 字）。
            nid.szTip = "热点新闻｜双击=立即轮播一轮｜右键=菜单"
            nid.hIcon = self._load_icon()
            self.nid = nid
            self.shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid))
            print("[ok] 托盘就绪（图标已设；双击=立即轮播，右键=菜单）")
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
# 卡片尺寸见下面「气泡版式」那段（2026-10-28 从 400x118 改成 440x152，给按钮留出独立的一行）。
# 【2026-10-07 用户要求「三行不过瘾，显示六行，间隔延长两秒」】停留 5200 → 7200 毫秒。
# 【2026-10-09 用户要求「气泡推送新闻还是太少，增加 10 组新闻」】一组 3 条 → 6 条 → **10 条**。
#   停留时间重新定档 7200 → 5600 毫秒，依据：
#     · 用户此前明确偏好「更长」，原话「间隔延长两秒」，所以**不取 3 秒那种**；
#     · 10 条 × 7.2 秒 = 72 秒一组，实测（真机 125% 缩放、当时的 400x118 卡片）太长：
#       用户多半在播到第 4~5 条时就点开别的窗口了，后面的栏目等于没播；
#     · 需求书给的建议区间就是 5200~6000 毫秒（一组 52~60 秒）；
#     · 气泡标题一行约 22 个汉字、最多三行（见上面「气泡版式」），实测读完一条
#       （含标签行的栏目/来源）需要 4.5~5.5 秒，
#       5600 毫秒正好卡在「读得完」与「不拖沓」之间，一组 56 秒，比原来的 6 条 ×7.2=43 秒
#       只长 13 秒，却多播 4 条、多覆盖 3 个栏目。
BALLOON_PER_ITEM_MS = 5600   # 每条新闻停留多久（毫秒）；一组 10 条 ≈ 56 秒
BALLOON_PLAY_COUNT = 10      # 一条气泡里滚动播放几条（用户要求「增加 10 组新闻」）
# 【2026-10-28 用户反馈「点打开进了网页，轮播就停止了」】
# 点「打开」原本把整组一起收掉：第 3 条点开，剩下 7 条就没了 —— 一轮 10 条等于只看 3 条。
# 现在改成：开完这一条，气泡先让开屏幕（浏览器要看清楚），隔这么久再**接着播这一组剩下的**。
BALLOON_RESUME_MS = 8000
# 气泡里「头条」那一行的前缀：自绘气泡会据此把它画成红色标签（见 _wndproc 的 WM_PAINT）。
BALLOON_HEADLINE_TAG = "【头条】"

# ────────────── 气泡版式（2026-10-28 用户：「打开和退出两个按键已经遮挡文字，调整布局」）──────────────
# 老版式的病根就在两个矩形上：正文画到「底边-10px」，而胶囊按钮从「底边-36px」起 ——
# 两块必然重叠，鼠标一悬停，按钮就压在正文第二、三行上（用户截图里就是这个）。
# 现在把卡片分成三条**互不相交**的横带，并且用下面的常量把话说死：
#   ① 标签行（栏目 / 来源）→ ② 新闻标题（最多 3 行）→ ③ 底部操作行（分页点 + 按钮 + 倒计时条）
# 正文的**下边界 = 底部操作行的上边界**，写成一个常量算式，测试直接断言不相交（tests/test_balloon_layout.py）。
# 卡片比原来高 34 像素、宽 40 像素，仍然只占右下角一小块（125% 缩放下 550×190）。
# 【2026-10-28 第三轮改版】用户看过 440×152 与 480×176 图文卡后说「还是不好看」，
# 让试一种苹果风格。这版按「macOS 通知」重画：没有胶囊标签、没有描边按钮，
# 圆角 20、字重 600、App 行（小图标 + 热点新闻 + 栏目）+ 右侧 84×84 圆角题图 +
# 底部无边框文字按钮 + 贴底 2px 进度线。几何仍然只从 _balloon_rects 出。
BALLOON_W, BALLOON_H = 480, 176
BALLOON_PAD_X = 22
BALLOON_APP_TOP = 14      # App 行（小图标 + 应用名 + 栏目）的顶
BALLOON_APP_H = 20        # 图标边长，也是这一行的高度
# 【2026-10-29 用户：「这个字体换成，聚光灯式的字体，包括卡片式字体，自动的字体」】
# 标题带从此**各档共用一条**：整行 436 宽、两行 × 30。原来它是「右侧让出 84 给题图、
# 两行 × 24」，那是 16px 字的老节奏 —— 换成聚光灯那一档（21.6/700）后行高 24 会顶到
# 下一行、340 的列宽又装不下本机一半的标题（见 _fit_lines 的实测）。题图因此从右侧
# 挪进底部那一档（左边），标题永远占满整行。
BALLOON_TEXT_TOP = 44     # 标题起始 y
BALLOON_TEXT_LINES = 2
BALLOON_LINE_H = 30       # 聚光灯那一档的行高
BALLOON_TEXT_BOTTOM = BALLOON_TEXT_TOP + BALLOON_TEXT_LINES * BALLOON_LINE_H   # 104
BALLOON_META_TOP = 112    # 来源 / 名次那一行
BALLOON_META_H = 14
BALLOON_THUMB = 58        # 题图边长（正方形，居中裁切；原来 84 挤在标题右边）
BALLOON_THUMB_GAP = 16    # 题图和它右边那行小字/分页点之间的气隙
BALLOON_THUMB_RADIUS = 14
BALLOON_THUMB_TOP = BALLOON_H - 8 - BALLOON_THUMB    # 110 —— 底距 8，和操作行同一档
BALLOON_FOOT_TOP = BALLOON_H - 42        # 134 —— 底部操作行；正文**绝不**伸进这一行
BALLOON_FOOT_BOTTOM = BALLOON_H - 8      # 168
# 【2026-10-28 用户：「打开和关闭，位置向底部靠」】操作行再往下压：
# 按钮底边离卡片底只有 12px（125% 屏上 15 像素），不再"悬"在中间。
# 【2026-10-28 第五轮·WinUI3】用户把 WinUI3 样张转正：按钮不再是紧凑胶囊，
# 改成 Fluent 的矩形按钮 —— 等宽（两颗都 64）、高 27、圆角 4、左右各 16px 内边距。
BALLOON_BTN_H = 27                       # 按钮高度（Fluent 34 物理像素 ÷ 1.25）
BALLOON_BTN_W = 64                       # 两颗按钮等宽（Fluent 的次按钮/主按钮同尺寸）
BALLOON_BTN_OPEN_W = BALLOON_BTN_W
BALLOON_BTN_CLOSE_W = BALLOON_BTN_W
BALLOON_BTN_GAP = 12
BALLOON_BTN_RADIUS = 4                   # Fluent 按钮圆角（不是 h/2 的胶囊）
BALLOON_DOT_W, BALLOON_DOT_ON_W, BALLOON_DOT_GAP = 6, 16, 5   # 分页点
# 【2026-10-28 用户：「倒计时条不要，看着心慌」】底部那根进度线删了（连同 BALLOON_BAR_H /
# BALLOON_BAR_TOP / BALLOON_TICK_MS 和 200ms 的重画计时器）。留这条注释是防止以后"好心"加回来：
# 气泡是「随手扫一眼」的东西，不是让人盯着等条走完的进度条。
BALLOON_RADIUS = 8                       # 【第五轮】WinUI3 的 8px 圆角（苹果那版是 20）
BALLOON_CARD_ALPHA = 252                 # 卡面几乎不透明：238 时桌面上的黑图标会透出个影子
BALLOON_TITLE_PX = 16                    # 兜底版式（_paint_fallback）那一档：宁可小一号，不许挤掉字
BALLOON_HEAD_PX = 21.6                   # 标题字号：聚光灯那一档，2026-10-29 起各档共用
# 标题带宽 = 480-22-22 = 436 逻辑像素，21.6 的汉字一行约 20 个，两行 40 个字。
BALLOON_TITLE_W = BALLOON_W - 2 * BALLOON_PAD_X
BALLOON_TITLE_MAX = 40

# ────────────── 播报样式（2026-10-28 用户：「都好看，不行就让用户可以自行选择吧」）──────────────
# 用户看过四张「播报方式」样张（翻牌 / 电视新闻条 / 直播速递 / 聚光灯），
# 后来又说「方块式字体的轮播，效果不好，这的样式取消」—— 翻牌已删掉。
# 剩下的让他自己挑：样式进本机配置（ui.balloon），托盘右键菜单的「播报样式」能改，
# 选完立刻播一条，下一条气泡就换。入口只放在右下角托盘菜单里（用户指定的位置）。
#
# 【2026-10-29 用户：「播报样式太多，可以精简成三个样式，聚光灯必须留下，你自己衡量」】
# 五档砍到三档。砍掉的是「卡片」和「新闻条」，理由不是"少一个算一个"：
#   · 卡片(card) —— 2026-10-29 把标题字号统一成聚光灯那一档之后，它和速递就是**同一张版面**，
#     只差"有没有打字机和声波"。留在菜单里，用户看到的是两个几乎一样的名字，
#     选哪个都不算错 —— 这种选项本身就是"太多"的那一种。
#     版面代码**没删**：它退成内部兜底那一张（`_paint_bcast` 画崩时走 `_paint_fallback`，
#     版式解析抛异常时 self.style 落到 "card"，见 `_set_current`），只是不再让人选。
#   · 新闻条(tv) —— 唯一一档版面不同的（标题带只有 302 宽：右边让给题图、底边让给跑马灯），
#     标题字号因此只能自成一套；底带那条跑马灯反复和悬停的[关闭][打开]抢位置（实拍叠成一团过，
#     专门加过 `_band_x2` 让位逻辑）。三档共用一支标题字体这件事，它是那个例外 —— 删掉之后
#     "换样式=换字号"这种割裂感就没了。整档删除（`_paint_tv` + 只给它用的 `font_hl`）。
# 留这三档的衡量：聚光灯是用户点名必须留的；速递是"题图还没下载回来也不空手"的那一档
# （自动分流的落点，也是本机缓存 miss 时唯一不依赖图的版式）；自动留着是因为**他不看菜单**
# —— 默认值必须做到"不用选也对"，这正是用户自己定过的那条（别做需要用户先去找的功能）。
#   auto —— 有图用聚光灯、没图用速递（默认；不选也好看，不用用户先懂设置）
#   live —— 打字机 + 声波，没图时最稳
#   spot —— 整卡大图 + 压暗 + 光扫，视觉冲击最强
BALLOON_STYLES = ("auto", "live", "spot")
BALLOON_STYLE_LABEL = {"auto": "自动", "live": "速递", "spot": "聚光灯"}
# 老取值归位表：本机配置（config.local.json 的 ui.balloon）里可能还写着已经下线的档 ——
# 用户以前手选过，升级后不该"偷偷改主意"。按最接近的活档归位，而不是一律回落 auto：
# 选过卡片的人本来就爱"干净的文字卡"（速递就是它的动画版），选过新闻条的人爱"带图的大标题"
# （交给自动分流：图到位给聚光灯，没到位给速递）。
BALLOON_STYLE_LEGACY = {"card": "live", "tv": "auto"}
BALLOON_BCAST_RADIUS = 20                # 播报版式共用的圆角（480×176 上 20 才像"通知"）
BALLOON_ANIM_MS = 6000                   # 一轮进场动画的时长（和样张一致）
BALLOON_ANIM_TICK_MS = 40                # 25fps：小画布重绘很轻，够顺


def _balloon_style(raw=None):
    """把用户选的播报样式收敛到合法值（空值/脏值一律 auto）。

    已经下线的老取值不算脏值 —— 先过一遍 `BALLOON_STYLE_LEGACY` 归到最近的活档，
    剩下的（拼错的、别的程序写进来的、将来又删掉的）才回落 auto。
    """
    try:
        v = str(raw if raw is not None else (_ui_cfg().get("balloon") or "auto"))
    except Exception:
        v = "auto"
    v = v.strip().lower()
    v = BALLOON_STYLE_LEGACY.get(v, v)
    return v if v in BALLOON_STYLES else "auto"


def _balloon_pick_style(raw=None, has_img=False):
    """这一条实际用哪个版式。

    「自动」按**题图真拿得到**分流（调用处传的是一句 `_balloon_img_local(url)` 命中，
    不是「这条有没有图 URL」—— 实测境内这些站每条都带图 URL，按 URL 判等于永远判成有图，
    分流就形同虚设）：图到本机的给大图聚光灯、还在路上的给打字机速递，
    不会出现一张什么都没有的大黑卡。

    **用户点名选的那一档不退**（2026-10-28 用户：「图片样式，会出现没有图片的样式」）。
    以前 spot 没图就偷偷换成 live，而题图是边播边后台补下的：池子 200 条 100% 有图 URL，
    开播那一刻本机只命中 95/200 = 48% —— 所以那一退几乎天天在退，一组里聚光灯 / 速递
    来回跳，看着就是"样式坏了"。现在 spot 无图由 `_paint_spot_backdrop` 画一张程序自己的底
    （深蓝渐变 + 主色光洗 + 一道慢慢走的光柱）：版式、字号、圆点、按钮位置一概不动，
    图到了下一帧自然盖上。速递那一档本来就没有"没图怎么办"的问题，照原样返回；
    三档里只有 auto 会分流（2026-10-29 精简：card / tv 两档已下线，见 BALLOON_STYLES 上面那段）。
    """
    s = _balloon_style(raw)
    if s == "auto":
        return "spot" if has_img else "live"
    return s


def _balloon_radius_of(style):
    """卡片版是 WinUI3 的 8px；播报版式按样张是 20px。

    卡片那一档从菜单下来以后（2026-10-29 精简成三档），style 只会因为兜底路径等于 "card"
    （版式解析抛异常时），所以这条判据留着是给那一张用的，别再当成"第五种可选样式"。
    """
    return BALLOON_RADIUS if style == "card" else BALLOON_BCAST_RADIUS


def _balloon_ease(p):
    """三次缓出（CSS cubic-bezier(.2,.8,.2,1) 的近似），动画看着不"顿"。"""
    p = max(0.0, min(1.0, p))
    return 1.0 - (1.0 - p) ** 3


def _balloon_rects(w=None, h=None, total=10, idx=0, thumb=True):
    """算出气泡各条带的矩形（**逻辑像素**，绘制时再乘缩放比）。

    单独拎出来有两个好处：① WM_PAINT 和点击命中判断共用同一份几何，不会再出现
    "画在这儿、判定在那儿"；② 可以纯静态单测（tests/test_balloon_layout.py 里断言
    正文带和按钮带永不相交 —— 这次用户报的就是这个）。

    苹果通知版（2026-10-28 第三轮）→ 2026-10-29 起标题带各档共用（聚光灯那一档）：
      · app    —— 顶部一行：小图标 + 「热点新闻」+ 栏目，右端是时间
      · text   —— **整行宽**的两行大标题（21.6/700）
      · thumb  —— 底部左角 58×58 圆角题图（没图时 None，来源行/分页点跟着回到最左）
      · meta   —— 来源 · 名次（题图右边那一栏）
      · foot   —— 分页点在左，两个按钮在右（关闭 / 打开，主操作贴最右，和 WinUI3 一致）
    """
    w = w or BALLOON_W
    h = h or BALLOON_H
    x0 = BALLOON_PAD_X
    xr = w - BALLOON_PAD_X
    # 操作行贴着底边（2026-10-28 用户两次要求往下靠）：底距固定 8 逻辑像素起步，
    # 按钮在带内居中后底边离卡片底约 12px；小尺寸卡片也按同样的底距收。
    foot_top, foot_bottom = h - 42, h - 8
    btn_y1 = foot_top + max(0, (foot_bottom - foot_top - BALLOON_BTN_H) // 2)
    btn_y2 = btn_y1 + BALLOON_BTN_H
    # 【2026-10-28 第五轮】顺序按 WinUI3 样张：次按钮「关闭」在左、主按钮「打开」贴最右。
    # 之前把「关闭」放在最右，鼠标扫过去顺手一点就把这条新闻退了，不合适。
    openc = (xr - BALLOON_BTN_OPEN_W, btn_y1, xr, btn_y2)
    close = (openc[0] - BALLOON_BTN_GAP - BALLOON_BTN_CLOSE_W, btn_y1,
             openc[0] - BALLOON_BTN_GAP, btn_y2)
    total = max(1, int(total or 1))
    pager_w = (total - 1) * (BALLOON_DOT_W + BALLOON_DOT_GAP) + BALLOON_DOT_ON_W
    dot_y1 = foot_top + ((foot_bottom - foot_top) - BALLOON_DOT_W) // 2
    cur = max(0, min(int(idx or 0), total - 1))
    app = (x0, BALLOON_APP_TOP, xr, BALLOON_APP_TOP + BALLOON_APP_H)
    # 【2026-10-29 用户：「这个字体换成，聚光灯式的字体，包括卡片式字体，自动的字体」】
    # 标题带各档共用一条：整行宽、两行 × 30（聚光灯那一档）。题图从右侧挪进底部那一档的
    # 左角，来源行和分页点跟着让出题图占的那 58+16 宽 —— 谁都不许压到谁。
    # 有图/没图只是**底部左角有没有那张方图**，纵向节奏一样。
    thumb_bottom_gap = BALLOON_H - (BALLOON_THUMB_TOP + BALLOON_THUMB)     # 8
    if thumb:
        ty1 = max(BALLOON_TEXT_TOP, h - thumb_bottom_gap - BALLOON_THUMB)
        thumb_r = (x0, ty1, x0 + BALLOON_THUMB, ty1 + BALLOON_THUMB)
        side_x1 = x0 + BALLOON_THUMB + BALLOON_THUMB_GAP
    else:
        thumb_r = None
        side_x1 = x0
    text_x2 = xr
    meta_y = min(BALLOON_META_TOP, foot_top - BALLOON_META_H)
    text_bottom = min(BALLOON_TEXT_BOTTOM, meta_y - 2, foot_top)
    if thumb_r:
        text_bottom = min(text_bottom, thumb_r[1] - 2)
    text = (x0, BALLOON_TEXT_TOP, text_x2, text_bottom)
    meta = (side_x1, meta_y, text_x2, meta_y + BALLOON_META_H)
    return {
        "card": (0, 0, w, h),
        "app": app,
        "icon": (x0, BALLOON_APP_TOP, x0 + BALLOON_APP_H, BALLOON_APP_TOP + BALLOON_APP_H),
        "thumb": thumb_r,
        "text": text,
        "meta": meta,
        "foot": (x0, foot_top, xr, foot_bottom),
        "pager": (side_x1, dot_y1, side_x1 + pager_w, dot_y1 + BALLOON_DOT_W),
        "btn_open": openc,
        "btn_close": close,
        "dots": {"n": total, "idx": cur,
                 "w": BALLOON_DOT_W, "on": BALLOON_DOT_ON_W, "gap": BALLOON_DOT_GAP},
    }


# ────────────── 气泡配色：抄控制台的设计令牌（2026-10-28 用户：「这个样式应该和控制台的颜色同步」）──────────────
# 原来气泡自带一套颜色（#2B6CB0 蓝、#E53E3E 红、#DD6B20 橙…），和控制台是**两套色**，
# 同一篇新闻在页面上是琥珀色标签、到气泡里变成蓝色 —— 看着像两个产品。
# 现在直接把 static/index.html 里的设计令牌搬过来当唯一出处：
#   :root 是暗色档（--card #20252d / --txt #e7ebf1 / --accent #ef6a3a …）
#   [data-theme="light"] 是亮色档（--card #ffffff / --txt #17202b / --accent #c94a1a …）
# 选哪一档走**和控制台同一条判据**：控制台默认「跟随系统」= 浏览器 prefers-color-scheme，
# 而 Windows 上这个信号就是注册表的 AppsUseLightTheme（本机实测 =1，也就是亮色档）。
# 用户在控制台里手动切过主题/主色的话，页面会把生效值 POST 到 /api/ui 存进本机配置，
# 气泡优先读那份 —— 页面上看到什么颜色，气泡就是什么颜色。
CONSOLE_TOKENS = {
    "dark": {
        "card": "#20252d", "card2": "#262d37", "line": "#303844", "line2": "#3e4856",
        "txt": "#e7ebf1", "txt2": "#a3aebe", "txt3": "#76818f",
        "ok": "#3ecf8e", "warn": "#f0b429", "danger": "#f2555a", "info": "#4f8cff",
        "accent": "#ef6a3a", "accent_ink": "#2a0f05",
        # 来源标签：hsl(var(--sh) var(--src-s) var(--src-l))，描边用 --src-b，底色带透明度 --src-bg
        "src_s": 50, "src_l": 70, "src_b": 36, "src_a": 0.12,
    },
    "light": {
        "card": "#ffffff", "card2": "#f7f9fc", "line": "#e2e7ef", "line2": "#d3dae5",
        "txt": "#17202b", "txt2": "#5b6675", "txt3": "#8a96a5",
        "ok": "#12a150", "warn": "#a86f00", "danger": "#d13438", "info": "#1a5fd0",
        "accent": "#c94a1a", "accent_ink": "#ffffff",
        "src_s": 56, "src_l": 33, "src_b": 74, "src_a": 0.08,
    },
}
# 控制台「主色」那排圆点对应的覆盖色（html[data-accent=…] 那几条规则），深浅两套各一组
CONSOLE_ACCENTS = {
    "dark": {"blue": ("#4f8cff", "#04122e"), "green": ("#3ecf8e", "#062016"),
             "violet": ("#a78bfa", "#1a1030"), "amber": ("#f0b429", "#221603")},
    "light": {"blue": ("#1a5fd0", "#ffffff"), "green": ("#0f9d58", "#ffffff"),
              "violet": ("#6d28d9", "#ffffff"), "amber": ("#a86f00", "#ffffff")},
}


def _rgb_of(ref):
    """COLORREF（GDI 用的 0x00BBGGRR）→ (r,g,b)。"""
    return (ref & 0xFF, (ref >> 8) & 0xFF, (ref >> 16) & 0xFF)


def _ref(r, g, b):
    """(r,g,b) → COLORREF。"""
    return (int(b) << 16) | (int(g) << 8) | int(r)


def _hex_ref(s):
    """'#20252d' → COLORREF。"""
    s = (s or "").strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        return _ref(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return _ref(0, 0, 0)


def _hsl_ref(hh, ss, ll):
    """HSL（色相 0~360、饱和/亮度 0~100）→ COLORREF，和控制台的 hsl() 一个口径。"""
    try:
        import colorsys
        r, g, b = colorsys.hls_to_rgb((hh % 360) / 360.0, ll / 100.0, ss / 100.0)
        return _ref(round(r * 255), round(g * 255), round(b * 255))
    except Exception:
        return _ref(0, 0, 0)


def _mix_ref(fg, bg, alpha):
    """带透明度的前景色压到背景色上（GDI 没有 alpha 通道，只能自己算混合后的实色）。"""
    a = max(0.0, min(1.0, float(alpha)))
    r1, g1, b1 = _rgb_of(fg)
    r2, g2, b2 = _rgb_of(bg)
    return _ref(round(r1 * a + r2 * (1 - a)),
                round(g1 * a + g2 * (1 - a)),
                round(b1 * a + b2 * (1 - a)))


def _system_prefers_light():
    """Windows 的「应用使用浅色模式」—— 浏览器 prefers-color-scheme 在 Windows 上就是它。"""
    try:
        import winreg
        # 必须查**打开的那把键句柄**：值挂在 Personalize 子键下，
        # 直接拿 HKEY_CURRENT_USER 根键去 QueryValueEx 会 FileNotFoundError，
        # 然后掉进下面的 except —— 真机实测过（本机 AppsUseLightTheme=1 是亮色，
        # 那样读会永远退回暗色，气泡和页面又成两套色，正好是用户报的「主题没变化」）。
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as _k:
            v, _t = winreg.QueryValueEx(_k, "AppsUseLightTheme")
        return bool(int(v))
    except Exception:
        # 读不到就退回暗色档：控制台 <html data-theme="dark"> 是写死的默认值，JS 没跑起来时也是暗色，
        # 气泡跟着它才不会和页面"两套色"。
        return False


def _balloon_theme_name(ui=None):
    """这一轮气泡该用哪一档：'dark' / 'light'。

    优先级和本页一致：本机配置里点了名的（控制台里手动切过）> 跟随系统 > 暗色兜底。
    """
    ui = ui if isinstance(ui, dict) else _ui_cfg()
    t = str(ui.get("theme") or "auto").strip().lower()
    if t in ("light", "亮色"):
        return "light"
    if t in ("dark", "暗色"):
        return "dark"
    return "light" if _system_prefers_light() else "dark"


def _balloon_palette(ui=None):
    """把控制台令牌翻译成 GDI 用的 COLORREF 表（气泡唯一的配色出处）。"""
    name = _balloon_theme_name(ui)
    tk = dict(CONSOLE_TOKENS.get(name) or CONSOLE_TOKENS["dark"])
    acc = str((ui if isinstance(ui, dict) else _ui_cfg()).get("accent") or "default").strip().lower()
    pair = (CONSOLE_ACCENTS.get(name) or {}).get(acc)
    if pair:
        tk["accent"], tk["accent_ink"] = pair
    pal = {"theme": name, "accent_name": acc,
           "src_s": tk.get("src_s", 50), "src_l": tk.get("src_l", 70),
           "src_b": tk.get("src_b", 36), "src_a": tk.get("src_a", 0.12)}
    for k in ("card", "card2", "line", "line2", "txt", "txt2", "txt3",
              "ok", "warn", "danger", "info", "accent", "accent_ink"):
        pal[k] = _hex_ref(tk.get(k, "#000000"))
    # 【2026-10-28 用户：「字体不清楚」】控制台那两档次要文字色是给**坐着凑近读的网页**用的
    # （亮色 --txt3 在白卡上对比只有 2.9:1），气泡是离一臂远扫一眼的东西，11.5px 的小字
    # 直接发灰。这里只往 txt 的方向把气泡这一份调实（网页不动）：暗色档同理提亮一档。
    pal["txt3"] = _mix_ref(pal["txt3"], pal["txt"], 0.62)
    pal["txt2"] = _mix_ref(pal["txt2"], pal["txt"], 0.80)
    return pal


def _balloon_split_head(head):
    """把 _balloon_head 生成的第一行拆回「栏目 / 来源 / 备注」三段，好分别排版。

    _balloon_head 是拼串的（`科技 · 快科技`、`【热榜】凤凰网 第4`、`国际·要闻 · 凤凰网`、`【头条】`），
    这里按同一套规则拆回去，不动数据链路：
      · `【X】尾巴`  → 栏目 X；尾巴末尾的 `第N` 当备注，其余当来源（`凤凰网 第4`）
      · `A · B`      → 栏目 A、来源 B
      · 其它         → 整串当栏目
    """
    s = (head or "").strip()
    if not s:
        return ("", "", "")
    if s.startswith("【") and "】" in s:
        badge = s[1:s.index("】")].strip()
        tail = s[s.index("】") + 1:].strip(" ·　")
    elif " · " in s:
        badge, tail = s.split(" · ", 1)
        badge, tail = badge.strip(), tail.strip(" ·　")
    else:
        return (s, "", "")
    # 尾巴只有两种形态：`来源`，或 `来源 第N`（热榜的排名）。
    # 来源名自己可能带空格（BBC News / The Guardian），所以只能从**末尾**摘 `第N`，
    # 不能按空格劈一刀 —— 劈了就把 "BBC News" 拆成 "BBC" + "News"，标签上少半截站名。
    m = re.search(r"\s(第\s*\d+)$", tail) if tail else None
    if m:
        return (badge, tail[:m.start()].strip(), m.group(1))
    return (badge, tail.strip(), "")


# ────────────── 气泡题图（2026-10-28 第二轮改版：用户选 A 图文卡）──────────────
# 题图在**服务端**先下到本机缓存，气泡绘制时只读本地文件：
#   · 绘制线程（窗口消息线程）绝不等网络 —— 收到条目时后台预热，轮到时文件多半已经在了；
#   · 同一条新闻的图只下一次（按 URL 的 sha1 命名），重启后也直接命中；
#   · 下载与解码都失败就画占位块，版面不跳、也不留一块空白。
BALLOON_IMG_DIR = os.path.join(
    os.getenv("LOCALAPPDATA") or tempfile.gettempdir(), "hotnews", "balloon_img")
_IMG_LOCK = threading.Lock()
_IMG_BUSY = set()          # 正在下载的 URL（避免一条图被并发拉几遍）
_IMG_FAILS = set()         # 下过但失败的 URL（这一进程里不再重试，别每帧都去撞超时）


def _balloon_img_local(url):
    """URL → 本地缓存路径（还没下好返回 ""）。判定只用文件在不在，不碰网络。"""
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return ""
    h = hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:24]
    p = os.path.join(BALLOON_IMG_DIR, h + ".img")
    try:
        if os.path.exists(p) and os.path.getsize(p) > 1024:
            return p
    except Exception:
        pass
    return ""


def _balloon_img_warm(url):
    """确保这张图在后台下一份（已经在本地 / 正在下 / 下过失败 → 直接返回）。"""
    if _balloon_img_local(url) or not isinstance(url, str) \
            or not url.startswith(("http://", "https://")):
        return
    with _IMG_LOCK:
        if url in _IMG_BUSY or url in _IMG_FAILS:
            return
        _IMG_BUSY.add(url)
    threading.Thread(target=_balloon_img_fetch, args=(url,), daemon=True).start()


def _balloon_img_fetch(url):
    """后台线程：直接连（trust_env=False，不读系统代理，和抓新闻一条口径）拉一张图存盘。"""
    try:
        os.makedirs(BALLOON_IMG_DIR, exist_ok=True)
        with httpx.Client(trust_env=False, timeout=8, follow_redirects=True) as c:
            r = c.get(url, headers={"User-Agent": "Mozilla/5.0 (hotnews balloon)"})
            body = r.content if r.status_code == 200 else b""
        # 8MB 上限：热榜站偶尔给 4K 大图，缓存目录别被几张图撑爆
        if 1024 < len(body) <= 8 * 1024 * 1024:
            h = hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:24]
            p = os.path.join(BALLOON_IMG_DIR, h + ".img")
            tmp = p + ".part"
            with open(tmp, "wb") as f:
                f.write(body)
            os.replace(tmp, p)
        else:
            with _IMG_LOCK:
                _IMG_FAILS.add(url)
    except Exception:
        with _IMG_LOCK:
            _IMG_FAILS.add(url)
    finally:
        with _IMG_LOCK:
            _IMG_BUSY.discard(url)


def _balloon_prefetch(entries):
    """一组条目进队列时顺手把题图都排上（**一条一条下**，别十条一起轰站方）。"""
    def _run():
        for e in entries or []:
            url = e[4] if len(e) > 4 else ""
            if _balloon_img_local(url) or not isinstance(url, str) \
                    or not url.startswith(("http://", "https://")):
                continue
            with _IMG_LOCK:
                if url in _IMG_BUSY or url in _IMG_FAILS:
                    continue
                _IMG_BUSY.add(url)
            _balloon_img_fetch(url)      # 在这个后台线程里同步等，一条一条来
    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        pass


class _Balloon:
    def __init__(self):
        self.ok = False
        self.q = []                 # 队列里每一项是「一组要滚动播放的条目」[(title,msg,warn), ...]
        # 【2026-10-28 用户：「切换样式模式，播报暂停了一分钟」】这一组播完之前，新来的
        # 那一组一直排在队列里（队列只在 not showing 时取）—— 一组是 10 条 × 5.6 秒 = 56 秒，
        # 用户看到的正是"点了没反应，一分钟后才换"。interrupt 是"把当前这组掐掉、立刻换"的
        # 请求标记：只有**用户主动开口**那几条路会置它（托盘选样式 / 立即轮播 / 双击图标），
        # 自动那一轮照常排队，不打断正在看的。
        self.interrupt = False
        self.lock = threading.Lock()
        self.hwnd = None
        self.cur = ("", "", False, "", "", "", "")
        self.showing = False
        self.playlist = []
        self.hover = False            # 鼠标是否停在气泡上（悬停才显示「打开/退出」)
        self.paused = False           # 悬停期间暂停自动关闭（否则来不及点按钮）
        self._btn_open = None         # 两个按钮的位置，点击时做命中判断
        self._btn_close = None
        self._tracking = False        # 是否已登记 WM_MOUSELEAVE
        self.cur_arrow = None         # 箭头光标句柄（WM_SETCURSOR 用），取不到就交给系统
        self.idx = 0                # 播到第几条
        # 【2026-10-28 第五轮】播报版式：每条开播前按配置 + 有没有图重新选一次，
        # 动画从 self._t0 起算（40ms 一支计时器推进，见 WM_TIMER 5）。
        # "card" 是**还没播过任何东西**时的初值：静态卡片版，不开计时器（见 _set_current
        # 那句 `!= "card"`）。2026-10-29 精简成三档以后它不再是用户能选的一档，只当内部值。
        self.style = "card"
        self._t0 = 0.0
        # ↓ 2026-10-28 新版式要用的三样：控制台同款配色、画刷缓存、倒计时条的剩余时间
        self.pal = None               # 见 _balloon_palette()；每条开播前刷一次，主题变了就跟着换
        self._brushes = {}            # COLORREF → HBRUSH（同色复用，换配色时整批删掉）
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
            # 【2026-10-28 修 重影/发虚】单靠老 API SetProcessDPIAware 在 PyInstaller 单文件 exe
            # 里可能不生效，DWM 把已按缩放比放大的位图再拉伸一次 = 双重缩放，整图糊 + 文字重影。
            # 改用更可靠的 shcore.SetProcessDpiAwareness（进程级，优先 Per-MonitorV2），
            # 老 API 作兜底；并在 build_exe.spec 里写进 exe manifest 的 dpiAware 声明，双保险。
            try:
                u.SetProcessDPIAware()   # 老 API，兜底
            except Exception:
                pass
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE = 2
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
            # 【2026-10-28 第四轮「Fluent 化」】卡片四周留一圈透明边给阴影。
            # 窗口比卡片大 2*M：外面那圈画的是本程序自己叠出来的柔和投影
            # （UpdateLayeredWindow 的逐像素 alpha，圆角是真抗锯齿，不再是 region 切出来的台阶）。
            self.M = int(20 * s)
            self.winW = self.W + 2 * self.M
            self.winH = self.H + 2 * self.M
            self._dib = None
            self._memdc = None
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

            class SIZE(ctypes.Structure):
                _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            class BLENDFUNCTION(ctypes.Structure):
                _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                            ("SourceConstantAlpha", ctypes.c_ubyte),
                            ("AlphaFormat", ctypes.c_ubyte)]

            self.WNDCLASSW, self.RECT, self.PAINTSTRUCT = WNDCLASSW, RECT, PAINTSTRUCT
            self.SIZE, self.POINT, self.BLENDFUNCTION = SIZE, POINT, BLENDFUNCTION

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
            u.DestroyIcon.argtypes = [wintypes.HICON]
            # 这两个不声明原型，x64 下句柄会被当 int 截断（SetCursor 拿到坏句柄直接无效）
            u.LoadCursorW.restype = wintypes.HANDLE
            u.SetCursor.argtypes = [wintypes.HANDLE]
            u.SetCursor.restype = wintypes.HANDLE
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
            # 量文字宽度：栏目/来源标签的胶囊要按**实际字宽**收紧（写死宽度遇到
            # 「国际·要闻」这种长栏目名就露白边），控制台那边是 padding:0 7px 跟着内容走。
            g.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR,
                                                ctypes.c_int, ctypes.POINTER(SIZE)]
            g.GetTextExtentPoint32W.restype = wintypes.BOOL
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
            # 【2026-10-12 用户反馈「打开/退出按钮毛刺感太强」】GDI RoundRect 没有
            # 抗锯齿，小胶囊在缩放屏上边缘全是台阶。改「4 倍离屏绘制 + HALFTONE 缩回」，
            # 需要的函数原型在这里补齐（StretchBlt/HALFTONE 走 gdi32）。
            g.CreateCompatibleDC.argtypes = [wintypes.HDC]
            g.CreateCompatibleDC.restype = wintypes.HDC
            g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
            g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
            g.DeleteDC.argtypes = [wintypes.HDC]
            g.StretchBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, wintypes.HDC,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wintypes.DWORD]
            g.StretchBlt.restype = wintypes.BOOL
            g.SetStretchBltMode.argtypes = [wintypes.HDC, ctypes.c_int]
            g.SetStretchBltMode.restype = ctypes.c_int
            g.SetBrushOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                        wintypes.LPVOID]
            g.SetViewportOrgEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                           wintypes.LPVOID]
            g.SetViewportOrgEx.restype = wintypes.BOOL
            # 【2026-10-28 第五轮】速递的「擦出式」进场靠 SaveDC + 矩形裁剪 + 还原。
            # 这几个必须钉 argtypes：不钉的话 ctypes 把 64 位 HDC 当 C int 传，直接
            # `OverflowError: int too long to convert`，三个版式全都掉进兜底卡片。
            g.SaveDC.argtypes = [wintypes.HDC]
            g.SaveDC.restype = ctypes.c_int
            g.RestoreDC.argtypes = [wintypes.HDC, ctypes.c_int]
            g.RestoreDC.restype = wintypes.BOOL
            g.IntersectClipRect.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.c_int]
            g.IntersectClipRect.restype = ctypes.c_int
            # 【2026-10-28 第四轮】分层窗口（逐像素 alpha）：画到 32 位 DIB 上再整块交给 DWM。
            # 圆角/阴影才能是真抗锯齿的；SetWindowRgn 那套只能切出台阶。
            class _BMIH(ctypes.Structure):
                _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                            ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                            ("biClrImportant", wintypes.DWORD)]
            self.BMIH = _BMIH
            g.CreateDIBSection.argtypes = [wintypes.HDC, wintypes.LPVOID, wintypes.UINT,
                                           ctypes.POINTER(ctypes.c_void_p),
                                           wintypes.HANDLE, wintypes.DWORD]
            g.CreateDIBSection.restype = wintypes.HBITMAP
            u.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.LPVOID,
                                              wintypes.LPVOID, wintypes.HDC, wintypes.LPVOID,
                                              wintypes.COLORREF, wintypes.LPVOID, wintypes.DWORD]
            u.UpdateLayeredWindow.restype = wintypes.BOOL
            # 【2026-10-28 第二轮】题图要 GDI+ 解码（JPEG/PNG/WebP 都能读），gdi32 只认 BMP。
            # 启动失败不算致命：_draw_thumb 会退成占位块，文字卡照常显示。
            self.gdip = None
            self.gdip_tok = None
            self._img_cache = {}          # 本地路径 → (GpImage*, w, h)，避免每帧重新解码
            try:
                gp = ctypes.WinDLL("gdiplus")

                class GdiplusStartupInput(ctypes.Structure):
                    _fields_ = [("GdiplusVersion", ctypes.c_uint32),
                                ("DebugEventCallback", ctypes.c_void_p),
                                ("SuppressBackgroundThread", ctypes.c_int),
                                ("SuppressExternalCodecs", ctypes.c_int)]

                inp = GdiplusStartupInput(1, None, 0, 0)
                tok = ctypes.c_size_t()
                gp.GdiplusStartup.argtypes = [ctypes.POINTER(ctypes.c_size_t),
                                              ctypes.POINTER(GdiplusStartupInput),
                                              ctypes.c_void_p]
                gp.GdipLoadImageFromFile.argtypes = [ctypes.c_wchar_p,
                                                     ctypes.POINTER(ctypes.c_void_p)]
                gp.GdipDisposeImage.argtypes = [ctypes.c_void_p]
                gp.GdipGetImageWidth.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
                gp.GdipGetImageHeight.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
                gp.GdipCreateFromHDC.argtypes = [wintypes.HDC,
                                                 ctypes.POINTER(ctypes.c_void_p)]
                gp.GdipDeleteGraphics.argtypes = [ctypes.c_void_p]
                gp.GdipSetInterpolationMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
                gp.GdipSetPixelOffsetMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
                gp.GdipDrawImageRectRectI.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                      ctypes.c_int, ctypes.c_int,
                                                      ctypes.c_int, ctypes.c_int,
                                                      ctypes.c_int, ctypes.c_int,
                                                      ctypes.c_int, ctypes.c_int,
                                                      ctypes.c_int, ctypes.c_void_p,
                                                      ctypes.c_void_p, ctypes.c_void_p]
                # 第四轮：软阴影 / 圆角卡面 / 胶囊按钮都走 GDI+（GDI 的 RoundRect 没抗锯齿）
                gp.GdipSetSmoothingMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
                gp.GdipSetPixelOffsetMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
                gp.GdipGraphicsClear.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                gp.GdipCreatePath.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)]
                gp.GdipAddPathArc.argtypes = [ctypes.c_void_p, ctypes.c_float, ctypes.c_float,
                                              ctypes.c_float, ctypes.c_float,
                                              ctypes.c_float, ctypes.c_float]
                gp.GdipClosePathFigure.argtypes = [ctypes.c_void_p]
                gp.GdipDeletePath.argtypes = [ctypes.c_void_p]
                gp.GdipCreateSolidFill.argtypes = [ctypes.c_uint,
                                                   ctypes.POINTER(ctypes.c_void_p)]
                gp.GdipFillPath.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
                gp.GdipDeleteBrush.argtypes = [ctypes.c_void_p]
                gp.GdipCreatePen1.argtypes = [ctypes.c_uint, ctypes.c_float, ctypes.c_int,
                                              ctypes.POINTER(ctypes.c_void_p)]
                gp.GdipDrawPath.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
                gp.GdipDeletePen.argtypes = [ctypes.c_void_p]
                gp.GdipSetClipPath.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
                # 第五轮：聚光灯版要「上暗-中透-下暗」的压暗层和一道斜着扫过的光。
                # 都是 REAL 参数，必须先把 argtypes 钉死 —— 不钉的话 ctypes 按 C double 传，
                # GDI+ 按 float 读，坐标会读成天文数字（整张卡直接黑掉）。
                gp.GdipCreateLineBrushI.argtypes = [ctypes.POINTER(wintypes.POINT),
                                                    ctypes.POINTER(wintypes.POINT),
                                                    ctypes.c_uint, ctypes.c_uint,
                                                    ctypes.c_int,
                                                    ctypes.POINTER(ctypes.c_void_p)]
                gp.GdipSetLinePresetBlend.argtypes = [ctypes.c_void_p,
                                                      ctypes.POINTER(ctypes.c_uint),
                                                      ctypes.POINTER(ctypes.c_float),
                                                      ctypes.c_int]
                gp.GdipFillPolygonI.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                ctypes.POINTER(wintypes.POINT),
                                                ctypes.c_int, ctypes.c_int]
                if gp.GdiplusStartup(ctypes.byref(tok), ctypes.byref(inp), None) == 0:
                    self.gdip, self.gdip_tok = gp, tok
            except Exception as ex:
                print(f"[warn] GDI+ 初始化失败（题图退成占位块）: {type(ex).__name__}: {ex}")
            g.SelectClipRgn.argtypes = [wintypes.HDC, wintypes.HANDLE]
            g.SelectClipRgn.restype = ctypes.c_int
            # 【2026-10-28 用户反馈「鼠标指向打开和退出，鼠标一直转圈圈」】
            # 普通箭头（IDC_ARROW=32512）：注册窗类时当默认光标用，WM_SETCURSOR 里也按回去
            # （见 _wndproc 的 0x0020 分支）。必须放在上面的原型声明之后 —— 早于 restype
            # 声明取回来的句柄在 x64 下会被当 32 位整数截断，SetCursor 拿到的是坏句柄。
            self.cur_arrow = u.LoadCursorW(None, 32512)
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

    def _draw_pager(self, hdc, rect, pal, total, idx):
        """底部那一排分页点：当前这一条拉长一截并用主色，走过的用亮一点的描边色。

        取代原来挤在标题尾巴上的 `(3/10)`（2026-10-28 用户选了「新闻卡 + 倒计时条」那版：
        一眼看出这组十条播到第几，又不占标题的字数）。同样走 4 倍离屏 ——
        6 像素的小圆点直接 RoundRect 会糊成方块。
        """
        u, g = self.u, self.g
        ctypes = self.ctypes
        x1, y1, x2, y2 = rect
        w, h = x2 - x1, y2 - y1
        s = self.scale
        if w <= 0 or h <= 0:
            return
        K = 4
        dot = max(2, int(BALLOON_DOT_W * s))
        on = max(dot, int(BALLOON_DOT_ON_W * s))
        gap = max(1, int(BALLOON_DOT_GAP * s))
        mem = g.CreateCompatibleDC(hdc)
        bmp = g.CreateCompatibleBitmap(hdc, w * K, h * K)
        old = g.SelectObject(mem, bmp)
        made = []
        try:
            bg = g.CreateSolidBrush(pal["card"])
            made.append(bg)
            u.FillRect(mem, ctypes.byref(self.RECT(0, 0, w * K, h * K)), bg)
            x = 0
            for i in range(max(1, int(total or 1))):
                ww = on if i == idx else dot
                col = pal["accent"] if i == idx else (pal["line2"] if i < idx else pal["line"])
                br = g.CreateSolidBrush(col)
                made.append(br)
                ob = g.SelectObject(mem, br)
                g.RoundRect(mem, x * K, 0, (x + ww) * K, h * K, h * K, h * K)
                g.SelectObject(mem, ob)
                x += ww + gap
            g.SetStretchBltMode(hdc, 4)
            g.SetBrushOrgEx(hdc, 0, 0, None)
            g.StretchBlt(hdc, x1, y1, w, h, mem, 0, 0, w * K, h * K, 0x00CC0020)
        finally:
            g.SelectObject(mem, old)
            for b in made:
                g.DeleteObject(b)
            g.DeleteObject(bmp)
            g.DeleteDC(mem)

    def _round_fill(self, hdc, rect, fill, line, radius):
        """圆角块填充（题图占位/裁切底用）。小面积，直接用 RoundRect 即可，不走 4 倍离屏。"""
        u, g = self.u, self.g
        ctypes = self.ctypes
        x1, y1, x2, y2 = rect
        br = g.CreateSolidBrush(fill)
        pn = g.CreatePen(0, max(1, int(self.scale)), line)
        ob = g.SelectObject(hdc, br)
        op = g.SelectObject(hdc, pn)
        try:
            d = max(1, int(2 * radius * self.scale))
            g.RoundRect(hdc, x1, y1, x2, y2, d, d)
        finally:
            g.SelectObject(hdc, ob)
            g.SelectObject(hdc, op)
            g.DeleteObject(br)
            g.DeleteObject(pn)

    def _draw_thumb(self, hdc, rect, path, pal, radius=BALLOON_THUMB_RADIUS,
                    zoom=1.0, ken=(0.5, 0.5)):
        """把题图贴进 rect（设备像素）：按目标宽高比裁原图（cover）+ 圆角裁剪 + 双三次缩放。

        没有图 / 解码失败 → 画一块 --card2 的圆角占位（版面不跳，看着也不像坏了）。
        解码结果按路径缓存在 self._img_cache，倒计时条每 200 毫秒的重画不会反复读盘。

        zoom / ken 给聚光灯版用：zoom=1.0 是最大范围（"离得最远"），越大越往里推；
        ken 是推镜头的锚点（0.5, 0.5 = 正中间）。
        注意这里吃的是**原图**（缓存里是几百 KB 的原尺寸），不是 84px 缩略图 ——
        整卡大图要是拿小图放大，背景会糊成一片（用户：「背景图片太模糊了」）。
        裁的是**目标矩形同宽高比**的一块，不是正方形 —— 方正裁片拉进扁卡，人会横着胖
        （用户：「图片变形了」）。
        """
        ctypes = self.ctypes
        g = self.g
        x1, y1, x2, y2 = rect
        if x2 <= x1 or y2 <= y1:
            return False
        gp = getattr(self, "gdip", None)
        _rad = radius * self.scale
        # 底：圆角块。分层画布上用 GDI+ 填（GDI 的 RoundRect 是台阶），
        # 图没到位 / 解码失败时就靠这块占位，版面不跳。
        if self._memdc and gp:
            _gr = self._gp_graphics()
            try:
                # 这里的 GDI+ 是画在「视口原点已挪到 M」的画布上，坐标就是卡片坐标，
                # 不要再自己加 M（加了等于挪两次，题图会往右下偏一整块留白）。
                self._gp_fill_round(_gr, x1, y1, x2 - x1, y2 - y1,
                                    _rad, self._argb(pal["card2"]))
            finally:
                gp.GdipDeleteGraphics(_gr)
        else:
            self._round_fill(hdc, rect, pal["card2"], pal["line"], radius)
        if not gp or not path:
            return False
        img = self._img_cache.get(path)
        if img is None:
            ip = ctypes.c_void_p()
            if gp.GdipLoadImageFromFile(path, ctypes.byref(ip)) != 0 or not ip:
                return False
            iw, ih = ctypes.c_uint(), ctypes.c_uint()
            gp.GdipGetImageWidth(ip, ctypes.byref(iw))
            gp.GdipGetImageHeight(ip, ctypes.byref(ih))
            if not iw.value or not ih.value:
                gp.GdipDisposeImage(ip)
                return False
            img = (ip, iw.value, ih.value)
            if len(self._img_cache) >= 6:          # 一组最多十条，留 6 张够用
                for _p, _im in list(self._img_cache.items()):
                    try:
                        gp.GdipDisposeImage(_im[0])
                    except Exception:
                        pass
                self._img_cache = {}
            self._img_cache[path] = img
        # 圆角裁剪：GDI+ 用圆角路径裁（抗锯齿）；没有 GDI+ 时退回 GDI 区域裁。
        # 【2026-10-28 第四轮】裁剪区域用的是**设备坐标**（不受视口原点影响），
        # 而这一帧的视口整体挪了 M（投影留白）—— 区域也得跟着挪，否则圆角会切错位置。
        _m = getattr(self, "M", 0)
        rgn = None
        if not gp:
            rgn = g.CreateRoundRectRgn(x1 + _m, y1 + _m, x2 + 1 + _m, y2 + 1 + _m,
                                       int(2 * radius * self.scale), int(2 * radius * self.scale))
        gfx = ctypes.c_void_p()
        ok = False
        try:
            if rgn:
                g.SelectClipRgn(hdc, rgn)
            if gp.GdipCreateFromHDC(hdc, ctypes.byref(gfx)) == 0 and gfx:
                gp.GdipSetInterpolationMode(gfx, 7)    # HighQualityBicubic
                gp.GdipSetPixelOffsetMode(gfx, 2)      # HalfPixel
                _cp = self._gp_round_path(gfx, x1, y1, x2 - x1, y2 - y1, _rad)
                gp.GdipSetClipPath(gfx, _cp, 0)        # CombineModeReplace
                gp.GdipDeletePath(_cp)
                iw, ih = img[1], img[2]
                _z = max(1.0, float(zoom or 1.0))
                # 【2026-10-28 用户：「图片变形了，应该是由远到近，渐进式展示」】
                # 原来是「一律裁正方形，再塞进目标矩形」—— 84×84 的缩略图没问题，
                # 但整卡 600×220 是扁的，方形裁片一拉，人脸就横着胖了一圈。
                # 现在按**目标矩形的宽高比**裁源图（cover），横竖同一个比例缩放，不变形；
                # zoom 只决定裁进来多大一块（1.0 = 能看到的最大范围，越大越近）。
                dw = max(1, x2 - x1)
                dh = max(1, y2 - y1)
                _ar = dw / float(dh)
                if iw / float(ih) > _ar:        # 图比卡宽 → 高度占满，左右裁
                    sh = ih / _z
                    sw = sh * _ar
                else:                          # 图比卡高（或正好）→ 宽度占满，上下裁
                    sw = iw / _z
                    sh = sw / _ar
                sw = max(1, min(int(round(sw)), iw))
                sh = max(1, min(int(round(sh)), ih))
                kx, ky = ken if ken else (0.5, 0.5)
                sx = int(round((iw - sw) * max(0.0, min(1.0, kx))))
                sy = int(round((ih - sh) * max(0.0, min(1.0, ky))))
                gp.GdipDrawImageRectRectI(gfx, img[0], x1, y1, dw, dh,
                                          sx, sy, sw, sh, 2, None, None, None)
                ok = True
        except Exception:
            ok = False
        finally:
            if gfx:
                try:
                    gp.GdipDeleteGraphics(gfx)
                except Exception:
                    pass
            if rgn:
                g.SelectClipRgn(hdc, None)
                g.DeleteObject(rgn)
        if not ok:
            self._round_fill(hdc, rect, pal["card2"], pal["line"], radius)
        return ok

    def _draw_app_icon(self, hdc, rect, pal):
        """App 行最左边的小图标：优先用程序自己的 .ico（就是托盘那个），取不到画圆角块。

        苹果通知的 App 行就是这个位置放一个 20px 左右的应用图标 —— 一眼知道是谁在说话。
        """
        u = self.u
        x1, y1, x2, y2 = rect
        size = max(8, x2 - x1)
        try:
            cand = _res("hotnews.ico")
            if cand and os.path.exists(cand):
                # IMAGE_ICON(1) | LR_LOADFROMFILE(0x10)，cx/cy 直接要目标尺寸，省一次缩放
                h = u.LoadImageW(None, cand, 1, size, size, 0x10 | 0x40)
                if h:
                    u.DrawIconEx(hdc, x1, y1, h, size, size, 0, None, 0x0003)  # DI_NORMAL
                    u.DestroyIcon(h)
                    return
        except Exception:
            pass
        # 兜底：主色圆角块 + 「热」
        self._round_fill(hdc, rect, pal["accent"], pal["accent"], 5)
        self.g.SelectObject(hdc, self.font_icon)
        self.g.SetTextColor(hdc, pal["accent_ink"])
        r = self.RECT(x1, y1, x2, y2)
        u.DrawTextW(hdc, "热", -1, self.ctypes.byref(r),
                    0x1 | 0x4 | 0x800)          # CENTER|VCENTER|NOPREFIX

    def _brush(self, ref):
        """按颜色取画刷（同色复用，别每次重绘都新建 —— GDI 对象泄漏会把桌面其他窗口拖坏）。"""
        b = self._brushes.get(ref)
        if not b:
            b = self.g.CreateSolidBrush(ref)
            self._brushes[ref] = b
        return b

    def _text_w(self, hdc, s, font):
        """量一段文字在当前字体下的像素宽（标签胶囊按它收紧）。

        【2026-10-28 第五轮】以前只量**已经被选进 DC 的那个字体**，传进来的 font
        其实是摆设 —— 播报版式要拿 19px / 10.5px 来回量，不先选字体量出来全是错的。
        """
        try:
            if font:
                self.g.SelectObject(hdc, font)
            sz = self.SIZE()
            if self.g.GetTextExtentPoint32W(hdc, s, len(s), self.ctypes.byref(sz)):
                return int(sz.cx)
        except Exception:
            pass
        return int(len(s or "") * 12 * self.scale)

    def _refresh_pal(self):
        """取一次控制台同款配色；主题/主色变了就把旧的画刷整批删掉重建。"""
        pal = _balloon_palette()
        if self.pal and self.pal.get("theme") == pal.get("theme") \
                and self.pal.get("accent") == pal.get("accent"):
            return self.pal
        for b in list((self._brushes or {}).values()):
            try:
                self.g.DeleteObject(b)
            except Exception:
                pass
        self._brushes = {}
        self.pal = pal
        return pal

    # ────────── 第四轮：GDI+ 装饰层（软阴影 / 抗锯齿圆角 / 常驻胶囊）+ 分层窗口 ──────────
    # 用户原话：「WinUI3 可以尝试一下」「还是不好看」。不用换框架也能拿到 Fluent 的七成质感：
    # 关键是**圆角和投影要抗锯齿**（GDI 的 RoundRect/SetWindowRgn 都是台阶），
    # 以及按钮要常驻（悬停才出现的按钮等于藏起来）。这两件都能在现有绘制里做。
    def _argb(self, cref, alpha=255):
        """COLORREF(0x00BBGGRR) → GDI+ 的 ARGB(0xAARRGGBB)。"""
        c = int(cref or 0)
        return ((int(alpha) & 0xFF) << 24) | ((c & 0xFF) << 16) \
            | (((c >> 8) & 0xFF) << 8) | ((c >> 16) & 0xFF)

    def _gp_round_path(self, gr, x, y, w, h, r):
        """圆角矩形路径（四段 90° 圆弧 + 闭合）。r 是圆角半径。"""
        gp = self.gdip
        path = ctypes.c_void_p()
        gp.GdipCreatePath(0, ctypes.byref(path))
        d = float(max(1, int(r)) * 2)
        x, y, w, h = float(x), float(y), float(w), float(h)
        gp.GdipAddPathArc(path, x, y, d, d, 180.0, 90.0)
        gp.GdipAddPathArc(path, x + w - d, y, d, d, 270.0, 90.0)
        gp.GdipAddPathArc(path, x + w - d, y + h - d, d, d, 0.0, 90.0)
        gp.GdipAddPathArc(path, x, y + h - d, d, d, 90.0, 90.0)
        gp.GdipClosePathFigure(path)
        return path

    def _gp_fill_round(self, gr, x, y, w, h, r, argb):
        gp = self.gdip
        p = self._gp_round_path(gr, x, y, w, h, r)
        br = ctypes.c_void_p()
        gp.GdipCreateSolidFill(argb, ctypes.byref(br))
        gp.GdipFillPath(gr, br, p)
        gp.GdipDeleteBrush(br)
        gp.GdipDeletePath(p)

    def _gp_stroke_round(self, gr, x, y, w, h, r, argb, width=1.0):
        gp = self.gdip
        p = self._gp_round_path(gr, x, y, w, h, r)
        pen = ctypes.c_void_p()
        gp.GdipCreatePen1(argb, float(max(1.0, width)), 2, ctypes.byref(pen))   # UnitPixel
        gp.GdipDrawPath(gr, pen, p)
        gp.GdipDeletePen(pen)
        gp.GdipDeletePath(p)

    def _gp_graphics(self):
        """拿一个画在 DIB 上的 Graphics（抗锯齿 + 高质量像素对齐）。"""
        gp = self.gdip
        gr = ctypes.c_void_p()
        gp.GdipCreateFromHDC(self._memdc, ctypes.byref(gr))
        gp.GdipSetSmoothingMode(gr, 4)      # AntiAlias
        gp.GdipSetPixelOffsetMode(gr, 2)    # HighQuality
        return gr

    def _paint_card_base(self, hdc, pal, g_rects=None, s=1.0):
        """铺底：柔和投影 + 圆角卡面 + 1px 描边（+ 悬停时才浮出的两颗按钮）。

        【2026-10-28 第五轮】按用户选定的 WinUI3 样张重画：
          · 8px 圆角（不是苹果的 20）、卡面基本不透明（252/255，只留一丝透）；
          · 「关闭 / 打开」是 Fluent 的矩形按钮：等宽、圆角 4、主操作贴最右；
          · 按钮平时不画，鼠标靠近卡片才浮出来（用户第三次要的就是这个）。
        全部画在窗口坐标里（卡片左上角在 (M, M)），然后用**视口原点**把后续 GDI 内容
        平移到卡片坐标系 —— 上面那一大段绘制代码一个字都不用改。
        """
        g = self.g
        M, W, H = self.M, self.W, self.H
        R = int(_balloon_radius_of(self.style) * self.scale)
        # 【坑】视口原点会留在 HDC 上：本次画完设成 M，下一次重绘时
        # GdipCreateFromHDC 会**继承**它 —— GDI+ 再叠一个 M，卡片每重绘一次就
        # 往右下跑一截（首版实拍整张卡歪到窗口外，投影成了一块黑板）。
        # 每次开画前先归零，GDI+ 和后面的 GDI 才在同一套坐标里。
        g.SetViewportOrgEx(self._memdc or hdc, 0, 0, None)
        if self._memdc and self.gdip:
            gr = self._gp_graphics()
            try:
                self.gdip.GdipGraphicsClear(gr, 0)
                # 投影：10 层同心圆角矩形由外向内叠，靠卡片边缘最深、向外淡出。
                # 整体下移 2px —— 光从上方来，卡片才"浮"在桌面之上（用户说的"浮不起来"）。
                _dy = int(2 * self.scale)
                for i in range(10, 0, -1):
                    grow = int(i * (M - 3) / 10.0) + 1
                    self._gp_fill_round(gr, M - grow, M - grow + _dy,
                                        W + 2 * grow, H + 2 * grow, R + grow,
                                        11 << 24)         # 纯黑 11/255，叠十层 ≈ 43% 黑
                # 卡面：几乎不透明（252/255）。原来 238 时，桌面上的黑色图标会透过卡片
                # 映出一个淡淡的影子（实测色差 9 级），看着像卡里夹了个水印。
                self._gp_fill_round(gr, M, M, W, H, R,
                                    self._argb(pal["card"], BALLOON_CARD_ALPHA))
                self._gp_stroke_round(gr, M, M, W, H, R, self._argb(pal["line"]),
                                      max(1.0, self.scale))
                # 【2026-10-28 第五轮·最终口径】用户：「要整成鼠标靠近才显示的状态」——
                # 两颗按钮平时不画（卡片只留内容），鼠标靠近卡片才浮出来。
                # 按钮的底片和字都由 _draw_buttons 画（它是每帧最后一步，才能压在
                # 聚光灯大图之上）—— 在这里画会被后面的版式盖掉。
            finally:
                self.gdip.GdipDeleteGraphics(gr)
            g.SetViewportOrgEx(self._memdc, M, M, None)
            return
        # 兜底（DIB/GDI+ 都没起来）：老样子，不透明卡片，至少别画歪
        u = self.u
        g.SetViewportOrgEx(hdc, M, M, None)
        u.FillRect(hdc, ctypes.byref(self.RECT(0, 0, W, H)), self._brush(pal["card"]))

    def _draw_buttons(self, hdc, g_rects, pal, s):
        """两颗按钮：**每次重绘都记命中矩形**，只有鼠标靠近时才把底片和字画出来。

        【2026-10-28 第五轮】用户：「要整成鼠标靠近才显示的状态」——按钮平时隐身，
        卡片只留内容。命中矩形照旧每帧算（不然鼠标第一次扫过去、重绘还没发生，
        点下去就落空），只是不画。
        底片也在这里画：这是每帧的最后一步，才能压在聚光灯大图之上
        （原来画在 _paint_card_base 里，被后面的版式直接盖掉 —— 实拍只剩两行裸文字）。
        """
        g, u = self.g, self.u
        self._btn_close = tuple(int(v * s) for v in g_rects["btn_close"])
        self._btn_open = tuple(int(v * s) for v in g_rects["btn_open"])
        if not self.hover:
            return
        # 底片：卡片/速递的底还是卡片色（Fluent 次级按钮：几乎透明的片 + 细边）；
        # 聚光灯底下是照片，浅灰会直接消失，改用深色片 + 白边。
        gp = getattr(self, "gdip", None)
        if self._memdc and gp:
            if self.style == "spot":
                _bgs = ((self._btn_close, 0x0A1016, 150, 0xFFFFFF, 70),
                        (self._btn_open, pal["accent"], 255, 0, 0))
            else:
                _bgs = ((self._btn_close, pal["line"], 70, pal["line2"], 190),
                        (self._btn_open, pal["accent"], 255, 0, 0))
            _gr = self._gp_graphics()
            try:
                for r, bg, a, bd, ba in _bgs:
                    # 这一帧的视口原点已经挪到 M（和字一样），坐标就是卡片坐标 ——
                    # 再自己加 M 会多挪一块投影留白，底片掉到字下面去（实拍验过）。
                    x, y = r[0], r[1]
                    w, h = r[2] - r[0], r[3] - r[1]
                    self._gp_fill_round(_gr, x, y, w, h,
                                        BALLOON_BTN_RADIUS * s, self._argb(bg, a))
                    if ba:
                        self._gp_stroke_round(_gr, x, y, w, h, BALLOON_BTN_RADIUS * s,
                                              self._argb(bd, ba), max(1.0, self.scale))
            finally:
                gp.GdipDeleteGraphics(_gr)
        g.SelectObject(hdc, self.font_link)
        # 聚光灯底下是照片，两行字都用白；其余版式底色还是卡片，沿用 txt2。
        _fg_close = _ref(255, 255, 255) if self.style == "spot" else pal["txt2"]
        for r, txt, fg in (
                (self._btn_close, "关闭", _fg_close),
                (self._btn_open, "打开", pal["accent_ink"])):
            g.SetTextColor(hdc, fg)
            rr = self.RECT(r[0], r[1], r[2], r[3])
            u.DrawTextW(hdc, txt, -1, ctypes.byref(rr),
                        0x20 | 0x1 | 0x4 | 0x8000 | 0x800)   # SINGLE|CENTER|VCENTER|NOPREFIX

    def _anim_t(self):
        """这一条从开播到现在过了多少毫秒 —— 播报版式的动画都从这里取时间。"""
        try:
            return max(0.0, (time.monotonic() - self._t0) * 1000.0)
        except Exception:
            return 0.0

    def _dt(self, hdc, txt, rect, font, color, s, flags=0x20 | 0x4 | 0x8000 | 0x800):
        """按**逻辑坐标**画一行字（内部乘缩放比），省得每个调用点都写一遍。

        【2026-10-28 修 字体发虚】`int()` 截断改成 `round()`：
        125% 缩放下逻辑坐标 13 → 16.25，`int` 砍成 16 会丢掉那 0.25 像素，
        整行字相对卡片网格偏移半像素，GDI 灰度抗锯齿就只能糊一条灰边；
        `round` 让它落在最近的物理像素上，笔画边缘才是实边。
        """
        g, u = self.g, self.u
        g.SelectObject(hdc, font)
        g.SetTextColor(hdc, color)
        r = self.RECT(int(round(rect[0] * s)), int(round(rect[1] * s)),
                      int(round(rect[2] * s)), int(round(rect[3] * s)))
        u.DrawTextW(hdc, txt, -1, ctypes.byref(r), flags)

    def _dt_shadow(self, hdc, txt, rect, font, color, s, flags=0x20 | 0x4 | 0x8000 | 0x800,
                   off=0.8, halo=0):
        """先垫一层黑影子再写字 —— 只给聚光灯用。

        那一档的底图是新闻原图，碰上大亮图（天空、白墙、图里自带大字）时，光靠压暗层
        还是会把白字吃掉（2026-10-28 用户：「这种模式字体不清楚」）。影子往右下挪一个多
        物理像素，勾边不糊字。

        【2026-10-28 二次更正·投影必须只有 1 个物理像素】
        用户最初反馈的「有重影」有一半是它：偏移 `off` 是按**逻辑像素**给值，`_dt` 里再
        乘缩放比取整。原来 `off=1.2`，125% 下 1.2*1.25 = 1.5 —— `int()` 砍成 1 物理像素，
        本来是对的；上一轮为了「落整像素」换成 `round()`，`round(1.5) = 2`，**影子从 1px
        变成 2px**。
        一层偏离 2 像素的纯黑字糊在 24px 的白字下面，观感就是"每个字都有双影"。
        现在把 `off` 压到 0.8：0.8*1.25 = 1.0 → 恰好 1 物理像素，既勾得住轮廓又不糊字。
        记住：这里要的是**整数物理像素等于 1**，不是"逻辑值取个整"。

        halo>0 时再绕字描一圈黑（上下左右 + 四个对角，共 8 遍）—— 描边是**贴着笔画**的，
        不靠方向，所以底图哪一块突然变亮都不会把字吃掉。有了它，底下那层压暗就可以调轻
        （2026-10-28 用户：「图背景样式还要改进一下，图片都看不清了有些」：上一版压到
        226~246/255，整张图下半截基本是黑的，聚光灯变成了黑底白字，图没意义了）。
        代价是每条标题多画 8 次 DrawTextW —— 文字短、重画只在动画帧和换条时发生，不值一提。

        【2026-10-28 修 重影/毛边，用户截图反馈「这个字体怎么优化一下，看的更清晰」】
        偏移必须先落到**整数物理像素**、再换算回逻辑坐标。
        原来直接把 `halo` 当作逻辑像素加进 rect，`_dt` 里再乘 s 取整 —— s=1.25 时
        同一个 1 逻辑像素的偏移，在 x 位置 22 处被算成 1px、在 41 处被算成 2px，
        八个方向粗细还不一致。结果是描边一圈深浅不匀、笔画之间被填满，看着就是
        "重影 + 毛边"，字号越小越明显（标题 19px、来源行 11.5px 首当其冲）。
        现在统一按 `round(v*s)/s` 换算：进 `_dt` 乘 s 之后正好是整数，八向等宽、每帧稳定。
        """
        def _px(v):
            """把逻辑像素换算成"整数物理像素"再折回逻辑坐标（_dt 里乘 s 后就是整数）。"""
            try:
                return round(float(v) * s) / s
            except Exception:
                return float(v)

        if halo:
            h = _px(halo)
            for dx, dy in ((-h, 0.0), (h, 0.0), (0.0, -h), (0.0, h),
                           (-h, -h), (h, -h), (-h, h), (h, h)):
                self._dt(hdc, txt, (rect[0] + dx, rect[1] + dy,
                                    rect[2] + dx, rect[3] + dy), font, 0, s, flags)
        _o = _px(off if off else 1.0)
        self._dt(hdc, txt, (rect[0] + _o, rect[1] + _o, rect[2] + _o, rect[3] + _o),
                 font, 0, s, flags)
        self._dt(hdc, txt, rect, font, color, s, flags)

    def _fit_lines(self, hdc, text, font, maxw, n=2):
        """把标题按**逻辑像素**宽折成最多 n 行（按字符折；中文一行 20 来个字，够用）。

        超出 n 行的部分在最后一行收成「…」，不让它把版面顶出卡片。

        【2026-10-29 修 提前 20% 折行】`maxw` 一直是逻辑像素，但 `_text_w` 量回来的是
        **物理像素**（字体按 `-round(px × scale)` 建的）—— 两个单位直接比大小，125% 屏上
        等于提前 20% 就折行、折不下就砍字。拿本机在跑的 200 条真标题量过（中位数 33 字）：
          · 速递旧版式（18/600、列宽 340）：81/200 条被收成省略号 = **40.5%**；
          · 聚光灯（21.6/700、列宽 436）：58/200 = 29%；
          · 单位对齐后同样这两档：31/200（15.5%）、14/200（7%）。
        所以这一行乘法不是装饰：它单独就把"标题看不全"这件事砍掉一大半，
        也是这次敢把速递/卡片的字号提到聚光灯那一档的前提。
        """
        box = maxw * (self.scale or 1.0)        # 逻辑像素 → 物理像素，和 _text_w 同一单位
        text = (text or "").strip()
        if not text:
            return [""]
        lines, cur = [], ""
        i = 0
        while i < len(text) and len(lines) < n:
            ch = text[i]
            if not cur or self._text_w(hdc, cur + ch, font) <= box:
                cur += ch
                i += 1
                continue
            lines.append(cur)
            cur = ""
        if cur and len(lines) < n:
            lines.append(cur)
            cur = ""
        if i < len(text) and lines:
            last = lines[-1]
            while last and self._text_w(hdc, last + "…", font) > box:
                last = last[:-1]
            lines[-1] = last + "…"
        return lines or [""]

    _grad_warned = False

    def _gp_grad_brush(self, x1, y1, x2, y2, colors, positions, horizontal=False):
        """竖向/横向渐变笔刷（GDI 没有渐变，只能借 GDI+）。

        聚光灯的压暗层和扫过去的那道光都靠它。
        【2026-10-28 用户：「这种模式字体不清楚」】mode 原来写死 4 —— LinearGradientMode
        只有 0(横)/1(竖)/2(斜)/3(反斜) 四个值，4 是非法参数，GdipCreateLineBrushI 直接返回
        错误，**压暗层一帧都没画上去**（拿状态码打出来才看见：每次都是 None）。底图是亮图时
        白字当然看不清。现在按方向传合法值，并且失败会吼一声，不再闷着。
        """
        gp = self.gdip
        br = ctypes.c_void_p()
        p1 = self.wintypes.POINT(int(x1), int(y1))
        p2 = self.wintypes.POINT(int(x2), int(y2))
        st = gp.GdipCreateLineBrushI(ctypes.byref(p1), ctypes.byref(p2),
                                     int(colors[0]), int(colors[-1]),
                                     0 if horizontal else 1, ctypes.byref(br))
        if st != 0 or not br:
            if not _Balloon._grad_warned:
                _Balloon._grad_warned = True
                print("[warn] GDI+ 渐变笔刷创建失败 status=%s（压暗层/扫光会缺）" % st)
            return None
        n = len(colors)
        ca = (ctypes.c_uint * n)(*[int(c) for c in colors])
        pa = (ctypes.c_float * n)(*[float(p) for p in positions])
        gp.GdipSetLinePresetBlend(br, ca, pa, n)
        return br

    def _paint_bcast(self, hdc, pal, s, g_rects):
        """播报版式的总入口：按 self.style 分派，最后把悬停按钮压在最上层。

        版式尺寸和样张一致（480×176 逻辑像素）：整行宽的两行大标题，题图在底部左角。
        所有版式都**不画进度条**（用户：「倒计时条不要，看着心慌」）。
        2026-10-29 精简成三档以后这里只剩两条分支：速递（live）和聚光灯（spot）
        —— 新闻条（tv）整档删掉了，理由写在 BALLOON_STYLES 上面那段。
        """
        self.g.SetBkMode(hdc, 1)                  # TRANSPARENT
        t = self._anim_t()
        try:
            if self.style == "live":
                self._paint_live(hdc, pal, s, t)
            else:
                self._paint_spot(hdc, pal, s, t)
        except Exception as ex:
            # 某个版式画崩了不能让整条气泡消失：退回卡片版的内容（至少字还在）
            print("[warn] 播报版式绘制失败（%s）: %s: %s" % (self.style, type(ex).__name__, ex))
            self._paint_fallback(hdc, pal, s)
        self._draw_buttons(hdc, g_rects, pal, s)

    def _paint_fallback(self, hdc, pal, s):
        """兜底：标题 + 来源两行字，颜色走当前配色。"""
        text = self.cur[1] if len(self.cur) > 1 else ""
        meta = self.cur[5] if len(self.cur) > 5 else ""
        self._dt(hdc, text, (22, 46, BALLOON_W - 22, 110), self.font_msg, pal["txt"], s,
                 0x10 | 0x2000 | 0x40000 | 0x800)
        if meta:
            self._dt(hdc, meta, (22, 140, 380, 154), self.font_meta, pal["txt3"], s)

    def _bc_open_link(self, hdc, s, color):
        """右下角那个「打开 ↗」：鼠标靠近后换成 Fluent 按钮，所以悬停时不画。"""
        if self.hover:
            return
        self._dt(hdc, "打开 ↗", (BALLOON_W - 22 - 64, 134, BALLOON_W - 22, 154),
                 self.font_link, color, s, 0x20 | 0x2 | 0x4 | 0x8000 | 0x800)

    def _paint_live(self, hdc, pal, s, t):
        """③ 直播速递：像主播念稿，标题一个字一个字打出来，右上声波一直在跳。

        没有题图也成立（这正是它在「自动」里接住纯文字稿的原因）。
        """
        g, u = self.g, self.u
        L = lambda v: int(round(v * s))
        text = self.cur[1] if len(self.cur) > 1 else ""
        meta = self.cur[5] if len(self.cur) > 5 else ""
        time_txt = self.cur[6] if len(self.cur) > 6 else ""
        img = self.cur[4] if len(self.cur) > 4 else ""
        self._draw_app_icon(hdc, (L(22), L(14), L(40), L(32)), pal)
        self._dt(hdc, "热点新闻", (47, 14, 200, 34), self.font_app, pal["txt"], s)
        ax = 47 + self._text_w(hdc, "热点新闻", self.font_app) / float(s) + 8
        gr = self._gp_graphics() if (self._memdc and self.gdip) else None
        if gr:
            try:
                # 绿点：1.3 秒一个呼吸
                e = 0.55 + 0.45 * abs(math.sin(t / 1300.0 * math.pi))
                self._gp_fill_round(gr, L(ax), L(20), L(7), L(7), L(3.5),
                                    self._argb(_mix_ref(pal["ok"], pal["card"], e), 255))
            finally:
                self.gdip.GdipDeleteGraphics(gr)
        self._dt(hdc, "实时播报", (ax + 12, 14, ax + 100, 34), self.font_badge,
                 pal["accent"], s)
        # ── 右上五根声波：高低错开，永远在跳 ──
        if gr:
            gr = self._gp_graphics()
            try:
                bx = BALLOON_W - 22 - 5 * 4.5
                for i in range(5):
                    hh = 4 + 9 * (0.5 + 0.5 * math.sin(t / 1050.0 * 2 * math.pi + i * 0.9))
                    x = bx + i * 4.5
                    self._gp_fill_round(gr, L(x), L(27 - hh), L(2.5), L(hh), L(1.2),
                                        self._argb(pal["accent"], 255))
            finally:
                self.gdip.GdipDeleteGraphics(gr)
        # ── 两行标题：打字机（第一行 0~1.32s，第二行 1.14~2.46s）──
        # 【2026-10-29 用户：「这个字体换成，聚光灯式的字体，包括卡片式字体，自动的字体」】
        # 字号字重 = 聚光灯那一档（21.6/700），标题带也和它共用同一条：整行 436 宽、两行 ×30。
        # 以前是 18/600 + 列宽 340（右边让给 74 的题图）：本机 200 条真标题量下来两行装不下
        # 81 条（40.5%），字还比聚光灯小一号。现在题图挪到底部左角，标题占满整行，
        # 装不下的只剩 14 条（7%）—— 字变大、被砍的字反而变少。
        lines = self._fit_lines(hdc, text, self.font_title, BALLOON_TITLE_W, 2)
        for i, ln in enumerate(lines[:2]):
            base, span, steps = (0.0, 1320.0, 14) if i == 0 else (1140.0, 1320.0, 10)
            e = max(0.0, min(1.0, (t - base) / span))
            frac = int(steps * e) / float(steps)
            if frac <= 0:
                continue
            g.SaveDC(hdc)
            g.IntersectClipRect(hdc, L(22), L(44 + i * 30),
                                L(22 + BALLOON_TITLE_W * frac), L(74 + i * 30))
            self._dt(hdc, ln, (22, 44 + i * 30, 22 + BALLOON_TITLE_W, 74 + i * 30),
                     self.font_title, pal["txt"], s)
            g.RestoreDC(hdc, -1)
        mtxt = meta + (" · " + time_txt if (meta and time_txt) else "")
        if mtxt:
            self._dt(hdc, mtxt, (22 + BALLOON_THUMB + BALLOON_THUMB_GAP, BALLOON_META_TOP,
                                 390, BALLOON_META_TOP + BALLOON_META_H),
                     self.font_meta, pal["txt3"], s)
        # ── 题图：念完两行才淡入（用 0.96→1 的轻微放大代替透明度，GDI 图片没有 alpha）──
        # 位置在底部左角（和卡片版同一格），右端那块留给「打开」和悬停时的两颗按钮。
        e = max(0.0, min(1.0, (t - 2400) / 600.0))
        if img and e > 0:
            side = BALLOON_THUMB * (0.96 + 0.04 * e)
            cx, cy = 22 + BALLOON_THUMB / 2.0, BALLOON_THUMB_TOP + BALLOON_THUMB / 2.0
            self._draw_thumb(hdc, (L(cx - side / 2), L(cy - side / 2),
                                   L(cx + side / 2), L(cy + side / 2)),
                             _balloon_img_local(img), pal, BALLOON_THUMB_RADIUS)
        self._bc_open_link(hdc, s, pal["accent"])

    def _paint_spot_backdrop(self, gr, Wd, Hd, R, pal, e):
        """聚光灯拿不到题图时的那张底（自己画一张，不退成别的样式）。

        为什么要有它（2026-10-28 用户：「图片样式，会出现没有图片的样式」）：题图是**边播边
        后台补下**的。实测本机池子 200 条 **100% 都带图 URL**，但开播那一刻缓存只命中 48%，
        慢的那几条十几秒才到 —— 以前一没命中就整条退回速递，一组里聚光灯 / 速递来回跳，
        看着就是"样式自己变了"。画一张底之后从头到尾是同一个版式：字位、圆点、按钮一个像素
        不动，图到了下一帧自然盖上。

        四层，全在圆角以内（和题图那一版共用同一套压暗 + 描边，所以字永远一样实）：
          ① 主色 → 近黑的竖底渐变（上面带色、下面压暗，标题本来就压在下半截）；
          ② 右上角一道斜着下来的主色光洗 —— 让整张卡有方向感，不是一张平涂；
          ③ 标题那一带一小片很软的白光池（"灯"打在这儿）；
          ④ 一道从左往右慢慢走过去的斜光柱，5.2 秒走完 —— 和有图那版的推近同一条时间轴，
             所以两种底来回切的时候节奏是一致的。

        为什么第一版要重画（2026-10-28 自查）：底渐变给的是深蓝(20,28,44)→近黑，主色光洗
        只有 118/255 且压在渐变上，再叠一层有图版沿用下来的 46/255 全局 veil 和底部 190/255
        —— 实拍出来主色几乎看不见，整张卡是一块死黑，看着像"图挂了"而不是"这一档长这样"。
        现在主色直接进底渐变（上半截给到 58% 的主色），五档主色一眼能分辨。
        """
        gdp, L = self.gdip, int
        dark, deep, white = _ref(9, 13, 21), _ref(20, 28, 44), _ref(255, 255, 255)
        acc = pal["accent"]

        def _fillp(br):
            """把一条渐变按圆角卡形铺满（路径用完即删，别让 GDI+ 对象漏着）。"""
            if not br:
                return
            pth = self._gp_round_path(gr, 0, 0, Wd, Hd, R)
            gdp.GdipFillPath(gr, br, pth)
            gdp.GdipDeletePath(pth)
            gdp.GdipDeleteBrush(br)

        # ① 底：上半截带主色，往下收到近黑（字在下半截，那里必须暗）
        _top = _mix_ref(acc, dark, 0.42)
        _fillp(self._gp_grad_brush(0, 0, 0, Hd,
                                   [self._argb(_top, 255),
                                    self._argb(_mix_ref(deep, dark, 0.55), 255),
                                    self._argb(dark, 255)],
                                   (0.0, 0.55, 1.0)))
        # ② 右上到左下的主色光洗：右上角最亮，往标题方向淡掉
        _hot = _mix_ref(acc, white, 0.22)
        _fillp(self._gp_grad_brush(Wd, 0, L(Wd * 0.15), Hd,
                                   [self._argb(_hot, 120),
                                    self._argb(_mix_ref(acc, dark, 0.55), 46),
                                    self._argb(dark, 0)],
                                   (0.0, 0.45, 1.0)))
        # ③ 标题那一带的软光池
        _fillp(self._gp_grad_brush(0, L(Hd * 0.30), 0, Hd,
                                   [self._argb(white, 0),
                                    self._argb(_ref(206, 222, 255), 34),
                                    self._argb(white, 0)],
                                   (0.0, 0.55, 1.0)))
        bw, x = 0.34 * Wd, -0.22 * Wd + 0.62 * Wd * e
        slant = L(0.30 * Hd)
        pts = (self.wintypes.POINT * 4)()
        pts[0] = self.wintypes.POINT(L(x), 0)
        pts[1] = self.wintypes.POINT(L(x + bw), 0)
        pts[2] = self.wintypes.POINT(L(x + bw - slant), Hd)
        pts[3] = self.wintypes.POINT(L(x - slant), Hd)
        br = self._gp_grad_brush(x, 0, x + bw, 0,
                                 [self._argb(white, 0),
                                  self._argb(white, 40),
                                  self._argb(white, 0)],
                                 (0.0, 0.5, 1.0), horizontal=True)
        if br:
            pth = self._gp_round_path(gr, 0, 0, Wd, Hd, R)
            gdp.GdipSetClipPath(gr, pth, 0)
            gdp.GdipFillPolygonI(gr, br, pts, 4, 1)
            gdp.GdipResetClip(gr)       # 裁剪不收回来，后面几层压暗就被裁进这道光里
            gdp.GdipDeletePath(pth)
            gdp.GdipDeleteBrush(br)

    def _paint_spot(self, hdc, pal, s, t):
        """④ 聚光灯大图：整卡铺原图、压暗、一道光扫过，标题从下往上浮。

        图没到位时**不换样式**：改画 `_paint_spot_backdrop` 那张底，压暗、字位、圆点、按钮
        和有图那一版完全一样（2026-10-28 用户：「图片样式，会出现没有图片的样式」）。

        背景用的是**原图**（缓存里几百 KB 那种），不是 84 的缩略图 —— 拿小图放大
        整卡会糊成一片（用户：「背景图片太模糊了」）。
        """
        g, u = self.g, self.u
        L = lambda v: int(round(v * s))
        Wd, Hd = L(BALLOON_W), L(BALLOON_H)
        text = self.cur[1] if len(self.cur) > 1 else ""
        meta = self.cur[5] if len(self.cur) > 5 else ""
        time_txt = self.cur[6] if len(self.cur) > 6 else ""
        img = self.cur[4] if len(self.cur) > 4 else ""
        # 【2026-10-28 用户：「应该是由远到近，渐进式展示」】
        # 从 zoom=1.0（能看到的范围最大、整张图"离得最远"）缓推到 1.10，
        # 锚点同时从正中慢慢偏向人物所在的一侧；5.2 秒推完就停住，不来回抽。
        _e = _balloon_ease(min(1.0, max(0.0, t / 5200.0)))
        zoom = 1.0 + 0.10 * _e
        ken = (0.50 + 0.12 * _e, 0.50 - 0.08 * _e)
        _pic = self._draw_thumb(hdc, (0, 0, Wd, Hd), _balloon_img_local(img), pal,
                                BALLOON_BCAST_RADIUS, zoom=zoom, ken=ken)
        # 【2026-10-28 用户：「图片样式，会出现没有图片的样式」】
        # 图没到位**不再退回速递**（以前这里画不出底图就直接退一档，一组里
        # 聚光灯 / 速递来回跳）。改由 _paint_spot_backdrop 画一张自己的底，字的位置一个像素不动，
        # 图在后台下好了下一帧自然盖上 —— 用户选的是样式，不该被下载进度改写。
        # GDI+ 起不来时（self.gdip 为 None，画不了渐变）仍退回速递：那是一张纯黑方块卡，
        # 比打字机难看，属于"环境坏了"而不是"这条没图"。
        if not _pic and not (self._memdc and self.gdip):
            self._paint_live(hdc, pal, s, t)
            return
        if self._memdc and self.gdip:
            R = int(BALLOON_BCAST_RADIUS * s)
            gr = self._gp_graphics()

            def _fill(br):
                """把一条渐变刷裁着圆角卡形铺满（GDI+ 的渐变是"无限"的，靠路径收边）。"""
                if not br:
                    return
                pth = self._gp_round_path(gr, 0, 0, Wd, Hd, R)
                self.gdip.GdipFillPath(gr, br, pth)
                self.gdip.GdipDeletePath(pth)
                self.gdip.GdipDeleteBrush(br)

            def _fill_solid(argb):
                """整卡铺一层纯色（圆角收边）—— 聚光灯那层很轻的全局 veil 用它。"""
                self._gp_fill_round(gr, 0, 0, Wd, Hd, R, argb)

            try:
                if not _pic:
                    # 没图：先铺一张画出来的底，再走和「有图」一模一样的压暗 + 描边 + 字位
                    self._paint_spot_backdrop(gr, Wd, Hd, R, pal, _e)
                # 【2026-10-28 用户：「图背景样式还要改进一下，图片都看不清了有些」】
                # 上一版为了救白字，把 56 以下整片压到 226~246/255 —— 图的下半截基本成了纯黑，
                # 聚光灯退化成"黑底白字"，而这档存在的理由本来就是"图好看"。
                # 现在三样分担，压暗只负责压住最亮的那部分，边缘对比交给贴着笔画的描边：
                #   ① 整卡一层很轻的 veil（18%）：把高光压下去一档，图还是图；
                #   ② 顶部一条给 App 行、底部一条给标题/来源行，峰值只有 190（原来 246）；
                #   ③ 字用 halo 描边（8 个方向各描一遍黑），底图某一块突然变亮也不会吃字。
                _fill_solid(self._argb(0x060A10, 46))
                _fill(self._gp_grad_brush(0, 0, 0, L(46),
                                          [self._argb(0x060A10, 150),
                                           self._argb(0x060A10, 70),
                                           self._argb(0x060A10, 0)],
                                          (0.0, 0.6, 1.0)))
                # 【2026-10-28 修 字体清晰度】底部这一档 190 → 212、中段 130 → 148。
                # 用户截图反馈标题"看着不清楚"：标题压在新闻原图上，峰值 190 时
                # 白字还得靠一圈黑描边撑对比，描边又把笔画间空隙填满 → 越看越糊。
                # 把标题所在的下半截再压暗一档（离 226~246 那个"图全黑"的极端还差很远，
                # 图照样看得出内容），字就能少依赖描边，边缘才干净。
                _fill(self._gp_grad_brush(0, L(56), 0, Hd,
                                          [self._argb(0x060A10, 0),
                                           self._argb(0x060A10, 78),
                                           self._argb(0x060A10, 148),
                                           self._argb(0x060A10, 212)],
                                          (0.0, 0.35, 0.68, 1.0)))
                # 扫光：1.2~3.72 秒从左边扫到右边
                e = _balloon_ease((t - 1200) / 2520.0)
                if 0.0 < e < 1.0:
                    bw = 0.46 * Wd
                    x = -0.55 * Wd + (1.18 + 0.55) * Wd * e
                    slant = int(0.287 * Hd)
                    pts = (self.wintypes.POINT * 4)()
                    pts[0] = self.wintypes.POINT(int(x), 0)
                    pts[1] = self.wintypes.POINT(int(x + bw), 0)
                    pts[2] = self.wintypes.POINT(int(x + bw - slant), Hd)
                    pts[3] = self.wintypes.POINT(int(x - slant), Hd)
                    br = self._gp_grad_brush(x, 0, x + bw, 0,
                                             [self._argb(0xFFFFFF, 0),
                                              self._argb(0xFFFFFF, 52),
                                              self._argb(0xFFFFFF, 0)],
                                             (0.0, 0.5, 1.0), horizontal=True)
                    if br:
                        pth = self._gp_round_path(gr, 0, 0, Wd, Hd, R)
                        self.gdip.GdipSetClipPath(gr, pth, 0)
                        self.gdip.GdipFillPolygonI(gr, br, pts, 4, 1)
                        # 用完把裁剪收回来：gr 是这一帧共用的，留着裁剪会把后面几条 _fill
                        # 一起裁进这道光里（无图那一版要先画底，顺序就撞上过）
                        self.gdip.GdipResetClip(gr)
                        self.gdip.GdipDeletePath(pth)
                        self.gdip.GdipDeleteBrush(br)
            finally:
                self.gdip.GdipDeleteGraphics(gr)
        white = _ref(255, 255, 255)
        # 次级文字（时间 / 来源）：压暗变轻了，这两档就得比原来亮一截，
        # 靠描边站住，不靠把底图涂黑站住。
        # 【2026-10-28 第七轮】再各提一档（0.88→0.93、0.84→0.90）：标题加粗之后，
        # 次级字要是还那么灰，整块就成了"一个粗标题 + 两行虚影"，层级反而乱。
        dim = _mix_ref(white, _ref(12, 16, 20), 0.93)
        dim2 = _mix_ref(white, _ref(12, 16, 20), 0.90)
        self._draw_app_icon(hdc, (L(22), L(13), L(40), L(31)), pal)
        self._dt_shadow(hdc, "热点新闻", (47, 13, 200, 33), self.font_app, white, s, halo=1)
        self._dt_shadow(hdc, time_txt or "刚刚", (BALLOON_W - 130, 13, BALLOON_W - 22, 33),
                        self.font_meta, dim, s, 0x20 | 0x2 | 0x4 | 0x8000 | 0x800, halo=1)
        # 标题从下往上浮（0.72 秒到位），两行 21.6/700 —— 这一档现在是四版式共用的 font_title
        # 行高 30、起始位置 83：21.6 的字塞在 27 的行高里会顶到下一行；
        # 83 + 30*2 = 143，来源行（146）还留 3px 空隙。
        lines = self._fit_lines(hdc, text, self.font_title, BALLOON_TITLE_W, 2)
        e = _balloon_ease(t / 720.0)
        top = 83 + int((1.0 - e) * 10)
        for i, ln in enumerate(lines[:2]):
            self._dt_shadow(hdc, ln, (22, top + i * 30, BALLOON_W - 22, top + i * 30 + 30),
                            self.font_title, white, s, halo=1)
        if meta:
            self._dt_shadow(hdc, meta, (22, 146, 330, 164), self.font_meta, dim2, s, halo=1)
        if not self.hover:
            self._dt_shadow(hdc, "打开 ↗", (BALLOON_W - 22 - 64, 146, BALLOON_W - 22, 164),
                            self.font_link, white, s, 0x20 | 0x2 | 0x4 | 0x8000 | 0x800,
                            halo=1)

    def _finish_paint(self, hwnd, ps, hdc):
        """结束这次绘制：分层窗口要把整块画布推给 DWM，普通窗口只要 EndPaint。"""
        self.u.EndPaint(hwnd, ctypes.byref(ps))
        if self._memdc:
            self._solidify_alpha()
            self._present()
        else:
            try:
                self.u.ReleaseDC(hwnd, hdc)
            except Exception:
                pass

    def _solidify_alpha(self):
        """卡片内部 alpha 钉成卡面那一档 —— 修 GDI 在 32 位 DIB 上画出的"透明洞"。

        分层窗口的像素带 alpha，而 GDI（DrawText / FillRect / StretchBlt / DrawIconEx）
        只认 XRGB，写进去的像素 alpha 一律是 0。第一版实拍里，分页点整条成了
        透出桌面的白杠、文字颜色也发灰，就是这来的。卡片本身是半透明的（BALLOON_CARD_ALPHA），
        所以画完把圆角矩形内每行可见区间的 alpha 写成那一档（圆角外留给 GDI+ 的投影）。
        220 行切片赋值，耗时可以忽略；比逐像素扫一遍快几个数量级。
        """
        if not (self._memdc and getattr(self, "_dib_bits", None)):
            return
        W, H, M = self.W, self.H, self.M
        R = int(_balloon_radius_of(self.style) * self.scale)
        stride = self.winW * 4
        raw = (ctypes.c_ubyte * (self.winW * self.winH * 4)) \
            .from_address(int(self._dib_bits.value or 0))
        mv = memoryview(raw).cast("B")
        card_a = bytes((int(BALLOON_CARD_ALPHA),))
        for y in range(H):
            # 离上/下边越近，圆角处越要往里收：inset = R - sqrt(R² - (R-d)²)
            d = min(y, H - 1 - y)
            inset = 0
            if d < R:
                inset = int(R - ((R * R - (R - d) * (R - d)) ** 0.5) + 0.5)
            x0, x1 = M + inset, M + W - inset
            if x1 <= x0:
                continue
            base = (M + y) * stride
            mv[base + x0 * 4 + 3: base + x1 * 4 + 3: 4] = card_a * (x1 - x0)

    def _present(self):
        """把 DIB 整块交给 DWM（逐像素 alpha：圆角/阴影外是透明的，鼠标也点不到）。"""
        if not (self._memdc and self.hwnd):
            return
        u = self.u
        sdc = u.GetDC(None)
        try:
            pt = self.POINT(self.x, self.y)
            sz = self.SIZE(self.winW, self.winH)
            src = self.POINT(0, 0)
            bf = self.BLENDFUNCTION(0, 0, 255, 1)          # AC_SRC_OVER / AC_SRC_ALPHA
            u.UpdateLayeredWindow(self.hwnd, sdc, ctypes.byref(pt), ctypes.byref(sz),
                                  self._memdc, ctypes.byref(src), 0, ctypes.byref(bf), 2)
        finally:
            u.ReleaseDC(None, sdc)

    def _wndproc(self, hwnd, msg, wp, lp):
        ctypes, wintypes = self.ctypes, self.wintypes
        u, g = self.u, self.g
        try:
            if msg == 0x0020:                      # WM_SETCURSOR —— 光标钉回普通箭头
                # 【2026-10-28 用户反馈「鼠标指向打开和退出，鼠标一直转圈圈」】
                # 转圈有两层来历，这里挡第二层：
                #   1) 开网页原本卡在气泡的消息线程里（ShellExecuteW 同步等浏览器起来），
                #      那几秒线程不在消息循环里 → 系统判定窗口没响应。已改后台线程，见 0x0201。
                #   2) 即便不卡，ShellExecute 一类调用期间系统会把"等待"光标盖到本进程窗口上；
                #      气泡只是右下角一张小卡片，用户在这儿不需要等待光标。
                # 返回 TRUE（非 0）= 我自己处理了，别让 DefWindowProc 再按"忙"来设光标。
                if self.cur_arrow:
                    u.SetCursor(self.cur_arrow)
                    return 1
            if msg == 0x000F:                      # WM_PAINT
                ps = self.PAINTSTRUCT()
                u.BeginPaint(hwnd, ctypes.byref(ps))
                hdc = self._memdc or u.GetDC(hwnd)
                rc = self.RECT(0, 0, self.W, self.H)
                title, text, warn = self.cur[0], self.cur[1], self.cur[2]
                img = self.cur[4] if len(self.cur) > 4 else ""
                meta = self.cur[5] if len(self.cur) > 5 else ""
                time_txt = self.cur[6] if len(self.cur) > 6 else ""
                s = self.scale
                pal = self._refresh_pal()
                total = max(1, len(self.playlist))
                _g = _balloon_rects(BALLOON_W, BALLOON_H, total=total, idx=self.idx,
                                    thumb=bool(img))

                def _dev(r):
                    return (int(r[0] * s), int(r[1] * s), int(r[2] * s), int(r[3] * s))

                # 【2026-10-28 用户：「这个样式应该和控制台的颜色同步」】
                # 卡片本身不再自带颜色，一律走 _balloon_palette()：控制台亮色它就亮色、
                # 暗色它就暗色，主色那五档（品牌/蓝/绿/紫/琥珀）也跟着页面的生效值走。
                # 判据和网页同一条：页面默认「跟随系统」= 注册表 AppsUseLightTheme；
                # 页面里手动点过亮/暗，会把生效值回报到 /api/ui，气泡优先读那份。
                # 【2026-10-28 第四轮】卡面/圆角/投影交给 GDI+（抗锯齿 + 逐像素 alpha），
                # 之后所有内容都画在卡片坐标系里（视口原点挪到留白 M 处）。
                self._paint_card_base(hdc, pal, _g, s)
                # 【2026-10-28 第五轮】播报版式各自整块画（速递/聚光灯；2026-10-29 精简掉新闻条）。
                # 它们和卡片版共用投影、圆角卡面、悬停按钮这一层底，内容层各画各的。
                # 走不到 _paint_bcast 的那一张（self.style == "card"）就是下面这段静态卡片版 ——
                # 用户菜单里已经没有它了，留作"版式解析炸了也还有一条新闻能读"的兜底。
                if self.style != "card":
                    self._paint_bcast(hdc, pal, s, _g)
                    self._finish_paint(hwnd, ps, hdc)
                    return 0
                g.SetBkMode(hdc, 1)                # TRANSPARENT
                badge, _src, _note = _balloon_split_head(title)
                # 【2026-10-09 用户要求「头条要有醒目标识」】苹果版不再有胶囊标签，
                # 栏目挪进 App 行「热点新闻 · 热点」；头条仍用 --danger、大事用 --warn 着色，
                # 只是从"色块"降成"一点颜色"—— 和控制台里 .tag 的用法同一个分寸。
                _head = (badge == "头条") or (title or "").startswith(BALLOON_HEADLINE_TAG)
                tint = pal["danger"] if _head else (pal["warn"] if warn else None)
                # ── App 行：小图标 + 热点新闻 + 栏目 …… 右端 时间 ──
                a = _dev(_g["app"])
                self._draw_app_icon(hdc, _dev(_g["icon"]), pal)
                g.SelectObject(hdc, self.font_app)
                g.SetTextColor(hdc, pal["txt"])
                ax = int(_g["icon"][2] * s) + int(8 * s)
                ar = self.RECT(ax, a[1], a[2], a[3])
                u.DrawTextW(hdc, "热点新闻", -1, ctypes.byref(ar),
                            0x20 | 0x4 | 0x8000 | 0x800)       # SINGLELINE|VCENTER|ELLIPSIS|NOPREFIX
                ax += self._text_w(hdc, "热点新闻", self.font_app) + int(6 * s)
                if badge:
                    g.SelectObject(hdc, self.font_meta)
                    g.SetTextColor(hdc, tint if tint else pal["txt3"])
                    dr = self.RECT(ax, a[1], ax + int(120 * s), a[3])
                    u.DrawTextW(hdc, "· " + badge, -1, ctypes.byref(dr),
                                0x20 | 0x4 | 0x8000 | 0x800)
                if time_txt:
                    g.SelectObject(hdc, self.font_meta)
                    g.SetTextColor(hdc, pal["txt3"])
                    tr2 = self.RECT(a[0], a[1], a[2], a[3])
                    u.DrawTextW(hdc, time_txt, -1, ctypes.byref(tr2),
                                0x20 | 0x2 | 0x8000 | 0x800)   # SINGLELINE|RIGHT|ELLIPSIS|NOPREFIX
                # 题图：底部左角 58×58 圆角（2026-10-29 起标题带走整行宽，题图从右侧挪下来了）；
                # 还没下好 / 没图就画占位块，有图 URL 但文件没到位时**仍然占位**，
                # 不让版面在两条之间跳。
                if _g["thumb"]:
                    self._draw_thumb(hdc, _dev(_g["thumb"]), _balloon_img_local(img), pal)
                # 新闻标题：这一行才是用户真正要读的东西，给它主字号主字色
                # 【2026-10-29 用户：「这个字体换成，聚光灯式的字体，包括卡片式字体，自动的字体」】
                # 卡片档的标题从此和聚光灯同一档（font_title = 21.6/700，真粗体）。
                # 上面那行注释说的"主字号"以前是 16/600 —— 换样式就换一号字，用户读到的
                # 意思是"这几档不是一回事"；现在各档（速递 / 聚光灯，加上退居兜底的卡片版）
                # 共用这一支 font_title —— 标题字号不再是"样式"的一部分。
                g.SelectObject(hdc, self.font_title)
                g.SetTextColor(hdc, pal["txt"])
                t = _dev(_g["text"])
                tr = self.RECT(t[0], t[1], t[2], t[3])
                u.DrawTextW(hdc, text, -1, ctypes.byref(tr),
                            0x10 | 0x2000 | 0x40000 | 0x800)   # WORDBREAK|EDITCONTROL|WORD_ELLIPSIS|NOPREFIX
                # 来源 · 名次：比正文轻一档
                if meta:
                    m = _dev(_g["meta"])
                    g.SelectObject(hdc, self.font_meta)
                    g.SetTextColor(hdc, pal["txt3"])
                    mr = self.RECT(m[0], m[1], m[2], m[3])
                    u.DrawTextW(hdc, meta, -1, ctypes.byref(mr),
                                0x20 | 0x4 | 0x8000 | 0x800)   # SINGLELINE|VCENTER|ELLIPSIS|NOPREFIX
                # 分页点：取代原来挤在标题尾巴上的 (3/10)；十条播到第几眼就能看见
                if total > 1:
                    self._draw_pager(hdc, _dev(_g["pager"]), pal, total, self.idx)
                # 【2026-10-28 用户：「倒计时条不要，看着心慌」】底部那根进度线整个删掉，
                # 连同 200ms 的重画计时器一起（没有条要刷了）。别再"好心"加回来：
                # 用户要的是「随手看一眼新闻」，不是「盯着倒计时等它走完」。
                # 【2026-10-07 用户要求「鼠标指向，显示退出和打开的字样」】
                # 【2026-10-28 第四轮】改成**常驻胶囊**：不再靠悬停才画 —— 悬停才出现的按钮，
                # 用户根本不知道可以点（"退出在哪"就是这么来的）。常驻灰胶囊「关闭」+
                # 主色胶囊「打开」，悬停时两个都提亮一档。
                self._draw_buttons(hdc, _g, pal, s)
                self._finish_paint(hwnd, ps, hdc)
                return 0
            if msg == 0x0113:                      # WM_TIMER
                if wp == 1:                        # 轮询队列：取下一组来播
                    pl = None
                    with self.lock:
                        _ip = self.interrupt
                        if self.q and (not self.showing or _ip):
                            pl = self.q.pop(0)
                        if _ip:
                            self.interrupt = False
                    if pl and self.showing:
                        # 用户主动要看的（选样式 / 立即轮播 / 双击托盘）不等上一组播完：
                        # 先把它掐掉再开新的。掐的动作在**本线程**做（这些计时器就是本线程
                        # SetTimer 出来的），调用方只留了个标记，不跨线程动窗口和计时器。
                        print("[ok] 用户主动要看，掐掉正在播的那一组（停在第 %d/%d 条），"
                              "立刻换成新的 %d 条"
                              % (self.idx + 1, len(self.playlist), len(pl)))
                        self._dismiss(hwnd, drop_pending=False)
                    if pl:
                        self.playlist = pl
                        self.idx = 0
                        self._apply_current()
                        self._place()
                        u.ShowWindow(hwnd, 4)       # SW_SHOWNOACTIVATE
                        u.SetWindowPos(hwnd, -1, self.x, self.y, self.winW, self.winH,
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
                    u.KillTimer(hwnd, 5)
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
                elif wp == 4:                      # 「打开」之后：接着播这一组剩下的
                    u.KillTimer(hwnd, 4)
                    self._resume_rest(hwnd)
                elif wp == 5:                      # 播报版式的动画节拍
                    if not self.showing:
                        u.KillTimer(hwnd, 5)
                    else:
                        u.InvalidateRect(hwnd, None, False)
                return 0
            if msg == 0x0200:                      # WM_MOUSEMOVE —— 悬停
                # 【2026-10-07 用户要求「鼠标指向，显示退出和打开的字样」】
                # 【2026-10-28 第四轮】窗口比卡片大一圈（投影留白 M），鼠标划过那圈阴影
                # 也会收到 WM_MOUSEMOVE。换算回卡片坐标再判，出了敏感圈就当移开。
                _mx = ctypes.c_short(lp & 0xFFFF).value - self.M
                _my = ctypes.c_short((lp >> 16) & 0xFFFF).value - self.M
                # 【2026-10-28 第五轮】用户：「要整成鼠标靠近才显示的状态」——
                # 判悬停的圈比卡片本身大一圈（投影那十几像素也算「靠近」），
                # 手刚凑到卡片边上按钮就浮出来，不用精确压到卡片上；
                # 出了这一圈才收（仍然不是整块留白，手从旁边路过不会误触暂停）。
                _pad = int(16 * self.scale)
                if not (-_pad <= _mx <= self.W + _pad
                        and -_pad <= _my <= self.H + _pad):
                    if self.hover:
                        self._mouse_leave(hwnd)
                    return 0
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
                self._mouse_leave(hwnd)
                return 0
            if msg == 0x0201:                      # WM_LBUTTONDOWN
                # 【2026-10-07 用户反馈「气泡的新闻无法点开」】原来点哪里都只打开程序首页，
                # 看不到那一条新闻。现在优先打开**当前正在播放的这条**的原文链接；
                # 没有链接（比如"我的软件发布"是纯提示、没有跳转目标）才回落到首页。
                # 【2026-10-28 修「点退出没反应、鼠标一直转」】坐标取错了一整轮：
                #   WM_LBUTTONDOWN 的 wParam 是**按键状态位**（按下左键就是 1），
                #   鼠标坐标全在 lParam 里 —— 低 16 位是 x、高 16 位是 y，
                #   而且两个都是有符号 short（副屏在主屏左边时坐标为负，
                #   不还原符号直接 & 0xFFFF 会得到 65535 附近的正数，越点越偏）。
                # 命中判断原来写成 `r[0] <= wp <= r[2]`，等于拿状态位"1"去比按钮的 x 区间
                # （卡片宽 400×缩放，两个胶囊贴在右侧 x≈330~386），永远判不中：
                # 「退出」于是和「打开」、和点正文走了同一条路 —— 都去开网页，
                # 用户看到的就是"退出这个键按了没用"。
                _x = ctypes.c_short(lp & 0xFFFF).value
                _y = ctypes.c_short((lp >> 16) & 0xFFFF).value
                # 【2026-10-28 第四轮】窗口比卡片大一圈（投影留白 M），按钮矩形记的是卡片坐标，
                # 先把鼠标位置换算回卡片坐标系；点在留白那圈（阴影/圆角外）就当没点。
                _x -= self.M
                _y -= self.M
                if not (0 <= _x <= self.W and 0 <= _y <= self.H):
                    return 0
                # 先看是不是点在两个按钮上（坐标来自上一次绘制）
                # 胶囊只有 56x26（×缩放），笔记本上更难瞄，判定区四边各放宽 3px；
                # 两个按钮之间留 8px 间隔，各放 3px 不会互相吃掉。
                _pad = int(3 * self.scale)

                def _in(r):
                    return bool(r) and (r[0] - _pad) <= _x <= (r[2] + _pad) \
                                and (r[1] - _pad) <= _y <= (r[3] + _pad)
                if _in(self._btn_close):
                    # 「退出」= 收起这条气泡，不打开任何东西。
                    # 连排在后面的那组也一起丢掉（drop_pending）：不然 300 毫秒后
                    # 轮询计时器把下一组顶上来，在用户眼里还是"退出按了没反应"。
                    self._dismiss(hwnd, drop_pending=True)
                    return 0
                _link = ""
                try:
                    if isinstance(self.cur, (tuple, list)) and len(self.cur) > 3:
                        _link = self.cur[3] or ""
                except Exception:
                    _link = ""
                # 剩下的落点（「打开」胶囊、以及卡片正文任意处）都是打开这一条。
                # 【2026-10-07】原来只用 webbrowser.open，在打包环境里偶尔不生效
                # （用户反馈"气泡点不开"），改用 ShellExecuteW 最直接可靠。
                # 【2026-10-28】开网页这一步从消息线程里挪走了：ShellExecuteW 同步等浏览器
                # 起来，那几秒气泡线程不在消息循环里待着，Windows 就判定窗口没响应、
                # 给鼠标挂上转圈。现在只做「收起」，开网页交给后台线程。
                # 【2026-10-28 兴趣投放】点开的是哪一类，就给哪一类记一分。
                # 这是「喜欢科技就多投科技」的全部信号来源：不看用户勾选，只看他真点开了什么。
                # 写盘失败也绝不影响开网页，所以整段包在 try 里。
                try:
                    _interest_bump(_balloon_split_head(self.cur[0])[0])
                except Exception:
                    pass
                # 【2026-10-28 第二次】点「打开」不再把整组一起收掉（用户反馈"进网页后轮播停了"）：
                # 摘掉已看过的这条，剩下的隔 BALLOON_RESUME_MS 毫秒接着播。
                _open_app_later(_link or APP_URL)
                self._hide_and_resume(hwnd)
                return 0
            if msg == 0x0002:                      # WM_DESTROY
                u.PostQuitMessage(0)
                return 0
        except Exception as ex:
            print(f"[warn] 气泡窗口消息处理异常: {type(ex).__name__}: {ex}")
        return u.DefWindowProcW(hwnd, msg, wp, lp)

    def _mouse_leave(self, hwnd):
        """鼠标离开卡片（真的移开，或者只是划过外面那圈阴影）：收起悬停态、恢复计时。"""
        self.hover = False
        if getattr(self, "paused", False):
            # 移开就恢复计时，重新给一整组的时间，别立刻消失
            try:
                self.u.SetTimer(hwnd, 2,
                                BALLOON_PER_ITEM_MS * max(1, len(self.playlist)), None)
                if len(self.playlist) > 1:
                    self.u.SetTimer(hwnd, 3, BALLOON_PER_ITEM_MS, None)
                self.paused = False
            except Exception:
                pass
        self._tracking = False
        self.u.InvalidateRect(hwnd, None, True)

    def _apply_current(self):
        """把当前播放到的那一条装进 self.cur。

        【2026-10-28】标题尾巴上那个 `(3/10)` 去掉了：新版式底部有一排分页点（用户选的
        「新闻卡 + 倒计时条」那版），进度由点表示，标题整行都是新闻本身，不用再占五个字符。
        每条开播前顺手刷一次配色 —— 用户在控制台里切了亮/暗、或 Windows 换了深浅色，
        下一条气泡就是那个颜色，不用重启程序。
        """
        try:
            self._refresh_pal()
        except Exception:
            pass
        pl = self.playlist
        if not pl:
            self.cur = ("", "", False, "", "", "", "")
            return
        i = max(0, min(self.idx, len(pl) - 1))
        row = pl[i]
        title, text, warn = row[0], row[1], row[2]
        link = row[3] if len(row) > 3 else ""
        img = row[4] if len(row) > 4 else ""
        meta = row[5] if len(row) > 5 else ""
        time_txt = row[6] if len(row) > 6 else ""
        self.cur = (title, text, warn, link, img, meta, time_txt)
        # 【2026-10-28 第五轮】这一条用哪个播报版式：配置里选的样式 + 这条有没有图。
        # auto 的默认口径是「图已经在本机就大图、还在路上就速递」；用户在任何地方改了样式，
        # 下一条开播就换，不用重启（和主题/主色一个规矩）。
        # 判"有没有图"要看**本机缓存**而不是 URL：URL 每条都有，缓存才是真拿得到（见上条注释）。
        # 用户点名那档不受这里影响 —— spot 没图也照画聚光灯，只是底换成画出来的那张。
        try:
            self.style = _balloon_pick_style(has_img=bool(_balloon_img_local(img)))
        except Exception:
            # 版式解析这一步整个炸了（读配置失败之类）也要有东西可画：退回静态卡片版 ——
            # 它就是 WM_PAINT 那条 fall-through（`if self.style != "card"` 才走 _paint_bcast），
            # 不带动画、不烧 CPU，字一定在。用户 2026-10-29 把菜单精简成三档，卡片没删、
            # 只是从"可选样式"降成"最后的兜底"。
            self.style = "card"
        self._t0 = time.monotonic()
        # 40ms 的动画节拍：只有播报版式需要（卡片版是静态的，别白烧 CPU）。
        try:
            if self.style != "card" and self.hwnd:
                self.u.SetTimer(self.hwnd, 5, BALLOON_ANIM_TICK_MS, None)
        except Exception:
            pass
        # 题图在后台补下（等文件到位后下一帧自然就画出来了；没有也不耽误正文）
        try:
            _balloon_img_warm(img)
        except Exception:
            pass

    def _place(self):
        """放到工作区右下角（避开任务栏）。

        窗口比卡片大一圈（投影留白），所以位置要再往左上挪 M，卡片本身才落在原来的点上。
        """
        rc = self.RECT()
        try:
            self.u.SystemParametersInfoW(0x0030, 0, self.ctypes.byref(rc), 0)   # SPI_GETWORKAREA
        except Exception:
            rc = self.RECT(0, 0, 1920, 1080)
        self.x = max(0, rc.right - self.W - self.pad - self.M)
        self.y = max(0, rc.bottom - self.H - self.pad - self.M)

    def _dismiss(self, hwnd, drop_pending=False):
        """收起气泡：停掉计时器、隐窗口、把状态清干净。

        2026-10-28 抽出来：「退出」和「打开」两条路原本各写一遍收尾，结果两处都不全
        —— 打开那条路没杀计时器（窗口已经隐了还在后台滚 idx）、也没清 paused。
        收拢成一个出口，以后要加动作只改这一处。
        drop_pending=True 是「退出」的口径：排队里剩下的那组也别再弹了。
        """
        try:
            self.u.KillTimer(hwnd, 2)              # 整组停留
            self.u.KillTimer(hwnd, 3)              # 逐条轮换
            self.u.KillTimer(hwnd, 4)              # 「打开」后接着播的那次唤醒
            self.u.KillTimer(hwnd, 5)              # 播报版式的动画节拍
        except Exception:
            pass
        self.showing = False
        self.hover = False
        self.paused = False
        self._tracking = False
        self.playlist = []
        self.idx = 0
        self._btn_open = None
        self._btn_close = None
        try:
            self.u.ShowWindow(hwnd, 0)             # SW_HIDE
        except Exception:
            pass
        if drop_pending:
            try:
                with self.lock:
                    del self.q[:]
            except Exception:
                pass

    def _hide_and_resume(self, hwnd):
        """点「打开」的收尾：刚点开的那条算看过，摘掉；这一组剩下的隔一会儿接着播。

        【2026-10-28 用户反馈「打开，进入网页，轮播就停止了」】原来「打开」和「退出」一样
        走整组收起：10 条播到第 3 条点进去看，剩下 7 条直接没了，一轮等于只看 3 条。

        和「退出」（_dismiss）的差别只在"要不要继续"：
          · 「退出」= 当前这组 + 排队里的下一组全丢掉；
          · 「打开」= 只摘掉刚点开这条，showing **保持 True** —— 300 毫秒的队列轮询
            （计时器 1）只在不 showing 时才取下一组，不然它会趁这 8 秒空档把下一组顶上来，
            剩下的条目就被挤掉了。什么时候再播交给计时器 4。
        点的正好是最后一条（剩下的空了）→ 老老实实整组收起，让下一组正常上来。
        """
        rest = self.playlist[self.idx + 1:]
        if not rest:
            self._dismiss(hwnd)
            return
        try:
            self.u.KillTimer(hwnd, 2)
            self.u.KillTimer(hwnd, 3)
            self.u.KillTimer(hwnd, 4)               # 重复点也不许排两个唤醒
            self.u.KillTimer(hwnd, 5)               # 动画节拍等重新亮出来时再架
        except Exception:
            pass
        self.playlist = rest
        self.idx = 0
        self.hover = False
        self.paused = False
        self._tracking = False
        self._btn_open = None
        self._btn_close = None
        try:
            self.u.ShowWindow(hwnd, 0)              # 先把屏幕让给浏览器
            self.u.SetTimer(hwnd, 4, BALLOON_RESUME_MS, None)
        except Exception:
            self._resume_rest(hwnd)                 # 计时器没架上也不能把剩下几条吞了

    def _resume_rest(self, hwnd):
        """计时器 4 到点：把这一组剩下的接着播（和一组刚开始时同一套动作）。"""
        if not self.playlist:
            self.showing = False
            return
        u = self.u
        self._apply_current()
        self._place()
        try:
            u.ShowWindow(hwnd, 4)                   # SW_SHOWNOACTIVATE
            u.SetWindowPos(hwnd, -1, self.x, self.y, self.winW, self.winH,
                           0x0010 | 0x0040)         # NOACTIVATE|SHOWWINDOW
            u.InvalidateRect(hwnd, None, True)
            u.SetTimer(hwnd, 2, BALLOON_PER_ITEM_MS * len(self.playlist), None)
            if len(self.playlist) > 1:
                u.SetTimer(hwnd, 3, BALLOON_PER_ITEM_MS, None)
        except Exception:
            pass
        self.showing = True

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
            wc.hCursor = self.cur_arrow            # 窗口默认光标：箭头（原来空着，靠系统兜底）
            u.RegisterClassW(ctypes.byref(wc))
            self.x, self.y = 0, 0
            self._place()
            hwnd = u.CreateWindowExW(
                0x00000008 | 0x00000080 | 0x08000000 | 0x00080000,   # TOPMOST|TOOLWINDOW|NOACTIVATE|LAYERED
                "HotNewsBalloonWnd", "热点新闻提醒",
                0x80000000,                               # WS_POPUP
                self.x, self.y, self.winW, self.winH,
                None, None, wc.hInstance, None)
            self.hwnd = hwnd
            s = self.scale
            # 【2026-10-28 用户：「这个样式应该和控制台的颜色同步」】
            # 这里**不再**自带一套画刷（原来白卡 #FFFFFF / 蓝条 #2B6CB0 / 橙条 #DD6B20 /
            # 浅红底 #FFF1F0…和网页那套令牌是两套色）。画刷改由 _brush() 按当前
            # 配色表临建临用（_refresh_pal 换主题时整批删掉重建），字号则固定下来。
            self.null_brush = g.GetStockObject(5)            # NULL_BRUSH（只描边不填充）
            # 字体：加粗走真粗体（weight 700 那一档不让 GDI 合成，合成出来发糊）。
            # 原来的 iQuality=0(DEFAULT) 在缩放屏上会出现锯齿/毛刺，所以一直显式给质量档。
            # 【2026-10-28 第六轮 用户：「字体发虚」】iQuality 从 5(CLEARTYPE) 改成 4(ANTIALIASED)。
            # 气泡是 WS_EX_LAYERED + UpdateLayeredWindow 的逐像素透明窗口：ClearType 把笔画
            # 边缘的灰度藏在 R/G/B 三个子像素里，DWM 再拿 alpha 和桌面混一次，子像素排列就
            # 被打散了 —— 实拍出来字边带橙/蓝毛边，看着就是"发虚"（亮色卡上尤其明显，
            # 量过：中间调像素 100% 有 ≥12 的通道偏差）。灰度抗锯齿只动亮度不动色相，
            # 混完还是干净的灰边。缩放比仍然按物理像素算，所以不会退回毛刺那一版。
            # 字号阶梯照抄控制台 --fs-* 的相对比例（正文 13 / 小标题 14 / meta 12 / micro 11），
            # 但整体放大一档：气泡是离着一臂远扫一眼的东西，页面是坐着凑近读的。
            # 苹果风字阶（2026-10-28 第三轮）：标题 16/600，App 名 12.5/600，备注 11.5/500，
            # 文字按钮 12.5/600。字重比上一版降一档（700 → 600/500），是"苹果"和"论坛加粗"
            # 最直接的区别；字号阶梯仍按控制台 --fs-* 的相对比例整体放一档。
            # 【2026-10-28 修 字体发虚】字号一律 `round()` 取整，不再 `int()` 截断。
            # 125% 缩放下 19 * 1.25 = 23.75，`int` 砍成 23 —— 奇数像素高让微软雅黑的
            # hinting 把笔画挤成不匀的宽度（横画尤其明显）；`round` 给 24，笔画落在整像素上。
            # 12.5 * 1.25 = 15.625 原来被砍成 15，现在 16，小字立刻实一档。
            self.font_msg = g.CreateFontW(-int(round(BALLOON_TITLE_PX * s)), 0, 0, 0, 600, 0, 0, 0,
                                         134, 5, 0, 4, 0, "Microsoft YaHei UI")   # 兜底版式的标题（小一号保平安）
            # 小字也各升一档：12.5 → 13.6（物理 16 → 17）、11.5 → 12.8（物理 14 → 16）。
            # 聚光灯那一版的小字是压在**新闻原图**上的，14 物理像素本来就偏小，档位上到 16 之后
            # 既好认又不至于跟标题抢戏。
            self.font_app = g.CreateFontW(-int(round(13.6 * s)), 0, 0, 0, 600, 0, 0, 0,
                                          134, 5, 0, 4, 0, "Microsoft YaHei UI")  # App 行「热点新闻」
            self.font_link = g.CreateFontW(-int(round(13.6 * s)), 0, 0, 0, 600, 0, 0, 0,
                                           134, 5, 0, 4, 0, "Microsoft YaHei UI")  # 打开 / 关闭
            self.font_meta = g.CreateFontW(-int(round(12.8 * s)), 0, 0, 0, 500, 0, 0, 0,
                                           134, 5, 0, 4, 0, "Microsoft YaHei UI")  # 来源/时间/栏目
            self.font_icon = g.CreateFontW(-int(round(11 * s)), 0, 0, 0, 700, 0, 0, 0,
                                           134, 5, 0, 4, 0, "Microsoft YaHei UI")  # 兜底图标的「热」
            # 【2026-10-28 第五轮·播报版式】当年给几档版式各配过一支字：新闻条的 19/600 大标题、
            # 速递的 18/600、「03 / 10」那种小字和滚动条里的 11.5 —— 别拿 16px 硬凑。
            # 【2026-10-28 第七轮·标题加粗放大】别再靠猜，这里是量出来的：
            #  同样 24 物理像素、拿「加配不加价 i6」统计"有墨"像素数 ——
            #     weight 400 → 1099   weight 500 → 1099（两者完全相同，写 500 等于没写）
            #     weight 600 → 1458   weight 700 → 1792（比 600 再厚 23%）
            #     weight 800 → 1792（到顶，和 700 同形）
            #  结论：这套雅黑只有 Regular / 一个中间档 / Bold 三级，600 不是"半粗"，700 才是
            #  真正的 Bold。所以字重是有效杠杆，600 → 700 是肉眼看得出来的差异。
            #  另外更正：上一轮把 int() 换 round() 后用户反馈「看着没啥变化」是**正常**的，
            #  因为 19*1.25 = 23.75，int 得 23、round 得 24，本来就只差 1 个物理像素。
            #  真正拉开差距的是字号 +1 档、字重 +1 档。
            # 【2026-10-29 精简成三档】上面那支 19.2 的 font_hl 随新闻条(tv)一起删了 ——
            # 它是"换样式=换字号"那个割裂感的唯一来源；现在标题只有 font_title 一支，
            # 外加兜底版式那支小一号的 font_msg。
            # 【2026-10-29 用户：「这个字体换成，聚光灯式的字体，包括卡片式字体，自动的字体」】
            # 这一档原来是聚光灯专用的 font_spot。现在它是**共用的标题字体**，所以改名
            # font_title：速递 / 自动 / 聚光灯（还有退居兜底的卡片版）的标题都是它
            # （21.6 逻辑像素 = 物理 27，真粗体 700）。以前速递用 18/600、卡片用 16/600，
            # 同一组新闻换一档样式标题就换一号字，用户读到的意思是"这几档不是一回事"。
            # 撑得住的底气来自版式：标题带从「让出 84 给题图、两行 ×24」改成「整行 436、
            # 两行 ×30」，加上 _fit_lines 那个单位 bug 修掉，本机 200 条真标题里两行装不下的
            # 从 81 条（速递旧版 40.5%）降到 14 条（7%）—— 字变大，被砍掉的标题反而更少。
            self.font_title = g.CreateFontW(-int(round(BALLOON_HEAD_PX * s)), 0, 0, 0, 700, 0, 0, 0,
                                            134, 5, 0, 4, 0, "Microsoft YaHei UI")
            self.font_num = g.CreateFontW(-int(round(11 * s)), 0, 0, 0, 500, 0, 0, 0,
                                          134, 5, 0, 4, 0, "Microsoft YaHei UI")
            self.font_badge = g.CreateFontW(-int(round(10.5 * s)), 0, 0, 0, 700, 0, 0, 0,
                                            134, 5, 0, 4, 0, "Microsoft YaHei UI")
            # 【2026-10-28 第四轮】不再用 SetWindowRgn 切直角圆角（那是台阶，不是抗锯齿）。
            # 改成：画到 32 位 DIB，圆角/阴影由 GDI+ 按 alpha 画准，再 UpdateLayeredWindow 交给 DWM。
            try:
                _bmi = self.BMIH()
                _bmi.biSize = ctypes.sizeof(self.BMIH)
                _bmi.biWidth = self.winW
                _bmi.biHeight = -self.winH          # 负数 = 自上而下，和 GDI 坐标系一致
                _bmi.biPlanes = 1
                _bmi.biBitCount = 32
                _bmi.biCompression = 0              # BI_RGB（GDI+ 按预乘 alpha 写这个缓冲）
                _bits = ctypes.c_void_p()
                _sdc = u.GetDC(None)
                self._dib = g.CreateDIBSection(_sdc, ctypes.byref(_bmi), 0,
                                               ctypes.byref(_bits), None, 0)
                u.ReleaseDC(None, _sdc)
                if not self._dib:
                    raise ctypes.WinError()
                self._dib_bits = _bits
                self._memdc = g.CreateCompatibleDC(None)
                g.SelectObject(self._memdc, self._dib)
                ctypes.memset(_bits, 0, self.winW * self.winH * 4)
                print("[ok] 气泡画布 %dx%d（含 %dpx 投影留白，逐像素透明）"
                      % (self.winW, self.winH, self.M))
            except Exception as ex:
                print(f"[warn] 气泡透明画布创建失败（退回不透明卡片）: {type(ex).__name__}: {ex}")
                self._dib = None
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


def _show_toast_list(entries, replace=False):
    """一条气泡里滚动播放多条：entries = [(title, msg, warn[, link]), ...]。

    用户要求「滚动播放气泡新闻」：一组最多 BALLOON_PLAY_COUNT 条（2026-10-09 起为 10 条），
    每条停留 BALLOON_PER_ITEM_MS（5600 毫秒），进度由底部那一排分页点表示
    （2026-10-28 新版式：原来挂在标题尾巴上的 (3/10) 挪走了，标题整行留给新闻本身）。
    第一行 = 这条自己的栏目（见 _balloon_head），不再有与栏目无关的固定抬头。

    replace=True 是「用户主动开口要现在看」的口径（托盘选播报样式 / 立即轮播一轮 / 双击图标）：
    上一组还在播就把它掐掉，别让人等完这 56 秒（2026-10-28 用户：「切换样式模式，播报暂停了一分钟」）。
    自动那一轮不传，照常排队 —— 正看着的东西被后台刷新顶掉，比等一分钟更烦人。
    """
    # 统一成 7 元组 (title, msg, warn, link, image, meta, time)；调用方可能只给 3/4 个元素
    norm = []
    for e in (entries or []):
        if not e or not (e[0] or e[1]):
            continue
        norm.append((e[0], e[1], bool(e[2]) if len(e) > 2 else False,
                     e[3] if len(e) > 3 else "",
                     e[4] if len(e) > 4 else "",
                     e[5] if len(e) > 5 else "",
                     e[6] if len(e) > 6 else ""))
    entries = norm[:BALLOON_PLAY_COUNT]
    if not entries:
        return
    if BALLOON.ok:
        with BALLOON.lock:
            BALLOON.q.append(list(entries))
            # 队列最多压 2 组，避免刷新风暴时排队排到天边
            if len(BALLOON.q) > 2:
                del BALLOON.q[:-2]
            if replace:
                BALLOON.interrupt = True
        # 题图在后台先下（不等它，正文先播；图到位了下一帧就画出来）
        _balloon_prefetch(entries)
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
# 【节流 2026-10-07】刷新每轮都弹太吵（那天最多 144 次），普通「更新提醒」限流到 30 分钟一次。
# 【2026-10-07 用户要求「气泡提示缩短到10分钟轮播，轮播内容不能重复」】改成 10 分钟。
# 【2026-10-25 抓取改成 5 分钟一轮后这里保持 10 分钟不变】用户要的是页面「常看常新」，
# 不是通知更频繁 —— 抓取两轮里最多打扰一次，观感和以前一样，只是数据更新更快。
# 注意：「新闻大事」和「我的软件发布」两条**不受这个节流限制**，仍然即时。
# 【2026-10-28 用户要求「设置轮播时间」】这个 10 分钟从写死改成能调：
#   默认还是 10 分钟；托盘右键「轮播间隔」点一下就在 5 / 10 / 30 / 60 分钟之间换，
#   存本机 config.local.json 的 notify 段（跟夜间免打扰、每日速览同一处，不进版本库、不上传）。
#   取值只认这几档：手改配置文件改出 0、负数、字符串、或 7 这种中间值，
#   一律回落默认或就近归到一档 —— 不能让一个坏值把气泡整个静音，或 1 分钟炸一次屏。
TOAST_MIN_CHOICES = (5, 10, 30, 60)      # 托盘「轮播间隔」那几档，单位：分钟
TOAST_MIN_DEFAULT = 10                   # 没设过 / 设坏了的时候用它
NEWS_LOCK = threading.Lock()


def _toast_min_sec():
    """「多久允许自动弹一组」，返回秒 —— 本机设置 toast_min（分钟），坏值回落默认。"""
    try:
        m = int(_notify_cfg().get("toast_min"))
    except (TypeError, ValueError):
        m = TOAST_MIN_DEFAULT
    if m < 1:
        m = TOAST_MIN_DEFAULT
    if m not in TOAST_MIN_CHOICES:
        m = min(TOAST_MIN_CHOICES, key=lambda x: abs(x - m))
    return m * 60


def _toast_min_set(minutes):
    """把「轮播间隔」写进本机设置，返回归好档的分钟数（托盘菜单用它）。"""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        m = TOAST_MIN_DEFAULT
    if m not in TOAST_MIN_CHOICES:
        m = min(TOAST_MIN_CHOICES, key=lambda x: abs(x - m))
    _save_notify_cfg({"toast_min": m})
    return m


# 托盘右键菜单的命令号（TrackPopupMenu 用 TPM_RETURNCMD 直接把选中的这个数还回来）。
# 2026-10-28 之前只有 1=打开、2=退出两个；加了「立即轮播」和「轮播间隔」四档以后
# 退出挪到 9，避免和"立即轮播"撞号 —— _run_cmd 与 _build_menu 都读这张表，改一处就行。
_TRAY_CMD_OPEN = 1
_TRAY_CMD_PLAY = 2
_TRAY_CMD_QUIT = 9
_TRAY_CMD_TOAST_BASE = 20          # 20+i 对应 TOAST_MIN_CHOICES[i]：20=5 分 … 23=60 分
_TRAY_CMD_STYLE_BASE = 30          # 30+i 对应 BALLOON_STYLES[i]：30=自动 31=速递 32=聚光灯
_TRAY_CMD_HOLD = 40                # 「工作时不打扰」开关（勾选=开）：2026-10-30 加的


def _tray_toast_label(minutes):
    """「轮播间隔」子菜单里那一档的文字。60 分钟写成人话：1 小时。"""
    return "1 小时" if minutes >= 60 else "%d 分钟" % minutes


def _has_cjk(s):
    """标题里有没有汉字 —— 用来保证气泡只播中文（用户看不懂英文）。"""
    for ch in (s or ""):
        if "\u4e00" <= ch <= "\u9fff":
            return True
    return False


# ============ 【2026-10-09 用户逐条定稿】气泡的「按栏目配额」挑选 ============
# 用户原话：「热点新闻的气泡推送新闻还是太少！！增加 10 组新闻，涉及头条（要有醒目标识），
#           科技，财经，娱乐等。财经类新闻一定要区别化类，不能错放栏目」
# 以及追加要求：「气泡：不要那句固定字样，哪个栏目就是哪个栏目」。
#
# 规则（2026-10-25 按用户新的口径重排，旧版见下一段注释）：
#   · 一组 BALLOON_PLAY_COUNT=10 条；
#   · 配额：头条 2、国内热榜 3、国际热点 2、科技 1、财经 1、娱乐 1；
#   · 任何一栏当期没有条目，名额顺着 软件 → 热榜 → 国际 → 全部 补下去（**绝不空着**）；
#   · 同一来源默认只取一条（沿用 2026-10-07「快科技不停推送」的修复），
#     实在凑不满才允许同源重复。
#
# 【2026-10-25 为什么重排】用户原话：「其实大部分用户，不会特意去翻新闻看，都是桌面的
# 气泡新闻精准投放。国内国际热点，头条新闻」—— 也就是气泡才是主消费入口，页面是次要的。
# 旧配额（头条2/科技2/财经2/娱乐2 + 软件1 + 热榜1，国际只在剩名额时补 1 条）里，
# 用户点名的三样「国内热点、国际热点、头条」有两样是"看运气补位"的：热榜常年只有 1 条、
# 国际常常 0 条。现在把它们提到配额内并给足名额。
# 旧规则（2026-10-09 需求书《气泡规格-20261009.md》第 2 条）：头条 1~2、科技 ≥2、财经 ≥2、
# 娱乐 ≥2，余下按 软件 → 热榜 → 国际 补足 —— 保留在下面 BALLOON_FILL_ORDER 里当兜底顺序。
BALLOON_CLS_QUOTA = (("头条", 2), ("热榜", 3), ("国际", 2), ("科技", 1), ("财经", 1), ("娱乐", 1))
BALLOON_FILL_ORDER = ("软件", "热榜", "国际")
# 日志与统计时的栏目固定顺序（头条在前，其余按配额与补位顺序，最后是国际/其他兜底）
BALLOON_CLS_ORDER = ("头条", "科技", "财经", "娱乐", "软件", "热榜", "国际", "其他")


def _balloon_cls_key(it):
    """气泡统计/日志用的栏目名：国际条目统一算「国际」（它们自己的栏目名另算）。"""
    cls = (it.get("cls") or "").strip()
    if cls:
        return cls
    return "国际" if it.get("region") == "intl" else "其他"


def _is_intl_item(it):
    return (it.get("region") or "") == "intl"


def _balloon_head(it):
    """气泡第一行 = **这条自己的栏目**（2026-10-09 用户要求，取代原先那句与栏目无关的固定抬头）。

    三条细则：
      · 头条 → `【头条】`（自绘气泡把它画成红色「着色标签」，和页面 .tag.fresh 同一手法，
        见 _wndproc；2026-10-28 起不再整卡变红底）；
      · 热榜 → `【热榜】<平台>热榜 第N`（用户点名「气泡里的热榜条目必须带出处」，
        例如 `【热榜】凤凰网热榜 第4`）；
      · 国际条目 → `国际·<栏目>`，与国内同栏目区分开（栏目名本身仍是该条的栏目）。
    其余情况就是栏目名本身：科技 / 财经 / 娱乐 / 软件 / 要闻（仅国际）/ 体育（仅国际）。
    """
    cls = (it.get("cls") or "").strip()
    # 2026-10-09 追加（用户：「气泡要显示新闻的出处」）：**每条**都要带来源站名。
    # 取值优先 source（热榜聚合出口补的那个字段），退回 label（各源自己的站名/标签）。
    src = (it.get("source") or "").strip() or (it.get("label") or "").strip()
    if cls == "热榜":
        try:
            rk = int(it.get("rank") or 0)
        except Exception:
            rk = 0
        if src and rk:
            return f"【热榜】{src} 第{rk}"
        if src:
            return f"【热榜】{src}"
        return "【热榜】"
    if not cls:
        cls = "要闻" if _is_intl_item(it) else "热点"
    if cls == "头条":
        base = BALLOON_HEADLINE_TAG
    else:
        base = f"国际·{cls}" if _is_intl_item(it) else cls
    return (base + " · " + src) if src else base


def _balloon_time_line(it):
    """App 行右端那段时间：「刚刚 / N 分钟前 / N 小时前 / N 天前」。

    超过 7 天的不显示 —— 那时候用户更关心名次，挂在右上角反而占地方。
    """
    try:
        pub = float(it.get("published") or 0)
        if pub > 0:
            age = max(0.0, time.time() - pub)
            if age < 90:
                return "刚刚"
            if age < 3600:
                return "%d 分钟前" % int(age // 60)
            if age < 86400:
                return "%d 小时前" % int(age // 3600)
            if age < 7 * 86400:
                return "%d 天前" % int(age // 86400)
    except Exception:
        pass
    return ""


def _balloon_meta_line(it):
    """标题下面那行小字：来源 · 热榜名次（没有就空着，不硬编）。"""
    bits = []
    src = (it.get("source") or it.get("label") or "").strip()
    if src:
        bits.append(src)
    try:
        r = int(it.get("rank") or 0)
        if 0 < r < 1000:
            bits.append("第 %d 位" % r)
    except Exception:
        pass
    return " · ".join(bits)


def _balloon_entry(it):
    """把一条新闻转成气泡条目 (第一行, 正文, warn, 链接, 题图, 来源行, 时间行)。"""
    # 国际条目优先用译文（_translate_items 是就地写回 title 的，英文留在 title_en）
    title = it.get("title") or ""
    if _is_intl_item(it) and not it.get("translated") and it.get("title_en"):
        title = it.get("title_en") or title
    return (_balloon_head(it), title[:BALLOON_TITLE_MAX], False, it.get("link") or "",
            it.get("image") or "", _balloon_meta_line(it), _balloon_time_line(it))


def _balloon_cls_tally(picked):
    """按栏目报数，形如 `头条2/科技2/财经2/娱乐2/软件1/热榜1`（用户要求把日志改成按栏目报数）。"""
    cnt = {}
    for it in picked:
        k = _balloon_cls_key(it)
        cnt[k] = cnt.get(k, 0) + 1
    order = [c for c in BALLOON_CLS_ORDER if cnt.get(c)]
    order += [c for c in cnt if c not in BALLOON_CLS_ORDER]
    return "/".join(f"{c}{cnt[c]}" for c in order)


# ============ 跨来源「同一件事」去重（2026-10-10 用户反馈）============
# 用户原话：「气泡显示的新闻好多是重复的，比如说，凤凰发的，其他网站也发，这种情况要杜绝」。
#
# 原来的去重只认**标题一字不差**（见 /api/news 里那句 `if it["title"] in seen`），
# 而同一件事被不同媒体转发时，标题几乎必然不同（改几个字、换个语序、加个前缀/来源），
# 所以那种重复**一条都挡不住**：382 条真实数据里就有 4 对，952 条里有 6 对。
#
# 判据用**真实抓取数据实测标定**（样本 952 条 / 628 条唯一标题，明细见
# logs\同一件事去重-20261010.md）：
#   ① Jaccard(2-gram) >= 0.50                       → 判为同一件事
#   ② Jaccard >= 0.36 且 共有 2-gram >= 10           → 判为同一件事（抓"长标题改写"）
#
# 为什么必须有 ②：广汽本田刹车踏板、一加16电池、缅北电诈那几对**真重复**的分数只有
# 0.385~0.413，光靠 ① 会全漏掉；而唯一一对**误杀**样本（郑钦文 vs 米拉·安德列娃
# 各自晋级中网八强 —— 是两位不同选手，属两条不同新闻）分数 0.438 却只有 7 个共有 gram。
# 用「绝对共有量」正好把这两类分开：真重复的共有 gram 都在 10 个以上，误杀那对只有 7 个。
_SIM_STOP = set("　 \t\r\n，。、；：？！“”‘’（）《》【】—…·,.!?;:\"'()[]<>-—_|/\\")


def _title_norm(t):
    """标题归一化：去掉标点与空白并转小写，只留汉字/字母/数字。"""
    return "".join(ch for ch in (t or "").lower() if ch not in _SIM_STOP)


def _title_grams(t, n=2):
    """标题的 2-gram 集合（中文短标题用 2-gram 最稳）。"""
    s = _title_norm(t)
    if len(s) < n:
        return {s} if s else set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _nums(t):
    """标题里出现的数字（价格/数量/比分/百分比/排名…）。

    **数字不同就一定不是同一件事** —— 这是纯相似度算法最容易踩的坑：
      「一加16售价3999元」vs「一加16售价4299元」两条标题几乎一模一样，
      相似度高达 0.9，但它们是两条不同的新闻；
      「某公司第一季度净利增长15%」vs「…第二季度…」同理。
    所以相似度之外单独设这道闸（2026-10-10 自测抓到的误杀，见测试用例第二节）。
    """
    out, cur = set(), ""
    for ch in _title_norm(t):
        if ch.isdigit() or (ch == "." and cur):
            cur += ch
        elif cur:
            out.add(cur.strip("."))
            cur = ""
    if cur:
        out.add(cur.strip("."))
    return {x for x in out if x}


def _dup_verdict(g1, g2):
    """两串 2-gram 是不是同一件事 —— **阈值只在这里定义一次**。"""
    if not g1 or not g2:
        return False
    inter = len(g1 & g2)
    if not inter:
        return False
    jac = inter / len(g1 | g2)
    return jac >= 0.50 or (jac >= 0.36 and inter >= 10)


def _common_prefix_len(n1, n2):
    """两条归一化标题的最长公共前缀长度（"国常会部署…"两条的前缀是"国常会部署"）。"""
    k = 0
    for a, b in zip(n1 or "", n2 or ""):
        if a != b:
            break
        k += 1
    return k


def _dup_verdict_full(g1, g2, d1, d2, cjk_common=0, is_prefix=False):
    """完整三档判重 —— **阈值只在这个函数和 `_dup_verdict` 里定义**。

    ① `jac >= 0.50`：措辞接近的同一件事；
    ② `jac >= 0.36 且 交集 >= 10`：长标题里大量 2-gram 重合；
    ③ `jac >= 0.15 且 中文 3-gram 交集 >= 2 且 方向词不冲突`：同一件事被写成两句
       完全不同的话（见 `_cjk_trigrams` 上面的注释），靠中文实词重合识别。
    方向词冲突（上调 vs 下调）在三档都先看，方向相反的一律不折叠。

    两道"前缀"闸（`is_prefix` = 一条归一化标题是另一条的前缀）：
      · 前缀 **且长度差 > 4** → 一律不折叠。
        「特斯拉宣布降价」vs「特斯拉宣布降价后车主维权」的 2-gram Jaccard 是 0.545，
        光看第①档一定会折叠 —— 但后者是前者的**后续发展**（车主维权），是两条新闻。
        注释里早就写着要用"长度差 ≤ 4"杜绝这类误杀，可那句话只写在第①档（确定性折叠）
        里，第①档卡掉了、第②档又放进来，等于没挡。现在两道闸都看前缀。
      · 第③档的中文 3-gram 交集 `cjk_common` **先扣掉共同前缀贡献的那部分**。
        「国常会部署稳增长组合拳」vs「国常会部署房地产工作」的共同 3-gram
        （国常会/常会部/会部署）**全部来自共同前缀"国常会部署"**，扣完只剩 0 →
        不折叠（同一主体的两个不同议题，是两条新闻）。
        真重复的共同实词是**散落在标题各处**的（特朗普/乌克兰/总统、英国/动物园/
        老虎/男子），扣完仍然 ≥2 → 折叠。
    """
    if not g1 or not g2:
        return False
    inter = len(g1 & g2) if (g1 and g2) else 0
    if not inter:
        return False
    if is_prefix and abs(len(g1) - len(g2)) > 4:
        return False
    jac = inter / len(g1 | g2)
    if jac >= 0.50 or (jac >= 0.36 and inter >= 10):
        return not _dir_conflict(d1, d2)
    if jac >= 0.15 and cjk_common >= 2:
        return not _dir_conflict(d1, d2)
    return False


# 【2026-10-11 六轮】第三档判据：**中文实词 3-gram 交集**。
# 为什么需要：同一件事被两家媒体写成两句完全不同的话时，2-gram Jaccard 常常只有 0.19~0.31，
#   够不到 0.50 那道闸 —— 真机池子（415 条）里抓到 4 对漏网的真重复：
#     「英国一男子闯入动物园虎区…」vs「英国一野生动物园发生老虎袭人事件 一男子重伤」(0.194)
#     「被拒绝6次之后，特朗普建议乌克兰更换总统…」vs「美乌吵起来了！泽连斯基怒斥…」(0.191)
#     「电视剧《伟大的长征》央视一套开播」vs「仅一小时即拿下全网收视第一！《伟大的长征》开播」(0.185)
#     「支付宝服务升级：失效银行卡可一键换绑或解绑…」vs「支付宝上的失效银行卡 终于可以一键…」(0.308)
#   它们的共同点是"**中文实词**高度重合"（特朗普/乌克兰/总统、英国/动物园/老虎/男子…）。
# 为什么**只取纯中文段**（不含数字/字母）：否则「小米17发布…」和「小米17评测…」会因为
#   共同含「小米1 / 米17」被判成同一件事 —— 那两条是同一个产品的两条不同新闻。
#   真重复的中文实词重合度远高于"同产品不同事件"，这条线正好把它们分开。
_CJK_SEG_RX = re.compile(r"[\u4e00-\u9fa5]{3,}")


def _cjk_trigrams(t):
    """标题里**纯中文连续段**（≥3 字）的 3-gram 集合。"""
    out = set()
    for seg in _CJK_SEG_RX.findall(_title_norm(t or "")):
        for i in range(len(seg) - 2):
            out.add(seg[i:i + 3])
    return out


# 【2026-10-11 六轮】方向词（上调/下调、大涨/大跌…）—— 与数字、序数同一类"互斥信号"。
# 为什么需要：第三档按中文实词重合判重，而「央行下调存款准备金率」与「央行上调存款
#   准备金率」的实词几乎完全一样，只有"下调/上调"两个字不同 —— 不设闸就会把两条
#   **方向相反**的新闻折叠成一条（这是最不能犯的错：用户看到的是"少了一条"）。
_DIR_RX = re.compile(r"上调|下调|上涨|下跌|大涨|大跌|涨停|跌停|增长|下降|增加|减少|"
                     r"提高|降低|加息|降息|升值|贬值|走高|走低|回落|反弹|涨|跌")


def _dirs(t):
    return {m.group(0) for m in _DIR_RX.finditer(_title_norm(t or ""))}


def _dir_conflict(d1, d2):
    """两条的方向词是否**互相冲突**（算法与 `_nums_conflict` 一致：子集关系不算冲突）。"""
    if not d1 or not d2:
        return False
    return not (d1 <= d2 or d2 <= d1)


def _nums_conflict(n1, n2):
    """两串数字是否**互相冲突**。

    只有"各有对方没有的数字"才算冲突（→ 一定不是同一件事）。
    一方是另一方的子集时**不算冲突** —— 那只是其中一条多写了信息：
      ·「…获刑3年半」vs「…致多人送医」      → {20,3} 与 {20}，子集关系，是同一件事；
      ·「2027长春大冬会火种…」vs「长春大冬会火种…」→ {2027} 与 {}，也是同一件事；
      ·「售价3999元」vs「售价4299元」        → {3999} 与 {4299}，互相冲突，**不是**同一件事。
    第一版写成"集合不相等就判冲突"，把上面那两条真重复也挡掉了（8 条降到 6 条，自测抓到）。
    """
    if not n1 or not n2:
        return False
    return not (n1 <= n2 or n2 <= n1)


# 【2026-10-11】序数（第一/第二/一季度/第3轮…）与 `_nums` 是同一类信号：
# 「公司第一季度净利增长15%」vs「公司第二季度净利增长15%」的 2-gram 相似度高达 0.8，
# 只有"第一/第二"这一个字不同 —— 光看相似度一定会被折叠成一条，但它们是两条新闻。
# 所以把序数也做成一道"互相冲突就放行"的闸（自测抓到的误杀，见 verify 第四节）。
_ORDINAL_RX = re.compile(r"第[0-9]+|第[一二三四五六七八九十]+"
                         r"|[0-9]+季度|[一二三四五六七八九十]季度")


def _ordinals(t):
    """标题里出现的序数集合（第一 / 第2 / 一季度 / 第三轮…）。"""
    return {m.group(0) for m in _ORDINAL_RX.finditer(_title_norm(t))}


def _ordinals_conflict(o1, o2):
    """两条标题的序数是否**互相冲突**（算法与 `_nums_conflict` 完全一致）。"""
    if not o1 or not o2:
        return False
    return not (o1 <= o2 or o2 <= o1)


def _same_event(t1, t2):
    """两条标题是不是「同一件事」（不同媒体转发）。纯函数，供单测直接用。"""
    if _nums_conflict(_nums(t1), _nums(t2)):
        return False
    if _ordinals_conflict(_ordinals(t1), _ordinals(t2)):
        return False
    _n1, _n2 = _title_norm(t1), _title_norm(t2)
    _lcp = _common_prefix_len(_n1, _n2)
    _prefix = bool(_n1 and _n2 and (_n1.startswith(_n2) or _n2.startswith(_n1)))
    # 共同中文 3-gram 扣掉"共同前缀"贡献的那部分（前缀长度 L 能贡献 L-2 个 3-gram）
    _cjk_common = len(_cjk_trigrams(t1) & _cjk_trigrams(t2)) - max(0, _lcp - 2)
    return _dup_verdict_full(_title_grams(t1), _title_grams(t2),
                             _dirs(t1), _dirs(t2),
                             cjk_common=_cjk_common, is_prefix=_prefix)


def _dedup_same_event(items, same_wording_only=False, with_also=False):
    """跨来源去重：同一件事只留**最先出现的那一条**，保持原顺序。

    `same_wording_only=True`（2026-10-25 为归档检索加）：只做下面①那一档确定性折叠，
    跳过②「同一事件不同措辞」。归档里同一个词跨 30 天会攒下一批稿子，
    用户明确在搜东西时不能把别家的报道折成一条 —— 与 /api/news 带 q 时不做②是同一条规矩。
    默认 False，既有调用点行为完全不变。

    `with_also=True`（2026-10-25 为「同一件事，几家在报」加）：折叠掉的那些**不丢**，
    挂到留下的这条上 —— `it["also"]`（最多 6 条兄弟稿）+ `it["also_n"]`（几家在报，
    按**不同来源**算，含本条自己）。只有一条时不挂（那是同一家媒体自己的副本，说不上"几家"）。
    判据一个字没改，只是把"顺手扔掉"改成"顺手记一笔"，所以不会引入新的误判。

    两道闸，先硬后软：

    **① 确定性折叠**（2026-10-11 加）—— 同一条新闻的**副本**，判据只有四种，
    全都是"是不是同一条"的事实判断，**不存在把两条不同新闻误判成一条**的可能：
      · 同 `id`；
      · 同 `link`；
      · 归一化标题完全一样（只差标点/大小写/空格的那些，例如
        「国常会：推出稳增长组合拳」与「国常会推出稳增长组合拳」）；
      · 一方归一化标题是另一方的子串、且两者长度差 ≤ 4 个字、数字不冲突
        （同一稿的两种长短写法）。长度差卡到 4 是为了杜绝
        「特斯拉宣布降价」vs「特斯拉宣布降价后车主维权」这类误杀。

    这一道**不看来源**（连同一家媒体自己发的副本也折叠）—— 因为 `_pick_balloon_items`
    凑不满 10 条时会放开「同来源只取一条」（见那里 ④），那时副本就同时进气泡了，
    这正是用户报的「头条和热榜经常重复」：热榜条目被复制进「头条」栏（见 `_collect`），
    副本与原条同 id 同 title，平时靠"同来源只取一条"挡着，一进兜底就漏出来。

    **② 同一事件不同措辞**（原有）—— Jaccard 相似度，阈值只定义在 `_dup_verdict`。
    【2026-10-29 修复】**同源也参与比较**：原先为「同一家媒体自己发的多条不合并」而对
    `klb == lb` 直接 continue，导致同源把同一事件改写两遍时（如凤凰网热榜把
    「中学筛查肺结核共用针头」列了第13名和第18名两条）漏重、双双上屏。放开后由
    `_nums_conflict` / `_ordinals_conflict` / `_dir_conflict` 三道事实闸 + Jaccard /
    中文3-gram 阈值兜底，不会误杀「同产品不同事件 / 不同价格 / 相反涨跌 /
    同一主体不同议题」这类条目；先比长度再算交集，长度差太大直接跳过，避免无谓开销。
    （`with_also` 模式下同源副本因 `labels` 不足两家，也不会被标成"几家在报"，行为一致。）
    """
    kept, keys, owners = [], [], []
    hard_ids, hard_links, hard_norm = {}, {}, {}     # 值 = 折叠到谁身上（with_also 要用）
    buckets = {}                                     # id(owner 条目) -> [owner, [被它折叠掉的兄弟稿]]
    if with_also:
        # 上一次运行挂上去的标注先清掉：这一轮没再折叠到兄弟稿的条目，
        # 不能继续顶着个"N 家在报"（池子每 10 分钟换一批，旧标注会一直赖着）。
        for _it in items:
            _it.pop("also", None)
            _it.pop("also_n", None)

    def _fold(owner, dup_item):
        if not with_also or owner is None or owner is dup_item:
            return                          # 找不到折叠到谁身上：宁可不记，也别记出个 None
        # 条目是 dict（不可哈希），用 id() 当键；这一轮里 owner 一定还活着（都在 items 里）。
        buckets.setdefault(id(owner), [owner, []])[1].append(dup_item)

    for it in items:
        lb = it.get("label") or ""
        title = it.get("title") or ""
        nums = _nums(title)
        ords = _ordinals(title)
        # ---- ① 确定性折叠 ----
        _id = (it.get("id") or "").strip()
        _lk = (it.get("link") or "").strip()
        _nt = _title_norm(title)
        if (_id and _id in hard_ids) or (_lk and _lk in hard_links) \
                or (_nt and _nt in hard_norm):
            _fold(hard_ids.get(_id) or hard_links.get(_lk) or hard_norm[_nt][2], it)
            continue
        _dup_hard = False
        if len(_nt) >= 6:
            for _ot, (_onums, _oords, _owner) in hard_norm.items():
                if len(_ot) < 6:
                    continue
                _lo, _hi = (_nt, _ot) if len(_nt) <= len(_ot) else (_ot, _nt)
                if len(_hi) - len(_lo) > 4 or _lo not in _hi:
                    continue
                if not _nums_conflict(nums, _onums) and not _ordinals_conflict(ords, _oords):
                    _dup_hard = True
                    _fold(_owner, it)
                    break
        if _dup_hard:
            continue
        # ---- ② 同一事件不同措辞 ----
        g = _title_grams(title)
        cg = _cjk_trigrams(title)     # 第三档用：中文实词 3-gram
        dr = _dirs(title)             # 方向词（上调/下调…）
        dup = False
        # same_wording_only 时比对集合给空的 —— ②一条都判不到，①照常生效
        for j, (klb, kg, knums, kords, kcg, kdr, knt) in enumerate(
                (() if same_wording_only else keys)):
            # 【2026-10-29 修复】去掉「同 label 直接跳过」—— 同源同事件改写也要合并，
            # 由下方 _nums_conflict / _ordinals_conflict / _dir_conflict 三道闸兜底防误杀。
            _lcp = _common_prefix_len(_nt, knt)
            _prefix = bool(_nt and knt and (_nt.startswith(knt) or knt.startswith(_nt)))
            _cjk_common = len(cg & kcg) - max(0, _lcp - 2)
            if _nums_conflict(nums, knums):   # 数字互相冲突 → 一定不是同一件事
                continue
            if _ordinals_conflict(ords, kords):   # 序数互相冲突 → 同上
                continue
            if not kg:
                continue
            # 长度预筛：Jaccard 的上界就是 min/max。
            # 第三档的门槛是 0.15，所以上界不到 0.15 的不用算（原来卡的 0.36 会把
            # 「英国一男子闯入虎区」vs「英国一野生动物园发生老虎袭人事件」筛掉）。
            lo, hi = (len(g), len(kg)) if len(g) < len(kg) else (len(kg), len(g))
            if not hi or lo / hi < 0.15:
                continue
            if _dup_verdict_full(g, kg, dr, kdr,
                                 cjk_common=_cjk_common, is_prefix=_prefix):
                dup = True
                _fold(owners[j], it)
                break
        if dup:
            continue
        keys.append((lb, g, nums, ords, cg, dr, _nt))
        owners.append(it)
        if _id:
            hard_ids[_id] = it
        if _lk:
            hard_links[_lk] = it
        if _nt:
            hard_norm[_nt] = (nums, ords, it)
        kept.append(it)
    if with_also:
        for owner, sibs in buckets.values():
            labels = {owner.get("label") or ""} | {(s.get("label") or "") for s in sibs}
            if len(labels) < 2:
                continue            # 只有同一家媒体自己的副本 —— 谈不上"几家在报"
            sibs.sort(key=lambda s: -(int(s.get("published") or 0)))
            owner["also"] = [{"label": s.get("label") or "",
                              "title": s.get("title") or "",
                              "link": s.get("link") or "",
                              "cls": (s.get("cls") or "").strip(),
                              "region": s.get("region") or "",
                              "published": int(s.get("published") or 0)}
                             for s in sibs[:6]]
            owner["also_n"] = len(labels)
            # 【2026-10-25 借鉴 GitHub 同类项目】last30days-skill-cn 的「跨平台同频热点」、
            # imsyy/DailyHotApi 的多平台热榜：同一条新闻**同时上几家实时热榜**，
            # 比"几家媒体都发了稿"更能说明它是真热点（热榜是各站读者投票/点击堆出来的）。
            # 所以单独数一个 `cross_hot` = 这一事件里出现过的**不同热榜平台数**（含本条自己），
            # ≥2 才记（一家上榜说明不了什么）。界面拿它标「全网热榜 ×N」，
            # 气泡拿它做同栏目内的第一排序键（见 `_pick_balloon_items` 的 sort_key）。
            _hot_lb = {(s.get("label") or "") for s in sibs if _is_hot_board(s)}
            if _is_hot_board(owner):
                _hot_lb.add(owner.get("label") or "")
            _hot_lb.discard("")
            if len(_hot_lb) >= 2:
                owner["cross_hot"] = len(_hot_lb)
            else:
                owner.pop("cross_hot", None)
    return kept


def _is_hot_board(it):
    """这条是不是「实时热榜」那一类条目（各站的榜，而不是普通新闻）。
    region 在页面侧是 "hot"，个别合并路径只写 cls，所以两个口径都认。"""
    return (it.get("region") or "") == "hot" or (it.get("cls") or "").strip() == "热榜"


def _annotate_cross_hot(pool):
    """就地给整池子算一遍「同一事件被几家热榜同时收录」（cross_hot）。
    复用 `_dedup_same_event(with_also=True)`，判据一个字不改；返回值不用管，
    标注写在条目字典上（页面 /api/news 也是同一批字典，两边口径天然一致）。"""
    try:
        _dedup_same_event(list(pool), with_also=True)
    except Exception:
        pass
    return pool


# ============ 兴趣投放：点开什么，就多投什么（2026-10-28） ============
# 用户原话：「比如，喜欢科技的就多投这方面的内容，喜欢社会的，就朝社会方面投放」。
# 做法刻意保持"无感"：**不问用户喜欢什么**，只看他真点开了哪一类（气泡上点开、
# 控制台上点开都算）。信号全在本机 %LOCALAPPDATA%\hotnews\interest.json，
# 不联网、不上传、不建账号。
#
# 为什么不是"给每类打标签让用户勾选"：用户明确说过「用户不会去设置这些」；
# 而且勾选出来的偏好是"我以为我喜欢"，点开的行为才是"我真的在看"。
# 为什么不是"全按兴趣排"：头条/热榜/国际是新闻的骨架——只推科技，用户会漏掉
# 真正的大事。所以兴趣**只在原来的配额上挪最多 2 席**（见 _interest_quota）：
#   · 冷启动（没点开过几条）完全不改，默认配额原样；
#   · 某一类明显偏多（≥1.8 倍）→ +1 席；非常偏（≥4 倍）→ +2 席；
#   · 席位先从最冷门的兴趣类里挪，再动热榜（保 2 席）、国际/头条（保 1 席）。
#   · 14 天半衰期：几个月前爱看娱乐，现在不看了，权重自己会淡下去。
INTEREST_PATH = os.path.join(
    os.getenv("LOCALAPPDATA") or tempfile.gettempdir(), "hotnews", "interest.json")
INTEREST_CLASSES = ("科技", "财经", "娱乐", "体育", "软件")
INTEREST_HALF_LIFE_DAYS = 14.0
INTEREST_MIN_EVIDENCE = 3.0        # 至少点开过 3 次（折算后）才开始挪席位，防一次误点改版面
INTEREST_MAX_SHIFT = 2             # 一轮最多挪 2 席，保证"还是新闻，不是兴趣专刊"
_INTEREST_LOCK = threading.Lock()
_INTEREST_CACHE = {"mtime": None, "raw": {}}
_INTEREST_LOGGED = [None]          # 上一次打印过的配额，换了才再打一行（日志不刷屏）


def _interest_class(name):
    """把气泡抬头/栏目名归一成兴趣档的键：'【热榜】'→'热榜'、'国际·要闻'→'要闻'、'热点'→'头条'。"""
    s = (name or "").replace("【", "").replace("】", "").strip()
    if "·" in s:
        s = s.split("·")[-1].strip()
    return {"热点": "头条", "国际": "要闻", "热榜": "热榜"}.get(s, s)


def _interest_weights(now=None):
    """读本机兴趣档 → {栏目: 当前权重}（按 14 天半衰期折算）。坏文件当没有，绝不抛。"""
    now = time.time() if now is None else now
    try:
        mtime = os.stat(INTEREST_PATH).st_mtime
    except OSError:
        return {}
    try:
        with _INTEREST_LOCK:
            if _INTEREST_CACHE["mtime"] != mtime:
                with open(INTEREST_PATH, "r", encoding="utf-8") as f:
                    _INTEREST_CACHE["raw"] = (json.load(f) or {}).get("cls") or {}
                _INTEREST_CACHE["mtime"] = mtime
            raw = dict(_INTEREST_CACHE["raw"])
    except Exception:
        return {}
    out = {}
    for k, v in raw.items():
        try:
            w = float((v or {}).get("w") or 0)
            at = float((v or {}).get("at") or now)
        except Exception:
            continue
        days = max(0.0, (now - at) / 86400.0)
        w *= 0.5 ** (days / INTEREST_HALF_LIFE_DAYS)
        if w > 0.01:
            out[str(k)] = w
    return out


def _interest_bump(name, gain=1.0):
    """给某一类记一分（点开一条 = 1 分）。先折算旧权重再加，写盘用 .tmp + os.replace。"""
    cls = _interest_class(name)
    if not cls:
        return None
    now = time.time()
    w = _interest_weights(now)
    w[cls] = w.get(cls, 0.0) + float(gain)
    data = {"v": 1, "cls": {k: {"w": round(v, 3), "at": now} for k, v in w.items()}}
    try:
        os.makedirs(os.path.dirname(INTEREST_PATH), exist_ok=True)
        tmp = INTEREST_PATH + ".tmp"
        with _INTEREST_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, INTEREST_PATH)
            _INTEREST_CACHE["mtime"] = os.stat(INTEREST_PATH).st_mtime
            _INTEREST_CACHE["raw"] = data["cls"]
        print("[ok] 兴趣 +1：%s（现在 %.1f 分）" % (cls, w[cls]))
        return w[cls]
    except Exception as ex:
        print("[warn] 兴趣档写入失败: %s" % type(ex).__name__)
        return None


def _interest_quota(base=None, weights=None):
    """在默认配额上按兴趣挪最多 2 席（纯函数，便于单测）。

    返回形如 (("头条", 2), ("热榜", 2), ("国际", 2), ("科技", 3), ...)。
    没有兴趣证据 / 兴趣不集中时原样返回 base —— 冷启动的行为与之前完全一致。
    """
    base = tuple(base or BALLOON_CLS_QUOTA)
    w = dict(_interest_weights() if weights is None else weights)
    total = sum(max(0.0, float(w.get(c) or 0)) for c in INTEREST_CLASSES)
    if total < INTEREST_MIN_EVIDENCE:
        return base
    ranked = sorted(INTEREST_CLASSES,
                    key=lambda c: (-float(w.get(c) or 0), INTEREST_CLASSES.index(c)))
    top = ranked[0]
    rest = [c for c in INTEREST_CLASSES if c != top]
    mean_rest = sum(max(0.0, float(w.get(c) or 0)) for c in rest) / max(1, len(rest))
    ratio = max(0.0, float(w.get(top) or 0)) / (mean_rest + 0.25)
    bonus = INTEREST_MAX_SHIFT if ratio >= 4.0 else (1 if ratio >= 1.8 else 0)
    if bonus <= 0:
        return base
    order = [c for c, _n in base]
    q = {c: int(n) for c, n in base}
    q[top] = q.get(top, 0) + bonus
    # 轻度偏好（1.8~4 倍）只从热榜那 3 席里挪 1 席，**不砍**别的兴趣类 ——
    # 用户只是"更爱科技"，不代表从此不看财经/娱乐，版面广度优先。
    # 特别偏（≥4 倍）才先砍最不看的同类栏目，再动热榜/国际/头条（各有底线）。
    if ratio >= 4.0:
        donors = sorted((c for c in rest if q.get(c, 0) > 0),
                        key=lambda c: (float(w.get(c) or 0), INTEREST_CLASSES.index(c)))
    else:
        donors = []
    for d in donors:
        if bonus <= 0:
            break
        take = min(bonus, q[d])
        q[d] -= take
        bonus -= take
    for d, floor in (("热榜", 2), ("国际", 1), ("头条", 1)):
        if bonus <= 0:
            break
        take = min(bonus, max(0, q.get(d, 0) - floor))
        if take > 0:
            q[d] = q.get(d, 0) - take
        bonus -= take
    if top not in order:
        order.append(top)
    return tuple((c, q[c]) for c in order if q.get(c, 0) > 0)


def _pick_balloon_items(fresh, count=None):
    """按栏目配额挑一组气泡条目（纯函数：只读 fresh，不改任何外部状态，便于单测）。

    2026-10-25 加 `count` 参数：默认仍是气泡的 10 条；「今日 60 秒」日报要 15 条，
    用同一个挑选器保证两个出口的口径一致（同一套配额、同一套去重、同一个排序）。

    这是本次「气泡扩容 + 栏目覆盖」的核心：原来只按 国际/国内/热榜 三个板块平分，
    10 个席位里 科技/财经/娱乐 常常一条都轮不到（用户反馈「财经、娱乐看不到」）。

    2026-10-10 追加：进门前先合并「同一件事」—— 气泡只有 10 个席位，
    同一件事占掉两个座位时用户一眼就看出重复（这是用户报的原话场景）。
    """
    cap = int(count or BALLOON_PLAY_COUNT)
    fresh = _dedup_same_event(list(fresh))
    picked, used, labels = [], set(), []

    def take(cands, maxn=1, no_repeat_src=True):
        for it in cands:
            if len(picked) >= cap or maxn <= 0:
                return
            if id(it) in used:
                continue
            _lb = it.get("label") or ""
            if no_repeat_src and _lb and _lb in labels:
                continue
            used.add(id(it))
            if _lb:
                labels.append(_lb)
            picked.append(it)
            maxn -= 1

    def by_cls(name):
        return [x for x in fresh if (x.get("cls") or "") == name]

    def cands(name):
        """配额/补位用的候选集。'热榜' 和 '国际' 是两个跨栏目的口径，得单独定义：
          · 热榜 = 国内热榜（cls 是热榜、但不是国际区的）；
          · 国际 = 国际区的非热榜条目（国际热榜那批归到「热榜」以外的国际口径）。
        两个定义互斥，同一个名额不会因为一条既算热榜又算国际而被占两次。"""
        if name == "热榜":
            return [x for x in fresh if (x.get("cls") or "") == "热榜" and not _is_intl_item(x)]
        if name == "国际":
            return [x for x in fresh if _is_intl_item(x) and (x.get("cls") or "") != "热榜"]
        return by_cls(name)

    def sort_key(x):
        # 【2026-10-25 用户「一定要精准投放，实时，热点，重大」】
        # 第一排序键改成 `cross_hot`（同一条新闻被几家实时热榜同时收录，见 _dedup_same_event），
        # 第二键才是时间。理由：热榜条目的 published 恒为 0（榜接口不给时间），
        # 只按时间排在栏内等于按抓取顺序；而"多家榜同时上榜"是读者用点击投出来的重大信号。
        return (-(int(x.get("cross_hot") or 0)), int(x.get("published") or 0))

    def order(cands_list):
        return sorted(cands_list, key=sort_key, reverse=True)

    # ① 按配额先占位：头条 2 / 国内热榜 3 / 国际 2 / 科技 1 / 财经 1 / 娱乐 1
    #    （2026-10-25 用户「国内国际热点，头条新闻」的新口径，见 BALLOON_CLS_QUOTA 上面那段）
    #    【2026-10-28 兴趣投放】配额先过一遍 _interest_quota：用户点开得多的那一类
    #    多拿 1~2 席，从最冷门的同类栏目里挪（热榜保 2、国际/头条保 1）。
    #    没有任何兴趣证据时它原样返回 BALLOON_CLS_QUOTA —— 冷启动行为不变。
    _quota = _interest_quota()
    if _quota != BALLOON_CLS_QUOTA and _INTEREST_LOGGED[0] != _quota:
        _INTEREST_LOGGED[0] = _quota
        print("[ok] 兴趣投放：配额调整为 %s（默认 %s）"
              % (dict(_quota), dict(BALLOON_CLS_QUOTA)))
    for _cls, _n in _quota:
        if len(picked) >= cap:
            break
        take(order(cands(_cls)), _n)
    # ② 补位第一轮：软件 → 热榜 → 国际，**每个候选栏目最多 1 条**。
    #    哪一栏当期没货，名额就顺着往下走；③④ 再放开条数把席位填满。
    for _cls in BALLOON_FILL_ORDER:
        if len(picked) >= cap:
            break
        take(order(cands(_cls)), 1)
    # ③ 还差就接着补：补位栏目不限 1 条 → 全部条目按时间 → 最后放开"同来源只取一条"凑满
    for _cls in BALLOON_FILL_ORDER:
        if len(picked) >= cap:
            break
        take(order(cands(_cls)), cap - len(picked))
    if len(picked) < cap:
        take(order(fresh), cap - len(picked))
    # ④ 某栏目当期没货 / 同源被去重挡住 → 放开"同来源只取一条"再补满
    if len(picked) < cap:
        take(order(fresh), cap - len(picked), no_repeat_src=False)
    return picked


# ============ 气泡「同一件事不重复投放」的跨轮记忆（2026-10-25 新增） ============
# 用户原话：「右下角，气泡轮播新闻，一定要精准投放，实时，热点，重大，
#             不能几个网站的新闻重复投放」
# 已有的两道闸为什么挡不住这句话：
#   · `_dedup_same_event`（在 `_pick_balloon_items` 进门处）只在**同一轮的 fresh 列表里**
#     合并同一件事 —— 上一轮播过的稿子不在这一轮的列表里，自然挡不住；
#   · `_BALLOON_SHOWN`（在 `_notify_news`）按 `id`/整句标题记账，只能挡**同一条**，
#     挡不住「同一件事换了个网站、标题措辞不同」的那批兄弟稿。
#     而热榜/头条本来就是多家转发的重灾区：同一件事往往分属不同 cls、不同来源，
#     隔 10 分钟又各占一个气泡席位 —— 这就是用户看到的"几个网站重复投放"。
# 所以这里加一份**有时限的事件记忆**：凡真的进了气泡的条目，把它的事件指纹存下来；
# 下一轮挑之前先按同一套判据（`_dup_verdict_full` / `_nums_conflict` / `_ordinals_conflict`）
# 把"和已播事件是同一件事"的条目整个去掉，不进配额、不占席位。
# 判据沿用现有阈值，**没有放宽也没有收紧**，只是把它的作用范围从"本轮"扩到"最近几小时"。
_BALLOON_EVENT_TTL = 6 * 3600    # 记忆窗口：6 小时。同一件事的后续发展（新数字/新序数）
                                 # 按现有判据会被放行，所以窗口长一点也不会压掉真正的新闻。
_BALLOON_EVENT_MAX = 400         # 最多记 400 个事件（10 条/轮 × 几十轮），防止长跑内存涨
_BALLOON_EVENTS = []             # [{at, nt, g, cg, dr, nums, ords, title}]，按写入先后排列
_BALLOON_EVENT_LOCK = threading.Lock()


def _balloon_event_sig(title):
    """一条标题的「事件指纹」，字段与 `_dedup_same_event` 第②档比对的完全一致。"""
    t = title or ""
    return {"nt": _title_norm(t),
            "g": _title_grams(t),
            "cg": _cjk_trigrams(t),
            "dr": _dirs(t),
            "nums": _nums(t),
            "ords": _ordinals(t),
            "title": t[:60]}


def _balloon_same_event(a, b):
    """两个事件指纹是不是同一件事 —— 判据与 `_dedup_same_event` 第②档同源，阈值不外溢。"""
    if not a["nt"] or not b["nt"]:
        return False
    if a["nt"] == b["nt"]:
        return True                              # 归一化后整句一样（只差标点/大小写）
    if _nums_conflict(a["nums"], b["nums"]):     # 数字冲突 → 一定不是同一件事
        return False
    if _ordinals_conflict(a["ords"], b["ords"]):  # 序数冲突 → 同上
        return False
    g1, g2 = a["g"], b["g"]
    if not g1 or not g2:
        return False
    lo, hi = (len(g1), len(g2)) if len(g1) < len(g2) else (len(g2), len(g1))
    if not hi or lo / hi < 0.15:                 # Jaccard 上界预筛，省掉无谓的集合运算
        return False
    _lcp = _common_prefix_len(a["nt"], b["nt"])
    _prefix = bool(a["nt"].startswith(b["nt"]) or b["nt"].startswith(a["nt"]))
    return _dup_verdict_full(g1, g2, a["dr"], b["dr"],
                             cjk_common=len(a["cg"] & b["cg"]) - max(0, _lcp - 2),
                             is_prefix=_prefix)


def _balloon_event_remember(items):
    """把真的播出去的条目记成"已播事件"；顺带清掉超出窗口的旧记录。"""
    now = time.time()
    with _BALLOON_EVENT_LOCK:
        for it in items:
            t = it.get("title") or ""
            if t:
                _BALLOON_EVENTS.append(dict(_balloon_event_sig(t), at=now))
        cutoff = now - _BALLOON_EVENT_TTL
        while _BALLOON_EVENTS and _BALLOON_EVENTS[0]["at"] < cutoff:
            _BALLOON_EVENTS.pop(0)
        if len(_BALLOON_EVENTS) > _BALLOON_EVENT_MAX:
            del _BALLOON_EVENTS[:-_BALLOON_EVENT_MAX]


def _balloon_event_filter(items):
    """去掉与"已播事件"是同一件事的条目 → (留下的, 被去掉的)。记忆为空时原样返回。"""
    with _BALLOON_EVENT_LOCK:
        mem = [m for m in _BALLOON_EVENTS if m["at"] >= time.time() - _BALLOON_EVENT_TTL]
    if not mem:
        return list(items), []
    keep, drop = [], []
    for it in items:
        sig = _balloon_event_sig(it.get("title") or "")
        if any(_balloon_same_event(sig, m) for m in mem):
            drop.append(it)
        else:
            keep.append(it)
    return keep, drop


# ============ 「今日 60 秒」日报 + 通知时段（2026-10-25 借鉴 GitHub 同类项目） ============
# 借鉴来源（本机 github.com 页面打不开，全部走 api.github.com 读 README，实测可用）：
#   · vikiboss/60s（5.8k★）——「每天 60 秒读懂世界」：一条编号清单看完当天要闻，
#     它的典型使用场景里就写着「🔔 桌面通知提醒」；
#   · sansan0/TrendRadar（63k★）—— timeline 时段调度：早晚各一次汇总、
#     以及"什么时间推送 / 什么时间不推送"；
#   · Jesseovo/last30days-skill-cn（1.9k★）—— 跨平台「同频热点」：
#     同一件事在多个平台同时上榜才算真热点（对应上面的 cross_hot）。
# 为什么值得抄：用户 2026-10-25 的原话是「其实大部分用户，不会特意去翻新闻看，
# 都是桌面的气泡新闻精准投放」—— 那就把"一天一屏看完"做成产品的主菜，
# 而不是让用户去翻 200 条列表。
# 与这些项目的关键差别：**全部本机生成**，不接任何第三方日报 API、不上传任何数据
# （60s 那份要请求别人的公共接口，我们直接用本机已经抓好的池子挑）。
DIGEST_COUNT = 15            # 日报条数（60 秒读完的体量）
DIGEST_TIMES_DEFAULT = "08:00,20:00"
NOTIFY_DEFAULTS = {
    "quiet_on": True,               # 夜间免打扰总开关（默认开：22:30–07:30 不弹气泡）
    "quiet_start": "22:30",
    "quiet_end": "07:30",
    "digest_on": True,              # 每日速览播报（默认开）
    "digest_times": DIGEST_TIMES_DEFAULT,
    # 【2026-10-28】「多久允许自动弹一组」的间隔，单位分钟，取值只认 TOAST_MIN_CHOICES 那几档。
    # 必须在这儿挂一份：_save_notify_cfg 落盘时只写 NOTIFY_DEFAULTS 里有的键，
    # 没挂号的键存进去也会被抹掉。
    "toast_min": 10,
    # 【2026-10-30】「工作时不打扰」总开关（见下面 _QUNS_HOLD 那段）。
    # 默认开：锁屏 / 投影演示时自动让路。关掉就是「任何时候照播」——
    # 这一条闸门以前没有任何开关，用户只能翻日志才发现气泡被系统状态静音了一小时。
    "hold_on": True,
}
# 【2026-10-28 用户：「这个样式应该和控制台的颜色同步」】
# 控制台的主题/主色原本只存在浏览器的 localStorage 里，桌面气泡读不到，于是页面切了暗色、
# 气泡还是浅色那套 —— 两边像两个产品。页面现在会把**生效值**回报到 /api/ui，存进本机配置的
# ui 段（和 notify 段一样：config.local.json 只在本机，不进版本库、不上传）。
# theme: auto=跟随 Windows 亮/暗（和控制台默认一致）/ light / dark；accent: default|blue|green|violet|amber。
# 【2026-10-28 用户：「都好看，不行就让用户可以自行选择吧」】
# balloon: auto|live|spot —— 气泡用哪种播报版式（2026-10-29 从五档精简成三档；老配置里的
# card / tv 由 BALLOON_STYLE_LEGACY 归位，不会被当成脏值悄悄改成自动）。默认 auto
# 不用用户先懂设置，
# 想换的人在右下角托盘右键菜单的「播报样式」里挑（2026-10-28 用户：「样式选择的入口放在右下角菜单里吧」
# —— 控制台那份下拉按这句撤掉了，两处入口会互相把对方的选择冲掉），选完立刻播一条给他看。
UI_DEFAULTS = {"theme": "auto", "accent": "default", "balloon": "auto"}
_WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _ui_cfg():
    """读本机生效的界面偏好（config.local.json 的 ui 段），缺项按 UI_DEFAULTS 补齐。"""
    sec = {}
    try:
        sec = dict((CONFIG.get("ui") or {}))
    except Exception:
        sec = {}
    for k, v in UI_DEFAULTS.items():
        sec.setdefault(k, v)
    return sec


def _save_ui_cfg(patch):
    """把界面偏好写进 config.local.json 的 ui 段（只认 UI_DEFAULTS 里挂号的键）。

    页面每次打开都会回报一次生效的主题/主色，值没变就别落盘 ——
    不然光是开着网页就反复重写本机配置，mtime 一直跳，排查时也说不清到底改没改过。
    """
    try:
        cur = {}
        try:
            with open(_LOCAL_CONFIG, "r", encoding="utf-8-sig") as f:
                cur = json.load(f) or {}
        except Exception:
            cur = {}
        if not isinstance(cur, dict):
            cur = {}
        sec = dict(UI_DEFAULTS)
        sec.update(cur.get("ui") or {})
        sec.update(patch or {})
        new = {k: sec[k] for k in UI_DEFAULTS}
        if dict(CONFIG.get("ui") or {}) == new and (cur.get("ui") or {}) == new:
            return True            # 已经是这份值，不写
        cur["ui"] = new
        os.makedirs(os.path.dirname(_LOCAL_CONFIG), exist_ok=True)
        with open(_LOCAL_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        CONFIG.setdefault("ui", {}).update(cur["ui"])
        return True
    except Exception as ex:
        print("[warn] 界面偏好写入失败: %s: %s" % (type(ex).__name__, ex))
        return False


def _notify_cfg():
    """读通知设置（config.local.json 的 notify 段优先），缺项按 NOTIFY_DEFAULTS 补齐。"""
    sec = {}
    try:
        sec = dict((CONFIG.get("notify") or {}))
    except Exception:
        sec = {}
    for k, v in NOTIFY_DEFAULTS.items():
        sec.setdefault(k, v)
    return sec


def _save_notify_cfg(patch):
    """把改动写进本机私有配置 %LOCALAPPDATA%\\hotnews\\config.local.json 的 notify 段。
    只动 notify，其它键（翻译邮箱等本机私有项）原样保留；写失败也不抛，返回 False。"""
    try:
        cur = {}
        try:
            with open(_LOCAL_CONFIG, "r", encoding="utf-8-sig") as f:
                cur = json.load(f) or {}
        except Exception:
            cur = {}
        if not isinstance(cur, dict):
            cur = {}
        sec = dict(NOTIFY_DEFAULTS)          # 先铺默认值：老文件缺键时不再 KeyError
        sec.update(cur.get("notify") or {})
        sec.update(patch or {})
        cur["notify"] = {k: sec[k] for k in NOTIFY_DEFAULTS}
        os.makedirs(os.path.dirname(_LOCAL_CONFIG), exist_ok=True)
        with open(_LOCAL_CONFIG, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        CONFIG.setdefault("notify", {}).update(cur["notify"])
        return True
    except Exception as ex:
        print("[warn] 通知设置写入失败: %s: %s" % (type(ex).__name__, ex))
        return False


def _hm_to_min(s, default=-1):
    """'22:30' → 分钟；不合法返回 default。"""
    try:
        h, m = str(s).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    return default


def _in_quiet_hours(ts=None):
    """现在是不是"夜间免打扰"时段（支持跨零点：22:30 → 次日 07:30）。
    关掉开关 / 时间写错 → 一律判 False（宁可多弹，也不因为配置坏掉而静音整个提醒）。"""
    cfg = _notify_cfg()
    if not cfg.get("quiet_on"):
        return False
    a = _hm_to_min(cfg.get("quiet_start"), -1)
    b = _hm_to_min(cfg.get("quiet_end"), -1)
    if a < 0 or b < 0 or a == b:
        return False
    t = time.localtime(ts)
    now = t.tm_hour * 60 + t.tm_min
    return now >= a or now < b if a > b else a <= now < b


# ============ 「别在工作时弹」：锁屏 / 投影演示（2026-10-28 立，2026-10-30 收窄） ============
# 用户这次的定位是「工作中，轻轻松松就把新闻看了」—— 那气泡就**不能**在开会投影、
# 锁屏走开时冒出来。Windows 自己已经知道这些状态，别另发明判据：
# SHQueryUserNotificationState 就是系统给通知中心的那个答案。
#   1 NOT_PRESENT（锁屏/休眠） 2 BUSY（全屏程序/勿扰） 3 RUNNING_D3D_FULL_SCREEN
#   4 PRESENTATION_MODE（投影/演示） 5 ACCEPTS 6 QUIET_TIME 7 APP（单应用全屏）
# 【2026-10-30 用户：「热点新闻，怎么不自动播报了？」】原来 2/3/6 也在这里挡。本机实测：
# 系统报一次 2（BUSY）就把自动播报连挡 8 轮 —— 10:21 到 11:01 **整整 56 分钟一声不响**，
# 而「长时间全屏干活」正是这个程序的主用法；上一份日志里同样的跳过已经攒了 15 次。
# 所以挡的范围按「弹了有没有用」重切一刀，只留两条：
#   · 1 锁屏/休眠 —— 弹了没人看见；
#   · 4 演示/投影 —— 弹了真会盖住画面，这是唯一会得罪人的。
# 2/3（全屏程序、D3D 全屏游戏）和 6（系统静默时段）不再挡：气泡是**自绘**在右下角的
# 一块 600×220，不抢焦点也不进系统通知中心，全屏看片时它冒出来并不影响画面（用户 2026-10-28
# 原话是「轻轻松松就把新闻看了」，不是「别看」）。真嫌吵的人给一个开关：托盘「工作时不打扰」
# / 控制台同名设置，键 = notify.hold_on。
_QUNS_HOLD = {
    1: "锁屏 / 休眠中",
    4: "演示 / 投影模式",
}
# 状态码的完整对照表留着不删：挡不挡由 _QUNS_HOLD 说了算，但「为什么没播」要说得出是哪一种。
_QUNS_LABEL = {
    0: "取不到状态",
    1: "NOT_PRESENT 锁屏/休眠",
    2: "BUSY 勿扰/全屏程序",
    3: "D3D 全屏游戏或视频",
    4: "PRESENTATION 演示/投影",
    5: "ACCEPTS 正常可弹",
    6: "QUIET_TIME 系统静默时段",
    7: "FULL_SCREEN_APP 单应用全屏",
}


def _hold_reason_from_state(st):
    """QUNS 状态码 → 中文原因；该弹就返回空串（纯函数，单测直接喂状态码）。"""
    try:
        return _QUNS_HOLD.get(int(st or 0), "")
    except Exception:
        return ""


def _quns_state():
    """问 Windows 现在是什么状态；取不到（老系统/非 Windows）返回 0 = 不拦。"""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        st = ctypes.c_int(0)
        fn = ctypes.windll.shell32.SHQueryUserNotificationState
        fn.argtypes = [ctypes.POINTER(ctypes.c_int)]
        fn.restype = ctypes.c_long
        if fn(ctypes.byref(st)) != 0:
            return 0
        return int(st.value)
    except Exception:
        return 0


def _foreground_hint():
    """一句「当时屏幕上是谁」：前台窗口标题 + 类名 + 尺寸，铺满整块屏的再标一下。
    闸门只说「系统认为你正忙」是说不清楚的 —— 谁把它判成正忙、是不是一直是它，
    得靠这一句现场线索。取不到就返回空串，**绝不影响播报**。"""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return "前台=无"
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        cls = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, cls, 64)
        rc = wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(rc))
        w = max(0, int(rc.right) - int(rc.left))
        h = max(0, int(rc.bottom) - int(rc.top))
        full = "·铺满屏" if (w >= u.GetSystemMetrics(0) and h >= u.GetSystemMetrics(1)) else ""
        title = " ".join((buf.value or "").split())[:40] or "(无标题)"
        return "前台=%s｜类=%s｜%dx%d%s" % (title, cls.value or "?", w, h, full)
    except Exception:
        return ""


def _notify_hold_reason():
    """现在不该自动弹气泡的原因（空串 = 可以弹）。手动「立即轮播」**不看**这个。
    开关 = notify.hold_on（托盘「工作时不打扰」/ 控制台同名项），关掉就是一律照播。"""
    if not _notify_cfg().get("hold_on", True):
        return ""
    return _hold_reason_from_state(_quns_state())


def _hold_note(reason=None):
    """把「为什么这一轮没播」说全：原因 + 系统状态码 + 当时前台窗口是谁。
    决定链（GET /api/notify 的 balloon 数组）和日志都吃这一句，用户不用翻文件就能看懂。"""
    r = reason if reason is not None else _notify_hold_reason()
    if not r:
        return ""
    st = _quns_state()
    bits = ["系统状态 %d=%s" % (st, _QUNS_LABEL.get(st, "未知"))]
    hint = _foreground_hint()
    if hint:
        bits.append(hint)
    return "%s（%s）" % (r, "；".join(bits))


def _digest_pool():
    """日报的候选池：三个板块合并 + 就地标一次 cross_hot（与页面 /api/news 同一批字典）。"""
    pool = list(PRELOAD.get("cn") or []) + list(PRELOAD.get("hot") or []) \
        + list(PRELOAD.get("intl") or [])
    pool = [x for x in pool if _has_cjk(x.get("title"))]
    return _annotate_cross_hot(pool)


def _daily_digest(n=DIGEST_COUNT, pool=None):
    """挑「今日 60 秒」的 n 条（复用气泡那套配额 + 同一套判重，口径不分叉）。"""
    pool = pool if pool is not None else _digest_pool()
    if not pool:
        return []
    # 日报要"今天"的：和气泡共用「常看常新」那把尺子（见 _balloon_fresh_cut）——
    # 只走 ① 硬上限 24 小时，不套 ② 的 6 小时优先档：早上 8 点那份要覆盖昨天夜里到
    # 今天的稿子。热榜条目 published 恒为 0（榜接口不给时间），按"新"放行。
    pool, _stale, _held = _balloon_fresh_cut(pool, prefer_hours=None)
    return _pick_balloon_items(pool, count=n)


def _digest_lines(items):
    out = []
    for i, it in enumerate(items, 1):
        title = (it.get("title") or "").strip()
        if _is_intl_item(it) and not it.get("translated") and it.get("title_en"):
            title = (it.get("title_en") or title).strip()
        out.append({"i": i,
                    "cls": _balloon_cls_key(it),
                    "title": title,
                    "source": (it.get("source") or it.get("label") or "").strip(),
                    "link": it.get("link") or "",
                    "published": int(it.get("published") or 0),
                    "cross_hot": int(it.get("cross_hot") or 0)})
    return out


def _digest_text(items):
    """60s 风格的纯文本日报（复制/命令行看都是这一份）。"""
    t = time.localtime()
    head = "今日 60 秒 · %04d年%02d月%02d日 %s" % (
        t.tm_year, t.tm_mon, t.tm_mday, _WEEKDAY_CN[t.tm_wday])
    lines = ["%2d. 【%s】%s（%s）" % (x["i"], x["cls"], x["title"], x["source"])
             for x in _digest_lines(items)]
    lines.append("—— 由「热点新闻」在本机生成 · %02d:%02d · 共 %d 条"
                 % (t.tm_hour, t.tm_min, len(lines)))
    return head, "\n".join(lines)


_DIGEST_LAST_SLOT = [""]        # 已播报过的 "日期-时段"，防同一时段重复弹


def _digest_slot(now=None):
    """现在该不该播日报：命中 digest_times 里某个时刻的 [该时刻, +90 分钟) 就返回那个时刻。
    抓取是 5 分钟一轮，落在窗口里就播一次、同一时段当天只播一次。
    窗口给到 90 分钟而不是 30 分钟，是为了照顾"机器 8 点没开、9 点才开机"的常见情形。"""
    cfg = _notify_cfg()
    if not cfg.get("digest_on"):
        return ""
    t = time.localtime(now)
    now_min = t.tm_hour * 60 + t.tm_min
    for hm in str(cfg.get("digest_times") or "").split(","):
        hm = hm.strip()
        m0 = _hm_to_min(hm, -1)
        if m0 < 0:
            continue
        if m0 <= now_min < m0 + 90:
            return "%04d-%02d-%02d %s" % (t.tm_year, t.tm_mon, t.tm_mday, hm)
    return ""


def _notify_digest(force=False):
    """弹「今日 60 秒」气泡（一条一条轮播，与更新提醒同一套自绘气泡）。"""
    slot = _digest_slot()
    if not slot and not force:
        return False
    if not force and _DIGEST_LAST_SLOT[0] == slot:
        return False
    if _in_quiet_hours() and not force:
        print("[ok] 今日 60 秒：现在处于夜间免打扰时段，跳过播报")
        return False
    if not force:
        _hold = _notify_hold_reason()
        if _hold:
            print("[ok] 今日 60 秒：%s，跳过（下一个时段/下一轮再说）" % _hold_note(_hold))
            return False
    if not force:
        # 刚弹过「更新提醒」/「大事提醒」就让一让，别连着炸两个气泡（同一块屏幕右下角）。
        try:
            if time.time() - max(NEWS_LAST_TOAST, _MAJOR_LAST_TOAST) < 240:
                return False
        except Exception:
            pass
    items = _daily_digest()
    if not items:
        print("[warn] 今日 60 秒：池子里没有可用条目，跳过")
        return False
    head, _txt = _digest_text(items)
    entries = [("%s · %d/%d" % (x["cls"], x["i"], len(items)),
                x["title"][:72], False, x["link"]) for x in _digest_lines(items)]
    _DIGEST_LAST_SLOT[0] = slot
    try:
        _balloon_event_remember(items)      # 日报播过的事件，更新提醒不再重复投放
    except Exception:
        pass
    print("[ok] 今日 60 秒：%s（%d 条，%s）" % (head, len(items), _balloon_cls_tally(items)))
    _show_toast_list(entries)
    return True


# ============ 关键词订阅：已于 2026-10-25 整体取消 ============
# 用户原话：「这个搜索栏要么精简，要么取消，用户是看新闻的，不是来搜索的」，
# 并在追问里选定「两处全收 + 连同订阅功能一起取消」。
# 因此这一节原来的本机词表（%LOCALAPPDATA%\hotnews\keywords_sub.json）、
# /api/keywords 读写接口、气泡的「只在命中时弹」过滤全部删除。
# 老用户机器上残留的 keywords_sub.json 不再被任何代码读取，留着不碍事（本机私有数据，
# 程序不删用户文件，也不上传）。界面与气泡的其余逻辑与此前一致：气泡仍按栏目配额挑 10 条。


# ============ 播报时效规则「常看常新」（2026-10-29 用户点名要一条规则）============
# 用户原话：「自动这个样式，还在播报一天前的旧新闻，一定要定规则：常看常新」
#
# 为什么会播到一天前的：抓取那头的时效**是故意**按栏目放宽的（见 CLS_MAX_AGE_HOURS：
# 科技 96h / 软件 120h / 娱乐 168h），那是 2026-10-07「娱乐新闻少的可怜」换来的 ——
# 控制台页面要"每个栏目都有内容"，放宽没错。但播报这一路以前只看"这条播过没有"，
# **从不看"这条多久了"**。2026-10-29 实测本机池子 200 条：>24h 的 46 条（23%）、
# 最旧 45.7 小时，它们照样能挤进那一轮 10 条 —— 用户看到的就是"一天前的旧新闻"。
#
# 规则四条（页面那一侧一条都不动，页面仍按栏目配额给全）：
#   ① 硬上限 24 小时：发布时间超过 24 小时的条目**一律不进播报候选**；
#   ② 优先 6 小时：6 小时内够一轮（10 条）就只从这 6 小时里挑，凑不满才放宽到 ①；
#   ③ 播过的记 26 小时、且跨重启保留（见 _BALLOON_SHOWN_TTL / _balloon_shown_load）
#      —— 记的时长必须比 ① 长，否则早上播过的一条到夜里"过期"又能重播一遍；
#   ④ 挑不出新鲜的就明说「没有新的可播」，绝不拿旧的凑数（见 _balloon_play_hint）。
# 拿不到发布时间的条目（实时热榜、部分解析器 published=0）按"新"放行：榜单本身就是
# 此刻的热度，一起挡掉等于把「热榜」那两三个配额档清空。「今日 60 秒」日报原来就有
# 同一条 24 小时口径（见 _daily_digest），现在两边共用这一个函数，不会再各写一套。
BALLOON_FRESH_HOURS = 24
BALLOON_FRESH_PREFER_HOURS = 6

# 「挑一组成员 → 当场记进播过名单」必须是一步。开机第一轮和刷新后的自动轮播是两条线程，
# 它们各自挑各自的，中间只隔几毫秒 —— 没有这把锁时两边可能挑中同一批（排序是确定的），
# 用户就会看到两遍同样的 10 条。锁只护挑选/记账，不护后面的窗口播放（那有 BALLOON.lock）。
_BALLOON_PICK_LOCK = threading.Lock()


def _balloon_age_h(it, now=None):
    """这条新闻到现在多少小时了。拿不到发布时间返回 None（调用方按"新"处理）。"""
    try:
        pub = float(it.get("published") or 0)
    except (TypeError, ValueError):
        return None
    if pub <= 0:
        return None
    return max(0.0, (time.time() if now is None else now) - pub) / 3600.0


def _balloon_fresh_cut(items, now=None, prefer_hours=None, prefer_need=None):
    """按上面那四条把候选裁一刀 → (可播的列表, 被 ① 挡掉的条数, 按 ② 让位的条数)。

    `prefer_hours` / `prefer_need` 不给就只做 ①（日报那一套：早上 8 点要的是"昨天到今天
    这一天"，不是"最近 6 小时"）。② 的门槛是"够一轮"：6 小时内的条数 ≥ prefer_need
    才收紧，否则宁可放宽一档也别让气泡空着。
    """
    now = time.time() if now is None else now
    keep, stale = [], 0
    for it in items:
        a = _balloon_age_h(it, now)
        if a is not None and a > BALLOON_FRESH_HOURS:
            stale += 1
            continue
        keep.append((a, it))
    if prefer_hours is None:
        return [it for _a, it in keep], stale, 0
    need = max(1, int(prefer_need if prefer_need is not None else BALLOON_PLAY_COUNT))
    _ph = float(prefer_hours)
    prefer = [it for a, it in keep if a is None or a <= _ph]
    if len(prefer) >= need:
        return prefer, stale, len(keep) - len(prefer)
    return [it for _a, it in keep], stale, 0


def _notify_news(items, force=False):
    """刷新后调用：把「值得播的最新新闻」弹成右下角气泡（更新提醒）。
    首次调用只建立基线不弹；节流窗口内（默认 10 分钟）不重复弹。
    **自动**那一套：有新增优先弹新增；没有新增也持续滚动「已进榜但未播过」的存量
    （2026-10-29 改 A：解决「晚上热榜趋稳后整晚不轮播」）。手动 force 另放开三道闸。

    force=True 是给托盘「立即轮播一轮」/双击图标用的（2026-10-28 用户要求）：
    用户明确开口要看了，就把三道"只有自动才需要"的闸放开 ——
    不看 10 分钟节流、不看夜间免打扰、没有新增就把整个池子当候选。
    **不放开**的三条，手动自动一个样：
      · 只播中文标题（用户 2026-10-07「还是有英文新闻播报」）；
      · 仍按 _pick_balloon_items 的栏目配额挑，凑够 10 条也不许一个栏目占满；
      · **不重复投放**（用户 2026-10-28 原话「每次请求手动轮播的时候，展示的新闻不能一样，
        也不能重复，轮播轮播，就是不能重复，反复」）。轮播的意义是往前滚，不是把同一批
        新闻来回播；手动那一轮播不到新条目就明说"没有新的可播"，绝不拿旧的凑数。
    """
    global NEWS_BASELINE, NEWS_LAST_TOAST
    _prev_toast = NEWS_LAST_TOAST        # 手动那一轮没播出东西时要把它退回去
    if not items:
        return
    new_ids = {it.get("id") for it in items if it.get("id")}
    if not new_ids:
        return
    with NEWS_LOCK:
        if not NEWS_BASELINE:
            NEWS_BASELINE.update(new_ids)
            if not force:
                return                  # 自动那一套：第一趟只建基线，不惊动用户
        added = new_ids - NEWS_BASELINE
        NEWS_BASELINE.update(new_ids)
        if len(NEWS_BASELINE) > 4000:
            NEWS_BASELINE = set(list(NEWS_BASELINE)[-2000:])
    if not added:
        if not force:
            # 【2026-10-29 改 A】自动轮播持续滚动「已进榜但未播过」的存量，
            # 不再只在有新进榜时才弹（热榜到晚间趋稳、无新进榜 → 旧逻辑整晚静默）。
            # 前置的 10 分钟节流 / 夜间免打扰 / 工作不打扰三道闸照常生效，
            # 后续常看常新(24h) / 未播(26h) / 同事件(6h) 过滤也照常生效；
            # 真正的新增集合 added 仍留给日志「本次共新增 N 条」用，候选池改用整池。
            pass
        else:
            added = new_ids             # 手动：没有"新增"就把整池当候选
    # 候选池：无新增时自动/手动都改用整池（手动上一句已把 added 置为整池，这里兜住自动）；
    # 有新增时仍是新增集（优先弹新进榜，旧行为不变）。
    _cand_ids = new_ids if not added else added
    now = time.time()
    if not force and now - NEWS_LAST_TOAST < _toast_min_sec():
        return
    # 【2026-10-25 借鉴 TrendRadar 的时段调度】夜间免打扰：默认 22:30–07:30 不弹气泡。
    # 只挡"主动弹"的这几类提醒，页面本身照常刷新；开关在右侧「程序设置」里，随时可关。
    # 手动那一轮不挡：深夜里主动双击托盘，就是要现在看。
    if not force and _in_quiet_hours(now):
        return
    # 【2026-10-28 工作中别打断 → 2026-10-30 收窄】现在只有**锁屏**（弹了没人看见）和
    # **投影/演示**（真会盖住画面）挡这一轮；全屏程序、系统勿扰、静默时段都照播。
    # 跳过不重置 NEWS_LAST_TOAST（下一轮还是按原来的节流窗口走），也不用补弹：
    # 下一轮刷新自然会带着新条目再来，条目不会因为让路而丢掉。
    # 决定链里带上「系统状态码 + 当时前台窗口是谁」—— 只写一句「系统认为你正忙」，
    # 用户下次还是不知道该找谁（2026-10-30 那次哑了 56 分钟就是这么查的）。
    if not force:
        _hold = _notify_hold_reason()
        if _hold:
            _balloon_trace("跳过：%s｜自动气泡不打扰，手动轮播不受影响" % _hold_note(_hold))
            return
    NEWS_LAST_TOAST = now
    fresh = [it for it in items if it.get("id") in _cand_ids]
    # 先给整池子标一遍「同一条新闻被几家实时热榜同时收录」（cross_hot），
    # 下面 `_pick_balloon_items` 拿它当第一排序键 —— 多家榜同上榜 = 用户要的"热点、重大"。
    _annotate_cross_hot(items)
    # 【2026-10-07 用户反馈「还是有英文新闻播报」】气泡里**只留中文条目**。
    # 国际热榜里曾有境外英文源，之前"优先中文"只是排序、挡不住它
    # 从热榜那 2 个名额挤进来。这里直接按"标题有没有汉字"过滤，最干脆。
    fresh = [x for x in fresh if _has_cjk(x.get("title"))]
    if not fresh:
        _balloon_trace("跳过：本轮新增 %d 条没有中文标题（气泡只播中文）" % len(added))
        if force:
            NEWS_LAST_TOAST = _prev_toast
            _balloon_play_hint("没有能播的条目",
                               "候选里没有中文标题的条目。等下一轮抓取好了再双击托盘。")
        return
    # 【2026-10-29 用户：「一定要定规则：常看常新」】规则 ①：先把超过 24 小时的一律挡掉。
    # ②（6 小时优先）**不在这里做**：要先扣掉「播过的」「同一件事的」，再收紧到 6 小时。
    # 反过来的话，6 小时里恰好凑够一批但全都播过时，会误判「没有新的可播」，
    # 而 6~24 小时里明明还有没播过的 —— 手动双击托盘会一连几个小时都是同一句提示。
    fresh, _stale, _ = _balloon_fresh_cut(fresh, prefer_hours=None)
    if _stale:
        _balloon_trace("挡掉 %d 条超过 %d 小时的旧闻（常看常新；页面照常留着，只是不播）"
                       % (_stale, BALLOON_FRESH_HOURS))
    if not fresh:
        _balloon_trace("跳过：候选里没有 %d 小时内的新闻（常看常新，不播旧的凑数）"
                       % BALLOON_FRESH_HOURS)
        if force:
            with NEWS_LOCK:
                NEWS_LAST_TOAST = _prev_toast
            _balloon_play_hint("没有新的可播",
                               "池子里没有 %d 小时内的新闻了。常看常新：不拿旧闻凑数，"
                               "等新稿抓进来再双击托盘。" % BALLOON_FRESH_HOURS)
        return
    # 【2026-10-25 用户要求「用户是看新闻的，不是来搜索的」】原来的「气泡只在命中时弹」
    # （按订阅词过滤）连同订阅功能一起取消了 —— 界面入口没了以后，这个过滤会变成
    # "用户没设过词却一直被静音"的暗坑，所以这里不再过滤，直接往下走。
    # 【2026-10-07 用户要求「轮播内容不能重复」】优先播没播过的条目。
    # 【2026-10-25 待办 #65】"全都播过就整份清空重来"改成了**按条记 26 小时**：
    # 本轮新增的一条都没播过时才往下走，都播过就这一轮不弹（下一句留记录）。
    # 为什么不再清空：清空等于把几千条历史一起忘掉，隔几天又冒回新增列表的老新闻
    # 立刻能再投一次 —— 那是用户明确反对的"重复投放"，闸不能自己拆自己。
    # 【2026-10-28 用户点名】这道闸**手动那一轮同样生效**：
    # "轮播轮播，就是不能重复，反复" —— 原来 force 会在这里放行旧的凑数，正好是他最烦的
    # 那种"每次双击都是同几条"。改成播不出就明说播不出（见 _balloon_play_hint）。
    # 【2026-10-29】从这一句起直到记账都在 _BALLOON_PICK_LOCK 里：开机第一轮和刷新后的
    # 自动轮播是两条线程，各自挑各自的会挑中同一批（排序确定），用户就看到两遍。
    with _BALLOON_PICK_LOCK:
        try:
            _seen = _balloon_seen()
            _unseen = [x for x in fresh if (x.get("id") or x.get("title")) not in _seen]
        except Exception:
            _unseen = list(fresh)
        if not _unseen:
            _balloon_trace("跳过：%d 条最近都播过（不重复投放%s）"
                           % (len(fresh), "，手动那一轮也不拿旧的凑" if force else ""))
            if force:
                with NEWS_LOCK:
                    NEWS_LAST_TOAST = _prev_toast
                _balloon_play_hint("没有新的可播",
                                   "这 %d 条最近都播过了。轮播不重播旧的，等新新闻抓进来再双击托盘。"
                                   % len(fresh))
            return
        fresh = _unseen
        # 【2026-10-25 用户要求「不能几个网站的新闻重复投放」】跨轮"同一件事"闸。
        # 上一轮气泡播过的那件事，这一轮别的网站的兄弟稿（标题措辞不同、id 也不同）
        # 不能再占席位 —— 判据与本轮内部去重完全相同，只是把视野扩到最近 6 小时。
        # 全被挡掉时**这一轮就不弹**：宁缺毋滥，用户要的是精准投放，不是每 10 分钟看一遍旧闻。
        # 【2026-10-28】手动那一轮同样不放开，理由跟上面那道闸一样。
        try:
            _novel, _dupes = _balloon_event_filter(fresh)
        except Exception:
            _novel, _dupes = fresh, []
        if _dupes:
            _balloon_trace("去掉 %d 条「已播事件的别家稿」，本轮可播 %d 条"
                           % (len(_dupes), len(_novel)))
        if not _novel:
            _balloon_trace("跳过：%d 条都是已播过的同一件事（不重复投放）" % len(fresh))
            if force:
                with NEWS_LOCK:
                    NEWS_LAST_TOAST = _prev_toast
                _balloon_play_hint("没有新的可播",
                                   "这几条讲的都是播过的那件事。等新事件抓进来再双击托盘。")
            return
        fresh = _novel
        # 规则 ②：在"24 小时内、没播过、也不是同一件事"的候选里，6 小时内够一轮就只播 6 小时。
        fresh, _s2, _held = _balloon_fresh_cut(fresh, prefer_hours=BALLOON_FRESH_PREFER_HOURS,
                                              prefer_need=BALLOON_PLAY_COUNT)
        if _held:
            _balloon_trace("这一轮只播 %d 小时内的：%d 条当天稍旧的自动让位"
                           % (BALLOON_FRESH_PREFER_HOURS, _held))
        # 【2026-10-09 用户要求「增加 10 组新闻」「哪个栏目就是哪个栏目」】
        # 挑选逻辑整段换成 _pick_balloon_items（按栏目配额：头条/科技/财经/娱乐 + 软件/热榜/国际补足），
        # 第一行改成这条自己的栏目名（_balloon_head），热榜那条还带平台出处。
        # 旧逻辑（国际/国内/热榜 三个板块平分 6 席）已删除：那样 10 席里科技/财经/娱乐 常常一条都轮不到。
        picked = _pick_balloon_items(fresh)
        if not picked:
            _balloon_trace("跳过：%d 条候选按栏目配额没挑出可播的" % len(fresh))
            if force:
                with NEWS_LOCK:
                    NEWS_LAST_TOAST = _prev_toast
                _balloon_play_hint("没有新的可播",
                                   "%d 条候选按栏目配额没凑出能播的一轮。等新新闻抓进来再双击托盘。"
                                   % len(fresh))
            return
        # 记录已播，保证"轮播内容不能重复"（按条记 26 小时、跨重启保留，见 _balloon_mark_shown）
        # 【2026-10-29】手动那一轮 force 落盘：不等 60 秒节流，用户刚看到的这几条当场写进名单。
        # 记账必须和挑选同在一把锁里：另一条线程紧跟着挑时，这批已经算"播过"。
        try:
            _balloon_mark_shown([_it.get("id") or _it.get("title") for _it in picked], force=force)
        except Exception:
            pass
        # 【2026-10-25】同一批条目还要按"事件"记一笔：下一轮别的网站的兄弟稿才会被挡掉。
        try:
            _balloon_event_remember(picked)
        except Exception:
            pass
    entries = [_balloon_entry(it) for it in picked[:BALLOON_PLAY_COUNT]]
    # 「本次共新增 N 条」这句只在自动那一套用：手动轮播的 added 是整个池子，
    # 挂上去会变成"本次共新增 260 条"，用户会以为一分钟里真冒出来 260 条新闻。
    if not force and len(added) > len(entries):
        _e = entries[-1]
        entries[-1] = (_e[0], _e[1] + f"　（本次共新增 {len(added)} 条）") + tuple(_e[2:])
    # 【2026-10-09 用户要求】日志口径改成**按栏目报数**，形如
    #   滚动播放 10 条（头条2/科技2/财经2/娱乐2/软件1/热榜1）
    _balloon_trace("滚动播放 %d 条（%s）"
                   % (len(entries), _balloon_cls_tally(picked)))
    _show_toast_list(entries, replace=force)


def _balloon_play_hint(title, body):
    """手动那一轮**播不出东西**时给的一条回执。

    2026-10-28 用户原话：「每次请求手动轮播的时候，展示的新闻不能一样，也不能重复，
    轮播轮播，就是不能重复，反复」。所以手动不许拿播过的旧的凑数；但用户点了托盘总得
    有个反应，不然就成了"双击没反应 = 功能坏了"。这条回执就是那个反应：**说清楚为什么没有**。
    """
    try:
        _show_toast(title, body)
    except Exception as ex:
        print("[warn] 轮播回执发不出去: %s: %s" % (type(ex).__name__, ex))


def _balloon_play_now(source="托盘"):
    """「立即轮播一轮」：不等下一轮刷新、不等节流，现在就把当前池子挑 10 条播出来。

    入口只有托盘两处（2026-10-28 用户选的口径）：右键菜单「立即轮播一轮」、双击托盘图标。
    为什么要另起线程 —— 这里是**被 Win32 消息线程调用的**（托盘那条回调），
    而下面 `_notify_news(force=True)` 要对整个池子做 cross_hot 标注 + 相似度去重 + 配额挑选，
    几百条量级要跑几百毫秒到几秒；卡在消息线程里就是"鼠标一直转"（今天刚修的那个 bug
    同一个成因，别再犯第二次）。
    抓取、去重、写已播记录这些一律复用自动那一套的函数，只是把三道"自动才需要"的闸放开。
    """
    try:
        items = (list(PRELOAD.get("cn") or []) + list(PRELOAD.get("hot") or [])
                 + list(PRELOAD.get("intl") or []))
    except Exception:
        items = []
    if not items:
        # 池子还空着：开机后第一轮没抓完（国内约 30 秒、国际约 40~49 秒实测），
        # 或者程序一直在后台、页面从没打开过。这时候**必须给一句话**，
        # 不然用户双击完什么都没发生，只会认定"这功能坏了"。
        print("[ok] %s：立即轮播 —— 池子还空着，先去踢一轮抓取" % source)
        _show_toast("新闻还在抓", "抓好第一轮会自动播出来，稍等半分钟。")
        for _r in ("hot", "cn", "intl"):
            try:
                _kick_collect(_r)
            except Exception:
                pass
        return
    print("[ok] %s：立即轮播一轮（候选 %d 条）" % (source, len(items)))
    threading.Thread(target=_notify_news, args=(items,),
                     kwargs={"force": True}, daemon=True).start()


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

# 【2026-10-10】娱乐栏目的「外国具体对象」总表 = data/keywords.txt 的 `[ent_foreign]`
# + 代码里那张补充表（`_ENT_FOREIGN_EXTRA`，定义在国际栏过滤那一段）。
# 合并放在这里而不是那里，是因为关键词表在本行才载入（见上面的顺序说明）。
_ENT_FOREIGN_ALL = frozenset(ENT_FOREIGN_MARKERS) | _ENT_FOREIGN_EXTRA

# ============ 【2026-10-11 三轮】关键字计数判据 ============
# 用户原话：「应该是关键字过滤，比如中文名字过滤，国外地名超过几个过滤」。
# 三组表都在 data/keywords.txt：
#   [foreign_places] 外国地名/机构（国家、城市、国际组织、外国政府机关、外国政要、外媒）
#   [cn_subjects]    中国主体（中国机构、行政区划、军队、国内特有事物）
#   [cn_surnames]    中国姓氏（「姓 + 1~2 个汉字」算一个中国信号：郑钦文/王娇萍/周先旺）
#
# ⚠ 品牌不进 [foreign_places]：苹果/特斯拉/英伟达是外国品牌不代表新闻发生在国外，
#   「广汽埃安 N60 新增苹果 CarPlay」就是被它坑过的。
FOREIGN_PLACES = _kw("foreign_places") | _kw("foreign_places_extra")
CN_SUBJECTS = _kw("cn_subjects") | _kw("cn_subjects_extra")
CN_SURNAMES = _kw("cn_surnames")
# 中国科技品牌（[cn_tech]）：也是**中国主体**。「小鹏集团官宣将携物理 AI 全品类产品亮相
# 巴黎车展」讲的是中国公司去欧洲，不能因为「巴黎」两个字就转去国际栏。
# ⚠ 只取**品牌名**那部分：[cn_tech] 里还有「中国/我国/国产/国内/省长/四部门…」（已由
#   [cn_subjects] 计过一次，重复计会把「白宫…曾在中国生活三年」这类真外国稿压回国内），
#   以及「坦克/澎湃/理想/长安/中兴/大模型/亿元…」这类**通用词**（「一辆坦克」会被算成中国分）。
_CN_BRAND_AMBIGUOUS = frozenset({
    "大模型", "国产化", "自主品牌", "自主可控", "亿元", "万元",
    "坦克", "澎湃", "理想", "长安", "中兴", "鸿蒙", "麒麟", "深蓝", "极狐", "哪吒",
})
CN_BRANDS_FOR_SCOPE = _kw("cn_tech") - _CN_BRAND_AMBIGUOUS - _kw("cn_subjects")
# 判「国际」需要的最少「外国分」。
# 【实测】阈值 = 2 时国内栏转国际 **0 条**（国内媒体报的国际新闻一条都进不了国际栏，
# 等于关掉这个能力）；阈值 = 1 时 9 条且样本合理。故取 1。
# 想更严格就改成 4（= 至少要有一个"政权中枢"级的外国信号）—— 但先看清上面这个数字。
INTL_PLACE_MIN = 1

# 【2026-10-11 四轮】加权：不再"每个词 1 分、打平就判国内"。
# 起因（用户截图）：国内头条里还留着「从选票到权力：德法意极右翼政党的不同制度路径」
# 「人物｜白宫新任发言人：曾在中国生活三年…」—— 后一条「白宫」与「中国」各 1 分打平，
# 平局按国内处理，于是一条明显的美国新闻留在了国内头条。
# 权重表：
#   4 分 = 外国政权中枢/国际组织/外国政要（[foreign_power]，加价表）
#         中国党政一把手/外事人名（[cn_leaders]）
#   2 分 = 普通外国地名/机构（[foreign_places] + [foreign_places_extra]）
#         中国机构/行政区划/国内特有事物（[cn_subjects]）
#   1 分 = 中文人名（姓 + 1~2 字）
FOREIGN_POWER = _kw("foreign_power")
CN_LEADERS = _kw("cn_leaders")
W_FOREIGN_PLACE = 2
W_FOREIGN_BONUS = 2     # [foreign_power] 里的词再额外加这么多（它们同时在 [foreign_places] 里）
W_CN_SUBJECT = 2
W_CN_LEADER = 4
W_CN_NAME = 1
# `[cn_subjects]` 里这些是**通用名词**，外国稿里同样会出现（「泰国东北部社区」「非洲大陆」
# 「日本本土」），当中国信号用会误伤 —— 真机标定样本：
# 「走进泰国东北部社区 感受伊桑文化的古朴与活力」是国际在线的**境外见闻稿**，
# 靠「东北」+「社区」凑出 2 分就被判回国内。计分时把它们排除。
_CN_GENERIC = frozenset({
    "社区", "村民", "村", "镇", "乡", "街道", "国内", "大陆", "本土",
    "东北", "西部", "中部", "老区", "县域",
})
# 姓后面紧跟这些字，基本是虚词/动词短语而不是人名 ——「曾在中国生活三年」的「曾在」、
# 「上周还不在候选名单上」的「周还」都曾被当成中文人名，白送一个中国分。
_CN_NAME_STOP = frozenset(
    "在于是的了和与为有这不我那大小中上下多少着过起来去出进开关把被让从向往到至"
    "如若因但却则便即才又更最没无别要想会能可应该须得来对就同跟以由还也都选"
)

# 中国姓氏 + 1~2 个汉字 → 一个中国信号。长姓优先，避免「欧阳」被「欧」抢。
# 【2026-10-12 回归断言修正】拆成「多字姓 / 单字姓」两组，单字姓加否定后顾：
# 单字姓前面出现「将/处/对/关/由/等/高/低/至/在/位/易/用/诸」这类字，几乎必然是
# 介词或动词用法（「将于」「对于」「处于」「高于」「位于」「易于」），不是人名。
# 起因：「俄罗斯总统助理将于近期访华」的「将于」被当成姓「于」，白送 1 分中国，
# 用户 2026-10-07 点名过的外交新闻又被平局规则打回国内（回归，被断言抓住）。
_CN_SURN_N = "|".join(sorted({s for s in CN_SURNAMES if len(s) > 1}, key=len, reverse=True))
_CN_SURN_1 = "|".join(sorted({s for s in CN_SURNAMES if len(s) == 1}))
_CN_NAME_RX = re.compile(
    r"(?:%s)[\u4e00-\u9fa5]{1,2}|(?<![将处于对关由等高低至在位易用诸隶适])"
    r"(?:%s)[\u4e00-\u9fa5]{1,2}" % (_CN_SURN_N, _CN_SURN_1)
)

# 【2026-10-12 用户口径】外交新闻 = 国际新闻：外国政要「访华」，主体在外国、中国在
# 客体位 —— 计分表里「俄罗斯(2)」对「中国(2)」打平就被平局规则留在国内（回归案例：
# 「俄罗斯总统助理将于近期访华」）。国际栏出口 _NEWS_CN_OBJECT_RX 早已按「客体式豁免」
# 把这类稿子留在国际；分流侧必须同等认，否则两套口径打架（用户被这类打架咬过两次）。
# 规则：命中访华句式 **且** 外国分里至少有一个正经外国主体（≥一档地名分）→ 判国际。
_DIPLO_VISIT_CN_RX = re.compile(r"访华|访京|到访中国|访问中国|对华国事访问")
# 标题**以单字国名简称开头**时的外国信号（「美大规模枪击案致9死」「法媒：…」「欧企…」）。
# 中文新闻标题大量用这种缩写，而单字词不能整表收（「美食/美女/法官/亚军/加油」都会被误命中），
# 所以只认**开头位置** + 一张"新闻名词"后视表。真机标定：国内热榜里留着的
# 「美大规模枪击案致9死，8人为亲属…」就是靠这一条拦下的。
_FOREIGN_ABBR_HEAD_RX = re.compile(
    r"^(?:美|英|法|德|俄|日|韩|印|泰|越|菲|乌|以|伊|澳|加|欧)"
    r"(?=校|军|企|股|债|媒|总|国|方|联|洲)"
)


def _scope_scores(title):
    """给标题打「中国分」「外国分」（同一个词重复出现只算一次）。

    返回 (中国得分, 外国得分)，权重见上面那一段。
    """
    t = title or ""
    if not t:
        return 0, 0
    cn_n = W_CN_SUBJECT * len({w for w in CN_SUBJECTS if w in t and w not in _CN_GENERIC})
    cn_n += W_CN_SUBJECT * len({w for w in CN_BRANDS_FOR_SCOPE if w in t})
    cn_n += W_CN_LEADER * len({w for w in CN_LEADERS if w in t})
    for m in _CN_NAME_RX.finditer(t):
        if len(m.group(0)) > 1 and m.group(0)[1] not in _CN_NAME_STOP:
            cn_n += W_CN_NAME
            break
    f_n = W_FOREIGN_PLACE * len({w for w in FOREIGN_PLACES if w in t})
    f_n += W_FOREIGN_BONUS * len({w for w in FOREIGN_POWER if w in t})
    if _FOREIGN_ABBR_HEAD_RX.search(t):
        f_n += W_FOREIGN_PLACE
    return cn_n, f_n

# 【2026-10-11 用户反馈「所有栏目的新闻，都存在国内和国际的新闻不能正确识别」】
# 根因：判定国别一直在**猜** —— `[domestic]` 词表里躺着「中国」两个字，于是
# CGTN / 新华社国际 / 中新网国际 这些**国际频道**报道「中国参与国际事务」的稿子，
# 只要标题出现「中国」就被判成国内、从国际栏剔掉。
# 实测（134 条真实国际稿）：误杀 36 条；娱乐 13 条被砍到只剩 2 条。
#   误杀样本：CGTN「China warns Philippines」、Meta 禁字节跳动、派拉蒙收购华纳、
#             新华社国际体育的中网赛事。
# 改法：**信源确定性** —— 国际频道（channel 以 intl- 开头）来的就是国际稿，
# 这**是事实不是猜测**。只留一张极小的安全网 [intl_local_only]（村委会/春晚/中超…
# 这类纯国内地方事务，国际频道不会发）。
#
# 同时给 `cn-intl-news`（国内媒体报的国际新闻）**免二次判定**：它已经过
# `_split_cn_scope` 那道「像国际且不像国内」才转过来的，再叠加一层严格判定就是
# 两套分类器互相打架 —— 实测把「Meta 禁字节跳动」「派拉蒙 1100 亿美元收购华纳」
# 这类纯国际稿又判回国内砍掉。这也是「识别错」的直接来源之一。
INTL_CHANNEL_LOCAL_ONLY = _kw("intl_local_only")


def _looks_ent_foreign(title):
    """娱乐栏目专用的「是不是国际」判定：必须有外国具体对象，光有外国地名不算。

    【2026-10-11 修口径不一致】原来这里只查 `ENT_FOREIGN_MARKERS`（纯 keywords.txt），
    而出口那道 `_is_domestic_for_intl` 查的是 `_ENT_FOREIGN_ALL`（= 关键词表 ∪
    `ENT_FOREIGN_MARKERS` 之外的代码兜底表 `_ENT_FOREIGN_EXTRA`）。同一条稿子
    「分流时」和「出口时」用的不是同一张表 —— 这正是用户反复看到的
    「两处判定互相打架」的其中一处。现在两处都查 `_ENT_FOREIGN_ALL`。
    """
    t = title or ""
    return any(k in t for k in _ENT_FOREIGN_ALL)


# 娱乐·**中国文娱主体**（命中即认定是国内娱乐，不转国际栏）。
# 为什么要单独一张：光靠 `_looks_domestic` 里的通用国内词挡不住娱乐稿 ——
# 实测国内娱乐 34 条里 `_looks_domestic` 只认出 2 条（"央视一套开播""梦圆中国电影"），
# 「第35届飞天奖揭晓」「国庆档票房」「优酷制播」这类**明摆着的国内影视**它一个都不认。
# 中国艺人名字是枚举不完的，所以这里**只登记"国内平台 / 国内奖项 / 国内档期 /
# 国内影视公司"**这类**机构与事件名** —— 它们不会出现在真国际娱乐稿的标题里。
_ENT_CN_SUBJECT_RX = re.compile(
    r"国产|华语|内地|央视|卫视|春晚|金鸡|百花|华表|飞天奖|金鹰|白玉兰|"
    r"国庆档|春节档|暑期档|贺岁档|豆瓣|猫眼|灯塔专业版|中影|华夏电影|"
    r"万达影业|博纳|光线传媒|华谊|阿里影业|腾讯视频|爱奇艺|优酷|芒果TV|"
    r"B站|bilibili|横店|北影节|上影节|中国电影|中国电视剧|中国影视")


# 娱乐·**英文稿的国际信号**（外国地名 / 国籍 / 国际奖项 / 国际机构）。
# 为什么需要：国际栏娱乐的直供源只有 CGTN 文化（英文），"拦国内"靠的是中国主体判据，
#   但**判不出中国主体 ≠ 就是国际** —— 真机里
#   「How digital intelligence makes urban governance smarter」（数字智能让城市治理更聪明）
#   讲的是中国城市，标题上一个中国词都没有，光靠主体判据会漏进国际栏。
# 所以英文稿还要**正面证明自己是国际的**：命中下面任一项（或 `_ENT_FOREIGN_ALL` 里的
#   文娱对象）才算。中英文分开处理：中文稿（境内源转来的）仍只认文娱对象表。
# ⚠ 刻意**不收** Chinese / China —— 那是"中国主体"，归 `_INTL_ENT_DOM_RX` 管。
_ENT_INTL_SIGNAL_RX = re.compile(
    r"\b(Nobel|Pulitzer|Booker|Academy Award|Emmy|Grammy|Golden Globe|BAFTA|"
    r"Cannes|Venice|Berlinale|Sundance|Oscar|Tony|Annie Award)\b"
    r"|\b(Canadian|American|British|French|German|Italian|Spanish|Portuguese|Dutch|"
    r"Belgian|Swiss|Austrian|Swedish|Norwegian|Danish|Finnish|Irish|Scottish|Welsh|"
    r"Polish|Czech|Hungarian|Greek|Turkish|Russian|Ukrainian|Japanese|Korean|Indian|"
    r"Pakistani|Bangladeshi|Australian|Brazilian|Argentine|Chilean|Mexican|Colombian|"
    r"Peruvian|Egyptian|Nigerian|Kenyan|South African|Moroccan|Israeli|Saudi|Emirati|"
    r"Qatari|Thai|Vietnamese|Filipino|Indonesian|Malaysian|Singaporean)\b"
    r"|\b(Atlanta|Boston|Chicago|Dallas|Denver|Detroit|Houston|Miami|"
    r"New York|Los Angeles|Seattle|San Francisco|Washington|Toronto|Vancouver|Montreal|"
    r"London|Paris|Berlin|Madrid|Barcelona|Rome|Milan|Naples|Amsterdam|Vienna|Zurich|"
    r"Geneva|Brussels|Lisbon|Warsaw|Prague|Budapest|Athens|Stockholm|Oslo|Copenhagen|"
    r"Helsinki|Dublin|Edinburgh|Moscow|Istanbul|Cairo|Nairobi|Lagos|Johannesburg|"
    r"Sydney|Melbourne|Auckland|Tokyo|Osaka|Kyoto|Seoul|Bangkok|Singapore|Jakarta|"
    r"Manila|Kuala Lumpur|Hanoi|Mumbai|Delhi|Karachi|Rio|Sao Paulo|Buenos Aires|"
    r"Mexico City|Hollywood|Broadway|Silicon Valley|North Macedonia|"
    # 【2026-10-11 六轮·补】国家名（用于判断"事件发生在国外"，见 INTL_ENT_CN_ABROAD_PASS）。
    # 只收**具体国家/地区**，不收 Europe/Asia/Africa 这类泛词 —— 泛词会让
    # 「中国文化在亚洲受欢迎」这种没有具体地点的稿子也被当成"发生在国外"。
    r"Brazil|Argentine|Argentina|Chile|Peru|Colombia|Panama|Cuba|Mexico|Egypt|Kenya|"
    r"Nigeria|Ethiopia|Tanzania|Morocco|Tunisia|Algeria|Ghana|Uganda|Zambia|Zimbabwe|"
    r"Thailand|Vietnam|Cambodia|Laos|Myanmar|Nepal|Sri Lanka|Bangladesh|Pakistan|"
    r"Afghanistan|Kazakhstan|Uzbekistan|Mongolia|Saudi Arabia|Emirates|Qatar|Kuwait|"
    r"Iran|Iraq|Jordan|Lebanon|Syria|Middle East|Poland|Hungary|Romania|Bulgaria|"
    r"Serbia|Croatia|Slovenia|Slovakia|Ukraine|Belarus|New Zealand|Iceland|"
    r"Luxembourg|Estonia|Latvia|Lithuania|Malta|Cyprus)\b",
    re.I)

# 【2026-10-11 六轮·用户「娱乐新闻少的可怜」】
# 国际娱乐在"只用境内权威媒体"这条硬约束下，供给量天生就小 —— 真机实测
# CGTN 文化（唯一的国际娱乐直供源）17 条里有 13 条是中国题材，全拦掉就只剩 4 条。
# 但那 13 条里有一类是**事件发生在国外**的（中国电影周在北马其顿、中国高定亮相巴黎
# 时装周、中国影视展在巴西落幕）—— 它们的**主体是中国、场域在国外**，本质是国际文化
# 交流，放在国际栏并不违和；真正该拦的是**场域也在国内**的（上海时装周、北京钟鼓楼、
# 国庆档票房、井冈山故居）。
# 所以这里加一道开关：主体是中国但**标题里有明确国外地点**的 → 放行（算国际文化交流）。
# 想退回"凡是中国主体一律拦"的旧口径，把下面这个常量改成 False 即可（不用改判据）。
INTL_ENT_CN_ABROAD_PASS = True

# ============ 【2026-10-11 七轮·用户拍板】国际娱乐接入境外权威娱乐媒体 ============
# 先把话说明白：这是**用户 2026-10-11 明确拍板**的例外，不是擅自破章程。
# 背景（都是实测，不是估计）：
#   · 章程 §9.1「默认只用境内权威媒体」下，国际娱乐的供给**物理上限就是 6~8 条** ——
#     CGTN 英文 culture 50 条（跨度 12 天）里判得出国际的只有 6 条，放宽时效到 30 天
#     也一条不增；CGTN 西/法/阿/俄四语种 culture 各 200 条，各只 2~3 条娱乐；
#     CGTN travel/china/video 分区全是中国题材；中国日报/环球时报英文/新华英文/
#     人民网英文/中新网英文/中国网 的娱乐 RSS 全部 404；国内三家娱乐源（凤凰/新浪/
#     国际在线）33 条里**一条真国际稿都没有**。
#   · 境外候选实测 20 家，能过「每条必须带高清大图」这一硬门槛（用户 2026-10-07 定的）
#     的只有两家：Variety（10 条全带图）、Consequence（15 条全带图）。
#     Billboard / THR / RollingStone / NME / IndieWire 有货但 **RSS 不带图**，上不了；
#     其余 13 家 403/404/超时。
# 所以这里只接这两家，并且：
#   ① 只进**国际娱乐栏**，不动要闻/科技/财经/体育（境外时政一概不碰）；
#   ② 加一道主权红线过滤（见 `_INTL_ENT_BLOCK_RX`）；
#   ③ 留总开关 `INTL_ENT_OVERSEAS`：改 False 立刻回到纯境内口径，不用改判据。
INTL_ENT_OVERSEAS = True
INTL_ENT_OVERSEAS_CHANNELS = {"intl-variety", "intl-consequence"}

# 境外娱乐源的主权红线：标题里出现这些词的一律剔除，不看语境。
# 娱乐新闻本来极少提这些，实测 25 条里只命中 1 条（"Taiwan's Sole Busan
# Competition Title"），剔掉不心疼，但能挡住境外媒体惯用的涉台/涉藏/涉疆表述口径。
# 注意只针对**境外源**生效 —— 境内源有自己的表述规范，走原来的判据。
_INTL_ENT_BLOCK_RX = re.compile(
    r"\btaiwan|\btaipei|\btibet|\btibetan|\bxinjiang|\buighur|\buyghur|"
    r"\bhong kong\b|\bmacau|\bmacao|\bdalai|\bfalun", re.I)


def _looks_ent_intl(title):
    """娱乐条目该不该进**国际**栏：有外国文娱具体对象，且完全看不出中国文娱主体。

    用在 `_split_cn_scope`（国内娱乐 → 国际娱乐）这一路。
    出口那道 `_is_domestic_for_intl` 用的是同一张 `_ENT_FOREIGN_ALL`，口径一致。
    """
    t = title or ""
    if not _looks_ent_foreign(t):
        return False
    if _looks_domestic(t):
        return False
    return not _ENT_CN_SUBJECT_RX.search(t)


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
# 被锁在国内头条/热榜里，用户反馈"国内还是有国际新闻"。
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
    max_age = _max_age_hours(src)
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


# ============ 【2026-10-09 补源·国际栏】四个「列表页自带图」的站点解析 ============
# 用户原话：「国际、娱乐栏目新闻太少！！给国际栏多接几个自带图的源」。
# 共同口径：**图片与时间都必须在列表页里拿到**，绝不走「正文页补图」那条路
# （实测综合源补图命中率只有 5/24，那是本次「新闻太少」的根因）。
# 逐条实测数据（HTTP / 耗时 / 条数 / 带图数 / 最新时间 / 图片来源字段）与
# 「试过没用上」的清单都在 logs\国际栏增补带图源-20261009.md。

# 国际在线·国际（news.cri.cn/world）的列表卡片：
#   <div class="list-li"><div class="list-pic…"><a href="/20261009/<uuid>.html" class="aImg"><img src="…">
#   <div class="list-title"><a href="…">标题</a></div> … <div class="list-time">2026年10月09日 14:39</div>
# 与娱乐频道（ent.cri.cn，见 _CRI_CARD_RE）是**两套模板**：这里的时间是中文年月日+时分、
# 卡片 class 也不同，所以单独一个正则，不混用（混用会让两个源互相解析失败）。
_CRI_LIST_CARD_RE = re.compile(
    r'<a[^>]+href="(/20\d{6}/[0-9a-f\-]{20,}\.html)"[^>]*class="aImg">\s*<img src="([^"]+)"'
    r'.*?<div class="list-title">\s*<a[^>]+href="[^"]*"[^>]*>([^<]+)</a>\s*</div>'
    r'.*?<div class="list-time">([\d年月日: ]{10,22})</div>', re.S)
# 列表页时间格式：2026年10月09日 14:39（站方已给到分，不需要去文章页取时间）
_CRI_LIST_TIME_FMT = "%Y年%m月%d日 %H:%M"


def _parse_cri_world(src):
    """国际在线·国际（news.cri.cn/world）：列表页 20 张卡片，标题 / 原图 / 精确时间一次拿全。

    实测（2026-10-09）：HTTP 200 / 0.03s / 卡片 20 张 / 全部带图 / 当天分钟级更新；
    首图抽 8 张全部达标（1920x1080、5472x3079、1440x810、1024x576…）。
    ⚠ 卡片给的是 **256x144 缩略图**（短边 144，直接下发会被低质图门槛整条剔除），
      按站方 URL 规则改写成原图（见 _cri_image / _CRI_THUMB_RE）。
    """
    raw = _fetch(src["url"])
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 国际在线·国际抓取失败")
        return []
    page = _decode_feed_bytes(raw)
    max_age = _max_age_hours(src)
    cutoff = datetime.now(CST).timestamp() - max_age * 3600
    out, seen, dropped = [], set(), 0
    for m in _CRI_LIST_CARD_RE.finditer(page):
        path, thumb, title, ttxt = m.group(1), m.group(2), _clean(m.group(3)), m.group(4)
        link = _norm_url(path, src["base"])
        if not title or len(title) < 4 or not link or link in seen:
            continue
        try:
            ts = int(datetime.strptime(ttxt.strip(), _CRI_LIST_TIME_FMT)
                     .replace(tzinfo=CST).timestamp())
        except Exception:
            ts = 0          # 时间解析不出来就交给时效收口处理（published=0 会被丢掉，不伪造时间）
        if ts and ts < cutoff:
            dropped += 1
            continue
        img = ""
        u = _norm_url(thumb, src["base"])
        if u and not any(x in u.lower() for x in _IMG_JUNK):
            img = _cri_image(u)          # 256x144 缩略图 → 原图
        seen.add(link)
        out.append({
            "id": hashlib.md5((src["id"] + title).encode("utf-8")).hexdigest()[:12],
            "region": src["region"], "channel": src["id"], "label": src["label"],
            "cls": src["cls"], "title": title, "desc": "",
            "link": link, "image": img, "published": ts, "translated": False,
        })
        if len(out) >= 24:
            break
    print(f"[ok] {src['id']}: 国际在线国际解析 {len(out)} 条"
          f"（带图 {sum(1 for x in out if x['image'])} 条，滤除过期 {dropped} 条）")
    return out


# 新华网（news.cn）「推荐位」卡片模板（国际频道的体育频道在用）：
#   <div class="column-center-item item-style1 …"><li><a href="/sports/20261009/<hash>/c.html">
#     <div class="coxe"><div class="tpwz"><img src="20261009/<hash>/<file>.jpg" class="img_fd"></div>
#     <div class="tpwm"><span>标题</span><p>2026-10-09 09:46:58</p></div></div></a></li></div>
# 列表页直接给到**秒**，图片也是原图（实测 981x552 ~ 1010x568，达标）。
# ⚠ 新华网其它频道（ent / tech / world）不是这套模板：它们的 img 只有 184~225 短边、
#   过不了 300 的门槛（实测后**没有**接入，见日志里「试过没用上」清单），别拿这个正则去套。
_XH_COXE_CARD_RE = re.compile(
    r'<li><a href="(/[a-z]+/20\d{6}/[0-9a-f]+/c\.html)"[^>]*>.*?'
    r'<div class="tpwz">\s*<img src="([^"]+)"[^>]*class="img_fd">.*?'
    r'<div class="tpwm">\s*<span>([^<]{4,140})</span>\s*<p>([\d\-: ]{10,25})</p>', re.S)


def _parse_xinhua_coxe(src):
    """新华网·体育（国际频道，www.news.cn/sports）：列表页 21 张图卡，标题/原图/秒级时间齐全。

    实测（2026-10-09）：HTTP 200 / 0.06~0.1s / 卡 21 张 / 全部带图 /
    24 小时内 7 条、48 小时内 20 条 / 图 981x552~1010x568 达标。
    内容以国际赛事为主（奥运申办、中网、世界羽联…），正对「国际板块留体育」的口径。
    """
    raw = _fetch(src["url"], timeout=12)
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 新华网·体育（国际频道）抓取失败")
        return []
    page = _decode_feed_bytes(raw)
    max_age = _max_age_hours(src)
    cutoff = datetime.now(CST).timestamp() - max_age * 3600
    out, seen, dropped, natl = [], set(), 0, 0
    for m in _XH_COXE_CARD_RE.finditer(page):
        path, imgsrc, title, ttxt = m.group(1), m.group(2), _clean(m.group(3)), m.group(4)
        link = _norm_url(path, src["base"])
        if not title or len(title) < 4 or not link or link in seen:
            continue
        try:
            ts = int(datetime.strptime(ttxt.strip(), "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=CST).timestamp())
        except Exception:
            ts = 0
        if ts and ts < cutoff:
            dropped += 1
            continue
        # 【2026-10-09 用户要求「国际，体育栏目里混着国内体育新闻」】国际频道的体育只留国际赛事：
        # 标题命中纯国内赛事词、或看不出任何国际信号的，**在这里就丢掉**
        #（用户原话：国内的体育不值得看）。放在源头过滤，比在国际频道各出口补漏可靠。
        # 【2026-10-10 修正】原来这里用的是 `_INTL_SPORT_DOM_RX`（国内赛事词太少）
        # 配 `_INTL_SPORT_INT_RX`（把中网/WTT/大冬会/外国国名当国际信号）——
        # 实测 11 条里放行 9 条中国赛事。现在两张表都换成"赛事与主体"口径，
        # 且与出口那道 `_is_domestic_for_intl` 共用同一张表，不会再出现两处判据不一致。
        if not _looks_sport_foreign(title):
            natl += 1
            continue
        img = _norm_url(imgsrc, src["base"]) or ""
        if img and any(x in img.lower() for x in _IMG_JUNK):
            img = ""
        seen.add(link)
        out.append({
            "id": hashlib.md5((src["id"] + title).encode("utf-8")).hexdigest()[:12],
            "region": src["region"], "channel": src["id"], "label": src["label"],
            "cls": src["cls"], "title": title, "desc": "",
            "link": link, "image": img, "published": ts, "translated": False,
        })
        if len(out) >= 24:
            break
    print(f"[ok] {src['id']}: 新华网·体育（国际频道）解析 {len(out)} 条"
          f"（带图 {sum(1 for x in out if x['image'])} 条，滤除过期 {dropped} 条，滤除非国际赛事 {natl} 条）")
    return out


# 人民网（people.com.cn）「焦点图」卡片：<li><a href="…/n1/2026/1009/c1002-40811303.html">
#   <img src="/NMediaFile/2026/1009/MAIN….jpg" width="660" height="370" alt="标题"></a>…
# 图片短边实测 370~558（达标），标题直接写在 img 的 alt 里（站方两处都写了标题，alt 更短更干净）。
_PEOPLE_CARD_RE = re.compile(
    r'<li><a href="([^"]+?/n1/(20\d\d)/(\d{2})(\d{2})/c\d+-\d+\.html)"[^>]*>\s*'
    r'<img src="([^"]+)"[^>]*alt="([^"]*)"', re.S)


def _parse_people_list(src):
    """人民网频道列表页的「焦点图」卡片（world.people.com.cn / ent.people.com.cn 同一套模板）。

    ⚠ 这个列表页**只给到日期**（写在文章 URL 的 /2026/1009/ 里，没有时分），
      所以发布时间按「该日期 00:00（东八区）」记 —— 是站方 URL 上写死的日期，不是猜的时点；
      副作用是这些条目在「最新优先」排序里会排在有具体时分的条目之后（已写进日志备查）。
      没有去抓正文页取时间：本次的要求是「自带图」，多抓一页就多一份失败风险，
      而且 6 条里但凡有一条抓不到时间，那条就会被时效收口整条丢掉。
    实测（2026-10-09）：world 200 / 0.09s / 6 张卡 / 图 660x370~996x558 全部达标；
                        ent   200 / 0.08s / 3 张卡（其中 2 张图不达标，会被后台低质图剔除）。
    """
    raw = _fetch(src["url"])
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 人民网列表抓取失败")
        return []
    page = _decode_feed_bytes(raw)
    max_age = _max_age_hours(src)
    cutoff = datetime.now(CST).timestamp() - max_age * 3600
    out, seen, dropped = [], set(), 0
    for m in _PEOPLE_CARD_RE.finditer(page):
        link, y, mo, d, imgsrc, title = (m.group(1), m.group(2), m.group(3),
                                         m.group(4), m.group(5), _clean(m.group(6)))
        if not link or link in seen:
            continue
        if not title or len(title) < 4:
            # 少数卡片的 alt 是空的 → 退回卡片后面的标题链接（站方在同一块里又写了一遍）
            mt = re.search(r'href="' + re.escape(link) + r'"[^>]*>([^<]{4,120})</a>', page)
            title = _clean(mt.group(1)) if mt else ""
            if not title:
                continue
        try:
            ts = int(datetime(int(y), int(mo), int(d), tzinfo=CST).timestamp())
        except Exception:
            ts = 0
        if ts and ts < cutoff:
            dropped += 1
            continue
        img = _norm_url(imgsrc, src["base"]) or ""
        if img and any(x in img.lower() for x in _IMG_JUNK):
            img = ""
        seen.add(link)
        out.append({
            "id": hashlib.md5((src["id"] + title).encode("utf-8")).hexdigest()[:12],
            "region": src["region"], "channel": src["id"], "label": src["label"],
            "cls": src["cls"], "title": title, "desc": "",
            "link": link, "image": img, "published": ts, "translated": False,
        })
        if len(out) >= 24:
            break
    print(f"[ok] {src['id']}: 人民网列表解析 {len(out)} 条"
          f"（带图 {sum(1 for x in out if x['image'])} 条，滤除过期 {dropped} 条）")
    return out


# 光明网（gmw.cn）图卡：<li><a href="2026-10/09/content_39036240.htm"><img src="https://imgworld.gmw.cn/…png"></a>
#                         <p class="pic_title"><a href="…">标题</a></p></li>
# 图片实测 580x320（短边 320，刚好过 300 门槛）；日期写在 href 的 2026-10/09/ 里，同样只有日。
_GMW_CARD_RE = re.compile(
    r'<li>\s*<a href="([^"]*?content_\d+\.htm)"[^>]*>\s*<img src="([^"]+)"[^>]*>\s*</a>\s*'
    r'<p class="pic_title">\s*<a[^>]*>([^<]{4,120})</a>', re.S)


def _parse_gmw_list(src):
    """光明网·国际（world.gmw.cn）：`pic_title` 图卡，标题 + 原图在列表页里，日期看 href。

    ⚠ 与人民网同样只给到日期（2026-10/09/），按当天 00:00（东八区）记，理由同上。
    实测（2026-10-09）：200 / 0.17s / 严格图卡 4 张（10-09、10-08、09-30、09-29）/
    图 580x320 达标 / 最新一条当天更新。
    """
    raw = _fetch(src["url"])
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 光明网国际抓取失败")
        return []
    page = _decode_feed_bytes(raw)
    max_age = _max_age_hours(src)
    cutoff = datetime.now(CST).timestamp() - max_age * 3600
    out, seen, dropped = [], set(), 0
    for m in _GMW_CARD_RE.finditer(page):
        path, imgsrc, title = m.group(1), m.group(2), _clean(m.group(3))
        link = _norm_url(path, src["base"])
        if not title or len(title) < 4 or not link or link in seen:
            continue
        ts = 0
        md = re.search(r"(20\d\d)-(\d{1,2})/(\d{1,2})/content_\d+\.htm", path)
        if md:
            try:
                ts = int(datetime(int(md.group(1)), int(md.group(2)), int(md.group(3)),
                                  tzinfo=CST).timestamp())
            except Exception:
                ts = 0
        if ts and ts < cutoff:
            dropped += 1
            continue
        img = _norm_url(imgsrc, src["base"]) or ""
        if img and any(x in img.lower() for x in _IMG_JUNK):
            img = ""
        seen.add(link)
        out.append({
            "id": hashlib.md5((src["id"] + title).encode("utf-8")).hexdigest()[:12],
            "region": src["region"], "channel": src["id"], "label": src["label"],
            "cls": src["cls"], "title": title, "desc": "",
            "link": link, "image": img, "published": ts, "translated": False,
        })
        if len(out) >= 24:
            break
    print(f"[ok] {src['id']}: 光明网国际解析 {len(out)} 条"
          f"（带图 {sum(1 for x in out if x['image'])} 条，滤除过期 {dropped} 条）")
    return out


# 国内板块里要做「国内 / 国际」内容分流的栏目（用户 2026-10-07 要求）：
#   · 热榜：原来就分流了
#   · 头条（2026-10-09 按用户口径定名；定名前叫另一个名字，改名记录见 README 与
#     logs\气泡与栏目定稿-20261009.md）：
#     「国内板块的头条里充次着国际新闻，全部移到国际板块」
# 科技 / 软件 / 娱乐**不做分流** —— 这两栏里的国际内容（AMD、马斯克、欧美乐坛…）
# 本来就属于该栏目本身，分流会把它们搬空。
# 【2026-10-07 用户再次强调】「区分国内和国际，娱乐栏目也不例外」→ 娱乐也纳入分流。
# 科技/软件仍不分流：那两栏里的国际内容（AMD、马斯克、欧美软件）本来就属于该栏目。
# 【2026-10-07 用户反馈「快科技里有国际新闻混在里面」】
# 科技栏目原来不参与国内/国际分流，于是「洛杉矶机场两架宽体机相撞」「比利时天才少年」
# 「谷歌/OpenAI/马斯克/韩国散户」这些国际科技新闻全留在国内科技里。
# 加进来之后：国际科技条目会带着 cls="科技" 转到国际板块，
# **国际板块自动多出一个"科技"栏目**（栏目是跟着数据走的，不用另外写死）。
# 【2026-10-09 定稿】国内栏目 = 头条 / 科技 / 财经 / 娱乐 / 软件 / 热榜（外加国际频道的
# 要闻 / 科技 / 财经 / 娱乐 / 体育 / 热榜 —— 上面这两项只在国际频道存在）。
# 民生类那一栏已整栏下线，不在任何分流集合里。
# 注意 财经 **故意不在这里**：它是本轮新补的国内栏目（华尔街见闻 / 中新网·财经），
# 加进来会让「美股/A股」这类稿子被分流到国际频道、把国内财经栏搬空；
# 国内财经栏要稳定供应（气泡配额要求财经 ≥2 条），所以财经条目一律留在国内。
CN_SPLIT_CLS = {"头条", "热榜", "娱乐", "科技"}

# 【2026-10-15 用户口径】「国内栏只留涉华财经」——纯外国宏观财经的识别与搬运。
# 用户原话（2026-10-15）：国内栏里出现「纯外国题材的财经稿（美联储/美国CPI）」→ **挪到国际栏**。
# 为什么只写这一条窄规则、而不是把「财经」整个塞进 CN_SPLIT_CLS：
#   见上面 2026-10-09 的注释 —— 那会把 A 股/美股同框的稿子一起搬走、把国内财经栏搬空，
#   而气泡配额要求国内财经 ≥2 条。真机实测（2026-10-15，国内财经 40 条）：
#   命中本规则只有 3 条，搬完仍剩 37 条，供应安全。
_FOREIGN_MACRO_RX = re.compile(
    r"美联储|美国CPI|美CPI|美国非农|非农|鲍威尔|欧洲央行|欧央行|日本央行|英国央行|加拿大央行|"
    r"澳洲联储|褐皮书|美股|纳斯达克|道琼斯|标普500|美债|美元指数|OPEC|国际货币基金组织|"
    r"世界银行|特朗普|白宫|美国商务部|美国财政部|欧洲统计局|欧盟统计局")
# 涉华角度：只要命中就**不搬**（留在国内财经）
_CN_ANGLE_RX = re.compile(
    r"中国|我国|中方|A股|沪指|深证|创业板|科创板|北交所|人民币|港股|中概|在华|对华|"
    r"中美|中欧|中日|中韩|国内|北京|上海|深圳")


def _is_foreign_macro(it):
    """这条财经稿是不是「纯外国宏观」（主体是外国央行/外国市场，且没有任何涉华角度）。"""
    t = (it.get("title") or "")
    if not t:
        return False
    if _CN_ANGLE_RX.search(t):
        return False
    return bool(_FOREIGN_MACRO_RX.search(t))
# 【2026-10-12 用户截图】「北约防空网几分钟内崩溃！挪威模拟：28架苏-57+…」挂在
# 「娱乐·凤凰网」——娱乐源的视频区混发军事新闻，栏目跟着源走就错位（用户口径：
# 「财经类新闻一定要区别化，不能错放栏目」是同一件事）。标题命中**窄军事词表**时，
# 先把栏目改成「头条」再走常规判定：留国内就显示在头条，转国际按既有「头条→要闻」
# 规则落到要闻。窄表不含「坦克」（坦克300/坦克世界不是军事）与字母数字机型（不稳）。
_MIL_RX = re.compile(
    r"防空|导弹|无人机|战斗机|轰炸机|军舰|航母|雷达|演习|击落|火炮|弹药|核弹|核武|"
    r"驻军|部队|战机|预警机|反导|北约|苏-\d|苏\d+|米-\d|歼-?\d|轰-\d"
)

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
        # 【2026-10-12】娱乐栏里的军事新闻先把栏目改成「头条」（见 _MIL_RX 注释）。
        # 改完自然跳过下面的娱乐专用判据，走通用分流；转国际时按「头条→要闻」落栏。
        # 幂等：第二次进本函数时 cls 已是「头条」，不会再改。
        if (it.get("cls") or "") == "娱乐" and _MIL_RX.search(_title):
            it["cls"] = "头条"
        # 【2026-10-07 用户反馈「国内热榜还是混进国际新闻」】关键改动：
        # **关键词很明确时以关键词为准，不再被站方栏目压住**。
        # 原因是凤凰把外交新闻归在「大陆」栏目里，而「大陆」在我的国内白名单中，
        # 于是「俄罗斯总统助理…将访华」这种标题被判成国内 —— 用户一眼就看出是国际。
        # 现在顺序是：站方说国际 → 国际；关键词明显是国际（且无强国内特征）→ 国际；
        # 站方说国内 → 国内；其余按关键词兜底。
        # 【2026-10-11 三轮】用户定的规则：**关键字计数过滤**，不再"命中一个词就定生死"。
        # 旧逻辑是「标题里有外国词就判国际」，词表稍缺就漏（康奈尔/白宫没进表就漏判），
        # 而且「广汽埃安 N60 新增苹果 CarPlay」这种只提外国品牌的**国内稿**会被误判成国际。
        # 新规则：标题里「外国地名/机构」和「中国主体」各自计数（同一词只算一次），
        #         外国 >= 2 且 外国 > 中国 → 国际；否则国内。
        if (it.get("cls") or "") == "娱乐":
            # 【2026-10-11】娱乐栏专用判据：必须有**外国文娱具体对象**
            # （好莱坞 / 奥斯卡 / 华纳兄弟 / Netflix …），且没有中国文娱主体。
            # 不用通用的 `_scope_scores` 地名计数 —— 娱乐稿里外国地名太常见
            # （"蔡少芬现身韩国""迪丽热巴亮相米兰"），按地名计数会把国内明星八卦判成国际。
            _is_intl = _looks_ent_intl(_title)
        elif _sec in FOREIGN_SECTIONS:
            _is_intl = True
        elif _strong_intl(_title):
            # 国际大奖/赛事优先判国际（诺贝尔、奥斯卡、奥运会…）。
            _is_intl = True
        elif (it.get("cls") or "") == "财经" and _is_foreign_macro(it):
            # 【2026-10-15 用户口径】纯外国宏观财经（美联储/美国CPI…且零涉华角度）→ 归国际财经。
            _is_intl = True
        else:
            _cn_n, _f_n = _scope_scores(_title)
            _is_intl = _f_n >= INTL_PLACE_MIN and (
                _f_n > _cn_n
                or (_f_n >= W_FOREIGN_PLACE and _DIPLO_VISIT_CN_RX.search(_title)))
        d = None          # 只有"判定要转国际"时才会被赋值
        # 【2026-10-15】能参与分流的栏目 = CN_SPLIT_CLS，外加「纯外国宏观财经」这一条例外
        #（财经整体不参与分流，见 CN_SPLIT_CLS 上面的注释）。
        _can_move = (it.get("cls") in CN_SPLIT_CLS) or (
            (it.get("cls") or "") == "财经" and _is_foreign_macro(it))
        if _can_move and _is_intl:
            d = dict(it)
            d["region"] = "intl"
            d["channel"] = "cn-intl-news"     # 标明「国内媒体报的国际新闻」
            d["translated"] = True            # 国内媒体发的，本来就是中文
            # 【2026-10-09 栏目定稿】国内「头条」转过去要落**国际频道的「要闻」**
            # （两栏是各自频道的"综合新闻"位；不改名的话国际频道会凭空多出一栏「头条」，
            #  而国际频道定稿只有 要闻/科技/财经/娱乐/体育/热榜 六栏）。
            # 科技 / 娱乐 / 热榜两边的栏目名相同，原样带过去即可。
            if d.get("cls") == "头条":
                d["cls"] = "要闻"      # 落到国际频道的「要闻」栏（国内那一栏定稿叫「头条」）
        # 【2026-10-09 用户要求「国际，娱乐栏目还是有国内新闻，你怎么不归纳到国内版里」】
        # 娱乐条目**一律留在国内版**：国际在线娱乐/凤凰娱乐/新浪娱乐本来就都是国内站，
        # 只有 CGTN 文化这类国际台的文化内容才配进国际栏。
        #
        # 【2026-10-10 修 bug】原来本函数的结尾是：
        #     if cls == "娱乐": dom.append(it); continue
        #         intl.append(d)          ← 死代码，永远执行不到
        #     else: dom.append(it)
        # 也就是**算出来的国际条目一条都没交给 intl** → `_spill_from_cn()` 恒返回空 →
        # `_collect("intl")` 里"并入国内板块转来的国际新闻"整段失效。
        # 后果很重：README 与 docs/COMPLIANCE.md 都明写着
        # 「其余国际内容来自境内综合媒体报的国际新闻，由国内板块自动转入国际板块
        #   （channel 标为 cn-intl-news）」—— **这个能力实际根本不存在**，
        # 对照声明核代码就能直接看出来（`cn-intl-news` 一条都不会出现）。
        #
        # 【2026-10-11 再改】娱乐的"一律留国内"取消，改成**真分流**：
        # 用户 2026-10-11 的原话是「所有栏目的新闻，都存在国内和国际不能正确识别」
        # 加上「国际板块的娱乐新闻太少」—— 两条合起来就是：娱乐也要认国别。
        # 现在娱乐按 `_looks_ent_intl`（有外国文娱具体对象 + 无中国文娱主体）判定：
        #   判国际 → **真移走**（与头条/热榜/科技一致，国内栏不再显示）；
        #   判国内 → 留在国内栏（国内明星八卦不会跑进国际栏）。
        # 国际栏那边的娱乐判据用的是同一张 `_ENT_FOREIGN_ALL`，两边不会打架。
        if (it.get("cls") or "") == "娱乐" and d is None:
            dom.append(it)
            continue
        if d is not None:
            # 【2026-10-11 三轮】用户拍板：**真移走**，不再「国内留一份、国际给一份」。
            # 原先两边都显示，用户看到的就是「头条里混进国际新闻」。
            # 注意 PRELOAD_ALL[region]（搜索池）仍是全量，只是列表不再显示。
            intl.append(d)
            continue
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
    # 实测取到 "国际"/"军事"/"台湾"…）；只有拿不到分类时才退回关键词黑名单。
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
        # 【2026-10-11 三轮】用户定的规则：**关键字计数过滤**，不再"命中一个词就定生死"。
        # 旧逻辑是「标题里有外国词就判国际」，词表稍缺就漏（康奈尔/白宫没进表就漏判），
        # 而且「广汽埃安 N60 新增苹果 CarPlay」这种只提外国品牌的**国内稿**会被误判成国际。
        # 新规则：标题里「外国地名/机构」和「中国主体」各自计数（同一词只算一次），
        #         外国 >= 2 且 外国 > 中国 → 国际；否则国内。
        if _sec in FOREIGN_SECTIONS:
            _is_intl = True
        elif _strong_intl(_title):
            # 国际大奖/赛事优先判国际（诺贝尔、奥斯卡、奥运会…）。
            _is_intl = True
        else:
            _cn_n, _f_n = _scope_scores(_title)
            _is_intl = _f_n >= INTL_PLACE_MIN and (
                _f_n > _cn_n
                or (_f_n >= W_FOREIGN_PLACE and _DIPLO_VISIT_CN_RX.search(_title)))
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
# 已经播过气泡的条目 → 上次播出的时间戳。用户要求「轮播内容不能重复」。
# 【2026-10-25 待办 #65】原来这是一份 set，且"本轮新增的全播过"时**整份清空**重来 ——
# 清空那一刻等于把几千条历史一起忘了：只要有条目隔几天又冒回新增列表（热榜反复上榜
# 是常态），它就会被当成"没播过"再投一次，正是用户说的"不能重复投放"被自己拆了。
# 现在改成**按条过期**：每条记 `_BALLOON_SHOWN_TTL` 小时，过期自然忘，
# 名单长度还有硬顶（超了从最久没播的开始删），长跑不会把内存吃掉。
# 【2026-10-29 用户：「一定要定规则：常看常新」】两处升级：
#   · 记的时长 12h → **26h**：必须比「常看常新」① 的 24 小时硬上限长，否则早上播过的
#     一条到夜里名单先把它忘了、它却还在 24 小时窗口里 → 同一件事播第二遍；
#   · **跨重启保留**：原来这是纯内存字典，程序一重启（升级、开机自启、崩溃拉起）
#     就等于"今天什么都没播过"，第一轮又能把上午那 10 条播一遍。
_BALLOON_SHOWN = {}
_BALLOON_SHOWN_TTL = (BALLOON_FRESH_HOURS + 2) * 3600     # 26 小时（见上面那条注释）
_BALLOON_SHOWN_MAX = 3000          # 名单条数硬顶
_BALLOON_SHOWN_LOCK = threading.Lock()
# 写盘单独一把锁 + 每次一个不重名的临时文件。原来固定写 `balloon_shown.json.tmp`：
# 开机强制落盘、双击托盘强制落盘、退出前 flush 是三条线程，同时写同一个 tmp 时
# 内容会交织，`os.replace` 也可能撞上对方还开着的句柄 —— 坏掉的那份 JSON 读回来当
# "没播过"，26 小时内又整批重播一遍，正好把规则 ③ 作废。
_BALLOON_SHOWN_IO_LOCK = threading.Lock()
# 本机私有数据，和控制台兴趣档 interest.json 同一个目录、同一套写法（.tmp + os.replace）
_BALLOON_SHOWN_PATH = os.path.join(
    os.getenv("LOCALAPPDATA") or tempfile.gettempdir(), "hotnews", "balloon_shown.json")
_BALLOON_SHOWN_SAVE_GAP = 60.0     # 落盘节流：一分钟最多写一次（一轮播报也就写一次）
_BALLOON_SHOWN_SAVED_AT = [0.0]


def _balloon_shown_load():
    """开机把上次播过的名单读回来（常看常新 ③：重启不该把"播过"这件事忘掉）。

    读不到 / 读坏了都当没有：宁可多播一条旧的，也不许因为一个缓存文件起不来。
    """
    try:
        with open(_BALLOON_SHOWN_PATH, "r", encoding="utf-8") as f:
            raw = (json.load(f) or {}).get("shown") or {}
    except FileNotFoundError:
        return 0
    except Exception as ex:
        print("[warn] 播过名单读取失败（当没播过，不影响播报）: %s: %s"
              % (type(ex).__name__, ex))
        return 0
    now = time.time()
    n = 0
    with _BALLOON_SHOWN_LOCK:
        for k, v in raw.items():
            try:
                t = float(v)
            except (TypeError, ValueError):
                continue
            if k and now - t <= _BALLOON_SHOWN_TTL:
                _BALLOON_SHOWN[str(k)] = t
                n += 1
    if n:
        print("[ok] 播过名单已读回 %d 条（常看常新：重启不重播上午播过的那几条）" % n)
    return n


def _balloon_shown_save(force=False):
    """整份名单写盘。失败只吼一声，绝不影响播报。

    【2026-10-29 实机验证时发现的漏网】force= True 时跳过 60 秒节流，只给两个场合用：
      · 用户**手动**双击托盘那一轮 —— 他刚看到的这几条必须当场落盘，
        否则"退出程序"正好落在节流窗口里，下一次开机又会把同一批播第二遍（规则 ③ 破功）；
      · 退出前那一次 flush（见 _quit_app_now / api_quit）。
    """
    now = time.time()
    if not force and now - _BALLOON_SHOWN_SAVED_AT[0] < _BALLOON_SHOWN_SAVE_GAP:
        return False
    try:
        with _BALLOON_SHOWN_IO_LOCK:
            with _BALLOON_SHOWN_LOCK:
                snap = dict(_BALLOON_SHOWN)
            d = os.path.dirname(_BALLOON_SHOWN_PATH) or "."
            os.makedirs(d, exist_ok=True)
            # 不重名的临时文件（pid + 线程号）：开机 / 手动 / 退出 flush 是三条线程，
            # 同时 force 也不会写串同一个 tmp。写盘本身由 _BALLOON_SHOWN_IO_LOCK 串行。
            tmp = "%s.%d.%d.tmp" % (_BALLOON_SHOWN_PATH, os.getpid(), threading.get_ident())
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"saved_at": now, "shown": snap}, f, ensure_ascii=False)
                os.replace(tmp, _BALLOON_SHOWN_PATH)
            except Exception:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            _BALLOON_SHOWN_SAVED_AT[0] = now
            return True
    except Exception as ex:
        print("[warn] 播过名单写盘失败（不影响播报）: %s: %s" % (type(ex).__name__, ex))
        return False


# 开机就把上次播过的名单读热。放模块级和 `TOKEN = _load_local_token()` 同一套路：
# 托盘线程、自动轮播都可能在服务一起来就播第一轮，名单晚一步就等于"今天没播过"。
_BALLOON_SHOWN_LOADED = _balloon_shown_load()


def _balloon_seen():
    """当前还在"最近播过"名单里的条目键（过期的顺手删掉，不整份清空）。"""
    now = time.time()
    with _BALLOON_SHOWN_LOCK:
        for k in [k for k, v in _BALLOON_SHOWN.items() if v < now - _BALLOON_SHOWN_TTL]:
            _BALLOON_SHOWN.pop(k, None)
        return set(_BALLOON_SHOWN.keys())


def _balloon_mark_shown(keys, force=False):
    """把真播出去的条目记进名单；超上限就从"最久没播"的那头删；顺手落盘（有节流）。"""
    now = time.time()
    with _BALLOON_SHOWN_LOCK:
        for k in keys:
            if k:
                _BALLOON_SHOWN[str(k)] = now
        over = len(_BALLOON_SHOWN) - _BALLOON_SHOWN_MAX
        if over > 0:
            for k in sorted(_BALLOON_SHOWN, key=lambda x: _BALLOON_SHOWN[x])[:over]:
                _BALLOON_SHOWN.pop(k, None)
    _balloon_shown_save(force=force)


def _balloon_shown_flush():
    """退出前把内存里的播过名单强行写盘（见 _balloon_shown_save 的 force 说明）。"""
    try:
        _balloon_shown_save(force=True)
    except Exception as ex:
        print("[warn] 退出前写播过名单失败: %s" % type(ex).__name__)


# 【2026-10-25 待办 #65】气泡这一轮为什么弹 / 为什么不弹，留一份可查的记录。
# print 那行本来就会进 hotnews.log（打包态 stdout 接的就是那个文件），但排查
# "气泡怎么不响了"时不该让用户去翻日志文件 —— 这里再留最近若干条在内存里，
# 跟着 GET /api/notify 一起回（只增不减，老调用方不受影响）。
_BALLOON_TRACE = []
_BALLOON_TRACE_MAX = 12


def _balloon_trace(text):
    """记一句气泡投放的决定（同时打日志）。返回原文，调用方可接着用。"""
    print("[气泡] %s" % text)
    _BALLOON_TRACE.append("%s %s" % (datetime.now(CST).strftime("%m-%d %H:%M"), text))
    while len(_BALLOON_TRACE) > _BALLOON_TRACE_MAX:
        _BALLOON_TRACE.pop(0)
    return text


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
    # 【2026-10-25】夜间免打扰同样挡这一类（时段可在「程序设置」里关）。
    # 大事本身不会因为晚上不弹就消失：早上解除免打扰后，它仍在页面热榜/头条里。
    if _in_quiet_hours(now):
        return
    _hold = _notify_hold_reason()       # 锁屏/演示：大事也等一等，别盖住画面、别弹给没人看
    if _hold:
        print("[ok] 大事提醒：跳过本轮 —— %s" % _hold_note(_hold))
        return
    _MAJOR_LAST_TOAST = now
    sample = fresh[:BALLOON_PLAY_COUNT]
    entries = []
    for it in sample:
        # 【2026-10-09】「⚠ 新闻大事」是**大事提醒**这一类通知的固定抬头（需求书里明确不属
        # 「更新提醒」那一类，不必改），这里只把后半段由「来源站名」改成**该条自己的栏目名**，
        # 与更新提醒气泡的第一行口径保持一致。
        _cls = (it.get("cls") or "").strip() or ("要闻" if _is_intl_item(it) else "头条")
        _bsrc = (it.get("source") or "").strip() or (it.get("label") or "").strip()
        entries.append(((f"⚠ 新闻大事 · {_cls}" + ((" · " + _bsrc) if _bsrc else "")),
                        (it.get("title") or "")[:72], True,
                        it.get("link") or ""))
    if len(fresh) > len(entries):
        _e = entries[-1]
        entries[-1] = (_e[0], _e[1] + f"　（另有 {len(fresh) - len(entries)} 条同类）") + tuple(_e[2:])
    print(f"[ok] 大事提醒：滚动播放 {len(entries)} 条")
    # 【2026-10-25 用户「不能几个网站的新闻重复投放」】大事提醒播过的事件也写进"已播事件"记忆，
    # 这样随后那波「更新提醒」不会把同一场地震/同一条快讯再播一遍。
    # 注意**只写不读**：大事提醒本身不做跨轮过滤 —— 这类通知漏报比多报严重。
    try:
        _balloon_event_remember(sample)
    except Exception:
        pass
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
                # 升级重启是"播过名单"最容易丢的一条路（os._exit 不走 atexit）。
                # 不 flush 的话，最近 60 秒里播过的那一批在新版起来后会被当成没播过。
                threading.Timer(0.2, lambda: (_balloon_shown_flush(), os._exit(0))).start()
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
    """后台预热两个 Region + 静默检查更新

    【2026-10-24 UI 整改·冷启动指标】启动事件里**只做必需且极快的事**：
    托盘/气泡的 start() 都只是起后台线程，不阻塞；其余（读译文缓存、升级残留检查）
    全部挪进后台线程 run() 里做 —— 「进程启动 → GET / 200」不再等这些磁盘动作。
    页面本身靠 SSR 首屏（PRELOAD 为空时立刻返回 loading，前端骨架屏接管），
    所以「先出页面、数据后到」的观感不受影响。
    实测数字见 logs\\UI整改-20261024\\测试日志.txt。
    """
    TRAY.start()                 # 右下角托盘图标（非 Windows 静默降级，内部起线程）
    BALLOON.start()              # 右下角自绘气泡（系统通知被关也照样弹，内部起线程）

    def run():
        # —— 以下三件事与首屏无关，一律放在后台线程里做 ——
        _load_trans_cache()      # 载入历史译文缓存，避免重复消耗免费翻译额度
        # 【2026-10-28 新增】开源热榜：先把上次抓的从磁盘读回来（断网/重启首屏也有这一栏），
        # 再 kick 一轮后台抓取。它排在新闻抓取之前，是因为它只有一个请求、
        # 一两秒就回来，不跟三十多个新闻源抢时间。
        try:
            _n, _ = _oss_load_disk()
            if _n:
                print("[ok] 开源热榜: 本机缓存载入 %d 条" % _n)
                # 读盘也算"这一栏活着"，得记一笔健康度：它 6 小时才抓一批，开机只读盘的话
                # 右栏「源健康度」里 GitHub 那行会一直是 idle / 0 条，而页面上明明摆着 20 条。
                _note_src_health(_oss_meta(), True, _n)
                # 缓存里只有数据、没有题图：GitHub 的分享卡是站方现画的，冷启动不预热的话
                # 首屏那 6 张每张要等 1~10 秒（2026-10-28 本机重启后第一次 /img 实测 10.7 秒），
                # 满栏白框看着就像坏了。_oss_refresh 那条路已经预热过，但「读到本机缓存」
                # 时 6 小时内不会再抓，所以这一步得补在这里。
                _oss_prewarm(_oss_snapshot()[0])
            _kick_oss()
        except Exception as ex:
            print("[warn] 开源热榜预热失败: %s" % type(ex).__name__)
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
        # 顺序上国内先做，这样「国内头条剔出的国际新闻」能顺手并进国际板块。
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
                        # 所以等到**出现中文国际条目**再弹。
                        # 【2026-10-28 用户「自动精准投放到桌面，不需要看手机」】最长只等 20 秒：
                        # 原来等 120 秒，开机后两分钟屏幕角落什么都没有，用户以为气泡坏了。
                        # 20 秒内国际还没中文稿就先播国内+热榜（国际那几席由配额自动让给别的栏目），
                        # 下一轮刷新再补；"先看到"比"凑齐国际"重要。
                        for _ in range(20):
                            time.sleep(1)
                            _it = PRELOAD.get("intl") or []
                            if any(_has_cjk(x.get("title"))
                                   and str(x.get("channel") or "").startswith("cn-intl")
                                   for x in _it):
                                break
                        # 工作时不打扰（2026-10-30 收窄）：夜间免打扰 / 锁屏 / 投影演示 → 这次不弹，
                        # 交给下一轮刷新（新条目自然会再触发一次）。全屏程序不再挡。
                        if _in_quiet_hours():
                            print("[ok] 启动气泡：夜间免打扰时段，跳过")
                            return
                        _hold = _notify_hold_reason()
                        if _hold:
                            print("[ok] 启动气泡：跳过 —— %s" % _hold_note(_hold))
                            return
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
                            # 【2026-10-09】启动播报与「更新提醒」用**同一套配额挑选**：
                            # 原来这里写死「热榜2+国际2+国内2」，10 个席位里科技/财经/娱乐
                            # 一条都轮不到，跟用户「涉及头条/科技/财经/娱乐」的要求不符。
                            # 配额顺序仍是国际媒体优先（用户看得懂中文），再交给统一的挑选器。
                            _ordered = (_intl + _hot + _cn)
                            _annotate_cross_hot(_ordered)   # 启动播报也按"几家榜同上榜"优先
                            # 【2026-10-29 用户：「一定要定规则：常看常新」】启动这一路以前
                            # 漏了两条闸：它从不看"这条多久了"，也没把播过的记进名单。
                            # 于是"关机一天再开机"正好是最容易播到昨天新闻的场合，而且开机播完
                            # 的这 10 条几分钟后还能被「更新提醒」再播一遍。现在和自动/手动同一口径：
                            # ① 超过 24 小时的一律不进候选；② 6 小时内够一轮就只取 6 小时；
                            # ③ 26 小时内播过的**开机也不再播**（只记不查的话，重启一次就原样重播）；
                            # 挑不出新鲜的就跳过（开机那一下没人等着看"没有新的可播"，日志留一句就够）。
                            _ordered, _stale, _ = _balloon_fresh_cut(_ordered, prefer_hours=None)
                            if _stale:
                                _balloon_trace("启动播报：挡掉 %d 条超过 %d 小时的旧闻"
                                               % (_stale, BALLOON_FRESH_HOURS))
                            # 挑选 + 记账同一把锁（与刷新后的自动轮播互斥，见 _BALLOON_PICK_LOCK）：
                            # 两边同时挑会挑中同一批，用户开机就看到两遍同样的 10 条。
                            with _BALLOON_PICK_LOCK:
                                try:
                                    _seen_boot = _balloon_seen()
                                except Exception:
                                    _seen_boot = set()
                                if _seen_boot:
                                    _ordered = [x for x in _ordered
                                                if (x.get("id") or x.get("title")) not in _seen_boot]
                                if not _ordered:
                                    print("[ok] 启动气泡：候选都播过或没有 %d 小时内的新闻，跳过"
                                          "（常看常新：不拿旧闻凑数）" % BALLOON_FRESH_HOURS)
                                    return
                                _ordered, _s2, _held = _balloon_fresh_cut(
                                    _ordered, prefer_hours=BALLOON_FRESH_PREFER_HOURS,
                                    prefer_need=BALLOON_PLAY_COUNT)
                                if _held:
                                    _balloon_trace("启动播报：这一轮只播 %d 小时内的，"
                                                   "%d 条稍旧的让位"
                                                   % (BALLOON_FRESH_PREFER_HOURS, _held))
                                _pick = _pick_balloon_items(_ordered)[:BALLOON_PLAY_COUNT]
                                if not _pick:
                                    print("[warn] 启动气泡：没有可用条目，跳过")
                                    return
                                # 【2026-10-25 用户「不能几个网站的新闻重复投放」】
                                # 启动播报播过的这件事也得进"已播事件"记忆 —— 否则几分钟后
                                # 第一波「更新提醒」会把同一条新闻的别家稿再播一遍。
                                try:
                                    _balloon_event_remember(_pick)
                                except Exception:
                                    pass
                                # 【2026-10-29 常看常新 ③】开机这 10 条也是"播过的"：不记进名单，
                                # 下一轮刷新就会把它们当新条目再投一遍（用户：「轮播轮播，就是不能重复，反复」）。
                                # force 落盘：万一用户看完第一轮就退出程序，这份名单不能丢在 60 秒节流里。
                                try:
                                    _balloon_mark_shown([x.get("id") or x.get("title")
                                                         for x in _pick], force=True)
                                except Exception:
                                    pass
                            _ents = [_balloon_entry(x) for x in _pick]
                            _n_intl = sum(1 for x in _pick if x.get("region") == "intl")
                            print("[ok] 启动气泡：播报 %d 条（%s；含国际 %d 条）"
                                  % (len(_ents), _balloon_cls_tally(_pick), _n_intl))
                            _show_toast_list(_ents)
                        except Exception as _ex:
                            print(f"[warn] 启动气泡失败: {type(_ex).__name__}")
                    threading.Thread(target=_boot_balloon, daemon=True).start()
            except Exception as ex:
                print(f"[warn] 预热 {region} 失败: {type(ex).__name__}: {ex}")
                import traceback as _tb
                print(_tb.format_exc())

        # 国内抓完再抓国际 —— 这样「国内头条里剔出的国际新闻」已经就绪，
        # 能顺手并进国际板块（见 _spill_from_cn）。
        do_intl()

        # 【2026-10-11 五轮】兜底：启动时「国际」可能**抢在「国内」前面**抓完。
        # 真机日志里就是这顺序：`cn: 已有一轮抓取在进行，本轮跳过（单飞）` →
        # 紧接着 intl 收口 → 最后 cn 才收口。此时 `_spill_from_cn()` 读的
        # PRELOAD_ALL["cn"] 还没就绪（为空）→ 国内转来的国际新闻（cn-intl-news，
        # 例如「从选票到权力：德法意极右翼政党的不同制度路径」）在国际栏缺席，
        # 要等下一次 10 分钟刷新才出现。这里在两边都就绪后补一次，**只增不减**。
        try:
            _sp = _spill_from_cn()
            _cur = PRELOAD.get("intl") or []
            if _sp and _cur:
                _have = {(x.get("title") or "") for x in _cur}
                _add = [s for s in _sp if (s.get("title") or "") not in _have]
                if _add:
                    PRELOAD["intl"] = _select_fresh(_add + _cur, "intl")
                    _note_item_hosts(_add)
                    print(f"[ok] intl: 补入国内转来的国际新闻 {len(_add)} 条")
        except Exception as _ex:
            print(f"[warn] intl 补入转入条目失败: {type(_ex).__name__}")

    def refresh_loop():
        # 【2026-10-25 用户要求「热榜和头条的新闻条数太多，版面太大，把刷新频率缩短了，
        #  常看常新的原则」】抓取周期 10 分钟 → **5 分钟**一轮。
        #  气泡不受影响：「更新提醒」的间隔默认仍是 10 分钟（_toast_min_sec，托盘里可调 5/10/30/60），
        #  用户要的「及时」是页面及时，不是通知及时。
        # 一轮内重建 热榜 + 国内源 + 国际源，刷新后弹气泡（更新提醒 / 大事提醒）。
        # 【2026-10-09 修 中-7】改走 _collect_exclusive：与页面按需抓取（_kick_collect）
        # 共用同一把「正在抓取」的闸，保证同一 region 同一时刻只有一轮。
        # 已被占着就跳过本轮（下一轮 5 分钟后再来），绝不并发跑两轮。
        while True:
            time.sleep(300)          # 5 分钟一轮
            # 【2026-10-28 新增】开源热榜跟着每一轮敲门，但它自己有 6 小时的闸
            # （OSS_TTL）与失败节流（OSS_FAIL_TTL），所以绝大多数轮次这里直接跳过、
            # 不发请求。效果是：这一栏最长 6 小时换一次，程序重启/断网期间也不会空着。
            try:
                _kick_oss()
            except Exception:
                pass
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
                # 【2026-10-25 借鉴 vikiboss/60s + TrendRadar 时段调度】
                # 每天 08:00 / 20:00 各播一次「今日 60 秒」（可在「程序设置」里关）。
                _notify_digest()
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


# ============ 跨天归档与历史检索（2026-10-25 新增）============
# 【为什么做】程序叫「热点新闻检索」，但可搜的池子只活在内存里：
#   · 主列表只留当天（用户要求常看常新），往前翻就没了；
#   · 关掉程序，当天抓到的新闻也一起没了。
# 这里把**每轮抓到的完整池子**（PRELOAD_ALL，含前面几天）落到本机 SQLite，
# 搜索时能选「今天 / 近 7 天 / 近 30 天」，还能看一个词这几天热到什么程度。
#
# 【四条硬约束】
#   ① 只用标准库 sqlite3 —— 不新增第三方依赖（requirements.txt 不变，打包不用多收包）；
#   ② 归档是**本机私有数据**：只写 %LOCALAPPDATA%\hotnews\history\history.db，
#      不上传、不外发、页面上也不显示这个路径；表里只有新闻本身字段，
#      没有机器名、用户名、浏览记录之类的个人信息；
#   ③ 归档绝不能拖垮抓取：写库全包在 try 里，失败只打一行 [warn]，
#      并且 5 分钟内不再撞第二次（只读盘/满盘时不会每轮都报错）；
#   ④ 有保留上限：只留 HIST_KEEP_DAYS 天，且行数硬顶 HIST_MAX_ROWS ——
#      长期运行不能把用户的盘越占越大。
_HIST_DIR = os.path.join(os.getenv("LOCALAPPDATA") or tempfile.gettempdir(),
                         "hotnews", "history")
_HIST_PATH = os.path.join(_HIST_DIR, "history.db")
HIST_KEEP_DAYS = 30      # 只留最近 30 天（按 last_seen 淘汰：还在流转的条目不会被误删）
HIST_MAX_ROWS = 60000    # 行数硬顶，超了从最久没见过的开始删
# 查询窗口上限：能选到 90 天，但归档只留 30 天 —— 超出部分是空的。
# 页面拿到 oldestDay 就知道实际能搜到哪天，不会把"没搜到"当成"没发生过"。
HIST_KEEP_DAYS_MAX_QUERY = 90
_HIST_LOCK = threading.Lock()
_HIST_CONN = None
_HIST_DISABLED_UNTIL = 0.0    # 建库/写库失败后的冷静期终点
_HIST_PRUNE_UNTIL = 0.0       # 清旧最多每 6 小时一次
_HIST_STATS = {"written": 0, "lastWrite": 0, "error": ""}

# 摘要这一列叫 `summary` 不叫 `desc`：DESC 是 SQL 关键字，当列名要么处处加引号，
# 要么哪天写漏一次就是一个查不出来的语法错。对外字段名仍然叫 desc（与 /api/news 一致）。
_HIST_SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
  key        TEXT PRIMARY KEY,
  region     TEXT,
  channel    TEXT,
  label      TEXT,
  cls        TEXT,
  title      TEXT,
  summary    TEXT,
  link       TEXT,
  image      TEXT,
  published  INTEGER,
  day        TEXT,
  heat       INTEGER,
  translated INTEGER,
  times      INTEGER,
  first_seen INTEGER,
  last_seen  INTEGER
);
CREATE INDEX IF NOT EXISTS ix_news_day  ON news(day);
CREATE INDEX IF NOT EXISTS ix_news_seen ON news(last_seen);
"""

_HIST_COLS = ("key", "region", "channel", "label", "cls", "title", "summary",
              "link", "image", "published", "day", "heat", "translated",
              "times", "first_seen", "last_seen")

# 一条新闻被反复抓到（每 10 分钟一轮）不该变成 10 行重复数据，所以按主键 upsert：
#   · title / summary：直接换新 —— 后台翻译会把英文标题写回成中文，必须跟上来；
#   · label / cls / image：新值非空才覆盖，别被某一轮的解析失败抹掉已有的图；
#   · heat：取两边较大值（热榜数字只涨不跌，跌了多半是源站换口径，不是真降了）；
#   · times：被抓到几轮，页面用它说明「这条在归档里出现过几次」；
#   · first_seen 不更新（保留第一次见到的时间），last_seen 每轮刷新（淘汰看它）。
_HIST_UPSERT = (
    "INSERT INTO news (" + ",".join(_HIST_COLS) + ") VALUES ("
    + ",".join("?" for _ in _HIST_COLS) + ") "
    "ON CONFLICT(key) DO UPDATE SET "
    "title=excluded.title, summary=excluded.summary, "
    "label=CASE WHEN excluded.label<>'' THEN excluded.label ELSE news.label END, "
    "cls=CASE WHEN excluded.cls<>'' THEN excluded.cls ELSE news.cls END, "
    "image=CASE WHEN excluded.image<>'' THEN excluded.image ELSE news.image END, "
    "heat=MAX(news.heat, excluded.heat), "
    "translated=excluded.translated, last_seen=excluded.last_seen, "
    "times=news.times+1"
)

# 查询时一次最多取回这么多行（搜索与趋势各自用各自的 limit，都夹在 HIST_SCAN_MAX 以内）
HIST_SCAN_MAX = 2000


def _hist_day(ts):
    """时间戳 → 本地日期字符串（归档按天分组、按天筛窗口都靠它）。"""
    try:
        return datetime.fromtimestamp(int(ts), CST).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(CST).strftime("%Y-%m-%d")


def _hist_conn():
    """取（惰性建）归档库连接。建库失败 → 设冷静期并抛出让调用方吞掉。"""
    global _HIST_CONN, _HIST_DISABLED_UNTIL
    if _HIST_CONN is not None:
        return _HIST_CONN
    try:
        os.makedirs(_HIST_DIR, exist_ok=True)
        c = sqlite3.connect(_HIST_PATH, check_same_thread=False)
        # busy_timeout：抓取线程在写、页面线程在读，读的时候等一下而不是直接报错
        c.execute("PRAGMA busy_timeout=1500")
        c.executescript(_HIST_SCHEMA)
        c.commit()
        _HIST_CONN = c
        print(f"[ok] 新闻归档已就绪（保留 {HIST_KEEP_DAYS} 天）")
        return c
    except Exception as ex:
        _HIST_CONN = None
        _HIST_DISABLED_UNTIL = time.time() + 300
        _HIST_STATS["error"] = (f"{type(ex).__name__}: {ex}")[:160]
        print(f"[warn] 新闻归档打不开，本次运行不再尝试（不影响正常看新闻）: "
              f"{type(ex).__name__}")
        raise


def _hist_prune(c):
    """按保留期和行数上限清库。最多每 6 小时一次 —— 全表扫描不该每轮都做。"""
    global _HIST_PRUNE_UNTIL
    now = time.time()
    if now < _HIST_PRUNE_UNTIL:
        return
    _HIST_PRUNE_UNTIL = now + 6 * 3600
    cut = int(now) - HIST_KEEP_DAYS * 86400
    c.execute("DELETE FROM news WHERE last_seen < ?", (cut,))
    over = (c.execute("SELECT COUNT(*) FROM news").fetchone()[0] or 0) - HIST_MAX_ROWS
    if over > 0:
        c.execute("DELETE FROM news WHERE key IN ("
                  "SELECT key FROM news ORDER BY last_seen ASC LIMIT ?)", (over,))
    c.commit()


def _hist_rows(region, items):
    """条目字典 → 待写行。脏数据（没标题的）直接跳过，不因为一条坏数据让整轮写不进去。"""
    now = int(time.time())
    out = []
    for it in items or []:
        try:
            title = (it.get("title") or "").strip()
            if not title:
                continue
            link = (it.get("link") or "").strip()
            key = (it.get("id") or "").strip() or \
                hashlib.md5((link + title).encode("utf-8")).hexdigest()[:12]
            pub = int(it.get("published") or 0)
            out.append((
                key[:40], region or "", (it.get("channel") or "")[:60],
                (it.get("label") or "")[:60], (it.get("cls") or "")[:20],
                title[:300], (it.get("desc") or "")[:600], link[:600],
                (it.get("image") or "")[:900], pub, _hist_day(pub or now),
                int(it.get("heat") or 0), 1 if it.get("translated") else 0,
                1, now, now,
            ))
        except Exception:
            continue
    return out


def _hist_record(region, items):
    """把这一轮的完整池子落进归档。**绝不向外抛异常**：抓取与页面不能因为归档崩。"""
    global _HIST_DISABLED_UNTIL, _HIST_CONN
    if time.time() < _HIST_DISABLED_UNTIL:
        return 0
    rows = _hist_rows(region, items)
    if not rows:
        return 0
    try:
        with _HIST_LOCK:
            c = _hist_conn()
            c.executemany(_HIST_UPSERT, rows)
            c.commit()
            _HIST_STATS["written"] += len(rows)
            _HIST_STATS["lastWrite"] = int(time.time())
            _hist_prune(c)
        return len(rows)
    except Exception as ex:
        with _HIST_LOCK:
            _HIST_CONN = None            # 连接可能已经废了，下次重建
            _HIST_DISABLED_UNTIL = time.time() + 300
            _HIST_STATS["error"] = (f"{type(ex).__name__}: {ex}")[:160]
        print(f"[warn] 新闻归档写入失败（不影响抓取，5 分钟内不再重试）: "
              f"{type(ex).__name__}")
        return 0


def _hist_where(q, region, days):
    """拼查询条件（时间窗口 + 分区 + 关键词）。返回 (where_sql, args, 起始日)。"""
    start = (datetime.now(CST) - timedelta(days=max(1, days) - 1)).strftime("%Y-%m-%d")
    where, args = ["day >= ?"], [start]
    if region in ("cn", "intl", "hot"):
        where.append("region = ?")
        args.append(region)
    kw = (q or "").strip().lower()
    if kw:
        # instr + lower：子串匹配，语义与前端搜索框完全一致（英文不区分大小写，
        # 中文没有大小写问题）。**不用 LIKE** —— LIKE 把 % 和 _ 当通配符，
        # 用户搜「50%」会连到一堆不相干的条目。
        where.append("(instr(lower(title), ?) > 0 OR instr(lower(summary), ?) > 0)")
        args += [kw, kw]
    return " AND ".join(where), args, start


def _hist_item(r):
    """一行归档 → 前端那套条目字段（字段名与 /api/news 一致，卡片渲染直接复用）。"""
    return {
        "id": r[0], "region": r[1] or "", "channel": r[2] or "", "label": r[3] or "",
        "cls": r[4] or "", "title": r[5] or "", "desc": r[6] or "", "link": r[7] or "",
        "image": r[8] or "", "published": int(r[9] or 0), "heat": int(r[11] or 0),
        "rank": 0, "translated": bool(r[12]), "day": r[10] or "",
        "times": int(r[13] or 1),
    }


_HIST_SELECT = (",").join(_HIST_COLS)


def _hist_query(q="", region="", days=30, limit=200):
    """查归档（只读）。返回 (items, note)：note 非空 = 归档此刻不可用，前端要说清。"""
    days = max(1, min(int(days or 30), HIST_KEEP_DAYS_MAX_QUERY))
    limit = max(1, min(int(limit or 200), NEWS_LIMIT_MAX))
    where, args, _start = _hist_where(q, region, days)
    args.append(min(HIST_SCAN_MAX, limit * 8))   # 折叠重复之前多取一些，保证折叠后还够数
    try:
        with _HIST_LOCK:
            c = _hist_conn()
            rows = c.execute(
                f"SELECT {_HIST_SELECT} FROM news WHERE {where} "
                f"ORDER BY published DESC, last_seen DESC LIMIT ?", args).fetchall()
    except Exception:
        return [], "本机归档暂时读不到（不影响看当前新闻）"
    items = [_hist_item(r) for r in rows]
    # 同一篇稿子被连续几轮抓到、标题又被改写过，会留下多条几乎一样的归档。
    # 这里只做**确定性折叠**（同链接 / 归一化标题一样 / 一方是另一方且只差几个字），
    # 不跑"同一事件不同措辞"那一档 —— 用户明确在搜东西，别把别家的报道合掉。
    items = _dedup_same_event(items, same_wording_only=True)
    return items[:limit], ""


def _hist_trend(q="", region="", days=30):
    """按天统计归档条数，**缺失的日期补 0**（不补零的折线会把"没数据"画成"没人提")。"""
    days = max(1, min(int(days or 30), HIST_KEEP_DAYS_MAX_QUERY))
    where, args, _start = _hist_where(q, region, days)
    try:
        with _HIST_LOCK:
            c = _hist_conn()
            rows = c.execute(
                f"SELECT day, COUNT(*) FROM news WHERE {where} GROUP BY day",
                args).fetchall()
    except Exception:
        return [], 0, ""
    by = {str(r[0]): int(r[1] or 0) for r in rows}
    base = datetime.now(CST).date() - timedelta(days=days - 1)
    series = []
    for i in range(days):
        d = (base + timedelta(days=i)).strftime("%Y-%m-%d")
        series.append({"day": d, "count": by.get(d, 0)})
    return series, sum(x["count"] for x in series), ""


def _hist_stats():
    """归档概况（条数 / 覆盖到哪一天 / 文件多大），只读，失败回空概况。

    两个"最早"要分清，页面上说错了会误导用户：
      · `oldestDay` = 库里**稿子本身**最早的那天（RSS 里挂个两年前的日期是常事，
        这条稿子不在「近 30 天」窗口里，搜不到它）；
      · `sinceDay` = 归档**从哪天开始攒**（按第一次被抓到的时间算），
        这才是「近 30 天能搜到哪儿」的答案 —— 页面用的是这个。
    """
    info = {"rows": 0, "oldestDay": "", "newestDay": "", "sinceDay": "", "bytes": 0,
            "keepDays": HIST_KEEP_DAYS, "maxRows": HIST_MAX_ROWS,
            "lastWrite": _HIST_STATS["lastWrite"], "ready": False, "note": ""}
    try:
        with _HIST_LOCK:
            c = _hist_conn()
            r = c.execute("SELECT COUNT(*), MIN(day), MAX(day), MIN(first_seen) "
                          "FROM news").fetchone()
        info["rows"] = int(r[0] or 0)
        info["oldestDay"] = str(r[1] or "")
        info["newestDay"] = str(r[2] or "")
        info["sinceDay"] = _hist_day(r[3]) if (r[3] or 0) else ""
        info["ready"] = True
        try:
            info["bytes"] = int(os.path.getsize(_HIST_PATH))
        except Exception:
            pass
    except Exception:
        info["note"] = _HIST_STATS["error"] or "归档尚未就绪"
    return info


# ============ 归档的三个只读接口 ============


@app.get("/api/history")
def api_history():
    """只读：本机归档概况。页面用它说明「近 30 天」实际覆盖到哪天。"""
    s = _hist_stats()
    return JSONResponse({"version": VERSION, **s},
                        media_type="application/json; charset=utf-8")


@app.get("/api/search")
def api_search(q: str = Query(""), region: str = Query(""),
               days: int = Query(30), limit: int = Query(100)):
    """搜本机归档（跨天）。

    与 /api/news 的分工：`/api/news?q=` 搜**今天内存里的池子**，这里搜**落盘的 30 天**。
    参数越界一律夹取后正常返回（沿用 2026-10-09 那次「limit=300 回空数组」的教训，
    不用 FastAPI 的 ge/le 校验去回 422）。
    不带 q 时按天倒序回归档里最新的条目，给「翻历史」用。
    """
    items, note = _hist_query(q=q, region=region, days=days, limit=limit)
    if items:
        # 归档里的图同样是远程地址，走 /img 代理必须先登记域名，否则整屏 403
        _note_item_hosts(items)
    s = _hist_stats()
    return JSONResponse({"version": VERSION, "count": len(items),
                         "q": (q or "").strip(), "region": region or "",
                         "days": max(1, min(int(days or 30), HIST_KEEP_DAYS_MAX_QUERY)),
                         "items": items, "note": note,
                         "archive": {"ready": s["ready"], "rows": s["rows"],
                                     "oldestDay": s["oldestDay"],
                                     "newestDay": s["newestDay"],
                                     "sinceDay": s["sinceDay"]}},
                        media_type="application/json; charset=utf-8")


@app.get("/api/trend")
def api_trend(q: str = Query(""), region: str = Query(""), days: int = Query(30)):
    """只读：一个词（或整个分区）每天在归档里出现多少条 —— 前端画折线用。

    说清楚这个数字是什么：它是**本机归档里那一天的条数**，不是全网热度指数。
    抓取轮次、源站当天发稿量都会影响它，所以页面必须带上口径说明，不能假装是热度。
    """
    series, total, note = _hist_trend(q=q, region=region, days=days)
    peak = max(series, key=lambda x: x["count"]) if series else None
    return JSONResponse({"version": VERSION, "q": (q or "").strip(),
                         "region": region or "",
                         "days": max(1, min(int(days or 30), HIST_KEEP_DAYS_MAX_QUERY)),
                         "total": total, "series": series, "note": note,
                         "peak": peak if (peak or {}).get("count") else None},
                        media_type="application/json; charset=utf-8")


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


# 【2026-10-09】列表接口一次最多回多少条。前端两处（index.html / intl.html）都请求 limit=200，
# 所以上限就定 200；超出按 200 截断（不再因为 FastAPI 的 le 校验回 422 —— 见 api_news 的说明）。
NEWS_LIMIT_MAX = 200

# 【2026-10-25 待办 #62】内存池空了（断网 / 还在预热）时，首屏回落本机归档的窗口。
ARCHIVE_BACKFILL_DAYS = 7      # 第一档：先找最近 7 天
ARCHIVE_BACKFILL_MIN = 6       # 第一档少于这个数 → 放宽再查一次（不是"就放弃"）
ARCHIVE_BACKFILL_SPAN = 30     # 第二档：放宽到保留期（归档最多留 HIST_KEEP_DAYS 天）


def _news_from_archive(region, limit):
    """**只读盘、不联网**：从本机归档捞一批垫首屏。返回 `(items, note)`。

    为什么要它：`api_news` 原来在内存池为空时直接回 `items: []` + `loading: true`，
    断网时后台抓多久都不会成，用户开页面看到的就是空屏 —— 而服务端首屏（`index()`）走的
    是同一个 `api_news`，所以连 HTML 也是空的。可 history.db 里明明躺着最近几轮抓到的完整新闻，
    `_hist_item` 的字段名与 /api/news 一致（卡片渲染直接复用），不用就浪费了。

    三条边界：
      · 只读归档、不改内存、不触发抓取 —— 抓到东西之后下一轮刷新自然回到实时池子，
        缓存不会把页面锁死在旧闻上；
      · 时间口径说实话：这批的 `published` 原样带着，页面顶栏那行「最新 …」就是它们的真实时间，
        用户一眼看得出"这是缓存不是直播"，不靠额外文案自证；
      · 7 天窗口凑不满 ARCHIVE_BACKFILL_MIN 条就放宽到保留期再查一次（断网几天的机器上，
        归档里最近一批可能就是十来天前的，那也比空屏强）；两档都没货 → 回 `([], note)`，
        调用方退回原来的"正在抓取"空响应。归档读不到时 note 非空，日志里能看出是哪种"没货"。
    """
    want = min(max(int(limit or 0) * 3, 60), NEWS_LIMIT_MAX)   # 后面还要过折叠，多取一点才够首屏
    items, note = _hist_query(region=region, days=ARCHIVE_BACKFILL_DAYS, limit=want)
    if len(items) < ARCHIVE_BACKFILL_MIN:
        older, older_note = _hist_query(region=region, days=ARCHIVE_BACKFILL_SPAN, limit=want)
        if len(older) > len(items):
            items, note = older, older_note
    if not items:
        return [], note          # note 非空 = 归档此刻读不到；空 = 归档里确实没这一栏的货
    newest_day = max([(it.get("day") or "") for it in items]) or "?"
    return items, "本机归档 %s 的 %d 条" % (newest_day, len(items))


@app.get("/api/news")
def api_news(region: str = Query("cn", enum=["cn", "intl", "hot"]), q: str = Query(""), limit: int = Query(48, ge=1)):
    """条目接口。`limit` **超上限按上限截断并正常返回**（不再 422 / 不再空数组）。

    【2026-10-09 用户实测报的 bug】`GET /api/news?limit=300` 返回空数组，`limit=50/100` 正常。
    根因：这里原来声明 `Query(48, ge=1, le=200)`，FastAPI 对越界的 `le` 会直接回 422，
    前端 `js.items || []` 拿到的是错误响应体 → 渲染成"什么都没有"。用户看到的就是"空数组"。
    修法（用户口径：「超上限就按上限截断并正常返回」）：去掉 `le`，在函数里夹取，
    并在响应里带上实际生效的 `limit`，调用方一眼能看出被截断过。

    【2026-10-25 待办 #62】内存池为空（断网 / 还在预热）时不再直接回空数组：先读本机归档
    垫这一屏（`_news_from_archive`），响应里 `cached: true` + `cached_note` 说清这批是缓存；
    归档也没货才回 `loading: true` 的空响应。服务端首屏 `index()` 走的是同一个函数，
    所以断网时打开页面看到的就是归档那批，不再是空屏干等。
    """
    if region not in SOURCES:
        return JSONResponse({"error": "unknown region"}, status_code=400)
    limit = max(1, min(int(limit or NEWS_LIMIT_MAX), NEWS_LIMIT_MAX))
    # 【搜索走更宽的池子】用户原则：主列表常看常新、绝不看旧新闻；
    # 但程序叫「热点新闻检索」，搜索时必须能搜到更早的新闻。
    # 所以带关键词时改搜 PRELOAD_ALL（当天过滤之前的完整池子，含最近几天）。
    _kw = (q or "").strip()
    from_archive = False           # 首屏这批是不是本机归档垫的（响应里要老实标出来）
    backfill_note = ""
    if _kw and PRELOAD_ALL.get(region):
        all_items = PRELOAD_ALL[region]
    elif PRELOAD.get(region):
        all_items = PRELOAD[region]
    else:
        # 没有预热数据 → **绝不在请求里同步抓取**，立刻返回并让后台去抓。
        _kick_collect(region)
        # 【2026-10-25 待办 #62】断网 / 还在预热时不再交白卷：先用本机归档垫这一屏，
        # 后台照常重抓，抓成之后下一次刷新就回到实时池子。归档也没货才回原来的空响应。
        backfill, backfill_note = _news_from_archive(region, limit)
        if not backfill:
            return JSONResponse(
                {"version": VERSION, "count": 0, "items": [], "loading": True,
                 "message": "正在抓取，请稍候…"},
                media_type="application/json; charset=utf-8")
        from_archive = True
        all_items = backfill
        print("[ok] %s: 内存池空，首屏回落本机归档（%s）" % (region, backfill_note))
    # 去重（同标题）
    # 【2026-10-28 修 热榜退回单源 · 真正卡住的那一道】
    # 热榜条目的 published 恒为 0，`_select_fresh` 按时间倒序一排，它们必然全落在列表最末；
    # 而澎湃这类平台的 RSS 稿和自家榜单是**同一批标题**，RSS 排在前面 —— 按原顺序去重，
    # 热榜条目被自家 RSS 抢先挡掉。实测：池子热榜 27 条/2 家进这道，出去只剩 12 条/1 家，
    # 消失的正是澎湃全部 15 条（凤凰没有 RSS 源进 cn，所以它毫发无损）。
    # 热榜是独立榜单块（前端 hotlistHTML 单独渲染），同一条稿子既上榜单又发 RSS 时**保榜单**。
    # 所以先让热榜把标题占进 seen，再让新闻流去重。
    seen, items = set(), []
    for it in all_items:
        if it.get("cls") != "热榜":
            continue
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        items.append(it)
    for it in all_items:
        if it.get("cls") == "热榜":
            continue
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        items.append(it)
    # 【2026-10-10 用户反馈「气泡里好多重复：凤凰发的，其他网站也发」】
    # 标题一字不差只是第一层；同一件事被别家转发时标题几乎必然不同，所以再合并一层
    # 「同一件事」（判据与标定见 _dedup_same_event 上面的注释）。
    # 搜索时**不做这层**：用户明确在找某条新闻，宁可多给几条，
    # 不能因为"别家也发过"就让某条搜不到。
    # 【2026-10-25 新增】`with_also=True`：被合掉的兄弟稿挂到这条上（`also` / `also_n`），
    # 页面上就是「同一件事，几家在报」那个可展开的标记 —— 合并照旧，但不再"悄悄地少了一条"。
    if not (q or "").strip():
        # 【2026-10-28 修 热榜退回单源】热榜是各平台**各自的榜单**，本来就在报同一批热点。
        # 走②「同一事件不同措辞」的跨源合并，会把澎湃/红星的榜单整批折进凤凰，最后只剩一家
        # —— 这正是用户反复反馈的「热榜只有凤凰」的最终根因（不在聚合层，在出口去重层）。
        # 实测（本机 2026-10-28 21:4x，同一份池子）：
        #   带 q（跳过 dedup）→ 热榜 2 家（凤凰 3 + 澎湃 4）
        #   不带 q（走 dedup）→ 热榜 1 家（凤凰 9）
        # 所以热榜只做①确定性副本折叠（同 id / link / 归一化标题），不做②跨源同事件合并；
        # 新闻流（头条/科技/财经…）照旧走完整去重，重复报道仍然只留一条。
        # 【2026-10-28 实测定论】这一支**完全不能过去重**，一次都不能。
        # 同一份池子实测过三版：
        #   ① 走完整去重                → 27条/2家 变 12条/1家（澎湃 15 条全没）
        #   ② 改成 `same_wording_only=True`（只跳第②档）
        #                               → 仍然 12条/1家：第①档「确定性折叠」照样折
        #   ③ 完全不去重                → 27条/2家 完整保留 ✅
        # 原因：热榜是各平台**各自的榜单**（凤凰榜、澎湃榜），多家报同一批热点是常态、
        # 不是重复；把它折叠成一家，用户看到的就是"热榜只有凤凰"。
        # 真正重复的那层（同一条稿子的副本）进门处的「同标题去重」已经抹过了，
        # 这里再折一次纯属误杀。
        _hot_part = [it for it in items if it.get("cls") == "热榜"]
        _rest_part = _dedup_same_event(
            [it for it in items if it.get("cls") != "热榜"],
            with_also=True)
        items = _hot_part + _rest_part
    # 【2026-10-15 用户口径】国际栏出口再兜一道：
    # 收集那一刻 CGTN 这类英文源的标题**还是英文**，判据可能判不出"中国主体"；
    # 后台翻译把中文标题写回来之后才看得出来（真机实例：「习近平、博科出席北京合作文件签字仪式」
    # 「中国副总理呼吁中美贸易关系更加稳定」—— 收集时漏过，译文写回后就该被拦）。
    # 这里用**同一套** `_split_domestic_items`（幂等），只在 intl 生效。
    if region == "intl":
        _keep, _late = _split_domestic_items(items, "intl")
        if _late:
            print("[ok] intl: 出口复核再剔掉中国主体稿 %d 条（译文写回后才判得出来）" % len(_late))
            for _d in _late[:6]:
                print("     · [%s·%s] %s" % (_d.get("cls"), _d.get("channel"),
                                            (_d.get("title") or "")[:48]))
            items = _keep
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
    # 显式声明 charset=utf-8，避免个别客户端把 JSON 误判为 GBK 导致乱码。
    # 【2026-10-09 修 limit 越界】`limit` 已经在函数开头夹到 ≤ NEWS_LIMIT_MAX，
    # 响应里回带实际生效值，调用方能分辨"只有这么多条"和"被上限截断了"。
    return JSONResponse({"version": VERSION, "count": len(items[:limit]),
                         "limit": limit, "limit_max": NEWS_LIMIT_MAX,
                         # 来源上色表（见 SRC_HUES）：跟着数据一起给，首屏一次画对，
                         # 不用前端再拉一次接口、也不会"先灰一下再上色"。
                         "src_hues": SRC_HUES,
                         # 【待办 #62】这批是归档垫的还是内存池里的实时数据，响应里说实话；
                         # 卡片时间戳本来就原样带着（页面顶栏显示的就是它们的时间），
                         # 所以老版页面不改也能看出"这是缓存"，这两个字段是给调用方/日志用的。
                         "cached": from_archive,
                         "cached_note": backfill_note if from_archive else "",
                         "items": items[:limit]},
                        media_type="application/json; charset=utf-8")


@app.get("/api/sources")
def api_sources():
    """**只读**：每个新闻源的健康度（最近一次抓到没有 / 抓了几条 / 连续失败几次）。

    【2026-10-24 UI 整改】硬约束：打开/刷新这个面板**不得触发抓取**。
    所以这里只做一件事 —— 读内存里的观测表 `_SRC_HEALTH`（由抓取链路顺手写入）：
    不调 _kick_collect、不碰 _collect、不发任何网络请求、不读缓存文件。

    返回（只增不减，老调用方不受影响）：
      {version, updatedAt, intervalSec, lastCollect, totals:{total,ok,warn,fail,idle},
       sources:[{id,label,region,cls,status,lastTs,lastCount,failStreak,okCount,failCount,error}]}
    `status`：ok=本轮抓通且有条目 / warn=抓通但 0 条（疑似改版）/ fail=本轮失败 / idle=还没轮到。
    """
    with _SRC_HEALTH_LOCK:
        snap = {k: dict(v) for k, v in _SRC_HEALTH.items()}
    sources, seen_ids = [], set()
    for meta in _all_sources_meta():
        sid = meta.get("id")
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)
        ent = snap.get(sid) or {}
        sources.append({
            "id": sid,
            "label": ent.get("label") or meta.get("label") or sid,
            "region": ent.get("region") or meta.get("region") or "",
            "cls": ent.get("cls") or meta.get("cls") or "",
            "status": ent.get("status") or "idle",
            "lastTs": int(ent.get("lastTs") or 0),
            "lastCount": int(ent.get("lastCount") or 0),
            "failStreak": int(ent.get("failStreak") or 0),
            "okCount": int(ent.get("okCount") or 0),
            "failCount": int(ent.get("failCount") or 0),
            "error": ent.get("error") or "",
        })
    # 表里出现过但不在登记表里的（例如以后新增的热榜子源）也别丢
    for sid, ent in snap.items():
        if sid in seen_ids:
            continue
        sources.append({
            "id": sid, "label": ent.get("label") or sid,
            "region": ent.get("region") or "", "cls": ent.get("cls") or "",
            "status": ent.get("status") or "idle",
            "lastTs": int(ent.get("lastTs") or 0),
            "lastCount": int(ent.get("lastCount") or 0),
            "failStreak": int(ent.get("failStreak") or 0),
            "okCount": int(ent.get("okCount") or 0),
            "failCount": int(ent.get("failCount") or 0),
            "error": ent.get("error") or "",
        })
    totals = {"total": len(sources), "ok": 0, "warn": 0, "fail": 0, "idle": 0}
    for s in sources:
        st = s.get("status")
        if st in ("ok", "warn", "fail"):
            totals[st] += 1
        else:
            totals["idle"] += 1
    last_collect = max([s.get("lastTs") or 0 for s in sources] or [0])
    return JSONResponse({
        "version": VERSION,
        "updatedAt": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
        "intervalSec": 300,           # 与 refresh_loop 的 5 分钟一轮一致（2026-10-25 从 600 改）
        "lastCollect": int(last_collect),
        "totals": totals,
        "sources": sources,
    }, media_type="application/json; charset=utf-8")


# ============ 开源热榜（GitHub 近 30 天新锐榜，2026-10-28 用户点名新增） ============
# 【数据源怎么定的】GitHub 官方**没有 trending 接口**，本机也打不开 github.com 的网页版
#   热门页（这条卡了两天，见 projects\功能待办.md 的「GitHub 热榜栏目」）。能按星标排序的
#   只有 search，所以这一栏的口径是 `created:><30 天前的日期>` + `sort=stars` + `order=desc`
#   —— 近 30 天新建、按累计星标降序。实测本机直连 api.github.com：HTTP 200 / 0.9~1.6 秒。
#   为什么不用全站累计榜（stars:>100000）：那份常年几乎不动，一天刷十次是同一批，
#   放在按「常看常新」组织的页面里就是一块死展板；新锐榜每天都在变，才配叫一栏。
# 【为什么不并进新闻池】这一栏不是新闻：条目跨度 30 天、没有站方发布时间、不该参与
#   国内/国际分流。要是写成 SOURCES 里的一家源，就得同时改五处已经踩过坑定下来的口径
#   —— 时效收口 _select_fresh、无图剔除、_split_cn_scope 分流、气泡投放、本机归档。
#   所以它单独一条链路：自己抓、自己缓存（内存 + 磁盘各一份）、自己一个 /api/oss 接口，
#   前端把它挂成左栏的一个栏目 tab。抓不到就整块不出现，一条新闻都不会被它带崩。
# 【题图哪来的】用户定过「每条新闻必须有高清大图，没图的不要上」。仓库本身没有题图，
#   但 GitHub 官方会给每个仓库现生成一张 1200x600 的分享卡
#   （opengraph.githubassets.com/1/<owner>/<repo>），短边 600 过得了
#   MIN_IMG_SHORT_SIDE=300 那道门槛；实测本机直连 image/png / 1.0~3.9 秒（首次生成慢一些）。
#   图上只有仓库名与站方自己写的那句简介，本程序不抓 README、不下代码。
# 【合规】这一栏只有三样公开信息：仓库全名、站方自己写的一句话简介、仓库地址。
#   不复制正文、不做站内阅读页、每条都直达 github.com 原页面；权利人可按
#   docs/COMPLIANCE.md 第 1 节的 Issue 通道要求移除，核实后立即下线。
#   境外来源的例外登记在 docs/COMPLIANCE.md 第 7 节（章程 §9.1「默认只用境内来源」）。
# 【题材闸门】本程序不做时政与敏感内容的二次传播（章程 §9.1），命中 _OSS_BLOCK_RX 的
#   仓库一条都不展示 —— 宁可这一栏少几条，也不让它变成敏感内容的入口。
OSS_ENABLED = True
OSS_CLS = "开源热榜"
OSS_SOURCE_ID = "oss-github-stars"
OSS_SOURCE_LABEL = "GitHub"
OSS_WINDOW_DAYS = 30       # 「近 30 天新建」这个窗口本身是榜单定义，不是时效妥协
OSS_LIMIT = 20             # 一屏能扫完的量；再多没人看
OSS_TTL = 6 * 3600         # 榜单一天挪不了几次，6 小时抓一次足够（省境外接口额度）
OSS_FAIL_TTL = 600         # 抓不到时 10 分钟内不再撞（境内直连不通时别每轮都试一遍）
OSS_TIMEOUT = 12           # 实测 0.9~1.6 秒，给到 12 秒兜抖动；比它长的抖动就当失败
OSS_SEARCH_URL = "https://api.github.com/search/repositories"
OSS_IMAGE_BASE = "https://opengraph.githubassets.com/1/"
# 仓库全名必须是 owner/repo 这两段安全字符：它要拼进图片地址，放开了就能改写请求路径，
# 也挡不住站方返回一个畸形 full_name 把 /img 当成跳转入口。
OSS_NAME_RX = re.compile(r"^[\w.\-]{1,100}/[\w.\-]{1,100}$")
_OSS_BLOCK_RX = re.compile(
    "|".join(re.escape(w) for w in (
        "政治", "政府", "党政", "选举", "投票系统", "抗议", "示威", "游行", "舆情", "监控",
        "军事", "军工", "武器", "弹药", "导弹", "情报", "间谍", "无人机打击",
        "色情", "成人网站", "porn", "nsfw",
        "漏洞利用", "exploit", "0day", "zero-day", "botnet", "malware", "ransomware",
        "keylogger", "钓鱼", "phishing", "scam",
    )), re.I)
_OSS_CACHE_FILE = os.path.join(
    os.getenv("LOCALAPPDATA") or tempfile.gettempdir(), "hotnews", "oss_cache.json")
_OSS = {"ts": 0.0, "items": [], "note": "", "next_try": 0.0}
_OSS_LOCK = threading.Lock()
_OSS_BUSY = [False]


def _oss_window_date(days=OSS_WINDOW_DAYS, today=None):
    """`created:>` 后面那个日期。

    单独拆出来、并把「今天」做成可传的参数，是为了让单测能钉一个假日期来断言窗口口径 ——
    测试绝不拿真实时钟当判据（时钟一跨天，测试就假绿或假红，见 tests/test_oss_board.py）。
    """
    base = today or datetime.now(CST)
    return (base - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")


def _oss_search_url(days=OSS_WINDOW_DAYS, limit=OSS_LIMIT, today=None):
    """search 地址。`>` 必须转义成 %3E，GitHub 那边对裸 `>` 的容忍度不稳定，别赌。"""
    q = "created:>%s" % _oss_window_date(days, today)
    return OSS_SEARCH_URL + "?" + urllib.parse.urlencode(
        {"q": q, "sort": "stars", "order": "desc",
         "per_page": max(1, min(100, int(limit)))})


def _oss_created_ts(s):
    """GitHub 的 created_at 是 UTC ISO8601（2026-10-05T09:12:33Z）→ epoch 秒；解析不出给 0。"""
    t = (s or "").strip()
    if not t:
        return 0
    try:
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        return int(datetime.fromisoformat(t).timestamp())
    except Exception:
        return 0


def _oss_link(u):
    """只认 github.com 上的仓库地址；其它协议、其它域名一律当「没有链接」。

    前端的 safeLink() 只挡 javascript:/data:，这一层再按**域名**收一次：
    站方返回什么就展示什么，等于把榜单的跳转权交给了返回数据的那台服务。
    """
    try:
        p = urllib.parse.urlsplit((u or "").strip())
    except Exception:
        return ""
    if p.scheme not in ("http", "https"):
        return ""
    if (p.hostname or "").lower() not in ("github.com", "www.github.com"):
        return ""
    if p.username or p.password or p.port not in (None, 80, 443):
        return ""
    try:
        ipaddress.ip_address(p.hostname or "")
        return ""                     # 裸 IP 的"github.com"不是 github.com
    except ValueError:
        pass
    return "%s://%s%s" % (p.scheme, p.netloc.lower(), p.path)


def _oss_desc(r, days=OSS_WINDOW_DAYS, zh=""):
    """卡片摘要：**中文打头**，英文简介只做补充。

    用户 2026-10-07 定过一次口径（「删除不易读的英文 GitHub 仓库，改用国内源」），
    所以这一栏不能整屏是英文：先用一句中文说清"这是什么榜、什么语言"，
    然后有译文就上译文；翻译没开（config.json 里 translate.enabled 默认 false）
    就附站方原文、并标明「简介原文」，绝不把英文裸摆在中文页面上冒充正文。
    """
    lang = (r.get("language") or "").strip()
    bits = ["GitHub 近 %d 天新建" % max(1, int(days))]
    if lang:
        bits.append(lang)
    head = " · ".join(bits)
    base = _clean(r.get("description") or "")[:200]
    zh = (zh or "").strip()
    if zh:
        return (head + "｜" + zh)[:240]
    if base:
        return (head + "｜简介原文：" + base)[:240]
    return head


def _oss_items_from_payload(payload, days=OSS_WINDOW_DAYS, limit=OSS_LIMIT,
                            today=None, translate=None):
    """search 响应 → 卡片条目。

    **纯函数**：不发网络请求、不读真实时钟（`today` 可注入）、翻译器由调用方注入
    （生产传 `_translate_text`，测试传一张假词典）。这样"口径"能测，"网络"不背测。
    字段沿用新闻卡片那一套（title/desc/link/image/published/heat/rank/label/cls），
    前端 cardHTML() 不用为这一栏另写一套渲染；额外只多 `stars` / `lang` / `created_text`
    三个字段，用来把星标数、语言、创建日期显示成中文卡片上看得懂的徽章。
    """
    rows = (payload or {}).get("items") or []
    keep = []
    seen, blocked, badlink = set(), 0, 0
    for r in rows:
        if not isinstance(r, dict) or r.get("archived"):
            continue
        name = (r.get("full_name") or "").strip()
        if not OSS_NAME_RX.match(name):
            badlink += 1
            continue
        key = name.lower()
        if key in seen:
            continue
        link = _oss_link(r.get("html_url"))
        if not link:
            badlink += 1
            continue
        base = _clean(r.get("description") or "")
        # 闸门看的是"这条会以什么面目出现"的所有字段：仓库名、简介、站方打的标签、
        # 还有语言分类 —— GitHub 的语言桶里就有 Porn/NSFW 这类，只查简介会漏过去。
        hay = " ".join([(r.get("language") or "").strip(), name, base,
                        " ".join(r.get("topics") or [])])
        if _OSS_BLOCK_RX.search(hay):
            blocked += 1
            continue
        try:
            stars = max(0, int(r.get("stargazers_count") or 0))
        except Exception:
            stars = 0
        try:
            forks = max(0, int(r.get("forks_count") or 0))
        except Exception:
            forks = 0
        lang = (r.get("language") or "").strip()[:40]
        ts = _oss_created_ts(r.get("created_at"))
        zh = ""
        if base and translate is not None:
            try:
                zh = (translate(base) or "").strip()
            except Exception:
                zh = ""
        seen.add(key)
        keep.append({
            "id": hashlib.md5(("oss-" + key).encode("utf-8")).hexdigest()[:12],
            "region": "cn",
            "channel": OSS_SOURCE_ID,
            "label": OSS_SOURCE_LABEL,
            "cls": OSS_CLS,
            "title": name[:120],
            "desc": _oss_desc(r, days, zh),
            "desc_en": base[:240],
            "link": link,
            "image": OSS_IMAGE_BASE + name,
            "published": ts,
            "created_text": (datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d")
                             if ts else ""),
            "heat": stars,
            "stars": stars,
            "forks": forks,
            "lang": lang,
            "source": "%s 开源热榜" % OSS_SOURCE_LABEL,
            "translated": bool(zh),
        })
    # 接口说按星标排，但闸门剔除过条目、也防它哪天改口 —— 这里自己再按星标定一次序，
    # 同数按仓库名，保证 rank 稳定可复现（测试就靠这一条断言名次连续）。
    keep.sort(key=lambda x: (-x["stars"], x["title"].lower()))
    out = keep[:max(1, int(limit))]
    for i, it in enumerate(out, 1):
        it["rank"] = i
    if blocked or badlink:
        print("[ok] %s: 闸门剔除 敏感题材 %d 条、地址不合规 %d 条" % (OSS_CLS, blocked, badlink))
    return out


def _oss_load_disk():
    """把上次抓到的榜从磁盘读回来（断网、重启后首屏照样有内容）。"""
    try:
        if not os.path.exists(_OSS_CACHE_FILE):
            return 0, ""
        with open(_OSS_CACHE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        items = d.get("items") or []
        if not isinstance(items, list) or not items:
            return 0, ""
        with _OSS_LOCK:
            _OSS["items"] = items
            _OSS["ts"] = float(d.get("ts") or 0)
            _OSS["note"] = "本机缓存"
        return len(items), d.get("window") or ""
    except Exception:
        return 0, ""


def _oss_save_disk():
    try:
        os.makedirs(os.path.dirname(_OSS_CACHE_FILE), exist_ok=True)
        with _OSS_LOCK:
            blob = {"ts": _OSS["ts"], "window": OSS_WINDOW_DAYS, "items": _OSS["items"]}
        with open(_OSS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(blob, f, ensure_ascii=False)
    except Exception as ex:
        print("[warn] 开源热榜缓存写盘失败: %s" % type(ex).__name__)


def _oss_snapshot():
    with _OSS_LOCK:
        return list(_OSS["items"]), float(_OSS["ts"]), _OSS["note"]


def _oss_prewarm(items, n=8):
    """把首屏要用的那几张分享卡提前取回**图片缓存**（前端一发请求就命中，不用排队画图）。

    为什么值得多做这一步：GitHub 的 og 卡是站方**现画**的，首次访问实测 1~4 秒；
    再赶上新闻那一轮正在批量补图（连接池就那么几个口子），浏览器能排到 20 秒以上 ——
    2026-10-28 本机实测就是这个样子，看着像"这一栏全是白框、坏了"。
    只预热前 n 张（「全部」视图露 6 张，点进这一栏看全的再等下一轮缓存），
    放在后台线程里做，取不到也不当回事：图片本来就允许失败，一条新闻都不该被它拖住。
    """
    rows = (items or [])[:max(0, int(n))]
    ok = 0
    for it in rows:
        u = (it or {}).get("image") or ""
        if not u.startswith("https://"):
            continue
        try:
            # timeout 给到 20 秒而不是默认的 10 秒：站方现画一张卡实测最慢的一张 10.4 秒
            # （榜首那个仓库），默认 10 秒正好卡在超时线上 —— 预热自己超时了，
            # 浏览器那一路就还得再等一遍 10 秒。预热在后台线程里，多等几秒没人看见，
            # 但 /img 的默认超时是 10 秒，卡在超时线上就会时不时给前端一个失败。
            res = _fetch(u, timeout=20)
            if isinstance(res, tuple):
                if res[0]:
                    ok += 1
            elif res:
                ok += 1
        except Exception:
            pass
    if rows:
        print("[ok] %s: 题图预热 %d/%d 张（浏览器那边就不用等站方现画）" % (OSS_CLS, ok, len(rows)))


def _oss_meta():
    """这一栏在「源健康度」面板里那一行的身份（伪源字典，跟热榜那套一个写法）。"""
    return {"id": OSS_SOURCE_ID, "label": OSS_SOURCE_LABEL,
            "region": "cn", "cls": OSS_CLS}


def _oss_refresh():
    """抓一轮开源热榜。

    成功 → 换掉内存与磁盘那两份，返回条数；
    失败 → **保留上一批**（旧榜继续显示，比整栏消失好），记 next_try 节流，返回 -1。
    这里不抛异常：这一栏再难看也不该把抓取线程带崩。
    """
    url = _oss_search_url()
    _meta = _oss_meta()
    try:
        raw = _fetch(url, timeout=OSS_TIMEOUT,
                     extra_headers={"Accept": "application/vnd.github+json"})
    except Exception as ex:
        raw = None
        print("[warn] 开源热榜抓取异常: %s" % type(ex).__name__)
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        with _OSS_LOCK:
            _OSS["next_try"] = time.time() + OSS_FAIL_TTL
            _OSS["note"] = "抓取失败，显示上一批"
        _note_src_health(_meta, False, 0, "抓取失败（接口不可达或额度用尽）")
        print("[warn] 开源热榜: 本轮没抓到，保留上一批（%d 条）" % len(_oss_snapshot()[0]))
        return -1
    try:
        payload = json.loads(raw.decode("utf-8", "ignore"))
    except Exception as ex:
        with _OSS_LOCK:
            _OSS["next_try"] = time.time() + OSS_FAIL_TTL
            _OSS["note"] = "响应解析失败"
        _note_src_health(_meta, False, 0, "响应解析失败")
        print("[warn] 开源热榜: 响应不是合法 JSON（%s）" % type(ex).__name__)
        return -1
    use_tr = bool(CONFIG.get("translate", {}).get("enabled", False))
    items = _oss_items_from_payload(
        payload, today=datetime.now(CST),
        translate=(_translate_text if use_tr else None))
    if not items:
        with _OSS_LOCK:
            _OSS["next_try"] = time.time() + OSS_FAIL_TTL
            _OSS["note"] = "抓到 0 条（题材闸门全滤掉了？）"
        _note_src_health(_meta, True, 0, "抓到 0 条")
        print("[warn] 开源热榜: 抓到 0 条，这一栏先收起")
        return 0
    with _OSS_LOCK:
        _OSS["items"] = items
        _OSS["ts"] = time.time()
        _OSS["note"] = "实时"
        _OSS["next_try"] = 0.0
    _note_item_hosts(items)       # 图片域名（opengraph.githubassets.com）登记进 /img 白名单
    _oss_save_disk()
    _note_src_health(_meta, True, len(items))
    print("[ok] %s: %d 条（窗口 近 %d 天新建 · 星标降序）" % (OSS_CLS, len(items), OSS_WINDOW_DAYS))
    _oss_prewarm(items)           # 后台把首屏那几张题图先取回来（见函数里的实测理由）
    return len(items)


def _kick_oss(force=False):
    """后台补一轮开源热榜（单飞 + 过期判定 + 失败节流）。抢不到闸就什么都不做。"""
    if not OSS_ENABLED:
        return False
    now = time.time()
    with _OSS_LOCK:
        if _OSS_BUSY[0]:
            return False
        if not force and (now - _OSS["ts"] < OSS_TTL or now < _OSS["next_try"]):
            return False
        _OSS_BUSY[0] = True

    def _bg():
        try:
            _oss_refresh()
        except Exception as ex:
            print("[warn] 开源热榜后台异常: %s" % type(ex).__name__)
        finally:
            with _OSS_LOCK:
                _OSS_BUSY[0] = False

    threading.Thread(target=_bg, daemon=True).start()
    return True


@app.get("/api/oss")
def api_oss():
    """开源热榜接口。**只读缓存优先**：缓存没过期就直接回，一条网络请求都不发；
    过期或还空着才 kick 后台一轮，本轮先回旧榜（旧榜也没有就是 loading + 空数组）。

    前端拿不到条目就把这一栏整块收起 —— 这一栏是加料，不该在任何情况下报错。
    """
    _kick_oss()
    items, ts, note = _oss_snapshot()
    _note_item_hosts(items)
    return JSONResponse({
        "version": VERSION,
        "cls": OSS_CLS,
        "window_days": OSS_WINDOW_DAYS,
        "count": len(items),
        "updatedAt": int(ts),
        "source": note or ("本机缓存" if items else "等待抓取"),
        "loading": bool(OSS_ENABLED) and not items,
        "enabled": bool(OSS_ENABLED),
        "items": items,
    }, media_type="application/json; charset=utf-8")


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
def api_update_check(request: Request):
    """手动检查更新：返回最新版本信息；有新版则顺带触发后台下载"""
    # 【2026-10-10 补闸门】这个接口**有副作用**：一旦发现新版，它会在后台把升级包
    # 下载到固定路径、并写升级状态 —— 而它原来是**裸 GET、没有任何鉴权**。
    # 本机任意进程、或用户访问的任意网页（简单 GET，无预检）都能拿它去触发下载。
    # 现在补上与其它副作用接口同一套闸门：同源 + 仅本机 + 本机令牌。
    # 前端 index.html 里对应的那次调用**已同步带上令牌头**，界面不受影响。
    _deny = _guard_mutating(request)
    if _deny is not None:
        return _deny
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
        _balloon_shown_flush()      # 同上：升级重启也别把最近 60 秒播过的名单丢掉
        sys.stdout.flush()
        os._exit(0)
    threading.Timer(0.15, _die).start()
    threading.Thread(target=_die, daemon=True).start()
    return {"ok": True, "message": "正在更新并重启"}


@app.get("/api/autostart")
def api_autostart_get(request: Request):
    """读开机自启状态（界面上的开关用它回显）。"""
    # 【2026-10-10 补闸门】与同一个路由的 POST 对齐 —— 读注册表状态也不该对任意
    # 本机页面开放（一个网页能据此判断"你装了它、还开着自启"）。
    # 前端 index.html 的读取处已同步带上令牌头。
    _deny = _guard_mutating(request)
    if _deny is not None:
        return _deny
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

@app.get("/api/digest")
def api_digest(n: int = Query(DIGEST_COUNT, ge=1, le=30),
               fmt: str = Query("json", enum=["json", "txt"])):
    """「今日 60 秒」日报：本机当天池子里按栏目配额挑 n 条（默认 15）。

    借鉴 vikiboss/60s 的「每天 60 秒读懂世界」，但**不请求任何外部接口**：
    条目全部来自本机已经抓好的国内 / 热榜 / 国际三个板块，
    与右下角气泡用的是同一个挑选器（`_pick_balloon_items`），口径不会分叉。
    `fmt=txt` 直接给可粘贴的纯文本（页面「复制」按钮用的就是它）。
    """
    items = _daily_digest(n)
    head, text = _digest_text(items)
    if fmt == "txt":
        return PlainTextResponse(head + "\n" + text,
                                 media_type="text/plain; charset=utf-8")
    return JSONResponse({"version": VERSION, "head": head, "count": len(items),
                         "at": int(time.time()), "lines": _digest_lines(items),
                         "text": text},
                        media_type="application/json; charset=utf-8")


@app.get("/api/notify")
def api_notify_get(request: Request):
    """读通知设置（夜间免打扰 / 每日速览），界面开关回显用。

    【2026-10-25 待办 #65】多带一份 `balloon`：最近 12 条气泡投放决定（播了几条 / 为什么没播）。
    只增不减，页面读的还是原来那几项；排查"气泡怎么不响了"时不用去翻 hotnews.log。
    """
    _deny = _guard_mutating(request)
    if _deny is not None:
        return _deny
    return JSONResponse({**_notify_cfg(),
                         "balloon": list(reversed(_BALLOON_TRACE))},
                        media_type="application/json; charset=utf-8")


@app.post("/api/notify")
async def api_notify_set(request: Request, payload: dict = None):
    """改通知设置，写本机私有配置 config.local.json 的 notify 段（不进版本库、不上传）。

    每一项都有回退：时间格式不合法就不改这一项；把 quiet_on 关掉就回到"全天都弹"，
    和改动前的行为完全一致。
    """
    _deny = _guard_mutating(request)
    if _deny:
        print("[warn] /api/notify 被拒绝：来源未通过本机校验")
        return _deny
    cur = _notify_cfg()
    patch = {}
    body = payload or {}
    for k in ("quiet_on", "digest_on", "hold_on"):
        if k in body:
            patch[k] = bool(body[k])
    for k in ("quiet_start", "quiet_end"):
        if k in body:
            v = str(body[k]).strip()
            if _hm_to_min(v, -1) >= 0:
                patch[k] = v
    if "digest_times" in body:
        raw = body["digest_times"]
        parts = raw if isinstance(raw, (list, tuple)) else str(raw).split(",")
        ok = [str(x).strip() for x in parts if _hm_to_min(x, -1) >= 0]
        if ok:
            patch["digest_times"] = ",".join(ok)
    if patch:
        _save_notify_cfg(patch)
        cur = _notify_cfg()
        print("[ok] 通知设置已更新：%s" % json.dumps(patch, ensure_ascii=False))
    return JSONResponse(cur, media_type="application/json; charset=utf-8")


@app.get("/api/ui")
def api_ui_get(request: Request):
    """读界面偏好，并回一份"气泡实际会用的配色"，页面和排查都能对上（本机校验同 /api/notify）。"""
    _deny = _guard_mutating(request)
    if _deny is not None:
        return _deny
    pal = _balloon_palette()

    def _hx(ref):
        r, g, b = _rgb_of(ref)
        return "#%02x%02x%02x" % (r, g, b)

    # 这一刻气泡实际会用的版式（auto 已按有没有图分流完），
    # 排查"选了聚光灯怎么出速递"时一眼能看出来。
    _cur = getattr(BALLOON, "cur", None) or ()
    _has_img = bool(len(_cur) > 4 and _balloon_img_local(_cur[4] or ""))
    return JSONResponse({**_ui_cfg(),
                         "resolved": {"theme": pal["theme"], "accent": pal["accent_name"],
                                      "balloon": _balloon_pick_style(has_img=_has_img),
                                      "card": _hx(pal["card"]), "txt": _hx(pal["txt"]),
                                      "accent_hex": _hx(pal["accent"])}},
                        media_type="application/json; charset=utf-8")


@app.post("/api/ui")
async def api_ui_set(request: Request, payload: dict = None):
    """控制台把它**当前生效**的主题/主色回报给本机，桌面气泡据此同步配色。

    只认页面那几档取值，别的一律忽略（不写脏值）；没写过就保持 "auto"，
    表现和控制台「跟随系统」的默认口径一致。
    """
    _deny = _guard_mutating(request)
    if _deny:
        print("[warn] /api/ui 被拒绝：来源未通过本机校验")
        return _deny
    body = payload or {}
    patch = {}
    t = str(body.get("theme") or "").strip().lower()
    if t in ("auto", "light", "dark"):
        patch["theme"] = t
    a = str(body.get("accent") or "").strip().lower()
    if a in ("default", "blue", "green", "violet", "amber"):
        patch["accent"] = a
    # 播报样式：页面和托盘菜单都写这一个键；脏值忽略，不让一键盘配置把气泡搞成白卡。
    b = str(body.get("balloon") or "").strip().lower()
    if b in BALLOON_STYLES:
        patch["balloon"] = b
    if patch:
        _save_ui_cfg(patch)
        print("[ok] 界面偏好已同步：%s" % json.dumps(patch, ensure_ascii=False))
    return JSONResponse(_ui_cfg(), media_type="application/json; charset=utf-8")


@app.get("/api/interest")
def api_interest_get():
    """本机兴趣档（只读）：各类权重 + 这一轮实际用的栏目配额。

    放在接口上是为了**可验证**：用户在控制台点开几条科技稿后，这里能直接看到
    科技权重上去、配额从 1 席变成 2~3 席 —— 不必去翻 %LOCALAPPDATA% 的 json。
    只回本机内存/本机文件里的数字，不联网、不上传。
    """
    w = _interest_weights()
    return {"weights": {k: round(v, 2) for k, v in
                        sorted(w.items(), key=lambda kv: -kv[1])},
            "quota": [[c, n] for c, n in _interest_quota()],
            "default": [[c, n] for c, n in BALLOON_CLS_QUOTA],
            "half_life_days": INTEREST_HALF_LIFE_DAYS}


@app.post("/api/interest")
async def api_interest_add(request: Request, payload: dict = None):
    """控制台里点开一条新闻 → 给它的栏目记一分（气泡点开走同一条内部函数）。

    有副作用（写本机文件），所以照样过令牌 + 同源 + 回环三道闸；
    页面那边是"点开就发、不等回包"（keepalive），发失败也不影响打开原文。
    """
    _deny = _guard_mutating(request)
    if _deny:
        print("[warn] /api/interest 被拒绝：来源未通过本机校验")
        return _deny
    cls = _interest_class((payload or {}).get("cls"))
    if not cls:
        return JSONResponse({"ok": False, "message": "缺少 cls"},
                            status_code=400, media_type="application/json; charset=utf-8")
    _interest_bump(cls, 1.0)
    return JSONResponse({"ok": True, "cls": cls,
                         "weights": {k: round(v, 2) for k, v in _interest_weights().items()}},
                        media_type="application/json; charset=utf-8")


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
    _balloon_shown_flush()      # 常看常新 ③：os._exit 不走 atexit，播过名单在退出前落盘

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
    # 【2026-10-10 修 声明不符】默认值从 True 改成 False。
    # 翻译会把条目标题/摘要发到**第三方服务**（mymemory / deepl 在境外，baidu 在境内），
    # 这与 docs/COMPLIANCE.md「不采集、不上传任何用户数据」「默认只用境内……」两句容易读成冲突，
    # 也和 docs/SECURITY.md 里承认的"可选机器翻译"口径对不上。
    # 现在：**默认关闭**；用户显式打开才会出网。对应披露写进 docs/COMPLIANCE.md 与 README.md。
    if not CONFIG.get("translate", {}).get("enabled", False):
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
# 不设限的话任何本机脚本都能拿它把翻译服务的免费额度刷光
# （额度用完国际板块就整体退回英文）。
# 【2026-10-10】调用它的闸门见下面 api_translate 里的 _guard_mutating。
TRANS_MAX_CHARS = 2000


@app.get("/api/translate")
def api_translate(request: Request, text: str = Query(""), target: str = "zh-CN"):
    # 【2026-10-10 补闸门】原来这个接口没有任何鉴权（注释写"本机页面用"，
    # 但前端 index.html / intl.html **一条都没调它**）。风险实在：它会把传进来的文本
    # 发到第三方翻译服务并消耗免费额度 —— 本机任意进程、或用户访问的任意网页
    # （简单 GET、无预检）都能拿它刷额度，额度刷光国际板块就整体退回英文。
    # 现在补上与其它副作用接口同一套闸门：同源 + 仅本机 + 本机访问令牌。
    _deny = _guard_mutating(request)
    if _deny is not None:
        return _deny
    cfg = CONFIG.get("translate", {})
    if not cfg.get("enabled", False) or not text:
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


# 【2026-10-18 修】界面模板读不到时的说明页 —— 服务是好的，但页面文件没了。
# 用文字页而不是裸露的 <h1>hotnews</h1>，好让用户知道发生了什么、点一下就好。
_MISSING_UI_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>热点新闻 · 界面文件缺失</title></head>
<body style="margin:0;font-family:system-ui,'Microsoft YaHei',sans-serif;background:#0f1115;color:#e8eaf0">
<div style="max-width:640px;margin:12vh auto;padding:0 24px;line-height:1.9">
<h1 style="font-size:22px">界面文件缺失</h1>
<p>抓取服务本身是正常的（数据接口可用），但页面模板读不到 ——
通常是临时目录被清理时把界面文件删掉了。</p>
<p>请按下面任一种方式恢复：</p>
<ol style="padding-left:20px">
<li>关闭本页，从<strong>桌面 / 开始菜单 / 右下角托盘图标</strong>重新打开「热点新闻」；</li>
<li>还不行就结束进程后重开：任务管理器里结束 <code>热点新闻.exe</code>，再双击程序。</li>
</ol>
<p style="color:#8b93a7;font-size:13px">作者：海风（kele551） ·
https://gitcode.com/kele551/hotnews</p>
</div></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    v = _load_version()
    ssr = ""
    tpl = _html_template("index.html")      # 【修 低-8】读过一次就走缓存（文件改了会自动重读）
    if tpl:
        # 【2026-10-24 修「关掉窗口再打开，又像重新加载了一遍」】
        # 原来首屏塞的是 `PRELOAD["cn"][:60]` —— 那是**没去重、没合事件、没排序**的原始前 60 条；
        # 而页面一起来，JS 立刻拉的是 `/api/news?region=cn&limit=200`（同标题去重 + 同一件事合并 +
        # 热榜前置 + 按时间排）。两份必然不同 → 每次打开都要"先画一版、再推翻重画一版"，
        # 用户看到的就是"又在重新加载"。
        # 现在首屏直接调**同一个** api_news（FastAPI 的装饰器原样返回函数，可以当普通函数调），
        # 数据与前端那次拉取逐条一致，前端 sig 判等后就不再重绘 —— 一次到位。
        # 上限取 NEWS_LIMIT_MAX，与前端写死的 limit=200 同值；哪天改常量也不会再错开。
        try:
            _first = json.loads(api_news(region="cn", q="", limit=NEWS_LIMIT_MAX).body)
            _items = _first.get("items") or []
        except Exception as _e:
            # 兜底：这条路径挂了也不能让首屏空着，退回原来的原始预热表
            print("[warn] 首屏 SSR 复用 /api/news 失败，退回预热表前 %d 条: %s" % (60, _e))
            _items = PRELOAD.get("cn", [])[:60]
        # 【2026-10-28】开源热榜：只读内存/磁盘缓存的快照，首屏这一步不产生任何外部请求
        try:
            _oss_snap = _oss_snapshot()
        except Exception:
            _oss_snap = ([], 0.0, "")
        payload = {
            "version": VERSION,
            "region": "cn",
            # 【修 高-4】有副作用的接口（退出/更新/开机自启）要带这个令牌
            "token": TOKEN,
            # 把预热好的条目直接塞进首屏，打开页面立刻有内容，不用等 JS 再拉一次
            "items": _items,
            # 来源上色表：模块级常量，和 /api/news 回带的是同一份，
            # 首屏那批卡片一次就染对颜色，不会"先灰一下再上色"
            "src_hues": SRC_HUES,
            # 【2026-10-28 新增】开源热榜：首屏直接给内存里那一批（**只读快照，不发请求**，
            # 抓取由 _warmup / /api/oss 的后台线程负责）。没有就是空数组，前端收起这一栏。
            # oss_win / oss_at 一起带：窗口天数只有服务端一个常量说了算，别让前端硬写 30，
            # 改了 OSS_WINDOW_DAYS 首屏那句「近 N 天新建」才跟得上。
            "oss": _oss_snap[0], "oss_win": OSS_WINDOW_DAYS, "oss_at": int(_oss_snap[1]),
        }
        # 【修 高-1】首屏这几条的图片域名也登记进 /img 白名单
        _note_item_hosts(payload["items"])
        # 【2026-10-28】开源热榜的题图在 opengraph.githubassets.com 上，同样要登记
        _note_item_hosts(payload["oss"])
        blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        ssr = tpl.replace("__SSR_DATA__", blob)
        ssr = ssr.replace('__SSR_REGION__', "cn")
        ssr = ssr.replace("__SSR_VERSION__", VERSION)
        return HTMLResponse(ssr, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})
    # 【2026-10-18 修】原来是裸露的 "<h1>hotnews</h1>"，用户完全看不出发生了什么，
    # 只会说"打开没有显示"。现在明确告诉用户界面文件丢了、怎么办（服务本身是好的）。
    return HTMLResponse(_MISSING_UI_PAGE, status_code=503,
                        headers={"Cache-Control": "no-store"})


# 【2026-10-09 修 低-8】静态 HTML 模板缓存：`/` 与 `/world` 原来**每个请求都读一次盘**
# （首页每次刷新都重新读 90KB 的 index.html）。模板是随程序分发的静态资源，不必每请求解码一遍。
# 【2026-10-25 改】缓存改成**认文件的 mtime+大小**：每次请求只做一次 os.stat（微秒级），
#   文件没变直接用缓存，变了立刻重读。理由：
#     · 维护者在源码态改 static/ 里的模板，原来要重启程序才看得见，改一次等一次；
#     · 打包态的「手工补丁」通道（直接编辑 %LOCALAPPDATA%\hotnews\static-<版本>\ 里那份 html，
#       不用重新打包）原来也必须重启才生效 —— 现在刷新页面就生效。
#   仍然是"不重复解码同一个文件"，低-8 那次要省的部分没丢。
_HTML_TPL = {}


def _html_template(name):
    """读 static/<name>（带缓存）；读不到返回空串（调用方各自兜底）。

    【2026-10-18 修】读失败**不再缓存空串**：原来一次读失败就把 "" 永久记住，
    即使文件后来回来了（或换了稳定目录）页面也一直是空的，只能重启程序。
    现在每次都重试，成功后才进缓存。
    """
    for d in (STATIC_DIR, os.path.join(BASE_DIR, "static")):
        p = os.path.join(d, name)
        try:
            st = os.stat(p)
        except Exception:
            continue
        mark = (d, st.st_mtime_ns, st.st_size)
        old = _HTML_TPL.get(name)
        if old and old[0] == mark:
            return old[1]
        try:
            with open(p, "r", encoding="utf-8") as f:
                tpl = f.read()
        except Exception:
            continue
        if tpl:
            _HTML_TPL[name] = (mark, tpl)
            return tpl
    return ""


def _load_version():
    p = _res("version.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"version": VERSION}


# 【2026-10-18 修】目录可能在运行中被清空/删除：mount 前先确保它存在，
# 否则 StaticFiles 会在**请求时**抛 RuntimeError（页面 500），而不是给个 404。
try:
    if not os.path.isdir(STATIC_DIR):
        os.makedirs(STATIC_DIR, exist_ok=True)
except Exception:
    pass
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
