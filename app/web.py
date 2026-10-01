"""本地网页：地图编辑器 + 管理页（SKU 字典导入、库存查询、登记日志）。"""
import re
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import codes, config, db, maps, parse_text, query, render, skus, status, stock, vision

app = FastAPI(title="库存地图", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=config.WEB_DIR), name="static")


@app.middleware("http")
async def no_cache(request, call_next):
    # 本地工具，页面脚本更新后要立即生效
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def _wh(wh_no: str) -> str:
    if not re.fullmatch(r"\d{2}", wh_no):
        raise HTTPException(400, "仓库号必须是两位数字")
    return wh_no


@app.get("/")
def index():
    return FileResponse(config.WEB_DIR / "index.html")


@app.get("/editor")
def editor():
    return FileResponse(config.WEB_DIR / "editor.html")


@app.get("/inbound")
def inbound():
    return FileResponse(config.WEB_DIR / "inbound.html")


# ---------- 仓库与地图 ----------

class NewWarehouse(BaseModel):
    wh_no: str
    name: str = ""
    width: int = 1600
    height: int = 1000
    cell: int = 20


@app.get("/api/warehouses")
def list_warehouses():
    return maps.list_warehouses()


@app.post("/api/warehouses")
def create_warehouse(body: NewWarehouse):
    wh = _wh(body.wh_no)
    if maps.get(wh):
        raise HTTPException(409, f"仓库 {wh} 已存在")
    if not (4 <= body.cell <= 200 and body.width >= body.cell and body.height >= body.cell):
        raise HTTPException(400, "尺寸不合理")
    if (body.width // body.cell) * (body.height // body.cell) > 250_000:
        raise HTTPException(400, "网格太多（超过 25 万格），请加大格子尺寸")
    m = maps.blank_map(wh, body.name, body.width, body.height, body.cell)
    res = maps.save(m)
    return {"ok": res["ok"], "map": m}


@app.delete("/api/warehouses/{wh_no}")
def delete_warehouse(wh_no: str):
    res = maps.delete(_wh(wh_no))
    return JSONResponse(res, status_code=200 if res["ok"] else 409)


@app.get("/api/maps/{wh_no}")
def get_map(wh_no: str):
    m = maps.get(_wh(wh_no))
    if not m:
        raise HTTPException(404, "没有这个仓库")
    return m


@app.put("/api/maps/{wh_no}")
def save_map(wh_no: str, m: dict):
    m["wh_no"] = _wh(wh_no)
    res = maps.save(m)
    return JSONResponse(res, status_code=200 if res["ok"] else 422)


@app.post("/api/maps/{wh_no}/validate")
def validate_map(wh_no: str, m: dict):
    m["wh_no"] = _wh(wh_no)
    a = maps.validate(m)
    blocks = {bid: {"zone": b["zone"], "cells": len(b["cells"])} for bid, b in a["blocks"].items()}
    return {"errors": a["errors"], "warnings": a["warnings"], "blocks": blocks}


@app.post("/api/maps/{wh_no}/bg")
async def upload_bg(wh_no: str, file: UploadFile = File(...)):
    wh = _wh(wh_no)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        raise HTTPException(400, "只支持 png / jpg / webp / bmp 图片")
    for old in config.MAP_ASSET_DIR.glob(f"bg_{wh}.*"):
        old.unlink()
    name = f"bg_{wh}{ext}"
    (config.MAP_ASSET_DIR / name).write_bytes(await file.read())
    return {"file": name}


@app.get("/api/maps/{wh_no}/bg")
def get_bg(wh_no: str):
    wh = _wh(wh_no)
    files = list(config.MAP_ASSET_DIR.glob(f"bg_{wh}.*"))
    if not files:
        raise HTTPException(404)
    return FileResponse(files[0])


@app.get("/api/maps/{wh_no}/render.png")
def render_map(wh_no: str, hl: str = "", zones: int = 1, grid: int = 0):
    m = maps.get(_wh(wh_no))
    if not m:
        raise HTTPException(404, "没有这个仓库")
    highlights = {}
    for i, part in enumerate([p for p in hl.split(",") if p.strip()], 1):
        z, _, s = part.strip().partition("-")
        highlights[(z.upper(), s)] = i
    png = render.render(m, highlights, show_zones=bool(zones), show_grid=bool(grid))
    return Response(png, media_type="image/png")


@app.get("/api/shelf/{wh_no}/{zone}/{shelf_no}")
def shelf_contents(wh_no: str, zone: str, shelf_no: str):
    rows = stock.shelf_contents(_wh(wh_no), zone.upper(), shelf_no)
    names = skus.names({r["sku"] for r in rows})
    for r in rows:
        r["name"] = names.get(r["sku"], "")
    return rows


@app.get("/api/shelves/{wh_no}")
def list_shelves(wh_no: str):
    """某仓库所有货架（地图保存后才有）及其当前登记条数，入库页用来点选货架。"""
    return db.query(
        "SELECT s.zone, s.shelf_no, s.layer_min, s.layer_max, s.regions, s.block_id, "
        "(SELECT COUNT(*) FROM current_stock c WHERE c.wh_no=s.wh_no AND c.zone=s.zone AND c.shelf_no=s.shelf_no) AS n "
        "FROM shelf s WHERE s.wh_no=? ORDER BY s.zone, s.shelf_no", (_wh(wh_no),))


class StockIn(BaseModel):
    sku: str
    wh: str
    zone: str
    shelf: str
    layer: int
    region: int
    qty: int | None = None
    action: str = stock.REGISTER
    note: str = ""
    who: str = ""


@app.post("/api/stock")
def post_stock(b: StockIn):
    """手动入库 / 移除：选好货架、层、区域后登记一条。登记时间由服务器在提交时记录。"""
    loc = codes.from_parts(b.wh, b.zone, b.shelf, b.layer, b.region)
    if not loc:
        raise HTTPException(400, "库位不对：层 0–99，区域从 1 开始")
    e = stock.Entry(sku=b.sku, loc_text=loc.code, qty=b.qty, action=b.action, note=b.note)
    at = db.now()
    r = stock.apply([e], stock.METHOD_MANUAL, "web", b.who.strip() or "未填", created_at=at)[0]
    if not r.ok:
        raise HTTPException(400, r.msg)
    return {"code": r.code, "sku": r.sku, "warnings": r.warnings, "at": at}


# ---------- 查询 / 日志 / 字典 ----------

@app.get("/api/status")
def get_status():
    return {"status": status.get(), "stats": stock.stats(), "strict_sku": config.STRICT_SKU}


def _result_payload(res: dict) -> dict:
    return {
        "lines": [{"seq": ln["seq"], "sku": ln["item"]["sku"], "name": ln["item"]["name"],
                   "need": ln["item"]["qty"], "loc": ln["row"]["loc_code"], "qty": ln["row"]["qty"],
                   "qty_at": ln["row"]["qty_at"], "qty_by": ln["row"]["qty_by"], "flags": ln["item"]["flags"]}
                  for ln in res["lines"]],
        "unregistered": [{"sku": it["sku"], "name": it["name"]} for it in res["unregistered"]],
        "unmatched": res["unmatched"],
        "hl": {wh: ",".join(f"{z}-{s}" for (z, s), _ in sorted(v.items(), key=lambda x: x[1]))
               for wh, v in res["warehouses"].items()},
    }


@app.get("/api/search")
def search(q: str):
    return _result_payload(query.run(parse_text.parse("查询 " + q).demands))


@app.post("/api/pick")
def pick(file: UploadFile = File(...)):
    """上传拣货单（图片 / PDF / Excel / CSV）→ 识别 → 库位表 + 地图红点定位。"""
    name = (file.filename or "").lower()
    data = file.file.read()
    try:
        if name.endswith((".xlsx", ".xls", ".csv")):
            rows = skus.rows_from_table(skus.read_table(name, data), want=("sku", "name", "qty"))
            if not rows:
                raise HTTPException(400, "没找到 SKU 列（表头需包含 SKU / 商家编码 / 货号 之一）")
            demands = [{"sku": r["sku"], "qty": int(q) if (q := (r.get("qty") or "").split(".")[0]).isdigit() else None,
                        "name": r.get("name", ""), "alt": []} for r in rows]
            source = "表格"
        else:
            if not config.vision_enabled():
                raise HTTPException(400, "还没有配置图片识别（.env 里填 DEEPSEEK_API_KEY 或 ANTHROPIC_API_KEY）")
            out = vision.extract_pdf(data) if name.endswith(".pdf") else vision.extract([data])
            if not out["rows"]:
                raise HTTPException(400, "这个文件里没有识别到拣货商品，请换一张清晰、完整的拣货单")
            demands = [{"sku": r["sku"], "qty": r["qty"], "name": r["name"], "alt": r["alt"]} for r in out["rows"]]
            source = "PDF 识别" if name.endswith(".pdf") else "图片识别"
    except vision.VisionError as e:
        raise HTTPException(400, str(e))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 - 文件格式五花八门，统一提示
        raise HTTPException(400, f"读取文件失败：{e}")
    out = _result_payload(query.run(demands))
    out["source"], out["rows"] = source, len(demands)
    return out


@app.get("/api/logs")
def logs(limit: int = 100, sku: str = ""):
    return stock.recent_logs(min(limit, 1000), sku or None)


@app.get("/api/skus")
def list_skus(q: str = "", limit: int = 50):
    like = f"%{q.strip()}%"
    return db.query("SELECT * FROM sku WHERE sku LIKE ? OR name LIKE ? OR aliases LIKE ? ORDER BY sku LIMIT ?",
                    (like, like, like, min(limit, 500)))


@app.post("/api/skus/import")
async def import_skus(file: UploadFile = File(...)):
    data = await file.read()
    try:
        table = skus.read_table(file.filename or "", data)
    except Exception as e:  # noqa: BLE001 - 文件格式五花八门，统一提示
        raise HTTPException(400, f"读取文件失败：{e}")
    header_row, cols = skus.detect_columns(table, want=("sku", "name", "aliases"))
    if header_row < 0:
        raise HTTPException(400, "没找到 SKU 列（表头需包含 SKU / 编码 / 货号 之一）")
    rows = skus.rows_from_table(table, want=("sku", "name", "aliases"))
    n = skus.upsert(rows)
    headers = table[header_row]
    return {"imported": n, "columns": {k: headers[v] for k, v in cols.items()}, "total": skus.count()}
