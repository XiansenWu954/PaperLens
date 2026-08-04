# Tasks: add-sse-frontend

- [x] 1. 后端依赖 + app（corsheaders + CORS_ALLOWED_ORIGINS + api/realtime app）
- [x] 2. REST 端点（POST /api/research, GET /api/research/<id> + ResearchTask 模型）
- [x] 3. SSE 端点 + 流映射（realtime/sse.py map_astream_to_sse + views async StreamingHttpResponse）
  - [x] 持久化 agent 结果到 ResearchTask
  - [x] 修复 citation_graph 节点 async ORM（SynchronousOnlyOperation）
- [x] 4. 后端测试（api 6 + realtime 12 = 18 tests）
- [x] 5. 前端骨架（router + pinia + types + store）
- [x] 6. useSse composable（EventSource + onScopeDispose + 退避重连）
- [x] 7. 视图与组件（SearchView/ResearchView/StepTimeline/CitationGraph d3-force/ResearchReport）
- [x] 8. 端到端验证 ✓
- [x] 9. 归档（specs 合并 + archive + git 提交）

---

## 附录：端到端真实输出（2026-07-30）

### SSE 事件流（task 4 "attention机制原理"）
```
event 统计: 4×step, 1×graph, 1×done
status: done, report: 5640字, sources: 65, graph_nodes: 53, error: (无)
```
完整事件序列：`connected → step(planner,3子查询) → step(fan_out,100→65sources) → step(citation_graph) → graph(53节点) → step(synthesizer) → token×N(综述逐字) → done`

### 引用图谱三类标注（53节点样本）
```
Transformer-XL (2019, 3175引用)      = 根（奠基性）
Rethinking Semantic Seg (2021, 3571) = 根+前沿
Informer (2021, 6359)                = 前沿
Are Transformers Effective (2023)    = 前沿
```

### 综述（按图谱三类组织，节选）
> ## 注意力机制原理：从基础到前沿的研究综述
> ### 1. 引言与奠基性工作
> 注意力机制的核心思想是模仿人类视觉系统的选择性关注能力...(Attention mechanisms in computer vision: A survey, 2022)。
> 奠基性工作主要围绕自注意力（Self-Attention）机制展开...

### 测试与构建
- 后端：90 tests OK（papers7+datasources15+agent17+rag19+citation14+api6+realtime12）
- 前端：npm run build 成功（89模块，462ms，无TS错误）
- 前端 dev：localhost:5173 可访问（HTTP 200）

## 调试过程（修 2 个 bug）
1. **citation_graph async ORM**：节点内 `Paper.objects.filter()` 在 async 上下文同步调用报 SynchronousOnlyOperation。
   修复：包进 sync_to_async(_resolve_seed_ids)()。
2. **PDF 4xx 重试拖慢**：脏 pdf_url(404) 重试4次拖慢 agent。修复：download_pdf 对 4xx 快速失败不重试。
