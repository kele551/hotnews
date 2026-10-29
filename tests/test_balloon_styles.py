# -*- coding: utf-8 -*-
# 作者: 海风（kele551）
# 用途: 气泡「播报样式可自选」的单测（2026-10-28）。
#
# 用户原话：「都好看，不行就让用户可以自行选择吧」+「视觉最冲击的这款，背景图片太模糊了」。
# 所以这一轮要盯的是：
#   1) 档位取值收敛（2026-10-29 用户：「播报样式太多，可以精简成三个样式，聚光灯必须留下」
#      → 只剩 自动/速递/聚光灯）—— 脏值/空值一律回「自动」；已经下线的老取值 card / tv
#      走 BALLOON_STYLE_LEGACY 归位，不被当成脏值偷偷改成自动（第 1 组，纯函数）；
#   2) 「自动」= 图已经在本机就聚光灯、还在路上就速递；**用户点名聚光灯时一档不跳**
#      —— 2026-10-28「图片样式，会出现没有图片的样式」，无图改画自己的一张底（第 2 组 + 5b）；
#   3) 入口只放**右下角托盘菜单**（用户：「样式选择的入口放在右下角菜单里吧」），
#      选完立刻播 —— 样式是给人看的，不看见等于没选（第 3 组，静态闸门）；
#   4) 控制台外观面板里**不该**再挂一份样式选择（两处入口会互相冲掉），
#      「翻牌」那档按用户「效果不好，这的样式取消」彻底删掉（第 4 组）。
#   5) 聚光灯的压暗层不能顶回黑底白字（2026-10-28「图背景样式还要改进一下，图片都看不清
#      了有些」，同日又被「这种模式字体不清楚」往回推了一档）—— 闸门按 y 把三层压暗叠起来
#      算**标题带里实际遮了多少**，正反两头都钉住（第 5 组）；
#   6) 用户主动开口的三条路不等上一组播完（2026-10-28「切换样式模式，播报暂停了一分钟」）
#      —— 掐窗口必须发生在气泡自己的消息线程里（第 6 组）；
#   7) 标题字号各档共用聚光灯那一档（2026-10-29「这个字体换成，聚光灯式的字体，包括卡片式
#      字体，自动的字体都换成聚光灯式的」）—— font_spot 改名 font_title，速递/自动/聚光灯
#      （加上退居兜底的卡片版）一支到底；顺带把 `_fit_lines` 的逻辑/物理单位 bug 闸住
#      （那一处单独砍掉过 40% 的标题字数）。同日精简样式时，唯一不跟这一档的新闻条(tv)
#      连同它专用的 font_hl 一起删掉了 —— 闸门现在钉"标题字体只剩这一支"（第 5c 组）。
#
# 为什么不 import server.py：见 test_balloon_layout.py 开头的说明 —— 这里同样只用 ast
# 摘纯函数，不建窗口、不读注册表、不看时钟、不碰网络。
import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server.py")
PAGE = os.path.join(REPO, "static", "index.html")

WANT_FUNCS = ("_balloon_style", "_balloon_pick_style", "_balloon_radius_of", "_ui_cfg")
WANT_CONSTS = ("BALLOON_STYLES", "BALLOON_STYLE_LABEL", "BALLOON_STYLE_LEGACY",
               "BALLOON_BCAST_RADIUS",
               "BALLOON_RADIUS", "BALLOON_ANIM_MS", "BALLOON_ANIM_TICK_MS",
               "UI_DEFAULTS", "_TRAY_CMD_STYLE_BASE",
               # 2026-10-29 标题字号各档共用那一组闸门要用（顺序别乱：TITLE_W 依赖 W/PAD_X）
               "BALLOON_H", "BALLOON_W", "BALLOON_PAD_X", "BALLOON_THUMB", "BALLOON_THUMB_GAP",
               "BALLOON_TEXT_TOP", "BALLOON_TEXT_LINES", "BALLOON_LINE_H",
               "BALLOON_TITLE_PX", "BALLOON_HEAD_PX", "BALLOON_TITLE_W")

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
    """摘出样式收敛那几个纯函数和常量；_ui_cfg 读的 CONFIG 给个空壳。"""
    src = open(SERVER, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"CONFIG": {"ui": {}}}
    grabbed = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.decorator_list \
                and node.name in WANT_FUNCS:
            grabbed[node.name] = node
        elif isinstance(node, ast.Assign):
            for n in _target_names(node):
                if n in WANT_CONSTS:
                    grabbed[n] = node
    done = set()
    for name in WANT_FUNCS + WANT_CONSTS:
        node = grabbed.get(name)
        if node is None:
            FAILS.append("摘取失败，缺: " + name)
            continue
        if id(node) in done:
            continue
        done.add(id(node))
        try:
            exec(compile(ast.Module(body=[node], type_ignores=[]), SERVER, "exec"), ns)
        except Exception as ex:
            FAILS.append("摘取 %s 失败: %s: %s" % (name, type(ex).__name__, ex))
    return ns, src


