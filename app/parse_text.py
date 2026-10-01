"""聊天文本解析。

登记（可多行，首行的指令对后续行持续生效）：
    登记 SKU-000123 01-A-03-2-0 40
    SKU-000456 01A0321            ← 余量可省略
    移除 SKU-000789 01-B-01-1-0
查询：
    查询 SKU-000123 SKU-000456 3   ← SKU 后面紧跟的整数视为需求数量
"""
import re
import unicodedata
from dataclasses import dataclass, field

from . import codes
from .stock import REGISTER, REMOVE, Entry

CMD_REGISTER = {"登记", "录入", "入库", "上架", "打点", "reg"}
CMD_REMOVE = {"移除", "下架", "清空", "删除", "remove"}
CMD_QUERY = {"查询", "查", "找", "拣货", "query", "q"}
CMD_HELP = {"帮助", "help", "?", "？", "菜单", "说明"}

QTY_RE = re.compile(r"^(\d{1,7})(个|件|只|把|pcs|pc|套|盒|箱)?$", re.I)


@dataclass
class Parsed:
    kind: str                      # register / query / help / unknown
    entries: list = field(default_factory=list)   # register: [Entry]
    errors: list = field(default_factory=list)    # register: [(label, 行原文, 原因)]
    demands: list = field(default_factory=list)   # query: [{"sku":..., "qty":...}]


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"@_user_\d+", " ", text)          # 去掉飞书 @ 占位符
    return text


def tokens(line: str) -> list:
    return [t for t in re.split(r"[\s,，;；、:：]+", line.strip()) if t]


def _cmd_of(tok: str):
    t = tok.lower()
    if t in CMD_REGISTER:
        return "register", REGISTER
    if t in CMD_REMOVE:
        return "register", REMOVE
    if t in CMD_QUERY:
        return "query", None
    if t in CMD_HELP:
        return "help", None
    return None, None


def _qty(tok: str):
    m = QTY_RE.match(tok)
    return int(m.group(1)) if m else None


def parse(text: str) -> Parsed:
    lines = [ln for ln in _fold(text).splitlines() if ln.strip()]
    if not lines:
        return Parsed("unknown")
    first = tokens(lines[0])
    kind, action = _cmd_of(first[0]) if first else (None, None)
    if kind == "help":
        return Parsed("help")
    if kind == "register":
        return _parse_register(lines, action)
    if kind == "query":
        return _parse_query(lines, strip_cmd=True)
    return Parsed("unknown", demands=_parse_query(lines, strip_cmd=False).demands)


def _parse_register(lines, action) -> Parsed:
    p = Parsed("register")
    for i, line in enumerate(lines, 1):
        toks = tokens(line)
        k, a = _cmd_of(toks[0]) if toks else (None, None)
        if k == "register":
            action = a
            toks = toks[1:]
        if not toks:
            continue
        label = f"第{i}行" if len(lines) > 1 else ""
        idx = next((j for j, t in enumerate(toks) if codes.parse(t)), None)
        if idx is None:
            p.errors.append((label, line.strip(), "没找到库位码（格式如 01-A-03-2-0）"))
            continue
        loc_tok = toks[idx]
        before, after = toks[:idx], toks[idx + 1:]
        if not before and after:
            before, after = after[:1], after[1:]
        if len(before) != 1:
            p.errors.append((label, line.strip(), "每行应为：SKU 库位码 [余量]"))
            continue
        qty = None
        if after:
            qty = _qty(after[0])
            if qty is None or len(after) > 1:
                p.errors.append((label, line.strip(), f"余量应为整数：{' '.join(after)}"))
                continue
            if action == REMOVE:
                qty = None
        p.entries.append(Entry(sku=before[0], loc_text=loc_tok, qty=qty, action=action, label=label))
    return p


def _parse_query(lines, strip_cmd) -> Parsed:
    p = Parsed("query")
    for n, line in enumerate(lines):
        toks = tokens(line)
        if strip_cmd and n == 0 and toks:
            toks = toks[1:]
        for t in toks:
            q = _qty(t)
            if q is not None and p.demands and p.demands[-1]["qty"] is None and not t.isalpha():
                p.demands[-1]["qty"] = q
            elif codes.parse(t):
                continue
            else:
                p.demands.append({"sku": t, "qty": None})
    return p


HELP_TEXT = """📦 库存地图机器人 使用说明

【查询库位】
· 直接发拣货单图片（单聊，或拣货群里直接发）
· 发 Excel / CSV 拣货单文件
· 文字：查询 SKU1 SKU2 …（SKU 后可跟需求数量）

【登记库位】（余量选填，填的是盘点数）
登记 SKU 库位码 [余量]
例：登记 SKU-000123 01-A-03-2-0 40
可多行批量：第一行写"登记"，后面每行一条

【移除】货已不在该库位时
移除 SKU 库位码

库位码：仓库号-区块-货架-层-包装，如 01-A-03-2-0
（也可以写成 01A0320）"""
