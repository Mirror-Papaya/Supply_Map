"""手动入库：层 + 区域(k/N) → 库位码，货架清单，登记人/登记时间，移除。"""
from fastapi.testclient import TestClient

from app import db, maps, stock, web
from app.stock import Entry


def _post(c, **kw):
    body = {"sku": "B40006", "wh": "01", "zone": "A", "shelf": "01", "layer": 3, "region": 3, "who": "测试"}
    body.update(kw)
    return c.post("/api/stock", json=body)


def test_inbound_builds_region_code(saved_map):
    c = TestClient(web.app)
    r = _post(c)
    assert r.status_code == 200 and r.json()["code"] == "01-A-01-03-3/5"      # 总数 5 来自货架设置
    rows = c.get("/api/shelf/01/A/01").json()
    assert [(x["sku"], x["layer"], x["region"], x["last_by"], x["last_method"]) for x in rows] == \
        [("B40006", 3, 3, "测试", "手动入库")]
    assert stock.locations_of(["B40006"])["B40006"][0]["loc_code"] == "01-A-01-03-3/5"


def test_log_records_who_and_submit_time(saved_map):
    c = TestClient(web.app)
    r = _post(c).json()
    log = c.get("/api/logs").json()[0]
    assert log["user_name"] == "测试" and log["method"] == "手动入库" and log["loc_code"] == "01-A-01-03-3/5"
    assert log["created_at"] == r["at"] and len(r["at"]) == 19                 # 提交时间由服务器自动记录
    assert _post(c, who="").status_code == 200
    assert c.get("/api/logs").json()[0]["user_name"] == "未填"


def test_one_region_holds_many_skus(saved_map):
    c = TestClient(web.app)
    assert _post(c).status_code == 200
    r = _post(c, sku="B40007")                       # 同一层同一区域放多种商品是正常的
    assert r.status_code == 200 and r.json()["warnings"] == [] or "字典" in "".join(r.json()["warnings"])
    assert sorted(x["sku"] for x in c.get("/api/shelf/01/A/01").json()) == ["B40006", "B40007"]


def test_shelf_list_has_regions_and_counts(saved_map):
    c = TestClient(web.app)
    _post(c); _post(c, sku="B40007", region=4)
    shelves = {(s["zone"], s["shelf_no"]): s for s in c.get("/api/shelves/01").json()}
    assert shelves[("A", "01")]["n"] == 2 and shelves[("A", "01")]["regions"] == 5
    assert shelves[("B", "01")]["n"] == 0


def test_inbound_rejects_bad_position(saved_map):
    c = TestClient(web.app)
    assert _post(c, region=0).status_code == 400            # 区域从 1 开始
    r = _post(c, region=6)                                  # 每层只有 5 个区域
    assert r.status_code == 400 and "只有 5 个区域" in r.json()["detail"]
    assert _post(c, layer=100).status_code == 400
    r = _post(c, layer=9)                                   # 货架只有 1–4 层
    assert r.status_code == 400 and "层号范围" in r.json()["detail"]
    assert _post(c, shelf="09").status_code == 400          # 没有这个货架
    assert c.get("/api/shelf/01/A/01").json() == []


def test_remove(saved_map):
    c = TestClient(web.app)
    _post(c); _post(c, sku="B40007")
    assert _post(c, action="移除").status_code == 200
    assert [x["sku"] for x in c.get("/api/shelf/01/A/01").json()] == ["B40007"]
    assert _post(c, action="移除").status_code == 400       # 已经不在了


def test_chat_code_without_warehouse_and_total(saved_map):
    r = stock.apply([Entry("B40006", "A-01-03-3/5")], "自动入库", "u1", "李四", created_at="2026-10-01 09:30:00")[0]
    assert r.ok and r.code == "01-A-01-03-3/5"              # 只有一个仓库：自动补仓库号
    assert db.query_one("SELECT created_at, user_name FROM stock_log")["created_at"] == "2026-10-01 09:30:00"
    bad = stock.apply([Entry("B40006", "A-01-03-3/6")], "自动入库")[0]
    assert not bad.ok and "每层分成 5 个区域" in bad.msg


def test_regions_cannot_change_when_shelf_has_stock(saved_map):
    assert stock.apply([Entry("B40006", "01-A-01-03-3/5")], "手动入库")[0].ok
    m = maps.get("01")
    m["blocks"]["1"]["regions"] = 6
    res = maps.save(m)
    assert not res["ok"] and any("每层区域数" in e["msg"] for e in res["errors"])
    m["blocks"]["2"]["regions"] = 7                          # 没有登记的货架可以改
    m["blocks"]["1"]["regions"] = 5
    assert maps.save(m)["ok"]
    assert {s["shelf_no"]: s["regions"] for s in db.query("SELECT * FROM shelf WHERE zone='A'")}["02"] == 7


def test_legacy_row_from_old_slot_rule_can_still_be_removed(saved_map):
    from app import codes
    with db.tx() as c:      # 旧规则登记的记录：没有 /总数，区域号 6 超过货架现在的 5 个区域
        stock._apply_to_current(c, "B40006", codes.parse("01-A-01-03-06"), stock.REGISTER, 80,
                                "2026-10-01 12:00:00", "网页", "网页")
    assert stock.shelf_contents("01", "A", "01")[0]["loc_code"] == "01-A-01-03-6"
    c = TestClient(web.app)
    assert _post(c, region=6, action="移除").status_code == 200
    assert stock.shelf_contents("01", "A", "01") == []
    assert _post(c, region=6).status_code == 400            # 新入库仍然必须在 1–5 个区域之内


def test_chat_quantity_wording():
    from app import parse_text
    for text, qty in [("入库 22001 A-01-03-2/5, 共58件", 58), ("入库 22001 A-01-03-2/5 共计58个", 58),
                      ("入库 22001 A-01-03-2/5 余量58", 58), ("入库 22001 A-01-03-2/5 x58", 58),
                      ("入库 22001 A-01-03-2/5 58", 58), ("入库 22001 A-01-03-2/5", None)]:
        p = parse_text.parse(text)
        assert not p.errors, (text, p.errors)
        assert (p.entries[0].sku, p.entries[0].loc_text, p.entries[0].qty) == ("22001", "A-01-03-2/5", qty), text
    assert parse_text.parse("入库 22001 A-01-03-2/5 共五十八件").errors      # 不是数字就还是报错


def test_query_does_not_treat_sku_as_quantity():
    from app import parse_text
    p = parse_text.parse("查询 A1 X100 B2")
    assert [d["sku"] for d in p.demands] == ["A1", "X100", "B2"]