def segment(src, marker):
    """从 `marker` 那行取到下一个同缩进行的段落（框出某个方法/分支）。

    【2026-10-28 修】缩进原来按 `head` 算：head 必然以换行结尾，那个式子恒等于 0，
    于是"一个方法"其实一路取到下一个顶格的 def —— 整节的类体都算进来了，`count()`
    数得比谁都多（`self._paint_live(` 明明只该出现一次，数出两处，另一处在注释里）。
    按 marker 自己的缩进算，闸门才真的只盯那一个方法。
    """
    i = src.find(marker)
    if i < 0:
        return ""
    indent = len(marker) - len(marker.lstrip())
    lines = src[i:].split("\n")
    out = [lines[0]]
    for ln in lines[1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) <= indent:
            break
        out.append(ln)
    return "\n".join(out)


def func_seg(src, name):
    """取某个顶层函数/带修饰器路由处理器的完整源码。"""
    i = src.find("\ndef " + name + "(")
    if i < 0:
        i = src.find("\nasync def " + name + "(")
    if i < 0:
        return ""
    j = src.find("\ndef ", i + 1)
    k = src.find("\nclass ", i + 1)
    m = src.find("\n@app.", i + 1)
    ends = [x for x in (j, k, m) if x > 0]
    return src[i:min(ends) if ends else len(src)]


ns, SRC = load_fragment()
PAGE_SRC = open(PAGE, encoding="utf-8").read()

STYLES = ns.get("BALLOON_STYLES") or ()
LABEL = ns.get("BALLOON_STYLE_LABEL") or {}

print("── 1. 档位与脏值收敛")
check("三档齐全且顺序固定（30~32 号就是按这个顺序排的）",
      STYLES == ("auto", "live", "spot"), repr(STYLES))
check("「翻牌」已按用户要求撤掉（他说方块式字体效果不好）",
      "flap" not in STYLES and "flap" not in LABEL and "_paint_flap" not in SRC,
      repr(STYLES))
# 【2026-10-29 用户：「播报样式太多，可以精简成三个样式，聚光灯必须留下，你自己衡量」】
# 卡片和新闻条从菜单里下来：卡片≈速递的静态版（标题字号统一后版面已相同），
# 新闻条是唯一一支自己定字号的（版面只有 302 宽）—— 删掉它才有"换样式不换字号"。
check("卡片(card) 与 新闻条(tv) 已从可选项里下来（菜单只剩三档）",
      "card" not in STYLES and "tv" not in STYLES, repr(STYLES))
check("聚光灯必须留着（用户点名）", "spot" in STYLES and LABEL.get("spot") == "聚光灯", repr(LABEL))
check("每档都有中文名（托盘和控制台共用一个词表）",
      all(s in LABEL and LABEL[s].strip() for s in STYLES), repr(LABEL))

_style = ns.get("_balloon_style")
if _style:
    check("空值 / None / 脏值一律回 auto",
          [_style(x) for x in (None, "", "  ", "neon", "SPOT2", 123)]
          == ["auto", "auto", "auto", "auto", "auto", "auto"],
          repr([_style(x) for x in (None, "", "  ", "neon", "SPOT2", 123)]))
    check("大小写不敏感（config.local.json 手改过也不至于回自动）",
          _style("  SPOT ") == "spot", repr(_style("  SPOT ")))
    check("配置里没这个键时读默认 auto（老配置文件直接升级也能开）",
          _style() == "auto", repr(_style()))
    ns["CONFIG"]["ui"]["balloon"] = "tv"
    check("跟着 config.local.json 的 ui.balloon 走（老档位 tv 归位成自动）",
          _style() == "auto", repr(_style()))
    ns["CONFIG"]["ui"]["balloon"] = "card"
    check("老配置里的 card 归位成速递（速递就是同一张版面的动画版，不是改成自动）",
          _style() == "live", repr(_style()))
    _lg = ns.get("BALLOON_STYLE_LEGACY") or {}
    check("归位表只指向还活着的档（不许把老取值归到另一个已下线的档）",
          _lg.get("card") == "live" and _lg.get("tv") == "auto"
          and all(v in STYLES for v in _lg.values()), repr(_lg))
    ns["CONFIG"]["ui"]["balloon"] = "flap"
    check("老配置里存着已撤掉的 flap → 回落 auto（不会画出一张白卡）",
          _style() == "auto", repr(_style()))
    ns["CONFIG"]["ui"]["balloon"] = "auto"
