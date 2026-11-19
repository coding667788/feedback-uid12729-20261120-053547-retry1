"""会话担保、失效条件与“旧代数永不复活”。"""

from __future__ import annotations

import unittest

from solution.engine import dispatch
from tests._helpers import (TempDB, associate, register_identity,
                            register_tenant, req, snapshot)


def login(db: TempDB, rid: str, tick: int, ident: str) -> dict:
    return dispatch(db.conn, req("login", "t", rid, tick, identity_id=ident))


class TestSessionGuarantee(unittest.TestCase):
    def setUp(self) -> None:
        self.db = TempDB()
        register_tenant(self.db, "t")
        register_identity(self.db, "t", "i1", "p1", 10)
        register_identity(self.db, "t", "i2", "p2", 11)
        register_identity(self.db, "t", "i3", "p3", 12)

    def tearDown(self) -> None:
        self.db.close()

    def test_session_guarantees_exact_members_and_generation(self) -> None:
        associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1)
        r = login(self.db, "s1", 21, "i1")
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["result"]["members"], ["i1", "i2"])
        self.assertEqual(r["result"]["subject_no"], "i1")
        snap = snapshot(self.db, "t")
        s = next(x for x in snap["sessions"] if x["session_id"] == "s1")
        self.assertTrue(s["valid"])
        self.assertEqual(s["members"], ["i1", "i2"])

    def test_session_invalidated_when_subject_advances(self) -> None:
        associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1)
        login(self.db, "s1", 21, "i1")
        # 并入 i3，主体成员集合变化、代数推进
        associate(self.db, "t", "e23", 22, "i2", "p2", 1, "i3", "p3", 1)
        snap = snapshot(self.db, "t")
        s = next(x for x in snap["sessions"] if x["session_id"] == "s1")
        self.assertFalse(s["valid"])
        self.assertEqual(s["invalid_reason"], "SUBJECT_GEN_ADVANCED")

    def test_old_generation_never_revives(self) -> None:
        associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1)
        login(self.db, "s1", 21, "i1")
        gen_at_login = next(
            x for x in snapshot(self.db, "t")["sessions"]
            if x["session_id"] == "s1")["subject_gen"]
        # 撤回 e12 拆成两个单点，再重新连回同样的成员集合
        dispatch(self.db.conn, req("revoke", "t", "rev1", 22, edge_id="e12"))
        associate(self.db, "t", "e12b", 23, "i1", "p1", 1, "i2", "p2", 1)
        snap = snapshot(self.db, "t")
        s = next(x for x in snap["sessions"] if x["session_id"] == "s1")
        self.assertFalse(s["valid"], "旧代数会话不得因分量恢复原样而复活")
        new_gen = snap["subjects"]["i1"]["subject_gen"]
        self.assertGreater(new_gen, gen_at_login)
        # 新登录拿到的是新代数
        r2 = login(self.db, "s2", 24, "i1")
        self.assertEqual(r2["result"]["subject_gen"], new_gen)
        self.assertTrue(next(
            x for x in snapshot(self.db, "t")["sessions"]
            if x["session_id"] == "s2")["valid"])

    def test_expiry_invalidates_prior_session(self) -> None:
        # 边有效期截止 tick 30
        associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1,
                  nb=10, na=30)
        login(self.db, "s1", 25, "i1")
        # tick 31 登录触发重建：到期边使分量拆分
        r = login(self.db, "s2", 31, "i2")
        self.assertEqual(r["result"]["members"], ["i2"])
        snap = snapshot(self.db, "t")
        old = next(x for x in snap["sessions"] if x["session_id"] == "s1")
        self.assertFalse(old["valid"])

    def test_split_invalidates_sessions_on_both_sides(self) -> None:
        associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1)
        associate(self.db, "t", "e23", 21, "i2", "p2", 1, "i3", "p3", 1)
        login(self.db, "s1", 22, "i1")
        dispatch(self.db.conn, req("revoke", "t", "rev-e12", 23, edge_id="e12"))
        snap = snapshot(self.db, "t")
        s = next(x for x in snap["sessions"] if x["session_id"] == "s1")
        self.assertFalse(s["valid"])
        self.assertEqual(snap["components"], [["i1"], ["i2", "i3"]])


if __name__ == "__main__":
    unittest.main()
