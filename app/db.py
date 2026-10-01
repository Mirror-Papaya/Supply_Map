"""SQLite 数据层。登记日志 stock_log 只追加，current_stock 是由日志推导出的当前库位缓存。"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS warehouse (
    wh_no      TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    map_json   TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS zone (
    wh_no  TEXT NOT NULL,
    letter TEXT NOT NULL,
    color  TEXT,
    PRIMARY KEY (wh_no, letter)
);
CREATE TABLE IF NOT EXISTS shelf (
    wh_no     TEXT NOT NULL,
    zone      TEXT NOT NULL,
    shelf_no  TEXT NOT NULL,
    layer_min INTEGER NOT NULL,
    layer_max INTEGER NOT NULL,
    block_id  INTEGER,
    PRIMARY KEY (wh_no, zone, shelf_no)
);
CREATE TABLE IF NOT EXISTS pack_code (
    code    TEXT PRIMARY KEY,
    meaning TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sku (
    sku        TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    aliases    TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS stock_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sku        TEXT NOT NULL,
    loc_code   TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('登记', '移除')),
    qty        INTEGER,
    created_at TEXT NOT NULL,
    method     TEXT NOT NULL,
    user_id    TEXT NOT NULL DEFAULT '',
    user_name  TEXT NOT NULL DEFAULT '',
    source_id  TEXT,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_stock_log_source ON stock_log (source_id) WHERE source_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_stock_log_sku ON stock_log (sku);
CREATE TABLE IF NOT EXISTS current_stock (
    sku         TEXT NOT NULL,
    loc_code    TEXT NOT NULL,
    wh_no       TEXT NOT NULL,
    zone        TEXT NOT NULL,
    shelf_no    TEXT NOT NULL,
    layer       INTEGER NOT NULL,
    pack        TEXT NOT NULL,
    qty         INTEGER,
    qty_at      TEXT,
    qty_by      TEXT,
    last_at     TEXT NOT NULL,
    last_by     TEXT NOT NULL,
    last_method TEXT NOT NULL,
    PRIMARY KEY (sku, loc_code)
);
CREATE INDEX IF NOT EXISTS ix_current_shelf ON current_stock (wh_no, zone, shelf_no);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS processed_msg (
    message_id TEXT PRIMARY KEY,
    at         TEXT NOT NULL
);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None
_conn_path = None


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect(path=None) -> sqlite3.Connection:
    """全局单连接 + 锁：本地单机工具，写入量很小，串行化最简单可靠。"""
    global _conn, _conn_path
    path = str(path or config.DB_PATH)
    with _lock:
        if _conn is None or _conn_path != path:
            if _conn is not None:
                _conn.close()
            _conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _conn_path = path
        return _conn


@contextmanager
def tx():
    """事务：with tx() as c: ...  出错自动回滚。"""
    with _lock:
        c = connect()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def query(sql, params=()):
    with _lock:
        return [dict(r) for r in connect().execute(sql, params).fetchall()]


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def kv_get(key, default=None):
    row = query_one("SELECT value FROM kv WHERE key=?", (key,))
    return row["value"] if row else default


def kv_set(key, value):
    with tx() as c:
        c.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def mark_message_processed(message_id: str) -> bool:
    """飞书可能重复推送同一事件；返回 False 表示已经处理过。"""
    with tx() as c:
        cur = c.execute("INSERT OR IGNORE INTO processed_msg(message_id, at) VALUES(?, ?)", (message_id, now()))
        return cur.rowcount == 1


def backup(dest_path) -> None:
    with _lock:
        dst = sqlite3.connect(str(dest_path))
        try:
            connect().backup(dst)
        finally:
            dst.close()