else:
    FAILS.append("_balloon_style 没摘到，后面全跳过")

print("── 2. 自动分流；用户点名的那一档不退（「图片样式，会出现没有图片的样式」）")
_pick = ns.get("_balloon_pick_style")
if _pick:
    check("自动 + 有图 → 聚光灯（视觉最强的给图文稿）",
          _pick("auto", has_img=True) == "spot", _pick("auto", has_img=True))
    check("自动 + 没图 → 速递（打字机在纯文字稿上最稳）",
          _pick("auto", has_img=False) == "live", _pick("auto", has_img=False))
    # 以前这里断言的是 spot 没图 → live，正是用户报的那个现象：题图边播边后台补下，
    # 实测 200 条 100% 带图 URL 但开播那刻缓存只命中 48%，于是"天天在退"，
    # 一组里聚光灯 / 速递来回跳。现在无图由 _paint_spot_backdrop 画一张底，版式不跳。
    check("强制聚光灯 + 没图 → **还是聚光灯**（画自己的底，不许换成别的样式）",
          _pick("spot", has_img=False) == "spot", _pick("spot", has_img=False))
    check("强制聚光灯且有图 → 就是聚光灯", _pick("spot", has_img=True) == "spot", "")
    check("点名那两档都不被分流改写（live/spot 进什么出什么）",
          [_pick(s, has_img=i) for s in ("live", "spot") for i in (True, False)]
          == ["live", "live", "spot", "spot"],
          str([_pick(s, has_img=i) for s in ("live", "spot") for i in (True, False)]))
    # 老取值直接喂进分流函数（GET /api/ui 那条路真的会带 raw 过来）也必须落到活档，
    # 否则界面会拿到一个"已经不存在版式"去画。
    check("已下线的老取值喂进分流也只会得到活档（card→live、tv→auto 再分流）",
          _pick("card", has_img=True) == "live" and _pick("tv", has_img=False) == "live"
          and _pick("tv", has_img=True) == "spot",
          "%s %s %s" % (_pick("card", has_img=True), _pick("tv", has_img=False),
                        _pick("tv", has_img=True)))
    check("默认参数就是自动 + 没图（老调用点不传参也不会炸）", _pick() == "live", _pick())

_rad = ns.get("_balloon_radius_of")
if _rad:
    check("内部兜底那一张（卡片版）圆角还是 WinUI3 的 8",
          _rad("card") == ns.get("BALLOON_RADIUS") == 8, str(_rad("card")))
    check("能选的那三档圆角统一 20（480×176 上才像一朵「通知」）",
          all(_rad(s) == ns.get("BALLOON_BCAST_RADIUS") == 20 for s in STYLES),
          str([_rad(s) for s in STYLES]))

print("── 3. 入口只在右下角托盘菜单，且选完立刻播")
check("默认配置里有 balloon=auto（不选也好看，不用用户先懂设置）",
      (ns.get("UI_DEFAULTS") or {}).get("balloon") == "auto", repr(ns.get("UI_DEFAULTS")))
_ui_post = func_seg(SRC, "api_ui_set")
check("POST /api/ui 收 balloon，且按 BALLOON_STYLES 白名单收（不另抄一份列表）",
      'body.get("balloon")' in _ui_post and "in BALLOON_STYLES" in _ui_post, "")
_ui_get = func_seg(SRC, "api_ui_get")
check("GET /api/ui 把「这一刻实际用的版式」也回出来（排查选了聚光灯怎么出速递）",
      '"balloon": _balloon_pick_style(has_img=_has_img)' in _ui_get, "")
check("没有多余的预览接口（入口只在托盘，选完那一支自己会播）",
      "/api/balloon/preview" not in SRC, "")

_menu = segment(SRC, "    def _build_menu(self):")
_run = segment(SRC, "    def _run_cmd(self, cmd):")
check("托盘菜单有「播报样式」子菜单", 'hsty' in _menu and '"播报样式"' in _menu, "")
check("子菜单按档位表挂（三档全挂上、当前那档带勾）",
      "_TRAY_CMD_STYLE_BASE + i" in _menu and "BALLOON_STYLES" in _menu
      and "_balloon_style()" in _menu and "CheckMenuItem(hsty" in _menu, "")
