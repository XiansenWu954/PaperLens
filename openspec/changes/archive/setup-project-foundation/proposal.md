# Change: setup-project-foundation

## Why（为什么做）
后续所有能力（多智能体、RAG、引用图谱、MCP、前端）都依赖同一套地基：
1. 一个能跑的 Django 项目骨架（含 ASGI，为 SSE 铺路）。
2. 一个统一的论文数据源工具层——四个免费源（OpenAlex/ArXiv/DBLP/S2匿名）都要带**本地缓存 + 限流退避**，否则跑几轮就被限流封死，也违背"本地库"约束。
3. 一个 DeepSeek 客户端封装，屏蔽 reasoning 开关、统一错误处理。

地基不打好，上层模块会反复踩同样的限流/模型名/reasoning 耗 token 的坑。

## What（改什么）
- 新建 Python venv，装最小依赖（django/daphne/drf/openai/litellm/pydantic/httpx/networkx）。
- 新建 Django 项目 `config/`，ASGI 就绪（Daphne 设为 INSTALLED_APPS 第一项），SQLite，SSE 路由排除 GZipMiddleware。
- 新建 `papers` app：`Paper`/`Author`/`Venue` 三个 ORM 模型（本地库存）。
- 新建 `datasources` 包：四源统一 `search()` 契约（gpt-researcher 模式）+ SQLite 缓存层 + 429 指数退避。
- 新建 `llm` 包：DeepSeek 客户端，封装 reasoning 开关 + 重试。
- 一个 `manage.py runserver` 能起、一个 `python -m datasources.smoke` 能从四源各取一条并命中缓存的端到端验证。

## Out of scope（本 change 不做）
- 多智能体编排（下一 change）
- RAG 全文/PDF 解析（下一 change）
- 引用图谱构建（独立 change）
- 前端
- DRF/SSE 端点（地基只保证 Django 能跑，端点在数据源 ready 后的 change）

## 风险
- DBLP SSL EOF（已验证需加 User-Agent 头）。
- ArXiv 2026 起 429 变严——缓存层是缓解，但首取仍要低频。
- DeepSeek reasoning 默认开——客户端默认对"简单调用"关 reasoning，"Agent 决策"开 reasoning，由调用方指定。
