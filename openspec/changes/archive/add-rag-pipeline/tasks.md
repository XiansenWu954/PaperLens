# Tasks: add-rag-pipeline

- [x] 1. 依赖与包骨架
  - [x] 1.1 requirements.txt 追加 pypdf、sentence-transformers、numpy
  - [x] 1.2 建 rag/ 包结构 + apps.py
  - [x] 1.3 rag 注册进 INSTALLED_APPS

- [x] 2. 数据模型
  - [x] 2.1 rag/models.py：Text（挂 Paper）+ Evidence（含 1-10 分）
  - [x] 2.2 makemigrations && migrate ✓
  - [x] 2.3 验证：Text/Evidence 可创建

- [x] 3. PDF 入库
  - [x] 3.1 rag/ingest.py：download_pdf（httpx+重试）+ parse_pdf（pypdf）
  - [x] 3.2 chunk_text（段落级 ~9000/overlap 250）
  - [x] 3.3 ingest_paper：去重跳过
  - [x] 3.4 验证：smoke ingest 2 切片 ✓

- [x] 4. 嵌入与向量库
  - [x] 4.1 rag/embedding.py：懒加载单例 paraphrase-multilingual-MiniLM-L12-v2 + normalize
  - [x] 4.2 rag/store.py：NumpyVectorStore（add/search 余弦）
  - [x] 4.3 验证：embed 384维，search top-k ✓

- [x] 5. pqac 引用
  - [x] 5.1 rag/citations.py：make_citation_key_for_paper（基于 paper_id 稳定）+ parse_citations + valid_keys_prompt
  - [x] 5.2 验证：key 稳定性 + 正则解析 ✓

- [x] 6. RCS 检索
  - [x] 6.1 rag/retrieval.py：retrieve_evidence（召回 top20→RCS→过滤 score>1→top-k）
  - [x] 6.2 _rcs_summary：DeepSeek thinking=False + response_format json + Pydantic 解析
  - [x] 6.3 gather_with_concurrency 并发限4
  - [x] 6.4 验证：mock RCS 固定分，过滤逻辑正确（low 全过滤/high 保留）✓

- [x] 7. gather_evidence 工具
  - [x] 7.1 rag/evidence.py：GATHER_EVIDENCE_TOOL schema + gather_evidence
  - [x] 7.2 自动 ingest 未入库论文（按引用数倒序 max_papers）
  - [x] 7.3 返回带 pqac 的证据 JSON

- [x] 8. researcher 集成
  - [x] 8.1 researcher ReAct 工具列表加 gather_evidence（get_agent_tools）
  - [x] 8.2 extract_notes 笔记带 pqac 引用（强化 RESEARCHER_EXTRACT 提示词）
  - [x] 8.3 验证：researcher 先 search_papers(5篇) 再 gather_evidence(10证据) ✓

- [x] 9. smoke + 端到端
  - [x] 9.1 rag/smoke.py：PDF→切片→嵌入→检索→RCS→pqac 证据 ✓
  - [x] 9.2 researcher 集成验证 ✓

- [x] 10. 测试 + 归档
  - [x] 10.1 rag/tests.py：19 tests（chunk/citations/embedding/store/RCS parse/过滤）
  - [x] 10.2 全套 58 tests OK (papers 7 + datasources 15 + agent 17 + rag 19)
  - [x] 10.3 specs 合并 + archive + git 提交

---

## 附录：smoke 真实输出（2026-07-30）

`python -m rag.smoke`：
```
--- 1. 检索一篇 ArXiv 论文 ---
论文: Do You Even Need Attention? arxiv=2105.02723 pdf=True
入库 paper id=66
--- 2. PDF ingest（下载+解析+切片+嵌入）---
切片数: 2
--- 3. 检索证据（召回→RCS→过滤）---
问题: attention 机制的核心思想是什么？
证据数: 2
  [pqac-3295c76a] score=3: 论文质疑注意力层必要性，但未解释注意力机制核心思想...
  [pqac-3295c76a] score=2: 论文探讨无注意力层的Transformer在图像分类...
RAG 管线通过 ✓
```
注：RCS 评分准确——该论文是 "Do You Even Need Attention"，确实不直接讲 attention 核心，
故评分低(2-3)，逻辑正确。

## researcher 集成验证
```
agent.tools search_papers query='Mamba state space model selective mechanism' -> 5 results, 5 upserted
rag.evidence gather_evidence 'Mamba选择性机制...' -> 10 条证据
agent.nodes.researcher researcher -> 2 iters, 1027 notes, 5 sources
```
researcher ReAct 循环：先 search_papers 入库 → 再 gather_evidence 取全文证据 → extract 笔记。

## 调试过程（修了 3 个 bug）
1. async 上下文同步 ORM：upsert_paper 用 sync_to_async 包裹
2. PDF 下载 SSL EOF：urllib 改 httpx(trust_env) + 重试
3. 测试 embedding 维度不匹配：mock embed 返回与测试 embedding 一致维度
