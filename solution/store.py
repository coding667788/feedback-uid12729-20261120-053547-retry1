"""SQLite 持久层：连接、DDL、崩溃注入。

设计要点：

* 每个请求一个短连接，``BEGIN IMMEDIATE`` 立即取得写锁，
  确认/续发/撤回/登录的并发由 SQLite 写锁串行化，配合每租户非递减时钟；
* WAL 日志 + ``busy_timeout``，Windows 原生环境同样支持；
* 崩溃注入仅由请求中的 ``crash`` 字段触发，两个注入点分别位于
  分量更新“未提交”与“已提交未回执”。
"""

from __future__ import annotations

import os
import sqlite3

from .errors import CrashAfterCommit, CrashBeforeCommit

DDL = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   TEXT PRIMARY KEY,
    clock       INTEGER NOT NULL,
    gen_seq     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS identities (
    tenant_id   TEXT NOT NULL,
    identity_id TEXT NOT NULL,
    provider    TEXT NOT NULL,
    id_gen      INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, identity_id)
);

CREATE TABLE IF NOT EXISTS edges (
    tenant_id   TEXT NOT NULL,
    edge_id     TEXT NOT NULL,
    id_a        TEXT NOT NULL,
    id_b        TEXT NOT NULL,
    state       TEXT NOT NULL,              -- active | revoked | stale
    gen_a       INTEGER NOT NULL,           -- 建边/续期时 A 身份代数
    gen_b       INTEGER NOT NULL,
    not_before  INTEGER NOT NULL,
    not_after   INTEGER NOT NULL,
    request_id  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, edge_id)
);

CREATE TABLE IF NOT EXISTS subjects (
    tenant_id   TEXT NOT NULL,
    subject_no  TEXT NOT NULL,              -- 分量最小身份 ID
    subject_gen INTEGER NOT NULL,
    members     TEXT NOT NULL,              -- JSON 数组，排序后存储
    PRIMARY KEY (tenant_id, subject_no)
);

CREATE TABLE IF NOT EXISTS sessions (
    tenant_id   TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    subject_no  TEXT NOT NULL,
    subject_gen INTEGER NOT NULL,
    members     TEXT NOT NULL,              -- JSON 数组：担保的确切身份集合
    valid       INTEGER NOT NULL,           -- 1 有效 0 失效
    invalid_reason TEXT,
    PRIMARY KEY (tenant_id, session_id)
);

CREATE TABLE IF NOT EXISTS receipts (
    tenant_id   TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    status      TEXT NOT NULL,
    payload     TEXT NOT NULL,              -- 完整收据 JSON
    PRIMARY KEY (tenant_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_edges_tenant ON edges(tenant_id);
CREATE INDEX IF NOT EXISTS idx_sessions_tenant ON sessions(tenant_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开一个带 WAL 与忙等待的连接；目录由调用方保证存在。"""
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize(db_path: str) -> None:
    """建库建表（幂等）。

    ``isolation_level=None`` 下连接处于自动提交模式，DDL 逐条生效，
    因此这里不包显式事务（executescript 也会自行处理事务边界）。
    """
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(DDL)
    finally:
        conn.close()


def maybe_crash(point: str | None, where: str) -> None:
    """崩溃注入点。

    ``point == "before_commit"``：在 COMMIT 之前抛出，连接关闭时事务回滚；
    ``point == "after_commit"``：在 COMMIT 之后、收据返回之前抛出。
    """
    if point is None:
        return
    if point == where == "before_commit":
        raise CrashBeforeCommit("synthetic crash before commit")
    if point == where == "after_commit":
        raise CrashAfterCommit("synthetic crash after commit")
