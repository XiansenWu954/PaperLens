# Change: add-rag-pipeline

## Why（为什么做）
Change 2 的 agent 已能产出综述，但综述的论断依赖 LLM 记忆 + 摘要级检索，存在两个问题：
1. **事实不 grounded**：综述里的具体论断（如"某方法在 X 数据集上达 Y%）未追溯到论文原文，可能不准。
2. **未利用全文**：目前只用摘要（abstract），ArXiv PDF 全文（方法/实验细节）的价值没发挥。

引入 RAG 全文证据层，让综述每条论断可追溯到具体论文段落，提升可信度。这是综述质量的核心保障。

## What（改什么）
- 新建 `rag` 包，复刻 **PaperQA2 三层 RAG**（缝合 future-house/paper-qa）：
  - **Docs（论文）→ Text（切片）→ Context（LLM总结证据+评分）** 三层分离。
  - PDF 下载 + pypdf 解析 + 段落级切片。
  - 本地 embedding（sentence-transformers，384 维）+ Numpy 向量库。
  - **RCS 重排序**（缝合 PaperQA2 `_map_fxn_summary`）：向量召回 top-20 → LLM 给每片段打 1-10 分 + 针对性摘要 → 过滤 ≤1。
  - **pqac 引用格式**（缝合 PaperQA2）：`pqac-{md5[:8]}`，注入 Valid Keys 防幻觉引用。
- researcher 节点新增 `gather_evidence` 工具：对已检索论文做全文 RAG 取证据。
- synthesizer 综述改用 grounded 证据，每论断标 pqac 引用。

## 地基事实验证（spec 前提，已验证）
| 事实 | 结果 |
|---|---|
| ArXiv PDF 下载 | ✅ 193KB 成功 |
| pypdf 解析 | ✅ 4 页 13448 字符（警告无害） |
| 本地 embedding | ✅ paraphrase-multilingual-MiniLM-L12-v2，384 维，中英相似度 0.75 |

## Out of scope
- 跨论文 RAG（多库合并）—— 后续
- 向量库升级到 Chroma —— 量大时再做，起步用 Numpy
- 引用图谱（独立 change add-citation-graph）

## 风险
- PDF 解析质量参差（pypdf 对扫描版/复杂版式弱）—— 先支持文本型 PDF。
- 全文 RAG 增加 token 开销 —— RCS 过滤控制注入量，每论文取 top-k 证据。
