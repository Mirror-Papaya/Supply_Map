"""库存登记与查询。

规则：
- 登记只追加日志（stock_log），从不覆盖历史。
- 余量是盘点数、选填；当前余量 = 该 SKU 在该库位最近一条填写了余量的登记（最后一次"移除"之后）。
- "移除" 表示 SKU 已离开该库位；之后重新登记，余量从新登记开始算。
"""
from dataclasses import dataclass, field

from . import codes, config, db, maps, skus

REGISTER, REMOVE = "登记", "移除"
METHOD_MANUAL, METHOD_AUTO = "手动入库", "自动入库"   # 网页手动录入 / 飞书群聊里发指令给机器人


@dataclass
class Entry:
    sku: str
    loc_text: str
    qty: int | None = None
    action: str = REGISTER
    note: str = ""
    source_id: str | None = None   # 去重键：多维表格 record_id / 飞书消息 id + 行号
    label: str = ""                # 反馈时用来指代这一条，比如 "第2行"


@dataclass
class Result:
    entry: Entry
    ok: bool
    msg: str = ""
    code: str = ""
    sku: str = ""
    warnings: list = field(default_factory=list)
    duplicate: bool = False


def check(e: Entry) -> Result:
    """只校验不写入。"""
    res = Result(entry=e, ok=False)
    if e.action not in (REGISTER, REMOVE):
        res.msg = f"未知操作：{e.action}"
        return res
    sku_text = (e.sku or "").strip()
    if not sku_text:
        res.msg = "缺少 SKU"
        return res
    loc = codes.parse(e.loc_text or "")
    if not loc:
        res.msg = f"库位码格式不对：{e.loc_text or '（空）'}，应为 仓库号-区块-货架-层-区域/总数，如 01-A-03-02-3/5"
        return res
    if not loc.wh:      # 省略了仓库号：只有一个仓库时自动补上
        whs = db.query("SELECT wh_no FROM warehouse")
        if len(whs) != 1:
            res.msg = f"库位码里要写仓库号，如 01-{loc.zone}-{loc.shelf}-{loc.layer:02d}-{loc.region}/{loc.total or 'N'}"
            return res
        loc = loc._replace(wh=whs[0]["wh_no"])
    res.code = loc.code
    shelf = maps.find_shelf(loc.wh, loc.zone, loc.shelf)
    if not shelf:
        if not db.query_one("SELECT 1 FROM warehouse WHERE wh_no=?", (loc.wh,)):
            res.msg = f"仓库 {loc.wh} 还没有地图"
        elif not db.query_one("SELECT 1 FROM zone WHERE wh_no=? AND letter=?", (loc.wh, loc.zone)):
            res.msg = f"仓库 {loc.wh} 没有区块 {loc.zone}"
        else:
            res.msg = f"区块 {loc.wh}-{loc.zone} 没有 {loc.shelf} 号货架"
        return res
    if not (shelf["layer_min"] <= loc.layer <= shelf["layer_max"]):
        res.msg = f"货架 {loc.shelf_key} 的层号范围是 {shelf['layer_min']}–{shelf['layer_max']}，没有第 {loc.layer} 层"
        return res
    n = shelf["regions"]
    legacy = None
    if e.action == REMOVE:
        # 按旧规则(件位)登记的记录，区域号可能超出现在的区域数、库位码也没有"/总数"：仍然允许按位置移除
        legacy = db.query_one("SELECT loc_code FROM current_stock WHERE UPPER(sku)=? AND wh_no=? AND zone=? "
                              "AND shelf_no=? AND layer=? AND region=? AND loc_code NOT LIKE '%/%'",
                              (skus.key(sku_text), loc.wh, loc.zone, loc.shelf, loc.layer, loc.region))
    if legacy:
        res.code = legacy["loc_code"]
    else:
        if loc.total is not None and loc.total != n:
            res.msg = f"货架 {loc.shelf_key} 每层分成 {n} 个区域，不是 {loc.total} 个"
            return res
        if loc.region > n:
            res.msg = f"货架 {loc.shelf_key} 每层只有 {n} 个区域，没有第 {loc.region} 区域"
            return res
        loc = loc._replace(total=n)
        res.code = loc.code
    known = skus.lookup(sku_text)
    if known:
        res.sku = known["sku"]
    else:
        # 字典外的 SKU：沿用已登记过的写法（大小写一致），否则原样
        prev = db.query_one("SELECT sku FROM current_stock WHERE UPPER(sku)=?", (skus.key(sku_text),))
        res.sku = prev["sku"] if prev else sku_text
        if config.STRICT_SKU:
            res.msg = f"SKU {sku_text} 不在商品字典中"
            return res
        if skus.count():
            res.warnings.append("SKU 不在商品字典中")
    if e.qty is not None and e.qty < 0:
        res.msg = "余量不能是负数"
        return res
    if e.action == REMOVE and not db.query_one(
            "SELECT 1 FROM current_stock WHERE sku=? AND loc_code=?", (res.sku, res.code)):
        res.msg = f"{res.code} 上没有登记 {res.sku}，无需移除"
        return res
    res.ok = True
    return res


