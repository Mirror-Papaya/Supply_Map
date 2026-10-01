"""拣货查询：拣货需求（SKU + 需求数量）→ 库位、余量、拣货顺序、异常提醒。

各种输入（文本 / 图片识别 / Excel、CSV）都先转成统一的需求列表：
    [{"sku": "原始SKU文本", "qty": 3 或 None, "name": "识别到的品名", "alt": ["其他编码"]}]
"""
from . import matcher, skus, stock


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def run(demands: list) -> dict:
    idx = matcher.Index()
    merged = {}
    unmatched = []
    for d in demands:
        m = idx.match(d.get("sku", ""), d.get("name", ""))
        if not m["sku"]:
            for alt in d.get("alt") or []:
                m2 = idx.match(alt)
                if m2["sku"]:
                    m = m2
                    break
        if not m["sku"]:
            unmatched.append({"input": d.get("sku", ""), "name": d.get("name", ""), "qty": d.get("qty"),
                              "candidates": m["candidates"]})
            continue
        it = merged.setdefault(m["sku"], {"sku": m["sku"], "qty": None, "inputs": [], "methods": set(),
                                          "ocr_name": d.get("name", "")})
        it["qty"] = _add(it["qty"], d.get("qty"))
        it["inputs"].append(d.get("sku", ""))
        it["methods"].add(m["method"])

    locs = stock.locations_of(merged)
    names = skus.names(merged)
    for sku, it in merged.items():
        rows = locs[sku]
        it["name"] = names.get(sku) or it["ocr_name"]
        it["locations"] = rows
        known = [r["qty"] for r in rows if r["qty"] is not None]
        it["total"] = sum(known) if known else None
        it["fuzzy"] = bool(it["methods"] - {"精确"})
        flags = []
        if not rows:
            flags.append("未登记")
        else:
            if len(rows) > 1:
                flags.append("多库位")
            if it["qty"] is not None and it["total"] is not None and it["total"] < it["qty"]:
                flags.append("余量不足")
        if it["fuzzy"]:
            flags.append("识别纠错")
        it["flags"] = flags

    # 拣货顺序：按 仓库→区块→货架→层→区域 排，同一货架共用一个序号
    lines = sorted(((r, merged[sku]) for sku in merged for r in locs[sku]),
                   key=lambda x: (x[0]["wh_no"], x[0]["zone"], x[0]["shelf_no"], x[0]["layer"], x[0]["region"],
                                  x[0]["sku"]))
    seq_of_shelf = {}
    out_lines = []
    for r, it in lines:
        key = (r["wh_no"], r["zone"], r["shelf_no"])
        seq = seq_of_shelf.setdefault(key, len(seq_of_shelf) + 1)
        out_lines.append({"seq": seq, "row": r, "item": it})
    warehouses = {}
    for (wh, zone, shelf_no), seq in seq_of_shelf.items():
        warehouses.setdefault(wh, {})[(zone, shelf_no)] = seq
    return {
        "items": list(merged.values()),
        "lines": out_lines,
        "unregistered": [it for it in merged.values() if not it["locations"]],
        "unmatched": unmatched,
        "warehouses": warehouses,
        "demand_rows": len(demands),
    }
