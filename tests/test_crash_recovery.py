"""独立 OS 进程的崩溃恢复：未提交回滚、已提交重放、按 ID 重启。

每个 worker 都是真实派生的独立 python 进程（见 support/_child_bootstrap.py），
崩溃以特殊退出码终止，runner 清除 crash 字段后按同一请求编号重放。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

from solution.runner import _BOOTSTRAP, run_subprocess
from solution.store import connect
from tests._helpers import TempDB, pair_conf

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _crash_request(tenant: str, rid: str, point: str) -> dict:
    a, b = f"{tenant}-i1", f"{tenant}-i2"
    return {
        "op": "associate", "tenant": tenant, "request_id": rid, "tick": 30,
        "identity_a": a, "identity_b": b, "not_before": 1, "not_after": 999,
        "crash": point,
        "confirmations": pair_conf(tenant, a, "p1", 1, b, "p2", 1, rid),
    }


class TestProcessCrashRecovery(unittest.TestCase):
    def _fresh_db(self) -> str:
        db = TempDB()
        # 库将交由独立 OS 进程持有：分离目录所有权，测试结束统一回收
        path = db.detach()
        self._detached.append(db)
        # 基础数据：建租户 + 两个身份（无崩溃）
        from solution.runner import run_in_process
        run_in_process(path, [
            {"op": "register_tenant", "tenant": "t", "request_id": "r0",
             "tick": 1},
            {"op": "register_identity", "tenant": "t", "request_id": "ri1",
             "tick": 2, "identity_id": "t-i1", "provider": "p1"},
            {"op": "register_identity", "tenant": "t", "request_id": "ri2",
             "tick": 3, "identity_id": "t-i2", "provider": "p2"},
        ])
        return path

    def setUp(self) -> None:
        self._detached: list[TempDB] = []
        self.path = self._fresh_db()

    def tearDown(self) -> None:
        for db in self._detached:
            db.cleanup()

    def _raw_spawn_on(self, db_path: str, request: dict) -> int:
        proc = subprocess.run(
            [sys.executable, "-I", _BOOTSTRAP, "--db", db_path,
             "--request-json", json.dumps(request, ensure_ascii=False)],
            capture_output=True, text=True, encoding="utf-8")
        return proc.returncode

    def _raw_spawn(self, request: dict) -> int:
        return self._raw_spawn_on(self.path, request)

    def test_crash_before_commit_leaves_no_side_effect(self) -> None:
        rid = "edge-before-commit"
        rc = self._raw_spawn(_crash_request("t", rid, "before_commit"))
        self.assertEqual(rc, 87)  # EXIT_CRASH_BEFORE_COMMIT
        # 进程已终止：库里既没有边也没有收据
        conn = connect(self.path)
        try:
            edge = conn.execute(
                "SELECT 1 FROM edges WHERE edge_id=?", (rid,)).fetchone()
            receipt = conn.execute(
                "SELECT 1 FROM receipts WHERE request_id=?", (rid,)).fetchone()
            self.assertIsNone(edge)
            self.assertIsNone(receipt)
        finally:
            conn.close()
        # 重启重放由 runner 完成：观察到 87 后去 crash 字段重试，
        # 因未提交无收据，重试是一次全新执行（replayed=False）。
        receipts = run_subprocess(
            self.path, [_crash_request("t", rid, "before_commit")])
        self.assertEqual(receipts[0]["status"], "OK")
        self.assertFalse(receipts[0]["replayed"])
        self.assertEqual(receipts[0]["crash_observed"], "before_commit")
        conn = connect(self.path)
        try:
            edge = conn.execute(
                "SELECT 1 FROM edges WHERE edge_id=?", (rid,)).fetchone()
            self.assertIsNotNone(edge, "重放后边应已正常落库")
        finally:
            conn.close()

    def test_crash_after_commit_replays_historical_receipt(self) -> None:
        rid = "edge-after-commit"
        # 在一个独立库上先制造崩溃，验证“提交已落库、进程未回执”的中间态
        probe = self._fresh_db()
        rc = self._raw_spawn_on(probe, _crash_request("t", rid, "after_commit"))
        self.assertEqual(rc, 88)  # EXIT_CRASH_AFTER_COMMIT
        conn = connect(probe)
        try:
            edge = conn.execute(
                "SELECT state FROM edges WHERE edge_id=?", (rid,)).fetchone()
            self.assertIsNotNone(edge)
            self.assertEqual(edge["state"], "active")
            receipt = conn.execute(
                "SELECT 1 FROM receipts WHERE request_id=?", (rid,)).fetchone()
            self.assertIsNotNone(receipt)
        finally:
            conn.close()

        # 在测试主库上让 runner 完整处理崩溃请求：它观察到退出码 88 后
        # 自动按 ID 重放，重放命中已持久收据，只回历史回执并附崩溃点标记。
        receipts = run_subprocess(
            self.path, [_crash_request("t", rid, "after_commit")])
        self.assertEqual(receipts[0]["status"], "OK")
        self.assertTrue(receipts[0]["replayed"])
        self.assertEqual(receipts[0]["crash_observed"], "after_commit")
        conn = connect(self.path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) c FROM edges WHERE edge_id=?",
                (rid,)).fetchone()["c"]
            self.assertEqual(n, 1)
        finally:
            conn.close()

    def test_runner_replays_both_crash_points_automatically(self) -> None:
        # 再注册两个身份，让两个崩溃请求连不同的身份对，互不触发重复边
        from solution.runner import run_in_process
        run_in_process(self.path, [
            {"op": "register_identity", "tenant": "t", "request_id": "ri3",
             "tick": 4, "identity_id": "t-i3", "provider": "p3"},
            {"op": "register_identity", "tenant": "t", "request_id": "ri4",
             "tick": 5, "identity_id": "t-i4", "provider": "p4"},
        ])

        def crash_req(rid: str, point: str, a: str, b: str,
                      pa: str, pb: str) -> dict:
            return {
                "op": "associate", "tenant": "t", "request_id": rid,
                "tick": 30, "identity_a": a, "identity_b": b,
                "not_before": 1, "not_after": 999, "crash": point,
                "confirmations": pair_conf("t", a, pa, 1, b, pb, 1, rid)}

        receipts = run_subprocess(self.path, [
            crash_req("auto-bc-uniq", "before_commit",
                      "t-i1", "t-i2", "p1", "p2"),
            crash_req("auto-ac-uniq", "after_commit",
                      "t-i3", "t-i4", "p3", "p4"),
        ])
        self.assertEqual([r["status"] for r in receipts], ["OK", "OK"])
        self.assertEqual(receipts[0]["crash_observed"], "before_commit")
        self.assertEqual(receipts[1]["crash_observed"], "after_commit")
        self.assertFalse(receipts[0]["replayed"])
        self.assertTrue(receipts[1]["replayed"])


if __name__ == "__main__":
    unittest.main()
