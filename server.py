# -*- coding: utf-8 -*-
"""
hotnews —— 热点新闻检索（国内 / 国际）
FastAPI 后端：抓取 RSS、解析题图、图片代理、翻译、SSR 首屏
"""
import sys
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
import ctypes
from datetime import datetime, timezone, timedelta
from math import gcd
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
MAX_AGE_HOURS = {"cn": 72, "intl": 240}
# 热榜更紧：超 48h 的旧闻不进榜（常看常新，老新闻不排序）
MAX_HOT_AGE_HOURS = 48
# 只保留新鲜条目：源一旦悄悄停更（返 200 但内容陈旧），不会再拿旧闻充数。
# 国内源更新密集，72 小时足够；国际源条目稀疏（France24 单源跨度可到两周），放宽到 10 天。

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
        # 凤凰资讯（要闻）：news.ifeng.com 首页内联 JSON，自带 975x549 缩略图 + newsTime。
        # 与热榜的凤凰同源，但归入「要闻」、带高清图；跨栏去重保证不与热榜重复。
        {"id": "cn-ifeng-news", "label": "凤凰网", "url": "https://news.ifeng.com/", "base": "https://news.ifeng.com", "region": "cn", "cls": "要闻", "parser": "ifeng_news", "max_age_hours": 72},
        # 红星新闻（要闻）：首页列表项无图（只有站点占位图），真实题图在文章页 <meta name=image>。
        # _parse_cdsb_news 并发抓文章页取首图（HD，实测 1080x720+），归入「要闻」。
        {"id": "cn-cdsb", "label": "红星新闻", "url": "https://www.cdsb.com/", "base": "https://www.cdsb.com", "region": "cn", "cls": "要闻", "parser": "cdsb_news", "max_age_hours": 72},
        {"id": "cn-tech",    "label": "IT之家", "url": "https://www.ithome.com/rss/",                     "base": "https://www.ithome.com",   "region": "cn", "cls": "科技"},
        {"id": "cn-geek",    "label": "极客公园", "url": "https://www.geekpark.net/rss",                  "base": "https://www.geekpark.net", "region": "cn", "cls": "科技"},
        # 软件类：小众软件 RSS，条条带图（feed 内 media:content 含图），内容偏软件推荐/效率工具
        {"id": "cn-soft",    "label": "小众软件", "url": "https://www.appinn.com/feed/",                  "base": "https://www.appinn.com",   "region": "cn", "cls": "软件"},
        # 科技/硬件评测：notebookcheck-cn.com 是 TYPO3 站点、无 RSS，走 HTML 解析（parser=nbc）
        {"id": "cn-nbc",     "label": "NotebookCheck", "url": "https://www.notebookcheck-cn.com/",        "base": "https://www.notebookcheck-cn.com", "region": "cn", "cls": "科技", "parser": "nbc"},
        # 软件类：iplaysoft（异次元软件世界）RSS 实测 30 条带图、更新活跃
        {"id": "cn-iplay",   "label": "iplaysoft", "url": "https://www.iplaysoft.com/feed/",              "base": "https://www.iplaysoft.com", "region": "cn", "cls": "软件"},
        # 软件类：mefcl（用户投稿站点），站方有 JS cookie 验证；抓首页（/feed/ 仍 403），
        # parser 内现取挑战 cookie 回放绕过
        {"id": "cn-mefcl",   "label": "mefcl",     "url": "https://www.mefcl.com/",                       "base": "https://www.mefcl.com",     "region": "cn", "cls": "软件", "parser": "mefcl"},
        # 软件类：国内中文源（用户要求删除不易读的英文 GitHub 仓库，改用国内）。
        # 开源中国 RSS 软件发布动态（50 条，中文，带 180~210 字摘要）；少数派 RSS 工具/效率软件实践（10 条，中文）。
        # 两者无题图 → 加入 NOIMG_SOURCES 豁免无图过滤，前端走渐变块样式。
        {"id": "cn-oschina", "label": "开源中国", "url": "https://www.oschina.net/news/rss", "base": "https://www.oschina.net", "region": "cn", "cls": "软件"},
        {"id": "cn-sspai",   "label": "少数派",   "url": "https://sspai.com/feed",           "base": "https://sspai.com",       "region": "cn", "cls": "软件"},
        # 娱乐栏目：凤凰娱乐首页（明星/绯闻/八卦）。首页内联 JSON 带标题+题图+newsTime，量大。
        {"id": "cn-ent", "label": "凤凰娱乐", "url": "https://ent.ifeng.com/", "base": "https://ent.ifeng.com", "region": "cn", "cls": "娱乐", "parser": "ifeng", "max_age_hours": 720},
    ],
    "intl": [
        {"id": "f24-main",    "label": "France24", "url": "https://www.france24.com/en/rss",         "base": "https://www.france24.com", "region": "intl", "cls": "要闻"},
        {"id": "f24-sport",   "label": "France24", "url": "https://www.france24.com/en/sport/rss",   "base": "https://www.france24.com", "region": "intl", "cls": "体育"},
        # 娱乐栏目（国际）：f24-culture（文化）已按用户要求删除，改用国际娱乐八卦源。
        # Variety 实测 10 条全带图；Billboard 10 条无图（音乐明星向），国际不做无图过滤，占位显示。
        {"id": "intl-variety",   "label": "Variety",   "url": "https://variety.com/feed/",    "base": "https://variety.com",   "region": "intl", "cls": "娱乐"},
        {"id": "intl-billboard", "label": "Billboard", "url": "https://www.billboard.com/feed/", "base": "https://www.billboard.com", "region": "intl", "cls": "娱乐"},
    ],
    # 实时热点（今日头条热榜）走独立 _parse_hot，不走 RSS，这里仅占位以通过 region 校验
    "hot": [],
}

# 无题图的集合类源（国内软件类 RSS 无配图）：_collect 豁免无图过滤，前端走渐变块样式
NOIMG_SOURCES = {"cn-oschina", "cn-sspai"}

_CACHE = {}     # url -> (ts, payload)
_IMG_CACHE = {} # url -> (ts, bytes, ctype)
PRELOAD = {"cn": [], "intl": [], "hot": []}   # 启动时预热好的条目，API 直接取用
_IMG_MEASURE = {}   # url -> (ts, (w,h)|None) 尺寸探测结果缓存：同一张图不重复下载
_IMG_BAD = set()    # 短期判死的图床（超时/非图内容/连不上），别让每张卡都去撞一次墙

