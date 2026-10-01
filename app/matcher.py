"""把识别/输入得到的 SKU 文本匹配到已知 SKU（商品字典 + 已登记过的 SKU）。

匹配顺序：精确 → 去符号 → 易混字符归一(O/0、I/1…) → 相似度 → 品名相似度。
"""
import difflib
import re

from . import db, skus

CONFUSABLE = str.maketrans({"O": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8"})


def _plain(s: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", (s or "").upper())


def _loose(s: str) -> str:
    return _plain(s).translate(CONFUSABLE)


class Index:
    def __init__(self):
        self.canon = {}      # UPPER → sku
        self.plain = {}      # plain → {sku}
        self.loose = {}      # loose → {sku}
        self.names = {}      # sku → name
        rows = db.query("SELECT sku, name, aliases FROM sku")
        rows += [{"sku": r["sku"], "name": "", "aliases": ""}
                 for r in db.query("SELECT DISTINCT sku FROM current_stock")]
        for r in rows:
            s = r["sku"]
            if r["name"]:
                self.names[s] = r["name"]
            self.names.setdefault(s, "")
            for form in [s] + skus.split_aliases(r["aliases"]):
                self.canon.setdefault(skus.key(form), s)
                self.plain.setdefault(_plain(form), set()).add(s)
                self.loose.setdefault(_loose(form), set()).add(s)
        self._plain_keys = list(self.plain)

    def __len__(self):
        return len(self.names)

    def match(self, text: str, name: str = "") -> dict:
        """返回 {"sku": 匹配到的 SKU 或 None, "method": 精确/纠错/近似/品名/未匹配, "candidates": [...]}"""
        t = (text or "").strip()
        if t:
            if skus.key(t) in self.canon:
                return {"sku": self.canon[skus.key(t)], "method": "精确", "candidates": []}
            p = _plain(t)
            if p and len(self.plain.get(p, ())) == 1:
                return {"sku": next(iter(self.plain[p])), "method": "精确", "candidates": []}
            lo = _loose(t)
            hit = self.loose.get(lo, set())
            if len(hit) == 1:
                return {"sku": next(iter(hit)), "method": "纠错", "candidates": []}
            if len(hit) > 1:
                return {"sku": None, "method": "未匹配", "candidates": sorted(hit)[:5]}
            if len(p) >= 5:
                close = difflib.get_close_matches(p, self._plain_keys, n=3, cutoff=0.86)
                cands = sorted({s for k in close for s in self.plain[k]})
                if len(close) == 1 and len(cands) == 1:
                    return {"sku": cands[0], "method": "近似", "candidates": []}
                if cands:
                    return {"sku": None, "method": "未匹配", "candidates": cands[:5]}
        if name and len(name) >= 4:
            best, best_s = None, 0.0
            for s, n in self.names.items():
                if not n:
                    continue
                score = difflib.SequenceMatcher(None, name, n).ratio()
                if score > best_s:
                    best, best_s = s, score
            if best and best_s >= 0.85:
                return {"sku": best, "method": "品名", "candidates": []}
        return {"sku": None, "method": "未匹配", "candidates": []}
