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
