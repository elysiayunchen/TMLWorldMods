# -*- coding: utf-8 -*-
"""生成泰拉瑞亚物品栏风格图标：像素草地土块 + 粉色小花。"""
import random

from PIL import Image, ImageDraw

random.seed(20)
S = 256
GRID = 16
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

C0, C1, C2 = 2, 13, 3  # 块范围：列 2..13，行 3..14

# 草顶不规则：每列的草起始行（3 或 4）
grass_top = {x: (3 if random.random() < 0.6 else 4) for x in range(C0, C1 + 1)}
# 保证不会太参差
for x in range(C0 + 1, C1 + 1):
    if abs(grass_top[x] - grass_top[x - 1]) > 1:
        grass_top[x] = grass_top[x - 1]

GRASS = [(76, 175, 80), (67, 160, 71), (56, 142, 60), (88, 190, 92)]
DIRT = [(121, 85, 72), (109, 76, 65), (93, 64, 55), (131, 95, 83)]
OUTLINE = (58, 40, 34, 255)

for x in range(C0, C1 + 1):
    top = grass_top[x]
    for y in range(top, 15):
        if y <= top + 1:
            c = random.choice(GRASS)
        else:
            c = random.choice(DIRT)
        d.rectangle([x * GRID, y * GRID, (x + 1) * GRID - 1,
                     (y + 1) * GRID - 1], fill=c)

# 块外描边（沿外沿 2px，像素感）
minx, maxx = C0 * GRID, (C1 + 1) * GRID - 1
miny, maxy = min(grass_top.values()) * GRID, 15 * GRID - 1
for t in range(3):
    d.rectangle([minx - t, miny - t, maxx + t, maxy + t],
                outline=OUTLINE)

# 草下沿一条深色分界
for x in range(C0, C1 + 1):
    y = grass_top[x] + 2
    d.rectangle([x * GRID, y * GRID, (x + 1) * GRID - 1, y * GRID + 2],
                fill=(52, 116, 55, 255))

# 泥土里加几粒小石子
for _ in range(7):
    x = random.randint(C0 + 1, C1 - 1)
    y = random.randint(7, 13)
    d.rectangle([x * GRID + 4, y * GRID + 4, x * GRID + 11, y * GRID + 11],
                fill=(161, 136, 127, 255))

# 粉色小花 ×2（泰拉粉点缀）
def flower(gx, gy):
    px, py = gx * GRID, gy * GRID
    d.rectangle([px + 5, py + 2, px + 10, py + 13],
                fill=(46, 125, 50, 255))            # 茎
    for dx, dy in ((0, 0), (8, 0), (0, 8), (8, 8)):
        d.rectangle([px - 1 + dx, py - 4 + dy, px + 6 + dx, py + 1 + dy],
                    fill=(244, 143, 177, 255))      # 花瓣
    d.rectangle([px + 3, py, px + 8, py + 5],
                fill=(255, 224, 130, 255))          # 花芯


flower(4, 1)
flower(10, 1)

img.save("app_preview.png")
img.save("app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                           (64, 64), (128, 128), (256, 256)])
print("icon generated")
