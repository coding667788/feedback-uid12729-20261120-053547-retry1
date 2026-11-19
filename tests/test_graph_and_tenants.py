"""图纯函数（连通分量、提供方冲突）与多租户编号隔离。"""

from __future__ import annotations

import unittest

from solution import graph
from solution.engine import dispatch
from tests._helpers import (TempDB, associate, register_identity,
                            register_tenant, req, snapshot)


class TestGraphPrimitives(unittest.TestCase):
    def test_components_include_isolated_nodes(self) -> None:
        identities = {"i1": "p1", "i2": "p2", "i3": "p3"}
        comps = graph.connected_components(identities, [("i1", "i2")])
        self.assertEqual(comps, [{"i1", "i2"}, {"i3"}])

    def test_components_sorted_by_min_id(self) -> None:
        identities = {f"i{k}": f"p{k}" for k in range(1, 5)}
        comps = graph.connected_components(
            identities, [("i3", "i4"), ("i1", "i2")])
        self.assertEqual([min(c) for c in comps], ["i1", "i3"])

    def test_provider_conflict_detection(self) -> None:
        identities = {"i1": "p1", "i2": "p2", "i3": "p1"}
        self.assertIsNone(graph.check_provider_conflict(
            identities, {"i1", "i2"}))
        self.assertEqual(
            graph.check_provider_conflict(identities, {"i1", "i2", "i3"}),
            "p1")


class TestTenantIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.db = TempDB()

    def tearDown(self) -> None:
        self.db.close()

    def test_same_numbers_isolated_across_tenants(self) -> None:
        for tenant in ("tX", "tY"):
            register_tenant(self.db, tenant)
            register_identity(self.db, tenant, "i1", "p1", 10)
            register_identity(self.db, tenant, "i2", "p2", 11)
            # 相同请求编号在两个租户各执行一次，互不干扰
            associate(self.db, tenant, "shared-edge", 20,
                      "i1", "p1", 1, "i2", "p2", 1)
            dispatch(self.db.conn,
                     req("login", tenant, "shared-login", 21, identity_id="i1"))
        sx = snapshot(self.db, "tX")
        sy = snapshot(self.db, "tY")
        # 两租户各自独立维护主体编号 i1 与代数
        self.assertEqual(sx["subjects"]["i1"]["members"], ["i1", "i2"])
        self.assertEqual(sy["subjects"]["i1"]["members"], ["i1", "i2"])
        self.assertEqual(len(sx["sessions"]), 1)
        self.assertEqual(len(sy["sessions"]), 1)
        # 收据表按 (tenant, request_id) 主键共存两份
        rows = self.db.conn.execute(
            "SELECT tenant_id FROM receipts WHERE request_id='shared-edge'"
        ).fetchall()
        self.assertEqual(sorted(r["tenant_id"] for r in rows), ["tX", "tY"])

    def test_unknown_tenant_rejected(self) -> None:
        r = dispatch(self.db.conn,
                     req("login", "ghost", "g1", 1, identity_id="i1"))
        self.assertEqual(r["status"], "UNKNOWN_TENANT")


if __name__ == "__main__":
    unittest.main()
