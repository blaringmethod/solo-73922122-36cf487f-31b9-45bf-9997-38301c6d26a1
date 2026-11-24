"""地类拆分核算核心求解器。

每个图幅构成一个运输型拆分问题：

- 变量 x[o][n] >= 0（整数）：旧地类 o 中划入新地类 n 的像元数，
  仅当对照表允许 (o, n) 时该变量存在（一个旧码可对应多个新码）；
- 行约束：对每个旧地类 o，sum_n x[o][n] = 旧图登记的 o 像元数；
- 列约束：对每个已复核的新地类 n，sum_o x[o][n] = 复核补报的整幅总数；
- 未复核的新地类列和不受限，吸收剩余像元。

在上述约束（非负整数拆分）下，求某对 (old, new) 重合像元数能取到的
最小值与最大值。约束矩阵全幺模，最小费用流给出的最优解天然为整数解。

网络（分层结构 s -> 旧码 -> 新码 -> t，前向无环）：

    s -> 旧码节点        容量=该旧码像元数（普通边，行和由满流守恒锁定）
    旧码 -> 新码节点      容量=图幅总数，查询相关边费用 0/1，其余 0
    已复核新码 -> t       下界=上界=该新码复核数（列和锁定，残余恒 0 不可回退）
    未复核新码 -> t       容量=图幅总数，吸收剩余像元
    t -> s               容量=图幅总数的零费用回流边，供下界补流形成环流

带下界的复核汇边按经典转换处理：边 u->v 下界 l、上界 u 记为残余容量
u-l，并对每个节点记账 balance[v] += l、balance[u] -= l；再建超级源 S'、
超级汇 T'，对 balance>0 的节点加 S'->v（容量 balance），balance<0 的
节点加 v->T'（容量 -balance）。S'->T' 最大流打满全部补流边，当且仅当
复核列下界可同时满足。随后禁用回流边与超级补流边（避免回流边的反向
残余抢占增广配额），在纯业务残余图上推 s->t 最大流：所有源边打满即
整幅像元都有合法去向、图幅可行。

优化在该可行满流上做负费用环消除（Bellman-Ford，松弛轮数固定，必然
终止）。所有前向费用非负（0/1）：
- min 模式：查询边费用 1、其余旧->新边 0，负环消除后查询边流量最小；
- max 模式：查询边费用 0、其余旧->新边费用 1，查询边流量最大。
复核汇边残余容量恒为 0，负环无法经它回退，列和在优化中始终恰好锁定；
源边则靠节点守恒在满流状态下保持打满。
"""
from __future__ import annotations

from dataclasses import dataclass, field

INF = float("inf")


class InfeasibleStateError(RuntimeError):
    """已提交状态被判定为不可解（正常情况下不应发生，因为写入前都会校验）。"""


class Edge:
    """残余网络中的一条有向边。"""

    __slots__ = ("fr", "to", "cap", "cost", "rev", "initial_cap")

    def __init__(self, fr: int, to: int, cap: int, cost: int, rev: int):
        self.fr = fr
        self.to = to
        self.cap = cap          # 当前残余容量
        self.cost = cost
        self.rev = rev          # 反向边在 graph[to] 中的下标
        self.initial_cap = cap  # 建网时的残余容量（上界-下界）


