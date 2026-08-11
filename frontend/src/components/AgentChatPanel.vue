<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import CitationGraph from './CitationGraph.vue'
import { listProjectChatSessions, projectChatStreamUrl, type ChatSession, type GraphData } from '../types'

const props = defineProps<{ projectId: number }>()
const emit = defineEmits<{ refreshed: [] }>()

type ConnectionState = 'idle' | 'connecting' | 'open' | 'closed' | 'error'
type ChatRole = 'user' | 'assistant' | 'tool' | 'system'
type TraceEvent = {
  type: string
  label: string
  detail: string
  payload: Record<string, any>
  count?: number
  items?: Record<string, any>[]
}
type TranscriptItem = { role: ChatRole; content: string; events?: TraceEvent[]; graphData?: GraphData }

const message = ref('')
const busy = ref(false)
const loadingSessions = ref(false)
const error = ref('')
const connection = ref<ConnectionState>('idle')
const sessionId = ref<number | null>(null)
const sessions = ref<ChatSession[]>([])
const transcript = ref<TranscriptItem[]>([])
let stream: EventSource | null = null

const prompts = [
  '比较这些论文的方法差异',
  '继续扩大论文范围',
  '生成 related work 草稿',
  '刷新引用图谱并推荐先读',
  '导出项目论文为 BibTeX',
]

const connectionLabel = computed(() => {
  const labels: Record<ConnectionState, string> = {
    idle: '待机',
    connecting: '连接中',
    open: '生成中',
    closed: '就绪',
    error: '异常',
  }
  return labels[connection.value]
})

onMounted(loadSessions)
watch(() => props.projectId, () => {
  closeStream()
  sessionId.value = null
  transcript.value = []
  loadSessions()
})

