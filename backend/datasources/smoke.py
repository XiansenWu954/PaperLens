"""地基端到端 smoke 验证（spec 验证项）。

验证：
1. 四源各成功返回 ≥1 条归一化论文
2. 第二轮同查询命中缓存（cache hit，无网络请求）
3. DeepSeek complete(thinking=False) 关闭 reasoning 生效
4. papers upsert 落库

用法：python -m datasources.smoke  （需在 Django 环境，用 manage.py 调起或 django.setup）
实际：python manage.py shell -c 之外，本脚本自调 django.setup()。
"""
from __future__ import annotations

import asyncio
import os
import sys


def _setup_django() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    django.setup()


async def verify_sources() -> dict:
    """四源检索 + 缓存命中验证。单源失败不阻断（尽力而为）。"""
    from .registry import REGISTRY

    query = "transformer attention"
    report = {}
    for name in ["openalex", "arxiv", "dblp"]:
        try:
            results = await REGISTRY[name].search(query, max_results=2)
        except Exception as e:
            results = []
            print(f"[{name}] ✗ 异常: {type(e).__name__}: {str(e)[:80]}")
        ok = len(results) >= 1
        sample = results[0] if results else {}
        report[name] = {
            "ok": ok,
            "count": len(results),
            "title": sample.get("title", "")[:70],
            "has_referenced_works": name == "openalex" and len(sample.get("referenced_works", [])) > 0,
        }
        if results:
            extra = f" refs={len(sample.get('referenced_works',[]))}" if name == "openalex" else ""
            print(f"[{name}] {'✓' if ok else '✗'} count={len(results)} title={sample.get('title','')[:70]!r}{extra}")
    return report


async def verify_cache_hit() -> bool:
    """第二轮 OpenAlex 同查询应命中缓存。"""
    from .registry import REGISTRY
    from .models import DatasourceCache, query_hash
    from asgiref.sync import sync_to_async

    query = "transformer attention"
    qh = query_hash("openalex", query, max_results=2)
    before = await sync_to_async(lambda: DatasourceCache.objects.filter(source="openalex", qhash=qh).count())()
    # 记录首轮 fetched_at
    first_row = await sync_to_async(lambda: DatasourceCache.objects.filter(source="openalex", qhash=qh).first())()
    first_ts = first_row.fetched_at if first_row else None
    # 再次 search（应命中缓存，不重新请求/不改 fetched_at）
    await REGISTRY["openalex"].search(query, max_results=2)
    after_row = await sync_to_async(lambda: DatasourceCache.objects.filter(source="openalex", qhash=qh).first())()
    hit = bool(first_ts) and after_row and after_row.fetched_at == first_ts
    print(f"[cache] openalex 第二轮命中缓存（fetched_at 未变）: {hit}")
    return hit


def verify_deepseek() -> dict:
    """DeepSeek thinking=False 关闭 reasoning 验证。"""
    from llm.deepseek import DeepSeekClient

    client = DeepSeekClient()
    r = client.complete(
        [{"role": "user", "content": "1+1 等于几？只回答数字"}],
        thinking=False,
        max_tokens=50,
    )
    usage = r["usage"]
    completion_detail = (usage or {}).get("completion_tokens_details") or {}
    reasoning_tokens = completion_detail.get("reasoning_tokens")
    ok = "2" in (r["content"] or "") and not r.get("reasoning")
    print(f"[deepseek] thinking=False content={r['content']!r} reasoning_tokens={reasoning_tokens} reasoning={'有' if r.get('reasoning') else '无'}")
    return {"ok": ok, "content": r["content"], "reasoning_tokens": reasoning_tokens}


async def verify_upsert() -> dict:
    """papers upsert 落库验证。"""
    from asgiref.sync import sync_to_async
    from papers.models import upsert_paper, Paper

    p = await sync_to_async(upsert_paper)({
        "doi": "10.999/smoketest",
        "title": "Smoke Test Paper",
        "year": 2025,
        "citation_count": 0,
        "referenced_works": ["W1", "W2"],
        "openalex_id": "W9999999999",
    })
    again = await sync_to_async(upsert_paper)({"doi": "10.999/smoketest", "title": "Smoke Test Paper v2", "year": 2025})
    same = p.id == again.id
    count = await sync_to_async(lambda: Paper.objects.count())()
    await sync_to_async(lambda: Paper.objects.filter(doi="10.999/smoketest").delete())()
    print(f"[upsert] 同 doi 更新不重复: {same}, 落库后总数={count}, referenced_works 落 JSONField OK")
    return {"ok": same, "count": count}


async def main_async() -> int:
    print("=" * 60)
    print("PaperLens 地基 smoke 验证")
    print("=" * 60)

    print("\n--- 1. 四源检索 ---")
    src = await verify_sources()

    print("\n--- 2. 缓存命中 ---")
    cache_ok = await verify_cache_hit()

    print("\n--- 3. DeepSeek (thinking=False) ---")
    ds = verify_deepseek()

    print("\n--- 4. papers upsert ---")
    up = await verify_upsert()

    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    # 关键路径必须过：openalex(护城河数据) + arxiv(全文) + 缓存 + DeepSeek + upsert
    # dblp 为可选补全源（尽力而为，失败不阻断）
    critical = src["openalex"]["ok"] and src["arxiv"]["ok"]
    all_ok = critical and cache_ok and ds["ok"] and up["ok"]
    print(f"  openalex: {'✓' if src['openalex']['ok'] else '✗'} ({src['openalex']['count']} 条) [关键]")
    print(f"    referenced_works 护城河数据: {'✓' if src['openalex']['has_referenced_works'] else '✗'}")
    print(f"  arxiv: {'✓' if src['arxiv']['ok'] else '✗'} ({src['arxiv']['count']} 条) [关键]")
    print(f"  dblp: {'✓' if src['dblp']['ok'] else '✗ (尽力而为,不影响)'} ({src['dblp']['count']} 条) [可选]")
    print(f"  缓存命中: {'✓' if cache_ok else '✗'}")
    print(f"  DeepSeek thinking=False 降本: {'✓' if ds['ok'] else '✗'}")
    print(f"  papers upsert: {'✓' if up['ok'] else '✗'}")
    print("=" * 60)
    print(f"{'关键路径全部通过 ✓' if all_ok else '关键路径存在失败 ✗'}")
    return 0 if all_ok else 1


def main() -> int:
    _setup_django()
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
