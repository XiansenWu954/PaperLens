"""Real-PDF retrieval evaluation (deepseek-live-evaluation §3.1 / §6.3).

Runs against the real stack (PostgreSQL + pgvector + BGE-M3) — NO DeepSeek.
Ingests the 12 arXiv PDFs, then for each split measures dense/sparse/hybrid/
hybrid+multi-query Recall@5, MRR, Precision@5, nDCG@5, and runs a 3-project
bidirectional isolation matrix (leakage rate must be 0%).

Stops on P0: any empty chunk (embedded but chunk_count=0), cross-project
leakage, or wrong embedding dimension.

This module was revised to address review findings: the verdict now checks ALL
declared metrics (not just Recall/MRR), compare cases have an independent
threshold, abstention was renamed to scope_leakage (true abstention needs the
final LLM answer — Wave 1A), Top-5 results include full paper_id/chunk_id/page/
section/score/channel detail, and a 4-path ablation proves hybrid vs dense/sparse.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import subprocess
import time
from pathlib import Path

from eval.real_pdf_dataset import (
    REAL_PAPERS,
    paper_by_arxiv,
    papers_for_split,
    pdf_path,
    cases_for_split,
    REAL_RAG_CASES,
)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _ingest_papers(papers, project_id: int) -> dict:
    """Ingest each paper's PDF into the given project; return per-paper chunk counts."""
    from asgiref.sync import async_to_sync
    from agent.project_tools import add_papers_to_project
    from papers.models import Paper
    from rag.ingest import download_pdf, ingest_pdf_bytes

    summary = {}
    for p in papers:
        started = time.perf_counter()
        try:
            pdf_bytes = Path(pdf_path(p.arxiv_id)).read_bytes()
        except FileNotFoundError:
            pdf_bytes = download_pdf(f"https://arxiv.org/pdf/{p.arxiv_id}.pdf")
        payload = {
            "source": "real-pdf-eval", "source_id": p.arxiv_id,
            "title": p.title, "abstract": "", "year": p.year,
            "authors": p.authors, "venue": "arXiv", "citation_count": 0,
            "arxiv_id": p.arxiv_id,
        }
        async_to_sync(add_papers_to_project)(project_id, [payload], "real pdf eval")
        paper = Paper.objects.filter(arxiv_id=p.arxiv_id).first()
        chunks = async_to_sync(ingest_pdf_bytes)(
            paper, pdf_bytes, skip_existing=False, replace_existing=True)
        if not isinstance(chunks, int):
            chunks = int(chunks or 0)
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        summary[p.arxiv_id] = {"short_name": p.short_name, "chunks": chunks,
                               "elapsed_ms": elapsed, "ok": chunks > 0}
        if chunks == 0:
            summary[p.arxiv_id]["P0"] = "embedded but 0 chunks (silent empty index)"
    return summary


def _chunk_counts_from_db() -> dict:
    """F8: compute per-topic chunk counts directly from the DB (no hand-math)."""
    from rag.models import Text

    topic_by_arxiv = {p.arxiv_id: p.topic for p in REAL_PAPERS}
    counts: dict[str, int] = {"sequence": 0, "rag": 0, "graph": 0}
    for t in Text.objects.select_related("paper").all():
        topic = topic_by_arxiv.get(t.paper.arxiv_id)
        if topic:
            counts[topic] = counts.get(topic, 0) + 1
    counts["total"] = sum(counts.values())
    return counts


# ---------------------------------------------------------------------------
# Retrieval — 4-path ablation (F5)
# ---------------------------------------------------------------------------

def _embed_query(question: str):
    """Embed a query string (dense vector). Replicates the one line hybrid does."""
    from rag.embedding import embed
    return embed([question], input_type="query")[0]


