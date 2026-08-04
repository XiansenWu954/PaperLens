# Spec delta: rag-pipeline

## ADDED Requirements

### Requirement: PDF 全文入库
系统必须能下载 ArXiv PDF、解析为文本、段落级切片、嵌入后存为 Text，挂到 Paper。

#### Scenario: 入库一篇论文
- **GIVEN** 一篇有 pdf_url 的 Paper
- **WHEN** 调用 ingest_paper
- **THEN** 下载 PDF → 解析 → 切片 → 每片嵌入存 Text
- **AND** 重复 ingest 同一 paper 不重复切片

### Requirement: 段落级切片
切片必须接近 PaperQA2 最优（~9000 字符段落级，重叠 250）。

#### Scenario: 切片参数
- **WHEN** 对一段长文本切片
- **THEN** 切片约 ≤ chunk_chars 字符，相邻片段有 overlap 重叠
- **AND** 每片为完整段落边界（不硬截断词）

### Requirement: 本地嵌入
embedding 必须用本地模型（零成本、无 GPU、无 API key），输出归一化向量。

#### Scenario: 嵌入维度
- **WHEN** 调用 embed
- **THEN** 返回 (n, 384) 归一化向量
- **AND** 相似语义的文本余弦相似度高

### Requirement: 向量检索
必须能用 Numpy 向量库对查询召回 top-k 相关切片。

#### Scenario: 召回
- **GIVEN** 已入库若干 Text
- **WHEN** 用查询向量检索 k=20
- **THEN** 返回余弦相似度 top-20 的 Text

### Requirement: RCS 重排序
召回后必须用 LLM 给每候选切片打 1-10 相关性分 + 针对性摘要，过滤 ≤1（缝合 PaperQA2 RCS）。

#### Scenario: RCS 评分过滤
- **GIVEN** 召回 20 候选
- **WHEN** retrieve_evidence 执行
- **THEN** 每候选得 1-10 分
- **AND** 返回 score>1 的 top-k 证据，含针对性摘要

### Requirement: pqac 引用格式
证据必须有 `pqac-{md5[:8]}` 引用 key，综述里用该 key 标注，可回引到论文。

#### Scenario: 引用 key 稳定
- **WHEN** 对同一 paper 的 Text 生成 citation_key
- **THEN** 返回稳定的 pqac-8位 串
- **AND** parse_citations 能从文本提取所有 pqac 串

### Requirement: gather_evidence 工具
researcher 必须能通过 gather_evidence 工具对已检索论文做全文 RAG 取证据。

#### Scenario: 工具调用
- **WHEN** researcher 调用 gather_evidence(question)
- **THEN** 对已入库论文做 retrieve_evidence
- **AND** 返回带 pqac 引用的证据 JSON

### Requirement: 端到端验证
必须有一条命令验证完整 RAG 管线。

#### Scenario: smoke 通过
- **WHEN** 执行 `python -m rag.smoke`
- **THEN** 下载 PDF → 解析 → 切片 → 嵌入 → 检索 → RCS → 返回带 pqac 证据

### Requirement: Local PDF byte ingestion
The RAG pipeline MUST support ingesting already-available PDF bytes, not only downloading from a remote `pdf_url`.

#### Scenario: Ingest generated or uploaded PDF bytes
- **GIVEN** a `Paper` record and valid PDF bytes
- **WHEN** `ingest_pdf_bytes` is called
- **THEN** the PDF is parsed, chunked, embedded, and persisted as `Text` rows linked to that paper.
- **AND** repeated ingestion with `skip_existing=True` MUST avoid duplicating chunks.

### Requirement: Project-scoped PDF/RAG quality gate
The PDF ingestion path MUST be covered by an executable evaluator.

#### Scenario: Evaluate PDF to project RAG
- **WHEN** `python manage.py evaluate_pdf_rag --write-report` is run
- **THEN** it validates PDF byte ingestion, chunk persistence, project-scoped retrieval, expected source titles, expected terms, and `pqac-*` source markers.
- **AND** the report records pass/fail, chunk count, case count, evidence count, and generated report path.
