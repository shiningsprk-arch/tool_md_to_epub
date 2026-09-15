# -*- coding: utf-8 -*-
"""从 `assets/icon_source.jpg` 生成工具图标 `icon.png`（1024²）。

宿主的 ToolIconHandler 对**外部工具只认包根目录的 `icon.png`**——同名 `.jpg` 不会被使用，
会静默回退到内置资源查找最终显示 `default_tool.png`。所以作者给的原图必须转成 PNG。

原图不是正方形时按中心裁剪成正方形（图标位是方的，拉伸会变形）。用法：

    python scripts/make_icon.py
"""
import os

from PIL import Image

SIZE = 1024
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO_ROOT, "assets", "icon_source.jpg")
OUTPUT = os.path.join(REPO_ROOT, "icon.png")


def main():
    with Image.open(SOURCE) as img:
        img = img.convert("RGB")
        if img.width != img.height:
            side = min(img.size)
            left = (img.width - side) // 2
            top = (img.height - side) // 2
            img = img.crop((left, top, left + side, top + side))
        img = img.resize((SIZE, SIZE), Image.LANCZOS)
        img.save(OUTPUT, "PNG", optimize=True)
    print("written: %s (%d bytes, %dx%d)" % (OUTPUT, os.path.getsize(OUTPUT), SIZE, SIZE))


if __name__ == "__main__":
    main()
