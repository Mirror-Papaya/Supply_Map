"""拣货单图片识别：DeepSeek（deepseek-flash）或 Claude 读图 → 结构化的 [SKU, 其他编码, 品名, 数量]。

识别只负责"照抄"，SKU 纠错交给 matcher 用商品字典做闭集匹配。
"""
import base64
import io
import json
import logging
import re
import time

import anthropic
import httpx
from PIL import Image, ImageOps

from . import config

log = logging.getLogger("vision")

MAX_EDGE = 2400
SEGMENT_RATIO = 1.6      # 长截图切段：每段高度 = 宽 × 1.6
SPLIT_THRESHOLD = 2.2    # 高/宽 超过这个比例才切段
OVERLAP = 0.12

PROMPT = """你会收到一张电商拣货单图片（如果有多段，是同一张长图从上到下按顺序切开的，相邻两段有少量重叠）。
请逐行提取每个待拣商品：
- sku：商家 SKU / 卖家 SKU / 商品编码（Seller SKU、Merchant SKU、货号）。逐字照抄，保留大小写、横杠、下划线、字母和数字，不要补全、不要猜；看不清的字符用 ? 代替。
- alt：这一行里其他像编码的字符串（平台 SKU ID、型号、条码等），没有就给空数组。
- name：商品名称或规格，简短即可。
- qty：该行的拣货数量（整数）；看不到就给 null。
同一商品在单据里出现多次就输出多行，不要合并。表头、合计行、订单号、快递单号、店铺名不是商品，不要输出。
多段图片的重叠部分里，同一行只输出一次。
版式提示：
- 如果有"SKU&变种名称"和"Parent SKU"两列，前者的编码是 sku，后者的编码放进 alt。
- 数量列里带圈的数字（②③⑤）就是数量；每个订单底部的"总数: n"、店铺名、订单号、物流单号、包裹号（如 BS1DT03010191）都不是商品。
如果图片不是拣货单 / 订单 / 商品清单，is_pick_list 给 false，rows 给空数组。"""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_pick_list", "rows"],
    "properties": {
        "is_pick_list": {"type": "boolean"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["sku", "alt", "name", "qty"],
                "properties": {
                    "sku": {"type": "string"},
                    "alt": {"type": "array", "items": {"type": "string"}},
                    "name": {"type": "string"},
                    "qty": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                },
            },
        },
    },
}


class VisionError(Exception):
    pass


def _encode(img: Image.Image) -> dict:
    w, h = img.size
    k = min(1.0, MAX_EDGE / max(w, h))
    if k < 1:
        img = img.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
    for q in (90, 80, 70, 60):
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q)
        if buf.tell() < 3_700_000:     # base64 后需小于 5MB
            break
    return {"type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def prepare(data: bytes) -> list:
    """图片 → Claude image 内容块；很长的截图切成多段，避免整体缩小后看不清。"""
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise VisionError(f"无法读取图片：{e}") from e
    w, h = img.size
    if h / w <= SPLIT_THRESHOLD:
        return [_encode(img)]
    seg_h = int(w * SEGMENT_RATIO)
    step = int(seg_h * (1 - OVERLAP))
    blocks, y = [], 0
    while True:
        blocks.append(_encode(img.crop((0, y, w, min(h, y + seg_h)))))
        if y + seg_h >= h or len(blocks) >= 12:
            break
        y += step
    return blocks


_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None, timeout=180.0, max_retries=2)
    return _client


JSON_HINT = """

请只输出一个 JSON 对象，不要任何解释或多余文字，格式如下：
{"is_pick_list": true, "rows": [{"sku": "B40006", "alt": [], "name": "商品名", "qty": 3}]}
qty 看不到时写 null；图片不是拣货单时输出 {"is_pick_list": false, "rows": []}。"""


def _parse_json(text: str) -> dict:
    t = (text or "").strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)      # 偶尔会带 ``` 代码块
    if m:
        t = m.group(1)
    try:
        out = json.loads(t)
    except json.JSONDecodeError as e:
        raise VisionError("识别结果格式异常，请重试") from e
    if not isinstance(out, dict):
        raise VisionError("识别结果格式异常，请重试")
    return out


def _normalize(out: dict) -> dict:
    """不同模型对格式的遵守程度不一样：补齐字段、统一类型，保证下游拿到的结构一致。"""
    rows = []
    for r in out.get("rows") or []:
        if not isinstance(r, dict):
            continue
        sku = str(r.get("sku") or "").strip()
        if not sku:
            continue
        alt = r.get("alt") or []
        alt = [str(a).strip() for a in alt if str(a).strip()] if isinstance(alt, list) else [str(alt).strip()]
        qty = r.get("qty")
        if isinstance(qty, str) and qty.strip().isdigit():
            qty = int(qty.strip())
        elif isinstance(qty, float) and qty.is_integer():
            qty = int(qty)
        if not isinstance(qty, int) or isinstance(qty, bool):
            qty = None
        rows.append({"sku": sku, "alt": alt, "name": str(r.get("name") or "").strip(), "qty": qty})
    return {"is_pick_list": bool(out.get("is_pick_list")) and bool(rows), "rows": rows}


def _with_hint(text: str | None) -> str:
    """PDF 自带的文字层：字符是准的（没有看错的问题），但排列顺序可能错乱，只用来核对编码。"""
    t = (text or "").strip()
    if not t:
        return ""
    return ("\n\n下面是这份 PDF 自带的文字层（字符准确，但排列顺序可能错乱；版式以图片为准，只用它核对 SKU 等编码的字符）：\n"
            + t[:6000])


