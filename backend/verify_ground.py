"""
PaperLens 地基事实验证脚本（非功能代码）
目的：在写任何 spec / 功能代码前，验证四条数据通路是否真的可用。
输出：逐项 PASS/FAIL + 关键字段采样。
"""
import json
import os
import sys
import time
import urllib.parse

import urllib.request


def load_env(path: str) -> dict:
    env = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def http_get(url: str, headers: dict | None = None, timeout: int = 30) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return -1, str(e).encode()


def http_post_json(url: str, body: dict, headers: dict | None = None, timeout: int = 30) -> tuple[int, bytes]:
    data = json.dumps(body).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return -1, str(e).encode()


def main() -> int:
    env = load_env(os.path.join(os.path.dirname(__file__), ".env"))
    results = {}

    # ============================================================
    # 检查 1：DeepSeek —— key 可用性 + 当前生效模型名
    # ============================================================
    print("\n" + "=" * 60)
    print("检查 1: DeepSeek API（key + 模型名）")
    print("=" * 60)
    key = env.get("DEEPSEEK_API_KEY", "")
    base = env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    print(f"  base_url: {base}")
    print(f"  key 前缀: {key[:8]}...{key[-4:]}（不打印全文）")

    # 1a. 尝试官方 /models 端点列出可用模型
    code, body = http_get(f"{base}/models", {"Authorization": f"Bearer {key}"})
    print(f"  GET /models -> HTTP {code}")
    models_listed = []
    if code == 200:
        try:
            mj = json.loads(body)
            models_listed = [m.get("id") for m in mj.get("data", [])]
            print(f"  可用模型: {models_listed}")
        except Exception as e:
            print(f"  解析 /models 失败: {e}")
    else:
        print(f"  /models 不可用，回退到 chat 测试。body 片段: {body[:200]!r}")

    # 1b. 逐个测试候选模型名（v4-flash / v4-pro / deepseek-chat）
    candidates = []
    if models_listed:
        candidates = models_listed
    else:
        candidates = ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat"]
    working_model = None
    for m in candidates:
        payload = {
            "model": m,
            "messages": [{"role": "user", "content": "回复两个字：通了"}],
            "max_tokens": 10,
            "temperature": 0,
        }
        code, body = http_post_json(
            f"{base}/chat/completions", payload, {"Authorization": f"Bearer {key}"}
        )
        snippet = ""
        if code == 200:
            try:
                snippet = json.loads(body)["choices"][0]["message"]["content"]
            except Exception:
                snippet = body[:120].decode(errors="replace")
        status = "PASS" if code == 200 else "FAIL"
        print(f"  模型 {m}: HTTP {code} [{status}] 回复={snippet!r}")
        if code == 200 and working_model is None:
            working_model = m
        # 仅在通过 /models 列出全部时，测试会很多，限制一下
        if len(candidates) > 4 and working_model:
            break
    results["deepseek"] = working_model

    # ============================================================
    # 检查 2：OpenAlex —— 元数据 + referenced_works（护城河数据通路）
    # ============================================================
    print("\n" + "=" * 60)
    print("检查 2: OpenAlex（CS 论文 + referenced_works 引用图谱字段）")
    print("=" * 60)
    oa_base = env.get("OPENALEX_BASE_URL", "https://api.openalex.org")
    oa_mail = env.get("OPENALEX_EMAIL", "")
    # CS 概念 C41008148，搜 transformer attention，取 1 条
    q = (
        f"{oa_base}/works?search=transformer+attention"
        f"&filter=concepts.id:C41008148,publication_year:2024"
        f"&per-page=2&mailto={oa_mail}"
    )
    code, body = http_get(q)
    print(f"  搜索 HTTP {code}")
    ref_works_ok = False
    sample_title = ""
    if code == 200:
        try:
            data = json.loads(body)
            works = data.get("results", [])
            print(f"  返回论文数: {len(works)}, 总数: {data.get('meta', {}).get('count')}")
            if works:
                w = works[0]
                sample_title = w.get("title", "")[:80]
                rw = w.get("referenced_works", [])
                print(f"  样本论文: {sample_title!r}")
                print(f"  referenced_works 字段存在: {bool(rw)}, 数量: {len(rw)}")
                if rw:
                    ref_works_ok = True
                    print(f"  示例引用: {rw[:3]}")
                ab = w.get("abstract_inverted_index")
                print(f"  摘要(倒排索引)存在: {ab is not None}")
        except Exception as e:
            print(f"  解析失败: {e}")
    else:
        print(f"  body 片段: {body[:200]!r}")
    results["openalex_referenced_works"] = ref_works_ok

    # ============================================================
    # 检查 3：ArXiv —— 预印本元数据（PDF 全文 RAG 用）
    # ============================================================
    print("\n" + "=" * 60)
    print("检查 3: ArXiv API（预印本元数据）")
    print("=" * 60)
    ax_base = env.get("ARXIV_BASE_URL", "http://export.arxiv.org/api/query")
    q = f"{ax_base}?search_query=all:transformer+attention&max_results=1"
    code, body = http_get(q)
    print(f"  搜索 HTTP {code}")
    arxiv_ok = False
    if code == 200:
        text = body.decode(errors="replace")
        # 简单判定：Atom feed 里有 entry 和 pdf link
        if "<entry>" in text and "pdf" in text.lower():
            arxiv_ok = True
            import re

            t = re.search(r"<title>(.*?)</title>", text[text.find("<entry>"):])
            pdf = re.search(r'href="(http[^"]+\.pdf)"', text)
            print(f"  样本论文标题: {t.group(1)[:80] if t else 'N/A'!r}")
            print(f"  PDF 链接: {pdf.group(1) if pdf else 'N/A'}")
        else:
            print(f"  响应不含 entry/pdf。片段: {text[:200]!r}")
    else:
        print(f"  body 片段: {body[:200]!r}")
    results["arxiv"] = arxiv_ok

    # ============================================================
    # 检查 4：DBLP —— CS 会议/作者元数据补全
    # ============================================================
    print("\n" + "=" * 60)
    print("检查 4: DBLP API（CS 会议/作者元数据）")
    print("=" * 60)
    dblp_base = env.get("DBLP_BASE_URL", "https://dblp.org/search/publ/api")
    q = f"{dblp_base}?q=attention+is+all+you+need&format=json&h=1"
    code, body = http_get(q)
    print(f"  搜索 HTTP {code}")
    dblp_ok = False
    if code == 200:
        try:
            data = json.loads(body)
            hits = data.get("result", {}).get("hits", {}).get("hit", [])
            print(f"  返回 hits: {len(hits)}")
            if hits:
                info = hits[0].get("info", {})
                print(f"  样本: {info.get('title','')[:80]!r} @ {info.get('venue','')}")
                dblp_ok = True
        except Exception as e:
            print(f"  解析失败: {e}")
    else:
        print(f"  body 片段: {body[:200]!r}")
    results["dblp"] = dblp_ok

    # ============================================================
    # 汇总
    # ============================================================
    print("\n" + "=" * 60)
    print("地基验证汇总")
    print("=" * 60)
    for k, v in results.items():
        mark = "✓ PASS" if v else "✗ FAIL"
        print(f"  {k}: {mark}  ({v})")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