# ---------- 题图「高清」门槛（低于此值的一律不要）----------
# 用户要求：小于 720x480 的图不要。但国内源实测普遍给不到这个尺寸
# （NotebookCheck 最大就 672x504、iplaysoft 680x425、Appinn 有 804x350），
# 硬卡会把整个板块清空，所以给一个「容差系数」把门槛按比例放宽。
MIN_IMG_W = 720
MIN_IMG_H = 480
IMG_TOL = 0.90       # 容差 0.90 → 实际门槛 648 x 432（672x504 这类主流图刚好卡在线上）
IMG_MIN_BYTES = 6144  # 小于 6KB 基本是占位图 / 纯色块 / 破图
# 明确的「非配图」特征：头像、占位图、像素点、站点 UI 图标
_IMG_JUNK_RE = re.compile(
    r"(avatar|gravatar|placeholder|loading|spacer|default[_\-]?(img|image|pic)|"
    r"no[_\-]?image|noimage|blank\.|1x1|pixel\.|icon[_\-]?\d+|/icons?/|"
    r"emoji|EmojiPl|Sprites|social[_\-]?(share|icon)|qrcode|qr[_\-]?code)",
    re.I)


def _load_config():
    path = _res("config.json")
    default = {
        "update": {"enabled": True},
        # contact：MyMemory 用邮箱标识身份，带上可把免费额度从 5k/天 提到 50k/天。
        # 实测不带 contact 会很快 429（额满），导致国际/体育条目译不出→保留英文。
        "translate": {"backend": "mymemory", "contact": "kele551@example.com",
                      "baidu": {"appid": "", "secret": ""}, "deepl": {"auth_key": ""}, "enabled": True},
    }
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("update", default["update"])
        data["update"].setdefault("enabled", True)
        data.setdefault("translate", default["translate"])
        for k, v in default["translate"].items():
            data["translate"].setdefault(k, v)
        return data
    except Exception:
        return default


CONFIG = _load_config()


def _fetch(url, timeout=10):
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
    now = datetime.now().timestamp()
    cache = _IMG_CACHE if key[0] == "img" else _CACHE
    hit = cache.get(url)
    if hit and (now - hit[0]) < (IMG_CACHE_TTL if key[0] == "img" else CACHE_TTL):
        return hit[1]
    try:
        # trust_env=False：不走系统/沙箱代理，本机直连国内站点更快更稳
        hdrs = {"User-Agent": UA}
        if key[0] == "img":
            # 部分图片 CDN 靠 Referer 防盗链，带上来源站点域名
            try:
                hdrs["Referer"] = urllib.parse.urlsplit(url).netloc
            except Exception:
                pass
        with httpx.Client(follow_redirects=True, timeout=timeout, trust_env=False,
                          headers=hdrs) as c:
            if key[0] == "img":
                r = c.get(url)
                if r.status_code == 200:
                    out = (r.content, r.headers.get("content-type", "image/jpeg"))
                else:
                    out = (None, "")
            else:
                r = c.get(url)
                # 必须返回原始字节 r.content，而不是 r.text：
                # httpx 的 r.text 会用它猜的编码（常误判 GBK 源为 utf-8）解码成字符串，
                # 一旦源字节不是合法 utf-8（如中关村在线 RSS 是 GBK），标题就被换成 U+FFFD 乱码。
                # 交给 feedparser.parse(bytes) 按各源 XML 声明的编码自行解码，才能正确还原中文。
                out = r.content if r.status_code == 200 else None
    except Exception:
        out = None
    cache[url] = (now, out)
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
MIN_IMG_SHORT_SIDE = 200  # 题图短边低于此值视为低质图（装饰图/破图），剔除（2026-10-06 新增）

IMG_MIN_BYTES = 8 * 1024  # 小于 8KB 的基本是占位图 / 破图 / 纯色块
IMG_SIZE_TTL = 86400     # 尺寸探测结果缓存 1 天
_IMG_SIZE = {}           # url -> (ts, (w,h) | None)
_IMG_JUNK = set()        # 判定不可用的图（占位图/头像/非图内容），短期不再重试

# URL 特征命中即判「废图」：头像、占位图、1x1 像素点、base64
_IMG_JUNK_PAT = re.compile(
    r"(avatar|gravatar|placeholder|default[_\-]?img|no[_\-]?image|blank\.(gif|png)"
    r"|1x1|pixel\.(gif|png)|spacer|loading\.(gif|svg)|icon[_\-]?\d+|logo\d*\.(png|jpg))",
    re.I)
# 长度/宽度都极小（<=160）→ 头像级废图
IMG_JUNK_SIDE = 160


def _img_min_wh():
    """容差后的实际门槛"""
    return (int(MIN_IMG_W * IMG_TOL), int(MIN_IMG_H * IMG_TOL))


def _img_url_junk(url):
    """纯 URL 特征判断废图（不联网，抓取阶段先用它挡掉头像/占位图）"""
    if not url:
        return True
    return bool(_IMG_JUNK_PAT.search(url))


def _upgrade_img_url(u):
    """
    把源给的缩略图 URL 升级成更大的原图。
    只对明确可推导的规则动手，改不动就原样返回（宁可用小图，不要改错成 404）。
    """
    if not u:
        return u
    # WordPress / 多数图床的 "-1024x576" 尺寸后缀 → 去掉拿原图
    s = re.sub(r"-\d{2,4}x\d{2,4}(?=\.[a-zA-Z]{3,4}(?:$|\?))", "", u)
    # 阿里云 OSS / 百度 BCE 的裁剪参数 → 去掉拿原图
    if "x-oss-process=" in s or "x-bce-process=" in s:
        s = re.sub(r"\?x-(oss|bce)-process=[^&]*$", "", s)
    # /sina/xxx/thumb/ → 新浪微博图床 thumb 版本更小的情况这里不动，避免改错
    return s


def _img_is_upscaled(w, h, min_base=180):
    """
    【已弃用，保留仅供排查】原意是识别「小图被等比拉伸」的假高清。

    判据失效的原因：把宽高同除最大公约数后得到的 (bw, bh) 恒为互质小数对，
    任何 4:3 图（如 672x504 → gcd 168 → (4,3)）都会被判成「拉伸图」。
    2026-10-06 实测把 NotebookCheck 整站误杀，已在 _img_ok 里摘掉调用。
    """
    if w <= 0 or h <= 0:
        return False
    g = gcd(w, h)
    # 公约数很小（比如 1x1 底色、2x2）不算；要找的是"基座本身很小"的情况
    if g <= 1:
        return False
    bw, bh = w // g, h // g
    return bw <= min_base and bh <= min_base
