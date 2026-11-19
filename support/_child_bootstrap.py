"""子进程引导：把工作区根注入 sys.path 后以包方式运行 worker。

``python -I`` 会忽略 PYTHONPATH 且不把 cwd 加入 sys.path，
因此独立进程无法直接 ``-m solution.worker``。本脚本由 runner 以文件路径调用，
先插入根目录再按包名导入，全程保持 -I 的其他隔离保护。
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from solution.worker import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
