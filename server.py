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

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CST = timezone(timedelta(hours=8))
CACHE_TTL = 300          # RSS 缓存 5 分钟
IMG_CACHE_TTL = 86400    # 图片缓存 1 天
MAX_AGE_HOURS = {"cn": 72, "intl": 240}
# 只保留新鲜条目：源一旦悄悄停更（返 200 但内容陈旧），不会再拿旧闻充数。
# 国内源更新密集，72 小时足够；国际源条目稀疏（France24 单源跨度可到两周），放宽到 10 天。

# ---------------- 数据源 ----------------
# 2026-10-06 全量实测后重构：人民网 / 新浪 / 网易 / 央视 / 环球网 / 澎湃 的 RSS
# 均已停更（人民网停在 2025-06-05，新浪停在 2025-09-23），仍返 200 但内容陈旧，
# 是最初「新闻太旧」的根因。现改用仍在实时更新的源。
SOURCES = {
    "cn": [
        {"id": "cn-scroll",  "label": "中新网", "url": "https://www.chinanews.com.cn/rss/scroll-news.xml", "base": "https://www.chinanews.com.cn", "region": "cn", "cls": "要闻"},
        {"id": "cn-china",   "label": "中新网", "url": "https://www.chinanews.com.cn/rss/china.xml",       "base": "https://www.chinanews.com.cn", "region": "cn", "cls": "国内"},
        {"id": "cn-world",   "label": "中新网", "url": "https://www.chinanews.com.cn/rss/world.xml",       "base": "https://www.chinanews.com.cn", "region": "cn", "cls": "国际"},
        {"id": "cn-society", "label": "中新网", "url": "https://www.chinanews.com.cn/rss/society.xml",     "base": "https://www.chinanews.com.cn", "region": "cn", "cls": "社会"},
        {"id": "cn-finance", "label": "界面",   "url": "https://a.jiemian.com/index.php?m=article&a=rss", "base": "https://www.jiemian.com",      "region": "cn", "cls": "财经"},
        {"id": "cn-tech",    "label": "IT之家", "url": "https://www.ithome.com/rss/",                     "base": "https://www.ithome.com",       "region": "cn", "cls": "科技"},
        {"id": "cn-sports",  "label": "中新网", "url": "https://www.chinanews.com.cn/rss/sports.xml",      "base": "https://www.chinanews.com.cn", "region": "cn", "cls": "体育"},
        {"id": "cn-culture", "label": "中新网", "url": "https://www.chinanews.com.cn/rss/culture.xml",     "base": "https://www.chinanews.com.cn", "region": "cn", "cls": "文化"},
    ],
    "intl": [
        {"id": "f24-main",    "label": "France24", "url": "https://www.france24.com/en/rss",           "base": "https://www.france24.com", "region": "intl", "cls": "要闻"},
        {"id": "f24-sport",   "label": "France24", "url": "https://www.france24.com/en/sport/rss",     "base": "https://www.france24.com", "region": "intl", "cls": "体育"},
        {"id": "f24-culture", "label": "France24", "url": "https://www.france24.com/en/culture/rss",   "base": "https://www.france24.com", "region": "intl", "cls": "文化"},
    ],
}

_CACHE = {}     # url -> (ts, payload)
_IMG_CACHE = {} # url -> (ts, bytes, ctype)
PRELOAD = {"cn": [], "intl": []}   # 启动时预热好的条目，API 直接取用


