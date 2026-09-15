# -*- coding: utf-8 -*-
"""生成工具图标 icon.png（1024²，浅蓝渐变圆角底 + 白色页面卡 + M↓ 标记）。

宿主的 ToolIconHandler 对动态工具（外部工具包）**只认包根目录的 `icon.png`**：
`TOOL_ROOT/<tool_id>/icon.png` 找不到才回退到内置资源
`webserver/resources/toolbox/<tool_id>.{jpg,png}` 与 `default_tool.png`。
所以图标必须是 PNG，且放在仓库根（打进 zip 的根）。

用法：python scripts/make_icon.py       # 覆盖写出 <repo>/icon.png
"""
import os

from PIL import Image, ImageChops, ImageDraw

SIZE = 1024
RADIUS = 208                     # 圆角半径
GRADIENT_TOP = (21, 101, 192)    # #1565C0
GRADIENT_BOTTOM = (66, 165, 245)  # #42A5F5
MARK = (16, 74, 150)             # 卡片上的 M↓ 用更深的蓝，保证对比度
CARD = (252, 253, 255)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _vertical_gradient(size, top, bottom):
    img = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        ratio = y / float(size - 1)
        draw.line([(0, y), (size, y)],
                  fill=tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)))
    return img


def _rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _sheen_mask(size, height, start_alpha, end_alpha):
    """自上而下渐隐的高光遮罩（避免硬边：纯渐变而不是实心图形）。"""
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    for y in range(min(height, size)):
        ratio = y / float(height - 1)
        draw.line([(0, y), (size, y)],
                  fill=int(start_alpha + (end_alpha - start_alpha) * ratio))
    return mask


def build_icon():
    rounded = _rounded_mask(SIZE, RADIUS)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(_vertical_gradient(SIZE, GRADIENT_TOP, GRADIENT_BOTTOM), (0, 0), rounded)

    # 顶部玻璃高光：白色 + 自上而下渐隐的 alpha，再用圆角遮罩裁掉外侧
    transparent = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    white = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 255))
    sheen = Image.composite(white, transparent,
                            ImageChops.multiply(_sheen_mask(SIZE, 620, 64, 0), rounded))
    canvas = Image.alpha_composite(canvas, sheen)

    # 白色页面卡（圆角矩形），作为 M↓ 的底
    card = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(card).rounded_rectangle([250, 190, 774, 834], radius=52,
                                           fill=CARD + (246,))
    canvas = Image.alpha_composite(canvas, card)

    draw = ImageDraw.Draw(canvas)
    # "M"：竖向笔画 + 中间下凹的折线，用圆角连接画出饱满的笔形
    draw.line([(340, 625), (340, 375), (438, 577), (536, 375), (536, 625)],
              fill=MARK + (255,), width=42, joint="curve")
    # "↓"：竖线 + 三角箭头，示意 Markdown → EPUB 的"转换输出"
    draw.line([(632, 375), (632, 563)], fill=MARK + (255,), width=42)
    draw.polygon([(584, 541), (680, 541), (632, 649)], fill=MARK + (255,))

    return canvas.convert("RGBA")


def main():
    out_path = os.path.join(REPO_ROOT, "icon.png")
    build_icon().save(out_path, "PNG", optimize=True)
    print("written: %s (%d bytes)" % (out_path, os.path.getsize(out_path)))


if __name__ == "__main__":
    main()
