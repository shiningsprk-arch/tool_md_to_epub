# -*- coding: utf-8 -*-
"""Markdown → EPUB3 转换引擎（纯函数，无 webserver / calibre 依赖，方便单测）。

设计约束（与产品约定一致）：
- **远程图片一律移除**，绝不联网抓取（隐私 / SSRF / 离线环境）；
- **本地相对图片缺失或越界 → 报错**，提示改用「目录 / zip 上传」保留图片目录结构，
  或勾选「不导入图片」；不会静默丢图；
- 勾选「不导入图片」时，所有图片被替换为 alt 文本（无 alt 则直接删除）；
- 图片语法在**渲染后的 HTML** 上统一处理 `<img>`，因此标准 `![]()`、reference-style
  等 Markdown 图片语法一视同仁；Obsidian `![[...]]` 在渲染前归一化。出于 XSS 考虑
  渲染器关闭内嵌 HTML（`html=False`），原文里的原始 HTML 标签会被转义为文本。

出包复用 `epub_writer`（EPUB3 OPF + nav + NCX + mimetype 首项 STORED），
最后经 `epub_writer.validate_epub` 守门。

三方依赖（markdown-it-py / mdit-py-plugins / beautifulsoup4 + lxml / Pillow）由宿主
镜像提供，本包不带副本；缺失时由 `check_dependencies()` 在转换入口给出 `deps.missing`
错误码，而不是让 ImportError 穿透到宿主变成 500。
"""
import hashlib
import html as _html
import importlib.util
import io
import logging
import os
import re
import uuid
import zipfile
from urllib.parse import unquote

from . import epub_writer

logger = logging.getLogger(__name__)

MARKDOWN_EXTS = (".md", ".markdown", ".mdown", ".mkd")

# 体积 / 数量上限（防误传整盘目录或图片炸弹）
MAX_MD_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 256 * 1024 * 1024
MAX_IMAGES = 3000
MAX_IMAGE_WIDTH = 1600  # 超过则等比缩小
MAX_IMAGE_PIXELS = 64_000_000

# 允许的位图格式（Pillow 识别名，排除 SVG 以防 XSS）
_ALLOWED_IMAGE_FORMATS = {"PNG", "JPEG", "GIF", "WEBP", "BMP"}
_FORMAT_EXT = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp", "BMP": "bmp"}
_EXT_MEDIA = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
}

_DATA_IMG_RE = re.compile(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.*)$", re.DOTALL | re.IGNORECASE)
_REMOTE_RE = re.compile(r"^(?:https?:)?//", re.IGNORECASE)
_OBSIDIAN_IMG_RE = re.compile(r"!\[\[([^\]|]+?)(?:\|([^\]]*))?\]\]")


