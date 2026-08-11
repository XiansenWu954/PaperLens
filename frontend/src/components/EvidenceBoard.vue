<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ProjectPaper } from '../types'

const props = defineProps<{ papers: ProjectPaper[]; loading?: boolean }>()
const emit = defineEmits<{
  setStatus: [paperId: number, status: ProjectPaper['status']]
  remove: [paperId: number]
  uploadPdf: [paperId: number, file: File]
  ingestPdf: [paperId: number]
}>()

type FilterKey = 'all' | ProjectPaper['status'] | 'rag-ready' | 'needs-rag'

const query = ref('')
const filter = ref<FilterKey>('all')
const pendingRemoveId = ref<number | null>(null)

const statusLabels: Record<ProjectPaper['status'], string> = {
  candidate: '候选',
  included: '采用',
  core: '核心',
  excluded: '排除',
}

const statusDescriptions: Record<ProjectPaper['status'], string> = {
  candidate: '待确认是否纳入 RAG 范围',
  included: '已纳入项目 RAG 范围',
  core: '核心证据，优先用于报告',
  excluded: '已排除，不参与 RAG/图谱',
}

const ingestionLabels: Record<ProjectPaper['ingestion_status'], string> = {
  pending: '待入库',
  parsing: '解析中',
  embedded: '已入库',
  failed: '入库失败',
}

const stats = computed(() => {
  const active = props.papers.filter((paper) => paper.status !== 'excluded')
  const ragReady = active.filter((paper) => paper.chunk_count > 0)
  return {
    total: props.papers.length,
    active: active.length,
    core: props.papers.filter((paper) => paper.status === 'core').length,
    excluded: props.papers.filter((paper) => paper.status === 'excluded').length,
    ragReady: ragReady.length,
    needsRag: active.length - ragReady.length,
    citations: props.papers.reduce((sum, paper) => sum + (paper.citation_count || 0), 0),
  }
})

const filteredPapers = computed(() => {
  const needle = query.value.trim().toLowerCase()
  return props.papers.filter((paper) => {
    const matchesFilter =
      filter.value === 'all'
      || paper.status === filter.value
      || (filter.value === 'rag-ready' && paper.status !== 'excluded' && paper.ingestion_status === 'embedded' && paper.chunk_count > 0)
      || (filter.value === 'needs-rag' && paper.status !== 'excluded' && paper.ingestion_status !== 'embedded')
    if (!matchesFilter) return false
    if (!needle) return true
    const haystack = [
      paper.title,
      paper.abstract,
      paper.venue,
      paper.arxiv_id,
      paper.doi,
      paper.source_reason,
      paper.notes,
    ].filter(Boolean).join(' ').toLowerCase()
    return haystack.includes(needle)
  })
})

const filterOptions: { key: FilterKey; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'core', label: '核心' },
  { key: 'included', label: '采用' },
  { key: 'candidate', label: '候选' },
  { key: 'excluded', label: '排除' },
  { key: 'rag-ready', label: '已向量化' },
  { key: 'needs-rag', label: '待向量化' },
]

function clearFilters() {
  query.value = ''
  filter.value = 'all'
}

function requestRemove(paperId: number) {
  pendingRemoveId.value = paperId
}

function cancelRemove() {
  pendingRemoveId.value = null
}

function confirmRemove(paperId: number) {
  emit('remove', paperId)
  pendingRemoveId.value = null
}

function uploadPdf(paperId: number, event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) {
    emit('uploadPdf', paperId, file)
    input.value = ''
  }
}
</script>

