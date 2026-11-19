"""提供方模拟确认（attestation）的签发与校验。

每条关联边需要两端提供方各自签发一份确认，语义为：

* 提供方为自己名下的某个身份（含身份当前代数）背书；
* 确认绑定对方身份、请求编号与有效时间窗；
* 用租户内提供方密钥做 HMAC-SHA256，离线环境下模拟真实签名体系。

确认结构（JSON 可序列化）::

    {
      "tenant": "t1", "provider": "p1",
      "identity": "t1/id-0001", "identity_gen": 1,
      "peer": "t1/id-0002", "request_id": "req-...",
      "not_before": 100, "not_after": 200,
      "alg": "HMAC-SHA256", "sig": "<hex>"
    }
"""

from __future__ import annotations

import hashlib
import hmac
import json

from .errors import ErrorCode

ALG = "HMAC-SHA256"

# 签名覆盖的字段顺序固定，避免字典序列化差异
_SIGNED_FIELDS = ("tenant", "provider", "identity", "identity_gen",
                  "peer", "request_id", "not_before", "not_after", "alg")


def provider_secret(tenant: str, provider: str) -> bytes:
    """确定性派生租户内提供方密钥：全部输入离线合成，无需外部密钥库。"""
    digest = hashlib.sha256(f"fed-dir://{tenant}/{provider}".encode("utf-8")).digest()
    return digest


def _signing_base(c: dict) -> bytes:
    payload = {k: c[k] for k in _SIGNED_FIELDS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def sign_confirmation(tenant: str, provider: str, identity: str, identity_gen: int,
                      peer: str, request_id: str, not_before: int,
                      not_after: int) -> dict:
    """由提供方签发一份确认。"""
    c = {
        "tenant": tenant,
        "provider": provider,
        "identity": identity,
        "identity_gen": identity_gen,
        "peer": peer,
        "request_id": request_id,
        "not_before": int(not_before),
        "not_after": int(not_after),
        "alg": ALG,
    }
    sig = hmac.new(provider_secret(tenant, provider), _signing_base(c),
                   hashlib.sha256).hexdigest()
    c["sig"] = sig
    return c


def verify_confirmation(c: dict, *, tenant: str, identity: str, peer: str,
                        request_id: str, provider: str, identity_gen: int,
                        now: int) -> str | None:
    """在事务当前时钟下重验一份确认。

    返回 None 表示通过；否则返回 :class:`ErrorCode` 中的拒绝码。
    所有分支都是纯函数，便于在同一 SQLite 事务内重放校验。
    """
    required = set(_SIGNED_FIELDS) | {"sig"}
    if not isinstance(c, dict) or not required.issubset(c):
        return ErrorCode.BAD_CONFIRMATION
    try:
        sig = str(c["sig"])
        expected = hmac.new(provider_secret(tenant, provider),
                            _signing_base(c), hashlib.sha256).hexdigest()
    except (TypeError, ValueError):
        return ErrorCode.BAD_CONFIRMATION
    if not hmac.compare_digest(sig, expected):
        return ErrorCode.BAD_CONFIRMATION
    # 绑定关系必须与本次请求完全一致，防止确认被挪用到别的边/请求
    if (c["tenant"] != tenant or c["provider"] != provider
            or c["identity"] != identity or c["peer"] != peer
            or c["request_id"] != request_id):
        return ErrorCode.BAD_CONFIRMATION
    if int(c["identity_gen"]) != identity_gen:
        # 确认签发于身份的旧代数：续发后旧确认立即失效
        return ErrorCode.STALE_CONFIRMATION
    if now < int(c["not_before"]):
        return ErrorCode.CONFIRMATION_NOT_YET_VALID
    if now > int(c["not_after"]):
        return ErrorCode.CONFIRMATION_EXPIRED
    return None
