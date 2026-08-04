/** Pinia store: research task state + SSE accumulation. */
import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { ClientStatus, GraphData, ResearchTask, SseStatus, StepEvent } from '../types'
import { createResearch, getResearch, sseUrl } from '../types'
import { useSse } from '../composables/useSse'

export const useResearchStore = defineStore('research', () => {
  const question = ref('')
  const taskId = ref<number | null>(null)
  const status = ref<ClientStatus>('idle')
  const connectionStatus = ref<SseStatus>('idle')
  const steps = ref<StepEvent[]>([])
  const report = ref('')
  const graph = ref<GraphData>({ nodes: [], edges: [] })
  const sources = ref<Record<string, unknown>[]>([])
  const errorMsg = ref('')
  const loadError = ref('')
  const retryCount = ref(0)
  let closeStream: (() => void) | null = null

  function reset() {
    closeStream?.()
    closeStream = null
    steps.value = []
    report.value = ''
    graph.value = { nodes: [], edges: [] }
    sources.value = []
    errorMsg.value = ''
    loadError.value = ''
    retryCount.value = 0
    connectionStatus.value = 'idle'
    status.value = 'idle'
  }

  async function start(q: string) {
    reset()
    question.value = q
    status.value = 'running'
    try {
      const { task_id } = await createResearch(q)
      taskId.value = task_id
      openStream(task_id)
    } catch (error) {
      status.value = 'error'
      errorMsg.value = friendlyError(error, '无法创建研究任务，请确认后端服务已启动。')
      throw error
    }
  }

  function openStream(id: number) {
    closeStream?.()
    const stream = useSse(sseUrl(id), {
      onStatus: (next) => {
        connectionStatus.value = next
      },
      onRetry: (attempt) => {
        retryCount.value = attempt
      },
      onStep: (event) => {
        steps.value.push({ ...event, receivedAt: new Date().toISOString() })
      },
      onToken: (text) => {
        report.value += text
      },
      onGraph: (nextGraph) => {
        graph.value = nextGraph || { nodes: [], edges: [] }
      },
      onDone: () => {
        status.value = 'done'
        fetchResult(id)
      },
      onError: (message) => {
        status.value = 'error'
        errorMsg.value = message
        fetchResult(id)
      },
    })
    closeStream = stream.close
  }

  async function fetchResult(id: number) {
    try {
      const task: ResearchTask = await getResearch(id)
      sources.value = task.sources || []
      if (task.final_report && !report.value) report.value = task.final_report
      if (task.citation_graph?.nodes?.length && !graph.value.nodes.length) graph.value = task.citation_graph
      if (task.status === 'done') status.value = 'done'
      if (task.status === 'error') {
        status.value = 'error'
        errorMsg.value = task.error_message || errorMsg.value
      }
    } catch {
      /* Fallback polling should not hide the stream state. */
    }
  }

  async function load(taskIdNum: number) {
    reset()
    try {
      const task: ResearchTask = await getResearch(taskIdNum)
      taskId.value = task.id
      question.value = task.question
      status.value = task.status === 'pending' ? 'running' : task.status
      report.value = task.final_report || ''
      graph.value = task.citation_graph || { nodes: [], edges: [] }
      sources.value = task.sources || []
      errorMsg.value = task.error_message || ''
      if (task.status === 'pending' || task.status === 'running') openStream(task.id)
    } catch (error) {
      status.value = 'error'
      loadError.value = friendlyError(error, '任务不存在或后端暂时不可用。')
      throw error
    }
  }

  function stopStream() {
    closeStream?.()
    closeStream = null
    connectionStatus.value = 'closed'
  }

  function friendlyError(error: unknown, fallback: string) {
    if (error instanceof TypeError) return fallback
    if (error instanceof Error) return error.message || fallback
    return fallback
  }

  return {
    question,
    taskId,
    status,
    connectionStatus,
    steps,
    report,
    graph,
    sources,
    errorMsg,
    loadError,
    retryCount,
    start,
    load,
    reset,
    stopStream,
  }
})