class MarkdownConvertError(Exception):
    """转换失败（带稳定错误码，边界层映射为 i18n 文案）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 依赖检查
# ---------------------------------------------------------------------------

# (import 名, 发行包名)：宿主镜像必须提供的组件。直接 import 之前先探一次，
# 缺组件时给出可诊断的错误码而不是裸 ImportError（宿主只会把它变成 500）。
# Pillow 不在此列：它在 _normalize_image 里按需导入，缺失时降级为 image.pil_missing。
_REQUIRED_MODULES = (
    ("markdown_it", "markdown-it-py"),
    ("mdit_py_plugins", "mdit-py-plugins"),
    ("bs4", "beautifulsoup4"),
    ("lxml", "lxml"),
)


def check_dependencies() -> None:
    """检查 Markdown 渲染与 HTML 图片改写所需组件是否齐备。

    :raises MarkdownConvertError: code = "deps.missing"，消息里列出缺失的发行包名。
    """
    missing = [pkg for mod, pkg in _REQUIRED_MODULES if importlib.util.find_spec(mod) is None]
    if missing:
        raise MarkdownConvertError("deps.missing", "宿主缺少依赖：%s" % "、".join(missing))


# ---------------------------------------------------------------------------
# 文本 / 路径工具
# ---------------------------------------------------------------------------

def natural_key(path: str):
    """自然排序键（让第2章排在 第10章 之前）。"""
    return [int(p) if p.isdigit() else p.lower()
            for p in re.split(r"(\d+)", path or "")]


def find_markdown_files(root: str) -> list:
    """递归收集 root 下的 Markdown 相对路径（忽略隐藏目录 / __MACOSX），自然排序。"""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__MACOSX"]
        for name in filenames:
            if name.startswith("."):
                continue
            if name.lower().endswith(MARKDOWN_EXTS):
                rel = os.path.relpath(os.path.join(dirpath, name), root)
                found.append(rel.replace(os.sep, "/"))
    found.sort(key=natural_key)
    return found


def read_text(path: str) -> str:
    """读文本：utf-8-sig 优先，回退 gb18030 / big5 / latin-1（容错不抛）。"""
    if os.path.getsize(path) > MAX_MD_BYTES:
        raise MarkdownConvertError(
            "file.too_large", "Markdown 文件过大（上限 %d MB）" % (MAX_MD_BYTES // 1024 // 1024))
    with open(path, "rb") as f:
        data = f.read()
    for enc in ("utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def split_front_matter(text: str):
    """拆分前导 YAML front matter，返回 (meta_dict, body)。

    仅解析顶层 `key: value`（title/author/authors/cover/language），不引入 YAML 依赖；
    没有 front matter 时返回 ({}, text)。
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if not lines or lines[0].strip() not in ("---", "---\r"):
        return {}, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            meta = {}
            for raw in lines[1:idx]:
                if not raw.strip() or raw.lstrip().startswith("#") or ":" not in raw:
                    continue
                key, _, value = raw.partition(":")
                key = key.strip().lower()
                value = value.strip().strip("'\"")
                if value:
                    meta[key] = value
            return meta, "\n".join(lines[idx + 1:])
    return {}, text


def _normalize_obsidian_images(text: str) -> str:
    """Obsidian `![[img.png]]` / `![[img.png|300]]` → 标准 Markdown 图片。"""

    def _repl(m):
        target = m.group(1).strip()
        # 跳过笔记内链（![[some note]]，无图片扩展名）
        if not target.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
            return m.group(0)
        alt = m.group(2) or ""
        width = ""
        if alt.strip().isdigit():
            width = ' width="%s"' % alt.strip()
            alt = ""
        return "![%s](%s)%s" % (alt, target, width)

    return _OBSIDIAN_IMG_RE.sub(_repl, text)


# ---------------------------------------------------------------------------
# Markdown 渲染 + 目录
# ---------------------------------------------------------------------------

def _make_parser():
    from markdown_it import MarkdownIt  # 由 check_dependencies() 守门
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    try:
        from mdit_py_plugins.footnote import footnote_plugin
        from mdit_py_plugins.tasklists import tasklists_plugin
        from mdit_py_plugins.deflist import deflist_plugin
        md.use(footnote_plugin).use(tasklists_plugin).use(deflist_plugin)
    except Exception as err:  # pragma: no cover - 插件缺失时降级为 CommonMark
        logger.info("[md_to_epub] mdit-py-plugins unavailable, fallback: %s", err)
    return md


def render_markdown(md, text: str):
    """渲染为 HTML，同时抽取标题层级目录。

    :return: (html_body, toc_nodes)；toc_nodes = [label, path, frag, [kids...], ...]
    """
    tokens = md.parse(text)
    toc_nodes = []
    stack = []  # [(level, children_list)]
    hid = 0
    for i, tok in enumerate(tokens):
        if tok.type != "heading_open":
            continue
        hid += 1
        anchor = "h%d" % hid
        tok.attrSet("id", anchor)
        level = int(tok.tag[1]) if tok.tag[1:].isdigit() else 1
        inline = tokens[i + 1] if i + 1 < len(tokens) else None
        label = inline.content.strip() if inline is not None and inline.type == "inline" else ""
        node = [label or ("§%d" % hid), "", anchor, []]
        while stack and stack[-1][0] >= level:
            stack.pop()
        if stack:
            stack[-1][1].append(node)
        else:
            toc_nodes.append(node)
        stack.append((level, node[3]))
    body = md.renderer.render(tokens, md.options, {})
    return body, toc_nodes


# ---------------------------------------------------------------------------
# 图片处理
# ---------------------------------------------------------------------------