class MinCostFlow:
    """最小费用流 / 环流：Bellman-Ford 连续最短路与负环消除。

    选 Bellman-Ford 而非 SPFA：松弛轮数固定为节点数-1，即使残余图中存在
    负费用反向边也必然终止（前向网络无环且前向费用非负）。
    """

    def __init__(self, num_nodes: int):
        self.graph: list[list[Edge]] = [[] for _ in range(num_nodes)]

    def add_edge(self, fr: int, to: int, cap: int, cost: int) -> Edge:
        fwd = Edge(fr, to, cap, cost, len(self.graph[to]))
        rev = Edge(to, fr, 0, -cost, len(self.graph[fr]))
        self.graph[fr].append(fwd)
        self.graph[to].append(rev)
        return fwd

    def flow_of(self, edge: Edge) -> int:
        """边的实际流量 = 初始残余容量 - 当前残余容量（另加该边下界）。"""
        return edge.initial_cap - edge.cap

    def disable_edge(self, edge: "Edge") -> None:
        """彻底禁用一条边（两个方向残余容量都置 0）。"""
        edge.cap = 0
        self.graph[edge.to][edge.rev].cap = 0

    def max_flow(self, s: int, t: int) -> int:
        """Bellman-Ford 最大流（忽略费用），返回流量。"""
        n = len(self.graph)
        total = 0
        while True:
            prev_v = [-1] * n
            prev_e = [-1] * n
            seen = [False] * n
            seen[s] = True
            queue = [s]
            head = 0
            while head < len(queue):
                u = queue[head]
                head += 1
                for i, e in enumerate(self.graph[u]):
                    if e.cap > 0 and not seen[e.to]:
                        seen[e.to] = True
                        prev_v[e.to] = u
                        prev_e[e.to] = i
                        queue.append(e.to)
            if not seen[t]:
                return total
            d = INF
            v = t
            while v != s:
                d = min(d, self.graph[prev_v[v]][prev_e[v]].cap)
                v = prev_v[v]
            v = t
            while v != s:
                e = self.graph[prev_v[v]][prev_e[v]]
                e.cap -= d
                self.graph[v][e.rev].cap += d
                v = prev_v[v]
            total += d

    def cancel_negative_cycles(self, max_rounds: int) -> None:
        """反复沿负费用环增广，直到无负环（达到最小费用环流）。"""
        n = len(self.graph)
        for _ in range(max_rounds):
            # 全零初始化等价于加一个连向所有节点的虚拟源，可探测任意负环
            dist = [0] * n
            prev_v = [-1] * n
            prev_e = [-1] * n
            relaxed = -1
            for _ in range(n):
                relaxed = -1
                for u in range(n):
                    du = dist[u]
                    for i, e in enumerate(self.graph[u]):
                        if e.cap > 0 and du + e.cost < dist[e.to]:
                            dist[e.to] = du + e.cost
                            prev_v[e.to] = u
                            prev_e[e.to] = i
                            relaxed = e.to
            if relaxed == -1:
                return
            v = relaxed
            for _ in range(n):
                v = prev_v[v]
            start = v
            edges = []
            u = start
            while True:
                i = prev_e[u]
                edges.append(self.graph[prev_v[u]][i])
                u = prev_v[u]
                if u == start:
                    break
            d = min(e.cap for e in edges)
            for e in edges:
                e.cap -= d
                self.graph[e.to][e.rev].cap += d


@dataclass
class SheetProblem:
    """单个图幅的拆分问题。"""

    old_counts: dict[str, int] = field(default_factory=dict)   # 旧地类 -> 像元数
    review_totals: dict[str, int] = field(default_factory=dict)  # 新地类 -> 复核整幅总数
    mapping: dict[str, set[str]] = field(default_factory=dict)  # 旧地类 -> 可对应新地类集合

    @property
    def total_pixels(self) -> int:
        return sum(self.old_counts.values())

    @property
    def reviewed_pixels(self) -> int:
        return sum(self.review_totals.values())


def _layout(prob: SheetProblem) -> dict:
    old_codes = sorted(prob.old_counts)
    new_codes = sorted(
        {n for targets in prob.mapping.values() for n in targets}
        | set(prob.review_totals)
    )
    s, t = 0, 1
    o_base = 2
    n_base = o_base + len(old_codes)
    super_s = n_base + len(new_codes)
    super_t = super_s + 1
    return {
        "old_codes": old_codes,
        "new_codes": new_codes,
        "s": s,
        "t": t,
        "o_idx": {o: o_base + i for i, o in enumerate(old_codes)},
        "n_idx": {n: n_base + i for i, n in enumerate(new_codes)},
        "super_s": super_s,
        "super_t": super_t,
        "num_nodes": super_t + 1,
    }


