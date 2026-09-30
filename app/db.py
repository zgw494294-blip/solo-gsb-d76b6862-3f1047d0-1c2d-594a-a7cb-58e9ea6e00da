"""SQLite 连接、建表与写事务工具。

并发模型：
* 数据库开启 WAL；写路径统一使用 ``BEGIN IMMEDIATE``，进入事务即持有写锁，
  同一时刻只会有一个写事务，后到的写事务串行等待（busy_timeout）。
* 每个回执请求在同一个写事务内完成"写回执 → 判定批次 → 切版本 → 标记批次"，
  因此并发提交的结果必然等价于某一个串行顺序。
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

# 默认数据文件放在当前工作目录；容器内由 compose 挂载到 /data。
DEFAULT_DB_PATH = os.path.join(os.getcwd(), "firmware_release.db")

# 等待写锁的最长时间（秒）。SQLite 自身会在该窗口内重试。
BUSY_TIMEOUT_SECONDS = 15.0
# busy_timeout 到期后，应用层再重试的次数（极端竞争下兜底）。
WRITE_RETRY_LIMIT = 60


def db_path() -> str:
    return os.environ.get("FIRMWARE_DB_PATH", DEFAULT_DB_PATH)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path(),
        timeout=BUSY_TIMEOUT_SECONDS,
        isolation_level=None,  # autocommit：所有事务边界由我们显式控制
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=%d" % int(BUSY_TIMEOUT_SECONDS * 1000))
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS device_groups (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    group_id        TEXT NOT NULL REFERENCES device_groups(id) ON DELETE CASCADE,
    device_id       TEXT NOT NULL,
    current_version TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (group_id, device_id)
);

CREATE TABLE IF NOT EXISTS batches (
    id          TEXT PRIMARY KEY,
    group_id    TEXT NOT NULL REFERENCES device_groups(id) ON DELETE CASCADE,
    status      TEXT NOT NULL CHECK (status IN ('open', 'failed', 'effective')),
    created_at  TEXT NOT NULL,
    closed_at   TEXT
);

-- open 状态全局唯一：同一设备组只要还有未结束批次，就无法再开新批。
CREATE UNIQUE INDEX IF NOT EXISTS ux_open_batch_per_group
    ON batches(group_id)
    WHERE status = 'open';

CREATE TABLE IF NOT EXISTS batch_targets (
    batch_id       TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    device_id      TEXT NOT NULL,
    target_version TEXT NOT NULL,
    PRIMARY KEY (batch_id, device_id)
);

CREATE TABLE IF NOT EXISTS receipts (
    batch_id   TEXT NOT NULL,
    device_id  TEXT NOT NULL,
    result     TEXT NOT NULL CHECK (result IN ('ready', 'failed')),
    version    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch_id, device_id),
    FOREIGN KEY (batch_id, device_id)
        REFERENCES batch_targets(batch_id, device_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_batches_group ON batches(group_id);
"""


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """运行一个 ``BEGIN IMMEDIATE`` 写事务，自动提交/回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def write_transaction() -> Iterator[sqlite3.Connection]:
    """获取连接并执行写事务；在 SQLITE_BUSY 时整体重试。

    重试必须回到整个事务的开头（重新拿写锁），因此连接也一并重建。
    """
    attempt = 0
    while True:
        attempt += 1
        conn = connect()
        try:
            with immediate(conn):
                yield conn
            return
        except sqlite3.OperationalError as exc:
            conn.close()
            if attempt > WRITE_RETRY_LIMIT or "locked" not in str(exc).lower():
                raise
            # busy_timeout 已经覆盖了绝大多数等待；这里只做短暂退避。
            time.sleep(0.02 * min(attempt, 10))
        except BaseException:
            conn.close()
            raise


@contextmanager
def read_transaction() -> Iterator[sqlite3.Connection]:
    """只读快照事务：同一事务内的多条查询看到一致状态，绝不会读到部分切换。"""
    conn = connect()
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
