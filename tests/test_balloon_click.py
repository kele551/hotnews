# -*- coding: utf-8 -*-
# 作者: 海风（kele551）
# 用途: 右下角自绘气泡「打开 / 退出」两个按钮的点击判定单测（2026-10-28）。
#
# 盯的是用户反馈的真 bug：「气泡弹出的窗口，有打开和退出两个按键，退出键不起作用，鼠标一直转」。
# 根因两条，都在这个文件里有闸：
#   1) WM_LBUTTONDOWN 的坐标取错了 —— wParam 是按键状态位（点左键就是 1），x/y 全在 lParam
#      的低 16 位 / 高 16 位里。原来命中判断写成 `r[0] <= wp <= r[2]`，等于拿"1"去比按钮的
#      x 区间（卡片宽 400×缩放，两个胶囊在右侧 x≈330~386），永远判不中 ——
#      「退出」于是也走了「打开」那条路，看上去就是"这个键按了没用"。
#   2) 开网页是在气泡自己的消息线程里同步做的（ShellExecuteW 要等浏览器起来），那几秒
#      线程不在消息循环里待着，Windows 判定窗口没响应 → 鼠标挂转圈。现在改后台线程。
#
# 为什么不 import server.py：那是八千多行、模块级要建 FastAPI 应用、读配置、起缓存目录。
# 这里用 ast 只把 `_Balloon._wndproc` / `_Balloon._dismiss` 和 `_open_app_later` 摘出来单独
# exec，配一个假的 user32/gdi32 记录调用 —— 全程不建窗口、不碰网络、不看时钟，结果可复现。
# （真窗口那一路另有本机脚本验：E:\Qdoer\tmp\ossbuild\verify_bubble.py，用真的
#   PostMessageW 投 WM_MOUSEMOVE / WM_LBUTTONDOWN，21 条断言全过。）
import ast
import ctypes
import os
import re
import sys
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SERVER = os.path.join(REPO, "server.py")

WM_LBUTTONDOWN = 0x0201
WM_SETCURSOR = 0x0020
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
MK_LBUTTON = 0x0001

