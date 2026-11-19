"""稳定错误码。

所有拒绝都以收据（receipt）形式持久化，错误码字符串即对外契约，
README“错误码”一节与测试用例均引用这里的常量值。
"""

from __future__ import annotations


class ErrorCode:
    # 成功
    OK = "OK"

    # 租户 / 身份 / 提供方
    TENANT_EXISTS = "TENANT_EXISTS"
    UNKNOWN_TENANT = "UNKNOWN_TENANT"
    UNKNOWN_IDENTITY = "UNKNOWN_IDENTITY"
    UNKNOWN_PROVIDER = "UNKNOWN_PROVIDER"
    IDENTITY_EXISTS = "IDENTITY_EXISTS"
    TENANT_MISMATCH = "TENANT_MISMATCH"

    # 时钟
    CLOCK_NOT_MONOTONIC = "CLOCK_NOT_MONOTONIC"

    # 模拟确认
    BAD_CONFIRMATION = "BAD_CONFIRMATION"
    STALE_CONFIRMATION = "STALE_CONFIRMATION"
    CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"
    CONFIRMATION_NOT_YET_VALID = "CONFIRMATION_NOT_YET_VALID"

    # 关联边
    DUPLICATE_ACTIVE_EDGE = "DUPLICATE_ACTIVE_EDGE"
    PROVIDER_CONFLICT = "PROVIDER_CONFLICT"
    EDGE_NOT_FOUND = "EDGE_NOT_FOUND"
    EDGE_REVOKED = "EDGE_REVOKED"
    EDGE_NOT_ACTIVE = "EDGE_NOT_ACTIVE"
    EDGE_NOT_RENEWABLE = "EDGE_NOT_RENEWABLE"

    # 会话
    NO_SUBJECT = "NO_SUBJECT"


class EngineCrash(Exception):
    """测试用崩溃注入基类：模拟真实进程被杀死。"""


class CrashBeforeCommit(EngineCrash):
    """分量更新已在事务内完成但尚未 COMMIT，进程终止。"""


class CrashAfterCommit(EngineCrash):
    """事务已 COMMIT 但收据尚未回到调用方，进程终止。"""


# 独立进程崩溃时使用的退出码（避开常见 0/1/2）
EXIT_CRASH_BEFORE_COMMIT = 87
EXIT_CRASH_AFTER_COMMIT = 88
