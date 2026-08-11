# Proposal: harden-project-scope-and-evidence

## Summary

建立统一的项目授权边界和证据真实性契约，修复项目级工具、RAG、报告、引用图谱
和 MCP 之间各自实现 scope/evidence 语义的问题。本 change 保留所有现有用户功能
和主要公开 API，只收紧内部查询、工具执行和质量判定。

## Motivation

完整性审计已经真实复现：项目 A 可以通过 `read_paper_section` 读取仅属于项目 B
的全文 chunk。当前 citation `reference_resolved` 也可能只凭 Harness 组装的字段为真，
没有重新验证数据库 chunk、项目 membership 和 content hash。与此同时，fulltext
安全门把 metadata 当作足够证据，并遗漏 `compare_papers` 和
`read_paper_section` 的全文结果。

这些问题不能继续由各工具追加零散 filter 或评测关键词修补。需要建立唯一可信的
执行上下文、项目 scope resolver、证据 envelope 和引用解析器。

## Scope

- 新增不可变 `ToolExecutionContext`，项目身份由服务端注入。
- 新增唯一 `ProjectScopeResolver`，集中处理 membership 和 excluded 状态。
- 新增 `EvidenceEnvelope`，统一 RAG、read、compare 和 report 的全文证据。
- 新增数据库驱动的 `CitationResolution`。
- 拆分 retrieval、reference resolution、citation binding 和 claim support 状态。
- 将 capability-aware evidence policy 写成显式矩阵。
- Project tools、Harness、MCP、RAG、Citation Graph 和 Report 迁移到同一边界。
- 增加跨项目、伪引用和 metadata bypass 的负向测试。

## Non-Goals

- 不实现 A1 自动下载/入库；PDF 安全由独立 change 处理。
- 不重写 Chat/SSE 协议或 LangGraph workflow。
- 不替换 embedding、vector store、Agent runtime 或 LLM provider。
- 不在本 change 中实现 claim-level LLM Judge；只定义其输入和状态边界。
- 不增加多用户认证系统；project scope 仍必须在当前单用户 demo 中正确隔离。

## Public Compatibility

- 保留 `query_project_rag(project_id, question, k)`。
- 保留现有项目 API 路径和 SSE 事件名。
- 工具结果可新增 `evidence` 和结构化状态字段，不删除当前前端依赖字段。
- 模型可见工具 schema 将删除 `project_id`；这是安全收紧，不是用户 API 变更。
- MCP read tools 保持名称，内部改用统一 resolver 和 output schema。

## Release Gate

本 change 未通过全部项目工具/MCP 参数化隔离测试、伪引用反向测试和 Docker
PostgreSQL 回归前，不得开始自动入库或真实 Agent 扩展测试。
