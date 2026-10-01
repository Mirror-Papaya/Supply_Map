# 库存地图（Supply_Map）— 项目交接说明

> 给在另一台电脑上继续开发的 Claude / 开发者。与用户全程用**中文**交流。

## 1. 项目目标

五金电商商家，SKU 多、库位分散复杂。目标是建一张"库存地图"，靠**飞书机器人 + 本地电脑部署**协同完成拣货定位：

1. **登记**：通过飞书表单（多维表格）和飞书聊天留言登记库位，内容包括 SKU、库位码、当前余量（选填）。系统自动记录登记时间、登记方式和登记人。
2. **查询**：在飞书聊天里发拣货单图片、Excel/CSV 或文字，机器人返回每个商品的库位和余量。结果有两种形式：**表格**（卡片表格）和**图像**（高亮货架、标出拣货顺序的定位图）。
3. **地图编辑**：一个独立的网格化编辑器，用来定义仓库结构。
   - 四类地图区块：功能、库存（货架）、走廊、未定义。
   - 功能区块可以加文字标注，每个货架有唯一编码。
   - "仓库区块"（字母）比地图区块高一级，以半透明覆盖显示，可以开关。
   - 图片尺寸和网格大小可以自定义。

完整需求清单见 [docs/需求清单.md](docs/需求清单.md)。

## 2. 重要决定（已与用户确认，不要擅自改动）

| 主题 | 决定 |
|---|---|
| **库位码** | 5 段，用 `-` 连接：`仓库号(2位数字)-仓库区块(大写字母)-货架编码(2位数字)-层编码(1位数字)-包装备注编码(1位数字)`，如 `01-A-03-2-1`。输入时容忍 `01A0321`、小写、全角字符。 |
| 层编码 | **允许 0**（0–9）。每个货架有"可用层号范围" `layer_min`–`layer_max`。 |
| 包装备注编码 | **含义暂未定**。只校验是 0–9 的数字；含义字典（`pack_code` 表）可以在管理页随时补。 |
| 编码对应关系 | 一个仓库号对应一张地图。仓库区块 = 地图上的字母覆盖层。货架 = 库存区块，编码在"同仓库 + 同区块"内唯一。前 4 段是物理位置，第 5 段不上地图。 |
| **余量** | 余量是**盘点数**，**选填**。登记**只追加日志，从不覆盖**。查询时取该 SKU 在该库位**最近一条填了余量的登记**，并显示盘点日期和盘点人；从来没填过就留空。所以**不做"拣货完成自动扣减"**。 |
| 移除 | 新增"移除"登记，表示 SKU 已离开该库位。移除之后再登记，余量从新登记开始算。 |
| **拣货单输入** | 目前**只有图片**，图片识别是主通道。ERP 服务商以后可能提供 Excel/CSV，这条通道已经实现并保留。所有输入都先转成统一的"拣货需求"`[{sku, qty, name, alt}]`。 |
| 图片识别 | 用 **Claude 视觉模型**（用户选定）。模型 `claude-opus-5`，开启服务端 `fallbacks: "default"`（beta `server-side-fallback-2026-07-01`），`effort=medium`，三者都可以在 `.env` 里改。Claude 只负责"照抄"，SKU 纠错由本地 `matcher` 对照商品字典做闭集匹配。 |
| 架构 | 本地 **SQLite 是唯一数据源**；多维表格只是登记输入渠道。飞书用**长连接（WebSocket）**，本地电脑不需要公网 IP。 |
| 飞书管理员 | 用户本人，权限审批可以自己完成。 |
| 排期方式 | 按 **Claude 连续工作的小时数**估算，**不要按人类工作日排期**（用户明确要求过）。用户侧的环节（飞书后台操作、提供材料、现场实测、画地图）单独列出，它们才是瓶颈。 |
| SKU 编码 | 用户正在另一个项目里重建 SKU 编码规范。商品字典支持"别名"列（旧码 / 平台码），以兼容新旧编码切换。 |
| MVP 范围 | 二期再做：卡片表单登记、写入前确认卡片、权限分级、地图版本历史、安全库存预警、对接 ERP 的 API、修改网格尺寸时自动迁移数据。 |

## 3. 当前进度（截至 2026-10-01）

