# tool_md_to_epub

MyBooks 工具箱**外部工具包**：把 Markdown 转换成 EPUB3 电子书。

支持三种输入：单个/多个 `.md`、整个目录（`webkitdirectory`，保留相对路径以便内嵌同目录图片）、
以及 `.zip`。**转换后默认导入书库**，同时保留产物供下载；入库可以在界面上关掉。

本仓库是独立发布单元（不是内置工具）：代码由使用者上传工具包安装，不打进 MyBooks 主仓库。

## 目录结构

```
tool_md_to_epub/
├── manifest.json            # 工具元数据（tool_id / entry_backend / api_routes ...）
├── .toolbuilder.json        # mytool build 配置：前端原生静态资源，无构建步骤
├── icon.png                 # 工具图标（动态工具只认包根的 icon.png，且必须是 PNG）
├── assets/
│   └── icon_source.jpg      # 图标原图（make_icon.py 的输入，不打进包）
├── backend/
│   ├── __init__.py          # 让 backend/ 成为包，工具内模块才能相对导入
│   ├── tool.py              # MdToEpubTool + ConvertHandler + DownloadHandler
│   ├── md_to_epub_lib.py    # 转换引擎（纯函数，可脱离 MyBooks 单测）
│   └── epub_writer.py       # EPUB3 打包器（OPF / nav / NCX / mimetype STORED），纯标准库
├── frontend/                # iframe 内运行的自包含静态页
│   ├── index.html
│   ├── app.js
│   ├── lib/{theme.css,i18n.js}    # 脚手架自带的共享样式与 i18n 胶水（未改动）
│   └── locales/{manifest,zh,en,zh-TW}.json
├── scripts/
│   ├── build.py             # 推荐构建入口：清理字节码 → mytool build → 校验 zip 形状
│   └── make_icon.py         # 从 assets/icon_source.jpg 生成 icon.png（1024²，中心裁方形）
└── tests/
    ├── test_md_to_epub_core.py    # 转换引擎单测（含依赖前置检查）
    └── test_package_layout.py     # 工具包契约守卫（manifest / 结构 / 鉴权 / 入库接线 / i18n 键）
```

## 安装与生效

1. 管理员在「系统管理 → 系统设置 → 高级配置项」打开 `ENABLE_TOOLBOX_DEV_MODE`。
2. 「系统管理 → 工具箱」上传打包产物 zip（`POST /api/toolbox/install/upload`）。
3. **重启 MyBooks 服务**才会真正加载（工具的 import 与路由挂载只在进程启动时执行一次，
   卸载同理）。
4. 打开 `/toolbox/md_to_epub` 使用。

安装之后，管理员对该工具的**禁用/启用是即时生效的**（不需要重启）：禁用后工具从列表消失、
`/api/toolbox/tool/md_to_epub/*` 直接 404。

> `tool_id` 是安装的唯一键。如果目标实例里已经存在同名的**内置**工具（本工具没有内置版，
> 不会出现这种情况），安装会被拒绝，只能走「更新」成为 builtin override。

## 构建

```bash
npm install -g mybooks-tools-builder
mytool validate .            # 只校验 manifest 与目录结构
python scripts/build.py      # 推荐：清理字节码 → mytool build → 校验 zip 形状
```

前端是原生 HTML/CSS/JS，没有构建步骤（`.toolbuilder.json` 的两项都是 `null`），打包就是把
`frontend/` 原样放进 zip。

`scripts/build.py` 除清字节码外还会校验产物形状（根目录必须有 `manifest.json`、不得出现
`__pycache__`/`.pyc`/`.DS_Store`/越界顶层条目、必需文件齐全），不通过就非零退出。

手工打包（没有 Node 时）也可以，但 zip **根目录**必须是 `manifest.json` / `backend/` /
`frontend/` / `icon.png`——不能套一层文件夹，宿主 `_read_manifest()` 只读归档根；
同时要排除 `__pycache__/` 与 `*.pyc`。

## 运行前提

工具后端跑在 MyBooks 进程内，**所有三方依赖都由宿主镜像提供**，包内不带任何副本：

| 组件 | 用途 | 缺失时 |
|---|---|---|
| `markdown-it-py`（实测 4.2.0） | Markdown 渲染 | 转换返回 `deps.missing` |
| `mdit-py-plugins`（实测 0.6.1） | 脚注 / 任务列表 / deflist 扩展语法 | 静默降级为 CommonMark |
| `beautifulsoup4` + `lxml` | 渲染后 HTML 的 `<img>` 重写 | 转换返回 `deps.missing` |
| `Pillow` | 图片校验与缩放 | 返回 `image.pil_missing` |

`bs4` / `lxml` / `Pillow` 是 MyBooks 现有依赖；`markdown-it-py` / `mdit-py-plugins` 需要镜像
内置。缺失不会变成 500，而是返回可诊断的错误码（前端会提示"宿主缺少依赖"并列出包名）。

## 行为约定

- **转换后默认导入书库**（`import_to_library` 缺省为开，界面可关）：**入库失败不会连带丢掉
  转换结果**——接口返回 `import.failed` 并照常带回下载链接，界面同时提示失败与下载入口，不白转
  一趟。入库归属当前操作的管理员，书库元数据取转换结果里的书名/作者（与 epub 内嵌一致）；