_IMG_SUB = ("//", "//s", "//t", "//d")   # 无协议头的 CDN 路径前缀，_fetch 只认 http(s) 开头


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


def _declared_size(m):
    """从 media:content / media:thumbnail 的 width/height 属性读声明尺寸"""
    try:
        w = int(str(m.get("width") or "0").strip())
        h = int(str(m.get("height") or "0").strip())
    except Exception:
        return (0, 0)
    return (w, h)


def _img_width_hint(u):
    """URL 里自带像素尺寸线索的（如 -q82-w672-h 里的 w672），提取出来做排序参考"""
    m = re.search(r"[_-]w(\d{2,4})(?:[_-]|$)", u or "")
    if m:
        return int(m.group(1))
    m = re.search(r"w_(\d{2,4})", u or "")
    if m:
        return int(m.group(1))
    return 0


def _img_candidates(entry, base):
    """
    列出条目里所有候选题图。返回按「声明尺寸从大到小」排序的 URL 列表，
    这样 _parse_one 取第一个就能拿到源提供的最高清那张，而不是碰运气取到 480x270 的缩略图。
    """
    picks = []   # [(排序键, url)]

    def add(u, key=0):
        u = _norm_url(u, base)
        if not u:
            return
        if any(u == p[1] for p in picks):
            return
        if not key:
            key = _img_width_hint(u)
        picks.append((key, u))

    # media:content / media:thumbnail 可能给多档尺寸，全部收集，按声明宽度排序取最大
    for m in entry.get("media_content") or []:
        if isinstance(m, dict) and m.get("url"):
            add(m.get("url"), _declared_size(m)[0] or _img_width_hint(m.get("url")))
    t = entry.get("media_thumbnail")
    if isinstance(t, list):
        for m in t:
            if isinstance(m, dict) and m.get("url"):
                add(m.get("url"), _declared_size(m)[0] or _img_width_hint(m.get("url")))
    for e in entry.get("enclosures") or []:
        if isinstance(e, dict) and str(e.get("type", "")).startswith("image"):
            add(e.get("url"))
    im = entry.get("images")
    if isinstance(im, list):
        for m in im:
            if isinstance(m, dict) and m.get("url"):
                add(m.get("url"))
    # summary / description 正文里的 img（通常比缩略图更清晰）
    raw = entry.get("summary") or entry.get("description") or ""
    # 部分源（如小众软件）把题图放在 <content> 全文而非 summary，需一并扫描
    for c in (entry.get("content") or []):
        if isinstance(c, dict):
            raw = raw + "\n" + (c.get("value") or "")
    # jquery-lazy 之类把真图藏在 data-original，src 是占位图（用户反馈的实际坑）
    for u in re.findall(r'<img[^>]+data-(?:original|src)=["\']([^"\']+)["\']', raw, re.I):
        add(u)
    for u in re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', raw, re.I):
        add(u)
    for u in re.findall(r'(?:src|href)=["\']([^"\']+\.(?:jpg|jpeg|png|webp|gif)(?:\?[^"\']*)?)["\']', raw, re.I):
        add(u)

    picks.sort(key=lambda p: (-p[0], p[1]))
    cands = [u for _, u in picks]

    # 拿来当配图的 URL 再做一层 CDN 升级（只做明确可推导的规则，改不动就原样）
    out = []
    for u in cands:
        # protocol-relative //cdn/xxx.jpg 直接补全
        if u.startswith("//"):
            u = "https:" + u
        out.append(u)
    return out


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


def _probe_total(r):
    """从 Content-Range / Content-Length 推断图片总字节数；推断不出返回 0"""
    try:
        cr = r.headers.get("content-range") or ""
        if "/" in cr:
            return int(cr.rsplit("/", 1)[1])
    except Exception:
        pass
    try:
        cl = r.headers.get("content-length")
        if cl and cl.isdigit():
            return int(cl)
    except Exception:
        pass
    return 0


def _probe_image(url):
    """
    探测图片真实像素与字节数。

    只用「头部若干 KB」就够了：宽高写在文件头里（PNG 前 24 字节、JPEG 的 SOF
    标记通常在几十 KB 内），没必要把整图拉下来。JPEG 有些源（Telegraph、Appinn）
    会在前面塞十几 KB 的 EXIF/色彩 ICC 分段，所以上限给到 96KB。
    返回 ((w,h) | None, 总字节数)
    """
    now = time.time()
    hit = _IMG_SIZE.get(url)
    if hit and now - hit[0] < IMG_SIZE_TTL:
        return hit[1], hit[2]
    if url in _IMG_JUNK:
        return None, 0
    dims, total = None, 0
    hdrs = {"User-Agent": UA, "Range": "bytes=0-98303"}
    try:
        hdrs["Referer"] = urllib.parse.urlsplit(url).netloc
    except Exception:
        pass
    try:
        with httpx.Client(follow_redirects=True, timeout=8, trust_env=False, headers=hdrs) as c:
            with c.stream("GET", url) as r:
                if r.status_code in (200, 206):
                    total = _probe_total(r)
                    buf = b""
                    for chunk in r.iter_bytes(8192):
                        buf += chunk
                        d = _image_dimensions(buf)
                        if d:
                            dims = d
                            break
                        if len(buf) >= 98304:
                            break
                else:
                    _IMG_JUNK.add(url)
    except Exception:
        pass
    # 流式拿不到（不支持 Range / 非图内容）→ 退回整图抓取，最后一道兜底
    if dims is None and url not in _IMG_JUNK:
        try:
            res = _fetch(url, timeout=10)
            blob = res[0] if isinstance(res, tuple) else res
            if blob:
                dims = _image_dimensions(blob)
                total = len(blob)
        except Exception:
            pass
    if dims is None:
        _IMG_JUNK.add(url)
    _IMG_SIZE[url] = (now, dims, total)
    return dims, total


def _measure_image(url):
    """兼容旧调用：只要尺寸"""
    return _probe_image(url)[0]