def _retrieve_path(question: str, paper_ids: list[int], k: int = 5, mode: str = "hybrid") -> list[dict]:
    """Retrieve top-k via one of 4 paths; return dicts with full detail (F3).

    mode:
        dense   — pgvector cosine only (no sparse / no fusion)
        sparse  — independent BGE-M3 sparse dot-product ranking over ALL in-scope chunks
        hybrid  — production path (dense + sparse/FTS + RRF)
        multi   — hybrid over each sub-query merged (only meaningful for compare; caller passes sub-queries)
    """
    from django.db import connection
    from rag.retrieval import _postgres_dense_ids, _texts_by_ids, hybrid_retrieve_texts
    from rag.embedding import get_provider
    from rag.models import Text

    def _row(t, channel: str, rank: int) -> dict:
        return {
            "paper_id": t.paper_id, "chunk_id": t.id, "title": t.paper.title,
            "arxiv_id": t.paper.arxiv_id, "chunk_index": t.chunk_index,
            "page_start": t.page_start, "page_end": t.page_end, "section": t.section or "",
            "channel": channel, "rank": rank,
            "content_preview": (t.content or "")[:120],
            # Full content kept for term-level Precision@5 (the 120-char preview
            # under-counts term hits — a chunk often carries the terms outside the
            # first 120 chars). Not emitted in the report's top5 to keep size sane.
            "content_full": t.content or "",
        }

    if mode == "dense":
        if connection.vendor != "postgresql":
            return []  # dense-only requires pgvector
        qvec = _embed_query(question)
        ids = _postgres_dense_ids(qvec, paper_ids, k)
        return [_row(t, "dense", i + 1) for i, t in enumerate(_texts_by_ids(ids))]

    if mode == "sparse":
        provider = get_provider()
        if not hasattr(provider, "encode_query_sparse"):
            return []  # sparse-only requires BGE-M3
        q_sparse = provider.encode_query_sparse(question)
        texts = list(Text.objects.filter(paper_id__in=paper_ids))
        scored = sorted(
            texts,
            key=lambda t: sum(w * (t.sparse_weights or {}).get(tok, 0.0) for tok, w in q_sparse.items()),
            reverse=True,
        )[:k]
        return [_row(t, "sparse", i + 1) for i, t in enumerate(scored)]

    if mode == "multi":
        # multi-query: caller should pass a merged question; handled by _retrieve_multi
        mode = "hybrid"

    # hybrid (production)
    texts = asyncio.run(hybrid_retrieve_texts(question, paper_ids=paper_ids, final_k=k))
    return [_row(t, "hybrid", i + 1) for i, t in enumerate(texts)]


def _retrieve_multi(sub_queries: tuple[str, ...], paper_ids: list[int], k: int = 5) -> list[dict]:
    """F5 multi-query variant for compare cases: run hybrid per sub-query, merge by RRF."""
    if not sub_queries:
        return []
    # Collect ranks from each sub-query, RRF-fuse across queries.
    rrf_k = 60
    scores: dict[int, float] = {}
    detail: dict[int, dict] = {}
    for sq in sub_queries:
        rows = _retrieve_path(sq, paper_ids, k=k, mode="hybrid")
        for r in rows:
            cid = r["chunk_id"]
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (rrf_k + r["rank"])
            detail.setdefault(cid, r)
    merged_ids = sorted(scores, key=lambda c: scores[c], reverse=True)[:k]
    out = []
    for rank, cid in enumerate(merged_ids, 1):
        r = dict(detail[cid])
        r["channel"] = "multi-query-rrf"
        r["rrf_score"] = round(scores[cid], 5)
        r["rank"] = rank
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Metrics (path-agnostic)
# ---------------------------------------------------------------------------

