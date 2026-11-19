"""生产引擎与独立重建小模型（support/oracle.py）的逐请求差分。

oracle 不 import 任何 solution 代码，用独立的内存 DFS 图与代数计数实现；
本测试在 cases.json 全部场景上逐请求断言状态码、身份代数、主体编号/成员、
会话有效性与失效原因完全一致。
"""

from __future__ import annotations

import os
import unittest

from support.diff_oracle import diff_scenario
from tests._helpers import load_cases, scenario


class TestOracleDifferential(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = load_cases()

    def test_every_scenario_matches_independent_oracle(self) -> None:
        for sc in self.doc["scenarios"]:
            with self.subTest(scenario=sc["id"]):
                records = diff_scenario(sc)
                # 每个请求都产出一条对比记录
                self.assertEqual(len(records), len(sc["requests"]))
                # 全部非崩溃请求状态都被两边一致判定
                for rec in records:
                    self.assertIn(rec["status"], {"OK"} | {
                        "TENANT_EXISTS", "UNKNOWN_TENANT", "UNKNOWN_IDENTITY",
                        "IDENTITY_EXISTS", "CLOCK_NOT_MONOTONIC",
                        "BAD_CONFIRMATION", "STALE_CONFIRMATION",
                        "CONFIRMATION_EXPIRED", "CONFIRMATION_NOT_YET_VALID",
                        "DUPLICATE_ACTIVE_EDGE", "PROVIDER_CONFLICT",
                        "EDGE_NOT_FOUND", "EDGE_REVOKED",
                        "EDGE_NOT_RENEWABLE", "NO_SUBJECT"})

    def test_conflict_scenario_expectations(self) -> None:
        sc = scenario(self.doc, "S2")
        diff_scenario(sc)  # 差分通过即引擎与 oracle 一致
        # 再用引擎实跑断言具体冲突结果
        from solution.runner import run_in_process
        import tempfile
        from solution.store import initialize
        with tempfile.TemporaryDirectory(prefix="fed-oracle-") as tmp:
            db = os.path.join(tmp, "x.db")
            initialize(db)
            receipts = run_in_process(db, sc["requests"])
            by_id = {r["request_id"]: r["status"] for r in receipts}
            self.assertEqual(by_id[sc["expected"]["conflict_request"]],
                             "PROVIDER_CONFLICT")


if __name__ == "__main__":
    unittest.main()