def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _normalize_image(data: bytes):
    """校验并（必要时）缩放图片，返回 (bytes, ext)；失败抛 MarkdownConvertError。"""
    try:
        from PIL import Image, ImageOps
    except ImportError as err:  # pragma: no cover
        raise MarkdownConvertError("image.pil_missing", "服务器缺少图像处理组件(PIL)") from err

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            if fmt not in _ALLOWED_IMAGE_FORMATS:
                raise MarkdownConvertError("image.unsupported", "不支持的图片格式：%s" % (fmt or "unknown"))
            width = img.width
            if width <= MAX_IMAGE_WIDTH:
                return data, _FORMAT_EXT[fmt]
            img = ImageOps.exif_transpose(img)
            ratio = MAX_IMAGE_WIDTH / float(img.width)
            img = img.resize((MAX_IMAGE_WIDTH, max(1, int(img.height * ratio))), Image.LANCZOS)
            if fmt == "PNG" and img.mode in ("RGBA", "LA", "P"):
                out = io.BytesIO()
                img.save(out, "PNG", optimize=True)
                return out.getvalue(), "png"
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, "JPEG", quality=88, optimize=True)
            return out.getvalue(), "jpg"
    except MarkdownConvertError:
        raise
    except Exception as err:
        raise MarkdownConvertError("image.invalid", "图片无法解析或已损坏：%s" % err) from err


