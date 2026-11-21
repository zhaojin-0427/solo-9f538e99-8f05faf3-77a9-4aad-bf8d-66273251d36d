"""SQLite 连接与建表脚本。

设计要点：
- ``partition_rev`` / ``feature_window`` / ``scoring_slice`` 是不可变的版本化事实记录，
  同一条同版记录可被多张确认单通过关联表共用，不随后续更正或换版而改变。
- ``preview`` 即确认单：创建时为 open，确认成功后写入 confirmation_no 变为 confirmed。
- 三类留存引用（源分区修订、窗口、切片）全部以规范化关联表保存，
  打开旧单时仅凭留存引用即可复算原清单，不依赖当前最新版本。
"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 风机某 UTC 日期的分区修订，修订号在同一 (风机, 日期) 内严格递增
CREATE TABLE IF NOT EXISTS partition_rev (
    id          INTEGER PRIMARY KEY,
    turbine_id  TEXT    NOT NULL,
    utc_date    TEXT    NOT NULL,
    revision    INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE (turbine_id, utc_date, revision)
);

-- 三日特征窗口的某个版本（窗口覆盖 end_date 及前两天）
CREATE TABLE IF NOT EXISTS feature_window (
    id           INTEGER PRIMARY KEY,
    turbine_id   TEXT    NOT NULL,
    feature_name TEXT    NOT NULL,
    end_date     TEXT    NOT NULL,
    version      INTEGER NOT NULL,
    created_at   TEXT    NOT NULL,
    UNIQUE (turbine_id, feature_name, end_date, version)
);

-- 告警评分切片的某个版本
CREATE TABLE IF NOT EXISTS scoring_slice (
    id          INTEGER PRIMARY KEY,
    turbine_id  TEXT    NOT NULL,
    slice_name  TEXT    NOT NULL,
    version     INTEGER NOT NULL,
    created_at  TEXT    NOT NULL,
    UNIQUE (turbine_id, slice_name, version)
);

-- 切片版本引用的窗口版本（不可变的多对多关系）
CREATE TABLE IF NOT EXISTS slice_window_ref (
    slice_id  INTEGER NOT NULL REFERENCES scoring_slice(id),
    window_id INTEGER NOT NULL REFERENCES feature_window(id),
    PRIMARY KEY (slice_id, window_id)
);
CREATE INDEX IF NOT EXISTS idx_swref_window ON slice_window_ref(window_id);
CREATE INDEX IF NOT EXISTS idx_swref_slice  ON slice_window_ref(slice_id);

-- 预览 / 确认单主表
CREATE TABLE IF NOT EXISTS preview (
    id                INTEGER PRIMARY KEY,
    status            TEXT    NOT NULL DEFAULT 'open',  -- open | confirmed
    corrections_json  TEXT    NOT NULL,                 -- 发起更正的 (风机, UTC 日期) 清单
    snapshot_json     TEXT    NOT NULL,                 -- 所见版本快照（确认时比对用）
    confirmation_no   TEXT    UNIQUE,
    created_at        TEXT    NOT NULL,
    confirmed_at      TEXT
);

-- 确认单留存的源分区修订引用（同版分区可被多张单共用）
CREATE TABLE IF NOT EXISTS order_partition_ref (
    preview_id   INTEGER NOT NULL REFERENCES preview(id),
    partition_id INTEGER NOT NULL REFERENCES partition_rev(id),
    PRIMARY KEY (preview_id, partition_id)
);

-- 确认单留存的窗口引用
CREATE TABLE IF NOT EXISTS order_window_ref (
    preview_id INTEGER NOT NULL REFERENCES preview(id),
    window_id  INTEGER NOT NULL REFERENCES feature_window(id),
    PRIMARY KEY (preview_id, window_id)
);

-- 每个留存窗口对应的三日源分区修订（按窗口维度留存，复算时逐窗还原）
CREATE TABLE IF NOT EXISTS order_window_partition (
    preview_id   INTEGER NOT NULL REFERENCES preview(id),
    window_id    INTEGER NOT NULL REFERENCES feature_window(id),
    partition_id INTEGER NOT NULL REFERENCES partition_rev(id),
    PRIMARY KEY (preview_id, window_id, partition_id)
);

-- 确认单留存的切片引用
CREATE TABLE IF NOT EXISTS order_slice_ref (
    preview_id INTEGER NOT NULL REFERENCES preview(id),
    slice_id   INTEGER NOT NULL REFERENCES scoring_slice(id),
    PRIMARY KEY (preview_id, slice_id)
);

-- 确认号发号器（仅确认成功时消耗一个序号）
CREATE TABLE IF NOT EXISTS order_seq (
    seq INTEGER PRIMARY KEY AUTOINCREMENT
);
"""


def db_path() -> str:
    return os.environ.get("APP_DB_PATH", "/data/vibration.db")


def get_conn() -> sqlite3.Connection:
    """每个请求一个连接，开启外键与 WAL。"""
    path = db_path()
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
