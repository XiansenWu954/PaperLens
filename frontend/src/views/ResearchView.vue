<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import CitationGraph from '../components/CitationGraph.vue'
import ResearchReport from '../components/ResearchReport.vue'
import StepTimeline from '../components/StepTimeline.vue'
import { useResearchStore } from '../stores/research'

const route = useRoute()
const router = useRouter()
const store = useResearchStore()
const copied = ref(false)
const copyError = ref('')
const activePanel = ref<'report' | 'graph'>('report')

const statusLabel = computed(() => {
  const labels = {
    idle: '待机',
    pending: '排队中',
    running: '研究中',
    done: '完成',
    error: '出错',
  }
  return labels[store.status] || store.status
})

const connectionLabel = computed(() => {
  const labels = {
    idle: '未连接',
    connecting: '连接中',
    open: '实时连接',
    retrying: `重连中 ${store.retryCount}/3`,
    closed: '已关闭',
    error: '连接失败',
  }
  return labels[store.connectionStatus] || store.connectionStatus
})

const reportMeta = computed(() => ({
  chars: store.report.length,
  sources: store.sources.length,
  steps: store.steps.length,
}))

onMounted(async () => {
  const id = Number(route.params.id)
  if (!Number.isFinite(id)) {
    await router.replace('/')
    return
  }
  if (store.taskId !== id) {
    try {
      await store.load(id)
    } catch {}
  }
})

async function copyReport() {
  if (!store.report) return
  copyError.value = ''
  try {
    await navigator.clipboard.writeText(store.report)
    copied.value = true
    setTimeout(() => { copied.value = false }, 1600)
  } catch {
    copyError.value = '复制失败，请手动选择报告内容。'
  }
}

function newResearch() {
  store.stopStream()
  router.push('/research')
}

async function retryLoad() {
  const id = Number(route.params.id)
  if (Number.isFinite(id)) {
    try {
      await store.load(id)
    } catch {}
  }
}
</script>

<template>
  <div class="research-page">
    <section class="task-header shell-panel">
      <div class="task-title">
        <button type="button" class="ghost-button" @click="newResearch">← 新研究</button>
        <div>
          <p class="kicker">Research task #{{ store.taskId || route.params.id }}</p>
          <h1>{{ store.question || '加载研究任务' }}</h1>
        </div>
      </div>

      <div class="task-status">
        <span class="status-pill">
          <span class="status-dot" :class="store.status"></span>
          {{ statusLabel }}
        </span>
        <span class="status-pill">
          <span class="status-dot" :class="store.connectionStatus"></span>
          {{ connectionLabel }}
        </span>
      </div>
    </section>

    <p v-if="store.loadError" class="error-banner">
      <span>{{ store.loadError }}</span>
      <button type="button" class="ghost-button" @click="retryLoad">重试</button>
    </p>
    <p v-else-if="store.errorMsg" class="error-banner">{{ store.errorMsg }}</p>
    <p v-if="copyError" class="error-banner">{{ copyError }}</p>

    <section class="metrics-row">
      <div class="metric quiet-panel"><strong>{{ reportMeta.steps }}</strong><span>进度事件</span></div>
      <div class="metric quiet-panel"><strong>{{ store.graph.nodes.length }}</strong><span>图谱节点</span></div>
      <div class="metric quiet-panel"><strong>{{ reportMeta.sources }}</strong><span>来源论文</span></div>
      <div class="metric quiet-panel"><strong>{{ reportMeta.chars }}</strong><span>报告字数</span></div>
    </section>

    <div class="workbench">
      <aside class="side-column">
        <section class="shell-panel side-card">
          <div class="card-head">
            <h2>工作流</h2>
          </div>
          <StepTimeline :steps="store.steps" :status="store.status" />
        </section>

        <section class="shell-panel side-card">
          <div class="card-head">
            <h2>来源</h2>
            <span>{{ store.sources.length }}</span>
          </div>
          <div v-if="store.sources.length" class="sources">
            <article v-for="(source, index) in store.sources.slice(0, 8)" :key="index">
              <strong>{{ source.title || 'Untitled paper' }}</strong>
              <p>{{ source.year || 'n.d.' }} · {{ source.venue || source.source || 'Unknown venue' }}</p>
            </article>
          </div>
          <p v-else class="empty-side">任务完成后显示论文来源。</p>
        </section>
      </aside>

      <main class="main-column">
        <div class="panel-toolbar shell-panel">
          <div class="tabs" role="tablist" aria-label="Research panels">
            <button type="button" role="tab" :aria-selected="activePanel === 'report'" class="toolbar-button" :class="{ active: activePanel === 'report' }" @click="activePanel = 'report'">报告</button>
            <button type="button" role="tab" :aria-selected="activePanel === 'graph'" class="toolbar-button" :class="{ active: activePanel === 'graph' }" @click="activePanel = 'graph'">图谱</button>
          </div>
          <div class="actions">
            <button type="button" class="secondary-button" :disabled="!store.report" @click="copyReport">{{ copied ? '已复制' : '复制报告' }}</button>
            <button type="button" class="secondary-button" @click="newResearch">重新研究</button>
          </div>
        </div>

        <section v-show="activePanel === 'report'" class="content-panel">
          <ResearchReport :report="store.report" :status="store.status" />
        </section>

        <section v-show="activePanel === 'graph'" class="content-panel shell-panel graph-panel">
          <div class="card-head">
            <div>
              <h2>引用图谱</h2>
              <p>节点大小代表引用量，颜色代表主题簇。</p>
            </div>
          </div>
          <CitationGraph :data="store.graph" />
        </section>
      </main>
    </div>
  </div>
