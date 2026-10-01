import copy

from app import codes, maps, matcher, parse_text, query, render, skus, stock
from app.stock import Entry


# ---------- 库位码 ----------

def test_code_parse_variants():
    for text in ["01-A-03-2-1", "01A0321", "01-a-03-2-1", "０１－Ａ－０３－２－１", "01 A 03 2 1", "01_A_03_2_1"]:
        assert codes.normalize(text) == "01-A-03-2-1", text
    assert codes.normalize("01-A-03-0-0") == "01-A-03-0-0"      # 层允许 0
    for bad in ["1-A-03-2-1", "01-AA-03-2-1", "01-A-3-2-1", "01-A-03-12-1", "01-A-03-2", "SKU-000123", ""]:
        assert codes.parse(bad) is None, bad


def test_code_from_parts():
    assert codes.from_parts(1, "a", 3, 2, 0).code == "01-A-03-2-0"
    assert codes.from_parts("01", "B", "12", "0", "9").code == "01-B-12-0-9"
    assert codes.from_parts(1.0, "A", 3.0, 2.0, 1.0).code == "01-A-03-2-1"
    assert codes.from_parts(1, "A", 3, 12, 0) is None
    assert codes.from_parts("", "A", 3, 1, 0) is None


# ---------- 地图 ----------

def test_map_analyze_ok(sample_map):
    a = maps.analyze(sample_map)
    assert not a["errors"], a["errors"]
    zs = {(b["zone"], b["shelf"]) for b in a["blocks"].values() if b["type"] == "storage"}
    assert zs == {("A", "01"), ("A", "02"), ("B", "01")}


def test_map_duplicate_and_cross_zone(sample_map):
    m = copy.deepcopy(sample_map)
    m["blocks"]["2"]["shelf"] = "01"
    errs = [e["msg"] for e in maps.analyze(m)["errors"]]
    assert any("重复" in e for e in errs)
    m = copy.deepcopy(sample_map)
    m["zones"][1] = "A" * 5 + "B" * 15      # 货架 1 跨 A/B
    errs = [e["msg"] for e in maps.analyze(m)["errors"]]
    assert any("多个仓库区块" in e for e in errs)


def test_map_save_blocks_removal_with_stock(saved_map):
    r = stock.apply([Entry("SKU1", "01-A-01-2-0", 5)], "聊天", "u1", "张三")
    assert r[0].ok
    m = copy.deepcopy(saved_map)
    del m["blocks"]["1"]
    res = maps.save(m)
    assert not res["ok"] and any("还有" in e["msg"] for e in res["errors"])
    m = copy.deepcopy(saved_map)
    m["blocks"]["1"]["layer_max"] = 1
    res = maps.save(m)
    assert not res["ok"]


def test_render_png(saved_map):
    png = render.render(saved_map, {("A", "01"): 1, ("B", "01"): 2})
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 1000


# ---------- 登记 ----------

def test_register_and_latest_qty(saved_map):
    r = stock.apply([Entry("SKU-1", "01a0120", 40)], "表单", "u1", "张三", created_at="2026-09-20 10:00:00")
    assert r[0].ok and r[0].code == "01-A-01-2-0"
    # 再次登记不填余量：保留 40（09-20 张三）
    r = stock.apply([Entry("SKU-1", "01-A-01-2-0")], "聊天", "u2", "李四", created_at="2026-09-24 10:00:00")
    cur = stock.locations_of(["SKU-1"])["SKU-1"]
    assert len(cur) == 1 and cur[0]["qty"] == 40 and cur[0]["qty_by"] == "张三" and cur[0]["last_by"] == "李四"
    # 新盘点数
    stock.apply([Entry("SKU-1", "01-A-01-2-0", 35)], "聊天", "u2", "李四")
    assert stock.locations_of(["SKU-1"])["SKU-1"][0]["qty"] == 35
    # 移除后重新登记：余量留空
    assert stock.apply([Entry("SKU-1", "01-A-01-2-0", action="移除")], "聊天", "u2", "李四")[0].ok
    assert stock.locations_of(["SKU-1"])["SKU-1"] == []
    stock.apply([Entry("SKU-1", "01-A-01-2-0")], "聊天", "u2", "李四")
    assert stock.locations_of(["SKU-1"])["SKU-1"][0]["qty"] is None
    # 日志完整保留，重算结果一致
    assert len(stock.recent_logs()) == 5
    before = stock.locations_of(["SKU-1"])
    stock.rebuild_current()
    assert stock.locations_of(["SKU-1"]) == before


def test_register_validation(saved_map):
    cases = {
        "01-A-09-1-0": "没有 09 号货架",
        "01-C-01-1-0": "没有区块 C",
        "02-A-01-1-0": "仓库 02 还没有地图",
        "01-A-01-5-0": "层号范围",
        "01-A-02-0-0": None,           # 货架 02 允许 0 层
        "abc": "格式不对",
    }
    for code, expect in cases.items():
        r = stock.check(Entry("S", code))
        if expect is None:
            assert r.ok, (code, r.msg)
        else:
            assert not r.ok and expect in r.msg, (code, r.msg)
    assert not stock.check(Entry("S", "01-A-01-1-0", action="移除")).ok