check("托盘里就是这三档（用户：「播报样式太多，可以精简成三个样式」）",
      len(STYLES) == 3 and LABEL.get("auto") == "自动" and LABEL.get("live") == "速递"
      and LABEL.get("spot") == "聚光灯", repr([LABEL.get(s) for s in STYLES]))
check("选完立刻存 + 立刻播一轮（不看见等于没选）",
      "_save_ui_cfg({\"balloon\": st})" in _run and "_balloon_play_now(" in _run, "")
check("命令号区间按表长算，加档不用再改分支",
      "_TRAY_CMD_STYLE_BASE + len(BALLOON_STYLES)" in _run, "")

print("── 4. 控制台不再挂第二份样式入口（用户指定的位置是右下角菜单）")
check("外观面板里没有样式下拉（两处入口会互相把对方的选择冲掉）",
      "apStyle" not in PAGE_SRC and "ui-balloon" not in PAGE_SRC, "")
check("页面回报 /api/ui 时也不带 balloon（不接管这个键）",
      "balloon" not in PAGE_SRC, "")

print("── 5. 渲染器：分派 + 兜底 + 没有进度条")
# 框出整段"播报版式"源码：从总入口到 _finish_paint 之前
_b0 = SRC.find("    def _paint_bcast(self, hdc, pal, s, g_rects):")
_b1 = SRC.find("    def _finish_paint(", _b0)
_paint = SRC[_b0:_b1] if _b0 >= 0 and _b1 > _b0 else ""
check("_paint_bcast 把能选的两档都分派到了（速递 / 聚光灯）",
      all(("_paint_" + s) in _paint for s in ("live", "spot"))
      and "self.style" in _paint, "")
check("新闻条整档删干净（分派里不再有 tv，方法本身也没了）",
      '"tv"' not in _paint and "_paint_tv" not in _paint
      and segment(SRC, "    def _paint_tv(self, hdc, pal, s, t):") == "",
      _paint[:0])
check("任何一档画崩都退卡片版（宁可难看，不许一片空白）",
      "_paint_fallback" in _paint and "except Exception" in _paint, "")
# 只查标识符，不查中文说明 —— 文档里写着「倒计时条不要」是允许的（那正是在解释为什么没有）
check("播报版式里没有进度条（用户：「倒计时条不要，看着心慌」）",
      not re.search(r"tick_left|tick_total|BALLOON_BAR|progress", _paint), "")
_anim = segment(SRC, "    def _paint_spot(self, hdc, pal, s, t):")
check("聚光灯整卡铺的是原尺寸图（用户：「背景图片太模糊了」）",
      "_draw_thumb(" in _anim and "zoom" in _anim and "ken" in _anim, "")
_timer = segment(SRC, "                elif wp == 5:")
check("动画计时器 5：还在播才续帧，收起就杀掉（不留空转）",
      "KillTimer" in _timer and "InvalidateRect" in _timer
      and "SetTimer(self.hwnd, 5, BALLOON_ANIM_TICK_MS" in SRC,
      "分支里 %s / 开节拍在换条那一处" % ("KillTimer" if "KillTimer" in _timer else "没有 KillTimer"))
# 【2026-10-28 第六轮】新闻条的跑马灯和悬停按钮抢同一条底带：实拍里三份字叠成一团，
# 当时是加 `_band_x2` 让位逻辑救的。**这一档 2026-10-29 整档删了**（用户：「播报样式太多，
# 可以精简成三个样式」），所以闸门跟着换成"别再把那套抢位逻辑写回来"—— 底带上现在只有
# 分页点和两颗胶囊，谁都不需要给谁让位。
_tvsrc = segment(SRC, "    def _paint_tv(self, hdc, pal, s, t):")
check("_band_x2 那套跑马灯让位逻辑已经不需要了（全仓库只剩注释里提它）",
      SRC.count("_band_x2") <= 1, "命中 %d 处" % SRC.count("_band_x2"))
check("底带上不再有第三种字（滚动条自己的「打开 ↗」实心块也随档删除）",
      _tvsrc == "" and "L(414)" not in _paint, _paint[:0])
