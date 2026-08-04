<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import AgentChatPanel from '../components/AgentChatPanel.vue'
import CitationGraph from '../components/CitationGraph.vue'
import EvidenceBoard from '../components/EvidenceBoard.vue'
import ReportStudio from '../components/ReportStudio.vue'
import RunInspector from '../components/RunInspector.vue'
import { useProjectsStore } from '../stores/projects'
import { getProjectCitationGraph, type GraphData, type ProjectPaper } from '../types'

const route = useRoute()
const store = useProjectsStore()
const active = ref<'evidence' | 'graph' | 'report' | 'runs'>('evidence')
const searchQuery = ref('')
const busy = ref(false)
const workflowBusy = ref(false)
const graphBusy = ref(false)
const pageError = ref('')
const searchError = ref('')
const ingestionMessage = ref('')
const graph = ref<GraphData>({ nodes: [], edges: [] })

const projectId = computed(() => Number(route.params.id))

onMounted(() => refreshAll())

async function refreshAll() {
  if (!Number.isFinite(projectId.value)) return
  pageError.value = ''
  try {
    await store.loadProject(projectId.value)
    await refreshGraph()
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '加载项目失败'
  }
}

async function refreshGraph() {
  graphBusy.value = true
  try {
    graph.value = await getProjectCitationGraph(projectId.value)
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '加载引用图谱失败'
    graph.value = { nodes: [], edges: [] }
  } finally {
    graphBusy.value = false
  }
}

async function searchAdd() {
  if (!searchQuery.value.trim()) return
  busy.value = true
  searchError.value = ''
  try {
    await store.searchAdd(projectId.value, searchQuery.value.trim())
    await refreshGraph()
    searchQuery.value = ''
  } catch (error) {
    searchError.value = error instanceof Error ? error.message : '检索加入失败'
  } finally {
    busy.value = false
  }
}

async function startWorkflow() {
  const question = searchQuery.value.trim() || store.currentProject?.title || ''
  if (!question) return
  workflowBusy.value = true
  searchError.value = ''
  ingestionMessage.value = ''
  try {
    await store.startWorkflow(projectId.value, question)
    ingestionMessage.value = '研究扩展工作流已启动：检索、入库、RAG、审阅和报告会写入运行日志。'
    active.value = 'runs'
  } catch (error) {
    searchError.value = error instanceof Error ? error.message : '启动研究工作流失败'
  } finally {
    workflowBusy.value = false
  }
}

async function setPaperStatus(paperId: number, status: ProjectPaper['status']) {
  pageError.value = ''
  try {
    await store.setPaperStatus(projectId.value, paperId, status)
    await store.loadProject(projectId.value)
    await refreshGraph()
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '更新论文状态失败'
  }
}

async function removePaper(paperId: number) {
  pageError.value = ''
  try {
    await store.removePaper(projectId.value, paperId)
    await store.loadProject(projectId.value)
    await refreshGraph()
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '移出论文失败'
  }
}

async function uploadPdf(paperId: number, file: File) {
  pageError.value = ''
  ingestionMessage.value = ''
  try {
    await store.uploadPdf(projectId.value, paperId, file)
    ingestionMessage.value = 'PDF 已进入入库队列，完成后会更新 RAG 状态。'
    await refreshGraph()
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : '上传 PDF 失败'
  }
}

async function ingestPdf(paperId: number) {
  pageError.value = ''
  ingestionMessage.value = ''
  try {
    await store.ingestFromPdfUrl(projectId.value, paperId)
    ingestionMessage.value = '已从论文链接创建入库任务。'
    await refreshGraph()
  } catch (error) {
    pageError.value = error instanceof Error ? error.message : 'PDF 入库失败'
  }
}

async function saveReport(payload: { title: string; content: string; source?: string }) {
  return store.saveReport(projectId.value, payload)
}
</script>

