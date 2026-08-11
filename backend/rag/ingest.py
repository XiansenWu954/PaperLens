"""PDF and text ingestion for project-scoped RAG."""
from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import time
from dataclasses import dataclass

from asgiref.sync import sync_to_async
from django.utils import timezone

from .embedding import embed, embedding_metadata
from .models import Text

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_CHARS = 3000
DEFAULT_OVERLAP = 250
UA = "PaperLens/0.3 (academic paper research agent)"


@dataclass(frozen=True)
class PageText:
    page: int
    text: str
    char_start: int
    char_end: int


@dataclass(frozen=True)
class Chunk:
    content: str
    char_start: int
    char_end: int
    page_start: int | None = None
    page_end: int | None = None
    section: str = ""


def download_pdf(url: str, timeout: float = 60.0, max_retries: int = 2) -> bytes:
    """Download a PDF with a stable user agent and bounded retries."""

    import httpx

    headers = {"User-Agent": UA}
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            with httpx.Client(trust_env=True, timeout=timeout, follow_redirects=True) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()
                return response.content
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                logger.info(
                    "PDF download returned a client error",
                    extra={
                        "event": "paper_pdf_download_client_error",
                        "status_code": exc.response.status_code,
                        "url_host": _safe_url_host(url),
                        "status": "error",
                    },
                )
                raise
            last_exc = exc
        except Exception as exc:
            last_exc = exc
        logger.warning(
            "PDF download attempt failed",
            extra={
                "event": "paper_pdf_download_retry",
                "attempt": attempt + 1,
                "url_host": _safe_url_host(url),
                "error": last_exc.__class__.__name__ if last_exc else "",
            },
        )
        time.sleep(1.0 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def parse_pdf_pages(pdf_bytes: bytes) -> tuple[str, list[PageText]]:
    """Extract page text and global character ranges from a PDF.

    优先用 Docling（布局/表格/公式感知，适合学术论文双栏）；
    Docling 不可用或解析为空时 fallback 到 pypdf（轻量纯文本兜底）。
    两者都返回 (full_text, list[PageText])，PageText 维护全局字符游标供 chunk 定位页码。
    """
    text, pages = _parse_pdf_with_docling(pdf_bytes)
    if text.strip():
        return text, pages
    logger.info("docling 解析为空或失败，回退到 pypdf", extra={"event": "pdf_parse_fallback_pypdf"})
    return _parse_pdf_with_pypdf(pdf_bytes)


def _parse_pdf_with_docling(pdf_bytes: bytes) -> tuple[str, list[PageText]]:
    """用 Docling 解析，按页拼接文本并维护字符偏移。失败时返回空。"""
    try:
        from docling.document_converter import DocumentConverter
    except Exception as exc:
        logger.info("docling 未安装，跳过", extra={"event": "pdf_parse_docling_missing", "error": exc.__class__.__name__})
        return "", []

    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(pdf_bytes)
        tmp_path = tmp.name
    try:
        conv = DocumentConverter()
        result = conv.convert(tmp_path)
        doc = result.document
        full_text = doc.export_to_markdown().strip()
        if not full_text:
            return "", []
        # Docling 不直接给逐页文本，按页号切分（用 page items 若可用，否则整篇作单页）
        pages: list[PageText] = []
        try:
            page_items = list(doc.iterate_items()) if hasattr(doc, "iterate_items") else []
            # 若能拿到页号，按页聚合；否则整篇归第 1 页
            per_page: dict[int, list[str]] = {}
            for item in page_items:
                page_no = getattr(item, "page_no", 1) or 1
                txt = getattr(item, "text", None) or ""
                if txt.strip():
                    per_page.setdefault(page_no, []).append(txt.strip())
            cursor = 0
            parts: list[str] = []
            for page_no in sorted(per_page):
                page_text = "\n".join(per_page[page_no])
                if parts:
                    parts.append("\n\n")
                    cursor += 2
                start = cursor
                parts.append(page_text)
                cursor += len(page_text)
                pages.append(PageText(page=page_no, text=page_text, char_start=start, char_end=cursor))
            full_text = "".join(parts)
        except Exception:
            # 退化为整篇归一页
            pages = [PageText(page=1, text=full_text, char_start=0, char_end=len(full_text))]
        return full_text, pages
    except Exception as exc:
        logger.warning(
            "docling 解析失败",
            extra={"event": "pdf_parse_docling_failed", "error": exc.__class__.__name__},
        )
        return "", []
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _parse_pdf_with_pypdf(pdf_bytes: bytes) -> tuple[str, list[PageText]]:
    """pypdf 兜底解析（纯文本，不感知布局）。"""
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    full_parts: list[str] = []
    pages: list[PageText] = []
    cursor = 0
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        text = text.strip()
        if not text:
            continue
        if full_parts:
            full_parts.append("\n\n")
            cursor += 2
        start = cursor
        full_parts.append(text)
        cursor += len(text)
        pages.append(PageText(page=index, text=text, char_start=start, char_end=cursor))
    return "".join(full_parts).strip(), pages


def parse_pdf(pdf_bytes: bytes) -> str:
    """Compatibility wrapper returning only extracted full text."""

    return parse_pdf_pages(pdf_bytes)[0]


def chunk_text(
    text: str,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    return [chunk.content for chunk in chunk_text_with_metadata(text, chunk_chars, overlap)]


def chunk_text_with_metadata(
    text: str,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_OVERLAP,
    pages: list[PageText] | None = None,
) -> list[Chunk]:
    """Split text into bounded chunks with offsets and coarse page/section labels."""

    text = (text or "").strip()
    if not text:
        return []
    chunks: list[Chunk] = []
    start = 0
    length = len(text)
    while start < length:
        target_end = min(start + chunk_chars, length)
        end = target_end
        if target_end < length:
            paragraph_break = text.rfind("\n\n", start + max(200, chunk_chars // 3), target_end)
            sentence_break = text.rfind(". ", start + max(200, chunk_chars // 3), target_end)
            end = max(paragraph_break, sentence_break)
            if end <= start:
                end = target_end
        content = text[start:end].strip()
        if content:
            absolute_start = start + len(text[start:end]) - len(text[start:end].lstrip())
            absolute_end = absolute_start + len(content)
            page_start, page_end = _page_range_for_span(absolute_start, absolute_end, pages or [])
            chunks.append(
                Chunk(
                    content=content,
                    char_start=absolute_start,
                    char_end=absolute_end,
                    page_start=page_start,
                    page_end=page_end,
                    section=_guess_section(content),
                )
            )
        if end >= length:
            break
        next_start = max(0, end - overlap)
        start = next_start if next_start > start else end
    return chunks


async def ingest_paper(paper) -> int:
    """Download a paper PDF and persist indexed chunks."""

    existing = await sync_to_async(lambda: paper.chunks.count())()
    if existing > 0:
        logger.info(
            "paper already has chunks; skipping ingest",
            extra={
                "event": "paper_pdf_ingest_skipped",
                "paper_id": paper.id,
                "chunk_count": existing,
                "status": "skipped",
            },
        )
        return existing

    if not paper.pdf_url:
        logger.warning(
            "paper has no pdf_url; cannot ingest",
            extra={
                "event": "paper_pdf_ingest_missing_url",
                "paper_id": paper.id,
                "status": "skipped",
            },
        )
        return 0

    started = time.perf_counter()
    logger.info(
        "paper PDF ingest started",
        extra={"event": "paper_pdf_ingest_started", "paper_id": paper.id, "status": "running"},
    )
    pdf_bytes = await sync_to_async(download_pdf)(paper.pdf_url)
    try:
        count = await ingest_pdf_bytes(paper, pdf_bytes, skip_existing=False, replace_existing=True)
    except Exception as exc:
        # §31.1: exception type + digest + safe frames — never the message.
        from agent.events import error_hash, safe_stack_frames

        logger.error(
            "paper PDF ingest failed",
            extra={
                "event": "paper_pdf_ingest_failed",
                "paper_id": paper.id,
                "status": "error",
                "error": exc.__class__.__name__,
                "error_hash": error_hash(exc),
                "stack_frames": safe_stack_frames(exc),
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        raise
    logger.info(
        "paper PDF ingest completed",
        extra={
            "event": "paper_pdf_ingest_completed",
            "paper_id": paper.id,
            "chunk_count": count,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "status": "done" if count else "empty",
        },
    )
    return count


async def ingest_pdf_bytes(
    paper,
    pdf_bytes: bytes,
    *,
    skip_existing: bool = True,
    replace_existing: bool = False,
) -> int:
    """Parse PDF bytes, chunk text, embed chunks, and persist Text rows."""

    if skip_existing:
        existing = await sync_to_async(lambda: paper.chunks.count())()
        if existing > 0:
            logger.info(
                "paper already has chunks; skipping ingest",
                extra={
                    "event": "paper_pdf_ingest_skipped",
                    "paper_id": paper.id,
                    "chunk_count": existing,
                    "status": "skipped",
                },
            )
            return existing

    full_text, pages = await sync_to_async(parse_pdf_pages)(pdf_bytes)
    return await ingest_text(paper, full_text, pages=pages, replace_existing=replace_existing)


async def ingest_text(
    paper,
    full_text: str,
    *,
    pages: list[PageText] | None = None,
    replace_existing: bool = False,
) -> int:
    """Chunk extracted paper text, embed it, and save RAG Text rows."""

    if len(full_text) < 200:
        logger.warning(
            "paper parsed text is too short",
            extra={
                "event": "paper_pdf_ingest_short_text",
                "paper_id": paper.id,
                "text_chars": len(full_text),
                "status": "empty",
            },
        )
        return 0

    chunks = chunk_text_with_metadata(full_text, pages=pages)
    if not chunks:
        return 0

    # 若 provider 支持 sparse（BGE-M3），同时编码 dense + sparse 词级权重
    from .embedding import get_provider
    provider = get_provider()
    sparse_list: list[dict[str, float]] = []
    if hasattr(provider, "encode_dense_sparse"):
        vectors_arr, sparse_list = await sync_to_async(provider.encode_dense_sparse, thread_sensitive=False)(
            [chunk.content for chunk in chunks]
        )
        vectors = vectors_arr
    else:
        vectors = await sync_to_async(embed, thread_sensitive=False)(
            [chunk.content for chunk in chunks],
            input_type="document",
        )
    meta = embedding_metadata()
    docname_prefix = (paper.title[:32] if paper.title else f"paper{paper.id}") + str(paper.year or "")
    from .citations import make_citation_key_for_paper

    citation_key = make_citation_key_for_paper(paper.id)
    indexed_at = timezone.now()

    def _save():
        if replace_existing:
            Text.objects.filter(paper=paper).delete()
        objs = [
            Text(
                paper=paper,
                docname=f"{docname_prefix} chunk{i}",
                chunk_index=i,
                content=chunk.content,
                embedding=vec.tolist(),
                embedding_model=str(meta["embedding_model"]),
                embedding_dim=int(meta["embedding_dim"]),
                embedding_version=str(meta["embedding_version"]),
                content_hash=_content_hash(chunk.content),
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                section=chunk.section,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                search_vector=_search_document(chunk.content, paper),
                sparse_weights=sparse_list[i] if i < len(sparse_list) else {},
                citation_key=citation_key,
                indexed_at=indexed_at,
            )
            for i, (chunk, vec) in enumerate(zip(chunks, vectors))
        ]
        Text.objects.bulk_create(objs)
        return len(objs)

    count = await sync_to_async(_save)()
    logger.info(
        "paper text chunks persisted",
        extra={
            "event": "paper_pdf_chunks_persisted",
            "paper_id": paper.id,
            "chunk_count": count,
            "text_chars": len(full_text),
            "embedding_model": meta["embedding_model"],
            "embedding_dim": meta["embedding_dim"],
            "embedding_version": meta["embedding_version"],
            "status": "done",
        },
    )
    return count


def _page_range_for_span(start: int, end: int, pages: list[PageText]) -> tuple[int | None, int | None]:
    hits = [page.page for page in pages if page.char_start < end and page.char_end > start]
    if not hits:
        return None, None
    return min(hits), max(hits)


def _guess_section(content: str) -> str:
    for line in content.splitlines()[:8]:
        candidate = line.strip()
        if not candidate:
            continue
        if re.match(r"^(\d+(\.\d+)*\.?\s+)?[A-Z][A-Za-z0-9 ,:/\-]{3,80}$", candidate):
            return candidate[:120]
    return ""


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _search_document(content: str, paper) -> str:
    return "\n".join(
        [
            paper.title or "",
            paper.abstract or "",
            paper.venue.name if getattr(paper, "venue_id", None) else "",
            content,
        ]
    )


def _safe_url_host(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).netloc
    except Exception:
        return ""