# 压暗层曾经一帧都没画上去：LinearGradientMode 只有 0~3，写死 4 会让 GdipCreateLineBrushI
# 直接返回 InvalidParameter，而老代码把 None 悄悄吞了 —— 亮底图上白字全糊（用户：
# 「这种模式字体不清楚」）。这条闸门看的就是别再退回"非法 mode + 静默失败"。
_grad = segment(SRC, "    def _gp_grad_brush(self")
check("渐变笔刷传的是合法 LinearGradientMode（0=横 / 1=竖），不是当年那个 4",
      "0 if horizontal else 1" in _grad
      and re.search(r"GdipCreateLineBrushI\([^)]*?,\s*4,", _grad, re.S) is None, "")
check("渐变创建失败要吼一声（静默 None 让压暗层缺了整整一天没人发现）",
      "渐变笔刷创建失败" in _grad and "_grad_warned" in _grad, "")
_spot = segment(SRC, "    def _paint_spot(self, hdc, pal, s, t):")
# 【2026-10-28 用户：「图背景样式还要改进一下，图片都看不清了有些」】上一版救白字救过了头，
# 把标题带压到 226~246/255 —— 字是站住了，图也一起没了，而这档存在的理由本来就是"图好看"。
# 解法是分工：压暗只管最亮那部分，剩下的对比交给**贴着笔画**的 8 向描边。
# 但同日另一句「这种模式字体不清楚」又要求标题那一档压得住原图高光 —— 当天晚上就把底部
# 那一档从 190 提到 212、中段 130 提到 148。所以闸门不能只盯**字面最大数**（那 212 在卡片
# 最底边，圆角里根本没图可看），得把三层压暗按 y 叠起来，算**标题带里实际压了多少**：
# 那儿轻到能看出是图，那儿又要重到白字不用描边硬撑。两头都钉住才算真的定过规则。
def _call_body(seg, from_i):
    """从 `from_i` 那处 `_gp_grad_brush(` 摘出括号里的整串实参（配对括号，不写死形状）。"""
    j = seg.index("(", from_i)
    depth, k = 0, j
    while k < len(seg):
        if seg[k] == "(":
            depth += 1
        elif seg[k] == ")":
            depth -= 1
            if depth == 0:
                break
        k += 1
    return seg[j + 1:k]


def _split_args(body):
    out, depth, cur = [], 0, ""
    for ch in body:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out


def _px(tok, Hd):
    """把 `L(46)` / `Hd` / `0` 这类实参换成**逻辑**像素（L() 就是逻辑 × 缩放，和 Hd 同比，约掉）。"""
    m = re.match(r"^L\((\d+)\)$", tok)
    if m:
        return float(m.group(1))
    if tok in ("Hd", "H"):
        return float(Hd)
    return float(tok) if re.match(r"^\d+$", tok) else None


def _band_alpha(y, y0, y1, alphas, fracs):
    if y1 <= y0 or not alphas or len(alphas) != len(fracs):
        return 0.0
    f = max(0.0, min(1.0, (y - y0) / float(y1 - y0)))
    for i in range(1, len(fracs)):
        if f <= fracs[i]:
            span = (fracs[i] - fracs[i - 1]) or 1.0
            k = (f - fracs[i - 1]) / span
            return alphas[i - 1] + (alphas[i] - alphas[i - 1]) * k
    return float(alphas[-1])


HD = ns.get("BALLOON_H") or 176
_scrim = [int(a) for a in re.findall(r"_argb\(0x060A10,\s*(\d+)\)", _spot)]
_veil = [int(a) for a in re.findall(r"_fill_solid\(self\._argb\(0x060A10,\s*(\d+)\)\)", _spot)]
_grads = []
_i = _spot.find("_gp_grad_brush(")
while _i >= 0:
    _body = _call_body(_spot, _i)
    _args = _split_args(_body)
    if len(_args) >= 6 and "0x060A10" in _args[4]:
        _y0, _y1 = _px(_args[1], HD), _px(_args[3], HD)
        _al = [int(x) for x in re.findall(r"_argb\(0x060A10,\s*(\d+)\)", _args[4])]
        _fr = [float(x) for x in re.findall(r"[\d.]+", _args[5].strip("()"))]
        _grads.append((_y0, _y1, _al, _fr))
    _i = _spot.find("_gp_grad_brush(", _i + 1)


def _scrim_at(y):
    """标题所在那一档 y 上，三层压暗叠完一共遮了多少（0~255）。"""
    tot = (_veil or [0])[0]
    for (y0, y1, al, fr) in _grads:
        tot += _band_alpha(y, y0, y1, al, fr)
    return tot


check("聚光灯的压暗层按字的位置分三块：整卡 veil + 顶部一条 + 下半张一条",
      _spot.count("_fill(") >= 2 and "_fill_solid(" in _spot and len(_grads) == 2,
      "摘到 %d 条渐变，alpha %s" % (len(_grads), _scrim))
