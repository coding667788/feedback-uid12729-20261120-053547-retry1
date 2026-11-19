"""批处理工作进程：从标准输入读取一组请求，顺序处理后输出收据数组。

供 :func:`solution.runner.run_concurrent` 调用，每个并发分组对应一个独立 OS 进程。
"""

from __future__ import annotations

import argparse
import json
import sys

from .engine import dispatch
from .store import connect, initialize


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量请求工作进程")
    parser.add_argument("--db", required=True)
    args = parser.parse_args(argv)
    requests = json.loads(sys.stdin.read())
    initialize(args.db)
    conn = connect(args.db)
    receipts = []
    try:
        for req in requests:
            receipts.append(dispatch(conn, req))
    finally:
        conn.close()
    sys.stdout.write(json.dumps(receipts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
