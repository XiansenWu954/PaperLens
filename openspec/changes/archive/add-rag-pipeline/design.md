# Design: add-rag-pipeline

> 锚定 PaperQA2（github future-house/paper-qa，arXiv 2409.13740）。
> 复刻其三层分离（Docs→Text→Context）+ RCS（LLM-as-reranker）+ pqac 引用。

## 1. 目录结构
```
backend/rag/
├── __init__.py
├── models.py            # Django: Text（切片）+ Evidence（证据）模型，挂 Paper
├── ingest.py            # PDF 下载 + pypdf 解析 + 段落级切片
├── embedding.py         # sentence-transformers 封装（懒加载单例）
├── store.py             # NumpyVectorStore（缝合 PaperQA2）
├── retrieval.py         # retrieve → RCS reranker → Context
├── citations.py         # pqac-{md5[:8]} 生成 + 回引解析
└── evidence.py          # gather_evidence 工具（供 researcher 调用）
```

## 2. 数据模型（rag/models.py，挂 papers.Paper）
```python
class Text(models.Model):
    paper = FK(Paper, related_name="chunks")
    docname = CharField          # 如 "Mamba2023 chunk3"
    chunk_index = IntegerField
    content = TextField          # 切片文本
    embedding = JSONField        # 384 维向量（存 JSON，Numpy 重建）
    citation_key = CharField     # pqac-xxxxxxxx（基于 paper）

class Evidence(models.Model):
    """LLM 总结的针对性证据（PaperQA2 Context 层）。"""
    text = FK(Text, related_name="evidence")  # 源切片
    question = TextField
    summary = TextField          # LLM 针对 question 的总结
    score = IntegerField         # 1-10 相关性
    citation_key = CharField     # pqac-xxxxxxxx
```

## 3. PDF 入库（rag/ingest.py）
- `download_pdf(paper) -> bytes`：用 paper.pdf_url（ArXiv），带 UA + 缓存。
- `parse_pdf(pdf_bytes) -> str`：pypdf 提取全文。
- `chunk_text(text, chunk_chars=9000, overlap=250)`：PaperQA2 博客最优段落级。
  简化：按段落 + 字符上限切，重叠 250。
- `ingest_paper(paper)`：下载→解析→切片→嵌入→存 Text。
- 入库前查重：paper 已有 Text 则跳过（本地库存约束）。

## 4. 嵌入（rag/embedding.py）
```python
_MODEL = None
def get_embedder():
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        _MODEL = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")  # 384维
    return _MODEL

def embed(texts):  # -> np.ndarray (n, 384)
    return get_embedder().encode(texts, normalize_embeddings=True)
```
懒加载单例，避免每次加载模型。normalize 便于余弦相似度。

## 5. 向量库（rag/store.py，缝合 PaperQA2 NumpyVectorStore）
```python
class NumpyVectorStore:
    def add(self, texts: list[Text]):  # 从 DB 重建矩阵
        self.matrix = np.array([t.embedding for t in texts])
        self.texts = texts
    def search(self, query_vec, k=20):
        sims = self.matrix @ query_vec  # normalize 后即余弦
        idx = sims.argsort()[::-1][:k]
        return [self.texts[i] for i in idx]
```
起步用内存 Numpy（按需从 DB 重建）；量大升 Chroma。

## 6. 检索 + RCS 重排序（rag/retrieval.py）
缝合 PaperQA2 `docs.aget_evidence` + `_map_fxn_summary`：
```python
async def retrieve_evidence(question, papers, k=10):
    # 1. 向量召回 top-20
    candidates = store.search(embed([question])[0], k=20)
    # 2. RCS: LLM 给每候选打 1-10 分 + 针对性摘要（并发限4）
    contexts = await gather_with_concurrency(4, [_rcs_summary(question, t) for t in candidates])
    # 3. 过滤 score<=1，取 k
    return [c for c in contexts if c.score > 1][:k]
```
`_rcs_summary`：DeepSeek thinking=False（简单评分降本），prompt 给 question + chunk，
返回 Pydantic {score: int 1-10, summary: str}。

## 7. pqac 引用（rag/citations.py，缝合 PaperQA2）
```python
def make_citation_key(text_obj):
    # 基于 paper 的稳定 key（同一论文的切片共享 key，便于综述引用归并到论文）
    return f"pqac-{hashlib.md5(str(text_obj.paper_id).encode()).hexdigest()[:8]}"

VALID_KEYS_PROMPT = "只能使用以下引用 key：{keys}。格式 (pqac-xxxxxxxx)。"

def parse_citations(text):
    return re.findall(r"\bpqac-[a-zA-Z0-9]{8}\b", text)
```
注：PaperQA2 原版 key 基于 question+context 内容，这里简化为基于 paper_id（综述里引用归并到论文更直观）。

## 8. gather_evidence 工具（rag/evidence.py，供 researcher）
```python
GATHER_EVIDENCE_TOOL = {
    "type":"function","function":{
        "name":"gather_evidence",
        "description":"对已检索论文做全文RAG，取与问题相关的证据段落（带pqac引用）。",
        "parameters":{"properties":{"question":{"type":"string"}},"required":["question"]}
    }
}
async def gather_evidence(question):
    papers = Paper.objects.filter(...)  # 当前 session 已入库的
    # 按 paper 分别 ingest（未入库的）+ retrieve_evidence
    evidences = await retrieve_evidence(question, papers)
    return json.dumps([{"summary":e.summary,"score":e.score,"citation":e.citation_key,"docname":...} for e in evidences])
```

## 9. researcher 集成
researcher 的 ReAct 工具列表加 `gather_evidence`（在 search_papers 之后调用）。
researcher extract_notes 时，笔记里的论断标 pqac 引用。

## 10. synthesizer 集成
synthesizer 收到的 notes 含 pqac 引用，综述末尾用 pqac→论文 映射生成来源列表。

## 11. 验证项
- `python -m rag.smoke`：下载一篇 ArXiv PDF → 解析切片 → 嵌入 → 检索某问题 → RCS 评分 → 返回带 pqac 的证据。
- researcher 端到端：调 gather_evidence 产出 grounded 证据。

## 12. 测试（rag/tests.py）
- chunk_text：切片大小/重叠
- citations：make_citation_key 稳定性 + parse_citations 正则
- embedding：维度 + normalize
- RCS：mock LLM 返回固定分，验证过滤逻辑
- retrieve_evidence：mock store，验证 top-k 过滤
