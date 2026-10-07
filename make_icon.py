# -*- coding: utf-8 -*-
"""生成 hotnews 图标：参考经典「NEWS 报纸」造型，做**镂空 + 立体感**版。

用户要求：
  · 参考那张玻璃质感的新报图标（NEWS 红字 + 黑色文字块/线条）
  · **镂空** —— 不要一块实心方块，去掉方底，图标浮在透明背景上，四周留白
  · **有立体感** —— 堆叠的纸页边缘、整体投影、斜面高光、折角

做法：1024x1024 上精细绘制（投影/高光/斜面都靠多图层 + 高斯模糊），
再用 LANCZOS 逐级降采样出 256/128/64/48/32/24/16，合成多尺寸 ICO。
小尺寸不会糊，也不会在大尺寸下显得糙。
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

S = 1024
OUT_ICO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hotnews.ico")
SIZES = [256, 128, 64, 48, 32, 24, 16]

PAPER_TOP = (255, 255, 255)
PAPER_BOT = (222, 231, 244)
EDGE_LIGHT = (255, 255, 255)
EDGE_DARK = (150, 168, 194)
NEWS_RED = (196, 22, 30)
INK = (38, 44, 54)
GREY_LINE = (120, 132, 150)

FONT_BOLD = r"C:\Windows\Fonts\arialbd.ttf"
FONT_FALLBACK = r"C:\Windows\Fonts\segoeuib.ttf"


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def vgrad(size, c1, c2):
    w, h = size
    img = Image.new("RGB", size, c1)
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        row = lerp(c1, c2, t)
        for x in range(w):
            px[x, y] = row
    return img


def rmask(size, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1],
                                        radius=radius, fill=255)
    return m


def paste_grad(base, box, radius, c1, c2):
    x, y, w, h = box
    g = vgrad((w, h), c1, c2)
    p = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    p.paste(g, (0, 0), rmask((w, h), radius))
    base.alpha_composite(p, (x, y))
    return base


def main():
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    # 纸张几何：主体略偏下，右上方露出堆叠的纸页边
    pw, ph = int(S * 0.72), int(S * 0.80)          # 主纸尺寸
    px0, py0 = int(S * 0.145), int(S * 0.115)      # 主纸左上
    radius = int(S * 0.045)

    # ---------- 1) 极淡的中性投影 ----------
    # 【2026-10-07 用户反馈「图标不要有黑色的透明壳」】
    # 原来画的是深蓝黑投影（RGB 12,30,58 且 alpha 高），在深色桌面/深色主题下
    # 会变成一圈脏兮兮的黑边 —— 看着就像图标外面套了个黑色透明壳。
    # 现在改成**很淡的中性灰**，只留一点点"离开桌面"的感觉，绝不再发黑。
    shadow = Image.new("L", (S, S), 0)
    sd = ImageDraw.Draw(shadow)
    for i, off in enumerate(((6, 10), (10, 16))):
        sd.rounded_rectangle([px0 + off[0], py0 + off[1],
                              px0 + pw + off[0], py0 + ph + off[1]],
                             radius=radius, fill=34 - i * 14)
    shadow = shadow.filter(ImageFilter.GaussianBlur(int(S * 0.016)))
    canvas = Image.alpha_composite(canvas, Image.merge("RGBA", (
        Image.new("L", (S, S), 120), Image.new("L", (S, S), 124),
        Image.new("L", (S, S), 130), shadow)))

    # ---------- 2) 堆叠的纸页边（后方两层，错位）----------
    for k, (dx, dy, tone) in enumerate((
            (int(S * 0.055), -int(S * 0.052), (232, 238, 248)),
            (int(S * 0.028), -int(S * 0.026), (242, 246, 252)))):
        ex, ey = px0 + dx, py0 + dy
        canvas = paste_grad(canvas, (ex, ey, pw, ph), radius,
                            tone, lerp(tone, EDGE_DARK, 0.35))
        # 页边描边，让两层分得开
        d = ImageDraw.Draw(canvas)
        d.rounded_rectangle([ex, ey, ex + pw, ey + ph], radius=radius,
                            outline=lerp(EDGE_DARK, (255, 255, 255), 0.35) + (200,), width=max(2, S // 320))

    # ---------- 3) 主纸（浅渐变 + 斜面）----------
    canvas = paste_grad(canvas, (px0, py0, pw, ph), radius, PAPER_TOP, PAPER_BOT)
    # 浅灰细描边：保证在**深色桌面**上也有清晰边界（用户要求"不要黑色壳"，那就用浅色描边）
    _rim = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(_rim).rounded_rectangle(
        [px0, py0, px0 + pw, py0 + ph], radius=radius,
        outline=(176, 186, 202, 235), width=max(3, S // 220))
    _rim = _rim.filter(ImageFilter.GaussianBlur(S // 700))
    canvas = Image.alpha_composite(canvas, _rim)

    # 斜面：左上亮边、右下暗边
    bevel = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bevel)
    bd.rounded_rectangle([px0, py0, px0 + pw, py0 + ph], radius=radius,
                         outline=(255, 255, 255, 210), width=max(3, S // 190))
    bevel = bevel.filter(ImageFilter.GaussianBlur(S // 420))
    canvas = Image.alpha_composite(canvas, bevel)

    dark = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dark)
    dd.rounded_rectangle([px0 + S // 300, py0 + S // 300,
                          px0 + pw + S // 300, py0 + ph + S // 300],
                         radius=radius, outline=EDGE_DARK + (150,),
                         width=max(3, S // 200))
    dark = dark.filter(ImageFilter.GaussianBlur(S // 500))
    # 只保留右下侧（用遮罩裁掉左上）
    cut = Image.new("L", (S, S), 0)
    ImageDraw.Draw(cut).polygon([(px0 + pw, py0 - 40), (px0 + pw + 60, py0 - 40),
                                 (px0 + pw + 60, py0 + ph + 60), (px0 - 40, py0 + ph + 60)],
                                fill=255)
    dark.putalpha(Image.composite(dark.getchannel("A"), Image.new("L", (S, S), 0), cut))
    canvas = Image.alpha_composite(canvas, dark)

    # ---------- 4) 玻璃高光（斜向扫过右上）----------
    gloss = Image.new("L", (S, S), 0)
    gd = ImageDraw.Draw(gloss)
    gd.polygon([(px0 + pw * 0.02, py0 + ph * 0.02),
                (px0 + pw * 0.98, py0 + ph * 0.02),
                (px0 + pw * 0.98, py0 + ph * 0.30),
                (px0 + pw * 0.02, py0 + ph * 0.44)], fill=96)
    gloss = gloss.filter(ImageFilter.GaussianBlur(int(S * 0.018)))
    gloss = Image.composite(gloss, Image.new("L", (S, S), 0),
                            rmask((S, S), radius)) if False else gloss
    # 裁到主纸范围内
    paper_mask = Image.new("L", (S, S), 0)
    paper_mask.paste(rmask((pw, ph), radius), (px0, py0))
    gloss = Image.composite(gloss, Image.new("L", (S, S), 0), paper_mask)
    canvas = Image.alpha_composite(canvas, Image.merge("RGBA", (
        Image.new("L", (S, S), 255), Image.new("L", (S, S), 255),
        Image.new("L", (S, S), 255), gloss)))

    # ---------- 5) 内容：NEWS 红字 + 文字块与线条 ----------
    d = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(FONT_BOLD, int(S * 0.185))
    except Exception:
        font = ImageFont.truetype(FONT_FALLBACK, int(S * 0.185))

    text = "NEWS"
    tb = d.textbbox((0, 0), text, font=font)
    tx = px0 + (pw - (tb[2] - tb[0])) // 2
    ty = py0 + int(ph * 0.085)
    # 字的投影，增加厚度
    d.text((tx + S // 300, ty + S // 300), text, font=font, fill=(120, 120, 130, 130))
    d.text((tx, ty), text, font=font, fill=NEWS_RED)

    # NEWS 下的分隔线
    ly = ty + (tb[3] - tb[1]) + int(S * 0.030)
    d.rounded_rectangle([px0 + int(pw * 0.075), ly,
                         px0 + pw - int(pw * 0.075), ly + int(S * 0.013)],
                        radius=int(S * 0.007), fill=INK)

    # 左侧文字线
    cx = px0 + int(pw * 0.075)
    cw = int(pw * 0.52)
    y = ly + int(S * 0.048)
    for i, ratio in enumerate((1.0, 0.92, 0.78, 0.88, 0.66)):
        h = int(S * 0.030)
        d.rounded_rectangle([cx, y, cx + int(cw * ratio), y + h],
                            radius=h // 2, fill=GREY_LINE)
        y += h + int(S * 0.030)

    # 右侧黑色「图片块」
    bx = px0 + int(pw * 0.635)
    bw = int(pw * 0.29)
    bh = int(ph * 0.255)
    by = ly + int(S * 0.052)
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=int(S * 0.016), fill=INK)
    # 图片块里两笔浅色横线，别显得空
    for i in range(2):
        yy = by + int(bh * (0.36 + i * 0.30))
        d.rounded_rectangle([bx + int(bw * 0.16), yy,
                             bx + int(bw * (0.80 - i * 0.22)), yy + int(S * 0.020)],
                            radius=int(S * 0.010), fill=(210, 216, 226))

    # ---------- 6) 右下折角（增强立体）----------
    fold = int(S * 0.115)
    fx, fy = px0 + pw - fold, py0 + ph - fold
    d.polygon([(px0 + pw, fy), (fx, py0 + ph), (px0 + pw, py0 + ph)],
              fill=(206, 217, 233, 255))
    d.polygon([(px0 + pw, fy), (fx, py0 + ph), (px0 + pw - 2, py0 + ph - 2)],
              fill=(186, 199, 219, 255))

    # ---------- 导出 ----------
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_ico_png")
    os.makedirs(out_dir, exist_ok=True)
    for s in SIZES:
        canvas.resize((s, s), Image.LANCZOS).save(os.path.join(out_dir, "icon_%d.png" % s))
    canvas.resize((256, 256), Image.LANCZOS).save(os.path.join(out_dir, "preview_256.png"))
    canvas.resize((128, 128), Image.LANCZOS).save(os.path.join(out_dir, "preview_128.png"))
    # 合成多尺寸 ICO：直接以各尺寸帧写入，Windows 才会挑到最合适的那张
    frames = [Image.open(os.path.join(out_dir, "icon_%d.png" % s)) for s in SIZES]
    frames[0].save(OUT_ICO, format="ICO", sizes=[(s, s) for s in SIZES])
    print("已生成:", OUT_ICO, "尺寸:", SIZES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