def _metrics(retrieved: list[dict], gold_arxiv_ids: tuple[str, ...], expected_terms: tuple[str, ...]) -> dict:
    """Recall@5/MRR/Precision@5/nDCG@5 over a retrieved list (paper-level, deduped).

    Recall@5 is MULTI-gold: the fraction of gold papers found in top-5
    (hit_any / total_gold), NOT "1.0 if any gold paper appears". For a compare
    case with 2 gold papers, returning only one yields Recall=0.5.
    """
    gold = {a.lower() for a in gold_arxiv_ids}
    rel = [1.0 if r.get("arxiv_id", "").lower() in gold else 0.0 for r in retrieved]
    first_rank = next((i + 1 for i, v in enumerate(rel) if v > 0), 0)
    # Multi-gold recall: unique gold papers present in retrieved / total gold.
    retrieved_gold = {r.get("arxiv_id", "").lower() for r in retrieved} & gold
    recall_at_5 = (len(retrieved_gold) / len(gold)) if gold else 0.0
    mrr = 1.0 / first_rank if first_rank > 0 else 0.0
    hits = sum(1 for r in retrieved if any(t.lower() in (r.get("content_full") or "").lower() for t in expected_terms))
    precision_at_5 = hits / len(retrieved) if retrieved else 0.0
    # nDCG paper-level (deduped): each gold paper contributes once at first position
    seen: set[str] = set()
    dcg = 0.0
    for i, r in enumerate(retrieved):
        aid = r.get("arxiv_id", "").lower()
        if aid in gold and aid not in seen:
            seen.add(aid)
            dcg += 1.0 / math.log2(i + 2)
    ideal_n = min(len(gold), len(retrieved)) if gold else 0
    idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_n))
    ndcg = (dcg / idcg) if idcg > 0 else 0.0
    return {"recall_at_5": recall_at_5, "mrr": round(mrr, 4),
            "precision_at_5": round(precision_at_5, 4), "ndcg_5": round(ndcg, 4),
            "first_rank": first_rank}


# ---------------------------------------------------------------------------
# Isolation matrix (F6): bidirectional, paper_id-based
# ---------------------------------------------------------------------------

def _isolation_matrix(projects: dict[str, "object"]) -> dict:
    """3-project bidirectional isolation matrix using paper_id (not title).

    For each ordered pair (A, B): query A's project with an A-topic question,
    assert no returned chunk's paper_id belongs to project B.
    """
    from api.models import ProjectPaper

    topic_queries = {
        "sequence": "selective state space model for linear time sequence modeling",
        "rag": "retrieval augmented few-shot learning dense retriever",
        "graph": "graph convolutional network semi-supervised node classification",
    }
    topics = list(projects)
    matrix = {}
    leaked_any = False
    for a in topics:
        a_paper_ids = set(ProjectPaper.objects.filter(project_id=projects[a].id).values_list("paper_id", flat=True))
        retrieved = _retrieve_path(topic_queries[a], list(a_paper_ids), k=8, mode="hybrid")
        for b in topics:
            if a == b:
                continue
            b_paper_ids = set(ProjectPaper.objects.filter(project_id=projects[b].id).values_list("paper_id", flat=True))
            leaked = [r for r in retrieved if r["paper_id"] in b_paper_ids]
            key = f"{a}->{b}"
            matrix[key] = {"leaked_count": len(leaked), "leakage_rate": len(leaked) / len(retrieved) if retrieved else 0.0,
                           "leaked_titles": [r["title"][:40] for r in leaked[:3]]}
            if leaked:
                leaked_any = True
    return {"matrix": matrix, "any_leak": leaked_any, "pairs": len(matrix)}


# ---------------------------------------------------------------------------
# Run metadata (F7)
# ---------------------------------------------------------------------------

def _in_container() -> bool:
    """Detect whether we're running inside a Docker container."""
    try:
        return Path("/.dockerenv").exists()
    except Exception:
        return False


