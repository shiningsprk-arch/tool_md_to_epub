# -*- coding: utf-8 -*-
"""Markdown → EPUB3：MyBooks 外部工具（Toolbox 工具包）后端。

manifest.json 里 `entry_backend` 指向本模块的 `MdToEpubTool`，`api_routes` 指向
`ConvertHandler` / `DownloadHandler`；宿主 toolbox_manager 把它们挂到：

    POST /api/toolbox/tool/md_to_epub/convert
    GET  /api/toolbox/tool/md_to_epub/download

两个注意点（外部工具与内置工具的差别）：

1. 宿主用 `collect_tool_routes()` 动态挂路由时，只额外包一层"工具被禁用就 404"的
   `prepare()`，**不会注入任何鉴权装饰器**（内置工具的路由是在 handlers/toolbox.py 里
   手写并自带 `@js @is_admin` 的）。所以鉴权必须在这里显式写上。
2. `api_routes[].path` 是正则片段，前缀 `/api/toolbox/tool/<tool_id>/` 由宿主拼，
   本模块不能自己写死完整路由。

转换逻辑是纯函数，放在同目录的 `md_to_epub_lib`（不依赖 webserver / calibre，可单测）；
本模块只负责工作目录、请求解析、线程池调度、产物入库与回传。
"""
import functools
import logging
import os
import re
import shutil
import time
import uuid
from typing import Optional

import tornado.escape
import tornado.ioloop

from webserver.handlers.base import BaseHandler, is_admin, js
from webserver.i18n import _
from webserver.services import AsyncService
from webserver.toolbox.base_tool import BaseTool

from . import md_to_epub_lib
from .md_to_epub_lib import MarkdownConvertError

_TOOL_WORK_TTL = 24 * 3600  # 工具产物保留 24h，超时由新任务触发惰性回收

# 与 manifest.json 的 api_routes 对应；宿主固定挂在 /api/toolbox/tool/<tool_id>/ 下
_API_ROOT = "/api/toolbox/tool/md_to_epub"

_SAFE_TOKEN_RE = re.compile(r"^[a-f0-9]{16}$")


def is_valid_token(token: str) -> bool:
    """产物 token 形状校验（download 路由用它挡路径穿越）。"""
    return bool(token and _SAFE_TOKEN_RE.match(token))


class MdToEpubTool(BaseTool):
    """将 Markdown 目录/zip 转换为 EPUB3 供下载。"""

    service_item_name = "Markdown转EPUB"

    # 与仓库根 manifest.json 的对应字段保持一致（tests/test_package_layout.py 会校验）
    @staticmethod
    def info() -> dict:
        return {
            "tool_id": "md_to_epub",
            "name": "Markdown转EPUB",
            "description": "将 Markdown（支持同目录图片、目录或 zip 上传）转换为 EPUB3 电子书",
            "revision": "1.1.0",
            "author": "黏菌",
            "publish_date": "2026-09-15",
            "repo_url": "https://github.com/shiningsprk-arch/tool_md_to_epub",
        }

    # ------------------------------------------------------------ 工作目录

    def new_work_dir(self) -> tuple:
        """分配一个新的任务工作目录，返回 (token, work_dir)。"""
        token = uuid.uuid4().hex[:16]
        self._gc_old_work_dirs()
        return token, self.api.storage.get_work_dir(token)

    def _gc_old_work_dirs(self) -> None:
        """惰性回收超过 TTL 的历史产物（失败静默，不影响当前任务）。"""
        try:
            root = self.api.storage.get_work_dir()  # 不带 key = 该工具的根目录
            if not os.path.isdir(root):
                return
            now = time.time()
            for name in os.listdir(root):
                path = os.path.join(root, name)
                if not os.path.isdir(path):
                    continue
                try:
                    if now - os.path.getmtime(path) > _TOOL_WORK_TTL:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    continue
        except OSError as err:
            logging.warning("[MdToEpubTool] GC failed: %s", err)

    # ---------------------------------------------------------------- 转换

    def build_epub(self, staging_dir: str, out_path: str, ignore_images: bool,
                   title: Optional[str] = None, author: Optional[str] = None) -> dict:
        """转换 staging_dir 下的 Markdown 并写出 out_path（在调用方线程执行）。"""
        result = md_to_epub_lib.convert(
            staging_dir, ignore_images=ignore_images, title=title or None, author=author or None)
        with open(out_path, "wb") as f:
            f.write(result.pop("epub"))
        return result

    # ---------------------------------------------------------------- 入库

    @AsyncService.register_function
    def import_epub(self, user_id: int, epub_path: str, title: str, author: str) -> int:
        """把产物 epub 导入书库，返回 Calibre book_id。

        必须用 `register_function`（同步变体）：它只做 `setup(db, scoped_session)` 注入后直接
        调用。不能用 `register_service`——它当前实现里 `async_mode()` 恒为 True，调用会被丢进
        后台队列并返回 None，拿不到 book_id；也不能绕开装饰器直接调 `self.db`，未注入时它是
        None（会在 `import_file` 内部炸）。

        `delete_after_import=False`：产物仍要供下载链接使用，工作目录交给 24h 惰性 GC 回收。
        入库落的是 Calibre 书库副本，删不删源文件都不影响已入库的书。
        """
        return self.api.calibre.import_file(
            user_id, epub_path, title or "", [author] if author else [],
            delete_after_import=False,
        )