def apply(entries: list, method: str, user_id: str = "", user_name: str = "", created_at: str | None = None) -> list:
    """校验并写入。每条独立成败，互不影响。"""
    results = []
    for e in entries:
        if e.source_id and db.query_one("SELECT 1 FROM stock_log WHERE source_id=?", (e.source_id,)):
            results.append(Result(entry=e, ok=False, msg="已经处理过，忽略重复提交", duplicate=True))
            continue
        r = check(e)
        if r.ok:
            _write(r, method, user_id, user_name, created_at or db.now())
        results.append(r)
    return results


def _write(r: Result, method, user_id, user_name, at):
    e = r.entry
    loc = codes.parse(r.code)
    with db.tx() as c:
        c.execute("INSERT INTO stock_log(sku, loc_code, action, qty, created_at, method, user_id, user_name, "
                  "source_id, note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (r.sku, r.code, e.action, e.qty, at, method, user_id, user_name, e.source_id, e.note or ""))
        _apply_to_current(c, r.sku, loc, e.action, e.qty, at, user_name or user_id, method)


def _apply_to_current(c, sku, loc, action, qty, at, who, method):
    if action == REMOVE:
        c.execute("DELETE FROM current_stock WHERE sku=? AND loc_code=?", (sku, loc.code))
        return
    old = c.execute("SELECT qty, qty_at, qty_by FROM current_stock WHERE sku=? AND loc_code=?",
                    (sku, loc.code)).fetchone()
    if qty is not None:
        q, q_at, q_by = qty, at, who
    elif old:
        q, q_at, q_by = old["qty"], old["qty_at"], old["qty_by"]
    else:
        q = q_at = q_by = None
    c.execute("INSERT INTO current_stock(sku, loc_code, wh_no, zone, shelf_no, layer, region, qty, qty_at, qty_by, "
              "last_at, last_by, last_method) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
              "ON CONFLICT(sku, loc_code) DO UPDATE SET qty=excluded.qty, qty_at=excluded.qty_at, "
              "qty_by=excluded.qty_by, last_at=excluded.last_at, last_by=excluded.last_by, "
              "last_method=excluded.last_method",
              (sku, loc.code, loc.wh, loc.zone, loc.shelf, loc.layer, loc.region, q, q_at, q_by, at, who, method))


def rebuild_current() -> int:
    """从日志完整重算 current_stock（修复/校验用）。"""
    logs = db.query("SELECT * FROM stock_log ORDER BY created_at, id")
    with db.tx() as c:
        c.execute("DELETE FROM current_stock")
        for g in logs:
            loc = codes.parse(g["loc_code"])
            if not loc:      # 旧格式里件位为 0 的历史记录，无法对应到新库位码，跳过
                continue
            _apply_to_current(c, g["sku"], loc, g["action"], g["qty"], g["created_at"],
                              g["user_name"] or g["user_id"], g["method"])
    return len(logs)


def locations_of(sku_list) -> dict:
    """{sku: [current_stock 行, ...]}，按拣货路径排序。"""
    sku_list = list(dict.fromkeys(sku_list))
    if not sku_list:
        return {}
    marks = ",".join("?" * len(sku_list))
    rows = db.query(f"SELECT * FROM current_stock WHERE sku IN ({marks}) "
                    f"ORDER BY wh_no, zone, shelf_no, layer, region", sku_list)
    out = {s: [] for s in sku_list}
    for r in rows:
        out[r["sku"]].append(r)
    return out


def recent_logs(limit=100, sku: str | None = None) -> list:
    if sku:
        return db.query("SELECT * FROM stock_log WHERE UPPER(sku)=? ORDER BY id DESC LIMIT ?", (skus.key(sku), limit))
    return db.query("SELECT * FROM stock_log ORDER BY id DESC LIMIT ?", (limit,))


def shelf_contents(wh, zone, shelf_no) -> list:
    return db.query("SELECT * FROM current_stock WHERE wh_no=? AND zone=? AND shelf_no=? "
                    "ORDER BY layer, region, sku", (wh, zone, shelf_no))


def stats() -> dict:
    one = lambda sql: db.query_one(sql)["n"]
    return {
        "warehouses": one("SELECT COUNT(*) n FROM warehouse"),
        "shelves": one("SELECT COUNT(*) n FROM shelf"),
        "skus": one("SELECT COUNT(*) n FROM sku"),
        "stock_rows": one("SELECT COUNT(*) n FROM current_stock"),
        "stock_skus": one("SELECT COUNT(DISTINCT sku) n FROM current_stock"),
        "logs": one("SELECT COUNT(*) n FROM stock_log"),
    }
