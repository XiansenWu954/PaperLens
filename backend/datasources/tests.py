"""datasources 单元测试：缓存 TTL + 归一化/去重 + 429 退避（mock，不依赖外网）。"""
import asyncio
import datetime as dt
from unittest import mock

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from datasources.base import cached_search, dedupe_source_id, normalize
from datasources.models import DatasourceCache, query_hash
from datasources import ratelimit


class NormalizeTest(TestCase):
    def test_normalize_required_fields(self):
        p = normalize(source="x", source_id="1", title="T", year=2024, doi="10.1/a")
        self.assertEqual(p["source"], "x")
        self.assertEqual(p["doi"], "10.1/a")
        self.assertEqual(p["referenced_works"], [])
        self.assertEqual(p["authors"], [])

    def test_normalize_strips_whitespace(self):
        p = normalize(source="x", source_id="1", title="  T  ", abstract="  A  ")
        self.assertEqual(p["title"], "T")
        self.assertEqual(p["abstract"], "A")


class DedupeKeyTest(TestCase):
    def test_prefers_doi(self):
        k = dedupe_source_id({"doi": "10.1/a", "arxiv_id": "x", "title": "t"})
        self.assertEqual(k, "doi:10.1/a")

    def test_falls_back_to_arxiv(self):
        k = dedupe_source_id({"arxiv_id": "2401.1", "title": "t"})
        self.assertEqual(k, "arxiv_id:2401.1")

    def test_falls_back_to_title(self):
        k = dedupe_source_id({"title": "  Some Title  "})
        self.assertEqual(k, "title:some title")


class CacheTTLTest(TestCase):
    def test_cache_miss_then_hit(self):
        qh = query_hash("s", "q", max_results=5)
        DatasourceCache.set("s", qh, [{"title": "cached"}])
        got = DatasourceCache.get("s", qh)
        self.assertEqual(got, [{"title": "cached"}])

    def test_cache_expired_returns_none(self):
        qh = query_hash("s", "q")
        DatasourceCache.set("s", qh, [{"x": 1}])
        # 手动把 fetched_at 调到 30 天前
        DatasourceCache.objects.filter(source="s", qhash=qh).update(
            fetched_at=timezone.now() - dt.timedelta(days=30)
        )
        self.assertIsNone(DatasourceCache.get("s", qh, ttl_days=7))

    def test_query_hash_stable(self):
        self.assertEqual(query_hash("s", "q", max_results=5), query_hash("s", "q", max_results=5))

    def test_query_hash_different_params(self):
        self.assertNotEqual(query_hash("s", "q", max_results=5), query_hash("s", "q", max_results=10))


class CachedSearchTest(TransactionTestCase):
    # 用 TransactionTestCase：cached_search 走 asyncio event loop，在 TestCase 的
    # 嵌套事务里并发写 SQLite 会 "database table is locked"。TransactionTestCase
    # 每测试后真实提交，避免该锁。

    def test_cached_search_hits_cache_not_fetch(self):
        async def fake_fetch(query, max_results):
            raise AssertionError("不应调用 fetch（应命中缓存）")

        # 预填缓存
        qh = query_hash("src", "q", max_results=10)
        DatasourceCache.set("src", qh, [{"title": "pre"}])
        results, hit = asyncio.run(cached_search("src", "q", fake_fetch, max_results=10))
        self.assertTrue(hit)
        self.assertEqual(results, [{"title": "pre"}])

    def test_cached_search_miss_calls_fetch(self):
        calls = {"n": 0}

        async def fake_fetch(query, max_results):
            calls["n"] += 1
            return [{"title": "fetched"}]

        results, hit = asyncio.run(cached_search("src_fresh", "q", fake_fetch, max_results=10))
        self.assertFalse(hit)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(results, [{"title": "fetched"}])
        # 第二次应命中
        results2, hit2 = asyncio.run(cached_search("src_fresh", "q", fake_fetch, max_results=10))
        self.assertTrue(hit2)
        self.assertEqual(calls["n"], 1)


