# Spec delta: project-foundation

## MODIFIED Requirements

### Requirement: Django 项目骨架可运行

项目必须有一个 ASGI 就绪的 Django 骨架，能启动并响应根健康路由，同时支持 V3
主数据库和显式 fallback 数据库配置。

#### Scenario: 启动开发服务器

- **WHEN** 执行 `python manage.py runserver`
- **THEN** 服务器以 ASGI 模式启动且无报错
- **AND** 访问 `/` 返回 HTTP 200 和基于 Django effective settings 的安全配置摘要。

#### Scenario: PostgreSQL 主路径迁移

- **WHEN** 在 V3 Docker/integration 环境执行 `python manage.py migrate`
- **THEN** 所有迁移 MUST 在 PostgreSQL 上成功应用
- **AND** pgvector extension 和所需索引 MUST 可用。

#### Scenario: SQLite fallback 迁移

- **WHEN** 在显式 SQLite fallback 或隔离单元测试环境执行迁移
- **THEN** 支持 SQLite 的迁移 MUST 成功应用
- **AND** 该结果 MUST NOT 被用来证明 PostgreSQL/pgvector 集成语义。

### Requirement: 论文本地库存模型

系统必须把抓取到的论文持久化到配置的本地数据库，V3 integration/demo 主路径为
PostgreSQL；同一论文不得因重复抓取产生重复全局记录。

#### Scenario: 论文 upsert

- **GIVEN** 任一数据源返回具有稳定 source ID、DOI、ArXiv ID 或归一化标题的论文
- **WHEN** 调用 papers upsert
- **THEN** 论文 MUST 写入当前配置数据库并保留来源及引用元数据
- **AND** 再次 upsert 相同规范身份时 MUST 更新或复用，而不是重复插入。

#### Scenario: 数据库实现无关性

- **WHEN** 业务层保存或读取论文库存
- **THEN** 它 MUST 使用 Django model/repository contract
- **AND** 业务逻辑 MUST NOT 依赖 `db.sqlite3` 文件路径或 SQLite-only SQL。
