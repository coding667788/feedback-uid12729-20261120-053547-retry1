"""模拟确认的签名、代数、时间窗与重放规则。"""

from __future__ import annotations

import json
import unittest

from solution.confirmations import sign_confirmation, verify_confirmation
from solution.engine import dispatch
from tests._helpers import (TempDB, associate, pair_conf, register_identity,
                            register_tenant, req)


class TestConfirmationVerification(unittest.TestCase):
    def _kw(self, **over):
        base = dict(tenant="t", identity="i1", peer="i2", request_id="r1",
                    provider="p1", identity_gen=1, now=50)
        base.update(over)
        return base

    def test_valid_confirmation_passes(self) -> None:
        c = sign_confirmation("t", "p1", "i1", 1, "i2", "r1", 10, 100)
        self.assertIsNone(verify_confirmation(c, **self._kw()))

    def test_tampered_signature_rejected(self) -> None:
        c = sign_confirmation("t", "p1", "i1", 1, "i2", "r1", 10, 100)
        c["sig"] = "00" * 32
        self.assertEqual(verify_confirmation(c, **self._kw()),
                         "BAD_CONFIRMATION")

    def test_wrong_binding_rejected(self) -> None:
        c = sign_confirmation("t", "p1", "i1", 1, "i2", "r1", 10, 100)
        # 挪到另一个 request_id 上 -> 绑定不一致
        self.assertEqual(
            verify_confirmation(c, **self._kw(request_id="other")),
            "BAD_CONFIRMATION")

    def test_stale_generation_rejected(self) -> None:
        c = sign_confirmation("t", "p1", "i1", 1, "i2", "r1", 10, 100)
        self.assertEqual(
            verify_confirmation(c, **self._kw(identity_gen=2)),
            "STALE_CONFIRMATION")

    def test_time_window_enforced(self) -> None:
        c = sign_confirmation("t", "p1", "i1", 1, "i2", "r1", 10, 100)
        self.assertEqual(
            verify_confirmation(c, **self._kw(now=5)),
            "CONFIRMATION_NOT_YET_VALID")
        self.assertEqual(
            verify_confirmation(c, **self._kw(now=101)),
            "CONFIRMATION_EXPIRED")


class TestEngineConfirmationPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.db = TempDB()
        register_tenant(self.db, "t")
        register_identity(self.db, "t", "i1", "p1", 10)
        register_identity(self.db, "t", "i2", "p2", 11)

    def tearDown(self) -> None:
        self.db.close()

    def test_missing_second_confirmation_rejected(self) -> None:
        only_one = pair_conf("t", "i1", "p1", 1, "i2", "p2", 1, "r")[:1]
        r = dispatch(self.db.conn,
                     req("associate", "t", "r", 20, identity_a="i1",
                         identity_b="i2", not_before=1, not_after=100,
                         confirmations=only_one))
        self.assertEqual(r["status"], "BAD_CONFIRMATION")

    def test_duplicate_confirmation_rejected(self) -> None:
        c = pair_conf("t", "i1", "p1", 1, "i2", "p2", 1, "r")
        r = dispatch(self.db.conn,
                     req("associate", "t", "r", 20, identity_a="i1",
                         identity_b="i2", not_before=1, not_after=100,
                         confirmations=[c[0], c[0]]))
        self.assertEqual(r["status"], "BAD_CONFIRMATION")

    def test_replay_returns_historical_receipt_without_side_effect(self) -> None:
        first = associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1)
        self.assertFalse(first["replayed"])
        # 同 ID 重放（即便 tick 落后）只回历史收据
        replay = associate(self.db, "t", "e12", 20, "i1", "p1", 1,
                           "i2", "p2", 1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "OK")
        # 边仍然只有一条
        snap = dispatch(self.db.conn,
                        req("login", "t", "probe", 21, identity_id="i1"))
        self.assertEqual(snap["result"]["members"], ["i1", "i2"])

    def test_clock_must_be_non_decreasing(self) -> None:
        associate(self.db, "t", "e12", 20, "i1", "p1", 1, "i2", "p2", 1)
        r = dispatch(self.db.conn,
                     req("login", "t", "late", 19, identity_id="i1"))
        self.assertEqual(r["status"], "CLOCK_NOT_MONOTONIC")


if __name__ == "__main__":
    unittest.main()
