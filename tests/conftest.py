import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    from app import config, db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "STRICT_SKU", False)
    db.connect(tmp_path / "test.db")
    yield
    if db._conn is not None:
        db._conn.close()
        db._conn = None


def paint_rect(m, r0, c0, r1, c1, t=None, bid=None, zone=None):
    for r in range(r0, r1 + 1):
        if t is not None:
            row = list(m["types"][r])
            for c in range(c0, c1 + 1):
                row[c] = str(t)
            m["types"][r] = "".join(row)
        if bid is not None:
            for c in range(c0, c1 + 1):
                m["bids"][r][c] = bid
        if zone is not None:
            row = list(m["zones"][r])
            for c in range(c0, c1 + 1):
                row[c] = zone
            m["zones"][r] = "".join(row)


@pytest.fixture
def sample_map():
    """仓库 01：区块 A 有货架 01(1-4层) 02(0-3层)，区块 B 有货架 01(1-2层)，一个打包台，一条走廊。"""
    from app import maps
    m = maps.blank_map("01", "一号仓", width=400, height=200, cell=20)   # 20 列 × 10 行
    paint_rect(m, 0, 0, 9, 9, zone="A")
    paint_rect(m, 0, 10, 9, 19, zone="B")
    paint_rect(m, 1, 1, 1, 6, t=2, bid=1)
    paint_rect(m, 3, 1, 3, 6, t=2, bid=2)
    paint_rect(m, 1, 12, 1, 17, t=2, bid=3)
    paint_rect(m, 7, 12, 8, 17, t=1, bid=4)
    paint_rect(m, 5, 0, 5, 19, t=3)
    m["blocks"] = {
        "1": {"type": "storage", "shelf": "01", "layer_min": 1, "layer_max": 4},
        "2": {"type": "storage", "shelf": "02", "layer_min": 0, "layer_max": 3},
        "3": {"type": "storage", "shelf": "01", "layer_min": 1, "layer_max": 2},
        "4": {"type": "function", "label": "打包台"},
    }
    m["next_id"] = 5
    return m


@pytest.fixture
def saved_map(sample_map):
    from app import maps
    res = maps.save(sample_map)
    assert res["ok"], res
    return sample_map
