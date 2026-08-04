# Spec delta: citation-graph

## ADDED Requirements

### Requirement: 引用相似图构建
系统必须基于种子论文的 referenced_works 构建 bibliographic coupling 相似图（复刻 Connected Papers 算法）。

#### Scenario: 共参考建图
- **GIVEN** 种子论文 A 引用 [1,2,3]，B 引用 [2,3,4]
- **WHEN** build_similarity_graph
- **THEN** A-B 之间有边，权重=共享参考数=2

### Requirement: 三类节点标注
图节点必须标注三类：奠基性根节点（pagerank）、最新前沿（pagerank×年衰减）、子主题簇（louvain）。

#### Scenario: 奠基性标注
- **WHEN** label_nodes 执行
- **THEN** 高 pagerank 的节点 is_root=True（top 20%）

#### Scenario: 前沿标注
- **WHEN** label_nodes 执行
- **THEN** pagerank×年衰减高的节点 is_frontier=True

#### Scenario: 子主题簇
- **WHEN** label_nodes 执行
- **THEN** 每节点有 cluster 编号（louvain 社区）

### Requirement: 可视化数据
必须生成前端可渲染的图 JSON（nodes 带 size/color/position，edges 带权重）。

#### Scenario: vis 数据结构
- **WHEN** to_vis_data 执行
- **THEN** 返回 {nodes:[{id,title,year,size,color_year,cluster,x,y,...}], edges:[{source,target,weight}]}
- **AND** node size ∝ citation_count，position 来自 spring_layout

### Requirement: agent 集成
citation_graph 节点必须在 fan_out 之后、synthesizer 之前运行，把三类标注注入综述。

#### Scenario: 综述按三类组织
- **WHEN** agent 运行
- **THEN** synthesizer 收到 citation_graph 数据
- **AND** 综述围绕奠基性/前沿/子主题组织

### Requirement: 候选池封顶
图节点数必须封顶，防止引用数爆炸。

#### Scenario: 节点封顶
- **GIVEN** 种子论文 referenced_works 总数 > max_nodes
- **WHEN** build_similarity_graph(max_nodes=200)
- **THEN** 图节点数 ≤ 200

### Requirement: id 归一化
referenced_works 的 openalex 全 URL 必须归一化为短 id。

#### Scenario: URL 归一化
- **WHEN** 处理 "https://openalex.org/W123"
- **THEN** 归一化为 "W123"

### Requirement: 端到端验证
必须有一条命令验证完整图谱构建。

#### Scenario: smoke 通过
- **WHEN** 执行 `python -m citation.smoke`
- **THEN** 检索种子 → 建图 → 三类标注 → 输出节点/边/根/frontier
