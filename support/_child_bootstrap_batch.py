"""子进程批量引导：与 _child_bootstrap 同理，加载 worker_batch。"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from solution.worker_batch import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