def _add_asset(assets: dict, total: dict, data: bytes) -> tuple:
    """归一化图片并登记进资产表（按内容去重）。返回 (name, media_type)。"""
    if len(data) > MAX_IMAGE_BYTES:
        raise MarkdownConvertError("image.too_large", "单张图片超过 %d MB" % (MAX_IMAGE_BYTES // 1024 // 1024))
    total["bytes"] += len(data)
    if total["bytes"] > MAX_TOTAL_IMAGE_BYTES:
        raise MarkdownConvertError("images.too_large", "图片总体积超过上限，请精简后重试")
    if len(assets) >= MAX_IMAGES:
        raise MarkdownConvertError("images.too_many", "图片数量超过上限（%d 张）" % MAX_IMAGES)
    norm, ext = _normalize_image(data)
    name = "images/img_%s.%s" % (_sha1(norm)[:10], ext)
    if name not in assets:
        assets[name] = (norm, _EXT_MEDIA[ext])
    return name, _EXT_MEDIA[ext]


def _resolve_local(base_dir: str, root: str, ref: str) -> str:
    """把相对引用解析为 root 内的真实文件路径；越界 / 不存在 / 非文件 → 抛错。"""
    rel = unquote(ref).split("#", 1)[0].split("?", 1)[0].strip()
    if not rel or "\x00" in rel:
        raise MarkdownConvertError("image.invalid_ref", "非法的图片引用：%s" % ref)
    if os.path.isabs(rel) or re.match(r"^[a-zA-Z]:", rel) or rel.startswith(("file:", "\\\\")):
        raise MarkdownConvertError("image.invalid_ref", "不支持绝对路径图片引用：%s" % ref)
    target = os.path.realpath(os.path.join(base_dir, rel.replace("/", os.sep)))
    if target != root and not target.startswith(root + os.sep):
        raise MarkdownConvertError("image.escape", "图片引用越界（禁止访问上传目录之外）：%s" % ref)
    if not os.path.isfile(target):
        raise MarkdownConvertError(
            "image.missing",
            "找不到图片：%s。请改用「目录 / zip 上传」保留图片目录结构，或勾选「不导入图片」" % ref)
    ext = os.path.splitext(target)[1].lower().lstrip(".")
    if ext not in _EXT_MEDIA:
        raise MarkdownConvertError("image.unsupported", "不支持的图片类型：%s" % ref)
    return target


def _rewrite_html_images(body: str, base_dir: str, root: str, assets: dict,
                         total: dict, warnings: list, ignore_images: bool):
    """在渲染后的 HTML 上统一改写/校验 `<img>`，返回新的 body 字符串。"""
    from bs4 import BeautifulSoup  # 由 check_dependencies() 守门（含 lxml 解析器）

    soup = BeautifulSoup('<div id="__md_root__">%s</div>' % body, "lxml")
    container = soup.find(id="__md_root__")
    for img in list(container.find_all("img")):
        alt = (img.get("alt") or "").strip()
        src = (img.get("src") or "").strip()
        width = img.get("width")
        title = img.get("title")

        if ignore_images:
            _replace_with_alt(soup, img, alt)
            continue
        if not src:
            img.decompose()
            continue

        m = _DATA_IMG_RE.match(src)
        if m:
            try:
                import base64
                data = base64.b64decode(m.group(2), validate=False)
                name, media = _add_asset(assets, total, data)
                _set_img(soup, img, name, alt, width, title)
            except MarkdownConvertError as err:
                raise err
            except Exception as err:
                raise MarkdownConvertError("image.invalid", "内嵌图片解析失败：%s" % err) from err
            continue

        if _REMOTE_RE.match(src):
            # 远程图片一律移除，不联网
            warnings.append(src)
            _replace_with_alt(soup, img, alt)
            continue

        target = _resolve_local(base_dir, root, src)
        with open(target, "rb") as f:
            data = f.read()
        name, media = _add_asset(assets, total, data)
        _set_img(soup, img, name, alt, width, title)

    return container.decode_contents()


def _set_img(soup, img, name, alt, width, title):
    new = soup.new_tag("img")
    new["src"] = name
    if alt:
        new["alt"] = alt
    if title:
        new["title"] = title
    if width and str(width).isdigit():
        new["width"] = str(width)
    img.replace_with(new)


def _replace_with_alt(soup, img, alt):
    if alt:
        span = soup.new_tag("span")
        span["class"] = "md-img-alt"
        span.string = alt
        img.replace_with(span)
    else:
        img.decompose()


# ---------------------------------------------------------------------------
# XHTML / EPUB 组装
# ---------------------------------------------------------------------------

_VOID_RE = re.compile(
    r"<(br|hr|img|input|meta|link|area|base|col|embed|source|track|wbr)(\s[^<>]*?)?\s*/?>",
    re.IGNORECASE)


def _self_close_void(fragment: str) -> str:
    """把 HTML 空元素改成 XML 自闭合（`<br>`→`<br/>`），供 XHTML 使用。"""
    return _VOID_RE.sub(lambda m: "<%s%s/>" % (m.group(1), m.group(2) or ""), fragment)


def _xhtml_doc(title: str, body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        '<head><meta charset="utf-8"/><title>%s</title></head><body>%s</body></html>'
        % (_html.escape(title or ""), _self_close_void(body))
    ).encode("utf-8")


def _fill_toc_path(nodes: list, path: str):
    for node in nodes:
        node[1] = path
        _fill_toc_path(node[3], path)


def convert(root: str, *, ignore_images: bool = False,
            title: str = None, author: str = None, language: str = None) -> dict:
    """把 root 目录下的所有 Markdown（自然排序）转换为一本 EPUB。

    :param root:          上传暂存根目录（图片引用以此为安全边界）。
    :param ignore_images: True 时不导入任何图片（含远程），仅保留 alt 文本。
    :return: {"epub": bytes, "title", "author", "image_count", "chapter_count", "warnings"}
    :raises MarkdownConvertError: 无可转换内容 / 图片缺失越界 / 宿主缺依赖（deps.missing）。
    """
    root = os.path.realpath(root)
    md_files = find_markdown_files(root)
    if not md_files:
        raise MarkdownConvertError("no_markdown", "未找到 Markdown 文件（.md / .markdown）")

    check_dependencies()
    md = _make_parser()
    assets = {}
    total = {"bytes": 0}
    warnings = []
    pages = []
    fm_title = fm_author = cover_ref = None

    for idx, rel in enumerate(md_files):
        abs_path = os.path.join(root, rel.replace("/", os.sep))
        text = read_text(abs_path)
        if idx == 0:
            fm, text = split_front_matter(text)
            fm_title = (fm.get("title") or "").strip() or None
            fm_author = (fm.get("author") or fm.get("authors") or "").strip() or None
            cover_ref = (fm.get("cover") or "").strip() or None
        text = _normalize_obsidian_images(text)
        body, toc_nodes = render_markdown(md, text)
        body = _rewrite_html_images(
            body, os.path.dirname(abs_path), root, assets, total, warnings, ignore_images)
        name = "content_%d.xhtml" % (idx + 1)
        _fill_toc_path(toc_nodes, name)
        if not toc_nodes:
            label = os.path.splitext(os.path.basename(rel))[0]
            toc_nodes = [[label or ("§%d" % (idx + 1)), name, None, []]]
        pages.append({"name": name, "title": os.path.splitext(os.path.basename(rel))[0],
                      "html": body, "toc": toc_nodes})

    title = title or fm_title or _first_heading(pages)
    if not title:
        title = os.path.splitext(os.path.basename(md_files[0]))[0] or "Untitled"
    author = author or fm_author or "佚名"
    language = language or "zho"

    # 封面：front matter `cover:` 指向的本地图片（受同样的越界/存在性约束）
    cover_name = cover_mt = None
    if cover_ref and not ignore_images:
        try:
            target = _resolve_local(os.path.dirname(os.path.join(root, md_files[0].replace("/", os.sep))),
                                    root, cover_ref)
            with open(target, "rb") as f:
                cdata = f.read()
            cname, cmt = _add_asset(assets, total, cdata)
            cover_name, cover_mt = cname, cmt
        except MarkdownConvertError as err:
            warnings.append(str(err))

    uid = "md-%s" % uuid.uuid4().hex
    manifest = []
    spine = []
    for i, page in enumerate(pages):
        mid = "page%d" % (i + 1)
        manifest.append((mid, page["name"], "application/xhtml+xml"))
        spine.append(mid)
    for name, (_data, media) in assets.items():
        if name == cover_name:
            continue  # 封面由 build_opf 以 cover-image 属性单列，避免同一 href 重复登记
        manifest.append(("img_%s" % _sha1(name.encode())[:12], name, media))
    manifest.append(("nav", "nav.xhtml", "application/xhtml+xml"))
    manifest.append(("ncx", "toc.ncx", "application/x-dtbncx+xml"))

    merged_toc = []
    for page in pages:
        merged_toc.extend(page["toc"])

    out = io.BytesIO()
    writer = epub_writer.EpubZipWriter(out)
    try:
        writer.write("META-INF/container.xml", epub_writer.build_container())
        writer.write("content.opf", epub_writer.build_opf(
            {"title": title, "authors": [author], "languages": [language]},
            manifest, spine, uid, cover_name, cover_mt))
        writer.write("toc.ncx", epub_writer.build_ncx(title, merged_toc, uid))
        writer.write("nav.xhtml", epub_writer.build_nav(title, merged_toc))
        for page in pages:
            writer.write(page["name"], _xhtml_doc(page["title"] or title, page["html"]))
        for name, (data, _media) in assets.items():
            writer.write(name, data)
    finally:
        writer.close()
    epub_bytes = out.getvalue()
    epub_writer.validate_epub(epub_bytes)

    return {
        "epub": epub_bytes,
        "title": title,
        "author": author,
        "image_count": len(assets),
        "chapter_count": len(pages),
        "warnings": warnings,
    }


def _first_heading(pages: list):
    for page in pages:
        for node in page["toc"]:
            if node[0] and not node[0].startswith("§"):
                return node[0]
    return None


# ---------------------------------------------------------------------------
# zip 安全解压
# ---------------------------------------------------------------------------

_ZIP_MAX_ENTRIES = 5000
_ZIP_MAX_TOTAL = 1024 * 1024 * 1024


def safe_extract_zip(zip_path: str, dest_root: str) -> None:
    """把 zip 解压到 dest_root，拒绝路径穿越 / zip bomb。

    :raises MarkdownConvertError: 非法 zip / 越界 / 超限。
    """
    dest_root = os.path.realpath(dest_root)
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as err:
        raise MarkdownConvertError("zip.invalid", "不是合法的 zip 文件：%s" % err) from err
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > _ZIP_MAX_ENTRIES:
            raise MarkdownConvertError("zip.too_many", "zip 内文件数超过上限（%d）" % _ZIP_MAX_ENTRIES)
        total = sum(i.file_size for i in infos)
        if total > _ZIP_MAX_TOTAL:
            raise MarkdownConvertError("zip.too_large", "zip 解压后体积超过上限")
        for info in infos:
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or re.match(r"^[a-zA-Z]:", name):
                raise MarkdownConvertError("zip.escape", "zip 内含非法路径：%s" % info.filename)
            target = os.path.realpath(os.path.join(dest_root, name))
            if target != dest_root and not target.startswith(dest_root + os.sep):
                raise MarkdownConvertError("zip.escape", "zip 内含越界路径：%s" % info.filename)
        zf.extractall(dest_root)
