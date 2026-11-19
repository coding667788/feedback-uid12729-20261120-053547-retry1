"""独立进程入口：一个进程只处理一个请求然后退出。

用法::

    python -m solution.worker --db <db> --request-json '<json>'

崩溃注入请求会以固定退出码终止（87/88），由父进程捕获后重启重放。
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import dispatch
from .errors import (CrashAfterCommit, CrashBeforeCommit,
                     EXIT_CRASH_AFTER_COMMIT, EXIT_CRASH_BEFORE_COMMIT)
from .store import connect, initialize


def run_one(db_path: str, request: dict) -> dict:
    # 独立进程可能拿到一个尚未建表的库文件；initialize 幂等。
    initialize(db_path)
    conn = connect(db_path)
    try:
        return dispatch(conn, request)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单请求工作进程")
    parser.add_argument("--db", required=True)
    parser.add_argument("--request-json", required=True)
    args = parser.parse_args(argv)
    request = json.loads(args.request_json)
    try:
        receipt = run_one(args.db, request)
    except CrashBeforeCommit:
        # 模拟真实 kill：事务随连接关闭而回滚，无收据返回
        return EXIT_CRASH_BEFORE_COMMIT
    except CrashAfterCommit:
        # 事务已提交，但调用方永远收不到这张收据
        return EXIT_CRASH_AFTER_COMMIT
    sys.stdout.write(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