def _img_ok(url):
    """
    题图是否够格做高清卡片。判断顺序（任一不过即淘汰）：
      1. URL 特征就是头像/占位图/站点图标  → 淘汰
      2. 测不出尺寸（图挂了 / 返回的不是图）→ 淘汰（旧逻辑是"测不出就留着"，
         结果列表里全是裂图空白，用户反馈强烈，这里改成不配图）
      3. 长边 <= 224px                    → 头像级废图
      4. 整图 < 8KB                       → 占位图 / 纯色块
      5. 低于容差门槛（默认 648x432）      → 不够清晰

    【踩过的坑】曾加过一条「宽高都是某个小数基座的整数倍 → 判定被放大的假高清」，
    实测是错的：672x504 的最大公约数是 168，化简只剩 (4,3)，于是所有 4:3 的正常图都被误杀
    （NotebookCheck 整站 672x504 全部躺枪）。这种启发式没有可靠依据，已在 ITER5 摘掉。
    """
    if not url or _img_url_junk(url):
        return False
    dims, nb = _probe_image(url)
    if not dims:
        return False
    w, h = dims
    if max(w, h) <= IMG_JUNK_SIDE:
        return False
    if nb and 0 < nb < IMG_MIN_BYTES:
        return False
    mw, mh = _img_min_wh()
    return w >= mw and h >= mh


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
    # mefcl 站方有 JS cookie 验证，需带 Cookie 绕过
    if src.get("parser") == "ifeng":
        return _parse_ifeng(src)
    if src.get("parser") == "ifeng_news":
        return _parse_ifeng_news(src)
    if src.get("parser") == "cdsb_news":
        return _parse_cdsb_news(src)
    if src.get("parser") == "thepaper":
        return _parse_thepaper(src)
    if src.get("parser") == "mefcl":
        return _parse_mefcl(src)
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
    cutoff = datetime.now(CST).timestamp() - MAX_AGE_HOURS.get(src.get("region", "cn"), 72) * 3600
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
    try:
        with httpx.Client(timeout=10, trust_env=False, headers={"User-Agent": UA}) as c:
            r = c.get("https://hacker-news.firebaseio.com/v0/topstories.json")
            ids = r.json() if r.status_code == 200 else []
    except Exception as ex:
        print(f"[warn] HN 榜单抓取异常: {type(ex).__name__}")
        return []
    ids = [i for i in (ids or []) if isinstance(i, int)][:30]

    def one(i):
        for attempt in range(2):
            try:
                with httpx.Client(timeout=10, trust_env=False, headers={"User-Agent": UA}) as c:
                    r = c.get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json")
                return r.json() if r.status_code == 200 else None
            except Exception:
                time.sleep(0.4)
        return None

    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
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
    与国内热榜保持同一套逻辑：真实更新时间倒序为主、热度（HN 分）为辅，
    超 MAX_HOT_AGE_HOURS 的旧闻不进榜（常看常新）。"""
    items = _hot_cn_intl()
    now = time.time()
    if _HN_CACHE["items"] and now - _HN_CACHE["ts"] < 180:
        items = items + _HN_CACHE["items"]
    else:
        hn = _hot_hn()
        _HN_CACHE["ts"], _HN_CACHE["items"] = now, hn
        items = items + hn
    cutoff = datetime.now(CST).timestamp() - MAX_HOT_AGE_HOURS * 3600
    fresh = [it for it in items if (it.get("published") or 0) >= cutoff]
    fresh.sort(key=lambda x: ((x.get("published") or 0), (x.get("heat") or 0)), reverse=True)
    for i, it in enumerate(fresh, 1):
        it["rank"] = i
    return fresh


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


def _hot_thepaper():
    """澎湃热榜：首页侧栏 hotNews 数组。

    选它的理由是数据最「实」：interactionNum 是站方自己在页面上显示的真实互动数
    （不是今日头条那种点进去对不上的站内加权 HotValue），且能用 contId 拼出
    newsDetail_forward_<contId> 直达正文，列表看到的和点进去看到的是同一篇。"""
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
        # 真实更新时间：trackPublishTime / pubTimeLong 是毫秒时间戳，publishTime 是字符串；
        # 都拿不到时按名次给一个近的时间，避免 published=0 被前端时间排序甩到最末。
        raw_ts = it.get("trackPublishTime") or it.get("pubTimeLong") or 0
        pub = 0
        if raw_ts:
            try:
                pub = int(raw_ts) / 1000
            except Exception:
                pub = 0
        if not pub:
            ps = it.get("publishTime")
            if ps:
                try:
                    pub = int(datetime.strptime(ps, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).timestamp())
                except Exception:
                    pub = 0
        if not pub:
            pub = int(time.time()) - idx * 120
        out.append({
            "id": hashlib.md5(("hot-thepaper-" + str(cid)).encode("utf-8")).hexdigest()[:12],
            "region": "hot", "channel": "thepaper-hot", "label": "澎湃新闻",
            "cls": "热榜", "title": title, "desc": "",
            "link": f"https://www.thepaper.cn/newsDetail_forward_{cid}", "image": "",
            "published": int(pub), "heat": heat, "rank": idx, "translated": False,
        })
    print(f"[ok] hot/澎湃新闻: {len(out)} 条")
    return out


def _cdsb_rel_time(tail):
    """红星首页锚文本尾巴里的时间：「红星新闻 10-06 20:58」「红星新闻 45分钟前」
    「红星新闻 今天 12:30」「红星新闻 2天前」等。解析成 UTC+8 秒级时间戳；解析不出返回 0。"""
    if not tail:
        return 0
    now = datetime.now(CST).timestamp()
    m = re.search(r"(\d+)\s*分钟前", tail)
    if m:
        return int(now - int(m.group(1)) * 60)
    m = re.search(r"(\d+)\s*小时前", tail)
    if m:
        return int(now - int(m.group(1)) * 3600)
    m = re.search(r"(\d+)\s*天前", tail)
    if m:
        return int(now - int(m.group(1)) * 86400)
    if "今天" in tail or "刚刚" in tail:
        mt = re.search(r"(\d{1,2}):(\d{2})", tail)
        if mt:
            d = datetime.now(CST).replace(hour=int(mt.group(1)), minute=int(mt.group(2)), second=0, microsecond=0)
            return int(d.timestamp())
        return int(now)
    m = re.search(r"(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})", tail)
    if m:
        try:
            d = datetime.now(CST).replace(month=int(m.group(1)), day=int(m.group(2)),
                                          hour=int(m.group(3)), minute=int(m.group(4)), second=0, microsecond=0)
            return int(d.timestamp())
        except Exception:
            return 0
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
        # 先截下尾巴用来解析真实更新时间，再把「来源 + 时间」切掉（保留足够标题长度，避免误伤）
        cut = title.rfind(" 红星新闻 ")
        rest = title[cut:] if cut > 0 else ""
        if cut > 8:
            title = title[:cut].strip()
        # 锚文本里混着纯图标 / 空 div，短于 6 字的不是标题
        if not title or len(title) < 6 or title in seen:
            continue
        seen.add(title)
        pub = _cdsb_rel_time(rest)
        if not pub:
            pub = int(time.time()) - len(out) * 120   # 兜底：拿不到时间就按位置给近的时间，避免沉底
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

    与国内 / 国际各版块保持同一套逻辑：每条带真实更新时间，按「更新时间倒序」为主、
    站方热度（互动数）为辅排序；超过 MAX_HOT_AGE_HOURS 的旧闻直接丢弃
    （常看常新，老新闻不进榜、不抢占前面位置）。三家按真实时间自然轮替，
    不再出现某一家因名次高而整体霸榜。任一平台抓取失败不影响其它两家。"""
    parts = []
    for fn in (_hot_thepaper, _hot_cdsb, _hot_ifeng):
        try:
            parts.extend(fn())
        except Exception as ex:
            print(f"[warn] 热榜子源异常: {type(ex).__name__}: {ex}")
    if not parts:
        return parts
    # 时效过滤：丢弃超过 MAX_HOT_AGE_HOURS 的旧闻（如红星一两天前的旧稿）
    cutoff = datetime.now(CST).timestamp() - MAX_HOT_AGE_HOURS * 3600
    fresh = [it for it in parts if (it.get("published") or 0) >= cutoff]
    dropped = len(parts) - len(fresh)
    if dropped:
        print(f"[ok] hot: 时效过滤丢弃 {dropped} 条旧闻（>{MAX_HOT_AGE_HOURS}h）")
    # 排序：更新时间倒序为主，站方热度（互动数）为辅 —— 与要闻 / 科技等版块同形
    fresh.sort(key=lambda x: ((x.get("published") or 0), (x.get("heat") or 0)), reverse=True)
    for i, it in enumerate(fresh, 1):
        it["rank"] = i
        it["score"] = round((len(fresh) - i + 1) / len(fresh), 4)
    print(f"[ok] hot: 聚合后 {len(fresh)} 条（澎湃/红星/凤凰按时间+热度混排）")
    return fresh