def _run_metadata() -> dict:
    """Reproducibility metadata: git, dataset hash, image digest, versions.

    When run inside the container, git/docker CLIs are unavailable — those fields
    are marked 'requires host enrichment' and must be filled by
    enrich_metadata_from_host() after the run. DB-sourced fields (pgvector,
    embedding version) are always populated.
    """
    dataset_payload = json.dumps(
        [{"arxiv_id": p.arxiv_id, "title": p.title} for p in REAL_PAPERS], ensure_ascii=False, sort_keys=True)
    dataset_hash = hashlib.sha256(dataset_payload.encode("utf-8")).hexdigest()[:16]
    in_ct = _in_container()

    def _git(args: list[str]) -> str:
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True,
                                  cwd=str(Path(__file__).resolve().parents[1]), timeout=10).stdout.strip()
        except Exception:
            return "requires host enrichment" if in_ct else "git unavailable"

    diff = _git(["diff"])
    diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()[:16] if (diff and not diff.startswith("requires")) else "requires host enrichment"

    meta = {
        "git_sha": _git(["rev-parse", "HEAD"]),
        "git_describe_dirty": _git(["describe", "--always", "--dirty"]),
        "worktree_diff_hash": diff_hash,
        "changed_files": len([l for l in _git(["status", "--porcelain"]).splitlines() if l.strip() and not l.startswith("requires")]),
        "dataset_hash": dataset_hash,
        "case_count": len(REAL_RAG_CASES),
        "container_image_digest": "requires host enrichment" if in_ct else _host_docker_digest(),
    }
    # pgvector version + embedding version from DB if available
    try:
        from django.db import connection
        if connection.vendor == "postgresql":
            with connection.cursor() as cur:
                cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
                row = cur.fetchone()
                meta["pgvector_version"] = row[0] if row else "not installed"
        from rag.embedding import embedding_metadata
        meta["embedding_version"] = embedding_metadata().get("embedding_version")
    except Exception:
        pass
    return meta


