# Design: setup-project-foundation

> 每个设计决策标明 SOTA 出处或地基验证依据，不凭空设计。

## 1. 目录结构
```
backend/
├── manage.py
├── .env                       # 已存在（DEEPSEEK_API_KEY 等）
├── .gitignore                 # 已存在
├── verify_ground.py           # 已存在（地基验证，保留）
├── requirements.txt
├── config/
│   ├── __init__.py
│   ├── settings.py            # Daphne 首项 + ASGI + SQLite + GZip 排除 SSE
│   ├── urls.py
│   ├── asgi.py                # ASGI_APPLICATION 指向
│   └── wsgi.py
├── papers/                    # Django app：本地论文库存
│   ├── models.py              # Paper/Author/Venue
│   ├── admin.py
│   ├── migrations/
│   └── apps.py
├── datasources/               # 论文数据源工具层（Function Calling 工具）
│   ├── __init__.py
│   ├── base.py                # PaperSearcher Protocol + 统一 search 契约
│   ├── cache.py               # SQLite 缓存层
│   ├── ratelimit.py           # 429 指数退避 + Retry-After
│   ├── openalex.py            # 主源（元数据+referenced_works+摘要）
│   ├── arxiv.py               # 预印本+PDF
│   ├── dblp.py                # 会议/作者（带 User-Agent 修 SSL）
│   ├── semantic_scholar.py    # 匿名层备用（引用数补全）
│   ├── registry.py            # 源注册 + 并发去重（gpt-researcher set().union 模式）
│   └── smoke.py               # 端到端验证入口
└── llm/
    ├── __init__.py
    └── deepseek.py            # DeepSeek 客户端：reasoning 开关 + 重试
```

## 2. 数据源统一契约（缝合 gpt-researcher）
gpt-researcher 的 retriever 接口是该项目里最干净的插件契约，直接采用：
- 来源：`gpt_researcher/retrievers/__init__.py`，已有 arxiv/openalex/semantic_scholar 实现。
- 落地（`datasources/base.py`）：
```python
from typing import Protocol
class PaperSearcher(Protocol):
    name: str
    async def search(self, query: str, max_results: int = 10) -> list[dict]:
        """返回统一结构 [{source, source_id, title, abstract, year, authors,
           venue, citation_count, pdf_url, referenced_works, raw}]"""
```
- 去重（`registry.py`，缝合 gpt-researcher `context_manager.py`）：多源结果按 `source_id`（优先 DOI/arxiv_id，否则 title 归一化）`dict` 去重，取并集截断 `max_results`。

## 3. 缓存层（本地库约束 + 防限流）
- SQLite 表 `datasource_cache(source, query_hash, payload_json, fetched_at)`，key=`(source, query_hash)`。
- 命中即返回，TTL 默认 7 天（论文元数据稳定）。`referenced_works` 也一并缓存。
- 这同时满足"本地库存"——抓过的论文永不重抓。

## 4. 限流退避（地基验证的硬需求）
- `ratelimit.py`：统一装饰器，捕获 HTTP 429，读 `Retry-After` 头，指数退避（1/2/4/8s，上限 30s），最多 4 次。
- 每源独立的最小间隔：OpenAlex 无强制（礼貌池 10/s，带 mailto）；ArXiv 强制 ≥3s/请求；DBLP 礼貌 1/s；S2 匿名层 ≥3s/请求（共享池 100/5min）。

## 5. DeepSeek 客户端（封装地基发现）
`llm/deepseek.py`——基于 openai sdk（DeepSeek 是 OpenAI 兼容端点）：
- 从 `os.environ["DEEPSEEK_API_KEY"]` 读 key，绝不硬编码。
- 默认 `model="deepseek-v4-flash"`，`base_url` 从 env。
- **reasoning 开关**（地基验证发现）：
  - `complete(messages, thinking=False)`：`thinking=False` 时传 `extra_body={"thinking":{"type":"disabled"}}` 关闭降本，用于简单生成/工具调用结果处理。
  - `thinking=True`（默认）保留思维链，用于 Agent 决策/复杂规划。
- `complete_with_tools()`：支持 Function Calling（`tools`/`tool_choice`），保留 reasoning（模型"想清楚"再调工具更准）。
- 重试：5xx/超时 指数退避 3 次。

## 6. Django 配置要点（为 SSE 铺路，现在就埋好）
- `INSTALLED_APPS` 第一项 `"daphne"`（ASGI；让 runserver 变 async，未来 SSE 不缓冲）。
- `ASGI_APPLICATION = "config.asgi.application"`。
- `MIDDLEWARE` 中 `GZipMiddleware` **保留但对 SSE 路由禁用**（本 change 先注释说明，SSE change 再细化中间件）——为避免现在过度设计，本 change 仅确保 ASGI 就绪，GZip 处理留到 SSE change。
- SQLite 默认库 `db.sqlite3`（也用作缓存表）。

## 7. ORM 模型（本地库存）
`papers/models.py`：
```python
class Paper(models.Model):
    s2_id = models.CharField(max_length=64, null=True, db_index=True)
    openalex_id = models.CharField(max_length=64, null=True, db_index=True)
    doi = models.CharField(max_length=128, null=True)
    arxiv_id = models.CharField(max_length=32, null=True, db_index=True)
    title = models.TextField()
    abstract = models.TextField(blank=True)
    year = models.IntegerField(null=True)
    citation_count = models.IntegerField(default=0)
    referenced_works = models.JSONField(default=list)   # ★护城河数据落库
    pdf_url = models.URLField(null=True)
    raw = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta: constraints = [UniqueConstraint("doi"), UniqueConstraint("arxiv_id")]
```
`Author`/`Venue` 标准字段（name/affiliation；venue 名/类型）。爬到的论文 `upsert` 进库，护城河的 `referenced_works` 直接落 `JSONField`，建图时从库读。

## 8. 验证项（tasks 最后一项）
`python -m datasources.smoke`：
- 从四源各搜一条 "transformer attention"，打印 title/year/有无 referenced_works。
- 第二次跑同查询，打印 `cache hit`，确认不重抓。
- `manage.py runserver` 起来访问根路由返回 200。
