"""事务化请求引擎：全部端点规则都在单个 SQLite 事务内完成。

关键不变量：

1. 一个分量内同一提供方至多一个身份——新增边在写入前先做模拟合并冲突检查，
   冲突则整笔拒绝，不写入任何边/主体/会话变化（“不得留下半个新主体”）；
2. 主体公开编号 = 分量内最小身份 ID；任何成员集合变化或成员身份代数变化
   都从租户单调序列分配新主体代数，旧代数永不复用、永不复活；
3. 会话担保“确切已确认身份集合 + 主体编号 + 当时主体代数”，
   受影响事务内逐条失效，且只失效不复活；
4. 已清除边的重复旧请求按请求编号重放历史收据，不重新执行副作用。
"""

from __future__ import annotations

import json
import sqlite3

from . import graph as graphmod
from .confirmations import verify_confirmation
from .errors import ErrorCode
from .store import maybe_crash


class Reject(Exception):
    """可预期的业务拒绝：携带稳定错误码，仍会写持久收据。"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------- 快照读取

def _load_identities(conn: sqlite3.Connection, tenant: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT identity_id, provider FROM identities WHERE tenant_id=?",
        (tenant,)).fetchall()
    return {r["identity_id"]: r["provider"] for r in rows}


def _load_identity_gens(conn: sqlite3.Connection, tenant: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT identity_id, id_gen FROM identities WHERE tenant_id=?",
        (tenant,)).fetchall()
    return {r["identity_id"]: r["id_gen"] for r in rows}


def _load_active_edges(conn: sqlite3.Connection, tenant: str,
                       now: int) -> list[dict]:
    """活动边 = state='active' 且在当前时钟下处于有效时间窗。"""
    rows = conn.execute(
        "SELECT * FROM edges WHERE tenant_id=? AND state='active'",
        (tenant,)).fetchall()
    out = []
    for r in rows:
        if r["not_before"] <= now <= r["not_after"]:
            out.append(dict(r))
    return out


def _load_subjects(conn: sqlite3.Connection, tenant: str) -> dict[str, dict]:
    rows = conn.execute(
        "SELECT subject_no, subject_gen, members FROM subjects WHERE tenant_id=?",
        (tenant,)).fetchall()
    return {r["subject_no"]: {"subject_gen": r["subject_gen"],
                              "members": set(json.loads(r["members"]))}
            for r in rows}


def _load_sessions(conn: sqlite3.Connection, tenant: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sessions WHERE tenant_id=?", (tenant,)).fetchall()


# ---------------------------------------------------------- 分量重建与代数

def _next_gen_seq(conn: sqlite3.Connection, tenant: str) -> int:
    row = conn.execute(
        "SELECT gen_seq FROM tenants WHERE tenant_id=?", (tenant,)).fetchone()
    nxt = row["gen_seq"] + 1
    conn.execute("UPDATE tenants SET gen_seq=? WHERE tenant_id=?", (nxt, tenant))
    return nxt


def rebuild_subjects(conn: sqlite3.Connection, tenant: str, now: int,
                     changed_members: set[str] | None = None) -> dict[str, Any]:
    """按当前活动边重建全租户主体视图并分配代数。

    代数规则：
      * 新编号（合并产生或拆分后新分量）-> 分配新代数；
      * 编号仍在且成员集合不变，且分量内没有身份在本请求发生代数变化
        -> 保留旧代数（冗余边、不拆图的撤回走这里）；
      * 其余情况（成员集合变化，或成员身份被续发）-> 分配新代数。

    返回摘要：``{"subjects": {编号: {"gen", "members"}}, "events": [...]}``。
    """
    identities = _load_identities(conn, tenant)
    id_gens = _load_identity_gens(conn, tenant)
    edges = _load_active_edges(conn, tenant, now)
    edge_pairs = [(e["id_a"], e["id_b"]) for e in edges]
    components = graphmod.connected_components(identities, edge_pairs)
    old = _load_subjects(conn, tenant)
    changed_members = changed_members or set()

    new_view: dict[str, dict] = {}
    events: list[dict] = []
    old_nos = set(old)
    new_nos = set()
    for comp in components:
        no = min(comp)
        new_nos.add(no)
        prior = old.get(no)
        if prior is None:
            gen = _next_gen_seq(conn, tenant)
            events.append({"event": "subject_created", "subject_no": no,
                           "subject_gen": gen, "members": sorted(comp)})
        elif prior["members"] != comp or (comp & changed_members):
            gen = _next_gen_seq(conn, tenant)
            events.append({"event": "subject_gen_advanced", "subject_no": no,
                           "old_gen": prior["subject_gen"], "subject_gen": gen,
                           "members": sorted(comp)})
        else:
            gen = prior["subject_gen"]
        new_view[no] = {"gen": gen, "members": comp}

    for gone in sorted(old_nos - new_nos):
        events.append({"event": "subject_number_retired", "subject_no": gone,
                       "last_gen": old[gone]["subject_gen"]})

    # 全量替换主体表（单事务内，外部在提交前看不到中间态）
    conn.execute("DELETE FROM subjects WHERE tenant_id=?", (tenant,))
    for no, info in sorted(new_view.items()):
        conn.execute(
            "INSERT INTO subjects(tenant_id, subject_no, subject_gen, members)"
            " VALUES(?,?,?,?)",
            (tenant, no, info["gen"], json.dumps(sorted(info["members"]))))
    return {"subjects": new_view, "events": events,
            "id_gens": id_gens}


def invalidate_sessions(conn: sqlite3.Connection, tenant: str,
                        view: dict[str, dict]) -> list[dict]:
    """按新主体视图失效所有不再被担保的会话。

    失效条件（满足其一）：编号消失、代数落后、担保成员集合与当前分量不一致。
    已失效会话保持失效，绝不复活。
    """
    invalidated: list[dict] = []
    for row in _load_sessions(conn, tenant):
        if not row["valid"]:
            continue
        no, gen = row["subject_no"], row["subject_gen"]
        members = set(json.loads(row["members"]))
        reason = None
        if no not in view:
            reason = "SUBJECT_NUMBER_GONE"
        elif view[no]["gen"] != gen:
            reason = "SUBJECT_GEN_ADVANCED"
        elif view[no]["members"] != members:
            reason = "GUARANTEED_MEMBERS_CHANGED"
        if reason:
            conn.execute(
                "UPDATE sessions SET valid=0, invalid_reason=? WHERE tenant_id=?"
                " AND session_id=?",
                (reason, tenant, row["session_id"]))
            invalidated.append({"session_id": row["session_id"],
                                "subject_no": no, "reason": reason})
    return invalidated


# ---------------------------------------------------------------- 收据工具

def _snapshot(conn: sqlite3.Connection, tenant: str, now: int) -> dict:
    """读取租户当前完整视图，用于收据与 result.json。"""
    subjects = _load_subjects(conn, tenant)
    subj_out = {}
    for no, info in sorted(subjects.items()):
        subj_out[no] = {"subject_gen": info["subject_gen"],
                        "members": sorted(info["members"])}
    sessions = []
    for r in _load_sessions(conn, tenant):
        sessions.append({"session_id": r["session_id"],
                         "subject_no": r["subject_no"],
                         "subject_gen": r["subject_gen"],
                         "members": json.loads(r["members"]),
                         "valid": bool(r["valid"]),
                         "invalid_reason": r["invalid_reason"]})
    edges = []
    for r in conn.execute("SELECT * FROM edges WHERE tenant_id=? ORDER BY edge_id",
                          (tenant,)).fetchall():
        edges.append({"edge_id": r["edge_id"], "id_a": r["id_a"],
                      "id_b": r["id_b"], "state": r["state"],
                      "gen_a": r["gen_a"], "gen_b": r["gen_b"],
                      "not_before": r["not_before"], "not_after": r["not_after"]})
    id_gens = _load_identity_gens(conn, tenant)
    active_edges = [(e["id_a"], e["id_b"]) for e in
                    _load_active_edges(conn, tenant, now)]
    comps = graphmod.connected_components(_load_identities(conn, tenant),
                                          active_edges)
    return {"clock": now, "identity_gens": dict(sorted(id_gens.items())),
            "subjects": subj_out,
            "components": [sorted(c) for c in comps],
            "sessions": sorted(sessions, key=lambda s: s["session_id"]),
            "edges": edges}


def _save_receipt(conn: sqlite3.Connection, tenant: str, request_id: str,
                  status: str, receipt: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO receipts(tenant_id, request_id, status, payload)"
        " VALUES(?,?,?,?)",
        (tenant, request_id, status, json.dumps(receipt, ensure_ascii=False)))


# ---------------------------------------------------------------- 各端点

def _get_identity(conn: sqlite3.Connection, tenant: str, ident: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM identities WHERE tenant_id=? AND identity_id=?",
        (tenant, ident)).fetchone()
    if row is None:
        raise Reject(ErrorCode.UNKNOWN_IDENTITY)
    return row


def _handle_register_tenant(conn: sqlite3.Connection, req: dict) -> dict:
    if conn.execute("SELECT 1 FROM tenants WHERE tenant_id=?",
                    (req["tenant"],)).fetchone():
        raise Reject(ErrorCode.TENANT_EXISTS)
    conn.execute("INSERT INTO tenants(tenant_id, clock, gen_seq) VALUES(?,0,0)",
                 (req["tenant"],))
    return {"tenant": req["tenant"]}


def _handle_register_identity(conn: sqlite3.Connection, req: dict,
                              now: int) -> dict:
    tenant, ident = req["tenant"], req["identity_id"]
    provider = req["provider"]
    if conn.execute(
            "SELECT 1 FROM identities WHERE tenant_id=? AND identity_id=?",
            (tenant, ident)).fetchone():
        raise Reject(ErrorCode.IDENTITY_EXISTS)
    conn.execute(
        "INSERT INTO identities(tenant_id, identity_id, provider, id_gen)"
        " VALUES(?,?,?,1)", (tenant, ident, provider))
    # 新身份先以单点分量存在，立即获得首个主体代数
    rebuilt = rebuild_subjects(conn, tenant, now)
    invalidate_sessions(conn, tenant, rebuilt["subjects"])
    return {"identity_id": ident, "provider": provider, "identity_gen": 1,
            "subject_no": ident}


def _validate_pair_confirmations(conn: sqlite3.Connection, req: dict,
                                 row_a: sqlite3.Row, row_b: sqlite3.Row,
                                 now: int) -> None:
    confs = req.get("confirmations")
    if not isinstance(confs, list) or len(confs) != 2:
        raise Reject(ErrorCode.BAD_CONFIRMATION)
    # 前置只核身份与提供方；代数是否过期交给 verify_confirmation 判 STALE，
    # 否则旧代数确认会在签名校验之前被误归为 BAD_CONFIRMATION。
    wanted = {(row_a["identity_id"], row_a["provider"]),
              (row_b["identity_id"], row_b["provider"])}
    seen = set()
    for c in confs:
        if not isinstance(c, dict):
            raise Reject(ErrorCode.BAD_CONFIRMATION)
        ident, prov, gen = c.get("identity"), c.get("provider"), c.get("identity_gen")
        key = (ident, prov)
        if key not in wanted or key in seen:
            raise Reject(ErrorCode.BAD_CONFIRMATION)
        seen.add(key)
        peer = (row_b["identity_id"] if ident == row_a["identity_id"]
                else row_a["identity_id"])
        # 按身份当前代数重验：确认内嵌旧代数时返回 STALE_CONFIRMATION
        current_gen = (row_a["id_gen"] if ident == row_a["identity_id"]
                       else row_b["id_gen"])
        code = verify_confirmation(
            c, tenant=req["tenant"], identity=ident, peer=peer,
            request_id=req["request_id"], provider=prov,
            identity_gen=current_gen, now=now)
        if code:
            raise Reject(code)


def _handle_associate(conn: sqlite3.Connection, req: dict, now: int) -> dict:
    tenant = req["tenant"]
    id_a, id_b = sorted((req["identity_a"], req["identity_b"]))
    if id_a == id_b:
        raise Reject(ErrorCode.BAD_CONFIRMATION)
    row_a = _get_identity(conn, tenant, id_a)
    row_b = _get_identity(conn, tenant, id_b)
    _validate_pair_confirmations(conn, req, row_a, row_b, now)

    # 已有活动边：幂等拒绝，避免平行重边
    dup = conn.execute(
        "SELECT edge_id, state FROM edges WHERE tenant_id=? AND id_a=? AND id_b=?",
        (tenant, id_a, id_b)).fetchall()
    if any(r["state"] == "active" for r in dup):
        raise Reject(ErrorCode.DUPLICATE_ACTIVE_EDGE)

    # 模拟合并：两端已在同一分量则结构不变；否则检查合并后提供方冲突
    identities = _load_identities(conn, tenant)
    active = _load_active_edges(conn, tenant, now)
    comps = graphmod.connected_components(
        identities, [(e["id_a"], e["id_b"]) for e in active])
    comp_a = next(c for c in comps if id_a in c)
    comp_b = next(c for c in comps if id_b in c)
    if comp_a is not comp_b:
        conflict_provider = graphmod.check_provider_conflict(
            identities, comp_a | comp_b)
        if conflict_provider is not None:
            raise Reject(ErrorCode.PROVIDER_CONFLICT)

    edge_id = req["request_id"]
    conn.execute(
        "INSERT INTO edges(tenant_id, edge_id, id_a, id_b, state, gen_a, gen_b,"
        " not_before, not_after, request_id) VALUES(?,?,?,?,'active',?,?,?,?,?)",
        (tenant, edge_id, id_a, id_b, row_a["id_gen"], row_b["id_gen"],
         req["not_before"], req["not_after"], req["request_id"]))

    rebuilt = rebuild_subjects(conn, tenant, now)
    invalidated = invalidate_sessions(conn, tenant, rebuilt["subjects"])
    return {"edge_id": edge_id, "id_a": id_a, "id_b": id_b,
            "subject_events": rebuilt["events"],
            "invalidated_sessions": invalidated}


def _handle_revoke(conn: sqlite3.Connection, req: dict, now: int) -> dict:
    tenant = req["tenant"]
    edge = conn.execute(
        "SELECT * FROM edges WHERE tenant_id=? AND edge_id=?",
        (tenant, req["edge_id"])).fetchone()
    if edge is None:
        raise Reject(ErrorCode.EDGE_NOT_FOUND)
    if edge["state"] == "revoked":
        raise Reject(ErrorCode.EDGE_REVOKED)
    conn.execute("UPDATE edges SET state='revoked' WHERE tenant_id=? AND edge_id=?",
                 (tenant, req["edge_id"]))
    rebuilt = rebuild_subjects(conn, tenant, now)
    invalidated = invalidate_sessions(conn, tenant, rebuilt["subjects"])
    return {"edge_id": req["edge_id"], "state": "revoked",
            "subject_events": rebuilt["events"],
            "invalidated_sessions": invalidated}


def _handle_reissue(conn: sqlite3.Connection, req: dict, now: int) -> dict:
    """身份续发：身份代数 +1，其全部活动边转为 stale（旧确认绑定旧代数）。"""
    tenant, ident = req["tenant"], req["identity_id"]
    row = _get_identity(conn, tenant, ident)
    new_gen = row["id_gen"] + 1
    conn.execute(
        "UPDATE identities SET id_gen=? WHERE tenant_id=? AND identity_id=?",
        (new_gen, tenant, ident))
    touched = conn.execute(
        "UPDATE edges SET state='stale' WHERE tenant_id=? AND state='active'"
        " AND (id_a=? OR id_b=?)",
        (tenant, ident, ident)).rowcount
    rebuilt = rebuild_subjects(conn, tenant, now, changed_members={ident})
    invalidated = invalidate_sessions(conn, tenant, rebuilt["subjects"])
    return {"identity_id": ident, "identity_gen": new_gen,
            "edges_marked_stale": touched,
            "subject_events": rebuilt["events"],
            "invalidated_sessions": invalidated}


def _handle_renew_edge(conn: sqlite3.Connection, req: dict, now: int) -> dict:
    """边续期：仅 stale 边可续，需双方按当前代数重新确认，产生新活动边。"""
    tenant = req["tenant"]
    old = conn.execute(
        "SELECT * FROM edges WHERE tenant_id=? AND edge_id=?",
        (tenant, req["edge_id"])).fetchone()
    if old is None:
        raise Reject(ErrorCode.EDGE_NOT_FOUND)
    if old["state"] == "revoked":
        raise Reject(ErrorCode.EDGE_REVOKED)
    if old["state"] == "active":
        raise Reject(ErrorCode.EDGE_NOT_RENEWABLE)
    id_a, id_b = old["id_a"], old["id_b"]
    row_a = _get_identity(conn, tenant, id_a)
    row_b = _get_identity(conn, tenant, id_b)
    _validate_pair_confirmations(conn, req, row_a, row_b, now)
    new_edge_id = req["request_id"]
    conn.execute(
        "INSERT INTO edges(tenant_id, edge_id, id_a, id_b, state, gen_a, gen_b,"
        " not_before, not_after, request_id) VALUES(?,?,?,?,'active',?,?,?,?,?)",
        (tenant, new_edge_id, id_a, id_b, row_a["id_gen"], row_b["id_gen"],
         req["not_before"], req["not_after"], req["request_id"]))
    rebuilt = rebuild_subjects(conn, tenant, now)
    invalidated = invalidate_sessions(conn, tenant, rebuilt["subjects"])
    return {"edge_id": new_edge_id, "renewed_from": req["edge_id"],
            "id_a": id_a, "id_b": id_b,
            "subject_events": rebuilt["events"],
            "invalidated_sessions": invalidated}


def _handle_login(conn: sqlite3.Connection, req: dict, now: int) -> dict:
    tenant, ident = req["tenant"], req["identity_id"]
    _get_identity(conn, tenant, ident)
    # 登录同样基于最新分量视图（含到期判定）：先重建反映时间推移，
    # 再按新视图失效旧会话，最后才创建本次会话——顺序不能反。
    rebuilt = rebuild_subjects(conn, tenant, now)
    view = rebuilt["subjects"]
    no = next((n for n, info in view.items() if ident in info["members"]), None)
    if no is None:
        raise Reject(ErrorCode.NO_SUBJECT)
    invalidate_sessions(conn, tenant, view)
    info = view[no]
    session_id = req["request_id"]
    members = sorted(info["members"])
    conn.execute(
        "INSERT INTO sessions(tenant_id, session_id, subject_no, subject_gen,"
        " members, valid, invalid_reason) VALUES(?,?,?,?,?,1,NULL)",
        (tenant, session_id, no, info["gen"], json.dumps(members)))
    return {"session_id": session_id, "subject_no": no,
            "subject_gen": info["gen"], "members": members}


_HANDLERS = {
    "register_tenant": lambda c, r, now: _handle_register_tenant(c, r),
    "register_identity": _handle_register_identity,
    "associate": _handle_associate,
    "revoke": _handle_revoke,
    "reissue": _handle_reissue,
    "renew_edge": _handle_renew_edge,
    "login": _handle_login,
}


def dispatch(conn: sqlite3.Connection, req: dict) -> dict:
    """在一个 BEGIN IMMEDIATE 事务内处理单个请求，返回持久收据。

    处理顺序：时钟校验 -> 幂等重放 -> 端点校验/变更 -> 分量/会话维护
    -> 崩溃注入 -> 提交 -> 提交后崩溃注入。拒绝与成功都写收据。
    """
    tenant = req["tenant"]
    request_id = req["request_id"]
    op = req["op"]
    conn.execute("BEGIN IMMEDIATE")
    now = int(req["tick"])
    try:
        tenant_row = conn.execute(
            "SELECT * FROM tenants WHERE tenant_id=?", (tenant,)).fetchone()
        if tenant_row is None:
            if op == "register_tenant":
                # 建租户是唯一允许在未知租户上执行的操作
                result = _handle_register_tenant(conn, req)
                conn.execute("UPDATE tenants SET clock=? WHERE tenant_id=?",
                             (now, tenant))
                receipt = {"request_id": request_id, "tenant": tenant,
                           "op": op, "tick": now, "status": ErrorCode.OK,
                           "result": result, "replayed": False,
                           "snapshot": _snapshot(conn, tenant, now)}
                _save_receipt(conn, tenant, request_id, ErrorCode.OK, receipt)
                maybe_crash(req.get("crash"), "before_commit")
                conn.execute("COMMIT")
                maybe_crash(req.get("crash"), "after_commit")
                return receipt
            raise Reject(ErrorCode.UNKNOWN_TENANT)

        # 幂等优先于时钟检查：已清除边的重复旧请求（tick 必然落后）
        # 只能重放历史回执，不重新执行任何副作用，也不推进时钟。
        prior = conn.execute(
            "SELECT payload FROM receipts WHERE tenant_id=? AND request_id=?",
            (tenant, request_id)).fetchone()
        if prior is not None:
            conn.execute("COMMIT")
            receipt = json.loads(prior["payload"])
            receipt["replayed"] = True
            return receipt

        if now < tenant_row["clock"]:
            raise Reject(ErrorCode.CLOCK_NOT_MONOTONIC)

        if op not in _HANDLERS:
            raise Reject(ErrorCode.BAD_CONFIRMATION)
        result = _HANDLERS[op](conn, req, now)

        # 时钟只在请求被真正接受（非重放、非拒绝）后推进
        conn.execute("UPDATE tenants SET clock=? WHERE tenant_id=?",
                     (now, tenant))
        receipt = {"request_id": request_id, "tenant": tenant, "op": op,
                   "tick": now, "status": ErrorCode.OK, "result": result,
                   "replayed": False,
                   "snapshot": _snapshot(conn, tenant, now)}
        _save_receipt(conn, tenant, request_id, ErrorCode.OK, receipt)

        maybe_crash(req.get("crash"), "before_commit")
        conn.execute("COMMIT")
    except Reject as exc:
        conn.execute("ROLLBACK")
        # 拒绝单独开事务写收据：任何主体/边/会话变化都已随回滚撤销，
        # 因此拒绝路径不可能留下半个新主体。
        conn.execute("BEGIN IMMEDIATE")
        # 回滚后时钟行恢复；再次确认租户存在（register_tenant 的重复场景除外）
        row = conn.execute("SELECT clock FROM tenants WHERE tenant_id=?",
                           (tenant,)).fetchone()
        cur_clock = row["clock"] if row else 0
        if now > cur_clock:
            conn.execute("UPDATE tenants SET clock=? WHERE tenant_id=?",
                         (now, tenant))
        receipt = {"request_id": request_id, "tenant": tenant, "op": op,
                   "tick": now, "status": exc.code, "result": None,
                   "replayed": False,
                   "snapshot": _snapshot(conn, tenant, max(now, cur_clock))
                   if row else None}
        _save_receipt(conn, tenant, request_id, exc.code, receipt)
        conn.execute("COMMIT")
        return receipt
    except Exception:
        conn.execute("ROLLBACK")
        raise

    maybe_crash(req.get("crash"), "after_commit")
    return receipt


def read_snapshot(conn: sqlite3.Connection, tenant: str) -> dict:
    """只读视图：按租户当前时钟返回分量/主体/会话/边状态。"""
    row = conn.execute("SELECT clock FROM tenants WHERE tenant_id=?",
                       (tenant,)).fetchone()
    if row is None:
        raise Reject(ErrorCode.UNKNOWN_TENANT)
    return _snapshot(conn, tenant, row["clock"])
