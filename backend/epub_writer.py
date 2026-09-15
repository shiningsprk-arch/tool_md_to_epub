# -*- coding: utf-8 -*-
"""通用 EPUB3 打包器（纯函数，无 webserver / calibre 依赖，方便单测）。

抽自 `epub_merge_lib.py` 中已被验证过的出包实现（mimetype 首项 STORED、
EPUB3 OPF + `properties="nav"`、`cover-image` 属性、dcterms:modified、
NCX 作为 EPUB2 兼容目录、href 统一 URI 转义），供 md_to_epub 等工具复用，
避免各工具各写一套容易踩 epubcheck 坑的打包逻辑。

刻意只保留"从零组装一本新书"所需的最小接口，不承担解析/合并既有 EPUB
的职责（那部分留在 epub_merge_lib）。
"""

import html
import io
import re
import zipfile
from datetime import datetime, timezone
from urllib.parse import quote, unquote

# href 输出编码白名单（与 epub_merge_lib 保持一致）：zip 名里的 `#`/空格/非 ASCII
# 直接写进 OPF/NCX 会变成非法 URI。
_HREF_SAFE = "/~@$+,-.;=[]!_'"

_GUESS_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".ttf": "application/x-font-ttf",
    ".otf": "application/vnd.ms-opentype",
    ".woff": "application/font-woff",
    ".woff2": "font/woff2",
}


def quote_href(name: str) -> str:
    """把 zip 条目名编码为合法 URI 路径（fragment/query/空格/非 ASCII 转义）。"""
    return quote(name or "", safe=_HREF_SAFE)


def quote_toc_src(path: str, frag) -> str:
    """编码 (路径, fragment) 二元组（路径里的 `#` 会被转义，不与分隔符混淆）。"""
    if frag:
        return "%s#%s" % (quote_href(path), quote(frag, safe=""))
    return quote_href(path)


def xml_attr(value: str) -> str:
    """XML 属性值转义（href 里已无裸 `&`，此处兜底引号/尖括号）。"""
    return html.escape(value or "", quote=True)


def guess_media_type(name: str) -> str:
    """按扩展名推断 media-type，未知回退 octet-stream。"""
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _GUESS_MEDIA_TYPES.get(ext, "application/octet-stream")