class ConvertHandler(BaseHandler):
    """POST /api/toolbox/tool/md_to_epub/convert —— multipart 上传、转换，并按开关入库。

    默认入库（`import_to_library` 缺省视为开）；入库失败与转换失败是两回事——转换成功但入库
    失败时返回 `{"err": "import.failed", "msg": <宿主原因>, "data": {...}}`，`data` 照常带上
    下载链接，前端既提示失败也保留下载入口，不白转一趟。

    转换与入库都在线程池里跑（Calibre 入库同步且可能较慢），避免阻塞 IOLoop。
    返回 `{"err": <错误码>, "msg": <中文兜底>}`；前端用错误码查自己的三语文案。
    """

    @js
    @is_admin
    async def post(self):
        files = self.request.files.get("files", [])
        if not files:
            return {"err": "params.missing", "msg": _("未选择文件")}
        rel_paths = self.get_arguments("relative_paths")
        ignore_images = self.get_argument("ignore_images", "0").lower() in ("1", "true", "yes")
        import_to_library = self.get_argument("import_to_library", "1").lower() in ("1", "true", "yes")
        title = (self.get_argument("title", "") or "").strip()
        author = (self.get_argument("author", "") or "").strip()

        tool = MdToEpubTool()
        loop = tornado.ioloop.IOLoop.current()
        token, work_dir = tool.new_work_dir()
        staging = os.path.join(work_dir, "src")
        out_path = os.path.join(work_dir, "out.epub")
        try:
            os.makedirs(staging, exist_ok=True)
            _stage_uploads(staging, files, rel_paths)
            if not md_to_epub_lib.find_markdown_files(staging):
                raise MarkdownConvertError(
                    "no_markdown", _("未找到 Markdown 文件（.md / .markdown）"))
            result = await loop.run_in_executor(
                None, functools.partial(
                    tool.build_epub, staging, out_path, ignore_images, title, author))
        except MarkdownConvertError as err:
            shutil.rmtree(work_dir, ignore_errors=True)
            return {"err": err.code, "msg": str(err)}
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        filename = "%s.epub" % (title or result.get("title") or "book")
        data = {
            "token": token,
            "filename": filename,
            "download_url": "%s/download?token=%s&name=%s" % (
                _API_ROOT, token, tornado.escape.url_escape(filename)),
            "title": result.get("title"),
            "image_count": result.get("image_count", 0),
            "chapter_count": result.get("chapter_count", 0),
            "warnings": result.get("warnings", []),
            "imported": False,
            "book_id": None,
        }

        if import_to_library:
            # 用转换结果里的书名/作者（而不是表单原始值），保证书库元数据与 epub 内嵌一致
            try:
                data["book_id"] = await loop.run_in_executor(
                    None, functools.partial(
                        tool.import_epub, self.current_user.id, out_path,
                        result.get("title") or "", result.get("author") or ""))
                data["imported"] = True
            except Exception as err:
                logging.warning("[MdToEpubTool] 入库失败: %s", err)
                return {"err": "import.failed", "msg": str(err), "data": data}

        return {"err": "ok", "msg": _("转换成功"), "data": data}


class DownloadHandler(BaseHandler):
    """GET /api/toolbox/tool/md_to_epub/download?token=..&name=.. —— 回传产物。

    这里是文件字节流而不是 JSON 信封，所以**不能**用 `@js`；而 `@is_admin` 在非管理员
    情况下返回的是 dict，没有 `@js` 兜底会被 Tornado 当成"返回值不为 None"报 500。
    因此按宿主 handlers/toolbox.py 里 `AdminEpubBeautifyBgRaw` 的既有做法：显式判权 +
    显式写出响应（前置检查与 `is_admin` 装饰器用的是同一对 `current_user`/`admin_user`）。
    """

    def get(self):
        if not self.current_user:
            self._deny(401, _("请先登录"))
            return
        if not self.admin_user:
            self._deny(403, _("当前用户非管理员, 无权限操作"))
            return

        token = self.get_argument("token", "")
        if not is_valid_token(token):
            self._deny(400, "Bad token")
            return
        tool = MdToEpubTool()
        path = os.path.join(tool.api.storage.get_work_dir(token), "out.epub")
        if not os.path.isfile(path):
            self._deny(404, "File not found")
            return

        name = os.path.basename(self.get_argument("name", "") or "book.epub")
        if not name.lower().endswith(".epub"):
            name += ".epub"
        self.set_header("Content-Type", "application/epub+zip")
        self.set_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''%s" % tornado.escape.url_escape(name, plus=False))
        with open(path, "rb") as f:
            self.write(f.read())

    def _deny(self, status: int, message: str) -> None:
        self.set_status(status)
        self.set_header("Content-Type", "text/plain; charset=utf-8")
        self.write(message)


def _stage_uploads(staging: str, files: list, rel_paths: list) -> None:
    """把上传文件按相对路径落到暂存目录；`.zip` 走安全解压。

    `files[i]` 与 `relative_paths[i]` 按下标对齐（前端按同一顺序 append）。
    """
    staging_real = os.path.realpath(staging)
    for idx, fileinfo in enumerate(files):
        filename = fileinfo.get("filename") or ""
        rel = rel_paths[idx] if idx < len(rel_paths) and rel_paths[idx] else filename
        rel = "/".join(p for p in rel.replace("\\", "/").split("/")
                       if p not in ("", ".", ".."))
        if not rel:
            continue
        ext = rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        if ext == "zip":
            tmp_zip = os.path.join(staging_real, "__upload_%d.zip" % idx)
            with open(tmp_zip, "wb") as f:
                f.write(fileinfo["body"])
            try:
                md_to_epub_lib.safe_extract_zip(tmp_zip, staging_real)
            finally:
                if os.path.exists(tmp_zip):
                    os.remove(tmp_zip)
            continue
        target = os.path.realpath(os.path.join(staging_real, rel))
        if target != staging_real and not target.startswith(staging_real + os.sep):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(fileinfo["body"])
