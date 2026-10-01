"""仓库地图：JSON 结构、校验、保存（同步生成区块/货架表）。

地图 JSON（编辑器与服务端共用）：
{
  "wh_no": "01", "name": "一号仓",
  "width": 1600, "height": 1000, "cell": 20,      # 图片尺寸与网格细分尺寸(px)
  "cols": 80, "rows": 50,                         # = width // cell, height // cell
  "types": ["0012...", ...],                      # 每行一个字符串，0未定义 1功能 2库存 3走廊
  "bids":  [[0, 0, 5, ...], ...],                 # 每格所属地图区块 id，0 = 无
  "zones": ["..AAA..", ...],                      # 仓库区块字母，'.' = 未划分
  "blocks": {"5": {"type": "storage", "shelf": "03", "layer_min": 1, "layer_max": 4},
             "6": {"type": "function", "label": "打包台"}},
  "zone_meta": {"A": {"color": "#4f86f7"}},
  "next_id": 7,
  "bg": {"file": "", "opacity": 0.5, "show": true}
}
"""
import json
import re

from . import db

T_UNDEF, T_FUNC, T_STORAGE, T_CORRIDOR = 0, 1, 2, 3
TYPE_OF_BLOCK = {"function": T_FUNC, "storage": T_STORAGE}

DEFAULT_ZONE_COLORS = [
    "#2f6fdb", "#d9480f", "#2b8a3e", "#ae3ec9", "#e67700", "#0c8599", "#c2255c", "#5c940d",
    "#1864ab", "#a61e4d", "#087f5b", "#862e9c", "#d6336c", "#364fc7", "#f08c00", "#495057",
]


def zone_color(letter: str) -> str:
    return DEFAULT_ZONE_COLORS[(ord(letter) - 65) % len(DEFAULT_ZONE_COLORS)]