# ---------------------------------------------------------------------------
# Windows 右下角通知（热榜头条变化时）
# 全链路 try/except 包裹：依赖缺失或任何异常都静默跳过，绝不拖累刷新与主服务。
# ---------------------------------------------------------------------------
_LAST_TOAST_TS = [0.0]          # 上次弹通知的时间戳（节流用）
_LAST_TOP_ID = [None]           # 上一次记录的热榜头条 id（用于判定是否变化）
_TOAST_MIN_GAP = 600            # 两次通知最小间隔（秒），避免刷屏


def _show_toast(title, msg, url):
    """Windows 右下角气泡通知（纯 ctypes，无第三方依赖）。点击气泡打开 url；失败静默。"""
    try:
        import ctypes
        from ctypes import WINFUNCTYPE, Structure, byref, sizeof, c_int, c_void_p
        from ctypes import c_uint, c_ulong, c_wchar, c_ubyte
        from ctypes.wintypes import HWND, UINT, WPARAM, LPARAM, HINSTANCE, HICON, MSG

        WM_COMMAND = 0x0111
        NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
        NIF_ICON, NIF_INFO, NIF_TIP, NIF_MESSAGE = 2, 0x10, 4, 1
        NIIF_INFO = 0x1
        IDI_APPLICATION = 32512
        NIN_BALLOONUSERCLICK = 0x0405

        class GUID(Structure):
            _fields_ = [("Data1", c_ulong), ("Data2", c_ushort),
                        ("Data3", c_ushort), ("Data4", c_ubyte * 8)]

        class NOTIFYICONDATA(Structure):
            _fields_ = [
                ("cbSize", c_ulong), ("hWnd", HWND), ("uID", c_uint),
                ("uFlags", c_uint), ("uCallbackMessage", c_uint),
                ("hIcon", HICON), ("szTip", c_wchar * 128),
                ("dwState", c_ulong), ("dwStateMask", c_ulong),
                ("szInfo", c_wchar * 256), ("uTimeout", c_ulong),
                ("szInfoTitle", c_wchar * 64), ("dwInfoFlags", c_ulong),
                ("guidItem", GUID), ("hBalloonIcon", HICON),
            ]

        WNDPROC = WINFUNCTYPE(c_int, HWND, UINT, WPARAM, LPARAM)

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_COMMAND and ((lparam >> 16) & 0xFFFF) == NIN_BALLOONUSERCLICK:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            return ctypes.windll.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        class WNDCLASS(Structure):
            _fields_ = [
                ("style", c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", c_int), ("cbWndExtra", c_int),
                ("hInstance", HINSTANCE), ("hIcon", HICON),
                ("hCursor", c_void_p), ("hbrBackground", c_void_p),
                ("lpszMenuName", c_wchar), ("lpszClassName", c_wchar),
            ]

        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        wcls = WNDCLASS()
        wcls.lpszClassName = "HotnewsTaskbar"
        wcls.lpfnWndProc = WNDPROC(wnd_proc)
        wcls.hInstance = hinst
        ctypes.windll.user32.RegisterClassW(byref(wcls))
        HWND_MESSAGE = c_void_p(-3)
        hwnd = ctypes.windll.user32.CreateWindowExW(
            0, "HotnewsTaskbar", "hotnews", 0, 0, 0, 0, 0,
            HWND_MESSAGE, 0, hinst, None)
        nid = NOTIFYICONDATA()
        nid.cbSize = sizeof(NOTIFYICONDATA)
        nid.hWnd = hwnd
        nid.uID = 0
        nid.uFlags = NIF_ICON | NIF_INFO | NIF_TIP | NIF_MESSAGE
        nid.uCallbackMessage = WM_COMMAND
        nid.hIcon = ctypes.windll.user32.LoadIconW(0, IDI_APPLICATION)
        nid.szTip = "热点新闻"
        nid.szInfo = (msg or "")[:255]
        nid.uTimeout = 2000
        nid.szInfoTitle = (title or "")[:63]
        nid.dwInfoFlags = NIIF_INFO
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, byref(nid))
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, byref(nid))
        m = MSG()
        start = time.time()
        while time.time() - start < 8:
            if ctypes.windll.user32.GetMessageW(byref(m), hwnd, 0, 0) == 0:
                break
            ctypes.windll.user32.TranslateMessage(byref(m))
            ctypes.windll.user32.DispatchMessageW(byref(m))
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_DELETE, byref(nid))
        ctypes.windll.user32.DestroyWindow(hwnd)
    except Exception:
        try:
            ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40)
        except Exception:
            pass


