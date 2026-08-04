# Design: add-sse-frontend

## 1. 后端目录新增
```
backend/
├── api/                    # DRF REST
│   ├── models.py           # ResearchTask
│   ├── views.py            # POST /api/research, GET /api/research/<id>
│   ├── urls.py
│   └── serializers.py
├── realtime/               # SSE
│   ├── views.py            # GET /api/research/<id>/stream
│   └── sse.py              # LangGraph astream → SSE 桥接
```

## 2. ResearchTask 模型（api/models.py）
```python
class ResearchTask(models.Model):
    question = models.TextField()
    status = models.CharField(default="pending")  # pending/running/done/error
    final_report = models.TextField(blank=True)
    citation_graph = models.JSONField(default=dict)  # vis_data
    sources = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
```

## 3. REST 端点（api/views.py）
- `POST /api/research` {question} → 创建 ResearchTask(status=pending)，返回 {task_id}。
- `GET /api/research/<id>` → 返回 {status, report, graph, sources}（完成后）。

## 4. SSE 端点（realtime/views.py）—— 核心
```python
async def research_stream(request, task_id):
    task = await sync_to_async(ResearchTask.objects.get)(id=task_id)
    task.status = "running"; await task.asave()

    async def event_stream():
        yield ": connected\n\n"
        try:
            graph = build_graph(DEFAULT_CONFIG)
            # astream 多模式：updates(节点) + messages(token)
            async for chunk in graph.astream(
                {"question": task.question},
                stream_mode=["updates", "messages"],
            ):
                async for evt in _map_chunk_to_sse(chunk, task):
                    yield evt
            # 完成：持久化
            await _persist(task)
            yield b"event: done\ndata: {}\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n".encode()

    resp = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp
```

## 5. chunk → SSE 映射（realtime/sse.py）
LangGraph `stream_mode=["updates","messages"]` v2 chunk 格式 `{type, data}`：
- `type=="updates"`：data 是 `{node_name: state_delta}`。
  - node=="citation_graph" 且 state_delta 含 citation_graph → 发 `event: graph`（vis_data）。
  - 其他 node → 发 `event: step` {node, delta_keys}。
- `type=="messages"`：data 是 (msg, meta)。msg.content 是 token 增量 → 发 `event: token` {text}。
  - 用 meta 里的 node 标签区分（synthesizer 的 token 才发给前端逐字渲染）。

```python
async def _map_chunk_to_sse(chunk, task):
    if chunk["type"] == "updates":
        for node, delta in chunk["data"].items():
            if node == "synthesizer" and "final_report" in delta:
                # 最终综述，发 step
                yield _sse("step", {"node": "synthesizer", "done": True})
            elif node == "citation_graph" and isinstance(delta, dict) and "citation_graph" in delta:
                yield _sse("graph", delta["citation_graph"].get("vis", {}))
            else:
                yield _sse("step", {"node": node})
    elif chunk["type"] == "messages":
        msg, meta = chunk["data"]
        if msg.content:  # token 增量
            yield _sse("token", {"text": msg.content})
```

## 6. CORS（config/settings.py）
```python
CORS_ALLOWED_ORIGINS = ["http://localhost:5173"]  # Vite dev
```
需装 django-cors-headers。

## 7. 前端结构（frontend/src）
```
src/
├── main.ts              # 挂载 + router + pinia
├── App.vue
├── router.ts            # / (search) /research/:id
├── stores/research.ts   # Pinia: SSE 状态 + 数据
├── composables/useSse.ts# EventSource + onScopeDispose + 退避重连
├── views/
│   ├── SearchView.vue       # 输入框 + 发起研究
│   └── ResearchView.vue     # SSE 时间线 + 图谱 + 报告
├── components/
│   ├── StepTimeline.vue     # agent 步骤时间线
│   ├── CitationGraph.vue    # d3-force 图谱可视化
│   ├── PaperCard.vue        # 论文详情卡片
│   └── ResearchReport.vue   # markdown 报告 + 来源
└── types.ts             # Node/Edge/Step 类型
```

## 8. useSse composable（缝合调研结论）
```ts
export function useSse(url: string, handlers: Handlers) {
  const es = new EventSource(url)
  es.addEventListener('step', e => handlers.onStep(JSON.parse(e.data)))
  es.addEventListener('token', e => handlers.onToken(JSON.parse(e.data)))
  es.addEventListener('graph', e => handlers.onGraph(JSON.parse(e.data)))
  es.addEventListener('done', () => { es.close(); handlers.onDone() })
  es.addEventListener('error', () => { /* 退避重连 */ })
  onScopeDispose(() => es.close())
}
```

## 9. CitationGraph.vue（d3-force）
```ts
// 用 d3-force 模拟 + canvas/svg 渲染
const sim = d3.forceSimulation(nodes)
  .force('link', d3.forceLink(edges).id(d => d.id))
  .force('charge', d3.forceManyBody().strength(-30))
  .force('center', d3.forceCenter(width/2, height/2))
// 节点 r = scale(citation_count)，fill = yearColorScale(year)
// 点击节点 → emit('select', node) → 弹 PaperCard
```

## 10. vite.config.ts 代理（避免 CORS 复杂）
可选：dev 用 vite proxy 把 /api 转发到 localhost:8000，省 CORS。两者择一，用 CORS 更标准。

## 11. 验证
- `curl -N localhost:8000/api/research/<id>/stream` 看到 step/token/graph/done 事件流。
- 前端 `npm run dev` → 输入问题 → 看到 agent 实时工作 + 图谱渲染 + 综述流式。

## 12. 测试
- 后端：ResearchTask CRUD、SSE chunk 映射（mock graph.astream）。
- 前端：useSse composable（mock EventSource）—— 前端测试可选，优先后端测试保覆盖。