def _load_config():
    path = _res("config.json")
    default = {
        "update": {"enabled": True},
        "translate": {"backend": "mymemory", "baidu": {"appid": "", "secret": ""}, "deepl": {"auth_key": ""}, "enabled": True},
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


def _fetch(url, timeout=20):
    key = ("img", url) if url.lower().startswith(("http://", "https://")) and "rss" not in url else ("doc", url)
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
                out = r.text if r.status_code == 200 else None
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


def _clean(text):
    if not text:
        return ""
    s = re.sub(r"<br\s*/?>|</p>|</div>", " ", text)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_one(src):
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
        desc = _clean(e.get("summary") or e.get("description") or "")[:220]
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
    """并发抓取一个 Region 的全部源，去重后按时间倒序"""
    out = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(_parse_one, SOURCES[region]):
            out.extend(r)
    seen, items = set(), []
    for it in out:
        if it["title"] in seen:
            continue
        seen.add(it["title"])
        items.append(it)
    items.sort(key=lambda x: x["published"], reverse=True)
    return _drop_shared_images(items)


@app.on_event("startup")
def _warmup():
    """后台预热两个 Region + 静默检查更新"""
    def run():
        for region in ("cn", "intl"):
            try:
                items = _collect(region)
                PRELOAD[region] = items
                print(f"[ok] 预热 {region}: {len(items)} 条")
            except Exception as ex:
                print(f"[warn] 预热 {region} 失败: {type(ex).__name__}")

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


@app.get("/api/news")
def api_news(region: str = Query("cn", enum=["cn", "intl"]), q: str = Query(""), limit: int = Query(48, ge=1, le=200)):
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
    items.sort(key=lambda x: x["published"], reverse=True)
    if q.strip():
        kw = q.strip().lower()
        items = [i for i in items if kw in i["title"].lower() or kw in i["desc"].lower()]
    return {"version": VERSION, "count": len(items), "items": items[:limit]}


@app.get("/api/version")
def api_version():
    return {"version": VERSION, "updatedAt": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")}


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


@app.get("/api/translate")
def api_translate(text: str = Query(""), target: str = "zh-CN"):
    cfg = CONFIG.get("translate", {})
    if not cfg.get("enabled", True) or not text:
        return {"ok": False, "reason": "disabled"}
    backend = cfg.get("backend", "mymemory")
    try:
        if backend == "baidu":
            bd = cfg.get("baidu", {})
            if not bd.get("appid") or not bd.get("secret"):
                return {"ok": False, "reason": "baidu 未配置 appid/secret"}
            from hashlib import md5
            import time
            salt = str(int(time.time()))
            sign = md5((bd["appid"] + text + salt + bd["secret"]).encode("utf-8")).hexdigest()
            with httpx.Client(timeout=15) as c:
                r = c.get("https://fanyi-api.baidu.com/api/trans/vip/translate", params={
                    "q": text, "from": "en", "to": "zh", "appid": bd["appid"], "salt": salt, "sign": sign})
                js = r.json()
            if js.get("trans_result"):
                return {"ok": True, "backend": "baidu", "text": js["trans_result"][0]["dst"]}
            return {"ok": False, "reason": str(js.get("error_code", "baidu error"))}
        elif backend == "deepl":
            dk = cfg.get("deepl", {})
            if not dk.get("auth_key"):
                return {"ok": False, "reason": "deepl 未配置 auth_key"}
            with httpx.Client(timeout=15) as c:
                r = c.post("https://api-free.deepl.com/v2/translate", data={
                    "text": text, "target_lang": "ZH", "auth_key": dk["auth_key"]})
                js = r.json()
            if js.get("translations"):
                return {"ok": True, "backend": "deepl", "text": js["translations"][0]["text"]}
            return {"ok": False, "reason": "deepl error"}
        else:  # mymemory 免费免 key
            with httpx.Client(timeout=15) as c:
                r = c.get("https://api.mymemory.translated.net/get", params={"q": text[:500], "langpair": "en|zh-CN"})
                js = r.json()
            t = (js.get("responseData") or {}).get("translatedText", "")
            if t and "MYMEMORY WARNING" not in t.upper():
                return {"ok": True, "backend": "mymemory", "text": t}
            return {"ok": False, "reason": "mymemory 未返回有效结果"}
    except Exception as ex:
        return {"ok": False, "reason": type(ex).__name__}


@app.get("/img")
def proxy_img(url: str = Query(...)):
    if not url.startswith(("http://", "https://")):
        return JSONResponse({"error": "bad url"}, status_code=400)
    res = _fetch(url)
    if not res or not res[0]:
        return Response(status_code=404)
    return Response(content=res[0], media_type=res[1] or "image/jpeg",
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
