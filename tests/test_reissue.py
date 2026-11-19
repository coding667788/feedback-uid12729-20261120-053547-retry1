"""身份续发（代数+1）与边续期（stale -> active）的代数链。"""

from __future__ import annotations

import unittest

from solution.engine import dispatch
from tests._helpers import (TempDB, associate, conf, register_identity,
                            register_tenant, req, snapshot)


class TestReissueAndRenewal(unittest.TestCase):
    def setUp(self) -> None:
        self.db = TempDB()
        register_tenant(self.db, "t")
        register_identity(self.db, "t", "i1", "p1", 10)
        register_identity(self.db, "t", "i2", "p2", 11)
        associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1)

    def tearDown(self) -> None:
        self.db.close()

    def test_reissue_bumps_generation_and_marks_edges_stale(self) -> None:
        r = dispatch(self.db.conn,
                     req("reissue", "t", "ri1", 22, identity_id="i1"))
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["result"]["identity_gen"], 2)
        self.assertEqual(r["result"]["edges_marked_stale"], 1)
        snap = snapshot(self.db, "t")
        self.assertEqual(snap["identity_gens"]["i1"], 2)
        edge = next(e for e in snap["edges"] if e["edge_id"] == "e12")
        self.assertEqual(edge["state"], "stale")
        # stale 边不再连通：两个单点分量
        self.assertEqual(snap["components"], [["i1"], ["i2"]])

    def test_renewal_requires_current_generation(self) -> None:
        dispatch(self.db.conn, req("reissue", "t", "ri1", 22, identity_id="i1"))
        # 用旧代数 1 自洽签发的确认 -> STALE_CONFIRMATION
        stale_confs = [
            conf("t", "i1", "p1", 1, "i2", "renew-bad", 1, 1000),
            conf("t", "i2", "p2", 1, "i1", "renew-bad", 1, 1000),
        ]
        bad = dispatch(self.db.conn,
                       req("renew_edge", "t", "renew-bad", 23, edge_id="e12",
                           not_before=1, not_after=1000,
                           confirmations=stale_confs))
        self.assertEqual(bad["status"], "STALE_CONFIRMATION")

    def test_renewal_with_current_generation_reconnects(self) -> None:
        dispatch(self.db.conn, req("reissue", "t", "ri1", 22, identity_id="i1"))
        good_confs = [
            conf("t", "i1", "p1", 2, "i2", "renew-ok", 1, 1000),
            conf("t", "i2", "p2", 1, "i1", "renew-ok", 1, 1000),
        ]
        ok = dispatch(self.db.conn,
                      req("renew_edge", "t", "renew-ok", 24, edge_id="e12",
                          not_before=1, not_after=1000,
                          confirmations=good_confs))
        self.assertEqual(ok["status"], "OK")
        self.assertEqual(ok["result"]["renewed_from"], "e12")
        snap = snapshot(self.db, "t")
        self.assertEqual(snap["components"], [["i1", "i2"]])
        # 旧 stale 边保留可追溯，新边 active
        states = {e["edge_id"]: e["state"] for e in snap["edges"]}
        self.assertEqual(states["e12"], "stale")
        self.assertEqual(states["renew-ok"], "active")

    def test_active_edge_is_not_renewable(self) -> None:
        good_confs = [
            conf("t", "i1", "p1", 1, "i2", "rn", 1, 1000),
            conf("t", "i2", "p2", 1, "i1", "rn", 1, 1000),
        ]
        r = dispatch(self.db.conn,
                     req("renew_edge", "t", "rn", 25, edge_id="e12",
                         not_before=1, not_after=1000, confirmations=good_confs))
        self.assertEqual(r["status"], "EDGE_NOT_RENEWABLE")


if __name__ == "__main__":
    unittest.main()