<template>
  <section class="workspace">
    <header class="project-header shell-panel">
      <div>
        <p class="kicker">Project Workspace</p>
        <h1>{{ store.currentProject?.title || '加载项目' }}</h1>
        <p>{{ store.currentProject?.description || '项目知识库、Agent Chat、证据和报告集中在这里。' }}</p>
      </div>
      <div class="stats">
        <span>{{ store.papers.length }} papers</span>
        <span>{{ store.reports.length }} reports</span>
        <span>{{ graph.nodes.length }} graph nodes</span>
      </div>
    </header>

    <p v-if="pageError || store.error" class="error-banner">
      <span>{{ pageError || store.error }}</span>
      <button type="button" class="ghost-button" @click="refreshAll">重试</button>
    </p>
    <p v-if="ingestionMessage" class="info-banner">{{ ingestionMessage }}</p>

    <form class="search-row shell-panel" @submit.prevent="searchAdd">
      <label class="sr-only" for="project-search">检索并加入项目论文</label>
      <input id="project-search" v-model="searchQuery" placeholder="检索并加入项目：DBLP / OpenAlex / arXiv 默认启用" />
      <button type="submit" class="primary-button" :disabled="busy || !searchQuery.trim()">{{ busy ? '检索中' : '检索加入' }}</button>
      <button type="button" class="secondary-button" :disabled="workflowBusy || (!searchQuery.trim() && !store.currentProject?.title)" @click="startWorkflow">
        {{ workflowBusy ? '启动中' : '扩大检索并生成报告' }}
      </button>
      <p v-if="searchError" class="search-error">{{ searchError }}</p>
    </form>

    <div class="layout">
      <main class="main">
        <nav class="tabs shell-panel" role="tablist" aria-label="Project workspace panels">
          <button type="button" role="tab" :aria-selected="active === 'evidence'" class="toolbar-button" :class="{ active: active === 'evidence' }" @click="active = 'evidence'">Evidence Board</button>
          <button type="button" role="tab" :aria-selected="active === 'graph'" class="toolbar-button" :class="{ active: active === 'graph' }" @click="active = 'graph'">Citation Map</button>
          <button type="button" role="tab" :aria-selected="active === 'report'" class="toolbar-button" :class="{ active: active === 'report' }" @click="active = 'report'">Report Studio</button>
          <button type="button" role="tab" :aria-selected="active === 'runs'" class="toolbar-button" :class="{ active: active === 'runs' }" @click="active = 'runs'">Run Inspector</button>
        </nav>

        <EvidenceBoard
          v-if="active === 'evidence'"
          :papers="store.papers"
          :loading="store.loading"
          @set-status="setPaperStatus"
          @remove="removePaper"
          @upload-pdf="uploadPdf"
          @ingest-pdf="ingestPdf"
        />

        <section v-if="active === 'graph'" class="shell-panel graph-card">
          <div class="card-head">
            <h2>Citation Map</h2>
            <p>基于项目论文的 referenced_works 构建。</p>
          </div>
          <p v-if="graphBusy" class="graph-loading">正在刷新引用图谱...</p>
          <CitationGraph v-else :data="graph" />
        </section>

        <ReportStudio
          v-if="active === 'report'"
          :reports="store.reports"
          :papers="store.papers"
          :loading="store.loading"
          :save-report="saveReport"
        />
        <RunInspector v-if="active === 'runs'" :runs="store.runs" />
      </main>

      <AgentChatPanel :project-id="projectId" @refreshed="refreshAll" />
    </div>
  </section>
</template>

<style scoped>
.workspace {
  display: grid;
  gap: 14px;
}

.project-header,
.search-row,
.tabs,
.graph-card {
  padding: 14px;
}

.project-header {
  display: flex;
  justify-content: space-between;
  gap: 16px;
}

.kicker {
  margin: 0 0 6px;
  color: var(--accent-strong);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

h1 {
  margin: 0 0 6px;
  font-family: var(--serif);
  font-size: clamp(26px, 4vw, 42px);
}

p {
  margin: 0;
  color: var(--text-muted);
}

.stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-content: flex-start;
  justify-content: flex-end;
}

.stats span {
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface-subtle);
  color: var(--text-soft);
  font-size: 12px;
}

.search-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto auto;
  gap: 10px;
}

.search-error {
  grid-column: 1 / -1;
  padding: 8px 10px;
  border: 1px solid #e7bab3;
  border-radius: var(--radius-sm);
  background: var(--err-soft);
  color: var(--err);
  font-size: 12px;
}

.error-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin: 0;
  padding: 10px 12px;
  border: 1px solid #e7bab3;
  border-radius: var(--radius);
  background: var(--err-soft);
  color: var(--err);
  font-size: 13px;
}

.info-banner {
  margin: 0;
  padding: 10px 12px;
  border: 1px solid rgba(39, 116, 93, 0.28);
  border-radius: var(--radius);
  background: rgba(39, 116, 93, 0.08);
  color: var(--ok);
  font-size: 13px;
}

input {
  min-height: 38px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--paper);
}

.layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px;
  gap: 14px;
  align-items: start;
}

.main {
  display: grid;
  gap: 12px;
  min-width: 0;
}

.tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

.graph-card {
  min-width: 0;
  overflow: hidden;
}

.toolbar-button.active {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent-strong);
}

.card-head {
  margin-bottom: 12px;
}

.graph-loading {
  padding: 28px;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  color: var(--text-muted);
  text-align: center;
}

.card-head h2 {
  margin: 0 0 4px;
  font-size: 16px;
}

@media (max-width: 1120px) {
  .layout {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 680px) {
  .project-header,
  .search-row {
    grid-template-columns: 1fr;
    flex-direction: column;
  }

  .search-row {
    display: grid;
  }

  .tabs .toolbar-button {
    flex: 1 1 130px;
    min-width: 0;
  }
}
</style>