async function loadSessions() {
  if (!Number.isFinite(props.projectId)) return
  loadingSessions.value = true
  try {
    sessions.value = await listProjectChatSessions(props.projectId)
    if (!sessionId.value && sessions.value.length) {
      selectSession(sessions.value[0])
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载聊天历史失败'
  } finally {
    loadingSessions.value = false
  }
}

function selectSession(session: ChatSession) {
  if (busy.value) return
  sessionId.value = session.id
  transcript.value = session.messages
    .filter((item) => item.role === 'user' || item.role === 'assistant')
    .map((item) => ({ role: item.role, content: item.content }))
  error.value = ''
  connection.value = 'closed'
}

function closeStream() {
  if (stream) {
    stream.close()
    stream = null
  }
}

async function send() {
  const value = message.value.trim()
  if (!value || busy.value) return
  closeStream()
  busy.value = true
  error.value = ''
  connection.value = 'connecting'
  transcript.value.push({ role: 'user', content: value })
  const assistant: TranscriptItem = { role: 'assistant', content: '', events: [] }
  transcript.value.push(assistant)
  const assistantIndex = transcript.value.length - 1
  message.value = ''

  stream = new EventSource(projectChatStreamUrl(props.projectId, value, sessionId.value))
  stream.onopen = () => {
    connection.value = 'open'
  }
  stream.onerror = () => {
    // 无论 busy 状态都关闭流，避免 done 后的 onerror 误触发导致 EventSource 永久重连。
    closeStream()
    if (!busy.value) return
    connection.value = 'error'
    const currentAssistant = transcript.value[assistantIndex]
    error.value = currentAssistant?.content
      ? '连接中断，已保留当前输出。'
      : 'Agent 连接失败，请确认后端服务正在运行且已配置 DEEPSEEK_API_KEY。'
    busy.value = false
    emit('refreshed')
  }

  for (const eventName of [
    'harness_started',
    'intent_detected',
    'tool_call',
    'tool_result',
    'search_results',
    'paper_added',
    'evidence',
    'graph',
    'llm_call',
    'llm_result',
    'quality_check',
    'done',
    'error',
  ]) {
    stream.addEventListener(eventName, (event) => {
      const payload = parsePayload(event as MessageEvent)
      transcript.value[assistantIndex]?.events?.push(describeEvent(eventName, payload))
      // graph 事件：把图谱数据存到消息上，供消息体内嵌入渲染
      if (eventName === 'graph' && transcript.value[assistantIndex]) {
        transcript.value[assistantIndex].graphData = payload as GraphData
      }
      if (typeof payload.session_id === 'number') sessionId.value = payload.session_id
      if (eventName === 'error') {
        error.value = payload.message || 'Agent Chat 失败'
        busy.value = false
        connection.value = 'error'
        closeStream()
        loadSessions()
      }
      if (eventName === 'done') {
        busy.value = false
        connection.value = 'closed'
        closeStream()
        loadSessions()
        emit('refreshed')
      }
    })
  }

  stream.addEventListener('token', (event) => {
    const payload = parsePayload(event as MessageEvent)
    if (transcript.value[assistantIndex]) {
      transcript.value[assistantIndex].content += payload.text || ''
    }
  })
}

function parsePayload(event: MessageEvent): Record<string, any> {
  try {
    return JSON.parse(event.data || '{}')
  } catch {
    return {}
  }
}

function describeEvent(eventName: string, payload: Record<string, any>): TraceEvent {
  if (eventName === 'harness_started') {
    return { type: eventName, label: `Run #${payload.run_id || '-'}`, detail: `session #${payload.session_id || '-'}`, payload }
  }
  if (eventName === 'intent_detected') {
    const tools = Array.isArray(payload.planned_tools) && payload.planned_tools.length ? payload.planned_tools.join(' -> ') : 'no autonomous tools'
    return { type: eventName, label: `Intent: ${payload.intent || 'unknown'}`, detail: tools, payload }
  }
  if (eventName === 'tool_call') {
    return { type: eventName, label: `Call ${payload.name}`, detail: payload.summary || formatArgs(payload.arguments), payload }
  }
  if (eventName === 'tool_result') {
    return { type: eventName, label: `Result ${payload.name}`, detail: formatResult(payload), payload }
  }
  if (eventName === 'search_results') {
    const count = Array.isArray(payload.papers) ? payload.papers.length : payload.count || 0
    return { type: eventName, label: `Search ${count}`, detail: 'candidate papers returned', payload, count, items: payload.papers || [] }
  }
  if (eventName === 'evidence') {
    const count = Array.isArray(payload.evidence) ? payload.evidence.length : 0
    return { type: eventName, label: `Evidence ${count}`, detail: payload.fallback || 'project-scoped RAG returned evidence', payload, count, items: payload.evidence || [] }
  }
  if (eventName === 'paper_added') {
    const added = Array.isArray(payload.added) ? payload.added.length : payload.count || 0
    return { type: eventName, label: `Library +${added}`, detail: 'synced to Evidence Board', payload, count: added, items: payload.added || [] }
  }
  if (eventName === 'graph') {
    const nodes = Array.isArray(payload.nodes) ? payload.nodes.length : 0
    const edges = Array.isArray(payload.edges) ? payload.edges.length : 0
    return { type: eventName, label: `Graph ${nodes} nodes`, detail: `${edges} relations`, payload }
  }
  if (eventName === 'quality_check') {
    return {
      type: eventName,
      label: `Quality: ${payload.verdict || 'unknown'}`,
      detail: `${payload.evidence_count || 0} evidence · ${payload.source_marker_count || 0} sources`,
      payload,
    }
  }
  if (eventName === 'llm_call') {
    return { type: eventName, label: 'DeepSeek answer', detail: String(payload.model || payload.mode || ''), payload }
  }
  if (eventName === 'llm_result') {
    return { type: eventName, label: `LLM ${payload.status || 'done'}`, detail: `${payload.answer_chars || 0} chars · ${Math.round(Number(payload.duration_ms || 0))} ms`, payload }
  }
  if (eventName === 'done') return { type: eventName, label: 'Done', detail: `run #${payload.run_id || '-'}`, payload }
  if (eventName === 'error') return { type: eventName, label: 'Error', detail: payload.message || '', payload }
  return { type: eventName, label: eventName, detail: '', payload }
}

function formatArgs(args: Record<string, any> = {}) {
  const entries = Object.entries(args)
    .filter(([key]) => key !== 'project_id')
    .map(([key, value]) => `${key}: ${String(value).slice(0, 80)}`)
  return entries.join(' · ')
}

function formatResult(payload: Record<string, any>) {
  return Object.entries(payload)
    .filter(([key]) => key !== 'name')
    .map(([key, value]) => `${key}: ${String(value).slice(0, 80)}`)
    .join(' · ')
}

function stop() {
  if (!busy.value) return
  busy.value = false
  connection.value = 'closed'
  closeStream()
}

function retry() {
  if (busy.value || transcript.value.length < 2) return
  const previous = [...transcript.value].reverse().find((item) => item.role === 'user')
  if (previous) {
    message.value = previous.content
    send()
  }
}

function applyPrompt(value: string) {
  if (busy.value) return
  message.value = value
}

function handleComposerKeydown(event: KeyboardEvent) {
  if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return
  event.preventDefault()
  send()
}

function toolCallCount(events: TraceEvent[] = []) {
  return events.filter((event) => event.type === 'tool_call').length
}

function evidenceFromEvents(events: TraceEvent[] = []) {
  const event = [...events].reverse().find((item) => item.type === 'evidence')
  const items = event?.items || event?.payload.evidence
  return Array.isArray(items) ? items.slice(0, 3) : []
}

function addedFromEvents(events: TraceEvent[] = []) {
  const event = [...events].reverse().find((item) => item.type === 'paper_added')
  const items = event?.items || event?.payload.added
  return Array.isArray(items) ? items.slice(0, 3) : []
}

function searchResultsFromEvents(events: TraceEvent[] = []) {
  const event = [...events].reverse().find((item) => item.type === 'search_results')
  const items = event?.items || event?.payload.papers
  return Array.isArray(items) ? items.slice(0, 3) : []
}

function qualityFromEvents(events: TraceEvent[] = []) {
  return [...events].reverse().find((event) => event.type === 'quality_check')?.payload || null
}

function formatChatContent(content: string) {
  const lines = content.split(/\r?\n/)
  const html: string[] = []
  let inList = false
  const closeList = () => {
    if (inList) {
      html.push('</ul>')
      inList = false
    }
  }

  for (const line of lines) {
    const trimmed = line.trim()
    if (!trimmed) {
      closeList()
      continue
    }
    const heading = trimmed.match(/^#{1,3}\s+(.+)$/)
    if (heading) {
      closeList()
      html.push(`<h3>${formatInline(heading[1])}</h3>`)
      continue
    }
    const bullet = trimmed.match(/^[-*]\s+(.+)$/)
    if (bullet) {
      if (!inList) {
        html.push('<ul>')
        inList = true
      }
      html.push(`<li>${formatInline(bullet[1])}</li>`)
      continue
    }
    closeList()
    html.push(`<p>${formatInline(trimmed)}</p>`)
  }
  closeList()
  return html.join('')
}

function formatInline(value: string) {
  return escapeHtml(value)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
}

function escapeHtml(value: string) {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

onBeforeUnmount(closeStream)
</script>

<template>
  <aside class="chat shell-panel" :aria-busy="busy">
    <div class="chat-head">
      <div>
        <h2>Agent Chat</h2>
      </div>
      <span :class="['stream-state', connection]">{{ connectionLabel }}</span>
    </div>

    <details v-if="sessions.length || loadingSessions" class="sessions">
      <summary>{{ loadingSessions ? '加载历史' : `${sessions.length} 个会话` }}</summary>
      <button
        v-for="session in sessions.slice(0, 4)"
        :key="session.id"
        class="session-row"
        :class="{ active: session.id === sessionId }"
        :disabled="busy"
          type="button"
          @click="selectSession(session)"
      >
        <span>{{ session.title }}</span>
        <small>{{ session.messages.length }} messages</small>
      </button>
    </details>

    <div class="messages">
      <div v-if="!transcript.length" class="empty">
        <div class="prompt-list">
          <button v-for="prompt in prompts" :key="prompt" type="button" class="prompt-chip" @click="applyPrompt(prompt)">{{ prompt }}</button>
        </div>
      </div>
      <article v-for="(item, index) in transcript" :key="index" :class="['message', item.role]">
        <strong>{{ item.role === 'user' ? 'You' : 'PaperLens Agent' }}</strong>
        <div
          class="message-content"
          v-html="formatChatContent(item.content || (busy && index === transcript.length - 1 ? '正在检索项目证据...' : ''))"
        />
        <div v-if="item.role === 'assistant' && item.events?.length" class="answer-meta">
          <span v-if="qualityFromEvents(item.events)" :class="['quality-pill', qualityFromEvents(item.events)?.verdict]">
            {{ qualityFromEvents(item.events)?.verdict }}
          </span>
          <span v-for="source in evidenceFromEvents(item.events)" :key="`${source.paper_id || source.title}-${source.citation || ''}`">
            {{ source.title || source.docname || `paper ${source.paper_id}` }}
          </span>
          <span v-for="paper in searchResultsFromEvents(item.events)" :key="`search-${paper.title}`">
            候选 · {{ paper.title }}
          </span>
          <span v-for="paper in addedFromEvents(item.events)" :key="paper.paper_id || paper.title">
            {{ paper.created ? '新增' : '已存在' }} · {{ paper.title }}
          </span>
        </div>
        <!-- 图谱嵌入：graph 事件触发的图谱直接在消息内渲染 -->
        <div v-if="item.graphData && item.graphData.nodes?.length" class="inline-graph">
          <CitationGraph :data="item.graphData" />
        </div>
        <details v-if="item.role === 'assistant' && item.events?.length" class="trace-panel">
          <summary>{{ toolCallCount(item.events) }} tools · {{ evidenceFromEvents(item.events).length }} evidence</summary>
          <div class="events">
            <span v-for="(event, eventIndex) in item.events" :key="`${event.type}-${eventIndex}`" :class="event.type">
              <b>{{ event.label }}</b>
              <small v-if="event.detail">{{ event.detail }}</small>
            </span>
          </div>
        </details>
      </article>
    </div>

    <p v-if="error" class="error-note" role="alert">{{ error }}</p>

    <div class="composer">
      <label class="sr-only" for="agent-message">向项目 Agent 提问</label>
      <textarea
        id="agent-message"
        v-model="message"
        rows="3"
        placeholder="向项目 Agent 提问，或要求继续检索/补充论文"
        @keydown="handleComposerKeydown"
      />
      <div class="composer-actions">
        <button type="button" class="secondary-button" :disabled="!busy" @click="stop">停止</button>
        <button type="button" class="secondary-button" :disabled="busy || transcript.length < 2" @click="retry">重试</button>
        <button type="button" class="primary-button" :disabled="busy || !message.trim()" @click="send">{{ busy ? '运行中' : '发送' }}</button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.chat {
  display: grid;
  grid-template-rows: auto auto minmax(320px, 1fr) auto auto;
  gap: 12px;
  min-height: 620px;
  padding: 16px;
}

h2,
p {
  margin: 0;
}

.chat-head,
.section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.chat-head h2 {
  font-size: 16px;
}

.sessions {
  display: grid;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface-subtle);
}

.sessions summary,
.trace-panel summary {
  color: var(--text-muted);
  cursor: pointer;
  font-size: 12px;
  font-weight: 650;
}

.session-row,
.prompt-chip {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-soft);
  cursor: pointer;
  text-align: left;
}

.session-row {
  display: grid;
  gap: 2px;
  padding: 7px 8px;
}

.session-row.active,
.session-row:hover:not(:disabled) {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.session-row span,
.session-row small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-row span {
  color: var(--text);
  font-size: 12px;
  font-weight: 650;
}

.session-row small {
  color: var(--text-muted);
  font-size: 11px;
}

.messages {
  display: grid;
  align-content: start;
  gap: 10px;
  overflow: auto;
}

.empty,
.message {
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface-subtle);
}

.message.user {
  background: var(--accent-soft);
}

.message strong {
  display: block;
  margin-bottom: 5px;
  font-size: 12px;
}

.message-content {
  color: var(--text-soft);
  font-size: 13px;
}

.message-content :deep(p) {
  margin: 0 0 8px;
}

.message-content :deep(p:last-child),
.message-content :deep(ul:last-child) {
  margin-bottom: 0;
}

.message-content :deep(h3) {
  margin: 10px 0 6px;
  color: var(--text);
  font-size: 13px;
  line-height: 1.35;
}

.message-content :deep(ul) {
  margin: 0 0 8px;
  padding-left: 18px;
}

.message-content :deep(li) {
  margin: 4px 0;
}

.message-content :deep(code) {
  padding: 1px 4px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--surface);
  color: var(--text);
  font-family: var(--mono);
  font-size: 12px;
}

