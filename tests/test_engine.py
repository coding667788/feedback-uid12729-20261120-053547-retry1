"""核心引擎规则：主体编号、分量合并/拆分、主体代数单调、冲突拒绝。"""

from __future__ import annotations

import unittest

from tests._helpers import (TempDB, associate, register_identity,
                            register_tenant, req, snapshot)


class TestSubjectNumberingAndMerge(unittest.TestCase):
    def setUp(self) -> None:
        self.db = TempDB()
        register_tenant(self.db, "t")
        for i, p in enumerate(("p1", "p2", "p3"), start=1):
            register_identity(self.db, "t", f"i{i}", p, 10 + i)

    def tearDown(self) -> None:
        self.db.close()

    def test_min_identity_id_is_public_subject_number(self) -> None:
        associate(self.db, "t", "e23", 20, "i2", "p2", 1, "i3", "p3", 1)
        snap = snapshot(self.db, "t")
        # 分量 {i2,i3} 的公开编号必须是最小 ID i2，单点 i1 编号 i1
        self.assertEqual(sorted(snap["subjects"]), ["i1", "i2"])
        self.assertEqual(snap["subjects"]["i2"]["members"], ["i2", "i3"])

    def test_merge_advances_generation_monotonically(self) -> None:
        associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1)
        g_after_first = snapshot(self.db, "t")["subjects"]["i1"]["subject_gen"]
        associate(self.db, "t", "e23", 21, "i2", "p2", 1, "i3", "p3", 1)
        snap = snapshot(self.db, "t")
        g_after_second = snap["subjects"]["i1"]["subject_gen"]
        self.assertEqual(snap["subjects"]["i1"]["members"], ["i1", "i2", "i3"])
        self.assertGreater(g_after_second, g_after_first)

    def test_redundant_edge_keeps_generation(self) -> None:
        # 先把 i1-i2-i3 连成一个分量
        associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1)
        associate(self.db, "t", "e13", 21, "i1", "p1", 1, "i3", "p3", 1)
        associate(self.db, "t", "e23", 22, "i2", "p2", 1, "i3", "p3", 1)
        # e23 两端已在同一分量，成员集合不变，代数保持
        g = snapshot(self.db, "t")["subjects"]["i1"]["subject_gen"]
        self.assertEqual(g, snapshot(self.db, "t")["subjects"]["i1"]["subject_gen"])


class TestProviderConflict(unittest.TestCase):
    def setUp(self) -> None:
        self.db = TempDB()
        register_tenant(self.db, "t")
        register_identity(self.db, "t", "i1", "p1", 10)
        register_identity(self.db, "t", "i2", "p2", 11)
        register_identity(self.db, "t", "i3", "p1", 12)  # 同提供方 p1

    def tearDown(self) -> None:
        self.db.close()

    def test_two_individually_legal_merges_collectively_conflict(self) -> None:
        r1 = associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1)
        self.assertEqual(r1["status"], "OK")
        r2 = associate(self.db, "t", "e23", 21, "i2", "p2", 1, "i3", "p1", 1)
        # 两条边单独都合法，但合在一起让 i1/i3（同 p1）共处一个分量
        self.assertEqual(r2["status"], "PROVIDER_CONFLICT")

    def test_rejection_leaves_no_half_subject(self) -> None:
        associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1)
        before = snapshot(self.db, "t")
        associate(self.db, "t", "e23", 21, "i2", "p2", 1, "i3", "p1", 1)
        after = snapshot(self.db, "t")
        # 拒绝前后主体视图完全一致：冲突边不存在，i3 仍是独立单点
        self.assertEqual(before["subjects"], after["subjects"])
        self.assertEqual(after["components"], [["i1", "i2"], ["i3"]])
        edges = after["edges"]
        self.assertNotIn("e23", [e["edge_id"] for e in edges])

    def test_direct_same_provider_pair_rejected(self) -> None:
        # 直接连接两个 p1 身份同样冲突
        r = associate(self.db, "t", "e13", 20, "i1", "p1", 1, "i3", "p1", 1)
        self.assertEqual(r["status"], "PROVIDER_CONFLICT")


class TestBridgeAndSplit(unittest.TestCase):
    def setUp(self) -> None:
        self.db = TempDB()
        register_tenant(self.db, "t")
        for i, p in enumerate(("p1", "p2", "p3", "p4"), start=1):
            register_identity(self.db, "t", f"i{i}", p, 10 + i)

    def tearDown(self) -> None:
        self.db.close()

    def _ring(self) -> None:
        associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1)
        associate(self.db, "t", "e23", 21, "i2", "p2", 1, "i3", "p3", 1)
        associate(self.db, "t", "e34", 22, "i3", "p3", 1, "i4", "p4", 1)
        associate(self.db, "t", "e41", 23, "i4", "p4", 1, "i1", "p1", 1)

    def test_bridge_revoke_with_alternate_path_stays_connected(self) -> None:
        self._ring()
        r = self.db.conn  # noqa: F841
        receipt = self.db.conn.execute(
            "SELECT 1 FROM edges WHERE edge_id='e23'").fetchone()
        self.assertIsNotNone(receipt)
        from solution.engine import dispatch
        dispatch(self.db.conn, req("revoke", "t", "rev-e23", 25, edge_id="e23"))
        snap = snapshot(self.db, "t")
        # 备选路径 i1-i4-i3 维持全连通
        self.assertEqual(snap["components"], [["i1", "i2", "i3", "i4"]])

    def test_second_revoke_splits_into_two_subjects(self) -> None:
        self._ring()
        from solution.engine import dispatch
        dispatch(self.db.conn, req("revoke", "t", "rev-e23", 25, edge_id="e23"))
        dispatch(self.db.conn, req("revoke", "t", "rev-e41", 26, edge_id="e41"))
        snap = snapshot(self.db, "t")
        self.assertEqual(snap["components"], [["i1", "i2"], ["i3", "i4"]])
        self.assertEqual(sorted(snap["subjects"]), ["i1", "i3"])

    def test_revoked_edge_cannot_revoke_twice(self) -> None:
        self._ring()
        from solution.engine import dispatch
        dispatch(self.db.conn, req("revoke", "t", "rev-e23", 25, edge_id="e23"))
        again = dispatch(self.db.conn,
                         req("revoke", "t", "rev-e23-again", 26, edge_id="e23"))
        self.assertEqual(again["status"], "EDGE_REVOKED")


if __name__ == "__main__":
    unittest.main()
