/** PaperLens frontend contracts. */

export type ResearchStatus = 'pending' | 'running' | 'waiting_ingestion' | 'done' | 'partial' | 'error'
export type ClientStatus = ResearchStatus | 'idle'
export type SseStatus = 'idle' | 'connecting' | 'open' | 'retrying' | 'closed' | 'error'

export interface GraphNode {
  id: number
  title: string
  year: number | null
  citation_count: number
  size: number
  color_year: number | null
  cluster: number
  cluster_label?: string
  is_root: boolean
  is_frontier: boolean
  seminal: number
  frontier: number
  x: number
  y: number
  arxiv_id: string | null
  doi: string | null
}

export interface GraphEdge {
  source: number
  target: number
  weight: number
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface StepEvent {
  node: string
  done?: boolean
  plan?: string[]
  notes?: number
  sources?: number
  receivedAt?: string
}

export interface ResearchTask {
  id: number
  question: string
  status: ResearchStatus
  final_report: string
  citation_graph: GraphData
  sources: Record<string, unknown>[]
  error_message: string
  created_at?: string
  updated_at?: string
}

export interface ResearchProject {
  id: number
  title: string
  description: string
  status: 'active' | 'archived'
  paper_count: number
  run_count: number
  latest_report_id: number | null
  created_at: string
  updated_at: string
}

export type IngestionStatus =
  | 'pending' | 'downloading' | 'parsing' | 'embedding'
  | 'committing' | 'embedded' | 'failed'

export interface ProjectPaper {
  id: number
  paper_id: number
  title: string
  abstract: string
  year: number | null
  venue: string
  citation_count: number
  doi: string | null
  arxiv_id: string | null
  openalex_id: string | null
  pdf_url: string | null
  status: 'candidate' | 'included' | 'core' | 'excluded'
  source_reason: string
  added_by: string
  notes: string
  ingestion_status: IngestionStatus
  latest_ingestion_job_id: number | null
  latest_ingestion_error: string
  embedding_model: string
  indexed_at: string | null
  chunk_count: number
  fulltext_ready: boolean
  latest_job_retryable: boolean
  created_at: string
  updated_at: string
}

export interface PaperIngestionJob {
  id: number
  project: number
  paper: number
  paper_title: string
  status: 'pending' | 'parsing' | 'embedded' | 'failed'
  source_kind: string
  file_name: string
  file_hash: string
  chunk_count: number
  error_code: string
  error_message: string
  retryable: boolean
  fulltext_ready: boolean
  celery_task_id: string
  created_at: string
  updated_at: string
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant' | 'tool' | 'system'
  content: string
  metadata: Record<string, unknown>
  created_at: string
}

export interface ChatSession {
  id: number
  project: number
  title: string
  messages: ChatMessage[]
  created_at: string
  updated_at: string
}

export interface ReportVersion {
  id: number
  project: number
  title: string
  content: string
  source: string
  created_at: string
}

export interface ProjectRunEvent {
  id: number
  event_type: string
  payload: Record<string, unknown>
  created_at: string
}

export interface DependencySummary {
  ready: number
  pending: number
  succeeded: number
  failed: number
  unavailable: number
  total: number
}

export interface ProjectRun {
  id: number
  project: number
  kind: 'research' | 'chat' | 'report' | 'ingestion' | 'workflow' | 'demo'
  status: ResearchStatus
  question: string
  output: string
  error_message: string
  sources: Record<string, unknown>[]
  citation_graph: GraphData | Record<string, unknown>
  events: ProjectRunEvent[]
  created_at: string
  updated_at: string
  workflow_phase?: string
  resume_count?: number
  dependency_summary?: DependencySummary
  report_id?: number | null
}

export interface ChatRunResult {
  answer: string
  events: { event: string; data: Record<string, unknown> }[]
}

export class ApiError extends Error {
  status: number
  requestId: string | null
  detail: string