def _notify_news(items):
    """热榜头条变了就弹右下角通知。首跑只记录基线不弹；10 分钟内至多一次。"""
    try:
        if not items:
            return
        top_id = items[0].get("id")
        now = time.time()
        if _LAST_TOP_ID[0] is None:
            _LAST_TOP_ID[0] = top_id
            return                                  # 首次：仅建基线，不弹
        if top_id == _LAST_TOP_ID[0]:
            return                                  # 头条没变，不弹
        _LAST_TOP_ID[0] = top_id
        if now - _LAST_TOAST_TS[0] < _TOAST_MIN_GAP:
            return                                  # 节流：间隔太短，这次不弹
        _LAST_TOAST_TS[0] = now
        head = (items[0].get("title") or "")[:42]
        threading.Thread(
            target=_show_toast,
            args=("热点新闻更新", f"头条：{head}", APP_URL),
            daemon=True,
        ).start()
    except Exception as ex:
        print(f"[warn] 通知失败（已忽略）: {type(ex).__name__}")


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
    cookie = _MEFCL_COOKIE.get("value")
    if not cookie or (now - _MEFCL_COOKIE.get("ts", 0)) > 1500:
        # 第一步：拿挑战页，抠合法 cookie
        try:
            with httpx.Client(follow_redirects=True, timeout=10, trust_env=False,
                              headers={"User-Agent": UA}) as c:
                r0 = c.get(src["url"])
            raw0 = r0.content if r0.status_code == 200 else b""
        except Exception as ex:
            print(f"[warn] mefcl 挑战页抓取异常: {type(ex).__name__}（回退上次结果）")
            return _MEFCL_LAST
        html0 = raw0.decode("utf-8", "ignore")
        m = re.search(r'ge_js_validator_63=([^";\s]+)', html0)
        if not m:
            print("[warn] mefcl 挑战页无 ge_js_validator cookie 字段，可能被改版（回退上次结果）")
            return _MEFCL_LAST
        cookie = m.group(1)
        _MEFCL_COOKIE["value"] = cookie
        _MEFCL_COOKIE["ts"] = now
    # 第二步：带合法 cookie 回放首页
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
        # 时间：<time datetime="...">
        tsm = re.search(r'datetime="(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"', blk)
        ts = 0
        if tsm:
            try:
                ts = int(datetime.strptime(tsm.group(1).replace("T", " "),
                                            "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST).timestamp())
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
    max_age = src.get("max_age_hours") or MAX_AGE_HOURS.get("cn", 168)
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


# ---------------- 要闻源：凤凰资讯 / 红星新闻（带高清图，与热榜分栏） ----------------
_CDSB_IMG_CACHE = {}   # 红星文章页 URL -> (ts, 首图URL)；首图稳定，缓存避免每次刷新都抓文章页


def _cdsb_article_image(article_url):
    """抓红星文章页取首图：<meta name="image" content> 优先（首图），回落正文 <img data-original>。
    带 10 分钟缓存；失败返回空（调用方按无图处理）。实测首图 1080x720+，过 HD 门槛。"""
    now = time.time()
    c = _CDSB_IMG_CACHE.get(article_url)
    if c and now - c[0] < 600:
        return c[1]
    img = ""
    try:
        raw = _fetch(article_url, timeout=8)
        if isinstance(raw, tuple):
            raw = raw[0]
        if raw:
            ah = raw.decode("utf-8", "ignore")
            m = re.search(r'<meta[^>]+name="image"[^>]+content="([^"]+)"', ah) \
                or re.search(r'<meta[^>]+content="([^"]+)"[^>]+name="image"', ah)
            if m:
                img = _norm_url(m.group(1), "https://www.cdsb.com/")
            if not img:
                im = re.search(r'<img\b[^>]*\bdata-original="([^"]+\.(?:jpg|jpeg|png|webp))"', ah)
                if im:
                    img = _norm_url(im.group(1), "https://www.cdsb.com/")
    except Exception as ex:
        print(f"[warn] 红星文章页取图失败 {article_url[:60]}: {type(ex).__name__}")
    _CDSB_IMG_CACHE[article_url] = (now, img)
    return img


def _parse_ifeng_news(src):
    """凤凰资讯（要闻）：news.ifeng.com 首页内联 JSON，每条约 975x549 缩略图 + newsTime。
    与热榜的凤凰同源但归入「要闻」、带高清图；低质缩略图由后台 _enrich_quality_bg 异步剔除。"""
    raw = _fetch(src["url"])
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 凤凰资讯抓取失败")
        return []
    page = raw.decode("utf-8", "ignore")
    max_age = src.get("max_age_hours") or MAX_AGE_HOURS.get("cn", 72)
    cutoff = datetime.now(CST).timestamp() - max_age * 3600
    out, seen = [], set()
    pat = r'"id":"(\d+)","title":"([^"]*)","url":"(https?://[^"]*ifeng\.com/c/[^"]+)"'
    for m in re.finditer(pat, page):
        url = m.group(3)
        title = _clean(m.group(2))
        if not title or url in seen:
            continue
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
            "id": hashlib.md5((src["id"] + m.group(1)).encode("utf-8")).hexdigest()[:12],
            "region": src["region"], "channel": src["id"],
            "label": src.get("label", src["id"]), "cls": src["cls"],
            "title": title, "desc": "", "link": url,
            "image": _norm_url(ims[0], "https://news.ifeng.com/") if ims else "",
            "published": ts or int(datetime.now(CST).timestamp()),
            "translated": False,
        })
        if len(out) >= 15:
            break
    print(f"[ok] {src['id']}: 凤凰要闻解析 {len(out)} 条（带图 {sum(1 for i in out if i['image'])}）")
    return out


