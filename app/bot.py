"""机器人对话逻辑（与飞书 SDK 解耦，方便测试）。

消息类型：
- 文本：登记 / 移除 / 查询 / 帮助；群聊里需要 @机器人
- 图片 / 富文本里的图片：识别拣货单后查询（拣货群里直接发即可）
- 文件：Excel / CSV 拣货单
"""
import logging
import re
from dataclasses import dataclass, field

from . import cards, config, maps, parse_text, query, render, skus, stock, vision

log = logging.getLogger("bot")


@dataclass
class Incoming:
    message_id: str
    chat_id: str
    chat_type: str            # p2p / group
    msg_type: str             # text / image / post / file ...
    content: dict
    sender_id: str = ""
    sender_name: str = ""
    created_at: str = ""      # 消息发送时间 "YYYY-MM-DD HH:MM:SS"（本地时间），空则用处理时间
    mentioned_bot: bool = False
    extra: dict = field(default_factory=dict)


class Responder:
    """由接入层实现：回复文本/卡片、上传图片/文件、下载消息里的资源。"""

    def text(self, text: str): raise NotImplementedError
    def card(self, card: dict): raise NotImplementedError
    def file(self, data: bytes, name: str): raise NotImplementedError
    def image(self, image_key: str): raise NotImplementedError
    def upload_image(self, png: bytes) -> str: raise NotImplementedError
    def download(self, key: str, kind: str) -> bytes: raise NotImplementedError


def handle(inc: Incoming, r: Responder):
    group = inc.chat_type == "group"
    if inc.msg_type == "text":
        text = inc.content.get("text", "")
        if group and not inc.mentioned_bot:
            return
        return _handle_text(inc, r, text)
    if inc.msg_type == "image":
        if group and not _pick_group(inc):
            return
        return _handle_images(inc, r, [inc.content.get("image_key")])
    if inc.msg_type == "post":
        images, text = _post_parts(inc.content)
        if images:
            if group and not (_pick_group(inc) or inc.mentioned_bot):
                return
            return _handle_images(inc, r, images)
        if group and not inc.mentioned_bot:
            return
        return _handle_text(inc, r, text)
    if inc.msg_type == "file":
        if group and not _pick_group(inc):
            return
        return _handle_file(inc, r)
    if not group:
        r.text(parse_text.HELP_TEXT)


def _pick_group(inc: Incoming) -> bool:
    return not config.PICK_CHAT_IDS or inc.chat_id in config.PICK_CHAT_IDS


def _post_parts(content: dict):
    """富文本：取出所有图片 key 和文字。content 可能是 {"title","content"} 或按语言包一层。"""
    body = content
    if "content" not in body:
        body = next((v for v in content.values() if isinstance(v, dict) and "content" in v), {})
    images, texts = [], [body.get("title") or ""]
    for para in body.get("content") or []:
        for el in para:
            if el.get("tag") == "img" and el.get("image_key"):
                images.append(el["image_key"])
            elif el.get("tag") in ("text", "a"):
                texts.append(el.get("text", ""))
        texts.append("\n")
    return images, "".join(texts)


# ---------------- 文本 ----------------

def _handle_text(inc: Incoming, r: Responder, text: str):
    p = parse_text.parse(text)
    if p.kind == "help":
        return r.text(parse_text.HELP_TEXT)
    if p.kind == "register":
        return _register(inc, r, p)
    if p.kind == "query":
        if not p.demands:
            return r.text("请在“查询”后面写上 SKU，例如：查询 SKU-000123 SKU-000456")
        return _query(r, p.demands, "文字")
    # 没有指令：像 SKU 就直接查，否则回帮助
    idx_hits = [d for d in p.demands if skus.lookup(d["sku"]) or
                stock.locations_of([d["sku"]]).get(d["sku"])]
    if idx_hits:
        return _query(r, p.demands, "文字")
    return r.text(parse_text.HELP_TEXT)


def _register(inc: Incoming, r: Responder, p):
    if not p.entries and not p.errors:
        return r.text("格式：入库 SKU 库位码 [余量]\n例：入库 B40006 01-A-01-03-3/5 40")
    for i, e in enumerate(p.entries):
        e.source_id = f"msg:{inc.message_id}:{i}"
    results = stock.apply(p.entries, stock.METHOD_AUTO, inc.sender_id, inc.sender_name,
                           created_at=inc.created_at or None)
    ok = [x for x in results if x.ok]
    bad = [x for x in results if not x.ok and not x.duplicate]
    lines = []
    if ok:
        lines.append(f"✅ 成功 {len(ok)} 条")
        for x in ok[:30]:
            qty = f" 余量 {x.entry.qty}" if x.entry.qty is not None else ""
            warn = f"（{'；'.join(x.warnings)}）" if x.warnings else ""
            lines.append(f"· {x.entry.action} {x.sku} → {x.code}{qty}{warn}")
        if len(ok) > 30:
            lines.append(f"· …等 {len(ok)} 条")
    if bad or p.errors:
        lines.append(f"❌ 失败 {len(bad) + len(p.errors)} 条")
        for label, raw, why in p.errors:
            lines.append(f"· {label + ' ' if label else ''}{raw}：{why}")
        for x in bad:
            lines.append(f"· {x.entry.label + ' ' if x.entry.label else ''}{x.entry.sku} {x.entry.loc_text}：{x.msg}")
    if not lines:
        lines.append("这条消息已经处理过了")
    r.text("\n".join(lines))


