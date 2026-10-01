import io

from PIL import Image

from app import vision


def _png(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


def test_prepare_normal_image():
    blocks = vision.prepare(_png(1200, 1600))
    assert len(blocks) == 1 and blocks[0]["source"]["media_type"] == "image/jpeg"


def test_prepare_splits_long_screenshot():
    blocks = vision.prepare(_png(1080, 8000))       # 手机长截图
    assert 4 <= len(blocks) <= 12


def test_prepare_bad_image():
    try:
        vision.prepare(b"not an image")
    except vision.VisionError:
        return
    raise AssertionError("应抛出 VisionError")


# ---------- DeepSeek ----------

class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload or {}, text

    def json(self):
        return self._payload


def _deepseek(monkeypatch, resp):
    from app import config
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(config, "VISION_PROVIDER", "auto")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return resp
    monkeypatch.setattr(vision.httpx, "post", fake_post)
    return seen


def _reply(content, finish="stop"):
    return _Resp(200, {"model": "deepseek-flash", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                       "choices": [{"finish_reason": finish, "message": {"content": content}}]})


def test_provider_selection(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "VISION_PROVIDER", "auto")
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert config.vision_provider() == "" and not config.vision_enabled()
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "a")
    assert config.vision_provider() == "anthropic"
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "d")
    assert config.vision_provider() == "deepseek"                  # 两个都配时优先 DeepSeek
    monkeypatch.setattr(config, "VISION_PROVIDER", "anthropic")
    assert config.vision_provider() == "anthropic"
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "")
    monkeypatch.setattr(config, "VISION_PROVIDER", "deepseek")
    assert config.vision_provider() == ""                          # 固定用 DeepSeek 但没有 Key


def test_deepseek_extract_sends_image_and_normalizes(monkeypatch):
    seen = _deepseek(monkeypatch, _reply(
        '```json\n{"is_pick_list": true, "rows": [{"sku": " B40006 ", "alt": "X1", "name": "锯片", "qty": "3"},'
        '{"sku": "632016", "alt": ["A"], "name": null, "qty": 2.0}, {"sku": "", "qty": 1}, "垃圾"]}\n```'))
    out = vision.extract([_png(1200, 1600)])
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["headers"]["Authorization"] == "Bearer sk-test"
    body = seen["body"]
    assert body["model"] == "deepseek-flash" and body["response_format"] == {"type": "json_object"}
    parts = body["messages"][0]["content"]
    assert parts[0]["type"] == "image_url" and parts[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "JSON" in parts[-1]["text"]
    assert out == {"is_pick_list": True, "rows": [
        {"sku": "B40006", "alt": ["X1"], "name": "锯片", "qty": 3},
        {"sku": "632016", "alt": ["A"], "name": "", "qty": 2}]}


def test_deepseek_not_a_pick_list(monkeypatch):
    _deepseek(monkeypatch, _reply('{"is_pick_list": true, "rows": []}'))
    assert vision.extract([_png(800, 800)]) == {"is_pick_list": False, "rows": []}


def test_deepseek_errors_are_readable(monkeypatch):
    import pytest
    for status, word in [(401, "Key 无效"), (402, "余额不足"), (429, "太频繁"), (400, "出错")]:
        _deepseek(monkeypatch, _Resp(status, {"error": {"message": "x"}}))
        monkeypatch.setattr(vision.time, "sleep", lambda s: None)
        with pytest.raises(vision.VisionError, match=word):
            vision.extract([_png(800, 800)])
    _deepseek(monkeypatch, _reply('{"is_pick_list": true', finish="length"))
    with pytest.raises(vision.VisionError, match="太长"):
        vision.extract([_png(800, 800)])
    _deepseek(monkeypatch, _reply("这不是 JSON"))
    with pytest.raises(vision.VisionError, match="格式异常"):
        vision.extract([_png(800, 800)])


# ---------- PDF ----------

def _pdf(pages):
    import pymupdf
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 100), f"拣货单 DL016 page {i + 1}")
    return doc.tobytes()


def test_render_pdf_pages_and_text():
    pages, total = vision.render_pdf(_pdf(3))
    assert total == 3 and len(pages) == 3
    assert pages[0][0][:8] == b"\x89PNG\r\n\x1a\n" and "DL016" in pages[0][1] and "page 2" in pages[1][1]
    pages, total = vision.render_pdf(_pdf(12))
    assert total == 12 and len(pages) == vision.PDF_MAX_PAGES


def test_render_pdf_rejects_garbage():
    import pytest
    with pytest.raises(vision.VisionError):
        vision.render_pdf(b"not a pdf")


def test_extract_pdf_merges_pages_and_passes_text_layer(monkeypatch):
    calls = []

    def fake_extract(images, text=None):
        calls.append((len(images), text))
        return {"is_pick_list": True, "rows": [{"sku": f"S{len(calls)}", "alt": [], "name": "", "qty": len(calls)}]}
    monkeypatch.setattr(vision, "extract", fake_extract)
    out = vision.extract_pdf(_pdf(12))
    assert [c[0] for c in calls] == [1] * 10 and "DL016" in calls[0][1]      # 一页一次请求，附带文字层
    assert out["pages"] == 12 and out["truncated"] and [r["sku"] for r in out["rows"]][:2] == ["S1", "S2"]


def test_text_layer_is_added_to_prompt(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(config, "VISION_PROVIDER", "auto")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["text"] = json["messages"][0]["content"][-1]["text"]
        return _Resp(200, {"choices": [{"finish_reason": "stop", "message": {"content": '{"is_pick_list": false, "rows": []}'}}]})
    monkeypatch.setattr(vision.httpx, "post", fake_post)
    vision.extract([_png(800, 800)], text="SKU DL016_P3")
    assert "文字层" in seen["text"] and "SKU DL016_P3" in seen["text"] and "JSON" in seen["text"]
    vision.extract([_png(800, 800)])
    assert "文字层" not in seen["text"]