  constructor(message: string, status: number, requestId: string | null, detail = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.requestId = requestId
    this.detail = detail
  }
}

/**
 * 把任意错误转成对用户友好的中文文案，并附带 requestId（如有）便于排查。
 * 各视图的 catch 块应统一使用此函数，避免直接展示原始英文堆栈/裸状态码。
 */
export function describeError(error: unknown, fallback = '操作失败，请稍后重试'): string {
  if (error instanceof ApiError) {
    let hint: string
    if (error.status === 0 || error.message.includes('Failed to fetch')) {
      hint = '无法连接服务器，请确认后端服务正在运行'
    } else if (error.status === 401 || error.status === 403) {
      hint = '请求未授权'
    } else if (error.status === 404) {
      hint = '请求的资源不存在'
    } else if (error.status === 429) {
      hint = '请求过于频繁，请稍后再试'
    } else if (error.status >= 500) {
      hint = '服务器内部错误'
    } else {
      hint = error.detail || error.message || fallback
    }
    return error.requestId ? `${hint}（编号 ${error.requestId}）` : hint
  }
  if (error instanceof Error) {
    if (error.message.includes('Failed to fetch') || error.name === 'TypeError') {
      return '无法连接服务器，请确认后端服务正在运行'
    }
    return error.message || fallback
  }
  return fallback
}

export const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '')

async function parseResponse<T>(response: Response, fallback: string): Promise<T> {
  const requestId = response.headers.get('X-Request-ID')
  const text = await response.text()
  let payload: any = null
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      payload = text
    }
  }
  if (!response.ok) {
    const detail = typeof payload === 'object' ? payload?.error || payload?.detail || '' : String(payload || '')
    throw new ApiError(detail || fallback, response.status, requestId, detail)
  }
  return payload as T
}

export async function createResearch(question: string): Promise<{ task_id: number; question: string }> {
  const response = await fetch(`${API_BASE}/api/research`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  })
  return parseResponse(response, `创建任务失败: ${response.status}`)
}

export async function getResearch(taskId: number): Promise<ResearchTask> {
  const response = await fetch(`${API_BASE}/api/research/${taskId}`)
  return parseResponse(response, `查询失败: ${response.status}`)
}

export function sseUrl(taskId: number): string {
  return `${API_BASE}/api/research/${taskId}/stream`
}

export function projectChatStreamUrl(projectId: number, message: string, sessionId?: number | null): string {
  const params = new URLSearchParams({ message })
  return `${API_BASE}/api/projects/${projectId}/chat/${sessionId || 0}/stream?${params.toString()}`
}

export async function listProjects(): Promise<ResearchProject[]> {
  const response = await fetch(`${API_BASE}/api/projects`)
  return parseResponse(response, `查询项目失败: ${response.status}`)
}

export async function createProject(payload: { title: string; description?: string }): Promise<ResearchProject> {
  const response = await fetch(`${API_BASE}/api/projects`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return parseResponse(response, `创建项目失败: ${response.status}`)
}

export async function seedDemoProject(): Promise<{ project: ResearchProject; count: number }> {
  const response = await fetch(`${API_BASE}/api/projects/demo-seed`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  })
  return parseResponse(response, `创建演示项目失败: ${response.status}`)
}

export async function getProject(projectId: number): Promise<ResearchProject> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}`)
  return parseResponse(response, `查询项目失败: ${response.status}`)
}

export async function listProjectPapers(projectId: number): Promise<ProjectPaper[]> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/papers`)
  return parseResponse(response, `查询项目论文失败: ${response.status}`)
}

export async function searchAddProjectPapers(projectId: number, query: string, maxResults = 5): Promise<{ count: number; results: Record<string, unknown>[] }> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/papers/search-add`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, max_results: maxResults }),
  })
  return parseResponse(response, `检索添加失败: ${response.status}`)
}

export async function updateProjectPaper(projectId: number, paperId: number, payload: Partial<ProjectPaper>): Promise<ProjectPaper> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/papers/${paperId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return parseResponse(response, `更新论文状态失败: ${response.status}`)
}

export async function removeProjectPaper(projectId: number, paperId: number): Promise<void> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/papers/${paperId}`, { method: 'DELETE' })
  await parseResponse(response, `移除论文失败: ${response.status}`)
}