- **远程图片一律移除**，绝不联网抓取（隐私 / SSRF / 离线环境），命中数量在界面提示；
- **本地相对图片缺失或越界 → 报错**，不会静默丢图；提示改用「目录 / zip 上传」保留图片目录
  结构，或勾选「不导入图片」（此时图片替换为 alt 文本）；
- 渲染器关闭内嵌 HTML（`html=False`），原文里的原始 HTML 标签会被转义为文本；
- 限额：单个 md 32 MB、单张图片 20 MB、图片总量 256 MB / 3000 张、图片宽度 >1600px 等比缩小、
  zip 5000 项 / 解压后 1 GB；产物在工具数据目录保留 24h，由后续任务惰性回收；
- 整包上传大小还受宿主 Tornado 请求体上限约束——**大图册建议用 zip 输入**。

## 后端接口

`manifest.json` 的 `api_routes` 由宿主挂载到 `/api/toolbox/tool/md_to_epub/<path>`：

| 路由 | 方法 | 说明 |
|---|---|---|
| `convert` | POST | multipart：`files`（可重复）、`relative_paths`（可重复，与 files 下标对齐）、`ignore_images`、`import_to_library`（缺省为开）、`title`、`author`；返回 `{"err": <错误码>, "msg": <中文兜底>, "data": {...}}`；`data` 含 `imported` / `book_id` 与 `download_url` |
| `download` | GET | `?token=<16 位 hex>&name=<文件名>`，回传 `application/epub+zip` |

两个路由都是**管理员限定**。宿主在挂载外部工具路由时只包一层"工具被禁用则 404"，
不注入任何鉴权装饰器，所以 `@js` / `@is_admin` 写在 `backend/tool.py` 里；
`download` 返回的是文件字节流，用不了 `@js`，因此按宿主 `AdminEpubBeautifyBgRaw` 的既有做法
显式判权 + 显式写出响应。

入库调用链有个**必须踩准的点**：`import_epub()` 用 `@AsyncService.register_function` 装饰。
宿主 `register_service` 的 `async_mode()` 恒为 True，调用会被丢进后台队列并返回 None，拿不到
`book_id`；而完全不加装饰器时 `self.db` 还没被注入（None），会在 `import_file` 内部崩。
只有 `register_function` 是"先 `setup(db, scoped_session)` 再同步调用"。
`tests/test_package_layout.py` 用 AST 把这条钉住了。

## 前端

- iframe 页面由宿主 `/get/tool/md_to_epub/index.html` 提供，入口固定 `frontend/index.html`；
- 与宿主通信只经 `MyBooksToolBridge`（`/static/toolbox-bridge.js`）：`toolId` / `theme` /
  `locale` / `fetch` / `notify`；
- 上传用 `bridge.fetch('convert', {method:'POST', body: FormData})`（不手动设 `Content-Type`，
  boundary 交给浏览器）；该用法宿主文档未覆盖，因此失败时回退到裸 `fetch`；
- 文案三语（zh / en / zh-TW）随包提供，跟着 `bridge.locale` 自动切换；后端错误码 →
  `error.<code>` 文案，缺译时回落到后端中文 `msg`；
- 深浅色跟随宿主，靠 `<body data-theme>` + `lib/theme.css` 的 CSS 变量。

## 测试

```bash
python tests/test_md_to_epub_core.py     # 转换引擎，18 个用例
python tests/test_package_layout.py      # 工具包契约，25 个用例
pytest tests/                            # 一次跑完
```

`test_md_to_epub_core.py` 覆盖 front matter、标题作者、本地图片内嵌、远程图片移除、缺失图片 /
路径穿越 / 绝对路径报错、忽略图片模式、data URI、Obsidian 语法、多文件自然排序、EPUB 结构守门、
zip 安全解压、宿主依赖前置检查。

`test_package_layout.py` 守的是「`mytool validate` 管不到、但一旦破就会在真实安装后炸掉」的约定：
manifest 必填字段与格式、`info()` 与 manifest 一致、`backend/__init__.py` 与 entry 模块存在、
`icon.png` 是正方形 PNG、**handler 的 `@js`/`@is_admin` 没被漏掉**（宿主不注入鉴权）、
**入库走的是 `register_function` 且默认开**（见「后端接口」里的说明）、后端每个错误码都有三语
文案、三语 key 集合一致且 HTML/JS 引用的 key 都存在、包内没夹带依赖副本。

两组测试都**不需要** MyBooks 环境与 calibre。

## 与内置版本的关系

转换引擎与 EPUB 打包器来自 MyBooks 内置实现分支 `feat/md-to-epub-20260910`（从未推上游），
本次改造把 HTTP 层、页面与文案按外部工具契约重写：

- 路由从内置的 `/api/toolbox/md_to_epub/*`（写在 `webserver/handlers/toolbox.py`）改成
  `manifest.json` 的 `api_routes`；
- 页面从 Vuetify 2 的 `.vue`（随宿主 App 构建）改成 iframe 内自包含静态页；
- 文案从宿主 `app/locales/*.json`（外部工具无法写入）搬到包内 `frontend/locales/*.json`；
- `md_to_epub_lib.py` 除相对导入与新增的 `check_dependencies()` 外与内置版逐行一致；
  `epub_writer.py` 逐字节一致。

内置版不再向 MyBooks 提交 PR——`tool_id` 保持 `md_to_epub`，避免与内置工具重名导致安装被拒。

## 许可

MIT，见 [LICENSE](LICENSE)。作者：黏菌。
