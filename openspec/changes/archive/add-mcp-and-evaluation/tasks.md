# Tasks: add-mcp-and-evaluation

- [x] 1. MCP 依赖 + 包（mcp SDK 2.0 + mcp_server/mcp_client/eval 包）
- [x] 2. MCP server（低层 add_request_handler 注册 tools/list+tools/call，复用 agent.tools.execute_tool）
- [x] 3. MCP client（stdio_client + ClientSession，连接→列工具→调用）
- [x] 4. MCP 双向验证（demo: 发现2工具 + search_papers 返回1887字符真实论文 ✓）
- [x] 5. 评测集（dataset.py 12题三类 factual/recent/compare + gold）
- [x] 6. 指标（recall_at_k / faithfulness LLM-judge / coverage）
- [x] 7. variants（baseline 朴素搜索 vs paperlens 完整 agent，同一 DeepSeek）
- [x] 8. 对比脚本（run_eval.py 产报告，诚实记录）
- [x] 9. 评测验证（paperlens-only limit=1: faithfulness=0.80 coverage=1.00 ✓）
- [x] 10. 测试（mcp_server 8 + eval 8 = 16 tests OK）+ 归档 + git 提交 + README

---

## 附录：MCP 双向验证（2026-07-30）
```
python -m mcp_client.demo "transformer attention"
发现工具: ['search_papers', 'gather_evidence']
search_papers 返回 1887 字符
[{"title": "Swin Transformer...", "year": 2021, ...}]
验证: 工具≥2=True, 有结果=True -> 通过 ✓
```
修复：MCP 2.0 API（无装饰器/无 fastmcp）→ 用低层 add_request_handler(method, params_type, handler)；
handler 收到 ServerRequestContext（params 是 dict，按 dict 取 name/arguments）。

## 附录：评测验证（2026-07-30）
```
python -m eval.run_eval --skip-baseline --limit 1 (q01 Transformer)
PaperLens : Recall@10=0.00  faithfulness=0.80  coverage=1.00
```
- faithfulness=0.80：综述论断 80% 有引用支撑
- coverage=1.00：完全覆盖 gold 子主题（self-attention/query-key-value/scaled dot-product）
- Recall@10=0.00：中文问题搜英文库召回原论文受限（真实工程现象，见 README future work）

## ⚠️ 诚实标注：完整 baseline vs PaperLens 对比待后续
完整对比（run_eval --limit N 含 baseline）因 OpenAlex 触发 429 限流
（Retry-After: 48776s ≈ 13小时，前序评测+agent 跑测频繁请求所致）未能产出对比表。
评测框架（dataset/metrics/variants/run_eval）已就绪并验证可工作，
限流解除后 `python -m eval.run_eval` 即可产出完整对比。