def blank_map(wh_no: str, name: str = "", width: int = 1600, height: int = 1000, cell: int = 20) -> dict:
    cols, rows = max(1, width // cell), max(1, height // cell)
    return {
        "wh_no": wh_no, "name": name, "width": width, "height": height, "cell": cell,
        "cols": cols, "rows": rows,
        "types": ["0" * cols for _ in range(rows)],
        "bids": [[0] * cols for _ in range(rows)],
        "zones": ["." * cols for _ in range(rows)],
        "blocks": {}, "zone_meta": {}, "next_id": 1,
        "bg": {"file": "", "opacity": 0.5, "show": True},
    }


def analyze(m: dict) -> dict:
    """计算每个区块的格子/归属区块字母，并给出错误与警告（不访问数据库）。"""
    errors, warnings = [], []
    rows, cols = int(m.get("rows", 0)), int(m.get("cols", 0))
    types, bids, zones = m.get("types", []), m.get("bids", []), m.get("zones", [])
    if len(types) != rows or len(bids) != rows or len(zones) != rows or any(
            len(types[r]) != cols or len(bids[r]) != cols or len(zones[r]) != cols for r in range(rows)):
        return {"blocks": {}, "zones": {}, "errors": [{"msg": "地图数据尺寸不一致，请重新保存或重建地图"}],
                "warnings": []}

    blocks_meta = {str(k): v for k, v in (m.get("blocks") or {}).items()}
    info = {}
    zone_cells = {}
    orphan_storage = 0
    for r in range(rows):
        trow, brow, zrow = types[r], bids[r], zones[r]
        for c in range(cols):
            z = zrow[c]
            if z != ".":
                zone_cells.setdefault(z, []).append((r, c))
            t = int(trow[c])
            bid = str(brow[c])
            if bid == "0" or bid not in blocks_meta:
                if t == T_STORAGE:
                    orphan_storage += 1
                continue
            meta = blocks_meta[bid]
            if TYPE_OF_BLOCK.get(meta.get("type")) != t:
                continue
            b = info.setdefault(bid, {"id": int(bid), **meta, "cells": [], "zone_set": set()})
            b["cells"].append((r, c))
            if z != ".":
                b["zone_set"].add(z)
            else:
                b["zone_set"].add(".")

    seen = {}
    for bid, b in info.items():
        rs = [p[0] for p in b["cells"]]
        cs = [p[1] for p in b["cells"]]
        b["bbox"] = (min(rs), min(cs), max(rs), max(cs))
        zs = b.pop("zone_set")
        b["zone"] = ""
        if b["type"] != "storage":
            if not (b.get("label") or "").strip():
                warnings.append({"block": b["id"], "msg": "功能区块未填写文字标注"})
            continue
        real = zs - {"."}
        where = f"货架(区块#{b['id']})"
        if len(real) == 1 and "." not in zs:
            b["zone"] = next(iter(real))
        elif not real:
            errors.append({"block": b["id"], "msg": f"{where} 没有落在任何仓库区块(字母)内"})
        elif len(real) > 1:
            errors.append({"block": b["id"], "msg": f"{where} 跨越了多个仓库区块：{'、'.join(sorted(real))}"})
        else:
            b["zone"] = next(iter(real))
            errors.append({"block": b["id"], "msg": f"{where} 部分格子不在仓库区块 {b['zone']} 内"})
        shelf = str(b.get("shelf") or "")
        if not re.fullmatch(r"\d{2}", shelf):
            errors.append({"block": b["id"], "msg": f"{where} 货架编码必须是两位数字（当前：{shelf or '未填'}）"})
        else:
            where = f"货架 {b['zone'] or '?'}-{shelf}"
        try:
            lo, hi = int(b.get("layer_min", 1)), int(b.get("layer_max", 1))
            if not (0 <= lo <= hi <= 99):
                raise ValueError
        except (TypeError, ValueError):
            errors.append({"block": b["id"], "msg": f"{where} 层号范围无效（应为 0–99，且最低层 ≤ 最高层）"})
        try:
            if not (1 <= int(b.get("regions", 1)) <= 99):
                raise ValueError
        except (TypeError, ValueError):
            errors.append({"block": b["id"], "msg": f"{where} 每层区域数无效（应为 1–99）"})
        if b["zone"] and re.fullmatch(r"\d{2}", shelf):
            key = (b["zone"], shelf)
            if key in seen:
                errors.append({"block": b["id"],
                               "msg": f"货架编码重复：{b['zone']}-{shelf}（区块#{seen[key]} 与 #{b['id']}）"})
            else:
                seen[key] = b["id"]
    if orphan_storage:
        warnings.append({"msg": f"有 {orphan_storage} 个库存格子不属于任何货架"})
    zinfo = {}
    for z, cells in zone_cells.items():
        rs = [p[0] for p in cells]
        cs = [p[1] for p in cells]
        zinfo[z] = {"cells": len(cells), "center": (sum(rs) / len(rs), sum(cs) / len(cs)),
                    "color": (m.get("zone_meta") or {}).get(z, {}).get("color") or zone_color(z)}
    return {"blocks": info, "zones": zinfo, "errors": errors, "warnings": warnings}


def _stock_conflicts(wh_no: str, shelves: dict) -> list:
    """保存前检查：地图里删掉/缩减的货架上是否还有库存登记。"""
    errors = []
    rows = db.query("SELECT zone, shelf_no, layer, COUNT(*) n FROM current_stock WHERE wh_no=? "
                    "GROUP BY zone, shelf_no, layer", (wh_no,))
    for r in rows:
        s = shelves.get((r["zone"], r["shelf_no"]))
        where = f"{wh_no}-{r['zone']}-{r['shelf_no']}"
        if s is None:
            errors.append({"msg": f"货架 {where} 上还有 {r['n']} 条库存登记，不能删除或改编码（请先移除登记）"})
        elif not (s["layer_min"] <= r["layer"] <= s["layer_max"]):
            errors.append({"block": s["id"],
                           "msg": f"货架 {where} 第 {r['layer']} 层还有 {r['n']} 条库存登记，层号范围必须包含该层"})
    # 区域数写在库位码里（3/5），货架上已有登记时不能改
    old = {(r["zone"], r["shelf_no"]): r["regions"]
           for r in db.query("SELECT zone, shelf_no, regions FROM shelf WHERE wh_no=?", (wh_no,))}
    for key in {(r["zone"], r["shelf_no"]) for r in rows}:
        s = shelves.get(key)
        if s and old.get(key) not in (None, s["regions"]):
            errors.append({"block": s["id"], "msg": f"货架 {wh_no}-{key[0]}-{key[1]} 上已有库存登记，"
                                                   f"不能把每层区域数从 {old[key]} 改成 {s['regions']}（请先移除登记）"})
    return errors


def validate(m: dict) -> dict:
    a = analyze(m)
    shelves = {(b["zone"], b["shelf"]): {"id": b["id"], "layer_min": int(b.get("layer_min", 1)),
                                         "layer_max": int(b.get("layer_max", 1)),
                                         "regions": int(b.get("regions", 1) or 1)}
               for b in a["blocks"].values() if b["type"] == "storage" and b["zone"]}
    a["errors"] += _stock_conflicts(m["wh_no"], shelves)
    return a


def save(m: dict) -> dict:
    """校验通过才保存；同时重建该仓库的 zone / shelf 表。"""
    wh = str(m.get("wh_no", ""))
    if not re.fullmatch(r"\d{2}", wh):
        return {"ok": False, "errors": [{"msg": "仓库号必须是两位数字"}], "warnings": []}
    a = validate(m)
    if a["errors"]:
        return {"ok": False, "errors": a["errors"], "warnings": a["warnings"]}
    with db.tx() as c:
        c.execute("INSERT INTO warehouse(wh_no, name, map_json, updated_at) VALUES(?,?,?,?) "
                  "ON CONFLICT(wh_no) DO UPDATE SET name=excluded.name, map_json=excluded.map_json, "
                  "updated_at=excluded.updated_at",
                  (wh, m.get("name", ""), json.dumps(m, ensure_ascii=False, separators=(",", ":")), db.now()))
        c.execute("DELETE FROM zone WHERE wh_no=?", (wh,))
        c.execute("DELETE FROM shelf WHERE wh_no=?", (wh,))
        for z, zi in a["zones"].items():
            c.execute("INSERT INTO zone(wh_no, letter, color) VALUES(?,?,?)", (wh, z, zi["color"]))
        for b in a["blocks"].values():
            if b["type"] == "storage":
                c.execute("INSERT INTO shelf(wh_no, zone, shelf_no, layer_min, layer_max, regions, block_id) "
                          "VALUES(?,?,?,?,?,?,?)",
                          (wh, b["zone"], b["shelf"], int(b["layer_min"]), int(b["layer_max"]),
                           int(b.get("regions", 1) or 1), b["id"]))
    n_shelf = sum(1 for b in a["blocks"].values() if b["type"] == "storage")
    return {"ok": True, "errors": [], "warnings": a["warnings"], "shelves": n_shelf, "zones": len(a["zones"])}


def get(wh_no: str) -> dict | None:
    row = db.query_one("SELECT map_json FROM warehouse WHERE wh_no=?", (wh_no,))
    return json.loads(row["map_json"]) if row and row["map_json"] else None


def list_warehouses() -> list:
    return db.query("SELECT w.wh_no, w.name, w.updated_at, "
                    "(SELECT COUNT(*) FROM shelf s WHERE s.wh_no=w.wh_no) AS shelves, "
                    "(SELECT COUNT(*) FROM zone z WHERE z.wh_no=w.wh_no) AS zones "
                    "FROM warehouse w ORDER BY w.wh_no")


def delete(wh_no: str) -> dict:
    n = db.query_one("SELECT COUNT(*) n FROM current_stock WHERE wh_no=?", (wh_no,))["n"]
    if n:
        return {"ok": False, "errors": [{"msg": f"仓库 {wh_no} 还有 {n} 条库存登记，不能删除"}]}
    with db.tx() as c:
        for t in ("warehouse", "zone", "shelf"):
            c.execute(f"DELETE FROM {t} WHERE wh_no=?", (wh_no,))
    return {"ok": True}


def find_shelf(wh: str, zone: str, shelf_no: str):
    return db.query_one("SELECT * FROM shelf WHERE wh_no=? AND zone=? AND shelf_no=?", (wh, zone, shelf_no))
