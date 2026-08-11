"""BibTeX / RIS 导入导出工具。

让研究者能在 Zotero 工作流与 PaperLens 项目库之间双向迁移论文：
- 导入：解析 .bib / .ris 文件 → 归一化为 upsert_paper 兼容的 payload 列表。
- 导出：项目论文序列化为 BibTeX / RIS 文本，可导回 Zotero / Mendeley / LaTeX。

字段映射以 upsert_paper 接受的结构为准（title/abstract/year/doi/arxiv_id/...）。
"""
from __future__ import annotations

import re
from typing import Any

# arxiv id 正则：eprint 字段或含 arxiv 的 url
_ARXIV_RE = re.compile(r"(?:arxiv\.org/abs/|arxiv\.org/pdf/)?(\d{4}\.\d{4,5}|[a-z\-]+/\d{7})", re.I)


def parse_bibtex(text: str) -> list[dict[str, Any]]:
    """解析 BibTeX 文本为 paper payload 列表（upsert_paper 兼容）。

    每个条目映射为：title/abstract/year/doi/arxiv_id/pdf_url/venue/authors/raw。
    """
    import bibtexparser

    db = bibtexparser.loads(text or "")
    payloads: list[dict[str, Any]] = []
    for entry in db.entries:
        payloads.append(_bibtex_entry_to_payload(entry))
    return payloads


def _bibtex_entry_to_payload(entry: dict[str, str]) -> dict[str, Any]:
    title = _clean(entry.get("title", ""))
    authors = _split_authors(entry.get("author", ""))
    year_raw = entry.get("year", "").strip()
    year = _safe_int(year_raw)
    doi = _clean(entry.get("doi", "")).lower() or None
    # arxiv：优先 eprint，其次从 url 找
    arxiv_id = None
    eprint = entry.get("eprint", "").strip()
    if eprint:
        m = _ARXIV_RE.search(eprint)
        arxiv_id = m.group(1).lower() if m else eprint.lower()
    if not arxiv_id:
        for url_field in ("url", "howpublished", "note"):
            m = _ARXIV_RE.search(entry.get(url_field, ""))
            if m:
                arxiv_id = m.group(1).lower()
                break
    venue = entry.get("journal") or entry.get("booktitle") or entry.get("publisher") or ""
    pdf_url = _clean(entry.get("url", "")) or None
    abstract = _clean(entry.get("abstract", "") or entry.get("abstractnote", ""))
    return {
        "title": title or "(untitled)",
        "abstract": abstract,
        "year": year,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "pdf_url": pdf_url,
        "venue_name": _clean(venue),
        "authors": authors,
        "raw": dict(entry),
    }


def parse_ris(text: str) -> list[dict[str, Any]]:
    """解析 RIS 文本为 paper payload 列表。

    RIS 以空行分隔记录，每行 "TY  - ..." 形式。
    """
    payloads: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text or ""):
        block = block.strip()
        if not block:
            continue
        fields: dict[str, list[str]] = {}
        for line in block.splitlines():
            # RIS 行格式："TY  - value"（tag 2 字符 + "  - " 分隔）
            if len(line) >= 6 and line[2:6] == "  - ":
                tag, value = line[0:2], line[6:].strip()
                fields.setdefault(tag, []).append(value)
        if fields:
            payloads.append(_ris_fields_to_payload(fields))
    return payloads


def _ris_fields_to_payload(fields: dict[str, list[str]]) -> dict[str, Any]:
    def first(tag: str) -> str:
        return fields.get(tag, [""])[0].strip()

    title = _clean(first("TI") or first("T1") or first("ST"))
    authors = []
    for tag in ("AU", "A1", "A2"):
        authors.extend(_split_authors(v) for v in fields.get(tag, []))
    # 拍平 authors（_split_authors 返回 list，extend 后会嵌套）
    flat_authors: list[str] = []
    for a in authors:
        if isinstance(a, list):
            flat_authors.extend(a)
        else:
            flat_authors.append(a)
    year = _safe_int(first("PY") or first("Y1") or first("DA")[:4])
    doi = _clean(first("DO")).lower() or None
    arxiv_id = None
    for url in fields.get("UR", []) + fields.get("L1", []):
        m = _ARXIV_RE.search(url)
        if m:
            arxiv_id = m.group(1).lower()
            break
    venue = first("JO") or first("JF") or first("JA")
    return {
        "title": title or "(untitled)",
        "abstract": _clean(first("AB") or first("N2")),
        "year": year,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "pdf_url": _clean(first("UR")) or None,
        "venue_name": _clean(venue),
        "authors": flat_authors,
        "raw": {tag: vals for tag, vals in fields.items()},
    }


