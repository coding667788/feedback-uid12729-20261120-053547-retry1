"""生产引擎与独立 oracle 的逐请求差分。

对每个场景：

1. 在临时 SQLite 库上顺序跑生产引擎，收集每张收据后的快照；
2. 在内存 oracle 上重放同一请求序列；
3. 逐请求对比状态码、身份代数、主体编号/代数/成员、会话有效性与失效原因。

任何分歧都抛出 :class:`AssertionError`，供测试与命令行人工核查使用。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from solution.engine import dispatch, read_snapshot  # noqa: E402
from solution.errors import CrashAfterCommit, CrashBeforeCommit  # noqa: E402
from solution.store import connect, initialize  # noqa: E402
from support.oracle import FederatedOracle  # noqa: E402


def _norm_snapshot(snap: dict) -> dict:
    """归一化两边快照，只比较语义字段，忽略 edges 等单边细节。"""
    subjects = {no: {"gen": info["subject_gen"],
                     "members": tuple(info["members"])}
                for no, info in snap["subjects"].items()}
    sessions = {s["session_id"]: {"no": s["subject_no"],
                                  "gen": s["subject_gen"],
                                  "members": tuple(s["members"]),
                                  "valid": s["valid"],
                                  "reason": s["invalid_reason"]}
                for s in snap["sessions"]}
    return {"identity_gens": dict(snap["identity_gens"]),
            "subjects": subjects, "sessions": sessions,
            "clock": snap["clock"]}


def _oracle_snapshot(oracle: FederatedOracle, tenant: str, now: int) -> dict:
    raw = oracle.snapshot(tenant, now)
    return _norm_snapshot(raw)


def diff_scenario(scenario: dict, keep_db: str | None = None) -> list[dict]:
    """返回逐请求对比记录；出现分歧直接抛 AssertionError。"""
    if keep_db:
        db_path = keep_db
        if os.path.exists(db_path):
            os.remove(db_path)
    else:
        tmp = tempfile.TemporaryDirectory(prefix="fed-diff-")
        db_path = os.path.join(tmp.name, "fed.db")
    initialize(db_path)
    oracle = FederatedOracle()
    conn = connect(db_path)
    records = []
    try:
        for req in scenario["requests"]:
            crash = req.get("crash")
            if crash:
                # 先在引擎上制造崩溃，验证两种崩溃点的持久化语义，
                # 再去掉 crash 标记正常执行，使引擎与 oracle 状态对齐。
                try:
                    dispatch(conn, dict(req))
                except CrashBeforeCommit:
                    assert crash == "before_commit"
                    # 未提交：事务回滚、无收据，重试是一次全新执行
                    engine_receipt = dispatch(
                        conn, {k: v for k, v in req.items() if k != "crash"})
                except CrashAfterCommit:
                    assert crash == "after_commit"
                    # 已提交：副作用与收据均落库，重试只能重放历史回执
                    engine_receipt = dispatch(
                        conn, {k: v for k, v in req.items() if k != "crash"})
                    assert engine_receipt["replayed"] is True
                engine_receipt["crash_observed"] = crash
                oracle_receipt = oracle.apply(dict(req))
            else:
                engine_receipt = dispatch(conn, req)
                oracle_receipt = oracle.apply(dict(req))
            assert engine_receipt["status"] == oracle_receipt["status"], (
                f"{scenario['id']} {req['request_id']} status mismatch: "
                f"{engine_receipt['status']} vs {oracle_receipt['status']}")
            if not (crash == "after_commit"):
                assert engine_receipt["replayed"] == oracle_receipt["replayed"], (
                    f"{scenario['id']} {req['request_id']} replay flag mismatch")
            tenant = req["tenant"]
            if engine_receipt["replayed"]:
                # 重放不推进时钟：两边收据内的历史快照按“保存时的时钟”各自冻结，
                # 这里只比对状态/标记，并在两边当前实时视图上另做一次对齐比较。
                live_eng = _norm_snapshot(read_snapshot(conn, tenant))
                live_orc = _oracle_snapshot(
                    oracle, tenant, oracle.clock[tenant])
                assert live_eng == live_orc, (
                    f"{scenario['id']} {req['request_id']} live mismatch after replay")
            elif engine_receipt["snapshot"] is not None:
                eng = _norm_snapshot(engine_receipt["snapshot"])
                orc = _oracle_snapshot(oracle, tenant, eng["clock"])
                assert eng == orc, (
                    f"{scenario['id']} {req['request_id']} snapshot mismatch\n"
                    f"engine={json.dumps(eng, ensure_ascii=False, sort_keys=True)}\n"
                    f"oracle={json.dumps(orc, ensure_ascii=False, sort_keys=True)}")
            records.append({"request_id": req["request_id"],
                            "status": engine_receipt["status"],
                            "replayed": engine_receipt["replayed"]})
    finally:
        conn.close()
        if not keep_db:
            tmp.cleanup()
    return records


def diff_doc(doc: dict) -> dict:
    report = {}
    for scenario in doc["scenarios"]:
        report[scenario["id"]] = diff_scenario(scenario)
    return report


if __name__ == "__main__":
    cases_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "cases.json")
    with open(cases_path, encoding="utf-8") as f:
        doc = json.load(f)
    report = diff_doc(doc)
    for sid, rows in report.items():
        print(sid, len(rows), "requests match oracle")
    print("ALL DIFFS CLEAR")
