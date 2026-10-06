# -*- coding: utf-8 -*-
"""生成 hotnews.exe 图标：深蓝底 + 橙色新闻卡片，与网页主色一致"""
from PIL import Image, ImageDraw

PRIMARY = (26, 77, 143)      # #1a4d8f
ACCENT = (232, 99, 47)       # #e8632f
WHITE = (255, 255, 255)

SIZE = 256
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 圆角矩形底
r = 52
d.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=r, fill=PRIMARY)

# 三张"新闻卡片"（错位堆叠，暗示图库/列表）
def card(x, y, w, h, fill, alpha=255):
    ov = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=fill + (alpha,))
    return Image.alpha_composite(img, ov)

# 后两张淡白卡片做层次
tmp = card(64, 62, 128, 92, WHITE, 70)
tmp = Image.alpha_composite(tmp, card(52, 76, 128, 92, WHITE, 95))
img = tmp
d = ImageDraw.Draw(img)

# 主卡片
d.rounded_rectangle([40, 90, 216, 194], radius=12, fill=WHITE)
# 卡片左侧橙条（强调色）
d.rounded_rectangle([40, 90, 58, 194], radius=6, fill=ACCENT)
# 标题线
d.rounded_rectangle([72, 112, 192, 126], radius=7, fill=PRIMARY)
d.rounded_rectangle([72, 138, 168, 150], radius=6, fill=(150, 162, 178))
d.rounded_rectangle([72, 162, 180, 174], radius=6, fill=(150, 162, 178))

img.save("hotnews.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("已生成 hotnews.ico")
