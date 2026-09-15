# -*- coding: utf-8 -*-
"""工具包契约守卫（纯标准库：AST + JSON + 正则，不 import 宿主 webserver）。

校验的是「mytool validate 管不到、但一旦破就会在真实安装后炸掉」的那些约定：

- manifest.json 必填字段 / 格式 / default_locale 属于 locales / api_routes 完整；
- entry_backend 指向的模块文件存在、entry_frontend 固定 index.html（宿主硬编码）、
  icon.png 存在且是 PNG（动态工具只认包根的 icon.png）；
- backend/tool.py 的 info() 与 manifest.json 一致；两个 handler 的鉴权装饰器没被漏掉
  （宿主挂载外部工具路由时不会注入鉴权）；
- 三语文案 key 集合一致，且 index.html / app.js / 后端错误码引用到的 key 都存在；
- 包内不夹带三方依赖副本（依赖由宿主镜像提供）。

运行：python tests/test_package_layout.py  或  pytest tests/
"""
import ast
import json
import os
import re
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)

LOCALE_CODES = ("zh", "en", "zh-TW")
DEFAULT_LOCALE = "zh"

# 与宿主 webserver/toolbox/toolbox_manager.py 的 REQUIRED_MANIFEST_FIELDS 保持一致
REQUIRED_FIELDS = (
    "tool_id", "name", "description", "revision", "author",
    "core_api_version", "entry_backend", "repo_url",
)
TOOL_ID_RE = re.compile(r"^[a-z0-9_]+$")
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# info() 与 manifest.json 中必须逐字一致的字段
INFO_FIELDS = ("tool_id", "name", "description", "revision", "author",
               "publish_date", "repo_url")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _read_json(path):
    return json.loads(_read(path))


def _load_locales():
    locales = {}
    for code in LOCALE_CODES:
        locales[code] = _read_json(os.path.join(ROOT, "frontend", "locales", "%s.json" % code))
    return locales


def _backend_module_path(entry_backend):
    module_rel = entry_backend.split(".")[:-1]   # "tool.MdToEpubTool" -> ["tool"]
    return os.path.join(ROOT, "backend", *module_rel) + ".py"


def _class_node(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError("backend/tool.py 里找不到类 %s" % name)


def _method_node(class_node, name):
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError("类 %s 里找不到方法 %s" % (class_node.name, name))


def _decorator_names(method_node):
    names = []
    for dec in method_node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Attribute):
            names.append(target.attr)
    return names


class ManifestTest(unittest.TestCase):
    """manifest.json 本身必须能过宿主 validate_manifest() 与 mytool validate。"""

    def setUp(self):
        self.manifest = _read_json(os.path.join(ROOT, "manifest.json"))

    def test_required_fields_present(self):
        missing = [f for f in REQUIRED_FIELDS if not self.manifest.get(f)]
        self.assertEqual(missing, [], "manifest.json 缺少必填字段：%s" % missing)

    def test_tool_id_and_versions(self):
        self.assertRegex(self.manifest["tool_id"], TOOL_ID_RE)
        self.assertRegex(self.manifest["revision"], SEMVER_RE)
        self.assertRegex(self.manifest["core_api_version"], SEMVER_RE)

    def test_repo_url_is_absolute_https(self):
        self.assertTrue(self.manifest["repo_url"].startswith("https://"))

    def test_locales_and_default(self):
        locales = self.manifest["locales"]
        self.assertIsInstance(locales, list)
        self.assertTrue(locales)
        self.assertIn(self.manifest["default_locale"], locales)

    def test_entry_points(self):
        # entry_frontend 必须逐字是 index.html：宿主 ToolFrontendIndexHandler 硬编码该路径
        self.assertEqual(self.manifest["entry_frontend"], "index.html")
        module_rel, _, class_name = self.manifest["entry_backend"].rpartition(".")
        self.assertEqual(module_rel, "tool")
        self.assertEqual(class_name, "MdToEpubTool")

    def test_api_routes_declared(self):
        routes = self.manifest["api_routes"]
        paths = [entry["path"] for entry in routes]
        self.assertEqual(sorted(paths), ["convert", "download"])
        for entry in routes:
            self.assertTrue(entry["handler"].startswith("tool."))
            # path 会被宿主拼成正则片段，不能带前导斜杠
            self.assertFalse(entry["path"].startswith("/"))

    def test_no_frontend_build_step(self):
        config = _read_json(os.path.join(ROOT, ".toolbuilder.json"))
        self.assertIsNone(config["frontendBuildCommand"])
        self.assertIsNone(config["frontendOutputDir"])


