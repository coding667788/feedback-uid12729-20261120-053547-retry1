"""请求序列回放器：进程内直调与独立子进程两种模式。

* :func:`run_in_process` 在当前进程顺序处理，便于单元测试与快速差分；
* :func:`run_subprocess` 每个请求派生全新 ``python -m solution.worker`` 进程，
  用于崩溃注入、重启重放与并发争用场景；
* 崩溃请求在子进程以特殊退出码终止后，父进程清除 ``crash`` 字段，
  按同一请求编号重放，验证“未提交则无副作用、已提交则只重放收据”。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from .engine import dispatch
from .errors import EXIT_CRASH_AFTER_COMMIT, EXIT_CRASH_BEFORE_COMMIT
from .store import connect

# 工作区根（solution 包的父目录）与子进程引导脚本。
# python -I 会忽略 PYTHONPATH 且不把 cwd 加入 sys.path，
# 故子进程改为运行 support 下的引导脚本，由它自行注入根目录再按包导入。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOOTSTRAP = os.path.join(_ROOT, "support", "_child_bootstrap.py")
_BOOTSTRAP_BATCH = os.path.join(_ROOT, "support", "_child_bootstrap_batch.py")


def run_in_process(db_path: str, requests: list[dict]) -> list[dict]:
    """顺序回放，返回每张收据（含拒绝收据）。"""
    receipts = []
    conn = connect(db_path)
    try:
        for req in requests:
            receipts.append(dispatch(conn, req))
    finally:
        conn.close()
    return receipts


def _spawn(db_path: str, request: dict) -> tuple[int, dict | None]:
    proc = subprocess.run(
        [sys.executable, "-I", _BOOTSTRAP,
         "--db", db_path, "--request-json", json.dumps(request, ensure_ascii=False)],
        capture_output=True, text=True, encoding="utf-8")
    receipt = None
    if proc.returncode == 0 and proc.stdout.strip():
        receipt = json.loads(proc.stdout)
    return proc.returncode, receipt


def run_subprocess(db_path: str, requests: list[dict],
                   retry_crashes: bool = True) -> list[dict]:
    """每请求一个独立进程；崩溃后按 ID 重放，返回最终收据序列。

    返回列表与 ``requests`` 等长：崩溃请求的位置放重放后的收据，
    并附加 ``crash_observed`` 字段记录观察到的崩溃点。
    """
    receipts: list[dict] = []
    for req in requests:
        code, receipt = _spawn(db_path, req)
        observed = None
        if code == EXIT_CRASH_BEFORE_COMMIT:
            observed = "before_commit"
        elif code == EXIT_CRASH_AFTER_COMMIT:
            observed = "after_commit"
        if observed is not None and retry_crashes:
            clean = {k: v for k, v in req.items() if k != "crash"}
            code2, receipt = _spawn(db_path, clean)
            if code2 != 0:
                raise RuntimeError(
                    f"replay after crash failed: rc={code2}, "
                    f"stderr-only crash; request={req.get('request_id')}")
            if receipt is not None:
                receipt["crash_observed"] = observed
        elif observed is not None:
            receipt = {"request_id": req["request_id"],
                       "status": "CRASH_UNRETRIED", "crash_observed": observed}
        if receipt is None:
            raise RuntimeError(
                f"worker produced no receipt, rc={code}: {req.get('request_id')}")
        receipts.append(receipt)
    return receipts


def run_concurrent(db_path: str, groups: list[list[dict]],
                   timeout: float = 60.0) -> list[dict]:
    """多进程并发：每组请求在一个独立进程内顺序执行，组间真正并行。

    用 :class:`subprocess.Popen` 同时启动，SQLite 写锁负责串行化提交；
    收集全部收据后按 request_id 排序，得到确定性顺序供断言。
    """
    procs = []
    for group in groups:
        payload = json.dumps(group, ensure_ascii=False)
        proc = subprocess.Popen(
            [sys.executable, "-I", _BOOTSTRAP_BATCH, "--db", db_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8")
        procs.append((proc, payload))

    receipts: list[dict] = []
    for proc, payload in procs:
        stdout, stderr = proc.communicate(payload, timeout=timeout)
        if proc.returncode != 0:
            raise RuntimeError(
                f"concurrent worker rc={proc.returncode}: {stderr[:300]}")
        receipts.extend(json.loads(stdout))
    receipts.sort(key=lambda r: r["request_id"])
    return receipts
