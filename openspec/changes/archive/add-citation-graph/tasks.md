# Tasks: add-citation-graph

- [x] 1. 包骨架（apps.py + 注册 INSTALLED_APPS + migrations 目录）
- [x] 2. 图构建（build_similarity_graph 手写 bibliographic coupling + norm_oid + max_nodes 封顶）
- [x] 3. 三类标注（label_nodes：pagerank/frontier年衰减/louvain + _percentile）
- [x] 4. 可视化数据（to_vis_data：nodes/edges/spring_layout/size∝citation + summarize_for_synthesis）
- [x] 5. agent 集成（AgentState 加 citation_graph 字段 + graph.py 加 citation_graph 节点 + synthesizer 注入三类）
- [x] 6. smoke + 端到端验证 ✓
- [x] 7. 测试（14 tests）+ 全套 72 tests OK + 归档 + git 提交

---

## 附录：smoke 真实输出（2026-07-30）

`python -m citation.smoke`（检索 8 篇 Mamba 论文）：
```
检索到 8 篇
有 referenced_works 的种子: 5
--- 2. 构建 bibliographic coupling 相似图 ---
节点: 5, 边: 5
--- 3. 三类标注 ---
奠基性根节点: 1, 最新前沿: 1, 子主题簇: 2
--- 4. 生成可视化数据 ---
vis nodes: 5, edges: 5
  - Pan-Mamba year=2024 size=188 cluster=0
  - RS³Mamba year=2024 size=292 cluster=0
  - Enhancing spatiotemporal year=2025 size=36 cluster=1
--- 5. 综述注入摘要 ---
奠基性论文: SSAMBA(2024)
最新前沿: SSAMBA(2024)
子主题簇 0: Pan-Mamba; RS³Mamba; SSAMBA; SambaMixer
引用图谱验证通过 ✓
```

## 算法说明（护城河核心）
- 复刻 Connected Papers：bibliographic coupling 相似图（非引用树）
- networkx 3.6 移除了内置 bibliographic_coupling → 手写（共享参考文献集合交集）
- 三类：pagerank(奠基) / pagerank×0.5^((2026-year)/5)(前沿) / louvain(子主题)
- 数据零成本：OpenAlex referenced_works（地基验证 140 条）

## 测试覆盖（14 tests）
norm_oid(3) + build_similarity_graph 共参考边权重/URL归一化/空refs(3) + label_nodes 三类/年衰减/空图(3) + percentile(2) + vis结构(1) + summarize(2)
