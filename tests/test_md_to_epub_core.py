# -*- coding: utf-8 -*-
"""md_to_epub 转换引擎单元测试（standalone，纯函数，无需 calibre）。

覆盖：front matter / 标题作者、本地图片内嵌、远程图片移除、缺失图片报错、
路径穿越拒绝、data URI、Obsidian 语法、多文件自然排序、不导入图片模式、
EPUB 结构守门、zip 安全解压、宿主依赖前置检查。

运行：python tests/test_md_to_epub_core.py  或  pytest tests/
（不依赖宿主 webserver / calibre；markdown-it-py 等三方组件由运行环境提供）
"""
import base64
import importlib.util
import io
import os
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend import epub_writer  # noqa: E402
from backend import md_to_epub_lib as lib  # noqa: E402


def _png_bytes(color=(10, 120, 220)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), color).save(buf, "PNG")
    return buf.getvalue()


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode, **({} if isinstance(data, bytes) else {"encoding": "utf-8"})) as f:
        f.write(data)


class MdToEpubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, **kw):
        return lib.convert(self.root, **kw)

    # ---------------------------------------------------------------- 基础

    def test_basic_convert_with_local_image(self):
        _write(os.path.join(self.root, "images", "pic.png"), _png_bytes())
        _write(os.path.join(self.root, "book.md"), (
            "---\ntitle: 测试书\nauthor: 张三\n---\n\n"
            "# 第一章\n\n正文 **粗体**。\n\n"
            "![本地图](images/pic.png)\n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n\n~~删除~~\n"
        ))
        res = self._run()
        self.assertEqual(res["title"], "测试书")
        self.assertEqual(res["author"], "张三")
        self.assertEqual(res["image_count"], 1)
        self.assertEqual(res["chapter_count"], 1)
        epub_writer.validate_epub(res["epub"])

        zf = zipfile.ZipFile(io.BytesIO(res["epub"]))
        content = zf.read("content_1.xhtml").decode("utf-8")
        self.assertIn("<table>", content)
        self.assertIn("<s>", content)
        self.assertIn("images/img_", content)
        self.assertEqual(zf.namelist()[0], "mimetype")

    def test_remote_image_removed_not_fetched(self):
        _write(os.path.join(self.root, "book.md"), "# T\n\n![x](https://example.com/x.png)\n")
        res = self._run()
        self.assertEqual(res["image_count"], 0)
        self.assertEqual(res["warnings"], ["https://example.com/x.png"])
        epub_writer.validate_epub(res["epub"])

    # ------------------------------------------------------------ 图片报错

    def test_missing_local_image_raises(self):
        _write(os.path.join(self.root, "book.md"), "# T\n\n![x](images/missing.png)\n")
        with self.assertRaises(lib.MarkdownConvertError) as ctx:
            self._run()
        self.assertEqual(ctx.exception.code, "image.missing")

    def test_path_traversal_rejected(self):
        _write(os.path.join(self.root, "book.md"), "# T\n\n![x](../../secret.png)\n")
        with self.assertRaises(lib.MarkdownConvertError) as ctx:
            self._run()
        self.assertEqual(ctx.exception.code, "image.escape")

    def test_absolute_path_rejected(self):
        _write(os.path.join(self.root, "book.md"), "# T\n\n![x](C:/windows/system32/x.png)\n")
        with self.assertRaises(lib.MarkdownConvertError) as ctx:
            self._run()
        self.assertEqual(ctx.exception.code, "image.invalid_ref")

    def test_ignore_images_keeps_alt(self):
        _write(os.path.join(self.root, "book.md"), "# T\n\n![找不到](images/missing.png)\n")
        res = self._run(ignore_images=True)
        self.assertEqual(res["image_count"], 0)
        content = zipfile.ZipFile(io.BytesIO(res["epub"])).read("content_1.xhtml").decode("utf-8")
        self.assertIn("找不到", content)
        self.assertNotIn("<img", content)

    # -------------------------------------------------------- 其它图片来源

    def test_front_matter_cover(self):
        _write(os.path.join(self.root, "cover.png"), _png_bytes((240, 200, 10)))
        _write(os.path.join(self.root, "images", "pic.png"), _png_bytes())
        _write(os.path.join(self.root, "book.md"), (
            "---\ntitle: C\ncover: cover.png\n---\n\n# T\n\n![x](images/pic.png)\n"))
        res = self._run()
        self.assertEqual(res["image_count"], 2)
        epub_writer.validate_epub(res["epub"])
        opf = zipfile.ZipFile(io.BytesIO(res["epub"])).read("content.opf").decode("utf-8")
        self.assertIn('properties="cover-image"', opf)

    def test_data_uri_image(self):
        b64 = base64.b64encode(_png_bytes()).decode()
        _write(os.path.join(self.root, "book.md"),
               "# T\n\n![inline](data:image/png;base64,%s)\n" % b64)
        res = self._run()
        self.assertEqual(res["image_count"], 1)
        epub_writer.validate_epub(res["epub"])

    def test_obsidian_wikilink(self):
        _write(os.path.join(self.root, "assets", "pic.png"), _png_bytes())
        _write(os.path.join(self.root, "book.md"), "# T\n\n![[assets/pic.png]]\n")
        res = self._run()
        self.assertEqual(res["image_count"], 1)

    # ---------------------------------------------------------------- 结构

    def test_multiple_files_natural_order(self):
        _write(os.path.join(self.root, "第10章.md"), "# 十\n\n正文\n")
        _write(os.path.join(self.root, "第2章.md"), "# 二\n\n正文\n")
        res = self._run()
        self.assertEqual(res["chapter_count"], 2)
        zf = zipfile.ZipFile(io.BytesIO(res["epub"]))
        first = zf.read("content_1.xhtml").decode("utf-8")
        self.assertIn("二", first)  # 第2章 应排在 第10章 之前

    def test_no_markdown_raises(self):
        _write(os.path.join(self.root, "note.txt"), "not markdown")
        with self.assertRaises(lib.MarkdownConvertError) as ctx:
            self._run()
        self.assertEqual(ctx.exception.code, "no_markdown")

    # ------------------------------------------------------------ zip 安全

    def test_safe_extract_zip(self):
        zip_path = os.path.join(self.root, "book.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("book.md", "# T\n")
            zf.writestr("images/pic.png", _png_bytes())
        dest = os.path.join(self.root, "out")
        lib.safe_extract_zip(zip_path, dest)
        self.assertTrue(os.path.isfile(os.path.join(dest, "book.md")))

    def test_zip_slip_rejected(self):
        zip_path = os.path.join(self.root, "evil.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../evil.txt", "x")
        with self.assertRaises(lib.MarkdownConvertError) as ctx:
            lib.safe_extract_zip(zip_path, os.path.join(self.root, "out"))
        self.assertEqual(ctx.exception.code, "zip.escape")


class DependencyCheckTest(unittest.TestCase):
    """宿主依赖前置检查：缺组件时给 deps.missing，而不是让 ImportError 穿透成 500。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _hide(self, *modules):
        """临时让 find_spec 报告指定模块不存在（其余照常走真实实现）。"""
        real_find_spec = importlib.util.find_spec

        def fake_find_spec(name, *args, **kwargs):
            if name in modules:
                return None
            return real_find_spec(name, *args, **kwargs)

        return mock.patch.object(lib.importlib.util, "find_spec", fake_find_spec)

    def test_required_modules_available(self):
        lib.check_dependencies()  # 运行环境齐备时不应抛错

    def test_missing_module_raises_deps_missing(self):
        with self._hide("markdown_it"):
            with self.assertRaises(lib.MarkdownConvertError) as ctx:
                lib.check_dependencies()
        self.assertEqual(ctx.exception.code, "deps.missing")
        self.assertIn("markdown-it-py", str(ctx.exception))

    def test_all_missing_modules_reported(self):
        with self._hide("markdown_it", "mdit_py_plugins", "bs4", "lxml"):
            with self.assertRaises(lib.MarkdownConvertError) as ctx:
                lib.check_dependencies()
        message = str(ctx.exception)
        for pkg in ("markdown-it-py", "mdit-py-plugins", "beautifulsoup4", "lxml"):
            self.assertIn(pkg, message)

    def test_convert_reports_deps_missing(self):
        _write(os.path.join(self.root, "book.md"), "# T\n\n正文\n")
        with self._hide("markdown_it"):
            with self.assertRaises(lib.MarkdownConvertError) as ctx:
                lib.convert(self.root)
        self.assertEqual(ctx.exception.code, "deps.missing")

    def test_no_markdown_takes_precedence_over_deps(self):
        """空上传仍报 no_markdown（用户向的错误优先于宿主缺组件）。"""
        _write(os.path.join(self.root, "note.txt"), "not markdown")
        with self._hide("markdown_it"):
            with self.assertRaises(lib.MarkdownConvertError) as ctx:
                lib.convert(self.root)
        self.assertEqual(ctx.exception.code, "no_markdown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
