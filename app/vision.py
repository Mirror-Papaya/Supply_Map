"""拣货单图片识别：Claude 读图 → 结构化的 [SKU, 其他编码, 品名, 数量]。

识别只负责"照抄"，SKU 纠错交给 matcher 用商品字典做闭集匹配。
"""
import base64
import io
import json
import logging

import anthropic
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


def extract(images: list) -> dict:
    """images: [bytes, ...]（一条消息里的一张或多张图）。返回 {"is_pick_list", "rows": [{"sku","alt","name","qty"}]}"""
    content = []
    for data in images:
        content += prepare(data)
    content.append({"type": "text", "text": PROMPT})
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
