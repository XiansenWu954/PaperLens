# Change: add-citation-graph（★护城河）

## Why（为什么做）
这是 PaperLens 的**核心差异化**。普通论文搜索是"关键词→摘要"，PaperLens 用引用图谱推理：
基于种子论文构建影响力图，识别**奠基性根节点 / 最新前沿 / 子主题簇**三类，让综述围绕这三类组织——
这是 Connected Papers（connectedpapers.com）同款体验，但开源、零成本、自建。

复刻 Connected Papers 算法核心：**co-citation + bibliographic coupling 相似图**（非简单引用树）。
数据走 OpenAlex `referenced_works`（地基验证返回完整列表）。

## What（改什么）
- 新建 `citation` 包。
- **图构建**（缝合 Connected Papers 算法）：
  - 从种子论文集出发，取每篇的 referenced_works（已在 papers.Paper.referenced_works 落库）。
  - 计算 **bibliographic coupling**（共享参考文献数）+ co-citation 相似度，建无向相似图。
  - 边权重 = 共享参考数（耦合强度）。
- **三类标注**：
  - 奠基性根节点：相似图上的 pagerank（高入度/重要性）。
  - 最新前沿：pagerank × 年份衰减 `0.5**((2026-year)/5)`。
  - 子主题簇：louvain 社区检测。
- **可视化数据**：node size∝citation_count, color∝year, position∝spring_layout。
- **集成 agent**：新增 citation_graph 节点，在 fan_out 之后、synthesizer 之前运行，
  把三类标注注入 synthesizer，让综述按"奠基/前沿/子主题"组织。
- 一个 `python -m citation.smoke` 验证：取几篇种子 → 建图 → 三类标注 → 输出图 JSON。

## 地基事实验证（spec 前提）
| 事实 | 结果 |
|---|---|
| OpenAlex referenced_works | ✅ 样本 140 条（change 1 验证） |
| networkx pagerank | ✅ 可用 |
| networkx louvain_communities | ✅ 可用 |
| bibliographic_coupling | ⚠️ networkx 3.6 已移除 → 手写（已验证逻辑正确） |
| spring_layout | ✅ 可用 |

## Out of scope
- cocitation 的完整反向边（需 OpenAlex 的 cited_by_api_list，调用多）—— v1 用 bibliographic coupling 足够，
  co-citation 作为增强（注释说明）。
- 前端图谱渲染（Change 5 SSE+Vue 一起）。

## 风险
- 图过大：种子引用数多时候选爆炸 → 候选池封顶（≤200）。
- OpenAlex id 格式：referenced_works 是 openalex.org/Wxxx 全 URL，需归一化为 Wxxx。