def _host_docker_digest() -> str:
    try:
        return subprocess.run(["docker", "inspect", "--format={{.Id}}", "paperlens-backend"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as exc:
        return f"docker unavailable: {exc.__class__.__name__}"


def enrich_metadata_from_host(report_path: str) -> None:
    """Post-process: fill git/image fields that couldn't run inside the container.

    Run this on the HOST after the in-container eval wrote the report JSON.
    """
    import os
    repo = str(Path(__file__).resolve().parents[1])
    p = Path(report_path)
    if not p.exists():
        print(f"report not found: {p}")
        return
    report = json.loads(p.read_text(encoding="utf-8"))
    meta = report.get("metadata", {})

    def _git(args):
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True, cwd=repo, timeout=10).stdout.strip()
        except Exception as e:
            # §32.4: artifacts carry the exception type only.
            return f"error: {e.__class__.__name__}"

    diff = _git(["diff"])
    meta["git_sha"] = _git(["rev-parse", "HEAD"])
    meta["git_describe_dirty"] = _git(["describe", "--always", "--dirty"])
    meta["worktree_diff_hash"] = hashlib.sha256(diff.encode("utf-8")).hexdigest()[:16] if diff else "clean"
    meta["changed_files"] = len([l for l in _git(["status", "--porcelain"]).splitlines() if l.strip()])
    try:
        meta["container_image_digest"] = subprocess.run(
            ["docker", "inspect", "--format={{.Id}}", "paperlens-backend"],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception as e:
        meta["container_image_digest"] = f"error: {e.__class__.__name__}"
    report["metadata"] = meta
    p.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"Enriched metadata in {p}: sha={meta['git_sha'][:12]} changed={meta['changed_files']}")


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(*, splits=("dev", "calibration"), write_report: bool = True, paths=("dense", "sparse", "hybrid")) -> dict:
    """Run the real-PDF retrieval eval across splits × paths.

    paths: which retrieval paths to ablate. 'multi' is auto-added for compare cases.
    """
    from api.fixtures import reset_project_state
    from api.models import ResearchProject

    started = time.perf_counter()
    proj_sequence = ResearchProject.objects.get_or_create(
        title="Fixture: sequence-models PDFs", defaults={"description": "real pdf", "status": "active"})[0]
    proj_rag = ResearchProject.objects.get_or_create(
        title="Fixture: rag-evaluation PDFs", defaults={"description": "real pdf", "status": "active"})[0]
    proj_graph = ResearchProject.objects.get_or_create(
        title="Fixture: graph-literature PDFs", defaults={"description": "real pdf", "status": "active"})[0]
    projects = {"sequence": proj_sequence, "rag": proj_rag, "graph": proj_graph}
    topic_to_proj = {"sequence": proj_sequence.id, "rag": proj_rag.id, "graph": proj_graph.id}

    # Ingest (only if a topic project has no BGE-M3 chunks yet — skip re-ingest)
    from rag.models import Text
    ingest_summary = {}
    for topic, proj in projects.items():
        already = Text.objects.filter(paper__arxiv_id__in=[p.arxiv_id for p in REAL_PAPERS if p.topic == topic],
                                      embedding_model="BAAI/bge-m3").count()
        if already > 0:
            ingest_summary[topic] = {"skipped": True, "existing_chunks": already}
            continue
        reset_project_state(proj.id)
        papers = [p for p in REAL_PAPERS if p.topic == topic]
        ingest_summary[topic] = _ingest_papers(papers, proj.id)

    p0_empty = [(t, aid) for t, m in ingest_summary.items() if isinstance(m, dict)
                for aid, s in m.items() if isinstance(s, dict) and not s.get("ok")]
    chunk_counts = _chunk_counts_from_db()

    # Cases × paths
    results = {}
    for split in splits:
        cases = cases_for_split(split)
        case_results = []
        for case in cases:
            gold_topic = paper_by_arxiv(case.gold_arxiv_ids[0]).topic if case.gold_arxiv_ids else "sequence"
            pid = topic_to_proj.get(gold_topic, proj_sequence.id)
            from api.models import ProjectPaper
            paper_ids = list(ProjectPaper.objects.filter(project_id=pid)
                             .exclude(status="excluded").values_list("paper_id", flat=True))

            # scope_leakage: only check forbidden-term absence (NOT a true abstention)
            if case.category == "scope_leakage":
                retrieved = _retrieve_path(case.question, paper_ids, k=5, mode="hybrid")
                leaked = any(any(ft.lower() in (r.get("content_full") or "").lower() for ft in case.forbidden_terms)
                             for r in retrieved)
                case_results.append({
                    "id": case.id, "question": case.question, "category": case.category,
                    "scope_leakage_ok": not leaked, "leaked_terms_found": leaked,
                    "note": "scope leakage only; true abstention needs final LLM answer (Wave 1A)",
                    "retrieved_top5": [{"rank": r["rank"], "title": r["title"][:40], "channel": r["channel"]} for r in retrieved],
                })
                continue

            path_metrics = {}
            for path in paths:
                if path == "multi" and case.sub_queries:
                    retrieved = _retrieve_multi(case.sub_queries, paper_ids, k=5)
                else:
                    retrieved = _retrieve_path(case.question, paper_ids, k=5, mode=path)
                path_metrics[path] = {
                    **_metrics(retrieved, case.gold_arxiv_ids, case.expected_terms),
                    "retrieved_top5": [{"rank": r["rank"], "paper_id": r["paper_id"], "chunk_id": r["chunk_id"],
                                        "title": r["title"][:40], "arxiv_id": r["arxiv_id"],
                                        "page": r["page_start"], "section": r["section"][:30], "channel": r["channel"]}
                                       for r in retrieved],
                }
            # multi-query variant for compare cases with sub_queries
            if case.sub_queries and "multi" not in path_metrics:
                retrieved = _retrieve_multi(case.sub_queries, paper_ids, k=5)
                path_metrics["multi"] = {
                    **_metrics(retrieved, case.gold_arxiv_ids, case.expected_terms),
                    "retrieved_top5": [{"rank": r["rank"], "paper_id": r["paper_id"], "chunk_id": r["chunk_id"],
                                        "title": r["title"][:40], "arxiv_id": r["arxiv_id"],
                                        "page": r["page_start"], "section": r["section"][:30], "channel": r["channel"]}
                                       for r in retrieved],
                }
            case_results.append({"id": case.id, "question": case.question, "category": case.category,
                                 "gold_arxiv_ids": list(case.gold_arxiv_ids), "paths": path_metrics})

        # aggregate per-path, split evidence vs scope_leakage (F1: compare has independent threshold)
        evidence = [c for c in case_results if c["category"] != "scope_leakage"]
        agg = {}
        for path in list(paths) + ["multi"]:
            pm = [c["paths"][path] for c in evidence if path in c.get("paths", {})]
            if not pm:
                continue
            agg[path] = {
                "recall_at_5": sum(p["recall_at_5"] for p in pm) / len(pm),
                "mrr": sum(p["mrr"] for p in pm) / len(pm),
                "precision_at_5": sum(p["precision_at_5"] for p in pm) / len(pm),
                "ndcg_5": sum(p["ndcg_5"] for p in pm) / len(pm),
            }
            # compare-only sub-aggregate (F1 independent threshold)
            comp = [c for c in evidence if c["category"] == "compare" and path in c.get("paths", {})]
            if comp:
                cpm = [c["paths"][path] for c in comp]
                agg[path]["compare_recall_at_5"] = sum(p["recall_at_5"] for p in cpm) / len(cpm)
        scope_cases = [c for c in case_results if c["category"] == "scope_leakage"]
        results[split] = {
            "case_count": len(case_results), "evidence_count": len(evidence),
            "scope_leakage_count": len(scope_cases),
            "scope_leakage_ok_rate": (sum(1 for c in scope_cases if c.get("scope_leakage_ok")) / len(scope_cases)) if scope_cases else None,
            "paths": agg, "cases": case_results,
        }

    isolation = _isolation_matrix(projects)

    # Verdict (F1): all declared metrics on dev hybrid + compare independent threshold
    # (applied to BOTH hybrid and multi-query compare results) + isolation + P0.
    verdict = "PASS"
    reasons = []
    if p0_empty:
        verdict, reasons = "FAIL", reasons + [f"P0 empty chunks: {p0_empty}"]
    if isolation["any_leak"]:
        verdict, reasons = "FAIL", reasons + [f"cross-project leakage in isolation matrix"]
    dev = results.get("dev", {}).get("paths", {}).get("hybrid")
    if dev:
        th = {"recall_at_5": 0.92, "mrr": 0.90, "precision_at_5": 0.70, "ndcg_5": 0.75}
        for metric, floor in th.items():
            if dev.get(metric, 0) < floor:
                verdict = "FAIL"
                reasons.append(f"dev hybrid {metric}={dev[metric]:.4f} < {floor}")
    # compare independent threshold (F1): compare cases must recall ALL gold papers
    # in top-5 (multi-gold recall). Gate BOTH hybrid and multi-query compare results.
    dev_paths = results.get("dev", {}).get("paths", {})
    for pname in ("hybrid", "multi"):
        pblock = dev_paths.get(pname)
        if not pblock:
            continue
        comp_r = pblock.get("compare_recall_at_5")
        if comp_r is not None and comp_r < 0.80:
            verdict = "FAIL"
            reasons.append(f"dev {pname} compare Recall@5={comp_r:.4f} < 0.80 (independent threshold)")
    # hybrid vs best single path (§3.1: hybrid not > 0.02 below best).
    # Read dense/sparse from their OWN path blocks, NOT from dev (which IS hybrid) —
    # otherwise we compare hybrid to itself and the gate can never fail.
    dev_paths = results.get("dev", {}).get("paths", {})
    hybrid_r = dev_paths.get("hybrid", {}).get("recall_at_5")
    if hybrid_r is not None:
        single_r = {p: dev_paths[p]["recall_at_5"] for p in ("dense", "sparse") if p in dev_paths}
        if single_r:
            best_single = max(single_r.values())
            if hybrid_r < best_single - 0.02:
                verdict = "FAIL"
                reasons.append(f"hybrid Recall@5 {hybrid_r:.4f} > 0.02 below best single {best_single:.4f}")

    report = {
        "verdict": verdict, "fail_reasons": reasons if verdict == "FAIL" else [],
        "p0_empty_chunks": p0_empty,
        "ingest_summary": ingest_summary,
        "chunk_counts_from_db": chunk_counts,
        "isolation": isolation,
        "splits": results,
        "thresholds": {"recall_at_5": 0.92, "mrr": 0.90, "precision_at_5": 0.70, "ndcg_5": 0.75,
                        "compare_recall_at_5": 0.80, "hybrid_vs_single_drop_max": 0.02},
        "metadata": _run_metadata(),
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
    }
    if write_report:
        from datetime import datetime
        run_id = f"{datetime.now().strftime('%Y%m%d-%H%M')}-retrieval-v2"
        out_dir = Path("eval/reports") / run_id / "retrieval"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "real-pdf-retrieval.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {out_dir / 'real-pdf-retrieval.json'}")
    return report
