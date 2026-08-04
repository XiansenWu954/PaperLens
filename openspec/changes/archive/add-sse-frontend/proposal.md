# Change: add-sse-frontend

## Why（为什么做）
前 4 个 change 完成了 agent 内核（多智能体+RAG+引用图谱），但目前只能命令行调用。
要让项目成为**可演示的完整产品**（简历最直观展示），需要：
1. 后端 SSE 流式端点，实时推送 agent 各步骤（planner/researcher/citation_graph/synthesizer）+ 逐字生成。
2. Vue 前端：输入研究问题 → 实时看 agent 工作 → 渲染引用图谱（护城河的可视化）→ 展示综述。

这是把"内核"变成"产品"的关键一跃。

## What（改什么）
- **后端**：
  - 新建 `api` app：DRF 端点 `POST /api/research`（建任务，返回 task_id）。
  - 新建 `realtime` SSE 端点 `GET /api/research/<task_id>/stream`：
    - 桥接 LangGraph `astream(stream_mode=["updates","messages"])`。
    - 事件：`step`（节点完成）、`token`（逐字）、`graph`（引用图谱 vis_data）、`done`、`error`。
    - 关键头：`text/event-stream` + `Cache-Control:no-cache` + `X-Accel-Buffering:no`。
  - ResearchTask ORM 模型存任务状态/最终综述/图谱。
  - 移除临时 sse_demo 端点。
- **前端**（Vue 3）：
  - `SearchView`：输入研究问题，发 POST 建 task，开 SSE。
  - `useSse` composable：EventSource + onScopeDispose 清理 + 退避重连。
  - `StepTimeline`：实时展示 agent 各节点步骤。
  - `CitationGraph`：d3-force 渲染引用图谱（节点 size∝citation/color∝year/聚类着色，点击弹 PaperCard）。
  - `ResearchReport`：流式渲染综述 markdown + 来源列表。
  - Pinia store 管理 SSE 状态。

## 地基事实验证
| 事实 | 结果 |
|---|---|
| Django async SSE（Daphne） | ✅ sse_demo 流式逐条到达，头正确 |
| Vue 3 + Vite 脚手架 | ✅ vue 3.5 / vite 8 / TS 6 |
| d3-force / vue-router / pinia / vueuse | ✅ 全装好 |

## Out of scope
- 用户认证（单用户本地演示，不做）
- 任务队列/后台 worker（同步 astream 跑在 SSE 生成器里，够演示）
- 移动端适配（桌面优先）

## 风险
- SSE 长连接 + Django runserver 并发——演示场景单连接够用。
- CORS：前后端不同端口（5173 vs 8000）→ Django 配 CORS 允许 localhost:5173。
- LangGraph astream 事件格式需适配到 SSE。