<template>
  <section class="board shell-panel">
    <div class="board-head">
      <div>
        <h2>Evidence Board</h2>
        <p>项目论文库、RAG 范围、证据状态和人工取舍。</p>
      </div>
      <span>{{ stats.active }} active / {{ stats.total }} papers</span>
    </div>

    <div v-if="papers.length" class="stats-grid">
      <div>
        <strong>{{ stats.ragReady }}</strong>
        <span>RAG ready</span>
      </div>
      <div>
        <strong>{{ stats.needsRag }}</strong>
        <span>needs vectors</span>
      </div>
      <div>
        <strong>{{ stats.core }}</strong>
        <span>core papers</span>
      </div>
      <div>
        <strong>{{ stats.citations }}</strong>
        <span>citations</span>
      </div>
    </div>

    <div v-if="papers.length" class="board-tools">
      <label class="sr-only" for="evidence-filter">筛选项目论文</label>
      <input id="evidence-filter" v-model="query" type="search" placeholder="筛选标题、摘要、DOI、arXiv 或备注" />
      <div class="segmented" aria-label="Evidence filters">
        <button
          v-for="option in filterOptions"
          :key="option.key"
          type="button"
          class="filter-button"
          :class="{ active: filter === option.key }"
          :aria-pressed="filter === option.key"
          @click="filter = option.key"
        >
          {{ option.label }}
        </button>
        <button v-if="query || filter !== 'all'" type="button" class="filter-button clear" @click="clearFilters">
          清空筛选
        </button>
      </div>
    </div>

    <div v-if="loading" class="empty">
      <strong>正在加载项目论文</strong>
      <p>同步项目库、向量状态和人工标记。</p>
    </div>

    <div v-else-if="!papers.length" class="empty">
      <strong>项目库为空</strong>
      <p>通过右侧 Agent Chat 或顶部检索入口补充论文。</p>
    </div>

    <div v-else-if="!filteredPapers.length" class="empty">
      <strong>没有匹配的证据</strong>
      <p>调整筛选条件或搜索词，保留项目库原始内容不变。</p>
    </div>

    <article
      v-for="paper in filteredPapers"
      :key="paper.paper_id"
      class="paper-row"
      :class="{ excluded: paper.status === 'excluded', core: paper.status === 'core' }"
    >
      <div class="paper-main">
        <div class="title-line">
          <h3>{{ paper.title }}</h3>
          <span class="status-pill" :class="paper.status">{{ statusLabels[paper.status] }}</span>
        </div>
        <p>{{ paper.year || 'n.d.' }} · {{ paper.venue || 'Unknown venue' }} · {{ paper.citation_count }} citations</p>
        <p class="abstract">{{ paper.abstract || '暂无摘要。' }}</p>
        <div class="meta">
          <span :class="{ good: paper.status !== 'excluded', muted: paper.status === 'excluded' }">
            {{ paper.status === 'excluded' ? '不参与 RAG/图谱' : '纳入项目范围' }}
          </span>
          <span
            :class="{
              warn: paper.ingestion_status === 'pending' || paper.ingestion_status === 'parsing',
              good: paper.ingestion_status === 'embedded' && paper.chunk_count > 0,
              bad: paper.ingestion_status === 'failed',
            }"
          >
            {{ ingestionLabels[paper.ingestion_status] }} · {{ paper.chunk_count }} chunks
          </span>
          <span v-if="paper.embedding_model">{{ paper.embedding_model }}</span>
          <span v-if="paper.indexed_at">indexed {{ new Date(paper.indexed_at).toLocaleDateString() }}</span>
          <span>{{ paper.added_by }}</span>
          <a v-if="paper.pdf_url" :href="paper.pdf_url" target="_blank" rel="noreferrer">PDF</a>
        </div>
        <p v-if="paper.source_reason || paper.notes" class="reason">
          {{ paper.source_reason || paper.notes }}
        </p>
      </div>
      <div class="paper-actions">
        <small>{{ statusDescriptions[paper.status] }}</small>
        <select
          :value="paper.status"
          :disabled="loading"
          :aria-label="`设置 ${paper.title} 的项目状态`"
          @change="emit('setStatus', paper.paper_id, ($event.target as HTMLSelectElement).value as ProjectPaper['status'])"
        >
          <option value="candidate">候选</option>
          <option value="included">采用</option>
          <option value="core">核心</option>
          <option value="excluded">排除</option>
        </select>
        <label class="secondary-button file-button" :class="{ disabled: loading }">
          <input type="file" accept="application/pdf,.pdf" :disabled="loading" @change="uploadPdf(paper.paper_id, $event)" />
          上传 PDF
        </label>
        <button
          type="button"
          class="secondary-button"
          :disabled="loading || !paper.pdf_url"
          @click="emit('ingestPdf', paper.paper_id)"
        >
          从链接入库
        </button>
        <p v-if="paper.ingestion_status === 'failed'" class="ingest-error">
          入库失败{{ paper.latest_ingestion_error ? `：${paper.latest_ingestion_error}` : '' }}，可重新上传或从链接重试。
        </p>
        <template v-if="pendingRemoveId === paper.paper_id">
          <button type="button" class="secondary-button" @click="cancelRemove">取消</button>
          <button type="button" class="danger-button" @click="confirmRemove(paper.paper_id)">确认移出</button>
        </template>
        <button v-else type="button" class="ghost-button" :disabled="loading" @click="requestRemove(paper.paper_id)">移出项目</button>
      </div>
    </article>
  </section>
