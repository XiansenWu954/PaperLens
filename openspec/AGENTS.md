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
- **LLM**：只用 DeepSeek，唯一密钥从 `backend/.env` 的 `DEEPSEEK_API_KEY` 读，**绝不硬编码进源码，绝不打印**。
- **零额外成本**：数据源只用免费的（OpenAlex/ArXiv/DBLP），**不注册任何付费/需 key 的服务**。Semantic Scholar 仅匿名层低频使用。
- **本地库**：所有抓取的论文/元数据/图谱节点持久化到本地 SQLite，**同份数据绝不重复请求**（既是防限流，也是"本地库存"要求）。
- **自建 Agent**：多智能体用 LangGraph 从零自建，DeepSeek 是唯一 LLM。
- **模型名**：当前可用模型为 `deepseek-v4-flash` / `deepseek-v4-pro`，**旧名 `deepseek-chat`/`deepseek-reasoner` 已停用，禁用**。
- **Reasoning 控制**：V4-Flash 默认带思维链（耗 token）。预算敏感节点（简单工具调用/纯生成）用 `thinking={"type":"disabled"}` 关闭降本。

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
| 环境 | WSL2 / Python 3.12.3 / Node v22.22.0 / 代理 7897 | 环境验证 |

## 4. 架构缝合依据（每个模块锚定一个 SOTA，不得凭空设计）
| 模块 | SOTA 参考 | 复刻什么 |
|---|---|---|
| 多智能体编排 | open_deep_research (当前 main) | supervisor 模式 + asyncio.gather 并行 + 4类state+reducer + 输入/输出state分离 |
| 全文 RAG | PaperQA2 (future-house/paper-qa) | Docs→Text→Context 三层 + RCS(LLM reranker) + pqac 引用格式 |
| 引用图谱 ★护城河 | Connected Papers 算法 | co-citation + bibliographic coupling 相似图 + pagerank/年衰减/聚类 |
| 工具层 | gpt-researcher retriever 契约 | `search()->[{href,body,title}]` + 去重 + 缓存退避 |
| MCP 双向 | open_deep_research + gpt-researcher mcp | 消费现成 mcp + 导出自己工具 |
| 实时流 | Django 4.2 async SSE | StreamingHttpResponse + X-Accel-Buffering:no + ASGI(Daphne) + astream 桥接 |

**原则**：写每个模块前，先在 design.md 里标明"这个模式来自哪个 SOTA 的哪个文件/类"，再落地。不抄则必须有明确理由。

## 5. 技术栈（已定）
- 后端：Django 4.2+ + DRF + Daphne(ASGI) + LangGraph + SQLite
- LLM：DeepSeek-V4-Flash（OpenAI 兼容端点，litellm 或 openai sdk）
- 向量：起步 NumpyVectorStore（PaperQA2 方案）/ 量大升 Chroma
- 图谱：networkx（bibliographic_coupling/cocitation/louvain 自带）+ cdlib Leiden
- 前端：Vue 3 + Vite + TS + Pinia + VueUse + d3-force/force-graph

## 6. 编码规范
- Python：类型注解必填，pydantic 做配置/结构化输出，异步优先（agent 全 async）。
- 密钥/配置：只从环境变量/`.env` 读；`.env` 在 `.gitignore`，绝不提交。
- 缓存：所有外部 API 调用必经缓存层（SQLite），带 429 指数退避 + Retry-After。
- 引用忠实：综述里每条声明必须有 pqac 引用 key，LLM 只能引用注入的 Valid Keys。
- 诚实量化：任何"提升"声称必须有同评测集的严格对比支撑（吸取 AppPilot 教训）。
- 注释密度：跟随周围代码风格；中文注释 OK，但标识符用英文。

## 7. 验证纪律
- 每个 change 的 `tasks.md` 最后一项必须是"验证项"：跑测试/最小闭环，附真实输出。
- 失败如实报告，不粉饰（AppPilot 的 playbook 改进"假提升"是前车之鉴）。

## 8. change 拆分原则
- 一个 change = 一个可独立交付、可独立验证的能力，不大杂烩。
- change-id 用 `kebab-case` 动词短语，如 `add-data-source-layer`、`add-rag-pipeline`。