def _build_network(prob: SheetProblem, layout: dict,
                   query: tuple[str, str] | None = None,
                   mode: str = "feasible") -> dict:
    """建带下界的环流网络，返回网络与关键边引用（同构：边顺序与 mode 无关）。"""
    s, t = layout["s"], layout["t"]
    o_idx, n_idx = layout["o_idx"], layout["n_idx"]
    super_s, super_t = layout["super_s"], layout["super_t"]
    mcf = MinCostFlow(layout["num_nodes"])
    total = prob.total_pixels
    # balance[v] > 0：该节点因下界需要额外流入 balance[v] 个单位
    balance = [0] * layout["num_nodes"]
    lower_bounds: dict[Edge, int] = {}

    def add_bounded(fr: int, to: int, lower: int, upper: int, cost: int) -> Edge:
        edge = mcf.add_edge(fr, to, upper - lower, cost)
        lower_bounds[edge] = lower
        balance[fr] -= lower
        balance[to] += lower
        return edge

    def edge_cost(o: str, n: str) -> int:
        if mode == "feasible" or query is None:
            return 0
        is_query = (o, n) == query
        if mode == "min":
            return 1 if is_query else 0
        return 0 if is_query else 1  # mode == "max"

    # 源边：普通容量边（无下界），行和"恰好"由整幅总流量守恒保证——
    # 所有旧码像元都必须离开 s，可行环流达到 total 时每条源边都打满。
    # 不能给源边设下界：补流阶段旧码节点需要经源边的残余容量继续送流。
    source_edges = []
    for o, c in prob.old_counts.items():
        if c > 0:
            edge = mcf.add_edge(s, o_idx[o], c, 0)
            lower_bounds[edge] = 0
            source_edges.append(edge)
    query_ref = None
    for o, targets in prob.mapping.items():
        if o not in o_idx:
            continue
        for n in targets:
            edge = mcf.add_edge(o_idx[o], n_idx[n], total, edge_cost(o, n))
            lower_bounds[edge] = 0
            if query == (o, n):
                query_ref = edge
    sink_edges = []
    for n in layout["new_codes"]:
        if n in prob.review_totals:
            # 复核汇边：下界=上界=复核数。固定边残余容量恒为 0，负环消除
            # 无法回退它，列和在整个优化过程中始终恰好锁定为复核数。
            # 可行性阶段补流借助 t->s 回流边与超级节点满足其下界。
            sink_edges.append(
                add_bounded(n_idx[n], t,
                            prob.review_totals[n], prob.review_totals[n], 0))
        else:
            edge = mcf.add_edge(n_idx[n], t, total, 0)
            lower_bounds[edge] = 0
    # 回流边：把 s->t 的整幅流转化为环流（零费用，容量足够大）
    back_edge = mcf.add_edge(t, s, total, 0)
    lower_bounds[back_edge] = 0
    # 下界转换的超级源/汇补流边
    super_edges = []
    for node, b in enumerate(balance):
        if b > 0:
            super_edges.append(mcf.add_edge(super_s, node, b, 0))
        elif b < 0:
            super_edges.append(mcf.add_edge(node, super_t, -b, 0))
    return {"mcf": mcf, "query_ref": query_ref, "source_edges": source_edges,
            "sink_edges": sink_edges, "back_edge": back_edge,
            "super_edges": super_edges, "lower_bounds": lower_bounds,
            "super_s": super_s, "super_t": super_t}


def _build_legend_only_network(prob: SheetProblem) -> tuple[MinCostFlow, int, int]:
    """忽略复核列约束的对照网络（新码汇边容量放开），用于诊断不可行原因。"""
    layout = _layout(prob)
    s, t = layout["s"], layout["t"]
    o_idx, n_idx = layout["o_idx"], layout["n_idx"]
    mcf = MinCostFlow(2 + len(layout["old_codes"]) + len(layout["new_codes"]))
    total = prob.total_pixels
    for o, c in prob.old_counts.items():
        if c > 0:
            mcf.add_edge(s, o_idx[o], c, 0)
    for o, targets in prob.mapping.items():
        if o not in o_idx:
            continue
        for n in targets:
            mcf.add_edge(o_idx[o], n_idx[n], total, 0)
    for n in layout["new_codes"]:
        mcf.add_edge(n_idx[n], t, total, 0)
    return mcf, s, t