**已完成（代码全部写完，29 项 pytest 通过）**
- 地图编辑器（`/editor`）：在浏览器里实测过新建仓库、划区块、画货架、写标注、保存、重新载入、导出图片。
- 本地管理页（`/`）：库位查询（附定位图）、SKU 字典导入、包装编码设置、登记日志、服务状态。查询功能已实测。
- 登记、查询、匹配纠错、定位图渲染、Excel 导出：有单元测试。
- 飞书机器人（长连接、对话逻辑、卡片、文本兜底）、多维表格同步、Claude 识别：用模拟数据做了单元测试。
- 配置工具 `run_setup.py`、启动脚本 `start.bat`、用户手册 `使用说明.md`。

**尚未在真实环境验证**
- 飞书真实连接，以及卡片 JSON 2.0（`table` / `options` 列）在飞书里的显示效果。
- Claude 对真实拣货单的识别准确率。
- 多维表格的建表 API（`create_table`，尤其是表单视图）和同步回写。

**用户侧状态**：`.env` 还没填。用户刚问过怎么获取 App ID / Secret / Anthropic Key，已经给了步骤。数据库是空的（测试数据已删除）。

## 4. 待办事项（按顺序）

1. 用户填写 `.env`（`FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `ANTHROPIC_API_KEY`），然后运行 `python run_setup.py check`。
2. 飞书后台配置：开通权限、设置通讯录权限范围为全员、**先启动程序**再选"长连接"订阅 `im.message.receive_v1`、发布版本。步骤见 `使用说明.md` 第三节。
   - 如果用户用的是国际版 **Lark**（larksuite.com），需要给 `lark.Client.builder()` 和 `lark.ws.Client` 加 `domain=lark.LARK_DOMAIN`。
3. 多维表格：用户新建多维表格并添加应用（可编辑），运行 `python run_setup.py bitable <链接>`，确认表单视图是否创建成功。
4. 用户提供 20–50 张真实拣货单图片和 SKU 清单：导入字典，测识别率（目标 ≥95%），按需调整 `vision.PROMPT`、`CLAUDE_EFFORT`、切图参数。
5. 在飞书里实测卡片显示；如果 `table` 组件报错，调整 `cards.result_card`（已有文本兜底）。
6. 用户画真实仓库地图。可以让用户发手绘图照片，由 Claude 生成地图 JSON 初稿。
7. 开机自启：在启动文件夹放 `start.bat` 的快捷方式。**这是持久化配置，必须先征得用户同意。**
8. 二期功能（见第 2 节"MVP 范围"）。

## 5. 运行与开发

```bash
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python -m pytest -q
.venv\Scripts\python run.py
```

- 运行 `run.py` 后，浏览器打开 http://127.0.0.1:8765 （编辑器在 `/editor`）。也可以双击 `start.bat`。
- 没配飞书密钥时只启动本地网页；配了就同时启动长连接，以及多维表格同步线程（配置了表格时）。
- 配置工具：`python run_setup.py check | bitable <链接> | chats`。
- 运行数据都在 `data/`（数据库、每日备份、日志、底图），已加入 `.gitignore`。
- 定位图中文字体默认用 `C:\Windows\Fonts\msyh.ttc`；非 Windows 系统要在 `.env` 设置 `FONT_PATH`。
- `.claude/launch.json` 用于 Claude Code 的预览功能（`preview_start name=warehouse-map`）。

## 6. 架构与数据流

```
飞书：多维表格表单 ──(轮询 search，20s)──┐
飞书：机器人消息 ──(长连接 WebSocket)──┐  │
                                    ▼  ▼
  feishu/gateway.py → bot.py ─→ parse_text / vision(Claude) / skus.read_table
                         │            ↓ 拣货需求 [{sku,qty,name,alt}]
                         │        query.py ─ matcher.py（字典纠错）─ stock.locations_of
                         │            ↓
                         └──→ cards.py（卡片/文本/Excel） + render.py（定位图 PNG）
  feishu/bitable_sync.py → stock.apply（登记/移除，只追加日志）
  web.py（FastAPI）→ 地图编辑器 / 管理页 → maps.py（校验、保存、生成 zone/shelf 表）
  db.py（SQLite）：warehouse / zone / shelf / pack_code / sku / stock_log / current_stock / kv / processed_msg