check("整卡那层 veil 还是很轻（加厚就等于全卡在涂黑）",
      bool(_veil) and _veil[0] <= 60, "veil=%s" % _veil)
check("标题带里实际压暗 ≤ 175（图的下半截还得看出是图，不是黑底白字）",
      bool(_grads) and _scrim_at(104) <= 175, "标题底 y=104 实测 %.0f" % _scrim_at(104))
check("标题带至少压到 110（「这种模式字体不清楚」那次修复不许悄悄退回去）",
      bool(_grads) and _scrim_at(104) >= 110, "实测 %.0f（原 190 那版太黑 / 更早 46 那版糊字）"
      % _scrim_at(104))
check("卡片正中那一块（图最该看出来的地方）压暗 ≤ 110 —— 这档存在的理由就是图好看",
      bool(_grads) and _scrim_at(70) <= 110, "y=70 实测 %.0f" % _scrim_at(70))
check("最上沿那档 ≤ 200（App 行要能读，但不许糊成一整块黑）",
      bool(_grads) and _scrim_at(0) <= 200, "y=0 实测 %.0f" % _scrim_at(0))
check("最底边那一档停在 212，不再往上（当年 226 / 246 就是把图糊成黑底）",
      max(_scrim) <= 212 and not ({226, 246} & set(_scrim)), "实测 %s" % sorted(_scrim))
_dims = [float(v) for v in re.findall(r"_mix_ref\(white, _ref\(12, 16, 20\),\s*([\d.]+)\)", _spot)]
check("压暗变轻了，时间/来源这两档次要文字就得比上一版更实（混白 ≥ 0.84）",
      len(_dims) == 2 and min(_dims) >= 0.84, "实测 %s（原 0.72/0.78）" % _dims)
check("聚光灯的字都垫一层黑影子 + 8 向描边（底图哪块突然发亮都吃不掉字）",
      "_dt_shadow(" in _spot and "def _dt_shadow" in SRC
      and _spot.count("halo=1") >= 5
      and re.search(r"for dx, dy in \(\(-h, 0\.0\), \(h, 0\.0\)", SRC) is not None,
      "带 halo 的文字行 %d 处" % _spot.count("halo=1"))

print("── 5b. 聚光灯拿不到图就画自己的底，不许换成别的样式（「图片样式，会出现没有图片的样式」）")
# 题图是边播边后台补下的：实测本机池子 200 条 100% 带图 URL，但开播那一刻缓存只命中 95 条（48%）。
# 旧代码两处都会在看不到文件时整条退回速递，于是一组里样式来回跳。
_bd = segment(SRC, "    def _paint_spot_backdrop(self, gr, Wd, Hd, R, pal, e):")
check("有无图专用的那张底（四层渐变，其中一层跟主色走）",
      bool(_bd) and _bd.count("_fillp(") >= 3 and "pal[\"accent\"]" in _bd,
      "渐变层 %d 层" % _bd.count("_fillp("))
check("底是裁在圆角以内的（斜光柱不许糊到卡外），用完把 GDI+ 裁剪收回来",
      "GdipSetClipPath" in _bd and "GdipResetClip" in _bd, "")
# 实拍过一版"只把主色当点缀"的底：主色只以 118/255 叠在深蓝渐变上，再被有图版沿用下来的
# 46/255 全局 veil 和底部 190/255 一压，五档主色全看不出来，整张卡就是一块死黑 —— 用户
# 只会读成"图挂了"。现在主色直接进底渐变和斜向光洗（_mix_ref(acc 两处以上）。
check("主色真的进了底渐变，不只是叠一层薄光洗（无图版不许画成死黑卡）",
      _bd.count("_mix_ref(acc") >= 2 and "_top = _mix_ref(acc, dark, 0.42)" in _bd,
      "主色混色 %d 处" % _bd.count("_mix_ref(acc"))
check("_paint_spot 里没图不再调 _paint_live —— 只剩「GDI+ 起不来」那一条环境性退路",
      _spot.count("self._paint_live(") <= 1
      and "if not _pic and not (self._memdc and self.gdip):" in _spot,
      "退路 %d 处" % _spot.count("self._paint_live("))
check("没图那一版走的还是同一套压暗 + 同一条时间轴（底图/底渐变都用 _e）",
      "_pic = self._draw_thumb(" in _spot
      and "self._paint_spot_backdrop(gr, Wd, Hd, R, pal, _e)" in _spot
      and "_e = _balloon_ease(min(1.0, max(0.0, t / 5200.0)))" in _spot, "")
