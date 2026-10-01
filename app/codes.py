"""5 段库位码：仓库号(2位数字)-仓库区块(大写字母)-货架编码(2位数字)-层编码(1位数字)-包装备注编码(1位数字)

标准写法 01-A-03-2-1。输入时容忍：无连接符(01A0321)、小写字母、全角字符、其他分隔符(— _ / . 空格)。
"""
import re
import unicodedata
from typing import NamedTuple

CANONICAL_RE = re.compile(r"^(\d{2})-([A-Z])-(\d{2})-(\d)-(\d)$")
_SEP = r"[\s\-_/.—–~·]*"
LOOSE_RE = re.compile(rf"^(\d{{2}}){_SEP}([A-Za-z]){_SEP}(\d{{2}}){_SEP}(\d){_SEP}(\d)$")


class Loc(NamedTuple):
    wh: str      # 仓库号 "01"
    zone: str    # 仓库区块 "A"
    shelf: str   # 货架编码 "03"
    layer: int   # 层编码 0-9
    pack: str    # 包装备注编码 "0"-"9"

    @property
    def code(self) -> str:
        return f"{self.wh}-{self.zone}-{self.shelf}-{self.layer}-{self.pack}"

    @property
    def shelf_key(self) -> str:
        return f"{self.wh}-{self.zone}-{self.shelf}"

    @property
    def sort_key(self):
        return (self.wh, self.zone, self.shelf, self.layer, self.pack)


def _fold(text: str) -> str:
    # NFKC：全角数字/字母/连接符 → 半角
    return unicodedata.normalize("NFKC", text or "").strip()


def parse(text: str) -> Loc | None:
    """解析库位码，失败返回 None。"""
    m = LOOSE_RE.match(_fold(text))
    if not m:
        return None
    wh, zone, shelf, layer, pack = m.groups()
    return Loc(wh, zone.upper(), shelf, int(layer), pack)


def normalize(text: str) -> str | None:
    loc = parse(text)
    return loc.code if loc else None


def from_parts(wh, zone, shelf, layer, pack) -> Loc | None:
    """表单分段字段 → Loc。数字字段可能是 3 / "3" / 3.0。"""
    try:
        wh_s = f"{int(float(str(wh).strip())):02d}"
        shelf_s = f"{int(float(str(shelf).strip())):02d}"
        layer_i = int(float(str(layer).strip()))
        pack_s = str(int(float(str(pack).strip())))
    except (TypeError, ValueError):
        return None
    zone_s = _fold(str(zone or "")).upper()
    return parse(f"{wh_s}-{zone_s}-{shelf_s}-{layer_i}-{pack_s}")


def parse_shelf_key(text: str):
    """"01-A-03" → ("01", "A", "03")"""
    m = re.match(rf"^(\d{{2}}){_SEP}([A-Za-z]){_SEP}(\d{{2}})$", _fold(text))
    if not m:
        return None
    return m.group(1), m.group(2).upper(), m.group(3)
