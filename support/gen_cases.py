"""生成可重放 cases.json：全部输入离线合成，确认用生产签名 API 签发。

场景设计（与 README“场景清单”对应）：

S1 基础合并/拆分与主体代数     S5 到期与新会话争用 + 重放旧请求
S2 两个合法合并共同形成冲突   S6 崩溃：未提交 / 已提交未回执
S3 撤回桥接边与备选路径并存   S7 并发：确认/续发/撤回争用
S4 身份续发与边续期代数链     S8 多租户同编号隔离
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solution.confirmations import sign_confirmation  # noqa: E402


def _conf(tenant: str, ident: str, provider: str, gen: int, peer: str,
          rid: str, nb: int, na: int) -> dict:
    return sign_confirmation(tenant, provider, ident, gen, peer, rid, nb, na)


def _pair(tenant: str, a: str, pa: str, ga: int, b: str, pb: str, gb: int,
          rid: str, nb: int, na: int) -> list[dict]:
    return [_conf(tenant, a, pa, ga, b, rid, nb, na),
            _conf(tenant, b, pb, gb, a, rid, nb, na)]


def _pair_with_stale(tenant: str, a: str, pa: str, signed_ga: int,
                     current_ga: int, b: str, pb: str, gb: int,
                     rid: str, nb: int, na: int) -> list[dict]:
    """A 端确认按旧代数 signed_ga 自洽签发；引擎按 current_ga 重验 -> STALE。"""
    return [_conf(tenant, a, pa, signed_ga, b, rid, nb, na),
            _conf(tenant, b, pb, gb, a, rid, nb, na)]


def _base(tenant: str) -> list[dict]:
    return [
        {"op": "register_tenant", "tenant": tenant, "request_id": f"{tenant}-r00",
         "tick": 1},
    ]


def _ident(tenant: str, ident: str, provider: str, tick: int) -> dict:
    return {"op": "register_identity", "tenant": tenant,
            "request_id": f"{tenant}-reg-{ident}", "tick": tick,
            "identity_id": ident, "provider": provider}


def _assoc(tenant: str, rid: str, tick: int, a: str, pa: str, ga: int,
           b: str, pb: str, gb: int, nb: int = 1, na: int = 1000,
           crash: str | None = None) -> dict:
    req = {"op": "associate", "tenant": tenant, "request_id": rid, "tick": tick,
           "identity_a": a, "identity_b": b, "not_before": nb, "not_after": na,
           "confirmations": _pair(tenant, a, pa, ga, b, pb, gb, rid, nb, na)}
    if crash:
        req["crash"] = crash
    return req


def scenario_1() -> dict:
    t = "tA"
    reqs = _base(t)
    # 四个身份：p1/i1, p2/i2, p3/i3, p4/i4；编号最小为 i1
    for i, p in enumerate(("p1", "p2", "p3", "p4"), start=1):
        reqs.append(_ident(t, f"i{i}", p, 10 + i))
    # 边 i1-i2、i2-i3 合并为同一主体 i1
    reqs += [
        _assoc(t, f"{t}-e12", 20, "i1", "p1", 1, "i2", "p2", 1),
        _assoc(t, f"{t}-e23", 21, "i2", "p2", 1, "i3", "p3", 1),
    ]
    # 登录担保 {i1,i2,i3} 代数记为 g3
    reqs.append({"op": "login", "tenant": t, "request_id": f"{t}-login-1",
                 "tick": 22, "identity_id": "i2"})
    # 接上 i4（仍合法：四个不同提供方）
    reqs.append(_assoc(t, f"{t}-e34", 23, "i3", "p3", 1, "i4", "p4", 1))
    # 撤回桥 i2-i3：若没有备选路径则拆成 {i1,i2} 与 {i3,i4}
    reqs.append({"op": "revoke", "tenant": t, "request_id": f"{t}-rev-e23",
                 "tick": 24, "edge_id": f"{t}-e23"})
    # 旧会话此刻必须失效
    # 再建桥 i1-i3，两个分量重新合并回主体 i1（新代数，旧会话不复活）
    reqs.append(_assoc(t, f"{t}-e13", 25, "i1", "p1", 1, "i3", "p3", 1))
    reqs.append({"op": "login", "tenant": t, "request_id": f"{t}-login-2",
                 "tick": 26, "identity_id": "i4"})
    return {"id": "S1", "name": "merge_split_remerge_generations",
            "description": "合并、桥接撤回拆分、重新合并的主体代数单调演进与会话失效",
            "requests": reqs,
            "expected": {
                "final_components": [["i1", "i2", "i3", "i4"]],
                "final_subject_no": "i1",
                "login1_valid_after_revoke": False,
                "login1_invalid_reason": "SUBJECT_GEN_ADVANCED",
                "login2_valid": True,
                "rejected_statuses": {},
            }}


def scenario_2() -> dict:
    t = "tB"
    reqs = _base(t)
    # i1=p1, i2=p2, i3=p1（同提供方 p1 的第二个身份，单独看都合法）
    reqs += [_ident(t, "i1", "p1", 10), _ident(t, "i2", "p2", 11),
             _ident(t, "i3", "p1", 12)]
    # 第一次合并 i1-i2：合法
    reqs.append(_assoc(t, f"{t}-e12", 20, "i1", "p1", 1, "i2", "p2", 1))
    # 第二次合并 i2-i3：单看也合法（i2 是 p2，i3 是 p1）
    reqs.append(_assoc(t, f"{t}-e23", 21, "i2", "p2", 1, "i3", "p1", 1))
    # 但两个合并共同让 i1 与 i3（同为 p1）落入同一分量 -> 拒绝，不留半个主体
    # 拒绝后 i1-i2 边仍在，i3 保持单点；再登录确认结构未被污染
    reqs.append({"op": "login", "tenant": t, "request_id": f"{t}-login-i3",
                 "tick": 22, "identity_id": "i3"})
    return {"id": "S2", "name": "two_legal_merges_form_provider_conflict",
            "description": "两条边各自合法但共同形成同提供方冲突，整笔拒绝且无半成品",
            "requests": reqs,
            "expected": {
                "conflict_request": f"{t}-e23",
                "conflict_status": "PROVIDER_CONFLICT",
                "components_after": [["i1", "i2"], ["i3"]],
                "subject_count": 2,
                "rejected_edge_absent": f"{t}-e23",
                "login_i3_subject_no": "i3",
            }}


def scenario_3() -> dict:
    t = "tC"
    reqs = _base(t)
    for i, p in enumerate(("p1", "p2", "p3", "p4"), start=1):
        reqs.append(_ident(t, f"i{i}", p, 10 + i))
    # 环：i1-i2, i2-i3, i3-i4, i4-i1（撤回桥 i2-i3 后仍有备选路径 i1-i4-i3）
    reqs += [
        _assoc(t, f"{t}-e12", 20, "i1", "p1", 1, "i2", "p2", 1),
        _assoc(t, f"{t}-e23", 21, "i2", "p2", 1, "i3", "p3", 1),
        _assoc(t, f"{t}-e34", 22, "i3", "p3", 1, "i4", "p4", 1),
        _assoc(t, f"{t}-e41", 23, "i4", "p4", 1, "i1", "p1", 1),
    ]
    reqs.append({"op": "login", "tenant": t, "request_id": f"{t}-login-ring",
                 "tick": 24, "identity_id": "i1"})
    # 撤回“桥” i2-i3：环上仍连通，分量不变，主体代数不变，会话继续有效
    reqs.append({"op": "revoke", "tenant": t, "request_id": f"{t}-rev-e23",
                 "tick": 25, "edge_id": f"{t}-e23"})
    # 再撤回 i4-i1：此时真正拆成 {i1,i2} 与 {i3,i4}
    reqs.append({"op": "revoke", "tenant": t, "request_id": f"{t}-rev-e41",
                 "tick": 26, "edge_id": f"{t}-e41"})
    return {"id": "S3", "name": "bridge_revoke_with_alternate_path",
            "description": "撤回桥接边但备选路径维持连通，二次撤回才真正拆分",
            "requests": reqs,
            "expected": {
                "components_after_first_revoke": [["i1", "i2", "i3", "i4"]],
                "ring_login_valid_after_first_revoke": True,
                "components_after_second_revoke": [["i1", "i2"], ["i3", "i4"]],
                "ring_login_valid_after_second_revoke": False,
            }}


def scenario_4() -> dict:
    t = "tD"
    reqs = _base(t)
    reqs += [_ident(t, "i1", "p1", 10), _ident(t, "i2", "p2", 11)]
    reqs.append(_assoc(t, f"{t}-e12", 20, "i1", "p1", 1, "i2", "p2", 1,
                       nb=1, na=100))
    reqs.append({"op": "login", "tenant": t, "request_id": f"{t}-login-1",
                 "tick": 21, "identity_id": "i1"})
    # 续发 i1：代数 1->2，边转 stale，旧会话失效，分量拆为两个单点
    reqs.append({"op": "reissue", "tenant": t, "request_id": f"{t}-reissue-i1",
                 "tick": 22, "identity_id": "i1"})
    # 用旧代数确认续期 stale 边 -> STALE_CONFIRMATION。
    # A 端确认在旧代数 1 下自洽签名（绑定关系全部正确），
    # 引擎按当前代数 2 重验 identity_gen 字段时拒绝。
    bad = {"op": "renew_edge", "tenant": t, "request_id": f"{t}-renew-bad",
           "tick": 23, "edge_id": f"{t}-e12",
           "not_before": 1, "not_after": 1000,
           "confirmations": _pair_with_stale(
               t, "i1", "p1", signed_ga=1, current_ga=2,
               b="i2", pb="p2", gb=1, rid=f"{t}-renew-bad", nb=1, na=1000)}
    reqs.append(bad)
    # 用当前代数重新确认续期 -> 成功，重新合并并产生新主体代数
    good = _assoc(t, f"{t}-renew-ok", 24, "i1", "p1", 2, "i2", "p2", 1)
    good["edge_id"] = f"{t}-e12"
    good["op"] = "renew_edge"
    reqs.append(good)
    reqs.append({"op": "login", "tenant": t, "request_id": f"{t}-login-2",
                 "tick": 25, "identity_id": "i1"})
    return {"id": "S4", "name": "reissue_and_renewal_generation_chain",
            "description": "身份续发使旧确认失效、边转 stale，按当代数重新确认后续期",
            "requests": reqs,
            "expected": {
                "i1_final_gen": 2,
                "bad_renew_status": "STALE_CONFIRMATION",
                "good_renew_status": "OK",
                "final_components": [["i1", "i2"]],
                "login1_valid": False,
                "login2_valid": True,
            }}


def scenario_5() -> dict:
    t = "tE"
    reqs = _base(t)
    reqs += [_ident(t, "i1", "p1", 10), _ident(t, "i2", "p2", 11)]
    # 有效期只到 tick 30
    reqs.append(_assoc(t, f"{t}-e12", 20, "i1", "p1", 1, "i2", "p2", 1,
                       nb=10, na=30))
    reqs.append({"op": "login", "tenant": t, "request_id": f"{t}-login-before",
                 "tick": 25, "identity_id": "i1"})
    # tick 31：边到期，分量在重建视角下拆开；新登录 i2 只能担保单点
    reqs.append({"op": "login", "tenant": t, "request_id": f"{t}-login-after",
                 "tick": 31, "identity_id": "i2"})
    # 旧请求重放（tick 20 的关联请求，编号相同）：只回历史收据
    replay = _assoc(t, f"{t}-e12", 20, "i1", "p1", 1, "i2", "p2", 1,
                    nb=10, na=30)
    reqs.append(replay)
    # 迟到的重复登录请求同编号重放
    reqs.append({"op": "login", "tenant": t,
                 "request_id": f"{t}-login-before", "tick": 40,
                 "identity_id": "i1"})
    return {"id": "S5", "name": "expiry_races_new_session_and_replay",
            "description": "边到期导致分量拆分，新旧登录争用，旧请求只重放历史回执",
            "requests": reqs,
            "expected": {
                "components_at_31": [["i1"], ["i2"]],
                "login_before_subject_gen_less_than_login_after": True,
                "replay_status": "OK",
                "replay_flag": True,
                "expired_edge_state": "active",
            }}


def scenario_6() -> dict:
    t = "tF"
    # 两组请求共用同一 DB，由测试分别在两个 DB 上执行崩溃与重放；
    # cases 里保留 crash 标记，runner 默认自动重试。
    reqs = _base(t)
    reqs += [_ident(t, "i1", "p1", 10), _ident(t, "i2", "p2", 11),
             _ident(t, "i3", "p3", 12)]
    reqs.append(_assoc(t, f"{t}-e12-bc", 20, "i1", "p1", 1, "i2", "p2", 1,
                       crash="before_commit"))
    reqs.append(_assoc(t, f"{t}-e23-ac", 21, "i2", "p2", 1, "i3", "p3", 1,
                       crash="after_commit"))
    return {"id": "S6", "name": "process_crash_before_and_after_commit",
            "description": "独立进程在未提交与已提交未回执两处终止，重启按 ID 重放",
            "requests": reqs,
            "expected": {
                "before_commit_request": f"{t}-e12-bc",
                "after_commit_request": f"{t}-e23-ac",
                "after_replay_components": [["i1", "i2", "i3"]],
                "before_replay_observed": "before_commit",
                "after_replay_observed": "after_commit",
            }}


def scenario_7() -> dict:
    t = "tG"
    reqs = _base(t)
    reqs += [_ident(t, "i1", "p1", 10), _ident(t, "i2", "p2", 11),
             _ident(t, "i3", "p3", 12)]
    a = _assoc(t, f"{t}-e12", 20, "i1", "p1", 1, "i2", "p2", 1)
    b = {"op": "reissue", "tenant": t, "request_id": f"{t}-reissue-i2",
         "tick": 21, "identity_id": "i2"}
    c = {"op": "revoke", "tenant": t, "request_id": f"{t}-rev-e12",
         "tick": 22, "edge_id": f"{t}-e12"}
    d = _assoc(t, f"{t}-e23", 23, "i2", "p2", 2, "i3", "p3", 1)
    # 并发分组：测试把三组同时交给独立进程，SQLite 写锁串行化
    return {"id": "S7", "name": "concurrent_associate_reissue_revoke",
            "description": "确认、续发、撤回在独立进程中并发争用同一批边",
            "requests": reqs,
            "concurrent_groups": [[a], [b], [c, d]],
            "expected": {
                "all_receipts_persisted": True,
                "no_duplicate_active_edge": True,
                "receipt_count": 4,
            }}


def scenario_8() -> dict:
    # 两个租户使用完全相同的身份编号与请求编号，状态必须互相隔离
    reqs: list[dict] = []
    for t in ("tX", "tY"):
        reqs += _base(t)
        reqs += [_ident(t, "i1", "p1", 10), _ident(t, "i2", "p2", 11)]
        reqs.append(_assoc(t, "shared-rid-edge", 20, "i1", "p1", 1,
                           "i2", "p2", 1))
        reqs.append({"op": "login", "tenant": t,
                     "request_id": "shared-rid-login", "tick": 21,
                     "identity_id": "i1"})
    return {"id": "S8", "name": "tenant_isolation_same_numbers",
            "description": "不同租户同身份编号、同请求编号、同主体编号互不干扰",
            "requests": reqs,
            "expected": {
                "tenants": ["tX", "tY"],
                "subject_no_each": "i1",
                "receipt_rows_per_tenant": 4,
            }}


def build() -> dict:
    scenarios = [scenario_1(), scenario_2(), scenario_3(), scenario_4(),
                 scenario_5(), scenario_6(), scenario_7(), scenario_8()]
    return {"schema_version": 1,
            "description": "本地联邦身份目录离线合成输入；时间为租户内单调逻辑时钟",
            "scenarios": scenarios}


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "cases.json")
    doc = build()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    total = sum(len(s["requests"]) for s in doc["scenarios"])
    print(f"wrote {out}: {len(doc['scenarios'])} scenarios, {total} requests")