def check_feasible(prob: SheetProblem) -> list[str]:
    """校验图幅是否存在合法拆分，返回问题描述列表（空列表 = 可行）。"""
    problems: list[str] = []
    total = prob.total_pixels
    reviewed = prob.reviewed_pixels
    for o, c in prob.old_counts.items():
        if c > 0 and not prob.mapping.get(o):
            problems.append(
                f"旧地类[{o}]有 {c} 像元，但对照表中它不能对应任何新地类")
    for n, v in prob.review_totals.items():
        if v > 0 and not any(n in targets for targets in prob.mapping.values()):
            problems.append(
                f"复核新地类[{n}]有 {v} 像元，但对照表中没有任何旧地类能划入它")
    if reviewed > total:
        problems.append(f"复核像元总数 {reviewed} 超过图幅像元总数 {total}")
    if problems or total == 0:
        return problems
    layout = _layout(prob)
    net = _build_network(prob, layout)
    mcf = net["mcf"]
    required = sum(e.cap for e in net["super_edges"]
                   if e.fr == net["super_s"])
    supplied = mcf.max_flow(net["super_s"], net["super_t"])
    feasible = False
    if supplied == required:
        # 补流已满足全部复核列下界。移除 t->s 回流边与超级补流边，使后续
        # 增广只能走真正的业务残余路径，再把 s->t 流推满（行约束可行）。
        mcf.disable_edge(net["back_edge"])
        for e in net["super_edges"]:
            mcf.disable_edge(e)
        mcf.max_flow(layout["s"], layout["t"])
        feasible = all(mcf.flow_of(e) == e.initial_cap
                       for e in net["source_edges"])
    if feasible:
        return []
    legend_mcf, ls, lt = _build_legend_only_network(prob)
    legend_flow = legend_mcf.max_flow(ls, lt)
    if legend_flow < total:
        problems.append(
            f"按对照表最多只能分配 {legend_flow} 像元，少于图幅像元总数 {total}")
    else:
        problems.append(
            f"全部复核总数无法同时满足：在满足各复核列恰好等于补报数的约束下，"
            f"不存在覆盖全部 {total} 像元的非负整数拆分")
    return problems


def _build_feasible_flow(prob: SheetProblem, query, mode: str) -> dict:
    """建网并得到满足全部下界、源边打满的可行满流网络（供优化阶段使用）。"""
    layout = _layout(prob)
    net = _build_network(prob, layout, query=query, mode=mode)
    mcf = net["mcf"]
    required = sum(e.cap for e in net["super_edges"]
                   if e.fr == net["super_s"])
    supplied = mcf.max_flow(net["super_s"], net["super_t"])
    if supplied != required:
        raise InfeasibleStateError("已提交状态不可解，数据不一致")
    # 移除回流边与超级补流边后推满业务流，建立满流基流
    mcf.disable_edge(net["back_edge"])
    for e in net["super_edges"]:
        mcf.disable_edge(e)
    mcf.max_flow(layout["s"], layout["t"])
    if not all(mcf.flow_of(e) == e.initial_cap for e in net["source_edges"]):
        raise InfeasibleStateError("已提交状态不可解，数据不一致")
    return net


def _optimize_query(prob: SheetProblem, old_code: str, new_code: str,
                    mode: str) -> int:
    """求查询边实际流量的最小（mode=min）或最大（max）值。"""
    net = _build_feasible_flow(prob, query=(old_code, new_code), mode=mode)
    mcf = net["mcf"]
    # 负环消除：在不破坏任何固定下界（复核列）的前提下最小化/最大化查询边。
    # 固定边残余容量为 0，负环无法经过它回退，列和始终锁定。
    mcf.cancel_negative_cycles(max_rounds=prob.total_pixels + 1)
    edge = net["query_ref"]
    return net["lower_bounds"][edge] + mcf.flow_of(edge)


def overlap_bounds(prob: SheetProblem, old_code: str, new_code: str) -> tuple[int, int]:
    """返回该图幅中 (old_code, new_code) 重合像元数的 (最小值, 最大值)。

    调用前要求图幅状态可行（写入路径已保证）。不在对照表中的地类对恒为 0。
    """
    if new_code not in prob.mapping.get(old_code, set()):
        return 0, 0
    if prob.old_counts.get(old_code, 0) <= 0 or prob.total_pixels == 0:
        return 0, 0
    lo = _optimize_query(prob, old_code, new_code, "min")
    hi = _optimize_query(prob, old_code, new_code, "max")
    return lo, hi
