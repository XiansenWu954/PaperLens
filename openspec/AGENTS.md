# PaperLens — AGENTS.md（编码范式宪法）

> 本文件是项目所有 AI/人类协作者的**最高工作规范**，优先级高于其他文档。
> 它把 OpenSpec 的 spec-driven 范式 + 本项目的硬约束固化下来，约束后续一切编码行为。

## 1. 工作循环（OpenSpec 范式）
任何功能编码**必须**先有对应的 change，且走完这个循环，顺序不可跳过：

1. **Explore**：先读代码、读地基验证结果、读 SOTA 参考，搞清楚现状再动手。
2. **Propose**：在 `openspec/changes/<change-id>/` 下手写四件套（`proposal.md` / `design.md` / `tasks.md` / `specs/<capability>.md`）。**未批准前不写功能代码。**
3. **Review**：把四件套交给用户批准。用户明确批准后才进入下一步。
4. **Apply**：严格按 `tasks.md` 清单逐项实现，每完成一项勾选一项。
5. **Archive**：change 完成且验证通过后，把 specs 合并进 `openspec/specs/`（当前真相源），change 移入 `changes/archive/`。

**铁律**：没有 spec 不编码；没有批准不 apply；不在 spec 外擅自加功能。

## 2. 项目硬约束（来自用户，不可违背）
- **LLM**：只用 DeepSeek 作为 LLM provider。密钥从环境注入（根 `.env` 是 Docker Compose 输入，`backend/.env` 是独立 Django 输入），**绝不硬编码进源码，绝不打印**。具体模型名从配置读取（当前 `deepseek-v4-flash` / `deepseek-v4-pro`），短期可用型号不作为永久架构硬约束。
- **零额外成本**：数据源只用免费的（OpenAlex/ArXiv/DBLP），**不注册任何付费/需 key 的服务**。Semantic Scholar 仅匿名层低频使用。
- **本地库**：所有抓取的论文/元数据/图谱节点持久化到本地数据库（PostgreSQL 主路径），**同份数据绝不重复请求**（既是防限流，也是"本地库存"要求）。
- **Agent 边界**：普通项目 Chat 使用 deterministic router + bounded ReAct Harness；LangGraph
  仅负责需要 checkpoint/wait/resume 的显式长流程。DeepSeek 是唯一 LLM provider。
- **Reasoning 控制**：V4-Flash 默认带思维链（耗 token）。预算敏感节点（简单工具调用/纯生成）用 `thinking={"type":"disabled"}` 关闭降本。
- **评测覆盖**：DeepSeek 调用成本不得作为缩减真实评测覆盖的理由；模型、模式、token、调用
  次数、延迟、失败率和停止原因仍必须完整记录。

## 3. 已验证的地基事实（spec 的前提，不得臆造）
| 事实 | 验证结果 | 出处 |
|---|---|---|
| DeepSeek 可用模型 | `deepseek-v4-flash` / `deepseek-v4-pro` | `verify_ground.py` 检查1 |
| V4-Flash 默认 reasoning | 约半数 completion token 花在思考上 | 同上 |
| 关闭 reasoning | `thinking={"type":"disabled"}` 有效 | 同上 |
| OpenAlex `referenced_works` | ✅ 返回完整引用列表（样本 95 条） | 检查2，**护城河零成本数据通路成立** |
| OpenAlex 摘要 | 部分论文 `abstract_inverted_index` 为空，需 ArXiv/字段补全 | 检查2 |
| ArXiv | ✅ 元数据可用，PDF 链接需修正则 | 检查3 |
| DBLP | ✅ 可用，需加 `User-Agent` 头修 SSL EOF | 检查4 |
| 环境 | Windows / Python 3.11+ / Node 22+ / Docker Desktop | 环境验证 |

## 4. 架构参考纪律（SOTA 是证据，不是强制复刻目标）
| 模块 | SOTA 参考 | 复刻什么 |
|---|---|---|
| 多智能体编排 | open_deep_research (当前 main) | supervisor 模式 + asyncio.gather 并行 + 4类state+reducer + 输入/输出state分离 |
| 全文 RAG | PaperQA2 (future-house/paper-qa) | Docs→Text→Context 三层 + RCS(LLM reranker) + pqac 引用格式 |
| 引用图谱 ★护城河 | Connected Papers 算法 | co-citation + bibliographic coupling 相似图 + pagerank/年衰减/聚类 |
| 工具层 | gpt-researcher retriever 契约 | `search()->[{href,body,title}]` + 去重 + 缓存退避 |
| MCP 双向 | open_deep_research + gpt-researcher mcp | 消费现成 mcp + 导出自己工具 |
| 实时流 | Django 4.2 async SSE | StreamingHttpResponse + X-Accel-Buffering:no + ASGI(Daphne) + astream 桥接 |

