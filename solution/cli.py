"""命令行入口：回放 cases.json 并产出 result.json。

用法::

    python -m solution --input cases.json --output result.json [--db dir/fed.db]

cases.json 结构见 README；result.json 包含每个场景的请求收据序列、
最终快照以及（可选）独立参考模型的差分结论。
"""

from __future__ import annotations

import argparse
import json
import os

from .engine import read_snapshot
from .runner import run_in_process, run_subprocess
from .store import connect, initialize


def _load_doc(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_scenario(scenario: dict, db_path: str, *, use_subprocess: bool) -> dict:
    if os.path.exists(db_path):
        os.remove(db_path)
    # 同时清掉 WAL 侧车文件，保证场景间完全隔离
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)
    initialize(db_path)
    requests = scenario["requests"]
    skipped_crash = []
    if use_subprocess:
        # 子进程模式：worker 以特殊退出码终止后由 runner 自动按 ID 重放
        receipts = run_subprocess(db_path, requests)
    else:
        # 进程内模式无法模拟“进程被杀”：崩溃注入请求剥离 crash 字段后执行，
        # 并记录被跳过的注入点；真实崩溃语义由 tests/test_crash_recovery.py 覆盖。
        clean_requests = []
        for req in requests:
            if req.get("crash"):
                skipped_crash.append({"request_id": req["request_id"],
                                      "crash": req["crash"]})
                req = {k: v for k, v in req.items() if k != "crash"}
            clean_requests.append(req)
        receipts = run_in_process(db_path, clean_requests)
    conn = connect(db_path)
    try:
        snapshots = {}
        tenants = {r["tenant"] for r in requests}
        for tenant in sorted(tenants):
            try:
                snapshots[tenant] = read_snapshot(conn, tenant)
            except Exception as exc:  # pragma: no cover - 防御性
                snapshots[tenant] = {"error": str(exc)}
    finally:
        conn.close()
    return {"scenario": scenario.get("name", scenario.get("id", "scenario")),
            "description": scenario.get("description", ""),
            "receipts": receipts, "final_snapshots": snapshots,
            "crash_injections_stripped_in_process": skipped_crash}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地联邦身份目录回放器")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--db", default=None,
                        help="SQLite 文件位置，默认放在输出文件同目录")
    parser.add_argument("--subprocess", action="store_true",
                        help="每个请求用独立 python 进程执行")
    args = parser.parse_args(argv)

    doc = _load_doc(args.input)
    out_dir = os.path.dirname(os.path.abspath(args.output))
    os.makedirs(out_dir, exist_ok=True)
    # 场景库统一收敛到输出目录下的 fed-db 子目录，不污染工作区根
    db_dir = os.path.join(out_dir, "fed-db")
    os.makedirs(db_dir, exist_ok=True)

    results = []
    for scenario in doc["scenarios"]:
        db_path = args.db or os.path.join(
            db_dir, f"fed-{scenario.get('id', scenario.get('name', 'x'))}.db")
        results.append(run_scenario(scenario, db_path,
                                    use_subprocess=args.subprocess))

    summary = {
        "tool": "solution",
        "input": os.path.abspath(args.input),
        "scenario_count": len(results),
        "receipt_count": sum(len(r["receipts"]) for r in results),
        "results": results,
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"wrote {args.output}: {summary['scenario_count']} scenarios, "
          f"{summary['receipt_count']} receipts")
    return 0