export interface ImportResult {
  format: string
  count: number
  added: { paper_id: number; title: string; created: boolean }[]
}

export async function importProjectPapers(
  projectId: number,
  text: string,
  format: 'bibtex' | 'ris',
): Promise<ImportResult> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/papers/import`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, format }),
  })
  return parseResponse(response, `导入失败: ${response.status}`)
}

export function exportPapersUrl(projectId: number, format: 'bib' | 'ris'): string {
  return `${API_BASE}/api/projects/${projectId}/papers/export.${format}`
}

export interface ConnectionPath {
  path: number[]
  reachable: boolean
  hops?: number
  nodes?: { id: number; title: string; year: number | null; citation_count: number }[]
  edges?: { source: number; target: number; weight: number }[]
  reason?: string
}

export async function getConnectionPath(projectId: number, paperAId: number, paperBId: number): Promise<ConnectionPath> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/papers/${paperAId}/path/${paperBId}`)
  return parseResponse(response, `查询连接路径失败: ${response.status}`)
}

export async function uploadProjectPaperPdf(projectId: number, paperId: number, file: File): Promise<PaperIngestionJob> {
  const body = new FormData()
  body.append('file', file)
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/papers/${paperId}/pdf-upload`, {
    method: 'POST',
    body,
  })
  return parseResponse(response, `上传 PDF 失败: ${response.status}`)
}

export async function ingestProjectPaperPdf(projectId: number, paperId: number, pdfUrl?: string): Promise<PaperIngestionJob> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/papers/${paperId}/ingest`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pdf_url: pdfUrl || '' }),
  })
  return parseResponse(response, `PDF 入库失败: ${response.status}`)
}

export async function listProjectIngestionJobs(projectId: number): Promise<PaperIngestionJob[]> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/ingestion-jobs`)
  return parseResponse(response, `查询入库任务失败: ${response.status}`)
}

export async function retryProjectIngestionJob(projectId: number, jobId: number): Promise<PaperIngestionJob> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/ingestion-jobs/${jobId}/retry`, {
    method: 'POST',
  })
  return parseResponse(response, `重试入库任务失败: ${response.status}`)
}

export async function listReports(projectId: number): Promise<ReportVersion[]> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/reports`)
  return parseResponse(response, `查询报告失败: ${response.status}`)
}

export async function getProjectCitationGraph(projectId: number): Promise<GraphData> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/citation-graph`)
  return parseResponse(response, `查询项目图谱失败: ${response.status}`)
}

export async function listProjectRuns(projectId: number, limit = 12): Promise<ProjectRun[]> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/runs?limit=${limit}`)
  return parseResponse(response, `查询运行日志失败: ${response.status}`)
}

export async function startResearchExpandWorkflow(projectId: number, question: string): Promise<ProjectRun> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/workflows/research-expand`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  })
  return parseResponse(response, `启动研究工作流失败: ${response.status}`)
}

export async function createReport(projectId: number, payload: { title: string; content: string; source?: string }): Promise<ReportVersion> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/reports`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return parseResponse(response, `保存报告失败: ${response.status}`)
}

export async function chatProject(projectId: number, message: string, sessionId?: number): Promise<ChatRunResult> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, session_id: sessionId }),
  })
  return parseResponse(response, `Agent Chat 失败: ${response.status}`)
}

export async function listProjectChatSessions(projectId: number): Promise<ChatSession[]> {
  const response = await fetch(`${API_BASE}/api/projects/${projectId}/chat`)
  return parseResponse(response, `查询聊天会话失败: ${response.status}`)
}
