"""命令行端到端：python -m solution --input ... --output ..."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestCli(unittest.TestCase):
    def test_cli_writes_result_document(self) -> None:
        out = os.path.join(ROOT, ".runtime-tmp", "cli-result.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if os.path.exists(out):
            os.remove(out)
        proc = subprocess.run(
            [sys.executable, "-m", "solution",
             "--input", os.path.join(ROOT, "cases.json"),
             "--output", out],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))
        with open(out, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["scenario_count"], 8)
        self.assertEqual(doc["receipt_count"], 68)
        # 每个场景都带最终快照
        for result in doc["results"]:
            self.assertIn("final_snapshots", result)
            self.assertTrue(result["receipts"])

    def test_cli_subprocess_mode(self) -> None:
        out = os.path.join(ROOT, ".runtime-tmp", "cli-result-sp.json")
        if os.path.exists(out):
            os.remove(out)
        proc = subprocess.run(
            [sys.executable, "-m", "solution",
             "--input", os.path.join(ROOT, "cases.json"),
             "--output", out, "--subprocess"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out, encoding="utf-8") as f:
            doc = json.load(f)
        crash_result = next(
            r for r in doc["results"]
            if r["scenario"] == "process_crash_before_and_after_commit")
        observed = {x["request_id"]: x.get("crash_observed")
                    for x in crash_result["receipts"]}
        self.assertEqual(observed["tF-e12-bc"], "before_commit")
        self.assertEqual(observed["tF-e23-ac"], "after_commit")


if __name__ == "__main__":
    unittest.main()
