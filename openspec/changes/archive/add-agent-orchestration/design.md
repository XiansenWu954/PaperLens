# Design: add-agent-orchestration

> 锚定 open_deep_research 当前 main 分支（github langchain-ai/open_deep_research
> `src/open_deep_research/deep_researcher.py` / `state.py`）。
> 复刻其 supervisor 模式 + asyncio.gather 并行 + State 4类+reducer + 输入/输出state分离。

## 1. 目录结构
```
backend/
├── agent/
│   ├── __init__.py
│   ├── graph.py          # StateGraph 编译（planner→researcher(并行)→synthesizer）
│   ├── state.py          # 4 类 state + reducer
│   ├── config.py         # 预算参数（max_concurrent/max_iterations/max_tool_calls）
│   ├── nodes/
│   │   ├── __init__.py
│   │   ├── planner.py    # 问题→检索计划（sub-queries）, Pydantic 结构化输出
│   │   ├── researcher.py # ReAct 子图：调 datasources 工具, tool_call_iterations 预算
│   │   └── synthesizer.py# 汇总研究笔记→结构化综述（带来源列表）
│   ├── tools.py          # datasources.search 包装成 Function Calling 工具
│   ├── prompts.py        # planner/researcher/synthesizer 三套提示词
│   └── runner.py         # 运行入口：build_graph + run + 验证
```

## 2. State 4 类 + reducer（缝合 open_deep_research `state.py`）
```python
class AgentState(TypedDict):
    question: str
    plan: list[str]                          # planner 产出的 sub-queries
    notes: Annotated[list[str], add]         # researcher 累加的笔记（operator.add）
    sources: Annotated[list[dict], add]      # 累加的来源（论文元数据）
    final_report: str

class ResearcherState(TypedDict):
    sub_query: str                           # 本子任务的问题
    messages: Annotated[list, add]           # ReAct 对话（operator.add）
    tool_call_iterations: int                # ReAct 预算
    notes: Annotated[list[str], add]
    sources: Annotated[list[dict], add]

class ResearcherOutputState(TypedDict):       # 只上报这些（不爆 supervisor 上下文）
    notes: list[str]
    sources: list[dict]
```
关键：researcher 的完整 ReAct messages 不上浮到 AgentState，只有 notes+sources 累加（输入/输出分离）。

## 3. 图拓扑（缝合 open_deep_research `deep_researcher.py` supervisor 模式）
```
START → planner → fan_out_researchers(asyncio.gather) → synthesizer → END
```
- `planner`：调用 DeepSeek，Pydantic 结构化输出 ResearchPlan(sub_queries: list[str])。
- `fan_out_researchers`：对每个 sub_query 起 researcher 子图，`asyncio.gather` 并发，
  上限 `max_concurrent_researchers`（默认 3）。每子任务合并 notes+sources 回 AgentState。
- `synthesizer`：把所有 notes + sources 喂 DeepSeek，产出结构化综述（markdown + 来源列表）。

注：本 change 不做 critic 回环（reviewer⇄reviser），仅硬预算兜底。critic 留待后续 change
（避免一次性引入过多复杂性；先验证 supervisor 主干通）。

## 4. researcher ReAct 子图
```python
# nodes/researcher.py
researcher_graph:
  START → react_agent ⇄ tool_node → extract_notes → END
```
- `react_agent`：DeepSeek complete_with_tools（保留 reasoning），工具=[search_papers]。
- `tool_node`：执行 tool_calls，调 datasources.tools.search_papers(query, max_results)。
- `tool_call_iterations` 预算（默认 3）：超出则强制进 extract_notes。
- `extract_notes`：让 DeepSeek 把 ReAct 历史压缩成针对 sub_query 的笔记。

## 5. 工具包装（datasources.search → Function Calling）
`agent/tools.py`：
```python
SEARCH_PAPERS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_papers",
        "description": "搜索 CS 论文。返回标题/作者/年份/引用数/doi 等。",
        "parameters": {"type":"object","properties":{
            "query":{"type":"string"},"max_results":{"type":"integer"}}, "required":["query"]},
    },
}

async def execute_tool(tool_name, args) -> str:
    if tool_name == "search_papers":
        results = await registry.search(args["query"], max_results=args.get("max_results",5))
        # 同步入库（本地库存约束）
        await sync_to_async(_upsert_batch)(results)
        return json.dumps([_to_brief(r) for r in results], ensure_ascii=False)
```

## 6. 配置（agent/config.py）
```python
@dataclass
class AgentConfig:
    max_sub_queries: int = 3              # planner 最多分解几个子查询
    max_concurrent_researchers: int = 3   # 并行 researcher 上限
    max_tool_calls_per_researcher: int = 3  # ReAct 工具调用预算
    planner_model: str = "deepseek-v4-flash"
    researcher_model: str = "deepseek-v4-flash"
    synthesizer_model: str = "deepseek-v4-flash"
```
planner/synthesizer 用 thinking=False 降本（结构化输出/汇总不需要思维链）；
researcher 的 react_agent 用 thinking=True（Function Calling 保留 reasoning，地基验证已证）。

## 7. 验证项（tasks 末项）
`python -m agent.runner "Mamba 状态空间模型最新进展"`：
- planner 产出 ≥1 个 sub_query
- researcher 调用 search_papers ≥1 次（真实 Function Calling）
- sources 累加 ≥3 条真实论文
- synthesizer 产出带来源的 markdown 综述
- 全程不崩，token 在预算内

## 8. 测试（agent/tests.py）
- planner：mock DeepSeek 返回固定 ResearchPlan，断言 sub_queries 提取正确
- researcher：mock datasources.search，断言 tool 执行 + notes 提取
- tools.execute_tool：真实调 datasources（openalex）断言返回 JSON + 入库
- 端到端：mock planner（固定 plan）+ 真实 researcher（真实 DeepSeek + 真实 openalex）跑通
