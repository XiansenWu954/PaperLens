"""gather_evidence 工具：对已检索论文做全文 RAG 取证据（供 researcher ReAct 调用）。

自动 ingest 未入库的论文，retrieve_evidence 取 grounded 证据，返回带 pqac 引用的 JSON。
"""
from __future__ import annotations

import json
import logging

from asgiref.sync import sync_to_async

from .ingest import ingest_paper
from .retrieval import retrieve_evidence

logger = logging.getLogger(__name__)

GATHER_EVIDENCE_TOOL = {
    "type": "function",
    "function": {
        "name": "gather_evidence",
        "description": (
            "对已检索的论文做全文 RAG，取与研究问题相关的证据段落。"
            "返回带 pqac 引用 key 的证据列表，用于在综述中标注来源。"
            "在 search_papers 检索论文之后调用，用具体研究问题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要查证据的具体研究问题，如 'Mamba 的选择性机制如何工作'",
                }
            },
            "required": ["question"],
        },
    },
}


async def gather_evidence(question: str, max_papers: int = 5) -> str:
    """对已入库论文做全文 RAG 取证据，返回 JSON。

    先 ingest（按引用数倒序取 max_papers 篇，pdf_url 优先），再 retrieve_evidence。
    """
    from papers.models import Paper

    # 取已入库论文（按引用数倒序，pdf_url 优先），ingest 未切片的
    def _get_papers():
        qs = Paper.objects.filter(pdf_url__isnull=False).exclude(pdf_url="").order_by("-citation_count")
        return list(qs[:max_papers])

    papers = await sync_to_async(_get_papers)()
    paper_ids = [p.id for p in papers]

    # ingest 未入库的（并行）
    import asyncio

    await asyncio.gather(*[ingest_paper(p) for p in papers], return_exceptions=True)

    # 检索证据
    evidences = await retrieve_evidence(question, paper_ids=paper_ids)
    result = [
        {
            "summary": e.summary,
            "score": e.score,
            "citation": e.citation_key,
            "docname": e.text.docname,
            "paper_id": e.text.paper_id,
        }
        for e in evidences
    ]
    logger.info("gather_evidence %r -> %d 条证据", question, len(result))
    return json.dumps(result, ensure_ascii=False)
