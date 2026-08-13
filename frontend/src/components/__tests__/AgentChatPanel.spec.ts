import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import AgentChatPanel from '../AgentChatPanel.vue'

/**
 * AgentChatPanel is the primary research surface. The manual §5.4 calls out the
 * critical interaction contracts: send disables repeat-submit, tool/token
 * events stream visibly, done closes the stream (no infinite reconnect), error
 * shows a recoverable message without leaking env vars, and graph events embed
 * inline. AgentChatPanel uses `new EventSource(...)` directly (not useSse), so
 * we install a fake EventSource and mock the chat-session API from types.
 */

const listProjectChatSessions = vi.fn()
vi.mock('../../types', async () => {
  const actual = await vi.importActual<typeof import('../../types')>('../../types')
  return {
    ...actual,
    listProjectChatSessions: (...args: unknown[]) => listProjectChatSessions(...args),
    projectChatStreamUrl: (projectId: number, message: string) =>
      `/api/projects/${projectId}/chat/0/stream?q=${encodeURIComponent(message)}`,
  }
})

interface FakeES {
  listeners: Record<string, ((ev: MessageEvent) => void)[]>
  closed: boolean
  onopen: ((ev: Event) => void) | null
  onerror: ((ev: Event) => void) | null
  close: () => void
  emit: (type: string, data?: unknown) => void
  open: () => void
}
const sources: FakeES[] = []

beforeEach(() => {
  sources.length = 0
  listProjectChatSessions.mockReset()
  listProjectChatSessions.mockResolvedValue([])
  // The fake instance IS the entry in `sources`, so close()/emit() mutate the
  // same object the tests assert against — no aliasing drift.
  class MockEventSource {
    listeners: Record<string, ((ev: MessageEvent) => void)[]> = {}
    closed = false
    onopen: ((ev: Event) => void) | null = null
    onerror: ((ev: Event) => void) | null = null
    url: string
    constructor(url: string) {
      this.url = url
    }
    addEventListener(type: string, h: (ev: MessageEvent) => void) {
      ;(this.listeners[type] ||= []).push(h)
    }
    close() {
      this.closed = true
    }
    emit(type: string, data?: unknown) {
      const payload = typeof data === 'undefined' ? '{}' : JSON.stringify(data)
      for (const h of this.listeners[type] || []) h(new MessageEvent(type, { data: payload }))
    }
    open() {
      this.onopen?.(new Event('open'))
    }
  }
  // Intercept construction so each `new EventSource(...)` returns a tracked instance.
  globalThis.EventSource = function (url: string) {
    const inst = new MockEventSource(url)
    sources.push(inst as unknown as FakeES)
    return inst
  } as unknown as typeof EventSource
})

afterEach(() => {
  vi.useRealTimers()
})

function mountPanel() {
  return mount(AgentChatPanel, { props: { projectId: 1 } })
}

async function send(panel: ReturnType<typeof mountPanel>, text: string) {
  await panel.find('#agent-message').setValue(text)
  await panel.find('.composer .primary-button').trigger('click')
}

describe('AgentChatPanel', () => {
  it('shows the empty state with quick-prompt chips before any message', () => {
    const panel = mountPanel()
    expect(panel.find('.empty').exists()).toBe(true)
    // 5 quick prompts (manual: lower the "what can I ask" barrier).
    expect(panel.findAll('.prompt-chip')).toHaveLength(5)
  })

  it('sends a message, pushes user + assistant items, and enters busy state', async () => {
    const panel = mountPanel()
    await send(panel, '比较这些论文的方法差异')
    await flushPromises()
    // 2 transcript items: the user message + an empty assistant placeholder.
    expect(panel.findAll('.message')).toHaveLength(2)
    expect(panel.find('.message.user').text()).toContain('比较这些论文的方法差异')
    expect(panel.find('aside.chat').attributes('aria-busy')).toBe('true')
  })

  it('disables repeat submission while busy', async () => {
    const panel = mountPanel()
    await send(panel, '问题一')
    const sendBtn = panel.find('.composer .primary-button')
    expect((sendBtn.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('streams tokens into the assistant message without leaking raw JSON', async () => {
    const panel = mountPanel()
    await send(panel, '问题')
    const es = sources[0]
    es.open()
    es.emit('token', { text: 'Hel' })
    es.emit('tool_call', { name: 'query_project_rag', arguments: { q: '问题' }, summary: '3 evidence' })
    es.emit('token', { text: 'lo' })
    await flushPromises()

    const assistant = panel.find('.message.assistant')
    expect(assistant.text()).toContain('Hello')
    // tool_call shows up in the trace summary, token payloads do not.
    expect(panel.find('details.trace-panel').text()).toContain('1 tool')
    expect(assistant.text()).not.toContain('{"text":"Hel"}')
  })

  it('closes the EventSource and emits refreshed on done (no reconnect)', async () => {
    const panel = mountPanel()
    await send(panel, '问题')
    const es = sources[0]
    es.emit('done')
    await flushPromises()
    expect(es.closed).toBe(true)
    expect(panel.emitted('refreshed')).toBeTruthy()
    // done flips busy off (the send button stays disabled only because the
    // composer was cleared on send — that's expected, not a stuck-busy bug).
    expect(panel.find('aside.chat').attributes('aria-busy')).toBe('false')
    // Only one EventSource ever created — done does not open another.
    expect(sources).toHaveLength(1)
  })

  it('shows a recoverable error note (no env-var leak) on a server error event', async () => {
    const panel = mountPanel()
    await send(panel, '问题')
    sources[0].emit('error', { message: '模型调用失败' })
    await flushPromises()
    expect(panel.find('p.error-note').text()).toContain('模型调用失败')
    expect(panel.text()).not.toMatch(/DEEPSEEK_API_KEY|os\.environ/)
  })

  it('embeds the graph inline when a graph event arrives (manual §5.6)', async () => {
    const panel = mountPanel()
    await send(panel, '生成图谱')
    sources[0].emit('graph', { nodes: [{ id: 1, title: 'Mamba' }], edges: [] })
    await flushPromises()
    // The assistant message should now contain an inline graph wrapper.
    expect(panel.find('.message.assistant .inline-graph').exists()).toBe(true)
  })

  it('stop() closes the stream and leaves partial output intact', async () => {
    const panel = mountPanel()
    await send(panel, '问题')
    const es = sources[0]
    es.emit('token', { text: '部分答案' })
    await panel.find('.composer button.secondary-button').trigger('click')
    expect(es.closed).toBe(true)
    // Partial output is retained, not cleared.
    expect(panel.find('.message.assistant').text()).toContain('部分答案')
  })
})