class RateLimit429Test(TestCase):
    """spec Requirement：收到 429 重试。用 mock httpx 响应，不依赖外网。"""

    def test_429_retries_then_succeeds(self):
        # 构造 mock 响应序列：两次 429，第三次 200
        call_count = {"n": 0}

        class FakeHeader:
            def __init__(self, d):
                self._d = d

            def get(self, k, default=None):
                return self._d.get(k, default)

        class FakeResp:
            def __init__(self, status, body, retry_after=None):
                self.status_code = status
                self._body = body
                self.headers = FakeHeader({"Retry-After": str(retry_after)} if retry_after else {})

            def json(self):
                return self._body

            def raise_for_status(self):
                pass

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None, headers=None):
                call_count["n"] += 1
                if call_count["n"] <= 2:
                    return FakeResp(429, {"e": "limited"}, retry_after=0)  # ra=0 加速
                return FakeResp(200, {"ok": True})

        with mock.patch("datasources.ratelimit.httpx.AsyncClient", FakeClient):
            # 跳过最小间隔（test_source 无间隔），base_delay 调小加速
            data = asyncio.run(
                ratelimit.fetch_json("test_source", "http://x", max_retries=4, base_delay=0.01, max_delay=0.05, timeout=5)
            )
        self.assertEqual(data, {"ok": True})
        self.assertEqual(call_count["n"], 3)  # 2次429 + 1次200

    def test_429_exhausted_raises(self):
        class FakeHeader:
            def get(self, k, default=None):
                return default

        class FakeResp:
            status_code = 429
            headers = FakeHeader()

            def json(self):
                return {}

            def raise_for_status(self):
                pass

        class FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None, headers=None):
                return FakeResp()

        with mock.patch("datasources.ratelimit.httpx.AsyncClient", FakeClient):
            with self.assertRaises(ratelimit.RateLimitError):
                asyncio.run(
                    ratelimit.fetch_json("test_source", "http://x", max_retries=2, base_delay=0.01, max_delay=0.05)
                )


class RegistryDedupeTest(TransactionTestCase):
    """registry 去重逻辑（不调真实源，用 fake searcher）。

    用 TransactionTestCase：search() 走 asyncio + 缓存写库，避免 SQLite 锁。
    """

    def test_registry_dedupes_by_source_id(self):
        from datasources.base import PaperSearcher

        class FakeA:
            name = "fakeA"

            async def search(self, query, max_results=10):
                return [normalize(source="fakeA", source_id="1", title="Shared", doi="10.1/x")]

        class FakeB:
            name = "fakeB"

            async def search(self, query, max_results=10):
                # 同 doi，应被去重
                return [
                    normalize(source="fakeB", source_id="2", title="Shared", doi="10.1/x"),
                    normalize(source="fakeB", source_id="3", title="Unique"),
                ]

        with mock.patch.dict("datasources.registry.REGISTRY", {"fakeA": FakeA(), "fakeB": FakeB()}):
            from datasources.registry import search
            results = asyncio.run(search("q", sources=["fakeA", "fakeB"], max_results=10))
        self.assertEqual(len(results), 2)  # Shared 去重 + Unique
        titles = [r["title"] for r in results]
        self.assertIn("Shared", titles)
        self.assertIn("Unique", titles)

    def test_registry_single_source_failure_does_not_block(self):
        class FailSrc:
            name = "fail"

            async def search(self, query, max_results=10):
                raise RuntimeError("boom")

        class OkSrc:
            name = "ok"

            async def search(self, query, max_results=10):
                return [normalize(source="ok", source_id="1", title="OK")]

        with mock.patch.dict("datasources.registry.REGISTRY", {"fail": FailSrc(), "ok": OkSrc()}):
            from datasources.registry import search
            results = asyncio.run(search("q", sources=["fail", "ok"], max_results=10))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "OK")

    def test_registry_single_source_timeout_does_not_block(self):
        class SlowSrc:
            name = "slow"

            async def search(self, query, max_results=10):
                await asyncio.sleep(0.05)
                return [normalize(source="slow", source_id="slow", title="Too late")]

        class OkSrc:
            name = "ok"

            async def search(self, query, max_results=10):
                return [normalize(source="ok", source_id="1", title="OK")]

        with (
            mock.patch.dict("datasources.registry.REGISTRY", {"slow": SlowSrc(), "ok": OkSrc()}),
            mock.patch("datasources.registry.SOURCE_TIMEOUT_SECONDS", 0.001),
        ):
            from datasources.registry import search

            results = asyncio.run(search("q", sources=["slow", "ok"], max_results=10))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["title"], "OK")

    def test_default_sources_include_dblp(self):
        from datasources.registry import DEFAULT_SOURCES

        self.assertIn("dblp", DEFAULT_SOURCES)
