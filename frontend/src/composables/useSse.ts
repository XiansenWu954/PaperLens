/** SSE client for the PaperLens research stream. */
import { ref } from 'vue'
import type { GraphData, SseStatus, StepEvent } from '../types'

export interface SseHandlers {
  onStep?: (event: StepEvent) => void
  onToken?: (text: string) => void
  onGraph?: (graph: GraphData) => void
  onDone?: () => void
  onError?: (message: string) => void
  onStatus?: (status: SseStatus) => void
  onRetry?: (attempt: number) => void
}

export function useSse(url: string, handlers: SseHandlers) {
  const status = ref<SseStatus>('idle')
  let source: EventSource | null = null
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let attempt = 0
  let closed = false

  function setStatus(next: SseStatus) {
    status.value = next
    handlers.onStatus?.(next)
  }

  function connect() {
    if (closed) return
    setStatus(attempt > 0 ? 'retrying' : 'connecting')
    source = new EventSource(url)

    source.onopen = () => {
      attempt = 0
      setStatus('open')
    }

    source.addEventListener('step', (event) => {
      try {
        handlers.onStep?.(JSON.parse((event as MessageEvent).data))
      } catch {}
    })

    source.addEventListener('token', (event) => {
      try {
        const data = JSON.parse((event as MessageEvent).data)
        handlers.onToken?.(data.text || '')
      } catch {}
    })

    source.addEventListener('graph', (event) => {
      try {
        handlers.onGraph?.(JSON.parse((event as MessageEvent).data))
      } catch {}
    })

    source.addEventListener('done', () => {
      source?.close()
      setStatus('closed')
      handlers.onDone?.()
    })

    source.addEventListener('error', (event) => {
      if (status.value === 'closed') return
      source?.close()
      try {
        const data = JSON.parse((event as MessageEvent).data)
        if (data?.message) {
          setStatus('error')
          handlers.onError?.(data.message)
          return
        }
      } catch {}

      if (!closed && attempt < 3) {
        attempt += 1
        setStatus('retrying')
        handlers.onRetry?.(attempt)
        retryTimer = setTimeout(connect, Math.min(1000 * 2 ** attempt, 8000))
      } else {
        setStatus('error')
        handlers.onError?.('实时连接中断，已停止自动重连。')
      }
    })
  }

  function close() {
    closed = true
    if (retryTimer) clearTimeout(retryTimer)
    source?.close()
    setStatus('closed')
  }

  connect()

  return { status, close }
}
