"""独立参考模型（oracle）：用与 solution 完全不同的代码结构重建同一套规则。

与生产引擎的区别：

* 纯内存字典 + 递归 DFS，不接触 SQLite，没有“事务”概念，
  每个请求要么完整 apply 要么在入口校验处整体拒绝；
* 主体代数用自己的单调计数器和“编号 -> 上次成员签名”表维护；
* HMAC 校验在此文件内独立实现，不 import solution 的任何代码。

测试用它逐请求对比生产引擎的状态码、主体编号/代数、会话有效性。
"""

from __future__ import annotations

import hashlib
import hmac
import json


def _secret(tenant: str, provider: str) -> bytes:
    return hashlib.sha256(f"fed-dir://{tenant}/{provider}".encode("utf-8")).digest()


def _sig_base(c: dict) -> bytes:
    keys = ("tenant", "provider", "identity", "identity_gen", "peer",
            "request_id", "not_before", "not_after", "alg")
    return json.dumps({k: c[k] for k in keys}, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _confirmation_ok(c: dict, *, tenant: str, identity: str, peer: str,
                     request_id: str, provider: str, identity_gen: int,
                     now: int) -> str | None:
    """独立重写的确认校验，返回 None 或拒绝码（与 ErrorCode 字符串一致）。"""
    if not isinstance(c, dict):
        return "BAD_CONFIRMATION"
    need = {"tenant", "provider", "identity", "identity_gen", "peer",
            "request_id", "not_before", "not_after", "alg", "sig"}
    if not need <= set(c):
        return "BAD_CONFIRMATION"
    want = hmac.new(_secret(tenant, provider), _sig_base(c),
                    hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, str(c["sig"])):
        return "BAD_CONFIRMATION"
    if (c["tenant"], c["provider"], c["identity"], c["peer"],
            c["request_id"]) != (tenant, provider, identity, peer, request_id):
        return "BAD_CONFIRMATION"
    if int(c["identity_gen"]) != identity_gen:
        return "STALE_CONFIRMATION"
    if now < int(c["not_before"]):
        return "CONFIRMATION_NOT_YET_VALID"
    if now > int(c["not_after"]):
        return "CONFIRMATION_EXPIRED"
    return None


class OracleReject(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class FederatedOracle:
    """参考模型实例：一个实例对应一个 SQLite 文件的全租户状态。"""

    def __init__(self) -> None:
        self.clock: dict[str, int] = {}
        self.gen_seq: dict[str, int] = {}
        # identity -> [provider, gen]
        self.identities: dict[str, dict[str, list]] = {}
        # edge_id -> dict(state, a, b, nb, na)
        self.edges: dict[str, dict[str, dict]] = {}
        # tenant -> subject_no -> {"gen", "members": tuple(sorted)}
        self.subjects: dict[str, dict[str, dict]] = {}
        # session_id -> dict(tenant, no, gen, members tuple, valid, reason)
        self.sessions: dict[str, dict] = {}
        self.receipts: dict[tuple[str, str], dict] = {}

    # ------------------------------------------------------------ 图工具
    @staticmethod
    def _dfs(adj: dict[str, set[str]], start: str, seen: set[str]) -> set[str]:
        stack = [start]
        comp = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            comp.add(node)
            stack.extend(n for n in adj[node] if n not in seen)
        return comp

    def _components(self, tenant: str, now: int) -> list[set[str]]:
        idents = self.identities[tenant]
        adj: dict[str, set[str]] = {i: set() for i in idents}
        for e in self.edges[tenant].values():
            if e["state"] != "active":
                continue
            if not (e["nb"] <= now <= e["na"]):
                continue
            adj[e["a"]].add(e["b"])
            adj[e["b"]].add(e["a"])
        seen: set[str] = set()
        out = []
        for node in sorted(adj):
            if node not in seen:
                out.append(self._dfs(adj, node, seen))
        return out

    def _advance_subjects(self, tenant: str, now: int,
                          changed: set[str]) -> list[dict]:
        comps = self._components(tenant, now)
        old = self.subjects.get(tenant, {})
        fresh: dict[str, dict] = {}
        events: list[dict] = []
        for comp in comps:
            no = min(comp)
            members = tuple(sorted(comp))
            prev = old.get(no)
            if prev is None:
                self.gen_seq[tenant] += 1
                gen = self.gen_seq[tenant]
                events.append({"event": "subject_created", "subject_no": no,
                               "subject_gen": gen})
            elif prev["members"] != members or (comp & changed):
                self.gen_seq[tenant] += 1
                gen = self.gen_seq[tenant]
                events.append({"event": "subject_gen_advanced", "subject_no": no,
                               "old_gen": prev["gen"], "subject_gen": gen})
            else:
                gen = prev["gen"]
            fresh[no] = {"gen": gen, "members": members}
        for gone in sorted(set(old) - set(fresh)):
            events.append({"event": "subject_number_retired", "subject_no": gone,
                           "last_gen": old[gone]["gen"]})
        self.subjects[tenant] = fresh
        return events

    def _invalidate(self, tenant: str) -> list[dict]:
        view = self.subjects[tenant]
        killed = []
        for sid, s in self.sessions.items():
            if s["tenant"] != tenant or not s["valid"]:
                continue
            reason = None
            if s["no"] not in view:
                reason = "SUBJECT_NUMBER_GONE"
            elif view[s["no"]]["gen"] != s["gen"]:
                reason = "SUBJECT_GEN_ADVANCED"
            elif view[s["no"]]["members"] != tuple(s["members"]):
                reason = "GUARANTEED_MEMBERS_CHANGED"
            if reason:
                s["valid"] = False
                s["reason"] = reason
                killed.append({"session_id": sid, "subject_no": s["no"],
                               "reason": reason})
        return killed

    def _check_confirmations(self, req: dict, a: str, b: str, now: int) -> None:
        confs = req.get("confirmations")
        if not isinstance(confs, list) or len(confs) != 2:
            raise OracleReject("BAD_CONFIRMATION")
        idents = self.identities[req["tenant"]]
        # 前置只核身份+提供方，代数过期交由 _confirmation_ok 判 STALE
        wanted = {(a, idents[a][0]), (b, idents[b][0])}
        seen = set()
        for c in confs:
            if not isinstance(c, dict):
                raise OracleReject("BAD_CONFIRMATION")
            key = (c.get("identity"), c.get("provider"))
            if key not in wanted or key in seen:
                raise OracleReject("BAD_CONFIRMATION")
            seen.add(key)
            ident = key[0]
            peer = b if ident == a else a
            code = _confirmation_ok(
                c, tenant=req["tenant"], identity=ident, peer=peer,
                request_id=req["request_id"], provider=key[1],
                identity_gen=idents[ident][1], now=now)
            if code:
                raise OracleReject(code)

    # ------------------------------------------------------------ 端点
    def apply(self, req: dict) -> dict:
        tenant, rid, op = req["tenant"], req["request_id"], req["op"]
        if op == "register_tenant":
            if tenant in self.clock:
                return self._reject(req, "TENANT_EXISTS")
            self.create_tenant(tenant)
            now = int(req["tick"])
            self.clock[tenant] = max(0, now)
            receipt = {"request_id": rid, "tenant": tenant, "op": op,
                       "tick": self.clock[tenant], "status": "OK",
                       "replayed": False,
                       "snapshot": self.snapshot(tenant, self.clock[tenant])}
            self.receipts[(tenant, rid)] = json.loads(json.dumps(receipt))
            return receipt
        if tenant not in self.clock:
            return self._reject(req, "UNKNOWN_TENANT", snapshot=False)
        now = int(req["tick"])

        # 幂等重放优先于时钟
        if (tenant, rid) in self.receipts:
            stored = json.loads(json.dumps(self.receipts[(tenant, rid)]))
            stored["replayed"] = True
            return stored
        if now < self.clock[tenant]:
            return self._reject(req, "CLOCK_NOT_MONOTONIC")

        try:
            self._dispatch(req, now)
        except OracleReject as exc:
            return self._reject(req, exc.code)

        self.clock[tenant] = now
        receipt = {"request_id": rid, "tenant": tenant, "op": op, "tick": now,
                   "status": "OK", "replayed": False,
                   "snapshot": self.snapshot(tenant, now)}
        self.receipts[(tenant, rid)] = json.loads(json.dumps(receipt))
        return receipt

    def _dispatch(self, req: dict, now: int) -> None:
        tenant = req["tenant"]
        op = req["op"]
        if op == "register_tenant":
            raise OracleReject("TENANT_EXISTS")  # apply 入口已单独处理
        if op == "register_identity":
            ident = req["identity_id"]
            if ident in self.identities[tenant]:
                raise OracleReject("IDENTITY_EXISTS")
            self.identities[tenant][ident] = [req["provider"], 1]
            self._advance_subjects(tenant, now, set())
            self._invalidate(tenant)
            return
        if op == "associate":
            self._associate(req, now)
        elif op == "revoke":
            self._revoke(req, now)
        elif op == "reissue":
            self._reissue(req, now)
        elif op == "renew_edge":
            self._renew(req, now)
        elif op == "login":
            self._login(req, now)
        else:
            raise OracleReject("BAD_CONFIRMATION")

    def _associate(self, req: dict, now: int) -> None:
        tenant = req["tenant"]
        a, b = sorted((req["identity_a"], req["identity_b"]))
        if a == b:
            raise OracleReject("BAD_CONFIRMATION")
        idents = self.identities[tenant]
        if a not in idents or b not in idents:
            raise OracleReject("UNKNOWN_IDENTITY")
        self._check_confirmations(req, a, b, now)
        for e in self.edges[tenant].values():
            if e["state"] == "active" and e["a"] == a and e["b"] == b:
                raise OracleReject("DUPLICATE_ACTIVE_EDGE")
        comps = self._components(tenant, now)
        ca = next(c for c in comps if a in c)
        cb = next(c for c in comps if b in c)
        if ca != cb:
            providers = [idents[i][0] for i in ca | cb]
            if len(set(providers)) != len(providers):
                raise OracleReject("PROVIDER_CONFLICT")
        self.edges[tenant][req["request_id"]] = {
            "state": "active", "a": a, "b": b,
            "nb": int(req["not_before"]), "na": int(req["not_after"])}
        self._advance_subjects(tenant, now, set())
        self._invalidate(tenant)

    def _revoke(self, req: dict, now: int) -> None:
        tenant = req["tenant"]
        edge = self.edges[tenant].get(req["edge_id"])
        if edge is None:
            raise OracleReject("EDGE_NOT_FOUND")
        if edge["state"] == "revoked":
            raise OracleReject("EDGE_REVOKED")
        edge["state"] = "revoked"
        self._advance_subjects(tenant, now, set())
        self._invalidate(tenant)

    def _reissue(self, req: dict, now: int) -> None:
        tenant, ident = req["tenant"], req["identity_id"]
        if ident not in self.identities[tenant]:
            raise OracleReject("UNKNOWN_IDENTITY")
        self.identities[tenant][ident][1] += 1
        for e in self.edges[tenant].values():
            if e["state"] == "active" and ident in (e["a"], e["b"]):
                e["state"] = "stale"
        self._advance_subjects(tenant, now, {ident})
        self._invalidate(tenant)

    def _renew(self, req: dict, now: int) -> None:
        tenant = req["tenant"]
        old = self.edges[tenant].get(req["edge_id"])
        if old is None:
            raise OracleReject("EDGE_NOT_FOUND")
        if old["state"] == "revoked":
            raise OracleReject("EDGE_REVOKED")
        if old["state"] == "active":
            raise OracleReject("EDGE_NOT_RENEWABLE")
        a, b = old["a"], old["b"]
        if a not in self.identities[tenant] or b not in self.identities[tenant]:
            raise OracleReject("UNKNOWN_IDENTITY")
        self._check_confirmations(req, a, b, now)
        self.edges[tenant][req["request_id"]] = {
            "state": "active", "a": a, "b": b,
            "nb": int(req["not_before"]), "na": int(req["not_after"])}
        self._advance_subjects(tenant, now, set())
        self._invalidate(tenant)

    def _login(self, req: dict, now: int) -> None:
        tenant, ident = req["tenant"], req["identity_id"]
        if ident not in self.identities[tenant]:
            raise OracleReject("UNKNOWN_IDENTITY")
        self._advance_subjects(tenant, now, set())
        for no, info in self.subjects[tenant].items():
            if ident in info["members"]:
                self.sessions[req["request_id"]] = {
                    "tenant": tenant, "no": no, "gen": info["gen"],
                    "members": list(info["members"]), "valid": True,
                    "reason": None}
                self._invalidate(tenant)
                return
        raise OracleReject("NO_SUBJECT")

    # ------------------------------------------------------------ 输出
    def _reject(self, req: dict, code: str, snapshot: bool = True) -> dict:
        tenant = req["tenant"]
        receipt = {"request_id": req["request_id"], "tenant": tenant,
                   "op": req["op"], "tick": req.get("tick"), "status": code,
                   "replayed": False,
                   "snapshot": (self.snapshot(tenant, self.clock.get(tenant, 0))
                                if snapshot and tenant in self.clock else None)}
        # 拒绝收据同样持久化，后续同 ID 请求只能重放
        if tenant in self.clock:
            self.receipts[(tenant, req["request_id"])] = json.loads(
                json.dumps(receipt))
            if int(req.get("tick", 0)) > self.clock[tenant]:
                self.clock[tenant] = int(req["tick"])
        return receipt

    def snapshot(self, tenant: str, now: int) -> dict:
        subjects = {no: {"subject_gen": info["gen"],
                         "members": list(info["members"])}
                    for no, info in sorted(self.subjects.get(tenant, {}).items())}
        sessions = [
            {"session_id": sid, "subject_no": s["no"], "subject_gen": s["gen"],
             "members": list(s["members"]), "valid": s["valid"],
             "invalid_reason": s["reason"]}
            for sid, s in sorted(self.sessions.items()) if s["tenant"] == tenant]
        gens = {i: v[1] for i, v in sorted(self.identities[tenant].items())}
        return {"clock": now, "identity_gens": gens, "subjects": subjects,
                "sessions": sessions}

    # 供 register_tenant 场景使用
    def create_tenant(self, tenant: str) -> None:
        self.clock[tenant] = 0
        self.gen_seq[tenant] = 0
        self.identities[tenant] = {}
        self.edges[tenant] = {}
        self.subjects[tenant] = {}
