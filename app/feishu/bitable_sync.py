"""多维表格表单登记 → 本地登记日志。

轮询"处理状态"为空、且 20 秒内没被编辑的记录，校验后写入本地，并回写 处理状态 / 处理说明 / 库位码。
失败时私信提交人。想重试：改好后把"处理状态"清空即可。
"""
import logging
import time
from datetime import datetime

from lark_oapi.api.bitable.v1 import (AppTableCreateHeader, AppTableFieldProperty, AppTableFieldPropertyOption,
                                      AppTableRecord, BatchUpdateAppTableRecordRequest,
                                      BatchUpdateAppTableRecordRequestBody, Condition, CreateAppTableRequest,
                                      CreateAppTableRequestBody, CreateAppTableViewRequest, FilterInfo, ReqTable,
                                      ReqView, SearchAppTableRecordRequest, SearchAppTableRecordRequestBody)

from .. import codes, config, db, status, stock
from . import client as fc

log = logging.getLogger("bitable")

F_SKU, F_WH, F_ZONE, F_SHELF, F_LAYER, F_PACK = "SKU", "仓库号", "区块", "货架", "层", "包装备注"
F_CODE, F_QTY, F_ACTION, F_NOTE = "库位码", "余量", "操作", "备注"
F_USER, F_TIME, F_STATE, F_MSG = "提交人", "提交时间", "处理状态", "处理说明"
QUIET_SECONDS = 20


def _text(v) -> str:
    """多维表格字段值 → 字符串（文本字段可能是富文本片段列表）。"""
    if v is None:
        return ""
    if isinstance(v, list):
        return "".join(_text(x) for x in v)
    if isinstance(v, dict):
        return str(v.get("text") or v.get("name") or v.get("value") or "")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _num(v):
    s = _text(v)
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return "bad"


def record_to_entry(record_id: str, f: dict):
    """返回 (Entry, 解析问题)。库位码字段优先；为空时用 仓库号/区块/货架/层/包装备注 拼。"""
    code_text = _text(f.get(F_CODE))
    parts = [f.get(k) for k in (F_WH, F_ZONE, F_SHELF, F_LAYER, F_PACK)]
    if not code_text and any(_text(p) for p in parts):
        loc = codes.from_parts(*[_text(p) or "" for p in parts])
        code_text = loc.code if loc else "-".join(_text(p) or "?" for p in parts)
    qty = _num(f.get(F_QTY))
    problem = "余量必须是整数" if qty == "bad" else ""
    action = _text(f.get(F_ACTION)) or stock.REGISTER
    e = stock.Entry(sku=_text(f.get(F_SKU)), loc_text=code_text, qty=None if qty == "bad" else qty,
                    action=action, note=_text(f.get(F_NOTE)), source_id=f"bitable:{record_id}")
    return e, problem


def _person(v):
    if isinstance(v, list) and v:
        v = v[0]
    if isinstance(v, dict):
        return v.get("id", ""), v.get("name", "")
    return getattr(v, "id", "") or "", getattr(v, "name", "") or ""


def _pending_records() -> list:
    cond = Condition.builder().field_name(F_STATE).operator("isEmpty").value([]).build()
    items, page = [], None
    while True:
        b = (SearchAppTableRecordRequest.builder().app_token(config.BITABLE_APP_TOKEN)
             .table_id(config.BITABLE_TABLE_ID).page_size(100))
        if page:
            b = b.page_token(page)
        req = b.request_body(SearchAppTableRecordRequestBody.builder()
                             .filter(FilterInfo.builder().conjunction("and").conditions([cond]).build())
                             .automatic_fields(True).build()).build()
        resp = fc._check(fc.client().bitable.v1.app_table_record.search(req), "读取多维表格")
        items += resp.data.items or []
        if not resp.data.has_more:
            return items
        page = resp.data.page_token


