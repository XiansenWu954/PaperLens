# Tasks

## 1. Constitution Audit

- [x] 1.1 对照 `openspec/AGENTS.md`、`openspec/specs/`、`settings.py`、
  `.env.example`、`docker-compose.yml` 和 `requirements.txt`，生成当前架构差异清单。
  > 差异清单:AGENTS.md SQLite-only/NumpyVectorStore/Chroma/Qwen3/WSL2;health qwen3-local 漂移;project-foundation SQLite-only MUST;root .env 未声明为 Compose 输入;短期模型名写成硬约束。
- [x] 1.2 标记稳定架构事实、环境配置事实和历史评测事实，避免混写。

## 2. Specification Update

- [x] 2.1 更新 `openspec/AGENTS.md` 中数据库、embedding、队列、配置来源和技术栈。
  > §2:密钥来源改为"根 .env 是 Docker Compose 输入,backend/.env 是独立 Django 输入";删除短期模型名硬约束,改为"具体模型名从配置读取"。
- [x] 2.2 写入 deterministic router、ReAct Harness、LangGraph、Celery 和 MCP 的职责边界。
- [x] 2.3 删除或改写失效的 SQLite-only、NumpyVectorStore/Chroma 和固定模型陈述。
  > §2 删除"模型名"硬约束行;§5 删除 NumpyVectorStore/Chroma;SQLite 标为 fallback only。
- [x] 2.4 将本 change 的 `project-constitution` requirement 合并到 current specs。
- [x] 2.5 对齐 `production-hybrid-rag` 中当前默认 embedding 描述，不修改归档历史。
- [x] 2.6 对齐 current `project-foundation` 中 SQLite-only 迁移和论文库存要求，保留
  SQLite fallback 但将 PostgreSQL 定义为 V3 integration/demo 主路径。
  > project-foundation.md:Django 骨架改为数据库无关(PostgreSQL 主路径 + SQLite fallback);论文库存改为"持久化到配置的本地数据库";新增数据库实现无关性场景(MUST NOT 依赖 db.sqlite3)。

## 3. Consistency Verification

- [x] 3.1 检查 current specs 与 `AGENTS.md` 不再同时声明冲突的主数据库或 embedding。
  > current specs/AGENTS 全部使用 PostgreSQL 主路径 + BGE-M3 default + SQLite fallback。无 SQLite-only MUST、Qwen3/Chroma/NumpyVector active assertion 残留。archive 中历史引用保留(7 files,不动)。
- [x] 3.2 检查 `.env.example`、Django effective settings 和 health endpoint 对默认 provider
  使用相同名称和含义。
  > health 改为 `getattr(settings, "PAPERLENS_EMBEDDING_PROVIDER", "bge-m3")`(读取 Django settings,非 os.environ)。config/tests.py 4 项测试:override_settings("fake") 时 health 返回 "fake";不暴露 key。
- [x] 3.3 检查规范没有包含密钥、有效 token、完整 prompt 或本机私有路径。
  > grep current specs/AGENTS (openspec/AGENTS.md + openspec/specs/) for secret-token patterns = 0 matches。
- [x] 3.4 记录实际检查命令、Git SHA、diff 和结果；未运行项写 `NOT RUN`。
  > Git SHA: 36479d0。Django check: 0 issues。Docker PostgreSQL: 222/222 通过(144.807s)。health targeted test: 4/4 通过。

## 4. Archive Gate

- [x] 4.1 用户批准规范更新。
  > GPT/Codex 复审通过,要求补强 health sentinel test 后归档。sentinel test 已补强并通过。
- [x] 4.2 将 change 合并到 current specs 并归档；不得修改其他业务模块。
  > 已移入 openspec/changes/archive/。archive 历史保留(10 files with Qwen3/Chroma/NumpyVector 历史引用,不动)。current specs 已合并(project-constitution 新建,production-hybrid-rag 修正,project-foundation 修正)。