```

- `stock_log` 是唯一的事实来源，只追加。`current_stock` 是从日志推导出的缓存，`stock.rebuild_current()` 可以全量重算。
- 地图 JSON 格式见 `app/maps.py` 文件头的说明。格子按行存成字符串或数组，`blocks` 存区块属性，`zone_meta` 存区块颜色。
- 保存地图时：如果被删除或改编码的货架上还有库存登记，或者缩减层号后有库存落在范围外，**拒绝保存**。

## 7. 文件说明

| 路径 | 说明 |
|---|---|
| `run.py` | 程序入口（可以在任意目录运行） |
| `run_setup.py` | 配置工具入口：检查 / 建多维表格 / 列出群 |
| `start.bat` | Windows 双击启动（没有 `.env` 时自动从模板复制） |
| `.env.example` | 配置模板（真实 `.env` 不进仓库） |
| `requirements.txt` | Python 依赖 |
| `使用说明.md` | 给用户看的操作手册（启动、画地图、飞书配置、多维表格、机器人用法） |
| `docs/需求清单.md` | 完整需求清单 v2 与决策记录 |
| `app/config.py` | 读取 `.env`；数据目录、模型、轮询间隔等 |
| `app/db.py` | SQLite 表结构、事务、消息去重、备份 |
| `app/codes.py` | 5 段库位码的解析、规范化和分段拼接 |
| `app/maps.py` | 地图 JSON：分析、校验、保存（同步 zone/shelf 表）、删除 |
| `app/stock.py` | 登记 / 移除 / 校验 / 当前库位 / 日志 / 统计 |
| `app/skus.py` | 商品字典（含别名）；Excel/CSV 读取与表头识别（字典导入和拣货单共用） |
| `app/matcher.py` | SKU 匹配：精确 → 去符号 → 易混字符(O/0、I/1…) → 相似度 → 品名 |
| `app/query.py` | 拣货需求 → 库位、余量、拣货顺序（按货架编号）、异常标记 |
| `app/render.py` | 地图 PNG 渲染（区块覆盖、高亮货架、序号徽标） |
| `app/cards.py` | 飞书卡片 JSON 2.0 / 文本兜底 / Excel 附件 |
| `app/vision.py` | Claude 读图（长截图切段、结构化输出、错误处理） |
| `app/parse_text.py` | 聊天指令解析（登记 / 移除 / 查询 / 帮助）和帮助文本 |
| `app/bot.py` | 对话逻辑（不依赖飞书 SDK）：文本 / 图片 / 富文本 / 文件 |
| `app/feishu/client.py` | 飞书 API 封装：回复、上传、下载资源、用户姓名、机器人 open_id |
| `app/feishu/gateway.py` | 长连接与事件分发（线程池、去重、状态钩子；绕过 SDK 模块级事件循环的处理） |
| `app/feishu/bitable_sync.py` | 多维表格轮询同步与回写、失败私信、建表（含表单视图） |
| `app/setup_tool.py` | `run_setup.py` 的实现 |
| `app/web.py` | FastAPI：页面、地图 API、渲染、查询、日志、字典导入、包装编码 |
| `app/main.py` | 启动 Web、飞书线程、同步线程、每日备份线程 |
| `app/status.py` | 运行状态（显示在首页） |
| `web/editor.html` `editor.js` `editor.css` | 地图编辑器（原生 JS + Canvas，不需要构建） |
| `web/index.html` `common.css` | 管理首页 |
| `tests/` | pytest：核心逻辑、机器人（模拟 Responder）、识别预处理 |

## 8. 注意事项

- `lark_oapi.ws.client` 在模块加载时就持有一个事件循环。在子线程里运行前，要先把 `wsc.loop` 换成本线程新建的循环（见 `gateway.run_forever`）。
- 飞书事件可能重复推送，用 `processed_msg` 按 message_id 去重，处理逻辑放进线程池，立即返回。
- 多维表格同步会跳过 20 秒内被编辑过的记录，以及空行，避免把正在填写的行判为失败。想重试时清空"处理状态"即可。
- 群聊里的文字需要 @机器人；图片和文件在拣货群（`PICK_CHAT_IDS`，留空表示所有群）里直接发就会识别。
- 修改 Claude API 相关代码前，先确认当前模型 ID 和参数写法（API 变化较快）。
- **不要提交** `.env`、`data/`、密钥或用户的业务数据。推送、发消息这类对外操作，要先征得用户确认。