class PackageLayoutTest(unittest.TestCase):
    """打包产物的形状：宿主按这些约定找文件。"""

    def setUp(self):
        self.manifest = _read_json(os.path.join(ROOT, "manifest.json"))

    def test_backend_package_and_entry_module(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "backend", "__init__.py")),
                        "backend/__init__.py 缺失时工具内无法相对导入")
        self.assertTrue(os.path.isfile(_backend_module_path(self.manifest["entry_backend"])))

    def test_frontend_entry_exists(self):
        entry = os.path.join(ROOT, "frontend", self.manifest["entry_frontend"])
        self.assertTrue(os.path.isfile(entry))
        html = _read(entry)
        # 宿主提供的 bridge 必须用绝对路径引入，其余资源相对 index.html
        self.assertIn('src="/static/toolbox-bridge.js"', html)
        self.assertIn('src="lib/i18n.js"', html)
        self.assertIn('src="app.js"', html)

    def test_icon_is_png_at_package_root(self):
        icon = os.path.join(ROOT, "icon.png")
        self.assertTrue(os.path.isfile(icon), "动态工具的图标只认包根 icon.png")
        with open(icon, "rb") as f:
            head = f.read(24)
        self.assertEqual(head[:8], b"\x89PNG\r\n\x1a\n")
        # 图标位是方的：直接从 IHDR 读宽高（不引 Pillow，保持本文件纯标准库）
        width = int.from_bytes(head[16:20], "big")
        height = int.from_bytes(head[20:24], "big")
        self.assertEqual(width, height, "icon.png 必须是正方形（%dx%d）" % (width, height))
        self.assertGreaterEqual(width, 256)
        # .jpg 放在包根不会被使用（宿主对动态工具只查 icon.png），留着只会误导
        self.assertFalse(os.path.isfile(os.path.join(ROOT, "icon.jpg")))

    def test_no_bundled_dependency_copies(self):
        """依赖由宿主镜像提供；夹带副本会让升级/审计出现分歧。"""
        self.assertFalse(os.path.isdir(os.path.join(ROOT, "backend", "vendor")))
        for dirpath, dirnames, _files in os.walk(os.path.join(ROOT, "backend")):
            for name in dirnames:
                self.assertNotIn(name, ("vendor", "site-packages"),
                                 "backend/ 下不应有依赖副本：%s" % os.path.join(dirpath, name))

    def test_required_runtime_files_exist(self):
        for rel in ("backend/tool.py", "backend/md_to_epub_lib.py", "backend/epub_writer.py",
                    "LICENSE", "README.md"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)), "缺少 %s" % rel)


class BackendContractTest(unittest.TestCase):
    """backend/tool.py 的契约：info() 与 manifest 一致、鉴权装饰器没漏。"""

    def setUp(self):
        self.tool_src = _read(os.path.join(ROOT, "backend", "tool.py"))
        self.tree = ast.parse(self.tool_src)
        self.manifest = _read_json(os.path.join(ROOT, "manifest.json"))

    def test_info_matches_manifest(self):
        info_fn = None
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "info":
                info_fn = node
        self.assertIsNotNone(info_fn, "backend/tool.py 里找不到 info()")
        returns = [s for s in info_fn.body if isinstance(s, ast.Return)]
        self.assertEqual(len(returns), 1)
        info = ast.literal_eval(returns[0].value)
        self.assertIsInstance(info, dict)
        for field in INFO_FIELDS:
            self.assertEqual(info.get(field), self.manifest.get(field),
                             "info().%s 与 manifest.json 不一致" % field)

    def test_tool_class_subclasses_base_tool(self):
        cls = _class_node(self.tree, self.manifest["entry_backend"].split(".")[-1])
        bases = [b.id if isinstance(b, ast.Name) else getattr(b, "attr", None)
                 for b in cls.bases]
        self.assertIn("BaseTool", bases)

    def test_convert_handler_requires_admin_and_js(self):
        """宿主不注入鉴权，@js / @is_admin 漏一个都会出问题（漏 @is_admin = 接口公开）。"""
        cls = _class_node(self.tree, "ConvertHandler")
        decorators = _decorator_names(_method_node(cls, "post"))
        self.assertIn("js", decorators)
        self.assertIn("is_admin", decorators)
        # @js 必须在最外层：@is_admin 非管理员时返回 dict，没有 @js 就没人序列化它
        self.assertLess(decorators.index("js"), decorators.index("is_admin"))

    def test_download_handler_checks_admin(self):
        """download 返回字节流用不了 @js，按宿主既有做法显式判权。"""
        cls = _class_node(self.tree, "DownloadHandler")
        method = _method_node(cls, "get")
        segment = ast.get_source_segment(self.tool_src, method) or ""
        self.assertIn("current_user", segment)
        self.assertIn("admin_user", segment)

    def test_import_method_uses_register_function(self):
        """入库必须走 register_function（同步变体）。

        register_service 在当前宿主实现里 `async_mode()` 恒为 True，调用会被丢进后台队列并
        返回 None —— 拿不到 book_id；而不加装饰器直接调 `self.db` 时它还没被注入（None），
        会在 import_file 内部炸。只有 register_function 会先 setup(db, scoped_session) 再同步
        调用，两者都规避。
        """
        cls = _class_node(self.tree, self.manifest["entry_backend"].split(".")[-1])
        decorators = _decorator_names(_method_node(cls, "import_epub"))
        self.assertIn("register_function", decorators)
        self.assertNotIn("register_service", decorators)

    def test_import_defaults_on_and_uses_current_user(self):
        """默认入库（前端不传参时也入库），且入库归属当前用户。"""
        method = _method_node(_class_node(self.tree, "ConvertHandler"), "post")
        segment = ast.get_source_segment(self.tool_src, method) or ""
        self.assertRegex(segment, r'get_argument\(\s*"import_to_library"\s*,\s*"1"\s*\)',
                         "import_to_library 的缺省值应为开，否则默认行为不是入库")
        self.assertIn("self.current_user.id", segment)
        self.assertIn("import_epub", segment)

    def test_routes_are_manifest_driven(self):
        """外部工具的路由前缀由宿主拼，代码里不能写死内置时代的路径。"""
        self.assertNotIn("/api/toolbox/md_to_epub/", self.tool_src)
        self.assertIn("/api/toolbox/tool/md_to_epub", self.tool_src)