**原则**：写每个模块前，在 `design.md` 记录参考方案解决了什么已复现问题、预期收益、
同数据基线和回滚边界。不得因为简历展示或概念完整性照搬框架；没有量化收益时保留现有
确定性路径。

## 5. 技术栈（V3 已定）
- 后端：Django 4.2+ + DRF + Daphne(ASGI)
- 主数据库/向量：PostgreSQL + pgvector（集成/演示主路径）；SQLite 仅限本地 fallback 或隔离单元测试
- 队列：Redis + Celery（PDF 解析、embedding、外部检索和长任务工作单元）
- LLM：DeepSeek（模型名从环境配置读取，当前 `deepseek-v4-flash`/`deepseek-v4-pro`）
- Embedding：BGE-M3（dense + sparse），维度 1024，version 随索引记录；测试默认 fake provider
- Agent：deterministic router（稳定基线）+ bounded ReAct Harness（开放式工具调用）
- 长流程：LangGraph（仅用于可恢复长任务：checkpoint/waiting/resume），普通 Chat 不图化
- 图谱：networkx（bibliographic_coupling/cocitation/louvain 自带）
- 前端：Vue 3 + Vite + TS + Pinia + VueUse + d3-force
- MCP：导出稳定项目读能力，复用内部工具契约和授权边界

## 6. 编码规范
- Python：类型注解必填，pydantic 做配置/结构化输出，异步优先（agent 全 async）。
- 密钥/配置：只从环境变量/`.env` 读；`.env` 在 `.gitignore`，绝不提交。
- 缓存：所有外部 API 调用必经缓存层（PostgreSQL/SQLite），带 429 指数退避 + Retry-After。
- 引用忠实：综述里每条声明必须有 pqac 引用 key，LLM 只能引用注入的 Valid Keys。
- 诚实量化：任何"提升"声称必须有同评测集的严格对比支撑（吸取 AppPilot 教训）。
- 注释密度：跟随周围代码风格；中文注释 OK，但标识符用英文。

## 7. 验证纪律
- 每个 change 的 `tasks.md` 最后一项必须是"验证项"：跑测试/最小闭环，附真实输出。
- 失败如实报告，不粉饰（AppPilot 的 playbook 改进"假提升"是前车之鉴）。

## 8. change 拆分原则
- 一个 change = 一个可独立交付、可独立验证的能力，不大杂烩。
- change-id 用 `kebab-case` 动词短语，如 `add-data-source-layer`、`add-rag-pipeline`。

## 9. 协作角色与发布授权

- **Codex：产品负责人和质量监管**。冻结 OpenSpec、接口、不变量、门槛和停止条件；静态
  审查 DS 代码与证据；决定是否交 GLM；裁决规范冲突并批准归档。Codex 不以测试数量代替
  质量判断。
- **DS：开发和基础验证**。只实现已批准 OpenSpec，安全/证据问题先写红测；不得自行扩大
  范围、改变门槛或修改 GLM 独立断言；按“已实现、原始证据、完整测试、仍未完成”汇报。
- **GLM：独立测试和大规模评测**。默认不修改生产代码；独立生成攻击样例、故障注入、真实
  模型重复评测和机器可读报告；不得复用 DS 的结论或硬编码通过数字。
- 未经 Codex 明确放行，DS 不进入下一任务组，GLM 不开始验收，change 不得归档。

## 10. 规范追踪与漂移控制

- 安全边界、数据不变量、API/用户可见语义、兼容性和发布门槛必须进入 capability spec。
- 技术选择、迁移、兼容适配和回滚理由进入 `design.md`。
- 缺陷、代码修复、case ID 和状态进入 `tasks.md`；原始失败与前后对照进入
  `docs/internal/**`，不得进入公开仓库。
- 小型实现修复不写入 spec；如果重复缺陷暴露缺失的不变量，必须升级为 spec delta。
- 每个 finding 必须关联 Requirement/Scenario、代码位置、正负控制、原始产物和审批状态。
- 每阶段结束必须重新对照 proposal 的 Goals/Non-Goals，并明确记录 `NO DRIFT`、
  `DRIFT RESOLVED` 或 `BLOCKED`。
- 规范冲突优先级：当前 capability specs 与已批准未归档 delta > `AGENTS.md`/design > tasks
  和机器报告 > README/手工交接。发现冲突不得静默选择方便实现的版本，必须由 Codex 裁决。
