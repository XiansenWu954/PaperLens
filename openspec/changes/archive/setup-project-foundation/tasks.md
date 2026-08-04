# Tasks: setup-project-foundation

> 实现清单。逐项执行，完成一项勾选一项。所有项基于 design.md 的缝合依据。

- [x] 1. 建虚拟环境与依赖
  - [x] 1.1 `python3 -m venv .venv` 在 backend/
  - [x] 1.2 写 `requirements.txt`
  - [x] 1.3 `pip install -r requirements.txt`
  - [x] 1.4 验证：`import django,daphne,openai,httpx,networkx` ✓（django 5.0.14）

- [x] 2. Django 项目骨架
  - [x] 2.1 `django-admin startproject config .` + startapp papers/datasources
  - [x] 2.2 settings.py：INSTALLED_APPS 首项 "daphne"；ASGI_APPLICATION
  - [x] 2.3 settings.py：SQLite + dotenv 读 .env
  - [x] 2.4 根路由 health view
  - [x] 2.5 验证：runserver 起，`/` 返回 200 ✓（Daphne 接管 reloader，ASGI 就绪）

- [x] 3. 论文本地库存模型（papers app）
  - [x] 3.1 Paper（含 referenced_works JSONField ★）/Author/Venue
  - [x] 3.2 UniqueConstraint on doi/arxiv_id/openalex_id
  - [x] 3.3 `upsert_paper(data)`
  - [x] 3.4 admin.py 注册
  - [x] 3.5 makemigrations && migrate ✓
  - [x] 3.6 验证：shell upsert 同 doi 更新不重复，referenced_works 可读 ✓

- [x] 4. 缓存与限流（datasources 基础设施）
  - [x] 4.1 DatasourceCache model + get/set，TTL 7 天
  - [x] 4.2 ratelimit.fetch_json：429 Retry-After / 指数退避，上限 4 次
  - [x] 4.3 每源最小间隔（ArXiv 3s、S2 3s、DBLP 1s、OpenAlex 无）
  - [x] 4.4 验证：缓存命中第二轮无新请求 ✓
  - [x] 4.5 异步安全：cached_search 用 sync_to_async 包裹 ORM 调用 ✓（修复 SynchronousOnlyOperation）

- [x] 5. 数据源统一契约与四源实现
  - [x] 5.1 base.py：PaperSearcher Protocol + 归一化 + dedupe_source_id
  - [x] 5.2 openalex.py：搜索 + referenced_works + 摘要倒排索引解码
  - [x] 5.3 arxiv.py：搜索 + PDF 链接（修正则）
  - [x] 5.4 dblp.py：curl 子进程兜底（httpx 代理 TLS 不可达）
  - [x] 5.5 semantic_scholar.py：匿名层备用
  - [x] 5.6 registry.py：并发 + 去重（gpt-researcher set().union 模式）
  - [x] 5.7 验证：openalex 2 条(140 refs) ✓、arxiv 2 条 ✓、多源去重 5 条 ✓
  - [x] 5.8 **DBLP 发现**：本机代理对 dblp.org TLS 握手不可达（curl 直连亦 SSL_ERROR_SYSCALL）。
        OpenAlex 已提供 venue/author 可替代；DBLP 从 DEFAULT_SOURCES 移除，实现保留。

- [x] 6. DeepSeek 客户端（llm 包）
  - [x] 6.1 llm/deepseek.py：openai sdk，env 读 key/base_url/model
  - [x] 6.2 complete(thinking=False)：extra_body={"thinking":{"type":"disabled"}}
  - [x] 6.3 complete_with_tools：保留 reasoning，返回 tool_calls
  - [x] 6.4 5xx/超时 重试 3 次
  - [x] 6.5 验证：complete("1+1",thinking=False) 返回 "2"，reasoning_tokens=None ✓

