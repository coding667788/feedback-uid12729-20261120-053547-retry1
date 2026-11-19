"""独立进程并发：确认/续发/撤回同时争用同一批边。

用 run_concurrent 把多个请求组放进真正并行的 OS 进程，
SQLite 的 BEGIN IMMEDIATE 写锁把提交串行化；测试断言最终状态一致、
所有收据持久、无重复活动边。
"""

from __future__ import annotations

import unittest

from solution.runner import run_concurrent, run_in_process
from solution.store import connect
from tests._helpers import TempDB, pair_conf


def _assoc(rid: str, a: str, pa: str, ga: int, b: str, pb: str, gb: int,
           tick: int) -> dict:
    return {"op": "associate", "tenant": "t", "request_id": rid, "tick": tick,
            "identity_a": a, "identity_b": b, "not_before": 1,
            "not_after": 999,
            "confirmations": pair_conf("t", a, pa, ga, b, pb, gb, rid)}


class TestConcurrentContention(unittest.TestCase):
    def setUp(self) -> None:
        self.db = TempDB()
        self.path = self.db.detach()  # 库交由并行子进程持有
        run_in_process(self.path, [
            {"op": "register_tenant", "tenant": "t", "request_id": "r0",
             "tick": 1},
            {"op": "register_identity", "tenant": "t", "request_id": "ri1",
             "tick": 2, "identity_id": "i1", "provider": "p1"},
            {"op": "register_identity", "tenant": "t", "request_id": "ri2",
             "tick": 3, "identity_id": "i2", "provider": "p2"},
            {"op": "register_identity", "tenant": "t", "request_id": "ri3",
             "tick": 4, "identity_id": "i3", "provider": "p3"},
        ])

    def tearDown(self) -> None:
        self.db.cleanup()

    def test_parallel_associate_reissue_revoke_serialize_cleanly(self) -> None:
        groups = [
            [_assoc("e12", "i1", "p1", 1, "i2", "p2", 1, 20)],
            [{"op": "reissue", "tenant": "t", "request_id": "ri-i2",
              "tick": 21, "identity_id": "i2"}],
            [{"op": "revoke", "tenant": "t", "request_id": "rev-e12",
              "tick": 22, "edge_id": "e12"},
             _assoc("e23", "i2", "p2", 2, "i3", "p3", 1, 23)],
        ]
        receipts = run_concurrent(self.path, groups)
        # 四张收据全部持久，状态都已确定
        self.assertEqual(len(receipts), 4)
        self.assertTrue(all(r["status"] for r in receipts))
        conn = connect(self.path)
        try:
            n_receipts = conn.execute(
                "SELECT COUNT(*) c FROM receipts WHERE tenant_id='t'"
            ).fetchone()["c"]
            self.assertEqual(n_receipts, 4 + 4)  # 4 张 setup + 4 张并发
            # 任意一对身份之间至多一条活动边
            active = conn.execute(
                "SELECT id_a, id_b, COUNT(*) c FROM edges WHERE state='active'"
                " GROUP BY id_a, id_b").fetchall()
            for row in active:
                self.assertEqual(row["c"], 1)
        finally:
            conn.close()

    def test_concurrent_duplicate_associate_one_wins(self) -> None:
        # 两个进程同时为同一对身份建边，但请求编号不同（否则被幂等去重）。
        # 写锁串行化后：先到者 OK，后到者命中 DUPLICATE_ACTIVE_EDGE。
        r1 = _assoc("dup-edge-1", "i1", "p1", 1, "i2", "p2", 1, 30)
        r2 = _assoc("dup-edge-2", "i1", "p1", 1, "i2", "p2", 1, 30)
        receipts = run_concurrent(self.path, [[r1], [r2]])
        statuses = sorted(r["status"] for r in receipts)
        self.assertEqual(statuses, ["DUPLICATE_ACTIVE_EDGE", "OK"])


if __name__ == "__main__":
    unittest.main()
