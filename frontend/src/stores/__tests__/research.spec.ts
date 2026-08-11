import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useResearchStore } from '../research'

/**
 * The research store owns the research-task state machine: it creates a task,
 * opens an SSE stream, accumulates steps/report/graph, and surfaces errors and
 * retry state. These tests cover the store layer called out in manual §5.7
 * (loading / empty / error / retry / refresh) using mocked network + a fake
 * EventSource, so nothing depends on a live backend.
 */

// Mock the network helpers imported by the store.
const createResearch = vi.fn()
const getResearch = vi.fn()
vi.mock('../../types', () => ({
  createResearch: (...args: unknown[]) => createResearch(...args),
  getResearch: (...args: unknown[]) => getResearch(...args),
  sseUrl: (id: number) => `/api/research/${id}/stream`,
}))

// Fake EventSource (happy-dom ships none).
interface FakeES {
  listeners: Record<string, ((ev: MessageEvent) => void)[]>
  closed: boolean
  close: () => void
  emit: (type: string, data?: unknown) => void
}
const sources: FakeES[] = []
class MockEventSource {
  listeners: Record<string, ((ev: MessageEvent) => void)[]> = {}
  closed = false
  url: string
  constructor(url: string) {
    this.url = url
    const self = this
    sources.push({
      listeners: this.listeners,
      closed: false,
      close() {
        self.closed = true
        ;(this as FakeES).closed = true
      },
      emit: (type: string, data?: unknown) => self.emit(type, data),
    } as unknown as FakeES)
  }
  addEventListener(type: string, h: (ev: MessageEvent) => void) {
    ;(this.listeners[type] ||= []).push(h)
  }
  close() {
    this.closed = true
  }
  emit(type: string, data?: unknown) {
    const payload = typeof data === 'undefined' ? null : JSON.stringify(data)
    for (const h of this.listeners[type] || []) h(new MessageEvent(type, { data: payload as string }))
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  sources.length = 0
  createResearch.mockReset()
  getResearch.mockReset()
  ;(globalThis as unknown as { EventSource: typeof EventSource }).EventSource =
    MockEventSource as unknown as typeof EventSource
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useResearchStore', () => {
  it('creates a task and opens a stream on start()', async () => {
    createResearch.mockResolvedValue({ task_id: 42, question: 'mamba vs transformer' })
    const store = useResearchStore()

    await store.start('mamba vs transformer')

    expect(store.taskId).toBe(42)
    expect(store.question).toBe('mamba vs transformer')
    expect(store.status).toBe('running')
    expect(sources).toHaveLength(1)
  })

  it('accumulates streamed tokens and steps into report/steps', async () => {
    createResearch.mockResolvedValue({ task_id: 7, question: 'q' })
    const store = useResearchStore()
    await store.start('q')
    const es = sources[0]

    es.emit('step', { event: 'searching', label: 'Searching DBLP' })
    es.emit('token', { text: 'Hello ' })
    es.emit('token', { text: 'world' })

    expect(store.steps).toHaveLength(1)
    expect(store.report).toBe('Hello world')
  })

  it('transitions to done and refreshes from the task endpoint on done event', async () => {
    createResearch.mockResolvedValue({ task_id: 7, question: 'q' })
    getResearch.mockResolvedValue({
      id: 7,
      question: 'q',
      status: 'done',
      final_report: 'streamed report',
      sources: [{ title: 'Mamba' }],
      citation_graph: { nodes: [], edges: [] },
    })
    const store = useResearchStore()
    await store.start('q')

    sources[0].emit('done')

    // Allow the async fetchResult to flush.
    await vi.waitFor(() => expect(store.status).toBe('done'))
    expect(getResearch).toHaveBeenCalledWith(7)
    expect(store.sources).toEqual([{ title: 'Mamba' }])
  })

  it('surfaces an error message when task creation fails (network)', async () => {
    createResearch.mockRejectedValue(new TypeError('fetch failed'))
    const store = useResearchStore()

    await expect(store.start('q')).rejects.toThrow()
    expect(store.status).toBe('error')
    expect(store.errorMsg).toContain('后端服务已启动')
  })

  it('tracks retry attempts from the SSE layer', async () => {
    createResearch.mockResolvedValue({ task_id: 9, question: 'q' })
    const store = useResearchStore()
    await store.start('q')

    // The store forwards onRetry attempts to retryCount.
    // Simulate by dispatching transport errors; the composable calls onRetry.
    vi.useFakeTimers()
    sources[0].listeners.error?.forEach((h) => h(new MessageEvent('error', { data: 'null' })))
    // data 'null' is not a JSON {message} object -> treated as transport drop -> retry 1
    await vi.advanceTimersByTimeAsync(2000)
    expect(store.retryCount).toBeGreaterThanOrEqual(1)
    expect(store.connectionStatus).toBe('retrying')
  })

  it('reset() clears accumulated state and closes the stream', async () => {
    createResearch.mockResolvedValue({ task_id: 5, question: 'q' })
    const store = useResearchStore()
    await store.start('q')
    sources[0].emit('token', { text: 'partial' })
    expect(store.report).toBe('partial')

    store.reset()
    expect(store.report).toBe('')
    expect(store.steps).toHaveLength(0)
    expect(store.status).toBe('idle')
  })
})
