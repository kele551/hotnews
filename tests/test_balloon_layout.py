# -*- coding: utf-8 -*-
# 作者: 海风（kele551）
# 用途: 右下角气泡的「版式」「配色」「投放策略」单测（2026-10-28）。
#
# 盯的是这一轮用户报/要的几件事：
#   1)「打开和退出两个按键已经遮挡文字，调整布局」—— 老版式里正文画到「底边-10px」、
#      胶囊按钮从「底边-36px」起，两个矩形必然重叠。现在卡片分成互不相交的横带，
#      几何由纯函数 `_balloon_rects()` 算，这个文件把它当数学题断言（第 1 组）。
#   2)「这个样式应该和控制台的颜色同步」+「用了几天主题色，感觉没有啥变化」——
#      气泡自带一套硬编码颜色的老毛病。现在配色只认一份出处：`CONSOLE_TOKENS` 必须和
#      static/index.html 里的设计令牌**逐字对上**（第 2 组），档位走同一条判据（第 3 组）。
#   3)「喜欢科技的就多投这方面的内容」—— 点开过的栏目按 14 天半衰期记分，配额最多挪
#      2 席，头条/热榜/国际留底线（第 7 组，纯函数喂权重，不看本机文件）。
#   4)「程序自动精准投放，不需要看手机」—— 启动 20 秒内就播、全屏/演示/勿扰时自动提醒
#      让路（第 8 组，映射表是纯函数）。
#
# 为什么不 import server.py：那是八千多行、模块级要建 FastAPI 应用、读配置、起缓存目录。
# 这里用 ast 只把版式/配色那几个纯函数和常量摘出来单独 exec —— 不建窗口、不读注册表
# （_system_prefers_light 换成桩，换台机器结果也一样）、不碰网络、不看时钟，可复现。
import ast
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server.py")
PAGE = os.path.join(REPO, "static", "index.html")

WANT_FUNCS = ("_ref", "_rgb_of", "_hex_ref", "_hsl_ref", "_mix_ref",
              "_system_prefers_light", "_balloon_theme_name", "_balloon_palette",
              "_balloon_split_head", "_balloon_rects",
              "_ui_cfg", "_is_intl_item", "_balloon_head", "_balloon_entry",
              "_balloon_meta_line", "_balloon_time_line", "_balloon_img_local",
              "_interest_class", "_interest_quota", "_hold_reason_from_state",
              "_notify_hold_reason", "_hold_note", "_foreground_hint", "_quns_state")
WANT_CONSTS = ("BALLOON_W", "BALLOON_H", "BALLOON_PAD_X",
               "BALLOON_APP_TOP", "BALLOON_APP_H",
               "BALLOON_TEXT_TOP", "BALLOON_TEXT_LINES", "BALLOON_LINE_H", "BALLOON_TEXT_BOTTOM",
               "BALLOON_META_TOP", "BALLOON_META_H",
               "BALLOON_THUMB", "BALLOON_THUMB_GAP", "BALLOON_THUMB_RADIUS", "BALLOON_THUMB_TOP",
               "BALLOON_FOOT_TOP", "BALLOON_FOOT_BOTTOM",
               "BALLOON_BTN_H", "BALLOON_BTN_W",
               "BALLOON_BTN_OPEN_W", "BALLOON_BTN_CLOSE_W", "BALLOON_BTN_GAP",
               "BALLOON_DOT_W", "BALLOON_DOT_ON_W", "BALLOON_DOT_GAP",
               "BALLOON_RADIUS",
               "BALLOON_TITLE_PX", "BALLOON_HEAD_PX", "BALLOON_TITLE_W", "BALLOON_TITLE_MAX",
               "BALLOON_IMG_DIR",
               "BALLOON_PER_ITEM_MS", "BALLOON_PLAY_COUNT",
               "BALLOON_HEADLINE_TAG",
               "BALLOON_CLS_QUOTA", "INTEREST_CLASSES", "INTEREST_HALF_LIFE_DAYS",
               "INTEREST_MIN_EVIDENCE", "INTEREST_MAX_SHIFT", "_QUNS_HOLD", "_QUNS_LABEL",
               "CONSOLE_TOKENS", "CONSOLE_ACCENTS", "UI_DEFAULTS",
               # NOTIFY_DEFAULTS 的字面量里引用了 DIGEST_TIMES_DEFAULT，摘它就得先摘被引用的那个
               "DIGEST_TIMES_DEFAULT", "NOTIFY_DEFAULTS")

# 控制台令牌名 → CONSOLE_TOKENS 的键（CSS 用连字符，Python 用下划线）
COLOR_VARS = {"card": "card", "card-2": "card2", "line": "line", "line-2": "line2",
              "txt": "txt", "txt-2": "txt2", "txt-3": "txt3",
              "ok": "ok", "warn": "warn", "danger": "danger", "info": "info",
              "accent": "accent", "accent-ink": "accent_ink"}
NUM_VARS = {"src-s": "src_s", "src-l": "src_l", "src-b": "src_b", "src-bg": "src_a"}
# 这几支函数只许从令牌表里取色：源码里再出现 #xxxxxx，就是"气泡又自带一套色"的老毛病复发
#（#000000 除外 —— 那是取不到值时的兜底黑，页面里也没有拿它当设计色用过）
COLOR_GUARD = ("_balloon_palette", "_balloon_theme_name")

FAILS = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("[ok]" if ok else "[!!]", name, ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name + (" — " + detail if detail else ""))


def _target_names(node):
    out = []
    for t in node.targets:
        if isinstance(t, ast.Name):
            out.append(t.id)
        elif isinstance(t, ast.Tuple):
            out.extend(e.id for e in t.elts if isinstance(e, ast.Name))
    return out


def load_fragment():
    """把版式/配色/投放那几个函数与常量摘进一个干净命名空间；类只留源码文本。"""
    src = open(SERVER, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"re": re, "CONFIG": {}, "os": os, "hashlib": hashlib, "tempfile": tempfile,
          "json": json, "threading": threading, "time": time, "sys": sys}
    grabbed = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.decorator_list \
                and node.name in WANT_FUNCS:
            grabbed[node.name] = node
        elif isinstance(node, ast.Assign):
            for n in _target_names(node):
                if n in WANT_CONSTS:
                    grabbed[n] = node
        elif isinstance(node, ast.ClassDef) and node.name == "_Balloon":
            # 只取源码文本做静态闸门：这个类 exec 出来等于起了半个程序（要建窗口）
            grabbed["_Balloon"] = node
    done = set()
    for name in WANT_FUNCS + WANT_CONSTS:
        node = grabbed.get(name)
        if node is None:
            FAILS.append("摘取失败，缺: " + name)
            continue
        if id(node) in done:            # `A, B, C = 1, 2, 3` 这种一行挂三个键的
            continue
        done.add(id(node))
        src_txt = ast.get_source_segment(src, node) or ""
        if name in COLOR_GUARD:
            hard = [s for s in re.findall(r"#[0-9a-fA-F]{3,8}\b", src_txt)
                    if s.lower() != "#000000"]
            if hard:
                FAILS.append("%s 里写死了色值 %s（配色只许从 CONSOLE_TOKENS 出）" % (name, hard))
        try:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
        except Exception as ex:
            FAILS.append("摘取 %s 失败: %s: %s" % (name, type(ex).__name__, ex))
    # 注册表那颗判据换成桩：单测不去读本机 Windows 设置（不然换台机器结果就变）
    ns["_system_prefers_light"] = lambda: True
    return ns, src, grabbed