def _extract_deepseek(blocks: list, hint: str = "") -> dict:
    content = [{"type": "image_url",
                "image_url": {"url": f"data:{b['source']['media_type']};base64,{b['source']['data']}"}} for b in blocks]
    content.append({"type": "text", "text": PROMPT + hint + JSON_HINT})
    body = {"model": config.DEEPSEEK_MODEL, "messages": [{"role": "user", "content": content}],
            "response_format": {"type": "json_object"}, "max_tokens": 8000, "stream": False}
    headers = {"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"}
    resp = None
    for attempt in (1, 2):         # 超时 / 限流 / 服务端 5xx 重试一次
        try:
            resp = httpx.post(f"{config.DEEPSEEK_BASE_URL}/chat/completions", headers=headers, json=body,
                              timeout=httpx.Timeout(180.0, connect=15.0))
        except httpx.TimeoutException as e:
            if attempt == 2:
                raise VisionError("DeepSeek 响应超时，请稍后再试") from e
            continue
        except httpx.HTTPError as e:
            raise VisionError("连不上 DeepSeek API，请检查网络 / 代理") from e
        if resp.status_code in (429, 500, 502, 503, 504) and attempt == 1:
            time.sleep(2)
            continue
        break
    if resp.status_code == 401:
        raise VisionError("DeepSeek API Key 无效，请检查 .env 里的 DEEPSEEK_API_KEY")
    if resp.status_code == 402:
        raise VisionError("DeepSeek 账户余额不足，请到 platform.deepseek.com 充值")
    if resp.status_code == 429:
        raise VisionError("DeepSeek API 调用太频繁，请稍后再试")
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message", "")
        except ValueError:
            msg = resp.text[:200]
        raise VisionError(f"DeepSeek API 出错（{resp.status_code}）：{msg}")
    data = resp.json()
    choice = (data.get("choices") or [{}])[0]
    if choice.get("finish_reason") == "length":
        raise VisionError("拣货单太长，请分成几张图片发送")
    out = _normalize(_parse_json((choice.get("message") or {}).get("content")))
    usage = data.get("usage") or {}
    log.info("识别完成 model=%s rows=%d in=%s out=%s", data.get("model"), len(out["rows"]),
             usage.get("prompt_tokens"), usage.get("completion_tokens"))
    return out


def extract(images: list, text: str | None = None) -> dict:
    """images: [bytes, ...]（一条消息里的一张或多张图）；text: PDF 文字层（可选，用来核对编码）。
    返回 {"is_pick_list", "rows": [{"sku","alt","name","qty"}]}"""
    provider = config.vision_provider()
    if not provider:
        raise VisionError("还没有配置图片识别（.env 里填 DEEPSEEK_API_KEY 或 ANTHROPIC_API_KEY）")
    blocks = []
    for data in images:
        blocks += prepare(data)
    hint = _with_hint(text)
    out = _extract_deepseek(blocks, hint) if provider == "deepseek" else _extract_claude(blocks, hint)
    return _normalize(out)


def _extract_claude(blocks: list, hint: str = "") -> dict:
    content = list(blocks)
    content.append({"type": "text", "text": PROMPT + hint})
    try:
        resp = _get_client().beta.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": config.CLAUDE_EFFORT,
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError as e:
        raise VisionError("Claude API Key 无效，请检查 .env 里的 ANTHROPIC_API_KEY") from e
    except anthropic.RateLimitError as e:
        raise VisionError("Claude API 调用太频繁，请稍后再试") from e
    except anthropic.APIStatusError as e:
        raise VisionError(f"Claude API 出错（{e.status_code}）：{e.message}") from e
    except anthropic.APIConnectionError as e:
        raise VisionError("连不上 Claude API，请检查网络 / 代理") from e
    if resp.stop_reason == "refusal":
        raise VisionError("图片识别被拒绝，请换一张清晰的拣货单图片")
    if resp.stop_reason == "max_tokens":
        raise VisionError("拣货单太长，请分成几张图片发送")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        out = json.loads(text)
    except json.JSONDecodeError as e:
        raise VisionError("识别结果格式异常，请重试") from e
    usage = resp.usage
    log.info("识别完成 model=%s rows=%d in=%s out=%s", resp.model, len(out.get("rows", [])),
             usage.input_tokens, usage.output_tokens)
    return out


# ---------------- PDF ----------------

PDF_MAX_PAGES = 10


def render_pdf(data: bytes, max_pages: int = PDF_MAX_PAGES, dpi: int = 130):
    """PDF → ([(页面 PNG, 页面文字层)], 总页数)。"""
    import pymupdf
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        raise VisionError(f"无法读取 PDF：{e}") from e
    pages = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pages.append((page.get_pixmap(dpi=dpi).tobytes("png"), page.get_text()))
    if not pages:
        raise VisionError("这个 PDF 没有内容")
    return pages, len(doc)


def extract_pdf(data: bytes) -> dict:
    """逐页识别后合并（一页一次请求，单页出错不会拖垮整份）。多返回 pages / truncated 两个字段。"""
    pages, total = render_pdf(data)
    rows = []
    for png, text in pages:
        rows += extract([png], text=text)["rows"]
    return {"is_pick_list": bool(rows), "rows": rows, "pages": total, "truncated": total > len(pages)}