def _parse_cdsb_news(src):
    """红星新闻（要闻）：首页 SSR 取文章链接+标题，并发抓文章页取 <meta name=image> 首图（HD）。
    首页列表项本身无图（站方占位图），真实题图只在文章页，故需逐篇取；首图缓存 10 分钟。"""
    raw = _fetch(src["url"])
    if isinstance(raw, tuple):
        raw = raw[0]
    if not raw:
        print("[warn] 红星新闻抓取失败")
        return []
    page = raw.decode("utf-8", "ignore")
    arts, seen = [], set()
    pat = r'<a\s[^>]*href="((?:https?:)?//[^"]*static\.cdsb\.com/micropub/Articles/[^"]+)"[^>]*>(.*?)</a>'
    for m in re.finditer(pat, page, re.S):
        title = _clean(m.group(2))
        cut = title.rfind(" 红星新闻 ")
        if cut > 8:
            title = title[:cut].strip()
        if not title or len(title) < 6 or title in seen:
            continue
        seen.add(title)
        arts.append((title, _norm_url(m.group(1), src["base"])))
        if len(arts) >= 15:
            break
    if not arts:
        return []
    # 并发抓文章页取首图（限 8 线程，避免瞬间打爆站方）
    with ThreadPoolExecutor(max_workers=8) as ex:
        imgs = list(ex.map(_cdsb_article_image, [a[1] for a in arts]))
    now = time.time()
    out = []
    for (title, link), img in zip(arts, imgs):
        out.append({
            "id": hashlib.md5((src["id"] + title).encode("utf-8")).hexdigest()[:12],
            "region": src["region"], "channel": src["id"],
            "label": src.get("label", src["id"]), "cls": src["cls"],
            "title": title, "desc": "", "link": link,
            "image": img or "",
            "published": int(now) - len(out) * 60,   # 首页按时间倒序，用位置近似新鲜度
            "translated": False,
        })
    print(f"[ok] {src['id']}: 红星要闻解析 {len(out)} 条（带图 {sum(1 for i in out if i['image'])}）")
    return out


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


def _collect(region):
    """并发抓取一个 Region 的全部源，去重后按时间倒序。
    国内源在此剔除无图新闻（快，不下载图）；低质图由后台 _enrich_quality_bg 异步剔除，
    避免首屏因下载测尺寸而变慢。"""
    if region == "hot":
        return _parse_hot()
    out = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(_parse_one, SOURCES[region]):
            out.extend(r)
    seen, items = set(), []
    for it in out:
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        items.append(it)
    items = _drop_shared_images(items)
    if region == "cn":
        before = len(items)
        # 集合类源（GitHub awesome 软件合集）无题图，豁免无图过滤，走前端渐变块样式；
        # 其余无图新闻（如界面 RSS 实测 0 图）一律剔除 —— 用户要求「不显示图片的新闻别上」。
        cn_only = [it for it in items if it.get("image") or it.get("channel") in NOIMG_SOURCES]
        print(f"[ok] {region}: 剔除无图 {before - len(cn_only)} 条，剩 {len(cn_only)} 条（低质图后台异步剔除）")
        # 实时热榜（澎湃/红星/凤凰）不再并入国内板块：它是独立「热榜」标签页，
        # 并入会导致同一家新闻在「要闻」与顶部热榜里重复出现（用户要求两栏不重复）。
        cn_only.sort(key=lambda x: x["published"], reverse=True)
        return cn_only
    items.sort(key=lambda x: x["published"], reverse=True)

    # 国际实时热榜（Hacker News）置顶，其后国际 RSS 卡片按时间倒序
    try:
        hot = _parse_hot_intl()
        seen_t = {it["title"] for it in items}
        hot = [h for h in hot if h["title"] not in seen_t]
        if hot:
            print(f"[ok] {region}: 合并国际热榜 {len(hot)} 条")
    except Exception as ex:
        hot = []
        print(f"[warn] 合并国际热榜失败: {type(ex).__name__}")

    if region == "intl":
        # 国际源全英文 → 英译中显示；英文原文保留在 title_en/desc_en（悬停可见）。
        # 翻译结果磁盘缓存，命中缓存时不发网络请求；失败则保留英文，绝不因翻译丢内容。
        #
        # 【重要】这里**不做耗时翻译**。翻译接口慢且常被限流（实测一次 45 段要 90s+，
        # 而 RSS 本身只要 11s），放在抓取里会把「国际」板块整体拖死。改为：
        # 本函数只负责出条目（先给英文），由 _warmup 在后台调用 _translate_intl_bg 补译文，
        # item 是同一批对象引用，译文补上后 PRELOAD 里的内容自然就变成中文了。
        #
        # 与国内保持一致：卡片无题图的一律剔除（低质图由 _enrich_quality_bg 异步再裁），
        # 保证「国内 / 国际」图片显示规则相同。
        before = len(items)
        items = [it for it in items if it.get("image") or it.get("channel") in NOIMG_SOURCES]
        print(f"[ok] {region}: 剔除无图 {before - len(items)} 条，剩 {len(items)} 条（低质图后台异步剔除）")
    return hot + items


def _enrich_quality_bg(region):
    """后台异步：下载题图测真实尺寸，剔除低质图（短边过小）。测不出则保留，不因网络误杀。"""
    items = PRELOAD.get(region) or []
    if not items:
        return
    with ThreadPoolExecutor(max_workers=12) as ex:
        # mefcl 的题图是 220x150 缩略图（站方 excerpt 仅此尺寸），若按全局 200px
        # 短边门槛会被全砍 → 整个板块消失。mefcl 只有这种图，针对该源豁免尺寸过滤。
        keep = list(ex.map(
            lambda it: True if it.get("channel") == "cn-mefcl"
            else (_img_ok(it["image"]) if it.get("image") else True),
            items))
    pruned = [it for it, k in zip(items, keep) if k]
    dropped = len(items) - len(pruned)
    if dropped:
        print(f"[ok] {region}: 后台剔除低质图 {dropped} 条，剩 {len(pruned)} 条")
    PRELOAD[region] = pruned


@app.on_event("startup")
def _warmup():
    """后台预热两个 Region + 静默检查更新"""
    _load_trans_cache()          # 载入历史译文缓存，避免重复消耗免费翻译额度
    def run():
        # 国际源在国外 + 还要后台翻译，实测 RSS 就要 11s；以前把它排在 cn 后面串行执行，
        # 首屏要等它。现在国际单独开线程，cn（国内首屏）不受影响先就绪。
        def do_intl():
            try:
                items = _collect("intl")
                PRELOAD["intl"] = items
                print(f"[ok] 预热 intl: {len(items)} 条")
            except Exception as ex:
                print(f"[warn] 预热 intl 失败: {type(ex).__name__}")
                return
            try:
                # 翻译慢且常被限流：不阻塞抓取，后台补齐译文
                if not _TRANS_BUSY[0]:
                    threading.Thread(target=_translate_intl_bg, args=(items,), daemon=True).start()
            except Exception:
                pass

        threading.Thread(target=do_intl, daemon=True).start()
        for region in ("hot", "cn"):
            try:
                items = _collect(region)
                PRELOAD[region] = items
                print(f"[ok] 预热 {region}: {len(items)} 条")
                # 国内源：后台异步剔除低质图（下载测尺寸），不阻塞首屏
                if region == "cn" and items:
                    threading.Thread(target=_enrich_quality_bg, args=(region,), daemon=True).start()
            except Exception as ex:
                print(f"[warn] 预热 {region} 失败: {type(ex).__name__}")

    def refresh_loop():
        # 国内 / 热榜 / 国际 三个板块统一每 10 分钟重建一次，与前端自动刷新节奏一致，
        # 也保证「国内」「国际」更新逻辑完全相同。
        n = 0
        while True:
            time.sleep(600)
            n += 1
            try:
                PRELOAD["cn"] = _collect("cn")
            except Exception:
                pass
            try:
                PRELOAD["hot"] = _parse_hot()
                _notify_news(PRELOAD["hot"])     # 热榜头条变化 → 右下角通知
            except Exception:
                pass
            try:
                items = _collect("intl")
                PRELOAD["intl"] = items
                if not _TRANS_BUSY[0]:
                    threading.Thread(target=_translate_intl_bg, args=(items,), daemon=True).start()
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
                ok, msg = updater.download_and_prepare(st["latest"])
                print(f"[update] {msg}")
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


