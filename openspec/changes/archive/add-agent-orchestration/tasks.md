# Tasks: add-agent-orchestration

- [x] 1. 依赖与包骨架
  - [x] 1.1 requirements.txt 追加 langgraph、langchain-core、aiohttp
  - [x] 1.2 pip install；验证 langgraph 1.2.10
  - [x] 1.3 建 agent/ 包目录结构

- [x] 2. State 与配置
  - [x] 2.1 agent/state.py：AgentState / ResearcherState / ResearcherOutputState + add reducer
  - [x] 2.2 agent/config.py：AgentConfig（预算 + 3 模型名 + reasoning 开关）
  - [x] 2.3 验证：reducer 行为单测（add 累加/None 容错）✓

- [x] 3. 工具层（datasources → Function Calling）
  - [x] 3.1 agent/tools.py：SEARCH_PAPERS_TOOL schema
  - [x] 3.2 execute_tool：registry.search + upsert_batch 入库
  - [x] 3.3 验证：真实 openalex 调用返回 JSON + 入库 ✓

- [x] 4. planner 节点
  - [x] 4.1 agent/prompts.py：planner 提示词
  - [x] 4.2 nodes/planner.py：thinking=False + Pydantic ResearchPlan + 容错解析
  - [x] 4.3 验证：mock 返回固定 JSON，sub_queries 提取正确 ✓

- [x] 5. researcher ReAct 子图
  - [x] 5.1 nodes/researcher.py：react_agent ⇄ tool_node + extract_notes
  - [x] 5.2 tool_call_iterations 预算控制
  - [x] 5.3 complete_with_tools 保留 reasoning（修了 thinking 参数 bug）
  - [x] 5.4 验证：单 sub_query 3 次工具调用，10+10+10 入库，44 sources ✓

- [x] 6. synthesizer 节点
  - [x] 6.1 nodes/synthesizer.py：notes+sources → markdown（thinking=False）
  - [x] 6.2 综述含来源列表
  - [x] 6.3 验证：产出结构化综述 ✓

- [x] 7. graph 编译 + fan_out
  - [x] 7.1 agent/graph.py：planner→fan_out_researchers→synthesizer
  - [x] 7.2 fan_out：asyncio.gather + Semaphore(max_concurrent)（修了 async 节点包裹 bug）
  - [x] 7.3 researcher 输出裁剪（只 notes+sources 上浮）
  - [x] 7.4 验证：graph 编译成功，fan_out 并行 + 单源失败不阻断 ✓

- [x] 8. runner + 端到端验证
  - [x] 8.1 agent/runner.py：build_graph + ainvoke
  - [x] 8.2 跑 "Mamba 状态空间模型最新进展"（附录真实输出）
  - [x] 8.3 sources≥1 + 综述>100字 验证通过 ✓

- [x] 9. 正式测试套件
  - [x] 9.1 agent/tests.py：reducer/planner解析/tools真实/researcher mock/synthesizer mock/graph
  - [x] 9.2 python manage.py test agent → 17 tests OK
  - [x] 9.3 全套 papers+datasources+agent → 39 tests OK

- [x] 10. 归档
  - [x] 10.1 specs 合并进 openspec/specs/agent-orchestration.md
  - [x] 10.2 change 移入 archive/
  - [x] 10.3 git 提交

---

## 附录：端到端真实输出（2026-07-30）

`python -m agent.runner "Mamba 状态空间模型的最新进展"`：
```
来源论文数: 56
研究笔记数: 3
  - Mamba: Linear-Time Sequence Modeling with Selective State Sp (2023) 引用=1030
  - Vision Mamba: Efficient Visual Representation Learning with  (2024) 引用=404
  - VMamba: Visual State Space Model (2024) 引用=371
  - Mamba YOLO: A Simple Baseline for Object Detection with Stat (2025) 引用=122
  - ChangeMamba: Remote Sensing Change Detection With Spatiotemp (2024) 引用=285
验证: sources≥1=True, 综述>100字=True -> 通过 ✓
```
综述结构完整：选择性机制/线性复杂度 → 视觉Mamba → 多模态/医学/安全应用 → 性能对比/展望 → 结论，
**每条论断标注真实来源论文（标题+年份+引用数）**，末尾附 7 条来源列表。引用数来自真实 OpenAlex/ArXiv 检索。

## 调试过程（修了 2 个 bug）
1. **async 节点包裹 bug**：`lambda s: planner(s,config)` 返回 coroutine，LangGraph 1.2 报 InvalidUpdateError。
   修复：改 `async def _planner(s): return await planner(s,config)` 显式 async 节点。
2. **complete_with_tools 缺 thinking 参数**：researcher 传 thinking= 配置项，但方法无此参数。
   修复：给 complete_with_tools 加 thinking 参数（默认 True 保留 reasoning）。

## 成本验证
researcher 用 thinking=True（Function Calling 保留 reasoning，更准）；
planner/synthesizer 用 thinking=False 降本。单次完整运行（planner+3 researcher+synthesizer）约 50-150k token，<$0.10。
