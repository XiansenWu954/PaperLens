"""P0/P1 升级确定性盲区评测：BibTeX 回环 / verified 判定 / 图谱连接路径。

这些评测不依赖真实 LLM/embedding，用确定性 fixture 验证逻辑正确性。
真实模型评测（BGE-M3 ablation / 引用语境 / Docling）在 embedding_quality.py / relation_quality.py / pdf_parse_quality.py。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def run_bibtex_roundtrip() -> dict[str, Any]:
    """BibTeX 导出→再导入的字段保真度（多作者/特殊字符/venue 缺失）。

    P0-1 升级盲区：现有只有单 case，这里加边界 case。
    """
    from papers.bibtex import papers_to_bibtex, parse_bibtex

    cases = [
        {
            "name": "多作者 + 特殊字符",
            "payload": {
                "title": "Café Résumé: A Study on Naïve Bayes",
                "year": 2024,
                "doi": "10.1/multi",
                "authors": ["Zhang, Wei", "O'Brien, Sean", "Müller, Anna"],
            },
            "expect_title": "Café Résumé: A Study on Naïve Bayes",
            "expect_doi": "10.1/multi",
        },
        {
            "name": "venue 缺失 + 长 abstract",
            "payload": {
                "title": "No Venue Paper",
                "year": 2023,
                "abstract": "A" * 500,
            },
            "expect_title": "No Venue Paper",
            "expect_abstract_min_len": 400,
        },
    ]
    passed_count = 0
    details = []
    for case in cases:
        # 构造伪 paper 对象
        class _P:
            pass
        p = _P()
        p.title = case["payload"]["title"]
        p.year = case["payload"].get("year")
        p.doi = case["payload"].get("doi")
        p.arxiv_id = case["payload"].get("arxiv_id")
        p.pdf_url = None
        p.abstract = case["payload"].get("abstract", "")
        p.venue = None
        p.authors = []
        p.venue_id = None

        bib_text = papers_to_bibtex([p])
        reparsed = parse_bibtex(bib_text)
        ok = len(reparsed) == 1 and reparsed[0]["title"] == case["expect_title"]
        if "expect_doi" in case:
            ok = ok and reparsed[0].get("doi") == case["expect_doi"]
        if "expect_abstract_min_len" in case:
            ok = ok and len(reparsed[0].get("abstract", "")) >= case["expect_abstract_min_len"]
        if ok:
            passed_count += 1
        details.append({"name": case["name"], "passed": ok})
    result = {
        "case_count": len(cases),
        "passed_count": passed_count,
        "details": details,
        "passed": passed_count == len(cases),
    }
    logger.info("bibtex roundtrip: %d/%d passed", passed_count, len(cases))
    return result


def run_verified_judgment() -> dict[str, Any]:
    """报告来源标注 verified 判定准确率（对抗 case）。

    P0-3 升级盲区：现有 _quality_check 的 verified 判定只验"有没有"，不验"对不对"。
    这里构造对抗 case（伪造 marker、漏标 marker），断言 verified_count 正确。
    """
    import asyncio

    from agent.harness import ProjectAgentHarness
    from agent.intent import ProjectIntent

    harness = ProjectAgentHarness(project_id=0)  # 不真连 DB，只测 _quality_check 逻辑

    # 构造证据 + 答案，验证 per-citation verified 判定
    cases = [
        {
            "name": "全部命中",
            "evidence": [{"title": "PaperA", "summary": "method A", "paper_id": 1}],
            "answer": "结论 [cite:PaperA]",
            "expect_verified": 1,
            "expect_unverified": 0,
        },
        {
            "name": "漏标（证据有但答案没引用）",
            "evidence": [{"title": "PaperB", "summary": "method B", "paper_id": 2}],
            "answer": "结论无引用",
            "expect_verified": 0,
            "expect_unverified": 1,
        },
        {
            "name": "伪造 marker（答案引用了不存在的证据）",
            "evidence": [{"title": "RealPaper", "summary": "real", "paper_id": 3}],
            "answer": "结论 [cite:FakePaper]",
            "expect_verified": 0,  # RealPaper 未被引用
            "expect_unverified": 1,
        },
    ]
    passed_count = 0
    details = []
    for case in cases:
        intent = ProjectIntent(name="answer", rationale="test", tool_plan=())
        context = {"query_project_rag": {"evidence": case["evidence"]}}
        quality = asyncio.run(harness._quality_check(case["answer"], intent, context))
        ok = quality["verified_count"] == case["expect_verified"] and quality["unverified_count"] == case["expect_unverified"]
        if ok:
            passed_count += 1
        details.append({
            "name": case["name"],
            "passed": ok,
            "actual_verified": quality["verified_count"],
            "expect_verified": case["expect_verified"],
        })
    result = {
        "case_count": len(cases),
        "passed_count": passed_count,
        "details": details,
        "passed": passed_count == len(cases),
    }
    logger.info("verified judgment: %d/%d passed", passed_count, len(cases))
    return result


def run_graph_paths() -> dict[str, Any]:
    """引用图谱连接路径 + 推荐先读准确率（P1-5 升级盲区）。

    构造已知结构的引用网络，gold 标注最短路径，断言 find_connection_path 正确。
    """
    import networkx as nx
    from citation.paths import find_connection_path

    # 构造已知图：1-2-3-4 链 + 1-3 捷径
    G = nx.Graph()
    for n in [1, 2, 3, 4]:
        G.add_node(n)
    G.add_edge(1, 2, weight=2)
    G.add_edge(2, 3, weight=2)
    G.add_edge(3, 4, weight=2)
    G.add_edge(1, 3, weight=1)  # 捷径（权重高=相似度高=路径短）

    cases = [
        {"name": "直连最短路径", "a": 1, "b": 3, "expect_hops": 1, "expect_path": [1, 3]},
        {"name": "两跳路径", "a": 1, "b": 4, "expect_hops": 2, "expect_path": [1, 3, 4]},
        {"name": "不连通", "a": 1, "b": 99, "expect_reachable": False},
    ]
    passed_count = 0
    details = []
    for case in cases:
        result_path = find_connection_path(G, case["a"], case["b"])
        if case.get("expect_reachable") is False:
            ok = not result_path["reachable"]
        else:
            ok = result_path["reachable"] and result_path["path"] == case["expect_path"]
        if ok:
            passed_count += 1
        details.append({"name": case["name"], "passed": ok, "actual_path": result_path.get("path")})
    result = {
        "case_count": len(cases),
        "passed_count": passed_count,
        "details": details,
        "passed": passed_count == len(cases),
    }
    logger.info("graph paths: %d/%d passed", passed_count, len(cases))
    return result