def _order_items(items):
    """
    统一排序：热榜（按热度排好的）置顶，其余严格按发布时间倒序，
    拿不到时间的条目沉到最后（它们没法参与时间排序，别混在中间打乱顺序）。
    """
    hot = [it for it in items if it.get("cls") == "热榜"]
    rest = [it for it in items if it.get("cls") != "热榜"]
    rest.sort(key=lambda x: (x.get("published") or 0), reverse=True)
    return hot + rest


def _purge_caches():
    """深度刷新：把所有层级的缓存一次性作废（含 RSS、图片字节、尺寸探测、共享榜缓存）"""
    n = len(_CACHE) + len(_IMG_CACHE) + len(_IMG_SIZE)
    _CACHE.clear()
    _IMG_CACHE.clear()
    _IMG_SIZE.clear()
    _IMG_JUNK.clear()
    _IMG_MEASURE.clear()
    _IMG_BAD.clear()
    _THEPAPER_CACHE["ts"] = 0.0
    _THEPAPER_CACHE["data"] = None
    _HN_CACHE["ts"] = 0
    _HN_CACHE["items"] = []
    for k in PRELOAD:
        PRELOAD[k] = []
    return n


@app.get("/api/news")
def api_news(region: str = Query("cn", enum=["cn", "intl", "hot"]), q: str = Query(""), limit: int = Query(48, ge=1, le=200)):
    if region not in SOURCES:
        return JSONResponse({"error": "unknown region"}, status_code=400)
    if PRELOAD.get(region):
        all_items = PRELOAD[region]
    else:
        all_items = _collect(region)
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
    items = _order_items(items)
    if q.strip():
        kw = q.strip().lower()
        items = [i for i in items if kw in i["title"].lower() or kw in i["desc"].lower()]
    # 显式声明 charset=utf-8，避免个别客户端把 JSON 误判为 GBK 导致乱码
    return JSONResponse({"version": VERSION, "count": len(items), "items": items[:limit]},
                        media_type="application/json; charset=utf-8")


_REFRESH_LOCK = threading.Lock()


def _filter_hd(items, region, workers=32):
    """按「高清门槛」剔选题图不达标的条目（默认门槛见 MIN_IMG_W/H 与 IMG_TOL）"""
    t0 = time.time()
    if not items:
        return items
    to_check = [it for it in items if it.get("image")]
    if not to_check:
        return items
    with ThreadPoolExecutor(max_workers=workers) as ex:
        keep = list(ex.map(lambda it: _img_ok(it["image"]), to_check))
    ok = {id(it) for it, k in zip(to_check, keep) if k}
    kept = [it for it in items if not it.get("image") or id(it) in ok]
    dropped = len(items) - len(kept)
    print(f"[ok] {region}: 高清筛选 {time.time()-t0:.1f}s，剔除 {dropped} 条，剩 {len(kept)} 条")
    return kept


@app.post("/api/refresh")
def api_refresh(region: str = Query("cn", enum=["cn", "intl", "hot"]), deep: int = Query(1)):
    """
    真正的「重新抓取」。

    /api/news 只要 PRELOAD 非空就直接返回预热数据，而 PRELOAD 由后台线程每 5 分钟
    才重建一次，重建时还会命中 300 秒 TTL 的 RSS 缓存 —— 这就是用户「连点十次刷新
    还是那批旧新闻」的原因。这个接口先把缓存全部作废（deep=1 连图片缓存一起），
    立刻同步抓一遍，再把新数据写回 PRELOAD，保证点一次就一定拿到源的最新内容。
    """
    if region not in SOURCES:
        return JSONResponse({"error": "unknown region"}, status_code=400)
    if not _REFRESH_LOCK.acquire(blocking=False):
        return JSONResponse({"ok": False, "busy": True, "message": "正在抓取中，请稍候"},
                            status_code=409)
    t0 = time.time()
    try:
        # deep=0（普通刷新）保留图片尺寸探测结果：一张给定 URL 的像素是不会变的，
        # 清掉它只会让每次刷新都重新下载上百张图去测尺寸（实测 30 秒 vs 3 秒）。
        saved_dims = None if deep else dict(_IMG_SIZE)
        saved_junk = None if deep else set(_IMG_JUNK)
        cleared = _purge_caches()
        if not deep:
            _IMG_SIZE.update(saved_dims or {})
            _IMG_JUNK.update(saved_junk or ())
        if deep:
            cleared += len(saved_dims or {})
        items = _collect(region)
        before = len(items)
        if region in ("cn", "intl"):
            items = _filter_hd(items, region)
        PRELOAD[region] = items
        return JSONResponse({
            "ok": True, "region": region, "version": VERSION,
            "fetched": before, "count": len(items),
            "cleared": cleared, "elapsed": round(time.time() - t0, 1),
            "items": items,
        }, media_type="application/json; charset=utf-8")
    except Exception as ex:
        return JSONResponse({"ok": False, "message": f"{type(ex).__name__}: {ex}"}, status_code=500)
    finally:
        _REFRESH_LOCK.release()


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
    if not updater.trigger_replace():
        return {"ok": False, "message": "没有待安装的更新"}
    def _die():
        time.sleep(0.6)
        os._exit(0)
    threading.Thread(target=_die, daemon=True).start()
    return {"ok": True, "message": "正在更新并重启"}


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
    jobs = []
    for it in todo:
        it["title_en"] = it.get("title") or ""
        if it.get("title"):
            jobs.append((it, "title", (it["title"] or "")[:300]))
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
