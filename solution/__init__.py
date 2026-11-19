"""本地联邦身份目录（local federated identity directory）。

仅依赖 Python 3.12 标准库与 SQLite（sqlite3 内嵌），离线运行。
模块划分：

* ``errors``       错误码与引擎异常
* ``confirmations`` 提供方模拟确认的签发与校验（HMAC-SHA256）
* ``graph``        由活动关联边求连通分量
* ``store``        SQLite 连接、DDL、崩溃注入
* ``engine``       全部端点的事务化处理逻辑
* ``worker``       独立进程入口（一次请求一个进程）
* ``runner``       进程内 / 子进程两种方式回放请求序列
* ``__main__``     命令行入口
"""

__all__ = ["errors", "confirmations", "graph", "store", "engine", "worker", "runner"]
__version__ = "1.0.0"
