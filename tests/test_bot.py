import json

import pytest

from app import bot, cards, config, skus, stock, vision
from app.feishu import bitable_sync


class FakeResponder(bot.Responder):
    def __init__(self, resources=None):
        self.out = []
        self.resources = resources or {}

    def text(self, text): self.out.append(("text", text))
    def card(self, card):
        json.dumps(card, ensure_ascii=False)
        self.out.append(("card", card))
    def image(self, key): self.out.append(("image", key))
    def file(self, data, name): self.out.append(("file", name))
    def upload_image(self, png):
        assert png[:4] == b"\x89PNG"
        return f"img_{len(self.out)}"
    def download(self, key, kind): return self.resources[key]


def msg(text, chat_type="p2p", mentioned=False, mid="m1"):
    return bot.Incoming(message_id=mid, chat_id="oc_1", chat_type=chat_type, msg_type="text",
                        content={"text": text}, sender_id="ou_1", sender_name="张三", mentioned_bot=mentioned)


def test_register_via_chat(saved_map):
    r = FakeResponder()
    bot.handle(msg("登记\nSKU-1 01-A-01-02-1/5 40\nSKU-2 01-A-09-01-1/5\nbad"), r)
    kind, text = r.out[0]
    assert kind == "text" and "成功 1 条" in text and "失败 2 条" in text and "没有 09 号货架" in text
    rows = stock.locations_of(["SKU-1"])["SKU-1"]
    assert rows[0]["qty"] == 40 and rows[0]["last_by"] == "张三" and rows[0]["last_method"] == "自动入库"
    # 同一条消息重复推送不会重复登记
    r2 = FakeResponder()
    bot.handle(msg("登记\nSKU-1 01-A-01-02-1/5 40"), r2)
    assert len(stock.recent_logs()) == 1


def test_group_requires_mention(saved_map):
    r = FakeResponder()
    bot.handle(msg("登记 SKU-1 01-A-01-02-1/5", chat_type="group"), r)
    assert r.out == []
    bot.handle(msg("登记 SKU-1 01-A-01-02-1/5", chat_type="group", mentioned=True), r)
    assert "成功 1 条" in r.out[0][1]


def test_text_query_card(saved_map):
    skus.upsert([{"sku": "A1", "name": "扳手"}])
    stock.apply([stock.Entry("A1", "01-A-01-01-1/5", 3)], "聊天", "u", "李四")
    r = FakeResponder()
    bot.handle(msg("查询 A1 5 NOPE"), r)
    kinds = [k for k, _ in r.out]
    assert kinds == ["card"]
    card = r.out[0][1]
    body = json.dumps(card, ensure_ascii=False)
    assert "01-A-01-01-1/5" in body and "余量不足" in body and "NOPE" in body
    assert any(e.get("tag") == "img" for e in card["body"]["elements"])
    # 不带指令、但是已知 SKU → 直接查询
    r = FakeResponder()
    bot.handle(msg("A1"), r)
    assert r.out[0][0] == "card"
    # 不认识的文字 → 帮助
    r = FakeResponder()
    bot.handle(msg("你好"), r)
    assert "使用说明" in r.out[0][1]


def test_image_query(saved_map, monkeypatch):
    skus.upsert([{"sku": "SKU-000123", "name": "USB充电线"}])
    stock.apply([stock.Entry("SKU-000123", "01-B-01-01-1/5")], "聊天")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(vision, "extract", lambda imgs: {"is_pick_list": True, "rows": [
        {"sku": "SKU-OOO123", "alt": [], "name": "USB充电线", "qty": 2},
        {"sku": "??", "alt": [], "name": "不认识的东西", "qty": 1}]})
    r = FakeResponder({"img_k": b"fake"})
    inc = bot.Incoming("m9", "oc_pick", "group", "image", {"image_key": "img_k"})
    bot.handle(inc, r)
    assert r.out[0][0] == "text" and "正在识别" in r.out[0][1]
    card = r.out[-1][1]
    body = json.dumps(card, ensure_ascii=False)
    assert "SKU-000123" in body and "已自动纠错" in body and "没找到" in body