check("聚光灯画完不提前 return（图和底之后是同一套字位，版式对不齐就是这里断的）",
      _spot.count("return") <= 2, "return %d 处" % _spot.count("return"))
check("auto 判「有没有图」看的是**本机缓存**而不是 URL（这些站每条都带 URL，按 URL 判等于不判）",
      "self.style = _balloon_pick_style(has_img=bool(_balloon_img_local(img)))" in SRC,
      "开播那一行的判据没换成 _balloon_img_local")
check("GET /api/ui 报的 resolved.balloon 用同一条判据（排查时别两套口径）",
      "_balloon_img_local(_cur[4] or \"\")" in func_seg(SRC, "api_ui_get"), "")

print("── 5c. 标题字号各档共用（用户：「这个字体换成，聚光灯式的字体，包括卡片式字体，自动的字体都换成聚光灯式的」）")
# 2026-10-29 这一改有两半：① font_spot 改名 font_title，速递 / 自动 / 聚光灯（还有退居兜底的
# 卡片版）的标题都是它（21.6 逻辑像素 / 700 真粗体）；② 顺手修了 `_fit_lines` 的单位 bug —— `_text_w` 返回
# **物理**像素，而版面宽是**逻辑**像素，两者直接比，等于把每一行都按 1/缩放比（125% 上少 20%）
# 提前砍字。实测 200 条真标题：速递 18/600 那版砍 81 条（40.5%），只修单位就掉到 31 条；
# 再把题图从标题右边挪到底部左角、标题带宽给到 436，字号大一档反而只砍 14 条（7%）。
_fonts = segment(SRC, "            self.font_title = g.CreateFontW(")
check("共用标题字体只有一支（font_title = BALLOON_HEAD_PX / 700 真粗体）",
      "self.font_title = g.CreateFontW(-int(round(BALLOON_HEAD_PX * s)), 0, 0, 0, 700" in SRC
      and "self.font_spot" not in SRC and "self.font_t18" not in SRC and bool(_fonts),
      "HEAD_PX=%s" % ns.get("BALLOON_HEAD_PX"))
check("字号就是一档 21.6（和当年聚光灯那档逐字相同，不是另配一支近似值）",
      ns["BALLOON_HEAD_PX"] == 21.6 and ns["BALLOON_TEXT_LINES"] * ns["BALLOON_LINE_H"] == 60,
      "%s px / %d 行 × %d" % (ns["BALLOON_HEAD_PX"], ns["BALLOON_TEXT_LINES"], ns["BALLOON_LINE_H"]))
_live = segment(SRC, "    def _paint_live(self, hdc, pal, s, t):")
# 卡片档不是另一个方法：WM_PAINT 里播报那三档画完就 return，剩下的那段就是卡片。
# 所以框"卡片路径"要从那句 `if self.style != "card":` 往下取一段，不能去找 if self.style == "card"。
_ci = SRC.find('if self.style != "card":')
_card = SRC[_ci:_ci + 4200] if _ci >= 0 else ""
check("聚光灯 / 速递两档的标题都走 font_title + 整行宽（一处改，两处跟着变）",
      all("self.font_title" in b and "BALLOON_TITLE_W" in b for b in (_spot, _live)),
      "live %d / spot %d 字符" % (len(_live), len(_spot)))
check("卡片档那段也换了同一支 font_title（当年那是 16/600 的 font_msg）",
      "g.SelectObject(hdc, self.font_title)" in _card
      and "SelectObject(hdc, self.font_msg)" not in _card,
      "卡片段 %d 字符" % len(_card))
check("自动那一档就是分流到 速递/聚光灯 之一（没有第三套字号）",
      "self.style = _balloon_pick_style(has_img=bool(_balloon_img_local(img)))" in SRC, "")
check("标题带宽 = 卡宽 - 左右内边距（题图已经挪到底角，不再从标题里切一刀）",
      ns["BALLOON_TITLE_W"] == ns["BALLOON_W"] - 2 * ns["BALLOON_PAD_X"] == 436,
      str(ns["BALLOON_TITLE_W"]))
# 【2026-10-29 精简成三档】新闻条是当时唯一没跟共用字号的一档（标题带只有 302 宽：
# 右边给题图、底边给跑马灯），所以它专用一支 19.2 的 font_hl。整档删掉之后，
# "换样式=换字号"这件事在代码里就再也发生不了了 —— 这条闸门钉的是别哪天又冒第二支标题字体。
check("新闻条专用那支 19.2 的 font_hl 随档删除（标题字体只剩 font_title 一支 + 兜底 font_msg）",
      "self.font_hl" not in SRC and re.search(r"19\.2 \* s", SRC) is None
      and SRC.count("= g.CreateFontW(-int(round(BALLOON_HEAD_PX * s)") == 1,
      "19.2 残留: %s" % (re.search(r"19\.2 \* s", SRC) is not None))