def segment(src, marker):
    """从 `marker` 那行起，取到下一个**同缩进**的行为止（拿来框出 WM_PAINT 那一段）。"""
    i = src.find(marker)
    if i < 0:
        return ""
    head = src[:i]
    indent = len(head) - head.rfind("\n") - 1
    lines = src[i:].split("\n")
    out = [lines[0]]
    for ln in lines[1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
            break
        out.append(ln)
    return "\n".join(out)


def func_seg(server_src, name):
    """取某个顶层函数的完整源码（到下一个顶格 def/class 为止）。"""
    i = server_src.find("\ndef " + name + "(")
    if i < 0:
        return ""
    j = server_src.find("\ndef ", i + 1)
    k = server_src.find("\nclass ", i + 1)
    ends = [x for x in (j, k) if x > 0]
    return server_src[i:min(ends) if ends else len(server_src)]


# ────────────── CSS 解析（第 2 组的同步闸门用） ──────────────
def css_blocks(src, needle):
    """返回所有以 `needle` 开头的规则体（这些块里只有声明，不套子规则，一层括号就够）。"""
    out = []
    pos = 0
    while True:
        i = src.find(needle, pos)
        if i < 0:
            return out
        j = src.find("{", i)
        if j < 0:
            return out
        d, k = 1, j + 1
        while k < len(src) and d:
            if src[k] == "{":
                d += 1
            elif src[k] == "}":
                d -= 1
            k += 1
        out.append(src[j + 1:k - 1])
        pos = j
    return out


def css_vars(block):
    return {k: v.strip() for k, v in re.findall(r"--([a-z0-9-]+)\s*:\s*([^;\n]+)", block)}


def norm_hex(v):
    v = (v or "").strip().lower()
    m = re.match(r"#([0-9a-f]{3}|[0-9a-f]{6})$", v)
    if not m:
        return v
    s = m.group(1)
    return "#" + ("".join(c * 2 for c in s) if len(s) == 3 else s)


def _wcag_lum(h):
    """#rrggbb → 相对亮度（WCAG 2.x 的公式，纯算，不碰任何本机状态）。"""
    def _ch(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(h[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _ch(r) + 0.7152 * _ch(g) + 0.0722 * _ch(b)


def wcag(h1, h2):
    """两个 #rrggbb 的对比度（1~21）。气泡是给一臂远扫一眼看的，小字低于 4.5 就是"看不清"。"""
    a, b = _wcag_lum(h1), _wcag_lum(h2)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def page_tokens(page_src):
    """从 index.html 读出暗色档 / 亮色档的令牌（带 `--card` 的那两块才是设计令牌块）。"""
    dark = [b for b in css_blocks(page_src, ":root") if "--card" in b]
    light = [b for b in css_blocks(page_src, '[data-theme="light"]') if "--card" in b]
    if not dark or not light:
        FAILS.append('index.html 里没找到设计令牌块（:root / [data-theme="light"]）')
        return {}, {}
    return css_vars(dark[0]), css_vars(light[0])


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ns, server_src, grabbed = load_fragment()
    page_src = open(PAGE, encoding="utf-8").read()
    R = ns["_balloon_rects"]
    hx = lambda ref: "#%02x%02x%02x" % ns["_rgb_of"](ref)
    print("")

    # ══ 1. 版式：横带互不相交（用户报的「按钮遮挡文字」就是它没做到）══
    g = R()
    card, app, icon = g["card"], g["app"], g["icon"]
    thumb, text, meta = g["thumb"], g["text"], g["meta"]
    foot = g["foot"]
    print("── 1. 版式（卡片 %d×%d 逻辑像素，苹果通知版）" % (ns["BALLOON_W"], ns["BALLOON_H"]))
    check("卡片就是常量那份尺寸", card == (0, 0, ns["BALLOON_W"], ns["BALLOON_H"]), str(card))
    check("App 行贴顶、图标是正方形、左边对齐内边距",
          app[1] == ns["BALLOON_APP_TOP"] and app[3] - app[1] == ns["BALLOON_APP_H"]
          and (icon[2] - icon[0]) == (icon[3] - icon[1]) == ns["BALLOON_APP_H"]
          and icon[0] == ns["BALLOON_PAD_X"], "app=%s icon=%s" % (app, icon))
    # 【2026-10-29 用户：「这个字体换成，聚光灯式的字体，包括卡片式字体，自动的字体」】
    # 四档共用聚光灯那一条标题带：整行宽、两行 × 30。代价是把题图从右侧那一栏挪进
    # **底部左角**（480 宽的卡上，84 的方图横在中间，标题只剩 340 物理像素 —— 实测
    # 200 条真标题里 81 条被砍字，40.5%）。挪完之后标题带宽 436、字号还大一档，
    # 砍字反而掉到 14 条（7%）。所以这里盯的不变式跟着换：
    #   · 标题带 = 整行宽（不再给图让位）；
    #   · 题图缩成底部左角那一张，和操作行共用那一档底距；
    #   · 来源行 / 分页点 / 两颗按钮都让出题图左边那一条，谁都不许和图相交。
    _ov = lambda a, b: bool(a) and bool(b) and a[0] < b[2] and b[0] < a[2] \
        and a[1] < b[3] and b[1] < a[3]
    check("题图缩成底部左角那一张正方形（原来是 84 挤在标题右边，把标题带压掉一半）",
          (thumb[2] - thumb[0]) == (thumb[3] - thumb[1]) == ns["BALLOON_THUMB"]
          and thumb[0] == ns["BALLOON_PAD_X"]
          and ns["BALLOON_THUMB"] < ns["BALLOON_W"] // 4, str(thumb))
    check("题图底距和操作行同一档（都是 8），下沿不出卡片",
          card[3] - thumb[3] == ns["BALLOON_H"] - (ns["BALLOON_THUMB_TOP"] + ns["BALLOON_THUMB"]) == 8,
          "底距 %d" % (card[3] - thumb[3]))
    check("标题带是整行宽（四档共用聚光灯那一档，21.6/700 才有地方站）",
          text == (ns["BALLOON_PAD_X"], ns["BALLOON_TEXT_TOP"],
                   ns["BALLOON_W"] - ns["BALLOON_PAD_X"], text[3])
          and text[2] - text[0] == ns["BALLOON_TITLE_W"], str(text))
    check("来源行和分页点一起让到题图右边（让出图宽 + 气隙，不是各挪各的）",
          meta[0] == thumb[2] + ns["BALLOON_THUMB_GAP"] == g["pager"][0],
          "meta.x=%d pager.x=%d thumb.right=%d" % (meta[0], g["pager"][0], thumb[2]))
    check("**标题带底边 < 来源行顶**（两行标题不压小字）", text[3] <= meta[1],
          "text.bottom=%d meta.top=%d" % (text[3], meta[1]))
    check("**来源行底边 ≤ 底部操作行顶边**（遮挡文字的老 bug）", meta[3] <= foot[1],
          "meta.bottom=%d foot.top=%d" % (meta[3], foot[1]))
    check("标题带给满两行（%d 行 × %d 像素）" % (ns["BALLOON_TEXT_LINES"], ns["BALLOON_LINE_H"]),
          text[3] - text[1] >= ns["BALLOON_TEXT_LINES"] * ns["BALLOON_LINE_H"],
          "高 %d" % (text[3] - text[1]))
    check("标题带和题图不相交（整行宽标题压在图上，等于把「按钮遮挡文字」再做一遍）",
          not _ov(text, thumb), "text=%s thumb=%s" % (text, thumb))
    check("题图不碰来源行、分页点、两颗按钮（底部那一栏挤了四样东西）",
          not any([_ov(thumb, b) for b in (meta, g["pager"], g["btn_open"], g["btn_close"])]),
          str([k for k, b in (("meta", meta), ("pager", g["pager"]),
                              ("open", g["btn_open"]), ("close", g["btn_close"]))
               if _ov(thumb, b)]))
    fit = ((text[2] - text[0]) // ns["BALLOON_HEAD_PX"]) * ns["BALLOON_TEXT_LINES"]
    check("标题截断长度 ≤ 版面装得下的字数（超了就永远少画一行）",
          ns["BALLOON_TITLE_MAX"] <= fit, "截 %d 字，版面约 %d 字" % (ns["BALLOON_TITLE_MAX"], fit))
    check("兜底版式那一档比共用标题字号小（挤不下时退一号，不退版式）",
          0 < ns["BALLOON_TITLE_PX"] < ns["BALLOON_HEAD_PX"],
          "%s vs %s" % (ns["BALLOON_TITLE_PX"], ns["BALLOON_HEAD_PX"]))
    check("按钮落在底部操作行里、不出卡片",
          foot[1] <= g["btn_open"][1] and g["btn_close"][3] <= foot[3]
          and g["btn_close"][3] <= card[3], str(g["btn_close"]))
    # 【2026-10-28 第五轮】顺序改成 WinUI3 样张：次按钮「关闭」在左，主按钮「打开」贴最右。
    check("两个按钮不相交（关闭在左、打开贴最右，中间留 GAP）",
          g["btn_close"][2] <= g["btn_open"][0]
          and (g["btn_open"][0] - g["btn_close"][2]) == ns["BALLOON_BTN_GAP"],
          "open=%s close=%s" % (g["btn_open"], g["btn_close"]))
    check("主按钮「打开」右边贴内边距（最右那颗是主操作）",
          g["btn_open"][2] == ns["BALLOON_W"] - ns["BALLOON_PAD_X"], str(g["btn_open"]))
    check("按钮行在标题带之下（几何上不可能压字）", text[3] < g["btn_open"][1],
          "text.bottom=%d btn.top=%d" % (text[3], g["btn_open"][1]))
    check("倒计时线已经删掉（用户原话「倒计时条不要，看着心慌」）",
          "bar" not in g and "BALLOON_BAR_H" not in ns and "BALLOON_BAR_TOP" not in ns,
          str(sorted(g.keys())))
    check("底边上只有分页点和两颗胶囊，谁都不许伸出卡片",
          g["pager"][3] <= card[3] and g["btn_close"][3] <= card[3]
          and g["btn_open"][3] <= card[3], "")
    check("分页点和按钮不相交（点在最左，按钮在最右）", g["pager"][2] <= g["btn_open"][0],
          "pager.right=%d open.left=%d" % (g["pager"][2], g["btn_open"][0]))
    check("分页点在底部行内垂直居中", foot[1] <= g["pager"][1] and g["pager"][3] <= foot[3],
          str(g["pager"]))
    gn = R(thumb=False)
    check("没题图时来源行和分页点回到最左（左角不再空着一张图的位置）",
          gn["thumb"] is None and gn["meta"][0] == gn["pager"][0] == ns["BALLOON_PAD_X"],
          str(gn["meta"]))
    check("标题带不管有没有图都是整行宽（右侧不再白留一条，图挪到底角了）",
          gn["text"] == text and gn["text"][2] == ns["BALLOON_W"] - ns["BALLOON_PAD_X"],
          "%s vs %s" % (gn["text"], text))
    check("有图/没图只是底部左角那张方图的差别（标题顶边、来源行高度一个像素不动）",
          gn["text"][:2] == text[:2] and gn["meta"][1] == meta[1]
          and gn["meta"][3] == meta[3], "%s vs %s" % (gn["meta"], meta))

    # 换条数、换序号、换尺寸都不许破坏上面的不变式
    bad = []
    n_case = 0
    for with_thumb in (True, False):
        for total in (1, 2, 6, 10, 12):
            for idx in range(total):
                for (w, h) in ((ns["BALLOON_W"], ns["BALLOON_H"]), (480, 170), (420, 140)):
                    n_case += 1
                    x = R(w, h, total=total, idx=idx, thumb=with_thumb)
                    if x["text"][3] > x["meta"][1]:
                        bad.append("压小字@%d条/%d/%dx%d" % (total, idx, w, h))
                    if x["meta"][3] > x["foot"][1]:
                        bad.append("压操作行@%d条/%d/%dx%d" % (total, idx, w, h))
                    if x["pager"][2] > x["btn_open"][0]:
                        bad.append("点碰按钮@%d条/%d/%dx%d" % (total, idx, w, h))
                    if x["btn_close"][2] > w or x["btn_close"][3] > h or x["btn_open"][0] < 0:
                        bad.append("出界@%d条/%d/%dx%d" % (total, idx, w, h))
                    # 【2026-10-29】题图搬进底部那一栏以后，判据从「标题右 ≤ 图左」
                    # 换成**真矩形相交**：小尺寸卡片（420×140）上图会顶到标题带底下，
                    # 老写法（只比 x）在这种尺寸上根本管不住纵向那一撞。
                    if x["thumb"] and _ov(x["text"], x["thumb"]):
                        bad.append("压题图@%d条/%d/%dx%d" % (total, idx, w, h))
                    for _k in ("meta", "pager", "btn_open", "btn_close"):
                        if x["thumb"] and _ov(x[_k], x["thumb"]):
                            bad.append("图撞%s@%d条/%d/%dx%d" % (_k, total, idx, w, h))
                    if x["thumb"] and x["thumb"][3] > h:
                        bad.append("图出界@%d条/%d/%dx%d" % (total, idx, w, h))
                    if x["dots"]["idx"] != min(max(idx, 0), total - 1) or x["dots"]["n"] != total:
                        bad.append("点位不对@%d条/%d/%dx%d" % (total, idx, w, h))
    check("%d 组（有无题图 × 1/2/6/10/12 条 × 三种尺寸）没有一组压字、压图、碰按钮或出界" % n_case,
          bad == [], "; ".join(bad[:4]))
    check("条数为 0、序号为负也不歪（兜底成 1 个点）",
          R(440, 152, total=0, idx=-3)["dots"]["n"] == 1
          and R(440, 152, total=0, idx=-3)["dots"]["idx"] == 0, "")
    check("_balloon_rects 的键就是绘制/命中用到的那一套（画和点同源）",
          {"card", "app", "icon", "thumb", "text", "meta", "foot", "pager",
           "btn_open", "btn_close", "dots"} <= set(g.keys()), str(sorted(g.keys())))
    check("操作行贴着底边（用户：「这两个键位置靠上，应该调整到底部」）",
          card[3] - g["btn_close"][3] <= 20 and g["btn_close"][1] >= foot[1],
          "底距 %d" % (card[3] - g["btn_close"][3]))

    # ══ 2. 配色同步闸门：CONSOLE_TOKENS 必须逐字等于页面里的设计令牌 ══
    print("── 2. 与控制台令牌同步")
    dark_css, light_css = page_tokens(page_src)
    for 档名, css in (("dark", dark_css), ("light", light_css)):
        tk = ns["CONSOLE_TOKENS"].get(档名) or {}
        wrong_color = []
        for var, key in COLOR_VARS.items():
            if var not in css:
                wrong_color.append("%s 页面里没有" % var)
            elif norm_hex(css[var]) != norm_hex(tk.get(key)):
                wrong_color.append("%s 页面=%s 气泡=%s" % (var, norm_hex(css[var]), norm_hex(tk.get(key))))
        wrong_num = []
        for var, key in NUM_VARS.items():
            pv = css.get(var, "")
            if var == "src-bg":
                ok = pv and abs(float(pv) - float(tk.get(key))) < 1e-9
            else:
                ok = pv.rstrip("%") == str(tk.get(key))
            if not ok:
                wrong_num.append("%s 页面=%s 气泡=%s" % (var, pv, tk.get(key)))
        check("气泡 %s 档 13 个颜色令牌和页面一致" % 档名, wrong_color == [], "; ".join(wrong_color))
        check("气泡 %s 档 4 个来源标签参数和页面一致" % 档名, wrong_num == [], "; ".join(wrong_num))

    # 主色那五档圆点对应的覆盖规则
    acc_dark = {m.group(1): css_vars(m.group(2)) for m in
                re.finditer(r'html\[data-accent="(\w+)"\]\s*\{([^}]*)\}', page_src)}
    acc_light = {m.group(1): css_vars(m.group(2)) for m in
                 re.finditer(r'html\[data-theme="light"\]\[data-accent="(\w+)"\]\s*\{([^}]*)\}', page_src)}
    for 档名 in ("dark", "light"):
        mine = ns["CONSOLE_ACCENTS"].get(档名) or {}
        css_tbl = acc_dark if 档名 == "dark" else acc_light
        diff = []
        for name, (a, ink) in mine.items():
            got = css_tbl.get(name) or {}
            if norm_hex(got.get("accent")) != norm_hex(a) \
                    or norm_hex(got.get("accent-ink")) != norm_hex(ink):
                diff.append("%s 页面=%s/%s 气泡=%s/%s"
                            % (name, got.get("accent"), got.get("accent-ink"), a, ink))
        check("%s 档四个主色覆盖值和页面一致" % 档名,
              diff == [] and len(css_tbl) == len(mine) == 4,
              "; ".join(diff) or ("页面 %d 档 / 气泡 %d 档" % (len(css_tbl), len(mine))))
    check("default（品牌色）不进覆盖表 —— 它就是用 --accent 本身",
          "default" not in ns["CONSOLE_ACCENTS"]["dark"], str(sorted(ns["CONSOLE_ACCENTS"]["dark"])))

    # 不只是常量抄对：真正发给 GDI 的那个 COLORREF 也要等于页面那个色
    p = ns["_balloon_palette"]({"theme": "dark", "accent": "default"})
    p2 = ns["_balloon_palette"]({"theme": "light", "accent": "default"})
    check("_balloon_palette(dark) 发出去的 card 就是页面 --card",
          hx(p["card"]) == norm_hex(dark_css["card"]), "%s vs %s" % (hx(p["card"]), dark_css["card"]))
    check("_balloon_palette(light) 的 txt / accent 也是亮色档那一组",
          hx(p2["txt"]) == norm_hex(light_css["txt"])
          and hx(p2["accent"]) == norm_hex(light_css["accent"]), "%s %s" % (hx(p2["txt"]), hx(p2["accent"])))
    check("亮/暗两档确实不一样（用户说的「看不出区别」就卡在这儿）",
          {hx(p[k]) for k in ("card", "txt")} != {hx(p2[k]) for k in ("card", "txt")},
          "%s / %s" % (sorted({hx(p[k]) for k in ("card", "txt")}), sorted({hx(p2[k]) for k in ("card", "txt")})))
    pb = ns["_balloon_palette"]({"theme": "light", "accent": "blue"})
    check("页面选了蓝主色 → 气泡的 accent 换成亮色档那个蓝",
          hx(pb["accent"]) == norm_hex(acc_light["blue"]["accent"])
          and hx(pb["accent_ink"]) == norm_hex(acc_light["blue"]["accent-ink"]), hx(pb["accent"]))
    check("主色只动 accent，card/txt 不动", pb["card"] == p2["card"] and pb["txt"] == p2["txt"], "")
    check("pal 里带 theme / accent_name（日志和 /api/ui 能报出实际用了哪一档）",
          p["theme"] == "dark" and p["accent_name"] == "default", str((p["theme"], p["accent_name"])))
    check("pal 里也带来源标签那四个参数（页面改色时气泡跟着改）",
          (p["src_s"], p["src_l"], p["src_b"], p["src_a"])
          == (ns["CONSOLE_TOKENS"]["dark"]["src_s"], ns["CONSOLE_TOKENS"]["dark"]["src_l"],
              ns["CONSOLE_TOKENS"]["dark"]["src_b"], ns["CONSOLE_TOKENS"]["dark"]["src_a"]),
          str((p["src_s"], p["src_l"], p["src_b"], p["src_a"])))

    # 对比度闸门：用户连着两轮报「字体不清楚」，其中一条真凶不是字体渲染，是**颜色太浅** ——
    # 页面板那档 --txt3 是给坐着凑近读的网页用的（亮色白卡上只有 2.9:1），搬到 11.5px 的小字
    # 上直接发灰。这里不写死色值（色值只许从 CONSOLE_TOKENS 出），只断言"发出去的那组 COLORREF
    # 摆在卡底上，对比度够不够"，纯算，换机器结果一样。
    print("── 2b. 气泡文字对比度（WCAG，一臂远扫一眼的下限）")
    for 档名, pp in (("暗色", p), ("亮色", p2)):
        base = hx(pp["card"])
        for key, 门槛 in (("txt", 7.0), ("txt2", 4.5), ("txt3", 4.5)):
            got = wcag(hx(pp[key]), base)
            check("%s档 %s on card 对比 ≥ %.1f（实测 %.2f，%s on %s）"
                  % (档名, key, 门槛, got, key, base), got >= 门槛,
                  "%.2f  %s vs %s" % (got, hx(pp[key]), base))
    check("提亮只动 txt2/txt3，主文字色和卡底没被改动（还是页面板那几个值）",
          hx(p["txt"]) == norm_hex(dark_css["txt"])
          and hx(p["card"]) == norm_hex(dark_css["card"])
          and hx(p2["txt"]) == norm_hex(light_css["txt"])
          and hx(p2["card"]) == norm_hex(light_css["card"]), "")
    # 页面板那档 --txt-3 的原文（CSS 用连字符，令牌表用下划线，这里查的是 CSS 那块）
    page_txt3 = norm_hex(light_css.get("txt-3") or "")
    check("txt3 确实比页面板那档更实（不是原样抄过来）",
          page_txt3 and wcag(hx(p2["txt3"]), hx(p2["card"]))
          > wcag(page_txt3, norm_hex(light_css["card"])),
          "气泡 %s %.2f vs 页面板 %s" % (hx(p2["txt3"]),
                                         wcag(hx(p2["txt3"]), hx(p2["card"])), page_txt3))

    # ══ 3. 档位判定：和控制台同一条判据 ══
    print("── 3. 主题判定")
    TN = ns["_balloon_theme_name"]
    check("theme=light → 亮色档", TN({"theme": "light"}) == "light")
    check("theme=dark → 暗色档", TN({"theme": "dark"}) == "dark")
    ns["_system_prefers_light"] = lambda: True
    check("theme=auto + 系统亮 → 亮（= 页面 prefers-color-scheme 的行为）",
          TN({"theme": "auto"}) == "light")
    ns["_system_prefers_light"] = lambda: False
    check("theme=auto + 系统暗 → 暗", TN({"theme": "auto"}) == "dark")
    check("脏取值（页面没这一档）退回跟随系统", TN({"theme": "????"}) == "dark")
    check("没写 ui 段也不炸：按默认 auto 走系统判据", TN({}) == TN(None) == "dark")
    check("_ui_cfg 缺项按 UI_DEFAULTS 补齐（默认跟随系统 + 品牌色）",
          ns["_ui_cfg"]() == dict(ns["UI_DEFAULTS"]), str(ns["_ui_cfg"]()))
    check("页面回报的 system 别名也认（控制台旧键写的是 system）",
          TN({"theme": "system"}) == "dark", "")
    # 注册表那颗判据：必须查**打开的键句柄**，不能拿 HKEY_CURRENT_USER 根键去查值
    sp_src = ast.get_source_segment(server_src, grabbed.get("_system_prefers_light")) or ""
    check("_system_prefers_light 查的是打开的那把句柄（不是 HKCU 根键）",
          "QueryValueEx(winreg.HKEY_CURRENT_USER" not in sp_src
          and " as " in sp_src and "AppsUseLightTheme" in sp_src,
          "查根键 = 永远 FileNotFoundError = 气泡永远暗色（2026-10-28 真踩过）")
    check("读不到注册表时退回暗色（和页面 <html data-theme=\"dark\"> 的默认一致）",
          "return False" in (sp_src.strip().splitlines() or [""])[-1],
          (sp_src.strip().splitlines() or [""])[-1])

    # ══ 4. 标签行与条目：_balloon_head 拼出去，_balloon_split_head 要拆回来 ══
    print("── 4. 标签行与条目")
    BH, SH = ns["_balloon_head"], ns["_balloon_split_head"]
    cases = [
        ({"cls": "科技", "source": "快科技"}, "科技 · 快科技", ("科技", "快科技", "")),
        ({"cls": "头条", "source": "凤凰网"}, "【头条】 · 凤凰网", ("头条", "凤凰网", "")),
        ({"cls": "头条"}, "【头条】", ("头条", "", "")),
        ({"cls": "热榜", "source": "凤凰网", "rank": 4}, "【热榜】凤凰网 第4", ("热榜", "凤凰网", "第4")),
        ({"cls": "热榜", "source": "凤凰网"}, "【热榜】凤凰网", ("热榜", "凤凰网", "")),
        ({"cls": "热榜"}, "【热榜】", ("热榜", "", "")),
        ({"cls": "要闻", "region": "intl", "label": "凤凰网"}, "国际·要闻 · 凤凰网",
         ("国际·要闻", "凤凰网", "")),
        ({"cls": "", "region": "intl"}, "国际·要闻", ("国际·要闻", "", "")),
        ({"cls": "", "region": "cn", "label": "36氪"}, "热点 · 36氪", ("热点", "36氪", "")),
    ]
    bad = []
    for it, want_head, want_split in cases:
        got_head = BH(it)
        if got_head != want_head:
            bad.append("拼出 %r（期望 %r）" % (got_head, want_head))
            continue
        got = SH(got_head)
        if got != want_split:
            bad.append("%r 拆成 %r（期望 %r）" % (got_head, got, want_split))
    check("%d 种抬头都拼得对、也拆得回（栏目/来源/备注各画各的）" % len(cases), bad == [],
          "; ".join(bad))
    check("来源名带空格不被劈开（BBC News 是站名，不是「BBC」+「News」）",
          SH("国际·要闻 · BBC News") == ("国际·要闻", "BBC News", ""), str(SH("国际·要闻 · BBC News")))
    check("只有末尾的排名才当备注（多字来源 + 第N 也拆得开）",
          SH("【热榜】BBC News 第9") == ("热榜", "BBC News", "第9"), str(SH("【热榜】BBC News 第9")))
    check("空抬头拆出三个空串（没栏目就不画空标签）", SH("") == ("", "", ""), str(SH("")))
    check("拆出来的三段不带残留分隔符",
          all(" · " not in x and not x.startswith("【") for x in SH("科技 · 快科技")),
          str(SH("科技 · 快科技")))

    e = ns["_balloon_entry"]({"cls": "科技", "source": "快科技", "link": "http://x",
                             "image": "http://img/x.png", "title": "标" * 200,
                             "published": 0})
    check("条目是 7 段（抬头/标题/warn/链接/题图/来源/时间）—— 题图和时间靠位次传",
          isinstance(e, tuple) and len(e) == 7, str(len(e)))
    check("标题按版面截断（超长不伸到按钮那一行）", len(e[1]) == ns["BALLOON_TITLE_MAX"], str(len(e[1])))
    check("题图 / 链接 / 来源都带上了",
          e[3] == "http://x" and e[4] == "http://img/x.png" and e[5] == "快科技", str(e[3:6]))
    check("没有发布时间就不编时间（右上角留空，不显示假数据）", e[6] == "", str(e[6]))
    e2 = ns["_balloon_entry"]({"cls": "热榜", "source": "凤凰网", "rank": 4, "title": "标题"})
    check("热榜条目：抬头带平台与名次、来源行写「第 N 位」",
          e2[0] == "【热榜】凤凰网 第4" and e2[5] == "凤凰网 · 第 4 位", "%s / %s" % (e2[0], e2[5]))
    check("国际条目未翻译时退回英文标题（不空着）",
          ns["_balloon_entry"]({"cls": "要闻", "region": "intl",
                                "title": "", "title_en": "English Title"})[1] == "English Title", "")
    tm = ns["_balloon_time_line"]({"published": time.time() - 5})
    check("时间行不空（刚发的是「刚刚」这类相对时间）", tm != "" and "刚刚" in tm, str(tm))

    # 题图缓存落地：只在文件真的存在（且 >1KB）时才返回路径
    pth = ""
    d = ""
    try:
        d = tempfile.mkdtemp(prefix="hn_balloon_")
        url = "https://example.invalid/pic.jpg"
        pth = os.path.join(d, hashlib.sha1(url.encode("utf-8")).hexdigest()[:24] + ".img")
        old = ns["BALLOON_IMG_DIR"]
        ns["BALLOON_IMG_DIR"] = d
        check("缓存没下好时不返回路径（绘制走占位块）", ns["_balloon_img_local"](url) == "", "")
        with open(pth, "wb") as f:
            f.write(b"x" * 2048)
        check("缓存到位后按 sha1 命中同一个文件", ns["_balloon_img_local"](url) == pth, "")
        check("非 http(s) 的题图一律不认（不把本地路径当图去读）",
              ns["_balloon_img_local"]("C:\\x.jpg") == "" and ns["_balloon_img_local"]("") == "", "")
        ns["BALLOON_IMG_DIR"] = old
    finally:
        if pth:
            try:
                os.remove(pth)
            except Exception:
                pass
        if d:
            try:
                os.rmdir(d)
            except Exception:
                pass

    # ══ 5. 颜色算术：GDI 没有 alpha 通道，混色和 HSL 都得自己算 ══
    print("── 5. 颜色算术")
    _hex, _hsl, _mix, _rgb = ns["_hex_ref"], ns["_hsl_ref"], ns["_mix_ref"], ns["_rgb_of"]
    check("#fff 和 #ffffff 是同一个色", _hex("#fff") == _hex("#ffffff"), "")
    check("#20252d 按 GDI 的 0x00BBGGRR 存", _rgb(_hex("#20252d")) == (0x20, 0x25, 0x2D),
          str(_rgb(_hex("#20252d"))))
    check("脏输入不至于崩（退回黑的）", _rgb(_hex("nonsense")) == (0, 0, 0), "")
    check("alpha=1 就是前景色本身", _rgb(_mix(_hex("#ff0000"), _hex("#000000"), 1.0)) == (255, 0, 0), "")
    check("alpha=0 就是背景色本身", _rgb(_mix(_hex("#ff0000"), _hex("#00ff00"), 0.0)) == (0, 255, 0), "")
    check("alpha 越界被夹住（不出现负色值）",
          _rgb(_mix(_hex("#ffffff"), _hex("#000000"), 9))[0] == 255
          and _rgb(_mix(_hex("#ffffff"), _hex("#000000"), -9))[0] == 0, "")
    check("半透明压黑底 = 变暗（和页面 color-mix 一个口径）",
          _rgb(_mix(_hex("#ffffff"), _hex("#000000"), 0.5))[0] == 128, "")
    check("饱和度 0 出来是灰（r=g=b）", len({_rgb(_hsl(210, 0, 60))}) == 1, str(_rgb(_hsl(210, 0, 60))))
    check("色相越界能绕回来（400° == 40°）且不越 0~255",
          _rgb(_hsl(400, 50, 70)) == _rgb(_hsl(40, 50, 70))
          and all(0 <= v <= 255 for v in _rgb(_hsl(400, 50, 70))), str(_rgb(_hsl(400, 50, 70))))

    # ══ 6. 结构闸门：WM_PAINT 真走这套几何与配色，别改回各画各的 ══
    print("── 6. 绘制链路")
    cls_src = ast.get_source_segment(server_src, grabbed.get("_Balloon")) or ""
    paint = segment(cls_src, "if msg == 0x000F:")
    check("框到了 WM_PAINT 那一段", paint.startswith("if msg == 0x000F:"), "%d 字符" % len(paint))
    check("WM_PAINT 取 _balloon_rects 那份几何（画和点击判定同源）", "_balloon_rects(" in paint, "")
    check("WM_PAINT 取色只走 _refresh_pal() + pal[...]",
          "_refresh_pal()" in paint and "pal[" in paint, "")
    check("WM_PAINT 里没有写死的旧色值（0x1F2430 / 0xB06C2B 那一套）",
          not any(t in paint for t in ("0x1F2430", "0xB06C2B", "0x3E3EE5", "0xF0F1FF")), "")
    check("倒计时条是真的删干净了（用户：「倒计时条不要，看着心慌」）",
          "tick_left" not in paint and "tick_total" not in paint
          and "BALLOON_TICK_MS" not in ns and "BALLOON_BAR_TOP" not in ns, "")
    check("分页点画的也是同一份 _g（点数/序号不再各算各的）", "_draw_pager(" in paint, "")
    check("App 行和题图都画了（苹果通知版的两个新件）",
          "_draw_app_icon(" in paint and "_draw_thumb(" in paint, "")
    check("App 行右端读 self.cur[6]、题图读 self.cur[4]、来源行读 self.cur[5]",
          all(k in paint for k in ("self.cur[4]", "self.cur[5]", "self.cur[6]")), "")
    _pbase = segment(cls_src, "def _paint_card_base")
    _btns = segment(cls_src, "def _draw_buttons")
    # 【2026-10-28 第六轮】胶囊底片挪进 _draw_buttons 了：它是每帧最后一步，
    # 才压得住播报版式铺的整张大图 / 跑马灯底带（放在 _paint_card_base 会被盖掉）。
    check("按钮平时隐身、鼠标靠近才浮出来（用户：「要整成鼠标靠近才显示的状态」）",
          "if not self.hover:" in _btns and "return" in _btns.split("if not self.hover:", 1)[1][:80],
          "")
    check("底片和字都在 _draw_buttons 里画（这样才压得住版式铺的图与跑马灯）",
          "_gp_fill_round" in _btns and '"关闭"' in _btns and '"打开"' in _btns, "")
    check("聚光灯上的次按钮改成深色片 + 白边（浅灰压在亮照片上会直接消失）",
          'if self.style == "spot":' in _btns and "0xFFFFFF, 70" in _btns, "")
    check("命中矩形照样每帧都记（不然鼠标第一次扫过去、重绘还没发生就点空）",
          _btns.find("self._btn_close =") >= 0
          and _btns.find("self._btn_close =") < _btns.find("if not self.hover:"), "")
    check("靠近就算悬停（敏感圈比卡片大一圈，手凑近投影边按钮就出来）",
          "16 * self.scale" in segment(cls_src, "if msg == 0x0200:"), "")
    check("WM_PAINT 用到的那些键都从 _balloon_rects 的 _g 里取（按钮几何整份交给 _draw_buttons）",
          all(('_g["%s"]' % k) in paint for k in
              ("app", "icon", "thumb", "text", "meta", "pager"))
          and "_draw_buttons(hdc, _g" in paint, "")
    ap = segment(cls_src, "def _apply_current")
    check("进度不再靠标题尾部的 (3/10)（改到底部那一排点）",
          "(%d)" not in ap and "str(len(self.playlist))" not in ap, ap[:160].replace("\n", " "))
    check("每条开播前刷一次配色（页面切了主题，下一条就跟上）", "_refresh_pal()" in ap, "")
    check("圆角是 WinUI3 那一档（卡片 8 / 题图 14），不是老版药丸",
          ns["BALLOON_RADIUS"] == 8 and ns["BALLOON_THUMB_RADIUS"] == 14
          and ns["BALLOON_THUMB_RADIUS"] * 2 < ns["BALLOON_THUMB"], "")
    check("按钮是 4px 圆角的小块（高 27 装得进贴着底边的操作行，悬停只提亮不挪位）",
          ns["BALLOON_BTN_H"] <= ns["BALLOON_FOOT_BOTTOM"] - ns["BALLOON_FOOT_TOP"]
          and ns["BALLOON_BTN_GAP"] == 12
          and "btn_close" in _btns, "")
    check("操作行贴着底边（底距不超过 20px）",
          ns["BALLOON_H"] - ns["BALLOON_FOOT_BOTTOM"] <= 20,
          str(ns["BALLOON_H"] - ns["BALLOON_FOOT_BOTTOM"]))
    # Fluent 化的三道结构闸：分层窗口 + GDI+ 装饰 + GDI 透明洞修补
    check("窗口是 WS_EX_LAYERED，用 UpdateLayeredWindow 逐像素透明",
          "0x00080000" in cls_src and "UpdateLayeredWindow" in cls_src, "")
    check("卡面/圆角/投影走 GDI+（_paint_card_base + 圆角路径）",
          "def _paint_card_base" in cls_src and "GdipFillPath" in cls_src
          and "_gp_stroke_round" in cls_src, "")
    check("GDI 画完把卡片内的 alpha 钉回卡面那一档（不然文字/分页点是透明洞）",
          "def _solidify_alpha" in cls_src and "_solidify_alpha()" in cls_src
          and "memoryview(raw).cast" in cls_src, "")
    # 【2026-10-28 第六轮 用户：「字体发虚」】ClearType 把笔画边缘的灰度藏在 R/G/B 三个
    # 子像素里，而分层窗口最后要拿 alpha 和桌面混一次 —— 子像素排列被打散，字边全是
    # 橙/蓝毛边（实拍量过：亮色卡上中间调像素 100% 带 ≥12 的通道偏差）。
    # 逐像素透明的窗口只能用灰度抗锯齿（ANTIALIASED_QUALITY=4），改回 5 就等于把这条改回去。
    # 【2026-10-29 精简成三档】新闻条专用那支 font_hl 随档删了，字体从 9 支变 8 支 ——
    # 所以这里**不再写死支数**：拿「有几支 CreateFontW」当分母，要求每一支都带质量参数、
    # 且全是 4。以后加一档字体也自动被这条盯住，删一档也不用顺手来改闸门。
    _q = re.findall(r"134,\s*5,\s*0,\s*(\d),\s*0", cls_src)
    _nf = cls_src.count("= g.CreateFontW(")
    check("气泡每一档字体都走灰度抗锯齿(4)，没有一档退回 ClearType(5)",
          _nf >= 1 and len(_q) == _nf and all(x == "4" for x in _q),
          "%d 支字体 / %d 处质量参数 %s" % (_nf, len(_q), sorted(set(_q))))
    check("每次开画先归零视口原点（不然 GDI+ 每重绘一次就往右下跑一截）",
          "SetViewportOrgEx" in cls_src and "0, 0, None" in cls_src, "")
    # 命中判定：点气泡某处打开的是**当前这条**（self.cur[3]），「关闭」走 _dismiss
    click = segment(cls_src, "if msg == 0x0201:")
    check("点击「关闭」先命中 _btn_close（和绘制同一份 device 坐标）",
          "_in(self._btn_close)" in click, "%d 字符" % len(click))
    check("点开一条 → 先记兴趣，再开网页（兴趣永远不会挡住打开）",
          click.find("_interest_bump(") >= 0
          and click.find("_interest_bump(") < click.find("_open_app_later("), "")
    check("「关闭」走 _dismiss(drop_pending=True)（连排队那组一起丢掉）",
          "_dismiss(hwnd, drop_pending=True)" in click, "")

    # ── 名字闸门：ctypes / ctypes.wintypes 上压根没有的属性，真机一 import 就炸 ──
    # 这个坑已经踩过两次，两次都是同一类：
    #   · wintypes.UINT_PTR（2026-10-28 托盘）→ _Tray.__init__ AttributeError，菜单双击全没；
    #   · ctypes.LPVOID（2026-10-28 气泡）→ _Balloon.__init__ AttributeError，
    #     「[warn] 自绘气泡初始化失败」，右下角一条都不弹，而所有单测照样全绿。
    # 单测只摘函数、从不跑 __init__，所以只能在纯静态这一层把"这个名字不存在"闸死。
    import ctypes as _ct
    import ctypes.wintypes as _wt
    _pool = {"ctypes": set(dir(_ct)), "wintypes": set(dir(_wt))}
    _bad = {}
    for _n in ast.walk(ast.parse(server_src)):
        if isinstance(_n, ast.Attribute) and isinstance(_n.value, ast.Name):
            _have = _pool.get(_n.value.id)
            if _have is not None and _n.attr not in _have:
                _bad.setdefault("%s.%s" % (_n.value.id, _n.attr), _n.lineno)
    check("server.py 用到的 ctypes / wintypes 属性全都真实存在", _bad == {}, repr(_bad))

    # ══ 7. 兴趣投放：点开什么就多投什么（纯函数，喂权重）══
    print("── 7. 兴趣投放")
    IC, IQ = ns["_interest_class"], ns["_interest_quota"]
    BASE = tuple(ns["BALLOON_CLS_QUOTA"])
    check("抬头归一：'【热榜】'→热榜、'国际·要闻'→要闻、'热点'→头条、'科技'→科技",
          (IC("【热榜】"), IC("国际·要闻"), IC("热点"), IC("科技"))
          == ("热榜", "要闻", "头条", "科技"), "")
    check("冷启动（没有任何点开记录）原样返回默认配额",
          IQ(weights={}) == BASE, str(IQ(weights={})))
    check("点开次数不够（证据 < 3）也不改版面",
          IQ(weights={"科技": 2.9}) == BASE, str(IQ(weights={"科技": 2.9})))
    check("兴趣均匀（各类一样多）不动版面",
          IQ(weights={"科技": 1, "财经": 1, "娱乐": 1, "软件": 1}) == BASE, "")

    mild = dict(IQ(weights={"科技": 3.0, "财经": 1.0, "娱乐": 1.0, "软件": 1.0}))
    check("轻度偏爱科技（约 3 倍）→ 科技 +1 席（从热榜挪 1 席，热榜保 2）",
          mild.get("科技") == 2 and mild.get("热榜") == 2 and mild.get("财经") == 1
          and mild.get("娱乐") == 1 and mild.get("国际") == 2, str(mild))
    check("轻度偏爱不砍别的兴趣类（财经/娱乐还是 1 席）",
          mild.get("财经") == 1 and mild.get("娱乐") == 1, str(mild))

    strong = dict(IQ(weights={"科技": 8.0}))
    check("特别偏爱科技（≥4 倍）→ 科技 +2 席，先砍最不看的兴趣类",
          strong.get("科技") == 3 and strong.get("财经", 0) == 0
          and strong.get("娱乐", 0) == 0, str(strong))
    check("特别偏爱也保新闻骨架：头条 2 / 热榜 3 / 国际 2",
          strong.get("头条") == 2 and strong.get("热榜") == 3 and strong.get("国际") == 2, str(strong))
    check("总席位不变（还是 10 条，不是兴趣专刊）",
          sum(mild.values()) == 10 and sum(strong.values()) == 10,
          "%d / %d" % (sum(mild.values()), sum(strong.values())))
    floor_q = dict(IQ(weights={"科技": 8.0}, base=(("热榜", 3), ("科技", 1))))
    check("底线：热榜最少留 2 席（名额从热榜挪时也不清零）",
          floor_q.get("热榜") == 2 and floor_q.get("科技") == 3, str(floor_q))
    check("瘦配额（表里没有国际/头条）不会 KeyError，也不凭空加栏目",
          set(floor_q) == {"热榜", "科技"}, str(floor_q))

    # ══ 8. 自动投放：启动就播 + 只让路给「弹了也没用」的状态（2026-10-30 收窄） ══
    print("── 8. 自动投放")
    HOLD = ns["_hold_reason_from_state"]
    check("锁屏 / 休眠 → 不弹（弹了也看不见）", "锁屏" in HOLD(1), HOLD(1))
    check("演示 / 投影模式 → 不弹（真会盖住画面）", "演示" in HOLD(4), HOLD(4))
    # 【2026-10-30 用户：「怎么不自动播报了？」】系统报一次 BUSY 就把播报连挡 8 轮、
    # 56 分钟一声不响，而长时间全屏干活正是这个程序的主用法 —— 这三种状态不再让路。
    check("全屏程序 / 系统勿扰 → 照播（2026-10-30 从闸门里摘掉）",
          HOLD(2) == "" and HOLD(3) == "" and HOLD(6) == "", "%r %r %r" % (HOLD(2), HOLD(3), HOLD(6)))
    check("正常 / 单应用全屏 / 取不到状态 → 照常弹",
          HOLD(5) == "" and HOLD(0) == "" and HOLD(7) == "" and HOLD(None) == "" and HOLD("abc") == "",
          "%r %r %r" % (HOLD(5), HOLD(0), HOLD(None)))
    check("状态码是字符串也认（注册表/JSON 回读时常见）", HOLD("1") == HOLD(1), "")
    check("对照表还是全的（挡不挡看 _QUNS_HOLD，「为什么没播」要报得出状态名）",
          set(ns["_QUNS_LABEL"]) >= {1, 2, 3, 4, 5, 6, 7}, str(sorted(ns["_QUNS_LABEL"])))
    # 「工作时不打扰」开关：关掉就是一律照播；没在 NOTIFY_DEFAULTS 挂号会被落盘时抹掉。
    check("hold_on 在 NOTIFY_DEFAULTS 挂号且默认开",
          "hold_on" in ns["NOTIFY_DEFAULTS"] and ns["NOTIFY_DEFAULTS"]["hold_on"] is True,
          str(ns["NOTIFY_DEFAULTS"].get("hold_on")))
    _quns_real = ns["_quns_state"]
    ns["_quns_state"] = lambda: 1                      # 桩：单测不去读本机 Windows 状态
    ns["_foreground_hint"] = lambda: "前台=测试窗口｜类=Test｜1920x1080·铺满屏"
    ns["_notify_cfg"] = lambda: {"hold_on": True}
    check("闸门开着 + 锁屏 → 报出原因", "锁屏" in ns["_notify_hold_reason"](),
          ns["_notify_hold_reason"]())
    ns["_notify_cfg"] = lambda: {"hold_on": False}
    check("开关一关 → 连锁屏也照播（用户想要的那条退路）",
          ns["_notify_hold_reason"]() == "", ns["_notify_hold_reason"]())
    ns["_notify_cfg"] = lambda: {}                     # 老配置没这个键：按 True 走，不能因为缺键静音
    check("配置里缺 hold_on（老本机配置）→ 按默认开走，不静音",
          "锁屏" in ns["_notify_hold_reason"](), ns["_notify_hold_reason"]())
    ns["_notify_cfg"] = lambda: {"hold_on": True}
    _note = ns["_hold_note"]()
    check("「为什么没播」报得出现场：状态码 + 当时前台窗口是谁",
          "系统状态 1" in _note and "前台=测试窗口" in _note, _note)
    check("可以弹的时候不产生噪音（空原因 = 空串）", ns["_hold_note"]("") == "", repr(ns["_hold_note"]("")))
    ns["_quns_state"] = _quns_real

    news_src = func_seg(server_src, "_notify_news")
    check("更新提醒：自动那一轮先问「现在能不能打扰」",
          "_notify_hold_reason()" in news_src, "%d 字符" % len(news_src))
    check("免打扰在写 NEWS_LAST_TOAST **之前**（跳过不消耗节流窗口）",
          "_notify_hold_reason()" in news_src
          and news_src.find("_notify_hold_reason()") < news_src.find("NEWS_LAST_TOAST = now"), "")
    check("手动轮播（force）不看免打扰（用户主动要看就给看）",
          "if not force:" in news_src[max(0, news_src.find("_notify_hold_reason()") - 200):
                                      news_src.find("_notify_hold_reason()")], "")
    check("跳过那一轮把原因写进决定链（GET /api/notify 看得见，不用翻日志）",
          '_balloon_trace("跳过：%s' in news_src and "_hold_note(_hold)" in news_src, "")
    check("大事提醒同样让路（锁屏/演示才等）",
          "_notify_hold_reason()" in func_seg(server_src, "_notify_major"), "")
    check("今日 60 秒同样让路", "_notify_hold_reason()" in func_seg(server_src, "_notify_digest"), "")

    boot = segment(server_src, "def _boot_balloon")
    check("启动播报最长等 20 秒（原来 120 秒，开机两分钟什么都没有）",
          "range(20)" in boot and "range(120)" not in boot, "%d 字符" % len(boot))
    check("启动播报也过夜间免打扰 / 锁屏 / 演示两道闸",
          "_in_quiet_hours()" in boot and "_notify_hold_reason()" in boot, "")
    check("启动播报和更新提醒用同一个挑选器（配额不分叉）",
          "_pick_balloon_items(" in boot, "")
    check("挑选器把配额过一遍兴趣表（默认原样）",
          "_interest_quota()" in func_seg(server_src, "_pick_balloon_items"), "")

    # 开关得有两个看得见的手：托盘那一条 + 控制台同名项（用户 2026-10-30：「直接改」）
    check("托盘菜单挂了「工作时不打扰」并带上当前勾选态",
          "_TRAY_CMD_HOLD" in segment(server_src, "    def _build_menu(self):")
          and "工作时不打扰" in segment(server_src, "    def _build_menu(self):"), "")
    _rc = segment(server_src, "    def _run_cmd(self, cmd):")
    check("托盘点它是取反 + 落盘 + 给一条回执气泡（不回执等于没改成）",
          'not bool(_notify_cfg().get("hold_on"' in _rc
          and '_save_notify_cfg({"hold_on": new})' in _rc and "_show_toast(" in _rc, "")
    # func_seg 只认顶格 `def`，这个路由是 `async def`，所以自己框一段
    _ai = server_src.find("async def api_notify_set")
    check("POST /api/notify 认 hold_on（页面的开关和托盘写同一个键）",
          _ai >= 0 and '"hold_on"' in server_src[_ai:_ai + 1500], "找不到 async def api_notify_set")
    check("index.html 有第三个设置项 setHold 并回显/保存 hold_on",
          'id="setHold"' in page_src and "j.hold_on" in page_src
          and "saveNotify({hold_on:" in page_src, "")

    # 控制台也得上报"点开了哪一类"，否则开了网页的程序永远学不到兴趣
    check("index.html 点开卡片就上报栏目（/api/interest）",
          page_src.count("/api/interest") >= 1 and "keepalive" in page_src, "")
    check("两个可点区域都带 data-cls（标题和题图）",
          page_src.count("data-cls=") >= 2, str(page_src.count("data-cls=")))
    check("上报带本机令牌（不带就是 403，兴趣永远学不到）",
          "POST_HEADERS" in page_src and "X-HN-Token" in page_src, "")
    check("接口同时有读有写（GET 能看到权重，POST 记一分）",
          '@app.get("/api/interest")' in server_src and '@app.post("/api/interest")' in server_src, "")
    check("写接口过本机三道闸（令牌 / 回环 / 同源）",
          "_guard_mutating(request)" in server_src.split('@app.post("/api/interest")')[1][:600], "")

    # ── 文案红线：这一段不许出现对外敏感词 ──
    hit = [w for w in ("绕过", "翻墙", "梯子", "科学上网", "破解")
           if w in paint or w in str(ns["CONSOLE_TOKENS"])]
    check("版式/配色这段代码里没有红线词", hit == [], "hit=%r" % (hit,))

    print("")
    if FAILS:
        print("失败 %d 条:" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
