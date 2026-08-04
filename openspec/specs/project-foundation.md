# Spec delta: project-foundation

## ADDED Requirements

### Requirement: Django 项目骨架可运行
项目必须有一个 ASGI 就绪的 Django 骨架，能 `manage.py runserver` 启动并响应根路由，为后续 SSE 流式端点铺路。

#### Scenario: 启动开发服务器
- **WHEN** 执行 `python manage.py runserver`
- **THEN** 服务器在 8000 端口以 ASGI 模式（Daphne 接管 runserver）启动，无报错
- **AND** 访问 `/` 返回 HTTP 200

#### Scenario: 数据库迁移就绪
- **WHEN** 执行 `python manage.py migrate`
- **THEN** 所有迁移成功应用，`db.sqlite3` 创建

### Requirement: 论文本地库存模型
系统必须把抓取到的论文持久化到本地 SQLite，作为"本地库存"，同一论文绝不重复抓取。

#### Scenario: 论文 upsert
- **GIVEN** OpenAlex 返回一篇 doi=10.xxx 的论文
- **WHEN** 调用 `papers` 的 upsert
- **THEN** 该论文写入 `papers_paper` 表，`referenced_works` 落 JSONField
- **AND** 再次 upsert 相同 doi 时更新而非重复插入

### Requirement: 统一的论文数据源工具层
四个免费源（OpenAlex/ArXiv/DBLP/S2匿名）必须实现统一 `search()` 契约，返回归一化结构，便于后续作为 Function Calling 工具和去重。

#### Scenario: 多源检索并去重
- **WHEN** 调用 `registry.search("transformer attention", sources=["openalex","arxiv"], max_results=10)`
- **THEN** 并发调用各源的 `search()`
- **AND** 结果按 source_id（DOI/arxiv_id/归一化title）去重后返回，总数 ≤ max_results

#### Scenario: 归一化字段
- **WHEN** 任一源返回论文
- **THEN** 每条含统一字段：source, source_id, title, abstract, year, authors, venue, citation_count, pdf_url, referenced_works, raw

### Requirement: 本地缓存防限流
所有外部数据源调用必经本地缓存，命中即返回，不重复请求，缓解 OpenAlex/ArXiv/S2 的限流。

#### Scenario: 缓存命中
- **GIVEN** 同一 (source, query) 已在 7 天内请求过
- **WHEN** 再次请求相同查询
- **THEN** 直接从 `datasource_cache` 表返回，不发起网络请求

### Requirement: 限流退避
遇到 HTTP 429 时，按 `Retry-After` 头或指数退避重试，不立即失败。

#### Scenario: 收到 429 重试
- **GIVEN** 数据源返回 HTTP 429 且带 `Retry-After: 2`
- **THEN** 等待 2 秒后重试
- **AND** 最多重试 4 次仍失败则抛出可识别异常（不静默吞掉）

### Requirement: DeepSeek 客户端封装
LLM 调用必须封装为统一客户端，密钥从环境变量读，支持 reasoning 开关以控制成本。

#### Scenario: 密钥从环境读取
- **WHEN** 初始化 DeepSeek 客户端
- **THEN** 从 `DEEPSEEK_API_KEY` 环境变量读取密钥
- **AND** 源码中不出现明文密钥

#### Scenario: 关闭 reasoning 降本
- **WHEN** 调用 `complete(messages, thinking=False)`
- **THEN** 请求体携带 `thinking={"type":"disabled"}`
- **AND** 响应不含 `reasoning_content`，token 消耗不含 reasoning_tokens

#### Scenario: Function Calling 保留 reasoning
- **WHEN** 调用 `complete_with_tools(messages, tools)`
- **THEN** 保留 reasoning（不传 disabled），模型先思考再决定工具调用

### Requirement: 端到端地基验证
地基交付时必须有一条命令验证四源可用且缓存生效。

#### Scenario: smoke 测试通过
- **WHEN** 执行 `python -m datasources.smoke`
- **THEN** 四源各成功返回 ≥1 条归一化论文
- **AND** 第二轮同查询打印 `cache hit`，无网络请求