check("兜底版式小一号（挤不下时退字号，不退版式）",
      "self.font_msg" in segment(SRC, "    def _paint_fallback(")
      and ns["BALLOON_TITLE_PX"] < ns["BALLOON_HEAD_PX"],
      "%s vs %s" % (ns["BALLOON_TITLE_PX"], ns["BALLOON_HEAD_PX"]))
_fit = segment(SRC, "    def _fit_lines(self, hdc, text, font, maxw, n=2):")
check("★ _fit_lines 把版面宽换算成物理像素再量字（单位不一致那次砍掉 40% 的标题）",
      "box = maxw * (self.scale or 1.0)" in _fit and "box" in _fit,
      "段长 %d" % len(_fit))
check("换算只做一次：之后三处比的是同一个 box，没有一处还直接拿 maxw 比",
      _fit.count("box") >= 3 and len(re.findall(r"<[=]?\s*maxw|>\s*=?\s*maxw", _fit)) == 0,
      "box %d 次 / 直接比 maxw %d 处"
      % (_fit.count("box"), len(re.findall(r"<[=]?\s*maxw|>\s*=?\s*maxw", _fit))))
check("裁切遮罩跟着新版面宽走（速递那两行是从遮罩里推上来的，写死旧宽度就推不满一行）",
      _live.count("L(22 + BALLOON_TITLE_W * frac)") >= 1
      and _live.count("BALLOON_TITLE_W") >= 3,
      "BALLOON_TITLE_W %d 处" % _live.count("BALLOON_TITLE_W"))
check("速递的来源行让到底角题图右边（和 _balloon_rects 那份几何同一口径）",
      "22 + BALLOON_THUMB + BALLOON_THUMB_GAP" in _live, "")
check("速递的题图也挪到底部左角（和卡片/聚光灯共用那一条底带）",
      "BALLOON_THUMB_TOP" in _live and "_draw_thumb(" in _live, "")

print("── 6. 用户主动开口时不等上一组播完（「切换样式模式，播报暂停了一分钟」）")
# 一组是 10 条 × 5.6 秒 = 56 秒，队列旧代码只在"没在播"时取下一组 —— 于是托盘点了样式，
# 气泡还在那儿慢慢播上一组，用户看到的就是"点了没反应，一分钟后才换"。
_toast = func_seg(SRC, "_show_toast_list")
check("入队只留标记，不在调用方线程动窗口（interrupt 在 BALLOON.lock 里置位）",
      "def _show_toast_list(entries, replace=False):" in _toast
      and "with BALLOON.lock:" in _toast
      and "if replace:\n                BALLOON.interrupt = True" in _toast, "")
check("掐的动作在气泡自己的消息线程里做（调用方只留标记，不跨线程动窗口和计时器）",
      "KillTimer" not in _toast and "ShowWindow" not in _toast, "")
_poll = segment(SRC, "                if wp == 1:")
check("轮询取队列：没在播 **或** 有人开口，就取下一组",
      "if self.q and (not self.showing or _ip):" in _poll and "_ip = self.interrupt" in _poll, "")
check("取到且还在播 → 本线程 _dismiss 掐掉旧的，drop_pending=False（队列后面那组留着）",
      "self._dismiss(hwnd, drop_pending=False)" in _poll, "")
check("标记用完就清（不置位就永远每 300 毫秒掐一次）",
      "self.interrupt = False" in _poll, "")
check("自动那一轮不传 replace —— 正看着的东西被后台刷新顶掉，比等半分钟更烦人",
      re.findall(r"_show_toast_list\([^\n]*replace=True", SRC) == []
      and "_show_toast_list(entries, replace=force)" in func_seg(SRC, "_notify_news"),
      repr(re.findall(r"_show_toast_list\([^\n]*replace[^\n]*", SRC)))
check("三条「人开口」的路都走 force：托盘样式 / 立即轮播 / 双击图标",
      "_balloon_play_now(\"托盘菜单\")" in SRC and "_balloon_play_now(\"播报样式\")" in SRC
      and "_balloon_play_now(\"双击托盘\")" in SRC, "")

print()
if FAILS:
    print("失败 %d 条:" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("全部通过")