def test_duplicate_source_id(saved_map):
    e = Entry("S", "01-A-01-1-0", 3, source_id="rec1")
    assert stock.apply([e], "表单")[0].ok
    r = stock.apply([e], "表单")[0]
    assert not r.ok and r.duplicate


def test_sku_dictionary_canonical(saved_map):
    skus.upsert([{"sku": "DL111250Z", "name": "得力卷尺5m", "aliases": "EDL111250Z"}])
    r = stock.apply([Entry("edl111250z", "01-A-01-1-0")], "聊天")[0]
    assert r.ok and r.sku == "DL111250Z" and not r.warnings
    r = stock.apply([Entry("UNKNOWN-1", "01-A-01-1-0")], "聊天")[0]
    assert r.ok and "不在商品字典" in r.warnings[0]


# ---------- 文本解析 ----------

def test_parse_register_multiline():
    p = parse_text.parse("登记\nSKU-1 01-A-01-2-0 40\nSKU-2 01A0110\n01-B-01-1-0 SKU-3 5个\n移除 SKU-4 01-A-01-1-0\nbad line")
    assert p.kind == "register"
    got = [(e.sku, e.loc_text, e.qty, e.action) for e in p.entries]
    assert got == [("SKU-1", "01-A-01-2-0", 40, "登记"), ("SKU-2", "01A0110", None, "登记"),
                   ("SKU-3", "01-B-01-1-0", 5, "登记"), ("SKU-4", "01-A-01-1-0", None, "移除")]
    assert len(p.errors) == 1


def test_parse_query_and_help():
    p = parse_text.parse("查询 SKU-1 3 SKU-2\nSKU-3 2件")
    assert p.kind == "query"
    assert p.demands == [{"sku": "SKU-1", "qty": 3}, {"sku": "SKU-2", "qty": None}, {"sku": "SKU-3", "qty": 2}]
    assert parse_text.parse("帮助").kind == "help"
    assert parse_text.parse("@_user_1 查询 X1").demands == [{"sku": "X1", "qty": None}]


# ---------- 匹配与查询 ----------

def test_matcher(saved_map):
    skus.upsert([{"sku": "DH-MHZ013-ED1", "name": "得力螺丝刀套装"}, {"sku": "SKU-000123", "name": "USB充电线1m白色"}])
    idx = matcher.Index()
    assert idx.match("dh-mhz013-ed1")["method"] == "精确"
    assert idx.match("DHMHZ013ED1")["sku"] == "DH-MHZ013-ED1"
    assert idx.match("SKU-OOO123")["sku"] == "SKU-000123"          # O→0 纠错
    assert idx.match("SKU-000128")["sku"] is None or idx.match("SKU-000128")["method"] != "精确"
    assert idx.match("???", name="USB充电线1m白色")["method"] == "品名"
    assert idx.match("NOPE")["sku"] is None


def test_query_run(saved_map):
    skus.upsert([{"sku": "A1", "name": "扳手"}, {"sku": "B2", "name": "钳子"}, {"sku": "C3", "name": "锤子"}])
    stock.apply([Entry("A1", "01-B-01-1-0", 10), Entry("A1", "01-A-02-0-0", 2), Entry("B2", "01-A-01-3-0")], "聊天")
    res = query.run([{"sku": "A1", "qty": 15}, {"sku": "b2", "qty": 1}, {"sku": "C3", "qty": 1},
                     {"sku": "ZZZ", "qty": 1}, {"sku": "a1", "qty": 1}])
    items = {it["sku"]: it for it in res["items"]}
    assert items["A1"]["qty"] == 16 and items["A1"]["total"] == 12
    assert "多库位" in items["A1"]["flags"] and "余量不足" in items["A1"]["flags"]
    assert items["B2"]["total"] is None and "余量不足" not in items["B2"]["flags"]
    assert items["C3"]["flags"] == ["未登记"]
    assert [u["input"] for u in res["unmatched"]] == ["ZZZ"]
    order = [(ln["seq"], ln["row"]["loc_code"]) for ln in res["lines"]]
    assert order == [(1, "01-A-01-3-0"), (2, "01-A-02-0-0"), (3, "01-B-01-1-0")]
    assert res["warehouses"] == {"01": {("A", "01"): 1, ("A", "02"): 2, ("B", "01"): 3}}


def test_read_table_detects_columns():
    from openpyxl import Workbook
    import io
    wb = Workbook()
    ws = wb.active
    ws.append(["拣货单 2026-09-25"])
    ws.append(["序号", "商品名称", "商家SKU", "拣货数量"])
    ws.append([1, "扳手", "A1", 2])
    ws.append([2, "钳子", "B2", 1])
    ws.append(["合计", "", "", 3])
    buf = io.BytesIO()
    wb.save(buf)
    rows = skus.rows_from_table(skus.read_table("pick.xlsx", buf.getvalue()))
    assert [(r["sku"], r["name"], r["qty"]) for r in rows] == [("A1", "扳手", "2"), ("B2", "钳子", "1")]