- [x] 7. 端到端 smoke 验证
  - [x] 7.1 datasources/smoke.py
  - [x] 7.2 真实输出（见下方附录）
  - [x] 7.3 runserver 起 + / 200 ✓

- [x] 8. 归档
  - [x] 8.1 tasks 全部勾选
  - [x] 8.2 specs 合并进 openspec/specs/project-foundation.md
  - [x] 8.3 change 移入 archive/
  - [x] 8.4 git init + 首次提交（确认 .env 未追踪）

---

## 附录：smoke 真实输出（2026-07-30）

```
============================================================
PaperLens 地基 smoke 验证
============================================================

--- 1. 四源检索 ---
[openalex] ✓ count=2 title='Swin Transformer...' refs=140
[arxiv] ✓ count=2 title='Dilated Neighborhood Attention Transformer'

--- 2. 缓存命中 ---
[cache] openalex 第二轮命中缓存（fetched_at 未变）: True

--- 3. DeepSeek (thinking=False) ---
[deepseek] thinking=False content='2' reasoning_tokens=None reasoning=无

--- 4. papers upsert ---
[upsert] 同 doi 更新不重复: True, 落库后总数=1, referenced_works 落 JSONField OK

============================================================
汇总
============================================================
  openalex: ✓ (2 条) [关键]
    referenced_works 护城河数据: ✓
  arxiv: ✓ (2 条) [关键]
  dblp: ✗ (尽力而为,不影响) (0 条) [可选]
  缓存命中: ✓
  DeepSeek thinking=False 降本: ✓
  papers upsert: ✓
============================================================
关键路径全部通过 ✓
```

多源去重集成测试：`search("attention is all you need")` → 5 条，含经典论文 6598 引用。
runserver：`/` 返回 `{"status": "ok"}` HTTP 200，Daphne 接管 reloader（ASGI 就绪）。

---

## 附录2：spec 弱验证场景补齐证据（2026-07-30）

spec Requirement 6 两个原本仅有代码、无执行证据的场景，经 `verify_function_calling.py` 实跑补齐：

### Requirement 6 场景「Function Calling 保留 reasoning」执行证据
```
=== 验证1: complete_with_tools Function Calling ===
  tool_calls: [{"id":"call_00_...","name":"search_papers",
                "arguments":"{\"query\":\"Mamba state space model\",\"max_results\":5}"}]
  reasoning_tokens: 51 (保留 reasoning)        ← >0，证明未传 thinking=disabled
  usage completion_tokens: 130
  调用了 search_papers: ✓
  保留 reasoning: ✓
```

### Requirement 场景「收到 429 重试」执行证据（mock server 实触发）
```
=== 验证2: 429 退避（mock server 先2次429再200）===
  test_source 429, retry after 1.0s (attempt 0)
  test_source 429, retry after 1.0s (attempt 1)
  mock 收到请求次数: 2（前2次429，第3次200）
  fetch_json 最终返回: {'ok': True, 'attempt': 2}
  耗时: 2.06s（应 ≥2s 退避等待）
  按退避重试并成功: ✓
  退避生效（耗时>1.5s）: ✓
```

---

## 附录3：正式测试套件结果（Django TestCase）

`python manage.py test papers datasources -v2` → **22 tests, OK**，覆盖：
- papers: upsert 去重(doi/arxiv/openalex)、referenced_works JSONField 持久化、Venue 唯一约束、doi 小写归一 (7)
- datasources: normalize 归一化、dedupe_source_id 优先级、cache TTL 过期/命中、query_hash 稳定性 (9)
- datasources: 429 退避成功重试 / 退避耗尽抛 RateLimitError (2)
- datasources: cached_search 缓存命中不调 fetch / 未命中调 fetch、registry 多源去重 + 单源失败不阻断 (4)

注：CachedSearchTest / RegistryDedupeTest 用 TransactionTestCase——asyncio event loop 在 TestCase
嵌套事务里并发写 SQLite 会 "database table is locked"，TransactionTestCase 真实提交避免该锁。