.prompt-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.prompt-chip {
  min-height: 30px;
  padding: 0 9px;
  font-size: 12px;
}

.prompt-chip:hover {
  border-color: var(--border-strong);
  background: var(--surface-muted);
}

.answer-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 9px;
}

.answer-meta span,
.quality-pill {
  max-width: 100%;
  overflow: hidden;
  padding: 3px 7px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.quality-pill.grounded {
  border-color: #a9c6be;
  color: var(--ok);
}

.quality-pill.partial,
.quality-pill.needs_more_evidence,
.quality-pill.needs_source_markers {
  border-color: #e1c48e;
  color: var(--warn);
}

.trace-panel {
  margin-top: 8px;
}

.inline-graph {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  max-height: 360px;
  overflow: hidden;
}

.events {
  display: grid;
  gap: 5px;
  margin-top: 8px;
}

.events span {
  display: grid;
  gap: 2px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-muted);
  font-size: 11px;
}

.events span.error {
  border-color: #e7bab3;
  background: var(--err-soft);
}

.events b {
  color: var(--text-soft);
  font-weight: 700;
}

.events small {
  overflow-wrap: anywhere;
  font-size: 11px;
}

.composer {
  display: grid;
  gap: 8px;
}

.composer-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.stream-state {
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 3px 8px;
  background: var(--surface-subtle);
  color: var(--text-muted);
  font-size: 11px;
}

.stream-state.open {
  border-color: #a9c6be;
  color: var(--ok);
}

.stream-state.error {
  border-color: #e7bab3;
  color: var(--err);
}

textarea {
  width: 100%;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--paper);
  resize: vertical;
}

.error-note {
  padding: 8px 10px;
  border: 1px solid #e7bab3;
  border-radius: var(--radius-sm);
  background: var(--err-soft);
  color: var(--err);
  font-size: 12px;
}

@media (max-width: 1120px) {
  .chat {
    min-height: 520px;
  }
}
</style>
