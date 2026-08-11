import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useSse } from '../useSse'
import type { StepEvent } from '../../types'

/**
 * useSse is the SSE client for the research stream. These tests cover the
 * event-routing contract named in the comprehensive test manual §5.7:
 *   - token / step / graph events are routed to the right handler
 *   - `done` closes the EventSource (no infinite reconnect)
 *   - retries back off (attempt < 3, exponential, capped at 8s) then stops
 *
 * happy-dom has no native EventSource, so we install a fully fake one that
 * lets each test drive the listener callbacks deterministically.
 */

interface FakeEventSource {
  url: string
  onopen: ((ev: Event) => void) | null
  listeners: Record<string, ((ev: MessageEvent) => void)[]>
  closed: boolean
  close: () => void
  emit: (type: string, data?: unknown) => void
  open: () => void
}

function installFakeEventSource() {
  const instances: FakeEventSource[] = []
  class MockEventSource {
    url: string
    onopen: ((ev: Event) => void) | null = null
    listeners: Record<string, ((ev: MessageEvent) => void)[]> = {}
    closed = false

    constructor(url: string) {
      this.url = url
      instances.push(this as unknown as FakeEventSource)
    }

    addEventListener(type: string, handler: (ev: MessageEvent) => void) {
      ;(this.listeners[type] ||= []).push(handler)
    }

    close() {
      this.closed = true
    }

    // Test helpers
    emit(type: string, data?: unknown) {
      const handlers = this.listeners[type] || []
      const payload = typeof data === 'undefined' ? null : JSON.stringify(data)
      for (const h of handlers) h(new MessageEvent(type, { data: payload as string }))
    }

    open() {
      this.onopen?.(new Event('open'))
    }
  }

  const original = globalThis.EventSource
  globalThis.EventSource = MockEventSource as unknown as typeof EventSource
  return {
    instances,
    restore() {
      globalThis.EventSource = original
    },
  }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('useSse', () => {
  it('routes token, step, and graph events to their handlers', async () => {
    const { instances, restore } = installFakeEventSource()
    const onToken = vi.fn()
    const onStep = vi.fn()
    const onGraph = vi.fn()

    useSse('/api/research/1/stream', { onToken, onStep, onGraph })
    const es = instances[0]
    es.open()

    es.emit('token', { text: 'Hel' })
    es.emit('token', { text: 'lo' })
    es.emit('step', { event: 'searching' } as unknown as StepEvent)
    es.emit('graph', { nodes: [{ id: 'p1' }], edges: [] })

    expect(onToken).toHaveBeenCalledTimes(2)
    expect(onToken).toHaveBeenNthCalledWith(1, 'Hel')
    expect(onToken).toHaveBeenNthCalledWith(2, 'lo')
    expect(onStep).toHaveBeenCalledTimes(1)
    expect(onGraph).toHaveBeenCalledTimes(1)
    expect(onGraph.mock.calls[0][0].nodes).toHaveLength(1)
    restore()
  })

  it('closes the EventSource and transitions to "closed" on done (no reconnect)', async () => {
    const { instances, restore } = installFakeEventSource()
    const onDone = vi.fn()
    const onStatus = vi.fn()

    const { status } = useSse('/api/research/1/stream', { onDone, onStatus })
    const es = instances[0]
    es.open()
    // connecting -> open
    expect(onStatus).toHaveBeenLastCalledWith('open')

    es.emit('done')
    expect(es.closed).toBe(true)
    expect(onDone).toHaveBeenCalledOnce()
    expect(status.value).toBe('closed')
    // A second done must not create a new connection.
    expect(instances).toHaveLength(1)
    restore()
  })

  it('retries up to 3 times with exponential backoff then stops', async () => {
    const { instances, restore } = installFakeEventSource()
    const onRetry = vi.fn()
    const onError = vi.fn()

    useSse('/api/research/1/stream', { onRetry, onError })

    // First transport error (no JSON message payload) -> retry 1
    instances[0].emit('error', {})
    expect(onRetry).toHaveBeenLastCalledWith(1)
    // Retry 1 fires after 2s (1000 * 2**1)
    expect(instances).toHaveLength(1)
    vi.advanceTimersByTime(2000)
    expect(instances).toHaveLength(2)

    // Second transport error -> retry 2
    instances[1].emit('error', {})
    expect(onRetry).toHaveBeenLastCalledWith(2)
    vi.advanceTimersByTime(4000) // 1000 * 2**2
    expect(instances).toHaveLength(3)

    // Third transport error -> retry 3
    instances[2].emit('error', {})
    expect(onRetry).toHaveBeenLastCalledWith(3)
    // Backoff capped at 8s (min(1000 * 2**3, 8000))
    vi.advanceTimersByTime(8000)
    expect(instances).toHaveLength(4)

    // Fourth transport error -> attempt would be 4, exceeds cap -> terminal error
    instances[3].emit('error', {})
    expect(onError).toHaveBeenCalledOnce()
    expect(onError.mock.calls[0][0]).toMatch(/已停止自动重连/)
    // No 5th connection is ever opened
    vi.advanceTimersByTime(20000)
    expect(instances).toHaveLength(4)
    restore()
  })

  it('treats an error event with a JSON message payload as terminal (no retry)', async () => {
    const { instances, restore } = installFakeEventSource()
    const onError = vi.fn()
    const onRetry = vi.fn()

    useSse('/api/research/1/stream', { onError, onRetry })
    const es = instances[0]
    es.open()

    // Emit an `error` event carrying a JSON message: this is a server-sent
    // terminal error, distinct from a transport drop, and must not retry.
    es.emit('error', { message: '任务执行失败：模型超时' })

    expect(onError).toHaveBeenCalledOnce()
    expect(onError).toHaveBeenLastCalledWith('任务执行失败：模型超时')
    expect(onRetry).not.toHaveBeenCalled()
    restore()
  })

  it('close() stops reconnection and prevents further connect attempts', async () => {
    const { instances, restore } = installFakeEventSource()
    const { status, close } = useSse('/api/research/1/stream', {})

    instances[0].emit('error', {}) // would schedule retry 1
    close()
    expect(status.value).toBe('closed')

    // Advancing timers must NOT open a new connection after close().
    vi.advanceTimersByTime(20000)
    expect(instances).toHaveLength(1)
    restore()
  })
})
