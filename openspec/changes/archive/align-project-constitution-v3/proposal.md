# Proposal: align-project-constitution-v3

## Summary

校准 PaperLens 的最高协作规范，使 `openspec/AGENTS.md`、当前 capability
spec、运行配置和 V3 实现采用同一组架构事实。本 change 只修正规范和文档，
不修改业务代码、数据库或公开 API。

## Motivation

`openspec/AGENTS.md` 声明自己是项目最高规范，但仍将 SQLite、
NumpyVectorStore/Chroma 和早期 embedding 路线描述为当前技术栈；实际 V3
已经使用 PostgreSQL/pgvector、Redis/Celery 和 BGE-M3。最高规范与实现事实
冲突，会导致后续协作者即使遵守规范，也可能重新引入过期架构或错误测试前提。

本 change 建立唯一架构真相源，明确当前框架的职责边界，并规定任何未来框架
替换必须通过独立 OpenSpec change 和同数据集收益验证。

## Scope

- 更新 `openspec/AGENTS.md` 的当前技术栈、环境配置和框架使用边界。
- 新增 `project-constitution` capability spec，记录架构真相源和变更纪律。
- 对齐 `production-hybrid-rag` 等现有 spec 中已经过期的 Qwen3/BGE-M3 描述。
- 对齐 `project-foundation` 中仍将 SQLite 文件和 SQLite-only 库存写成当前 MUST 的描述。
- 明确 Docker、独立后端和测试环境的配置来源。
- 明确 deterministic router、ReAct Harness、LangGraph、Celery 和 MCP 的职责。

## Non-Goals

- 不修改任何 Python、Vue、数据库迁移或 Docker 实现。
- 不解决项目隔离、引用验证、PDF 安全或异步工作流缺陷；这些由后续 change 处理。
- 不引入 LangChain `create_agent`、Elasticsearch、独立向量数据库、Temporal 或微服务。
- 不重写历史归档 change；历史记录保持原样。

## Compatibility

本 change 没有运行时行为变化。它只使后续 change 以当前 V3 事实为基线，避免
继续引用已经失效的 V1/V2 假设。

允许对 health/status 输出进行最小一致性修复，但 health MUST 读取 Django effective
settings，而不是重新解释原始环境变量。

## Approval Gate

本 change 获得用户批准并归档前，不得开始 `harden-project-scope-and-evidence`
的业务代码实现。
