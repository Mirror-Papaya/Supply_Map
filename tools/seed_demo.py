"""测试数据：在 01 号仓库地图上加 10 个货架，再随机登记约 100 条库存，用来试"拣货单 → 地图红点定位"。

    python tools/seed_demo.py            # 加货架 + 加库存（可重复运行，不会重复加）
    python tools/seed_demo.py --remove   # 一键清除：删掉测试库存和这 10 个测试货架

测试库存的登记人都是"测试数据"，只有这些会被清除，其他人的登记不受影响。
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, maps, stock  # noqa: E402

WH = "01"
WHO = "测试数据"

# (区块字母, 货架编码, 行0, 列0, 行1, 列1, 每层区域数)  —— 都是左上到右下、包含端点的格子范围
SHELVES = [
    ("A", "03", 12, 4, 16, 23, 5), ("A", "04", 12, 30, 16, 49, 5),
    ("B", "01", 34, 4, 38, 23, 4), ("B", "02", 34, 30, 38, 49, 4),
    ("B", "03", 42, 4, 46, 23, 4), ("B", "04", 42, 30, 46, 49, 4),
    ("C", "01", 15, 60, 23, 63, 3), ("C", "02", 15, 70, 23, 73, 3),
    ("C", "03", 33, 60, 45, 63, 3), ("C", "04", 33, 70, 45, 73, 3),
]
# 仓库区块（字母覆盖层）：B 区在走廊下方，C 区在右侧（避开走廊所在的 25–30 行）
ZONES = [("B", 32, 1, 47, 54), ("C", 14, 57, 24, 78), ("C", 31, 57, 47, 78)]
# 拣货单里常出现的 SKU，保证测试时一定有库存可以定位
MUST_HAVE = ["DL016", "DL110106", "30001", "B40006", "22001", "540013"]


def _grid(m):
    return [list(r) for r in m["types"]], [list(r) for r in m["bids"]], [list(r) for r in m["zones"]]


def _save(m, types, bids, zones):
    m["types"] = ["".join(r) for r in types]
    m["bids"] = bids
    m["zones"] = ["".join(r) for r in zones]
    res = maps.save(m)
    if not res["ok"]:
        raise SystemExit("保存地图失败：" + "；".join(e["msg"] for e in res["errors"]))


def add_shelves():
    m = maps.get(WH)
    if not m:
        raise SystemExit(f"仓库 {WH} 还没有地图，请先在地图编辑器里建好")
    types, bids, zones = _grid(m)
    existing = maps.analyze(m)["blocks"]
    exists = {(b["zone"], b["shelf"]) for b in existing.values() if b["type"] == "storage"}
    added = 0
    for z, r0, c0, r1, c1 in ZONES:
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                zones[r][c] = z
    for z, no, r0, c0, r1, c1, regions in SHELVES:
        if (z, no) in exists:
            continue
        bid = m["next_id"]
        m["next_id"] = bid + 1
        m["blocks"][str(bid)] = {"type": "storage", "shelf": no, "layer_min": 1, "layer_max": 4, "regions": regions}
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                types[r][c] = str(maps.T_STORAGE)
                bids[r][c] = bid
        added += 1
    _save(m, types, bids, zones)
    print(f"货架：新增 {added} 个（共 {len(SHELVES)} 个测试货架）")


def seed_stock(n=100, seed=20261001):
    rnd = random.Random(seed)
    shelves = db.query("SELECT zone, shelf_no, layer_min, layer_max, regions FROM shelf WHERE wh_no=?", (WH,))
    if not shelves:
        raise SystemExit("还没有货架")
    if db.query_one("SELECT 1 FROM stock_log WHERE user_name=? LIMIT 1", (WHO,)):
        print("已经有测试库存了（要重来请先加 --remove）")
        return
    pool = [r["sku"] for r in db.query("SELECT sku FROM sku ORDER BY sku")]
    must = [s for s in MUST_HAVE if s in set(pool)]
    chosen = list(dict.fromkeys(must + rnd.sample(pool, min(len(pool), n))))[:n]
    entries = []
    for sku in chosen:
        s = rnd.choice(shelves)
        entries.append(stock.Entry(
            sku=sku, note="测试数据", qty=rnd.choice([None, rnd.randint(1, 200)]),
            loc_text=f"{WH}-{s['zone']}-{s['shelf_no']}-{rnd.randint(s['layer_min'], s['layer_max']):02d}-"
                     f"{rnd.randint(1, s['regions'])}/{s['regions']}"))
    ok = 0
    for r in stock.apply(entries, stock.METHOD_MANUAL, "seed", WHO):
        ok += r.ok
        if not r.ok:
            print("  跳过：", r.entry.sku, r.entry.loc_text, r.msg)
    print(f"库存：登记 {ok} 条测试库存（SKU 共 {len(chosen)} 种）")


def remove():
    with db.tx() as c:
        n = c.execute("DELETE FROM stock_log WHERE user_name=?", (WHO,)).rowcount
    stock.rebuild_current()
    print(f"已删除 {n} 条测试库存日志")
    m = maps.get(WH)
    types, bids, zones = _grid(m)
    existing = maps.analyze(m)["blocks"]
    mine = {(z, no) for z, no, *_ in SHELVES}
    gone = {b["id"] for b in existing.values() if b["type"] == "storage" and (b["zone"], b["shelf"]) in mine}
    for r in range(m["rows"]):
        for c in range(m["cols"]):
            if bids[r][c] in gone:
                bids[r][c], types[r][c] = 0, "0"
    for z, r0, c0, r1, c1 in ZONES:
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                zones[r][c] = "."
    for bid in gone:
        m["blocks"].pop(str(bid), None)
    _save(m, types, bids, zones)
    print(f"已删除 {len(gone)} 个测试货架")


if __name__ == "__main__":
    db.connect()
    if "--remove" in sys.argv:
        remove()
    else:
        add_shelves()
        seed_stock()