def _strip_tags(text: str) -> str:
    """去标签取纯文本（dc:description 用），连续空白折叠。"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


class EpubZipWriter:
    """把条目流式写入目标 EPUB（mimetype 首项 STORED，其余 DEFLATED）。"""

    def __init__(self, out):
        self._zf = zipfile.ZipFile(out, "w")
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        info.create_system = 0
        self._zf.writestr(info, b"application/epub+zip")
        self._names = {"mimetype"}

    def write(self, name: str, data: bytes) -> None:
        if name in self._names:
            return
        info = zipfile.ZipInfo(name)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 0
        self._zf.writestr(info, data)
        self._names.add(name)

    def close(self):
        self._zf.close()


def build_container() -> bytes:
    """EPUB 容器描述文件（固定指向 content.opf）。"""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="content.opf" '
        'media-type="application/oebps-package+xml"/></rootfiles>'
        "</container>"
    ).encode("utf-8")


def build_opf(meta: dict, manifest: list, spine: list, uid: str,
              cover_name: str | None = None, cover_mt: str | None = None) -> bytes:
    """组装 OPF（EPUB 3.0；href 统一 URI 转义）。

    3.0 而不是 2.0：输出带 EPUB3 导航文档（`properties="nav"`），EPUB2 包
    不支持 nav 文件（epubcheck HTM-004）；NCX 保留并由 `spine toc="ncx"` 引用，
    兼容旧阅读器。EPUB3 必需项：`dcterms:modified`；封面用 `properties="cover-image"`
    （EPUB2 的 `<meta name="cover">` / `<guide>` 在 3.0 不合法，不再输出）。

    :param manifest: [(id, href, media_type), ...]
    :param spine:    [manifest_id, ...]（正文阅读顺序）
    """
    creators = "".join(
        "<dc:creator>%s</dc:creator>" % html.escape(a)
        for a in meta.get("authors") or [])
    langs = "".join(
        "<dc:language>%s</dc:language>" % html.escape(lang)
        for lang in meta.get("languages") or ["zho"])
    subjects = "".join(
        "<dc:subject>%s</dc:subject>" % html.escape(t)
        for t in meta.get("tags") or [])
    items = "".join(
        '<item id="%s" href="%s" media-type="%s"%s/>' % (
            iid, _attr_escape(quote_href(href)), mt,
            ' properties="nav"' if href == "nav.xhtml" else "")
        for iid, href, mt in manifest)
    if cover_name:
        items += ('<item id="cover-image" href="%s" media-type="%s" '
                  'properties="cover-image"/>'
                  % (_attr_escape(quote_href(cover_name)), cover_mt))
    refs = "".join('<itemref idref="%s"/>' % iid for iid in spine)
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    head = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<package version="3.0" xmlns="http://www.idpf.org/2007/opf" '
        'unique-identifier="book-id">',
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">',
        '<dc:identifier id="book-id">%s</dc:identifier>' % uid,
        "<dc:title>%s</dc:title>" % html.escape(meta.get("title") or ""),
        creators,
        "<dc:contributor>MyBooks toolbox</dc:contributor>",
        langs,
        "<dc:description>%s</dc:description>" % html.escape(
            _strip_tags(meta.get("description") or "")),
        subjects,
        '<meta property="dcterms:modified">%s</meta>' % modified,
    ]
    if meta.get("publisher"):
        head.append("<dc:publisher>%s</dc:publisher>"
                    % html.escape(meta["publisher"]))
    head.append("</metadata>")
    head.append("<manifest>%s</manifest>" % items)
    head.append('<spine toc="ncx">%s</spine>' % refs)
    head.append("</package>")
    return "".join(head).encode("utf-8")


def _attr_escape(value: str) -> str:
    return html.escape(value or "", quote=True)


def build_ncx(title: str, nodes: list, uid: str) -> bytes:
    """组装 NCX。

    :param nodes: 嵌套目录节点 [(label, path, frag, [子节点...]), ...]
    """
    order = 0

    def _emit(items):
        nonlocal order
        xml = []
        for label, path, frag, kids in items:
            order += 1
            cur = order
            xml.append(
                '<navPoint id="np%d" playOrder="%d"><navLabel><text>%s</text>'
                '</navLabel><content src="%s"/>%s</navPoint>'
                % (cur, cur, html.escape(label), _attr_escape(quote_toc_src(path, frag)),
                   "".join(_emit(kids))))
        return xml

    navpoints = "".join(_emit(nodes or []))
    head = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">',
        "<head>",
        '<meta name="dtb:uid" content="%s"/>' % _attr_escape(uid),
        '<meta name="dtb:depth" content="3"/>',
        '<meta name="dtb:totalPageCount" content="0"/>',
        '<meta name="dtb:maxPageNumber" content="0"/>',
        "</head>",
        "<docTitle><text>%s</text></docTitle>" % html.escape(title),
        "<navMap>%s</navMap></ncx>" % navpoints,
    ]
    return "".join(head).encode("utf-8")


def build_nav(title: str, nodes: list) -> bytes:
    """生成 EPUB3 导航文档（`properties="nav"`，与 NCX 同源同层级）。"""

    def _emit(items):
        lis = []
        for label, path, frag, kids in items:
            sub = _emit(kids)
            lis.append('<li><a href="%s">%s</a>%s</li>' % (
                _attr_escape(quote_toc_src(path, frag)), html.escape(label),
                ("<ol>%s</ol>" % sub) if sub else ""))
        return "".join(lis)

    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">'
        "<head><title>%s</title></head><body>"
        '<nav epub:type="toc" id="toc"><h1>%s</h1><ol>%s</ol></nav>'
        "</body></html>"
        % (html.escape(title), html.escape(title), _emit(nodes or []))
    ).encode("utf-8")


def validate_epub(data) -> list:
    """校验输出的 EPUB 合法性（入库/返回前守门）。

    :param data: 输出字节（bytes）或文件路径（str/PathLike）。
    检查：mimetype 首项 STORED / container→OPF 可达 / OPF 可解析 /
    manifest id 唯一 / spine idref 全命中 / manifest href 全存在 /
    dc:title 非空 / NCX 良构且 playOrder 唯一。通过返回 warnings（恒为空），
    致命问题抛 ValueError。

    实现抄自 `epub_merge_lib.validate_output`（同款校验口径），此处独立一份，
    以便本模块不反向依赖 epub_merge。
    """
    import os
    if isinstance(data, (str, os.PathLike)):
        source = data
    else:
        source = io.BytesIO(bytes(data))
    try:
        zf = zipfile.ZipFile(source)
    except zipfile.BadZipFile as err:
        raise ValueError("输出不是合法 zip：%s" % err) from err
    with zf:
        return _validate_open_zip(zf)


def _normalize_zip_path(href: str, base_dir: str = "") -> str:
    """href → zip 条目名（unquote + 去 fragment/query + 前导 `/` + `../` 归一）。"""
    if not href:
        return ""
    path = unquote(href.split("#", 1)[0].split("?", 1)[0])
    path = path.lstrip("/")
    if base_dir:
        path = base_dir.rstrip("/") + "/" + path
    parts = []
    for seg in path.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def _validate_open_zip(zf) -> list:
    import xml.etree.ElementTree as _ET

    infos = [i for i in zf.infolist() if not i.is_dir()]
    if not infos or infos[0].filename != "mimetype" \
            or infos[0].compress_type != zipfile.ZIP_STORED:
        raise ValueError("输出 mimetype 缺失或位置/压缩方式不规范")
    names = {i.filename for i in infos}

    def _read(name):
        try:
            return zf.read(name)
        except KeyError:
            raise ValueError("输出缺文件：%s" % name) from None

    def _target(href):
        return _normalize_zip_path(href or "")

    container = _read("META-INF/container.xml").decode("utf-8", errors="replace")
    m = re.search(r"""full-path\s*=\s*["']([^"']+)["']""", container, re.IGNORECASE)
    opf_path = m.group(1) if m else None
    if not opf_path or opf_path not in names:
        raise ValueError("输出缺 OPF 描述文件")
    try:
        root = _ET.fromstring(_read(opf_path))
    except _ET.ParseError as err:
        raise ValueError("输出 OPF 解析失败：%s" % err) from err

    _OPF = "{http://www.idpf.org/2007/opf}"
    _DC = "{http://purl.org/dc/elements/1.1/}"
    title_el = root.find("./%smetadata/%stitle" % (_OPF, _DC))
    if title_el is None or not (title_el.text or "").strip():
        raise ValueError("输出缺书名（metadata 命名空间异常？）")

    man_ids = set()
    for item in root.findall("./%smanifest/%sitem" % (_OPF, _OPF)):
        iid, href = item.get("id"), item.get("href")
        if not iid or not href:
            raise ValueError("输出 manifest 条目缺 id/href")
        if iid in man_ids:
            raise ValueError("输出 manifest id 重复：%s" % iid)
        man_ids.add(iid)
        if _target(href) not in names:
            raise ValueError("输出 manifest 引用缺失：%s" % href)

    spine_el = root.find("./%sspine" % _OPF)
    if spine_el is None:
        raise ValueError("输出缺 spine")
    toc_id = spine_el.get("toc")
    if toc_id and toc_id not in man_ids:
        raise ValueError("输出 spine toc 指向不明：%s" % toc_id)
    for ref in spine_el.findall("./%sitemref" % _OPF):
        if ref.get("idref") not in man_ids:
            raise ValueError("输出 spine 引用不明：%s" % ref.get("idref"))

    ncx_name = next((i.get("href") for i in root.findall("./%smanifest/%sitem" % (_OPF, _OPF))
                     if (i.get("href") or "").lower().endswith(".ncx")), None)
    if ncx_name:
        try:
            ncx_root = _ET.fromstring(_read(_target(ncx_name)))
        except _ET.ParseError as err:
            raise ValueError("输出 NCX 解析失败：%s" % err) from err
        _NCX = "{http://www.daisy.org/z3986/2005/ncx/}"
        orders = []
        for np in ncx_root.iter(_NCX + "navPoint"):
            try:
                orders.append(int(np.get("playOrder")))
            except (TypeError, ValueError):
                raise ValueError("输出 NCX playOrder 非法") from None
            content = np.find(_NCX + "content")
            src = _target((content.get("src") or "").split("#", 1)[0]) if content is not None else ""
            if src and src not in names:
                raise ValueError("输出 NCX 引用缺失：%s" % src)
        if len(set(orders)) != len(orders):
            raise ValueError("输出 NCX playOrder 重复")
    return []