def test_image_not_pick_list(saved_map, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(vision, "extract", lambda imgs: {"is_pick_list": False, "rows": []})
    r = FakeResponder({"k": b"x"})
    bot.handle(bot.Incoming("m2", "oc", "p2p", "image", {"image_key": "k"}), r)
    assert "没有识别到" in r.out[-1][1]


def test_post_with_image(saved_map, monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test")
    seen = {}
    monkeypatch.setattr(vision, "extract", lambda imgs: seen.setdefault("n", len(imgs)) and
                        {"is_pick_list": False, "rows": []})
    content = {"title": "", "content": [[{"tag": "img", "image_key": "k1"}], [{"tag": "text", "text": "看看"}],
                                        [{"tag": "img", "image_key": "k2"}]]}
    r = FakeResponder({"k1": b"1", "k2": b"2"})
    bot.handle(bot.Incoming("m3", "oc", "p2p", "post", content), r)
    assert seen["n"] == 2


def test_excel_file_query(saved_map):
    from openpyxl import Workbook
    import io
    skus.upsert([{"sku": "A1", "name": "扳手"}])
    stock.apply([stock.Entry("A1", "01-A-02-00-1/5", 9)], "聊天")
    wb = Workbook()
    ws = wb.active
    ws.append(["商家SKU", "商品名称", "数量"])
    ws.append(["A1", "扳手", 2])
    buf = io.BytesIO()
    wb.save(buf)
    r = FakeResponder({"f1": buf.getvalue()})
    bot.handle(bot.Incoming("m4", "oc", "p2p", "file", {"file_key": "f1", "file_name": "拣货单.xlsx"}), r)
    body = json.dumps(r.out[-1][1], ensure_ascii=False)
    assert "01-A-02-00-1/5" in body


def test_card_fallback_to_text(saved_map):
    stock.apply([stock.Entry("A1", "01-A-02-00-1/5", 9)], "聊天")

    class Broken(FakeResponder):
        def card(self, card): raise RuntimeError("card schema error")

    r = Broken()
    bot.handle(msg("查询 A1"), r)
    assert r.out[0][0] == "text" and "01-A-02-00-1/5" in r.out[0][1]
    assert r.out[1][0] == "image"


def test_xlsx_export(saved_map):
    from app import query
    stock.apply([stock.Entry("A1", "01-A-02-00-1/5", 9)], "聊天")
    data = cards.result_xlsx(query.run([{"sku": "A1", "qty": 1}, {"sku": "X", "qty": 1}]))
    assert data[:2] == b"PK"


# ---------- 多维表格记录解析 ----------

def test_bitable_record_parsing(saved_map):
    e, problem = bitable_sync.record_to_entry("rec1", {
        "SKU": [{"text": "SKU-9", "type": "text"}], "仓库号": "01", "区块": "A", "货架": 2.0, "层": 0.0,
        "包装备注": 1.0, "余量": 12.0})
    assert not problem and e.loc_text == "01-A-02-00-1" and e.qty == 12 and e.source_id == "bitable:rec1"
    e, _ = bitable_sync.record_to_entry("rec2", {"SKU": "S", "库位码": [{"text": "01a0110"}], "操作": "移除"})
    assert e.loc_text == "01a0110" and e.action == "移除" and e.qty is None
    _, problem = bitable_sync.record_to_entry("rec3", {"SKU": "S", "库位码": "01-A-01-01-1/5", "余量": "很多"})
    assert problem
    e2, _ = bitable_sync.record_to_entry("rec9", {"SKU": "S", "仓库号": "01", "区块": "A", "货架": 1.0, "层": 3.0, "区域": 3.0})
    assert e2.loc_text == "01-A-01-03-3" and stock.check(e2).code == "01-A-01-03-3/5"     # 区域字段；总数按货架补全


def test_bitable_sync_once(saved_map, monkeypatch):
    class Rec:
        def __init__(self, rid, fields, mod_ms):
            self.record_id, self.fields, self.last_modified_time = rid, fields, mod_ms
            self.created_time = 1758700000000
            self.created_by = None

    import time
    old = time.time() * 1000 - 60_000
    recs = [Rec("r1", {"SKU": "S1", "库位码": "01-A-01-01-1/5", "余量": 5,
                       "提交人": [{"id": "ou_9", "name": "王五"}]}, old),
            Rec("r2", {"SKU": "S2", "库位码": "01-Z-01-01-1/5", "提交人": [{"id": "ou_9", "name": "王五"}]}, old),
            Rec("r3", {"SKU": "S3", "库位码": "01-A-01-01-1/5"}, time.time() * 1000),     # 刚编辑，暂不处理
            Rec("r4", {}, old)]                                                         # 空行
    monkeypatch.setattr(bitable_sync, "_pending_records", lambda: recs)
    updates, sent = [], []

    class FakeTable:
        def batch_update(self, req):
            updates.extend(req.request_body.records)
            return type("R", (), {"success": lambda self: True})()

    class FakeClient:
        class bitable:
            class v1:
                app_table_record = FakeTable()

    monkeypatch.setattr(bitable_sync.fc, "client", lambda: FakeClient)
    monkeypatch.setattr(bitable_sync.fc, "send", lambda *a, **k: sent.append(a))
    assert bitable_sync.sync_once() == 2
    by_id = {u.record_id: u.fields for u in updates}
    assert by_id["r1"]["处理状态"] == "成功" and by_id["r2"]["处理状态"] == "失败"
    assert "没有区块 Z" in by_id["r2"]["处理说明"] and len(sent) == 1
    row = stock.locations_of(["S1"])["S1"][0]
    assert row["qty"] == 5 and row["last_by"] == "王五" and row["last_method"] == "表单"


def test_pdf_file_query(saved_map, monkeypatch):
    skus.upsert([{"sku": "DL016", "name": "HOOK KNIFE"}])
    stock.apply([stock.Entry("DL016", "01-A-01-02-3/5", 9)], "聊天")
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(vision, "extract_pdf", lambda data: {"is_pick_list": True, "pages": 1, "truncated": False, "rows": [
        {"sku": "DL016_P3", "alt": ["DL016"], "name": "Deli cutter", "qty": 1},
        {"sku": "DL016_P3", "alt": [], "name": "Deli cutter", "qty": 1}]})
    r = FakeResponder({"f9": b"%PDF"})
    bot.handle(bot.Incoming("m8", "oc", "p2p", "file", {"file_key": "f9", "file_name": "BigSeller - Orders.pdf"}), r)
    assert "正在识别" in r.out[0][1]
    body = json.dumps(r.out[-1][1], ensure_ascii=False)
    assert "01-A-01-02-3/5" in body and "DL016" in body


def test_pdf_without_vision_key(saved_map, monkeypatch):
    for k in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setattr(config, k, "")
    r = FakeResponder({"f9": b"%PDF"})
    bot.handle(bot.Incoming("m8", "oc", "p2p", "file", {"file_key": "f9", "file_name": "a.pdf"}), r)
    assert "还没有配置图片识别" in r.out[0][1]
