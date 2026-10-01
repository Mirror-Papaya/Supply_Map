"""拣货单 → 地图红点定位：渲染、上传接口（图片 / PDF / Excel）。"""
import io

from fastapi.testclient import TestClient
from PIL import Image

from app import config, maps, render, skus, stock, vision, web


def _count(png: bytes, color):
    img = Image.open(io.BytesIO(png)).convert("RGB")
    return sum(1 for px in img.getdata() if px == color)


def test_render_draws_red_dots_only_for_highlighted_shelves(saved_map):
    m = maps.get("01")
    plain = render.render(m)
    marked = render.render(m, {("A", "01"): 1, ("A", "02"): 2})
    assert _count(plain, render.HILITE) == 0
    assert _count(marked, render.HILITE) > 200                       # 两个实心红点 + 红色边框
    assert render.render(m, {("A", "01"): 1}) != marked              # 红点数量随目标货架变化


def _client_with_stock():
    skus.upsert([{"sku": "A1", "name": "扳手"}, {"sku": "DL016", "name": "HOOK KNIFE"}])
    stock.apply([stock.Entry("A1", "01-A-02-00-1/5", 9), stock.Entry("DL016", "01-A-01-02-3/5", 4)], "手动入库")
    return TestClient(web.app)


def test_pick_excel_upload(saved_map):
    from openpyxl import Workbook
    c = _client_with_stock()
    wb = Workbook()
    ws = wb.active
    ws.append(["商家SKU", "商品名称", "数量"])
    ws.append(["A1", "扳手", 2])
    ws.append(["DL016", "钩刀", 1])
    buf = io.BytesIO()
    wb.save(buf)
    r = c.post("/api/pick", files={"file": ("拣货单.xlsx", buf.getvalue(), "application/octet-stream")})
    assert r.status_code == 200
    d = r.json()
    assert d["source"] == "表格" and d["rows"] == 2 and len(d["lines"]) == 2
    assert d["hl"]["01"] == "A-01,A-02"                           # 按拣货顺序列出要标红点的货架（先 A-01 后 A-02）
    assert {l["loc"] for l in d["lines"]} == {"01-A-02-00-1/5", "01-A-01-02-3/5"}


def test_pick_image_and_pdf_upload(saved_map, monkeypatch):
    c = _client_with_stock()
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "sk-test")
    rows = [{"sku": "DL016_P3", "alt": ["DL016"], "name": "cutter", "qty": 1}, {"sku": "ZZZ", "alt": [], "name": "", "qty": 1}]
    monkeypatch.setattr(vision, "extract", lambda imgs, text=None: {"is_pick_list": True, "rows": rows})
    monkeypatch.setattr(vision, "extract_pdf", lambda data: {"is_pick_list": True, "rows": rows, "pages": 1, "truncated": False})
    r = c.post("/api/pick", files={"file": ("a.png", b"\x89PNG", "image/png")})
    assert r.status_code == 200 and r.json()["source"] == "图片识别" and r.json()["hl"] == {"01": "A-01"}
    assert [u["input"] for u in r.json()["unmatched"]] == ["ZZZ"]
    r = c.post("/api/pick", files={"file": ("BigSeller.pdf", b"%PDF", "application/pdf")})
    assert r.status_code == 200 and r.json()["source"] == "PDF 识别" and r.json()["rows"] == 2


def test_pick_errors_are_readable(saved_map, monkeypatch):
    c = _client_with_stock()
    for k in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setattr(config, k, "")
    r = c.post("/api/pick", files={"file": ("a.png", b"x", "image/png")})
    assert r.status_code == 400 and "还没有配置图片识别" in r.json()["detail"]
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(vision, "extract", lambda imgs, text=None: {"is_pick_list": False, "rows": []})
    r = c.post("/api/pick", files={"file": ("a.png", b"x", "image/png")})
    assert r.status_code == 400 and "没有识别到" in r.json()["detail"]
    r = c.post("/api/pick", files={"file": ("bad.xlsx", b"not excel", "application/octet-stream")})
    assert r.status_code == 400 and "读取文件失败" in r.json()["detail"]