</template>

<style scoped>
.research-page {
  display: grid;
  gap: 16px;
}

.task-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 18px;
}

.task-title {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  min-width: 0;
}

.task-title h1 {
  margin: 2px 0 0;
  font-family: var(--serif);
  font-size: clamp(22px, 3vw, 34px);
  font-weight: 650;
  line-height: 1.18;
}

.kicker {
  margin: 0;
  color: var(--text-muted);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

.task-status {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 30px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface-subtle);
  color: var(--text-soft);
  font-size: 12px;
  white-space: nowrap;
}

.error-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin: 0;
  padding: 12px 14px;
  border: 1px solid #e7bab3;
  border-radius: var(--radius);
  background: var(--err-soft);
  color: var(--err);
}

.metrics-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.metric {
  padding: 12px 14px;
}

.metric strong,
.metric span {
  display: block;
}

.metric strong {
  font-size: 22px;
  line-height: 1.1;
}

.metric span {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 12px;
}

.workbench {
  display: grid;
  grid-template-columns: 320px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.side-column,
.main-column {
  display: grid;
  gap: 14px;
}

.side-column {
  position: sticky;
  top: 14px;
}

.side-card {
  padding: 16px;
}

.card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.card-head h2 {
  margin: 0;
  font-size: 15px;
}

.card-head p {
  margin: 4px 0 0;
  color: var(--text-muted);
  font-size: 12px;
}

.sources {
  display: grid;
  gap: 10px;
}

.sources article {
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border);
}

.sources article:last-child {
  padding-bottom: 0;
  border-bottom: 0;
}

.sources strong {
  display: -webkit-box;
  overflow: hidden;
  color: var(--text);
  font-size: 13px;
  line-height: 1.35;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.sources p,
.empty-side {
  margin: 5px 0 0;
  color: var(--text-muted);
  font-size: 12px;
}

.panel-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px;
}

.tabs,
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.toolbar-button.active {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent-strong);
}

.content-panel {
  min-width: 0;
}

.graph-panel {
  padding: 16px;
}

@media (max-width: 1080px) {
  .workbench {
    grid-template-columns: 1fr;
  }

  .side-column {
    position: static;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .task-header,
  .task-title,
  .panel-toolbar {
    flex-direction: column;
    align-items: stretch;
  }

  .task-status {
    justify-content: flex-start;
  }

  .metrics-row,
  .side-column {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 560px) {
  .metrics-row,
  .side-column {
    grid-template-columns: 1fr;
  }

  .actions {
    display: grid;
    grid-template-columns: 1fr;
  }
}
</style>