def sync_once() -> int:
    now_ms = time.time() * 1000
    updates, notices = [], []
    for rec in _pending_records():
        f = rec.fields or {}
        if not _text(f.get(F_SKU)) and not _text(f.get(F_CODE)) and not _text(f.get(F_SHELF)):
            continue                                     # 空行，等填写
        if rec.last_modified_time and now_ms - rec.last_modified_time < QUIET_SECONDS * 1000:
            continue                                     # 可能还在编辑
        user_id, user_name = _person(f.get(F_USER) or rec.created_by)
        at = datetime.fromtimestamp((rec.created_time or now_ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        entry, problem = record_to_entry(rec.record_id, f)
        if problem:
            ok, msg, code = False, problem, ""
        else:
            res = stock.apply([entry], "表单", user_id, user_name, created_at=at)[0]
            ok = res.ok or res.duplicate
            code = res.code
            if res.duplicate:
                msg = "已处理过"
            elif res.ok:
                msg = f"已{entry.action} {res.sku} → {res.code}" + (f"（{'；'.join(res.warnings)}）" if res.warnings else "")
            else:
                msg = res.msg
        fields = {F_STATE: "成功" if ok else "失败", F_MSG: msg}
        if code:
            fields[F_CODE] = code
        updates.append(AppTableRecord.builder().record_id(rec.record_id).fields(fields).build())
        if not ok and user_id:
            notices.append((user_id, f"❌ 表单登记失败：{entry.sku or '（无SKU）'} {entry.loc_text or ''}\n原因：{msg}\n"
                                     f"请在多维表格里修改后，把“处理状态”清空即可重新处理。"))
    for i in range(0, len(updates), 500):
        req = (BatchUpdateAppTableRecordRequest.builder().app_token(config.BITABLE_APP_TOKEN)
               .table_id(config.BITABLE_TABLE_ID)
               .request_body(BatchUpdateAppTableRecordRequestBody.builder().records(updates[i:i + 500]).build())
               .build())
        fc._check(fc.client().bitable.v1.app_table_record.batch_update(req), "回写多维表格")
    for open_id, text in notices:
        try:
            fc.send(open_id, "text", {"text": text}, id_type="open_id")
        except Exception:  # noqa: BLE001
            log.exception("私信提交人失败")
    return len(updates)


def run_forever():
    while True:
        try:
            n = sync_once()
            if n:
                log.info("多维表格同步 %d 条", n)
            status.set(bitable="正常", bitable_error="", bitable_last_sync=datetime.now().strftime("%H:%M:%S"))
        except Exception as e:  # noqa: BLE001
            log.exception("多维表格同步失败")
            status.set(bitable="出错", bitable_error=str(e)[:200])
        time.sleep(config.BITABLE_POLL_SECONDS)


# ---------------- 初始化：建表 + 表单视图 ----------------

def _opt(names):
    return AppTableFieldProperty.builder().options(
        [AppTableFieldPropertyOption.builder().name(n).build() for n in names]).build()


def create_table(app_token: str, name: str = "库存登记") -> str:
    whs = [r["wh_no"] for r in db.query("SELECT wh_no FROM warehouse ORDER BY wh_no")] or ["01"]
    zones = sorted({r["letter"] for r in db.query("SELECT letter FROM zone")}) or ["A", "B", "C"]
    num = AppTableFieldProperty.builder().formatter("0").build()
    H = lambda n, t, p=None: (AppTableCreateHeader.builder().field_name(n).type(t).property(p).build()
                              if p else AppTableCreateHeader.builder().field_name(n).type(t).build())
    fields = [
        H(F_SKU, 1), H(F_WH, 3, _opt(whs)), H(F_ZONE, 3, _opt(zones)), H(F_SHELF, 2, num), H(F_LAYER, 2, num),
        H(F_PACK, 2, num), H(F_QTY, 2, num), H(F_ACTION, 3, _opt([stock.REGISTER, stock.REMOVE])), H(F_NOTE, 1),
        H(F_CODE, 1), H(F_USER, 1003), H(F_TIME, 1001), H(F_STATE, 3, _opt(["成功", "失败"])), H(F_MSG, 1),
    ]
    req = (CreateAppTableRequest.builder().app_token(app_token)
           .request_body(CreateAppTableRequestBody.builder()
                         .table(ReqTable.builder().name(name).default_view_name("全部登记").fields(fields).build())
                         .build()).build())
    table_id = fc._check(fc.client().bitable.v1.app_table.create(req), "创建数据表").data.table_id
    try:
        vreq = (CreateAppTableViewRequest.builder().app_token(app_token).table_id(table_id)
                .request_body(ReqView.builder().view_name("登记表单").view_type("form").build()).build())
        fc._check(fc.client().bitable.v1.app_table_view.create(vreq), "创建表单视图")
    except Exception as e:  # noqa: BLE001
        log.warning("自动创建表单视图失败（可在多维表格里手动添加表单视图）：%s", e)
    return table_id
