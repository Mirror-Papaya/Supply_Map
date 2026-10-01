"""5 段库位码：仓库号(2位)-仓库区块(大写字母)-货架编码(2位)-层(2位，从下往上数)-区域/总数

每个货架的每一层被分成 N 个大区域（建货架时选定，同一货架各层的 N 相同）。区域从左往右数。
标准写法 01-A-01-03-3/5：01 号仓 A 区 01 号货架，从下往上第 3 层，这一层 5 个区域里的第 3 个。

输入时容忍：小写字母、全角字符、其他分隔符(— _ . 空格)、层只写一位(3-3/5)。
- 写了 "/总数" 时，仓库号可以省略：A-01-03-3/5（只有一个仓库时自动补上；多个仓库必须写）。
- 不写 "/总数" 时（如表单里分字段填），仓库号必须写，总数按货架的设置补全。
这样 B40006 这样的 SKU 不会被误认成库位码。
层允许 0；区域从 1 开始。
"""
import re
import unicodedata
from typing import NamedTuple

_SEP = r"[\s\-_.—–~·]*"          # 不含 "/"，"/" 留给 区域/总数
_SEP1 = r"[\s\-_.—–~·]+"
# 层 + 区域：层写两位时可以紧挨着区域(033)，层只写一位时必须有分隔符(3-3)
_LR = rf"(?:(\d{{2}}){_SEP}|(\d){_SEP1})(\d{{1,2}})"
WITH_TOTAL_RE = re.compile(rf"^(?:(\d{{2}}){_SEP})?([A-Za-z]){_SEP}(\d{{2}}){_SEP}{_LR}\s*/\s*(\d{{1,2}})$")
NO_TOTAL_RE = re.compile(rf"^(\d{{2}}){_SEP}([A-Za-z]){_SEP}(\d{{2}}){_SEP}{_LR}$")


class Loc(NamedTuple):
    wh: str                 # 仓库号 "01"；输入时省略则为 ""
    zone: str               # 仓库区块 "A"
    shelf: str              # 货架编码 "01"
    layer: int              # 层，从下往上数，0–99
    region: int             # 区域，从左往右数，1–99
    total: int | None = None  # 这一层一共分几个区域；输入时没写则为 None，由货架设置补全

    @property
    def code(self) -> str:
        t = f"/{self.total}" if self.total else ""
        return f"{self.wh}-{self.zone}-{self.shelf}-{self.layer:02d}-{self.region}{t}"

    @property
    def shelf_key(self) -> str:
        return f"{self.wh}-{self.zone}-{self.shelf}"

    @property
    def sort_key(self):
        return (self.wh, self.zone, self.shelf, self.layer, self.region)


def _fold(text: str) -> str:
    # NFKC：全角数字/字母/连接符 → 半角
    return unicodedata.normalize("NFKC", text or "").strip()


def parse(text: str) -> Loc | None:
    """解析库位码，失败返回 None。"""
    t = _fold(text)
    m = WITH_TOTAL_RE.match(t)
    if m:
        wh, zone, shelf, l2, l1, region, total = m.groups()
    else:
        m = NO_TOTAL_RE.match(t)
        if not m:
            return None
        wh, zone, shelf, l2, l1, region = m.groups()
        total = None
    layer = l2 if l2 is not None else l1
    region_i, total_i = int(region), int(total) if total is not None else None
    if region_i < 1 or (total_i is not None and not (1 <= total_i and region_i <= total_i)):
        return None
    return Loc(wh or "", zone.upper(), shelf, int(layer), region_i, total_i)


def normalize(text: str) -> str | None:
    loc = parse(text)
    return loc.code if loc else None


def from_parts(wh, zone, shelf, layer, region, total=None) -> Loc | None:
    """表单分段字段 → Loc。数字字段可能是 3 / "3" / 3.0。仓库号必填。"""
    try:
        wh_s = f"{int(float(str(wh).strip())):02d}"
        shelf_s = f"{int(float(str(shelf).strip())):02d}"
        layer_i = int(float(str(layer).strip()))
        region_i = int(float(str(region).strip()))
        total_i = int(float(str(total).strip())) if total not in (None, "") else None
    except (TypeError, ValueError):
        return None
    zone_s = _fold(str(zone or "")).upper()
    return parse(f"{wh_s}-{zone_s}-{shelf_s}-{layer_i}-{region_i}" + (f"/{total_i}" if total_i else ""))


def parse_shelf_key(text: str):
    """"01-A-03" → ("01", "A", "03")"""
    m = re.match(rf"^(\d{{2}}){_SEP}([A-Za-z]){_SEP}(\d{{2}})$", _fold(text))
    if not m:
        return None
    return m.group(1), m.group(2).upper(), m.group(3)
