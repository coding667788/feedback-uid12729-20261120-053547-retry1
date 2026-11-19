"""测试共享工具：临时库、请求构造、收据查询。"""

from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from solution.confirmations import sign_confirmation  # noqa: E402
from solution.engine import dispatch, read_snapshot  # noqa: E402
from solution.store import connect, initialize  # noqa: E402


class TempDB:
    """每个测试一个独立临时 SQLite 库，关闭时清理整个临时目录。

    子进程测试会把库交给独立 OS 进程使用，调用 :meth:`detach` 后
    ``close`` 只断开连接、不删目录，由测试在末尾用 :meth:`cleanup` 回收。
    """

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fed-test-")
        self.dir = self._tmp.name
        self.path = os.path.join(self.dir, "fed.db")
        initialize(self.path)
        self.conn = connect(self.path)
        self._detached = False

    def detach(self) -> str:
        """分离临时目录所有权，返回库路径；关闭连接时不再清理。"""
        self._detached = True
        self.conn.close()
        return self.path

    def cleanup(self) -> None:
        """显式回收分离出去的临时目录（幂等）。"""
        if self._detached:
            self._tmp.cleanup()
            self._detached = False

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            if not self._detached:
                self._tmp.cleanup()

    def __enter__(self) -> "TempDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def conf(tenant: str, ident: str, provider: str, gen: int, peer: str,
         rid: str, nb: int = 1, na: int = 10_000) -> dict:
    return sign_confirmation(tenant, provider, ident, gen, peer, rid, nb, na)


def pair_conf(tenant: str, a: str, pa: str, ga: int, b: str, pb: str,
              gb: int, rid: str, nb: int = 1, na: int = 10_000) -> list[dict]:
    return [conf(tenant, a, pa, ga, b, rid, nb, na),
            conf(tenant, b, pb, gb, a, rid, nb, na)]


def req(op: str, tenant: str, rid: str, tick: int, **extra) -> dict:
    base = {"op": op, "tenant": tenant, "request_id": rid, "tick": tick}
    base.update(extra)
    return base


def register_tenant(db: TempDB, tenant: str, tick: int = 1) -> dict:
    return dispatch(db.conn, req("register_tenant", tenant, f"{tenant}-r00", tick))


def register_identity(db: TempDB, tenant: str, ident: str, provider: str,
                      tick: int) -> dict:
    return dispatch(db.conn,
                    req("register_identity", tenant, f"{tenant}-reg-{ident}",
                        tick, identity_id=ident, provider=provider))


def associate(db: TempDB, tenant: str, rid: str, tick: int, a: str, pa: str,
              ga: int, b: str, pb: str, gb: int, *, nb: int = 1,
              na: int = 10_000, crash: str | None = None) -> dict:
    r = req("associate", tenant, rid, tick, identity_a=a, identity_b=b,
            not_before=nb, not_after=na,
            confirmations=pair_conf(tenant, a, pa, ga, b, pb, gb, rid, nb, na))
    if crash:
        r["crash"] = crash
    return dispatch(db.conn, r)


def statuses(receipts: list[dict]) -> list[str]:
    return [r["status"] for r in receipts]


def snapshot(db: TempDB, tenant: str) -> dict:
    return read_snapshot(db.conn, tenant)


def load_cases() -> dict:
    with open(os.path.join(ROOT, "cases.json"), encoding="utf-8") as f:
        return json.load(f)


def scenario(doc: dict, sid: str) -> dict:
    return next(s for s in doc["scenarios"] if s["id"] == sid)
