# Design: add-citation-graph（★护城河）

> 锚定 Connected Papers 算法（创始人 Medium 文 + Behera 2023 学术分析）：
> co-citation + bibliographic coupling 相似图，非引用树。

## 1. 目录结构
```
backend/citation/
├── __init__.py
├── apps.py
├── graph_build.py    # 建图：referenced_works → bibliographic coupling 相似图
├── analyze.py        # 三类标注：pagerank / 年衰减 frontier / louvain 社区
├── visualize.py      # 生成前端图 JSON（nodes/edges/位置/size/color）
└── smoke.py          # 端到端验证
```

## 2. 图构建（graph_build.py）
```python
def build_similarity_graph(seed_papers: list[Paper], max_nodes: int = 200) -> nx.Graph:
    """从种子论文构建 bibliographic coupling 相似图。

    种子论文的 referenced_works 已落库（papers.Paper.referenced_works）。
    相似度 = 两篇种子共享的参考文献数。
    """
    # 1. 取每篇种子的 referenced_works 集合（归一化 openalex id）
    refs = {p.id: {_norm_oid(r) for r in (p.referenced_works or [])} for p in seed_papers}

    # 2. bibliographic coupling：共享参考数 = 边权重
    G = nx.Graph()
    for p in seed_papers:
        G.add_node(p.id, paper=p)
    ids = list(refs.keys())
    for i, a in enumerate(ids):
        for b in ids[i+1:]:
            w = len(refs[a] & refs[b])
            if w > 0:
                G.add_edge(a, b, weight=w)

    # 3. 封顶节点数（按度数保留 top max_nodes）
    if G.number_of_nodes() > max_nodes:
        keep = sorted(G.degree, key=lambda x: -x[1])[:max_nodes]
        G = G.subgraph([n for n, _ in keep]).copy()
    return G
```
注：v1 用 bibliographic coupling（种子间共享参考）。co-citation（被同批引用）需额外
取 OpenAlex 反向引用，调用多，作为 v2 增强，代码预留接口。

`_norm_oid`：`https://openalex.org/W123` → `W123`。

## 3. 三类标注（analyze.py）
```python
def label_nodes(G: nx.Graph, current_year: int = 2026) -> dict:
    """标注三类：奠基性/最新前沿/子主题簇。"""
    pr = nx.pagerank(G, weight="weight")
    try:
        communities = nx.community.louvain_communities(G, seed=42, weight="weight")
    except Exception:
        communities = [set(G.nodes())]
    comm_id = {n: i for i, comm in enumerate(communities) for n in comm}

    labels = {}
    for n in G.nodes():
        paper = G.nodes[n].get("paper")
        year = paper.year if paper else None
        # 奠基性：pagerank 高
        seminal = pr.get(n, 0)
        # 最新前沿：pagerank × 年衰减
        if year:
            frontier = seminal * (0.5 ** ((current_year - year) / 5))
        else:
            frontier = 0
        labels[n] = {
            "seminal": seminal,
            "frontier": frontier,
            "cluster": comm_id.get(n, 0),
            "is_root": seminal > _percentile(pr.values(), 80),  # top 20% 为根
            "is_frontier": frontier > _percentile([v for v in labels.values()], 80),
        }
    return labels
```
`_percentile`：简单分位数辅助。

## 4. 可视化数据（visualize.py）
```python
def to_vis_data(G, labels) -> dict:
    pos = nx.spring_layout(G, weight="weight", seed=42)
    nodes = []
    for n in G.nodes():
        p = G.nodes[n].get("paper")
        nodes.append({
            "id": n, "title": p.title[:60] if p else "", "year": p.year,
            "citation_count": p.citation_count if p else 0,
            "size": p.citation_count if p else 1,  # ∝ 引用数
            "color_year": p.year if p else None,    # ∝ 年份
            "cluster": labels[n]["cluster"],
            "is_root": labels[n]["is_root"],
            "is_frontier": labels[n]["is_frontier"],
            "x": pos[n][0], "y": pos[n][1],
        })
    edges = [{"source": u, "target": v, "weight": d["weight"]} for u, v, d in G.edges(data=True)]
    return {"nodes": nodes, "edges": edges}
```

## 5. agent 集成（graph.py 加 citation_graph 节点）
```
planner → fan_out_researchers → citation_graph → synthesizer → END
```
citation_graph 节点：取 sources 里的论文（已入库），build_similarity_graph + label + to_vis_data，
结果存 AgentState["citation_graph"]，并把三类标注摘要注入 synthesizer 的输入（让综述按三类组织）。

AgentState 加字段：`citation_graph: dict`（vis_data）。

## 6. 验证项
`python -m citation.smoke`：检索一批 Mamba 论文 → 建图 → 三类标注 → 输出节点数/边数/根节点/frontier。

## 7. 测试（citation/tests.py）
- graph_build：手写造 referenced_works，验证 coupling 边权重正确
- analyze：mock 小图，验证 pagerank/louvain/frontier 标注
- visualize：验证 to_vis_data 结构（nodes/edges/x,y）
- _norm_oid：URL→id 归一化
