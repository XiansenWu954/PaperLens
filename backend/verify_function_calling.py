"""验证 spec Requirement 6 的两个缺失执行证据：

1. complete_with_tools 真实 Function Calling + 保留 reasoning
2. 429 退避真实触发（用本地 mock server 返回 429）
"""
from __future__ import annotations

import asyncio
import json
import os
import sys


def _setup_django() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()


# ============================================================
# 验证 1：complete_with_tools 保留 reasoning
# ============================================================
def verify_function_calling() -> dict:
    from llm.deepseek import DeepSeekClient

    # 定义一个论文搜索工具（OpenAI Function Calling schema）
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_papers",
                "description": "搜索计算机科学论文。返回相关论文列表。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索关键词，如 'transformer attention'",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "最大返回数，默认 5",
                        },
                    },
                    "required": ["query"],
                },
            },
        }
    ]

    client = DeepSeekClient()
    messages = [
        {"role": "system", "content": "你是 CS 论文研究助手。需要查论文时调用 search_papers 工具。"},
        {"role": "user", "content": "帮我找 3 篇关于 Mamba 状态空间模型的论文。"},
    ]
    r = client.complete_with_tools(messages, tools, max_tokens=2048)
    usage = r["usage"] or {}
    completion_detail = usage.get("completion_tokens_details") or {}
    reasoning_tokens = completion_detail.get("reasoning_tokens")

    tool_calls = r["tool_calls"]
    called = bool(tool_calls) and tool_calls[0]["name"] == "search_papers"
    # 保留 reasoning 的判定：有 reasoning_tokens（>0）即视为保留（complete_with_tools 不传 disabled）
    preserved = reasoning_tokens is not None and reasoning_tokens > 0

    print("=== 验证1: complete_with_tools Function Calling ===")
    print("  tool_calls:", json.dumps(tool_calls, ensure_ascii=False)[:200])
    print("  reasoning_tokens:", reasoning_tokens, "(保留 reasoning)" if preserved else "(无 reasoning)")
    print("  usage completion_tokens:", usage.get("completion_tokens"))
    print("  调用了 search_papers:", "✓" if called else "✗")
    print("  保留 reasoning:", "✓" if preserved else "✗")
    return {"called": called, "preserved": preserved, "tool_calls": tool_calls, "reasoning_tokens": reasoning_tokens}


# ============================================================
# 验证 2：429 退避真实触发
# ============================================================
async def verify_429_retry() -> dict:
    """启动一个本地 mock server，先返回 429(带 Retry-After)，再返回 200，
    断言 ratelimit.fetch_json 按退避重试并最终成功。"""
    from aiohttp import web  # noqa
    import aiohttp

    # 计数器：前 N 次返回 429
    state = {"count": 0, "retry_after_value": None}

    async def handler(request: web.Request):
        state["retry_after_value"] = request.query.get("ra", "1")
        if state["count"] < 2:
            state["count"] += 1
            return web.Response(
                status=429,
                headers={"Retry-After": request.query.get("ra", "1")},
                text="rate limited",
            )
        return web.json_response({"ok": True, "attempt": state["count"]})

    app = web.Application()
    app.router.add_get("/test", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18923)
    await site.start()
    print("=== 验证2: 429 退避（mock server 先2次429再200）===")

    try:
        # ratelimit.fetch_json 用 httpx，直接打 mock server
        # 注意：mock 在 localhost，ratelimit 的 _respect_interval 对未知 source 无延迟
        from datasources.ratelimit import fetch_json
        import time

        t0 = time.time()
        data = await fetch_json(
            "test_source",
            "http://127.0.0.1:18923/test",
            params={"ra": "1"},
            max_retries=4,
            base_delay=0.1,
            max_delay=1.0,
            timeout=10.0,
        )
        elapsed = time.time() - t0
        retried_ok = isinstance(data, dict) and data.get("ok") is True
        print(f"  mock 收到请求次数: {state['count']}（前2次429，第3次200）")
        print(f"  fetch_json 最终返回: {data}")
        print(f"  耗时: {elapsed:.2f}s（应 ≥2s 退避等待）")
        print(f"  按退避重试并成功: {'✓' if retried_ok else '✗'}")
        print(f"  退避生效（耗时>1.5s）: {'✓' if elapsed > 1.5 else '✗'}")
        return {"retried_ok": retried_ok, "attempts": state["count"], "elapsed": elapsed}
    finally:
        await runner.cleanup()


def main() -> int:
    _setup_django()
    print("=" * 60)
    print("补验证：spec 弱验证场景执行证据")
    print("=" * 60)

    print()
    fc = verify_function_calling()

    print()
    retry = asyncio.run(verify_429_retry())

    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    print(f"  complete_with_tools 调用工具: {'✓' if fc['called'] else '✗'}")
    print(f"  complete_with_tools 保留 reasoning: {'✓' if fc['preserved'] else '✗'} (reasoning_tokens={fc['reasoning_tokens']})")
    print(f"  429 退避重试成功: {'✓' if retry['retried_ok'] else '✗'}")
    print(f"  退避等待生效: {'✓' if retry['elapsed'] > 1.5 else '✗'} ({retry['elapsed']:.2f}s)")
    ok = fc["called"] and fc["preserved"] and retry["retried_ok"] and retry["elapsed"] > 1.5
    print("=" * 60)
    print(f"{'两个弱验证场景全部补齐 ✓' if ok else '仍有失败 ✗'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
