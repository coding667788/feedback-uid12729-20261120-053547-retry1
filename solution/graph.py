"""由活动关联边计算连通分量。

独立的纯函数模块：输入“身份 -> 提供方”映射与活动边集合，
输出连通分量（每个分量为成员集合），并负责提供方冲突检测——
一个合法分量内，同一提供方至多出现一个身份。
"""

from __future__ import annotations

from collections import defaultdict


def connected_components(identities: dict[str, str],
                         edges: list[tuple[str, str]]) -> list[set[str]]:
    """对 ``edges``（仅活动边）做无向连通分量。

    ``identities`` 给出全量身份，保证孤立身份也以单点分量返回。
    返回分量列表，每个分量是成员身份 ID 的集合；顺序按分量最小 ID 排序，
    与“主体公开编号 = 分量最小身份 ID”的规则一致，便于稳定断言。
    """
    adj: dict[str, set[str]] = {i: set() for i in identities}
    for a, b in edges:
        if a not in adj or b not in adj:
            # 理论上不会发生：边的两端必然是已注册身份；防御性忽略
            continue
        adj[a].add(b)
        adj[b].add(a)

    seen: set[str] = set()
    components: list[set[str]] = []
    for start in sorted(adj):
        if start in seen:
            continue
        stack = [start]
        comp: set[str] = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            comp.add(node)
            stack.extend(adj[node] - seen)
        components.append(comp)
    components.sort(key=lambda c: min(c))
    return components


def provider_conflict(identities: dict[str, str],
                      component: set[str]) -> str | None:
    """返回分量内重复出现的提供方编号；无冲突返回 None。"""
    providers = defaultdict(list)
    for ident in component:
        providers[identities[ident]].append(ident)
    for provider, members in sorted(providers.items()):
        if len(members) > 1:
            return provider
    return None


def check_provider_conflict(identities: dict[str, str],
                            component: set[str]) -> str | None:
    """对外语义别名：冲突时返回提供方 ID，否则 None。"""
    return provider_conflict(identities, component)