</template>

<style scoped>
.board {
  padding: 16px;
}

.board-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
}

h2,
h3,
p {
  margin: 0;
}

.board-head h2 {
  font-size: 16px;
}

.board-head p,
.board-head span,
.paper-main p {
  color: var(--text-muted);
  font-size: 12px;
}

.empty {
  padding: 28px;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  text-align: center;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 12px;
}

.stats-grid div {
  min-width: 0;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-subtle);
}

.stats-grid strong {
  display: block;
  font-size: 18px;
  line-height: 1.1;
}

.stats-grid span {
  display: block;
  margin-top: 3px;
  color: var(--text-muted);
  font-size: 11px;
}

.board-tools {
  display: grid;
  gap: 10px;
  margin-bottom: 4px;
}

.board-tools input {
  width: 100%;
  min-height: 36px;
  padding: 0 11px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--paper);
}

.segmented {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.filter-button {
  min-height: 30px;
  padding: 0 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-soft);
  cursor: pointer;
  font-size: 12px;
}

.filter-button.active {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent-strong);
}

.filter-button.clear {
  color: var(--text-muted);
}

.paper-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 150px;
  gap: 16px;
  padding: 14px 0;
  border-top: 1px solid var(--border);
}

.paper-row.excluded {
  color: var(--text-muted);
}

.paper-row.core {
  border-left: 3px solid var(--accent);
  padding-left: 12px;
}

.title-line {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.paper-main h3 {
  margin-bottom: 5px;
  font-size: 15px;
}

.status-pill {
  flex: 0 0 auto;
  padding: 3px 7px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-soft);
  font-size: 11px;
}

.status-pill.core,
.meta .good {
  border-color: rgba(39, 116, 93, 0.3);
  background: rgba(39, 116, 93, 0.08);
  color: var(--ok);
}

.status-pill.excluded,
.meta .muted {
  background: var(--surface-muted);
  color: var(--text-muted);
}

.meta .warn {
  border-color: rgba(148, 101, 19, 0.3);
  background: rgba(148, 101, 19, 0.08);
  color: var(--warn);
}

.meta .bad {
  border-color: #d9a79f;
  background: var(--err-soft);
  color: var(--err);
}

.abstract {
  display: -webkit-box;
  overflow: hidden;
  margin-top: 7px;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}

.meta span,
.meta a {
  padding: 3px 7px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface-subtle);
  color: var(--text-soft);
  font-size: 11px;
  text-decoration: none;
}

.reason {
  margin-top: 8px;
  color: var(--text-soft);
  font-size: 12px;
}

.paper-actions {
  display: grid;
  align-content: start;
  gap: 8px;
}

.file-button {
  position: relative;
  overflow: hidden;
}

.file-button input {
  position: absolute;
  inset: 0;
  opacity: 0;
  cursor: pointer;
}

.file-button.disabled {
  opacity: 0.6;
  pointer-events: none;
}

.ingest-error {
  padding: 8px;
  border: 1px solid #e7bab3;
  border-radius: var(--radius-sm);
  background: var(--err-soft);
  color: var(--err);
  font-size: 11px;
}

.paper-actions small {
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.35;
}

select {
  width: 100%;
  min-height: 34px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
}

.danger-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 34px;
  padding: 0 10px;
  border: 1px solid #d9a79f;
  border-radius: var(--radius-sm);
  background: var(--err-soft);
  color: var(--err);
  cursor: pointer;
}

@media (max-width: 720px) {
  .board-head,
  .title-line {
    display: grid;
  }

  .stats-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .paper-row {
    grid-template-columns: 1fr;
  }

  .paper-actions {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