# ---------------- 图片 / 文件 ----------------

def _handle_images(inc: Incoming, r: Responder, keys: list):
    keys = [k for k in keys if k]
    if not keys:
        return
    if not config.vision_enabled():
        return r.text("还没有配置图片识别（DeepSeek / Claude API Key），暂时只能用文字或 Excel 查询")
    r.text(f"收到 {len(keys)} 张图片，正在识别拣货单，大约需要 10–40 秒…")
    try:
        data = [r.download(k, "image") for k in keys]
        out = vision.extract(data)
    except vision.VisionError as e:
        return r.text(f"识别失败：{e}")
    if not out.get("is_pick_list") or not out.get("rows"):
        return r.text("这张图片里没有识别到拣货商品。请发送清晰、完整的拣货单截图或照片。")
    demands = [{"sku": row.get("sku", ""), "qty": row.get("qty"), "name": row.get("name", ""),
                "alt": row.get("alt") or []} for row in out["rows"]]
    _query(r, demands, "图片识别")


def _handle_pdf(inc: Incoming, r: Responder, name: str):
    if not config.vision_enabled():
        return r.text("还没有配置图片识别（DeepSeek / Claude API Key），暂时不能识别 PDF 拣货单")
    r.text("收到 PDF，正在识别拣货单（每页约 5 秒）…")
    try:
        data = r.download(inc.content.get("file_key"), "file")
        out = vision.extract_pdf(data)
    except vision.VisionError as e:
        return r.text(f"识别失败：{e}")
    except Exception as e:  # noqa: BLE001
        log.exception("读取 PDF 失败")
        return r.text(f"读取文件失败：{e}")
    if not out["rows"]:
        return r.text("这个 PDF 里没有识别到拣货商品。")
    if out.get("truncated"):
        r.text(f"这个 PDF 共 {out['pages']} 页，只识别了前 {vision.PDF_MAX_PAGES} 页，其余请分开发送。")
    demands = [{"sku": row.get("sku", ""), "qty": row.get("qty"), "name": row.get("name", ""),
                "alt": row.get("alt") or []} for row in out["rows"]]
    _query(r, demands, "PDF识别")


def _handle_file(inc: Incoming, r: Responder):
    name = inc.content.get("file_name", "")
    if re.search(r"\.pdf$", name, re.I):
        return _handle_pdf(inc, r, name)
    if not re.search(r"\.(xlsx|xls|csv)$", name, re.I):
        if inc.chat_type == "p2p":
            r.text("只支持 Excel（.xlsx / .xls）、CSV 或 PDF 格式的拣货单")
        return
    try:
        data = r.download(inc.content.get("file_key"), "file")
        rows = skus.rows_from_table(skus.read_table(name, data), want=("sku", "name", "qty"))
    except Exception as e:  # noqa: BLE001
        log.exception("读取拣货单文件失败")
        return r.text(f"读取文件失败：{e}")
    if not rows:
        return r.text("没找到 SKU 列。表头需要包含 SKU / 商家编码 / 货号 之一。")
    demands = []
    for row in rows:
        q = re.sub(r"\.0+$", "", row.get("qty") or "")
        demands.append({"sku": row["sku"], "qty": int(q) if q.isdigit() else None, "name": row.get("name", "")})
    _query(r, demands, "表格")


# ---------------- 查询输出 ----------------

def _query(r: Responder, demands: list, source: str):
    res = query.run(demands)
    image_keys = {}
    for wh, hl in res["warehouses"].items():
        m = maps.get(wh)
        if not m:
            continue
        try:
            image_keys[wh] = r.upload_image(render.render(m, hl))
        except Exception:  # noqa: BLE001
            log.exception("定位图上传失败")
    try:
        r.card(cards.result_card(res, image_keys, source))
    except Exception:  # noqa: BLE001
        log.exception("卡片发送失败，改发文本")
        r.text(cards.result_text(res))
        for key in image_keys.values():
            r.image(key)
    if len(res["lines"]) + len(res["unregistered"]) > 30:
        try:
            r.file(cards.result_xlsx(res), "拣货定位.xlsx")
        except Exception:  # noqa: BLE001
            log.exception("Excel 附件发送失败")
    return res