FAILS = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("[ok]" if ok else "[!!]", name, ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(name + (" — " + detail if detail else ""))


def load():
    src = open(SERVER, encoding="utf-8").read()
    tree = ast.parse(src)
    return src, tree


def class_methods(tree, cls):
    for n in tree.body:
        if isinstance(n, ast.ClassDef) and n.name == cls:
            return {m.name: m for m in n.body if isinstance(m, ast.FunctionDef)}
    return {}


def top_func(tree, name):
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


def seg(text, start, stop):
    """截一段函数体：从含 start 的那行到含 stop 的那行之前。"""
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(stop, i + len(start))
    return text[i:j if j > 0 else len(text)]


def code_of(fn):
    """把方法还原成**纯代码**文本。

    为什么不用源码字符串：这一轮的注释里专门写了"命中判断原来写成 `r[0] <= wp <= r[2]`"，
    拿源码做"不许出现 wp"这种文字闸会把自己的注释当成违规抓出来。ast.unparse 去注释，
    顺带把 `0xFFFF` 渲染成十进制 65535、把 `(lp >> 16) & 0xFFFF` 的多余括号去掉
    —— 所以下面的正则两种写法都认。
    """
    return ast.unparse(fn) if fn is not None else ""


def branch(fn, const):
    """取方法里 `if msg == const:` 那一段的代码。"""
    if fn is None:
        return ""
    for n in ast.walk(fn):
        if isinstance(n, ast.If) and any(
                isinstance(c, ast.Constant) and c.value == const for c in ast.walk(n.test)):
            return ast.unparse(n)
    return ""


def pack(x, y):
    """Win32 的 lParam：低 16 位 x、高 16 位 y（负数走补码）。"""
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


# ── 假的 user32 / gdi32：只记录调用，什么都不真干 ──
class Rec:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _f(*a):
            self.calls.append((name,) + tuple(a))
            return 0
        return _f

    def names(self):
        return [c[0] for c in self.calls]

    def reset(self):
        self.calls = []


class FakeSelf:
    """把 _Balloon 需要的状态凑出来：不建窗口，只喂消息。"""

    def __init__(self, meth, opens):
        self.ctypes, self.wintypes = ctypes, wintypes
        self.u, self.g, self.k = Rec(), Rec(), Rec()
        self.scale = 1.0
        self.cur = ("【头条】某条", "正文", False, "https://example.com/news")
        self.hover = False
        self.paused = False
        self.showing = True
        self._tracking = False
        # 【2026-10-28】一组三条，用来验「点打开 → 剩下的接着播」（原来只放一条，
        # 一点就整组收掉，那个 bug 正是这么漏过去的）。
        self.playlist = [("a", "b", False, "https://example.com/news"),
                         ("c", "d", False, "https://example.com/news2"),
                         ("e", "f", False, "")]
        self.idx = 0
        self.q = [[("x", "y", False, "")], [("p", "q", False, "")]]   # 还排着两组
        self.lock = __import__("threading").Lock()
        self.cur_arrow = 777                       # LoadCursorW(None, 32512) 的句柄桩
        # 版式（逻辑像素、scale=1），和 _balloon_rects() 同一份：
        # 【2026-10-28 第五轮】按 WinUI3 样张，关闭在左、打开贴最右。
        self._btn_close = (296, 112, 354, 138)
        self._btn_open = (362, 112, 420, 138)
        self.hwnd = 1234
        self.x, self.y, self.W, self.H = 0, 0, 440, 152
        self.winW, self.winH = 440, 152            # 分层窗口尺寸（显示时要传给 SetWindowPos）
        self.M = 0                                 # 分层窗口的投影留白（这里不画，取 0 省换算）
        self.placed = []                           # _place() 被叫了几次
        self.applied = []                          # _apply_current() 被叫了几次
        for name in ("_wndproc", "_dismiss", "_hide_and_resume", "_resume_rest",
                     "_mouse_leave"):
            setattr(self, name, meth[name].__get__(self, FakeSelf))
        self.opens = opens
        # 这两支只碰窗口几何/播放下标，另有实机脚本验；这里给记录桩，免得牵进 DrawText
        self._place = lambda: self.placed.append(1)
        self._apply_current = lambda: self.applied.append(self.idx)
        self.RECT = wintypes.RECT                  # 真 ctypes 结构（计时器 5 要 byref 它）

    # WM_PAINT 才用得到的东西，这里一律不给（测的就是"不画也不会崩"）


def build_namespace(opens):
    """exec 摘出来的几个方法 + 一个开网页记录桩。"""
    src, tree = load()
    ns = {"print": print, "ctypes": ctypes, "wintypes": wintypes,
          "threading": __import__("threading"), "webbrowser": __import__("webbrowser"),
          "APP_URL": "http://localhost:8000", "BALLOON_HEADLINE_TAG": "【头条】",
          "BALLOON_PER_ITEM_MS": 5600, "BALLOON_RESUME_MS": 8000,
          "BALLOON_W": 440, "BALLOON_H": 152,
          # 真几何在 tests/test_balloon_layout.py 里逐个矩形断言；这里不画，只测消息处理
          "_balloon_rects": lambda w=440, h=152, total=1, idx=0: {},
          "_open_app_later": lambda url=None: opens.append(url)}
    meth = class_methods(tree, "_Balloon")
    want = ("_wndproc", "_dismiss", "_hide_and_resume", "_resume_rest", "_mouse_leave")
    for name in want:
        if name not in meth:
            FAILS.append("_Balloon 里找不到方法 " + name)
            return None, None, src
        exec(compile(ast.Module(body=[meth[name]], type_ignores=[]), SERVER, "exec"), ns)
    return {n: ns[n] for n in want}, None, src


def main():
    opens = []
    meth, _, src = build_namespace(opens)
    if meth is None:
        return finish()
    wndproc, dismiss = meth["_wndproc"], meth["_dismiss"]
    src, tree = load()
    balloon = class_methods(tree, "_Balloon")
    body = code_of(balloon.get("_wndproc"))          # 去注释的整个消息处理
    click = branch(balloon.get("_wndproc"), 0x0201)  # WM_LBUTTONDOWN 那一段
    print("摘到 _Balloon._wndproc / _dismiss，喂真消息跑一遍：")

    # ── 1. 结构：坐标必须从 lParam 里解出来 ──
    check("WM_LBUTTONDOWN 那段还在（没被顺手删掉）", len(click) > 200, "%d 字" % len(click))
    check("x 从 lParam 低 16 位取，且按 short 还原符号",
          bool(re.search(r"c_short\(\s*lp & (0xFFFF|65535)\s*\)", click)))
    check("y 从 lParam 高 16 位取，且按 short 还原符号",
          bool(re.search(r"c_short\(\s*\(?\s*lp >> 16\s*\)?\s*& (0xFFFF|65535)\s*\)", click)))
    check("命中判断不再用 wParam（那条状态位当坐标是这次的真 bug）",
          "wp" not in click, "这段里出现 wp = %r" % ("wp" in click,))

    # ── 1b. 转圈的第二层：WM_SETCURSOR 要把光标按回箭头 ──
    # 【2026-10-28 用户第二次反馈「鼠标指向打开和退出，鼠标一直转圈圈」】
    # 光把 ShellExecuteW 挪出消息线程还不够：等待光标一旦盖到本进程窗口上，
    # 只有我们自己处理 WM_SETCURSOR 才按得回去。
    sc = branch(balloon.get("_wndproc"), 0x0020)
    check("WM_SETCURSOR（0x0020）有单独分支", len(sc) > 30, "%d 字" % len(sc))
    check("该分支设的是箭头光标并 return 1（TRUE，交给 DefWindowProc 就会继续转）",
          "SetCursor(self.cur_arrow)" in sc and "return 1" in sc,
          sc.replace("\n", " ")[:110])
    fs = FakeSelf(meth, opens)
    opens.clear()
    _r = fs._wndproc(1234, WM_SETCURSOR, 1234, 0)
    check("喂一条 WM_SETCURSOR 真的调到 SetCursor 且返回非 0",
          "SetCursor" in fs.u.names() and _r == 1,
          "返回=%r 调用=%r" % (_r, fs.u.names()))
    check("窗类注册时把 hCursor 也设成箭头（不注册默认光标就得靠兜底）",
          "wc.hCursor = self.cur_arrow" in code_of(balloon.get("_thread")))
    check("LoadCursorW/SetCursor 声明了原型（x64 句柄不声明会被截断）",
          "u.LoadCursorW.restype = wintypes.HANDLE" in code_of(balloon.get("__init__"))
          and "u.SetCursor.argtypes" in code_of(balloon.get("__init__")))
    _ic = code_of(balloon.get("__init__"))
    check("取箭头光标这行排在 restype 声明**之后**（先取后声明 → 句柄被截成 32 位，SetCursor 直接失效）",
          _ic.index("u.LoadCursorW.restype") < _ic.index("self.cur_arrow = u.LoadCursorW"),
          "restype@%d cur_arrow@%d" % (_ic.index("u.LoadCursorW.restype"),
                                       _ic.index("self.cur_arrow = u.LoadCursorW")))
    check("取箭头光标兜底：__init__ 顶部先给 cur_arrow=None（初始化失败时 _wndproc 判空才不会崩）",
          "self.cur_arrow = None" in _ic)

    # ── 2. 行为：点「退出」= 只收起，绝不开网页 ──
    fs = FakeSelf(meth, opens)
    opens.clear()
    fs._wndproc(1234, WM_LBUTTONDOWN, MK_LBUTTON, pack(325, 125))   # 关闭按钮中心（最左那颗）
    closes = [c for c in fs.u.calls if c[0] == "ShowWindow"]
    check("点「退出」→ 窗口隐掉（SW_HIDE=0）", bool(closes) and closes[-1][2] == 0,
          "ShowWindow=%s" % (repr(closes[-1][1:]) if closes else "没调",))
    check("点「退出」没有去开任何东西", opens == [], "opens=%r" % (opens,))
    check("点「退出」把排在后面的两组也丢掉（不然 300 毫秒后又弹出来）", fs.q == [],
          "q=%r" % (fs.q,))
    check("点「关闭」把四个计时器都杀了（2 停留 / 3 轮换 / 4 打开后唤醒 / 5 播报动画）",
          sorted([c[2] for c in fs.u.calls if c[0] == "KillTimer"]) == [2, 3, 4, 5],
          "KillTimer=%r" % ([c[2] for c in fs.u.calls if c[0] == "KillTimer"],))

    # ── 3. 行为：点「打开」/点正文 = 开当前那条，且不在消息线程里开 ──
    fs = FakeSelf(meth, opens)
    opens.clear()
    fs._wndproc(1234, WM_LBUTTONDOWN, MK_LBUTTON, pack(391, 125))   # 打开按钮中心（贴最右那颗）
    check("点「打开」开的是当前那条的原文链接", opens == ["https://example.com/news"],
          "opens=%r" % (opens,))
    fs = FakeSelf(meth, opens)
    opens.clear()
    fs._wndproc(1234, WM_LBUTTONDOWN, MK_LBUTTON, pack(60, 70))
    check("点卡片正文也开这一条（README 定的口径）", opens == ["https://example.com/news"],
          "opens=%r" % (opens,))

    # ── 3b. 点「打开」不许把这一组剩下的吞掉（用户：「进网页后轮播就停了」）──
    fs = FakeSelf(meth, opens)
    fs.idx = 1                                   # 播到第 2 条（共 3 条）时点开
    fs.playlist = fs.playlist[:]                 # 一份独立列表
    opens.clear()
    fs._wndproc(1234, WM_LBUTTONDOWN, MK_LBUTTON, pack(391, 125))
    check("点开第 2 条 → 已看过的两条摘掉、只留剩下那条", fs.playlist == [("e", "f", False, "")],
          "playlist=%r" % (fs.playlist,))
    check("点开之后 showing 仍是 True（不然队列轮询会趁 8 秒空档顶下一组上来）",
          fs.showing is True, "showing=%r" % (fs.showing,))
    check("点开之后架的是计时器 4（BALLOON_RESUME_MS），不是整组收起",
          [c[2] for c in fs.u.calls if c[0] == "SetTimer"] == [4]
          and [c[3] for c in fs.u.calls if c[0] == "SetTimer"] == [8000],
          "SetTimer=%r" % ([c[1:] for c in fs.u.calls if c[0] == "SetTimer"],))
    check("屏幕先让给浏览器（ShowWindow SW_HIDE）", any(c[0] == "ShowWindow" and c[2] == 0
                                                  for c in fs.u.calls), "")
    check("「打开」不清排队里的下一组", len(fs.q) == 2, "q=%d 组" % len(fs.q))

    # 点的正好是最后一条 → 剩下的空了，老实整组收起
    fs = FakeSelf(meth, opens)
    fs.idx = 2
    opens.clear()
    fs._wndproc(1234, WM_LBUTTONDOWN, MK_LBUTTON, pack(391, 125))
    check("点开的正好是最后一条 → 整组收起（不留一个空壳窗口）",
          any(c[0] == "ShowWindow" and c[2] == 0 for c in fs.u.calls)
          and [c[2] for c in fs.u.calls if c[0] == "SetTimer"] == [],
          "SetTimer=%r" % ([c[1:] for c in fs.u.calls if c[0] == "SetTimer"],))

    # 计时器 4 到点 → 剩下那几条接着播
    fs = FakeSelf(meth, opens)
    fs.playlist = [("c", "d", False, ""), ("e", "f", False, "")]
    fs._wndproc(1234, WM_TIMER, 4, 0)
    check("计时器 4：先杀掉自己（重复点不许排两个唤醒）",
          [c[2] for c in fs.u.calls if c[0] == "KillTimer"] == [4],
          "KillTimer=%r" % ([c[2] for c in fs.u.calls if c[0] == "KillTimer"],))
    check("计时器 4：把剩下那组重新亮出来（SW_SHOWNOACTIVATE=4，不抢焦点）",
          any(c[0] == "ShowWindow" and c[2] == 4 for c in fs.u.calls), "")
    check("计时器 4：重新架起停留/轮换计时器（2 条 → 停留 2×5600、轮换 5600）",
          [c[2] for c in fs.u.calls if c[0] == "SetTimer"] == [2, 3]
          and [c[3] for c in fs.u.calls if c[0] == "SetTimer"] == [5600 * 2, 5600],
          "SetTimer=%r" % ([c[1:] for c in fs.u.calls if c[0] == "SetTimer"],))
    check("计时器 4：不再有倒计时条那支计时器（用户：「倒计时条不要」）",
          [c[2] for c in fs.u.calls if c[0] == "SetTimer"].count(5) == 0, "")
    check("计时器 4：调 _apply_current 换成本组第一条", fs.applied == [0], str(fs.applied))

    # 悬停/移开：自动关闭的停留计时器跟着冻住与继续
    fs = FakeSelf(meth, opens)
    fs._wndproc(1234, 0x0200, 0, 0)              # WM_MOUSEMOVE
    check("鼠标移上去：停留/轮换计时器被杀（悬停 = 暂停，来不及点也走得开）",
          {2, 3} <= {c[2] for c in fs.u.calls if c[0] == "KillTimer"}
          and fs.paused is True, "KillTimer=%r" % ([c[2] for c in fs.u.calls if c[0] == "KillTimer"],))
    fs = FakeSelf(meth, opens)
    fs.hover = True
    fs.paused = True
    fs._wndproc(1234, 0x02A3, 0, 0)              # WM_MOUSELEAVE
    check("鼠标移开：停留/轮换计时器重新架上、paused 放掉",
          {2, 3} <= {c[2] for c in fs.u.calls if c[0] == "SetTimer"}
          and fs.paused is False, "SetTimer=%r" % ([c[1:] for c in fs.u.calls if c[0] == "SetTimer"],))

    # ── 4. 没有链接的纯提示回落到首页 ──
    fs = FakeSelf(meth, opens)
    fs.cur = ("我的软件发布", "只有一条提示", True, "")
    opens.clear()
    fs._wndproc(1234, WM_LBUTTONDOWN, MK_LBUTTON, pack(60, 70))
    check("没有链接时回落 APP_URL", opens == ["http://localhost:8000"], "opens=%r" % (opens,))

    # ── 5. 气泡的消息线程里不许出现同步开网页的调用 ──
    check("气泡 _wndproc 里没有直接 ShellExecuteW（那几秒会把鼠标冻成转圈）",
          "ShellExecuteW" not in body)
    check("气泡 _wndproc 里没有直接 webbrowser.open", "webbrowser.open" not in body)
    check("开网页走 _open_app_later", "_open_app_later(" in click)
    lat = code_of(top_func(tree, "_open_app_later"))
    check("_open_app_later 确实起了后台线程",
          "threading.Thread" in lat and "target=_open_app_window" in lat and "daemon=True" in lat,
          lat.replace("\n", " ")[:120])

    # ── 6. 托盘那两条消息回调也不能在消息线程里同步开网页 ──
    tray = class_methods(tree, "_Tray")
    tbody = "\n".join(code_of(m) for m in tray.values())
    check("托盘（点气泡/右键菜单）改成了 _open_app_later",
          "_open_app_window()" not in tbody and "_open_app_later" in tbody)
    check("_open_app_window 收 url 参数、空值回落首页",
          "url = url or APP_URL" in code_of(top_func(tree, "_open_app_window")))

    # ── 7. 两条路的收尾各有唯一出处 ──
    dm = code_of(balloon.get("_dismiss"))
    check("收尾动作收拢在 _dismiss 里（隐窗口 + 杀计时器）",
          "ShowWindow" in dm and "KillTimer" in dm)
    check("_dismiss 的 drop_pending 才清队列（打开那条路不清，退出才清）",
          "if drop_pending" in dm and "self.q" in dm)
    check("「退出」调 _dismiss(drop_pending=True)（原来两条路各写一遍、都不全）",
          "_dismiss(hwnd, drop_pending=True)" in click, "调用 %d 次" % click.count("_dismiss("))
    hr = code_of(balloon.get("_hide_and_resume"))
    check("「打开」调 _hide_and_resume（只摘掉看过那条，剩下的接着播）",
          "_hide_and_resume(hwnd)" in click and "_hide_and_resume" in body, "")
    check("_hide_and_resume：杀 2/3/4（重复点不排两个唤醒），架计时器 4",
          all(k in hr for k in ("KillTimer(hwnd, 2)", "KillTimer(hwnd, 3)",
                                "KillTimer(hwnd, 4)",
                                "SetTimer(hwnd, 4, BALLOON_RESUME_MS")))
    check("_hide_and_resume：剩下空了就老实整组收起（调 _dismiss）",
          "_dismiss(hwnd)" in hr and "if not rest" in hr)
    rs = code_of(balloon.get("_resume_rest"))
    check("_resume_rest：亮窗口 + 重架 2/3（没有倒计时条那支了）",
          all(k in rs for k in ("ShowWindow", "SetTimer(hwnd, 2", "SetTimer(hwnd, 3"))
          and "_arm_item_clock" not in rs)
    check("_resume_rest 也走 _apply_current（换成本组第一条）",
          "_apply_current()" in rs and "_place()" in rs)

    # ── 8. 按钮判定区别拿原始像素硬卡（125%/150% 屏上小胶囊不好瞄） ──
    check("命中判定按缩放放宽了一点边（3px × scale）",
          "_pad = int(3 * self.scale)" in click and "_pad" in click)

    # ── 9. 文案红线：这一段不许出现对外敏感词 ──
    hit = [w for w in ("绕过", "翻墙", "梯子", "科学上网", "破解") if w in click]
    check("点击那段代码里没有红线词", hit == [], "hit=%r" % (hit,))

    print("")
    return finish()


def finish():
    if FAILS:
        print("失败 %d 条:" % len(FAILS))
        for f in FAILS:
            print("  - " + f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