def papers_to_bibtex(papers: list[Any]) -> str:
    """把 paper 对象列表（需有 title/year/doi/arxiv_id 等属性）序列化为 BibTeX 文本。"""
    lines: list[str] = []
    for p in papers:
        key = _bibkey(p)
        entry_type = "article" if getattr(p, "venue_id", None) else "misc"
        lines.append(f"@{entry_type}{{{key},")
        lines.append(f"  title = {{{_brace(getattr(p, 'title', ''))}}},")
        author_names = _author_names(p)
        if author_names:
            lines.append(f"  author = {{{' and '.join(author_names)}}},")
        year = getattr(p, "year", None)
        if year:
            lines.append(f"  year = {{{year}}},")
        venue = _venue_name(p)
        if venue:
            lines.append(f"  journal = {{{_brace(venue)}}},")
        if getattr(p, "doi", None):
            lines.append(f"  doi = {{{p.doi}}},")
        if getattr(p, "arxiv_id", None):
            lines.append(f"  eprint = {{{p.arxiv_id}}},")
        if getattr(p, "pdf_url", None):
            lines.append(f"  url = {{{p.pdf_url}}},")
        abstract = getattr(p, "abstract", "")
        if abstract:
            lines.append(f"  abstract = {{{_brace(abstract)}}},")
        lines.append("}\n")
    return "\n".join(lines)


def papers_to_ris(papers: list[Any]) -> str:
    """把 paper 对象列表序列化为 RIS 文本。"""
    blocks: list[str] = []
    for p in papers:
        lines = ["TY  - JOUR"]
        title = getattr(p, "title", "")
        if title:
            lines.append(f"TI  - {title}")
        for name in _author_names(p):
            lines.append(f"AU  - {name}")
        if getattr(p, "year", None):
            lines.append(f"PY  - {p.year}")
        venue = _venue_name(p)
        if venue:
            lines.append(f"JO  - {venue}")
        if getattr(p, "doi", None):
            lines.append(f"DO  - {p.doi}")
        if getattr(p, "pdf_url", None):
            lines.append(f"UR  - {p.pdf_url}")
        abstract = getattr(p, "abstract", "")
        if abstract:
            lines.append(f"AB  - {abstract}")
        lines.append("ER  - ")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


# ---- 辅助函数 ----

def _clean(value: str) -> str:
    """去除 BibTeX 花括号包裹与多余空白。"""
    if not value:
        return ""
    cleaned = value.strip()
    # 去除成对的花括号（BibTeX 常用来保护大小写）
    while cleaned.startswith("{") and cleaned.endswith("}"):
        cleaned = cleaned[1:-1].strip()
    return re.sub(r"\s+", " ", cleaned)


def _split_authors(value: str) -> list[str]:
    """BibTeX author 字段以 ' and ' 分隔。"""
    if not value:
        return []
    parts = re.split(r"\s+and\s+", value.strip())
    return [_clean(p) for p in parts if _clean(p)]


def _safe_int(value: str) -> int | None:
    m = re.search(r"\d{4}", value or "")
    return int(m.group()) if m else None


def _bibkey(p: Any) -> str:
    """生成稳定的 BibTeX key：firstauthor+year+titleword。"""
    authors = _author_names(p)
    first = authors[0].split()[-1].lower() if authors else "anon"
    first = re.sub(r"[^a-z]", "", first) or "anon"
    year = getattr(p, "year", "") or "nd"
    title_word = ""
    for w in re.findall(r"[A-Za-z]+", getattr(p, "title", "")):
        if w.lower() not in {"the", "a", "an", "of", "for", "and", "on", "in", "to"}:
            title_word = w.lower()
            break
    return f"{first}{year}{title_word}"


def _brace(value: str) -> str:
    return (value or "").replace("{", "").replace("}", "")


def _author_names(p: Any) -> list[str]:
    """从 paper 对象取作者名列表（兼容 many-to-many authors 或 raw）。"""
    try:
        authors = list(p.authors.all()) if p.pk else []
    except Exception:
        authors = []
    if authors:
        return [str(a.name) for a in authors if getattr(a, "name", "")]
    return []


def _venue_name(p: Any) -> str:
    venue = getattr(p, "venue", None)
    if venue and getattr(venue, "name", None):
        return venue.name
    return ""