class I18nTest(unittest.TestCase):
    """三语言案 + HTML/JS 引用一致性。"""

    def setUp(self):
        self.locales = _load_locales()
        self.html = _read(os.path.join(ROOT, "frontend", "index.html"))
        self.js = _read(os.path.join(ROOT, "frontend", "app.js"))

    def test_locale_files_match_locales_manifest(self):
        declared = _read_json(os.path.join(ROOT, "frontend", "locales", "manifest.json"))
        on_disk = sorted(name[:-5] for name in os.listdir(
            os.path.join(ROOT, "frontend", "locales")) if name.endswith(".json")
            and name != "manifest.json")
        self.assertEqual(sorted(declared["locales"]), on_disk)
        self.assertIn(declared["default"], declared["locales"])
        # 工具包 manifest.json 的 locales 声明也要对得上
        tool_manifest = _read_json(os.path.join(ROOT, "manifest.json"))
        self.assertEqual(sorted(tool_manifest["locales"]), on_disk)

    def test_catalogs_are_flat_string_tables(self):
        for code, table in self.locales.items():
            for key, value in table.items():
                self.assertIsInstance(value, str, "%s.json: %s 不是字符串" % (code, key))

    def test_key_sets_identical(self):
        reference = set(self.locales[DEFAULT_LOCALE])
        for code, table in self.locales.items():
            self.assertEqual(set(table), reference,
                             "%s.json 的 key 集合与 %s.json 不一致" % (code, DEFAULT_LOCALE))

    def test_html_keys_are_translated(self):
        keys = set(re.findall(r'data-i18n="([^"]+)"', self.html))
        for spec in re.findall(r'data-i18n-attr="([^"]+)"', self.html):
            for rule in spec.split(";"):
                parts = rule.split(":")
                if len(parts) == 2:
                    keys.add(parts[1].strip())
        self.assertTrue(keys)
        for code, table in self.locales.items():
            missing = sorted(k for k in keys if k not in table)
            self.assertEqual(missing, [], "%s.json 缺少 index.html 引用的文案：%s" % (code, missing))

    def test_js_literal_keys_are_translated(self):
        keys = set(re.findall(r"i18n\.t\('([^']+)'", self.js))
        keys |= set(re.findall(r"i18n\.t\(\"([^\"]+)\"", self.js))
        self.assertTrue(keys, "app.js 里没有找到 i18n.t 调用？")
        for code, table in self.locales.items():
            missing = sorted(k for k in keys if k not in table)
            self.assertEqual(missing, [], "%s.json 缺少 app.js 引用的文案：%s" % (code, missing))

    def test_backend_error_codes_are_translated(self):
        """后端抛/返回的每个错误码都要有 error.<code> 文案，否则界面回落到中文 msg。"""
        codes = set()
        lib_src = _read(os.path.join(ROOT, "backend", "md_to_epub_lib.py"))
        codes |= set(re.findall(r'MarkdownConvertError\(\s*"([a-z0-9_.]+)"', lib_src))
        tool_src = _read(os.path.join(ROOT, "backend", "tool.py"))
        codes |= set(re.findall(r'"err":\s*"([a-z0-9_.]+)"', tool_src)) - {"ok"}
        self.assertIn("no_markdown", codes)
        self.assertIn("deps.missing", codes)
        for code, table in self.locales.items():
            missing = sorted(c for c in codes if "error.%s" % c not in table)
            self.assertEqual(missing, [], "%s.json 缺少错误码文案：%s" % (code, missing))


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=2))
