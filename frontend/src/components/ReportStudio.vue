<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ProjectPaper, ReportVersion } from '../types'
import ResearchReport from './ResearchReport.vue'

const props = defineProps<{
  reports: ReportVersion[]
  papers: ProjectPaper[]
  loading?: boolean
  saveReport: (payload: { title: string; content: string; source?: string }) => Promise<ReportVersion>
}>()

const selectedId = ref<number | null>(null)
const draftTitle = ref('')
const draftContent = ref('')
const editing = ref(false)
const saving = ref(false)
const saveError = ref('')
const copyState = ref('')

const activePapers = computed(() => props.papers.filter((paper) => paper.status !== 'excluded'))
const selectedReport = computed(() => {
  if (!props.reports.length) return null
  return props.reports.find((report) => report.id === selectedId.value) || props.reports[0]
})

const draftChanged = computed(() => {
  const report = selectedReport.value
  return draftTitle.value.trim() !== (report?.title || '').trim()
    || draftContent.value.trim() !== (report?.content || '').trim()
})

const audit = computed(() => {
  const content = draftContent.value || selectedReport.value?.content || ''
  const normalized = normalize(content)
  const citedPapers = activePapers.value.filter((paper) => {
    const candidates = [
      paper.title,
      paper.doi || '',
      paper.arxiv_id || '',
      paper.openalex_id || '',
    ].filter(Boolean)
    return candidates.some((candidate) => normalize(candidate).length > 8 && normalized.includes(normalize(candidate)))
  })
  const pqacCount = (content.match(/pqac-[a-zA-Z0-9]{8}/g) || []).length
  const sourceMarkerCount = (content.match(/\[(source|citation|evidence|来源|引用)[:：][^\]]+\]/gi) || []).length
  const hasEvidenceAnchors = citedPapers.length > 0 || pqacCount > 0 || sourceMarkerCount > 0
  return {
    citedPapers,
    missingPapers: activePapers.value.filter((paper) => !citedPapers.some((item) => item.paper_id === paper.paper_id)),
    pqacCount,
    sourceMarkerCount,
    hasEvidenceAnchors,
  }
})

const reportSourceLabel = computed(() => {
  const source = selectedReport.value?.source || 'draft'
  const labels: Record<string, string> = {
    agent: 'Agent draft',
    chat: 'Chat draft',
    user: 'Manual draft',
    demo: 'Demo asset',
  }
  return labels[source] || source
})

watch(
  () => props.reports.map((report) => report.id).join(','),
  () => {
    if (!props.reports.length) {
      selectedId.value = null
      if (!editing.value) {
        draftTitle.value = ''
        draftContent.value = ''
      }
      return
    }
    if (!selectedId.value || !props.reports.some((report) => report.id === selectedId.value)) {
      selectReport(props.reports[0])
    }
  },
  { immediate: true },
)

function normalize(value: string) {
  return value.toLowerCase().replace(/\s+/g, ' ').trim()
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}

function selectReport(report: ReportVersion) {
  if (!canDiscardDraft()) return
  selectedId.value = report.id
  draftTitle.value = report.title
  draftContent.value = report.content
  editing.value = false
  saveError.value = ''
}

function startNewDraft() {
  if (!canDiscardDraft()) return
  selectedId.value = null
  draftTitle.value = `Research report ${props.reports.length + 1}`
  draftContent.value = ''
  editing.value = true
  saveError.value = ''
}

function editSelected() {
  if (editing.value) return
  if (selectedReport.value) {
    draftTitle.value = selectedReport.value.title
    draftContent.value = selectedReport.value.content
  }
  editing.value = true
  saveError.value = ''
}

function cancelEditing() {
  if (!canDiscardDraft()) return
  if (selectedReport.value) {
    draftTitle.value = selectedReport.value.title
    draftContent.value = selectedReport.value.content
    editing.value = false
    saveError.value = ''
  } else {
    draftTitle.value = ''
    draftContent.value = ''
    editing.value = false
  }
}

function canDiscardDraft() {
  if (!editing.value || !draftChanged.value) return true
  return window.confirm('当前草稿尚未保存，确认放弃这些修改？')
}

async function saveDraft() {
  if (!draftTitle.value.trim() || !draftContent.value.trim()) return
  saving.value = true
  saveError.value = ''
  try {
    const report = await props.saveReport({
      title: draftTitle.value.trim(),
      content: draftContent.value.trim(),
      source: selectedReport.value ? 'user' : 'user',
    })
    selectReport(report)
    editing.value = false
  } catch (error) {
    saveError.value = error instanceof Error ? error.message : '保存报告失败'
  } finally {
    saving.value = false
  }
}

async function copyReport() {
  const content = draftContent.value || selectedReport.value?.content || ''
  if (!content) return
  try {
    await navigator.clipboard.writeText(content)
    copyState.value = '已复制'
  } catch {
    copyState.value = '复制失败'
  }
  window.setTimeout(() => {
    copyState.value = ''
  }, 1600)
}
</script>

<template>
  <section class="studio shell-panel">
    <header class="studio-head">
      <div>
        <h2>Report Studio</h2>
        <p>管理版本、编辑草稿，并检查报告是否连接到项目证据。</p>
      </div>
      <div class="actions">
        <button type="button" class="secondary-button" :disabled="saving" @click="startNewDraft">新建版本</button>
        <button v-if="!editing" type="button" class="secondary-button" :disabled="!selectedReport || saving" @click="editSelected">编辑当前</button>
        <button v-else type="button" class="secondary-button" :disabled="saving" @click="cancelEditing">取消编辑</button>
        <button type="button" class="primary-button" :disabled="saving || !draftTitle.trim() || !draftContent.trim() || !draftChanged" @click="saveDraft">
          {{ saving ? '保存中' : '保存版本' }}
        </button>
      </div>
    </header>

    <div class="studio-grid">
      <aside class="report-side">
        <section class="version-list" aria-label="报告版本">
          <div class="side-head">
            <strong>{{ reports.length }} versions</strong>
            <span v-if="selectedReport">{{ reportSourceLabel }}</span>
          </div>
          <button
            v-for="report in reports"
            :key="report.id"
            type="button"
            class="version-row"
            :class="{ active: selectedReport?.id === report.id && !editing }"
            :disabled="saving"
            @click="selectReport(report)"
          >
            <span>{{ report.title }}</span>
            <small>{{ formatDate(report.created_at) }}</small>
          </button>
          <div v-if="!reports.length" class="empty-state">
            <strong>暂无报告版本</strong>
            <p>可以从 Agent Chat 生成章节，也可以先保存一份人工草稿。</p>
          </div>
        </section>

        <section class="audit-panel" aria-label="引用核查">
          <div class="side-head">
            <strong>Evidence Audit</strong>
            <span>{{ audit.citedPapers.length }}/{{ activePapers.length }} covered</span>
          </div>
          <div class="audit-meter">
            <span :style="{ width: activePapers.length ? `${Math.round(audit.citedPapers.length / activePapers.length * 100)}%` : '0%' }"></span>
          </div>
          <div class="audit-grid">
            <span>论文标题命中</span>
            <strong>{{ audit.citedPapers.length }}</strong>
            <span>pqac 标记</span>
            <strong>{{ audit.pqacCount }}</strong>
            <span>来源标记</span>
            <strong>{{ audit.sourceMarkerCount }}</strong>
          </div>
          <p class="audit-note" :class="{ warn: !audit.hasEvidenceAnchors }">
            {{ audit.hasEvidenceAnchors ? '报告包含可追踪证据锚点，仍建议人工核查语义是否忠实。' : '当前报告缺少明显证据锚点，提交前应补充论文标题或引用标记。' }}
          </p>
          <div v-if="audit.missingPapers.length" class="missing-list">
            <span>未覆盖论文</span>
            <small v-for="paper in audit.missingPapers.slice(0, 4)" :key="paper.paper_id">{{ paper.title }}</small>
          </div>
        </section>
      </aside>

      <main class="report-main">
        <div class="editor-bar">
          <input v-model="draftTitle" :disabled="!editing" placeholder="报告标题" />
          <button type="button" class="secondary-button" :disabled="!(draftContent || selectedReport?.content)" @click="copyReport">
            {{ copyState || '复制 Markdown' }}
          </button>
        </div>

        <textarea
          v-if="editing"
          v-model="draftContent"
          class="draft-editor"
          placeholder="在这里整理 Agent 生成内容、加入人工判断，并保留论文标题或引用标记。"
        ></textarea>
        <ResearchReport v-else :report="selectedReport?.content || ''" :status="loading ? 'running' : reports.length ? 'done' : 'idle'" />

        <p v-if="saveError" class="error-text">{{ saveError }}</p>
      </main>
    </div>
  </section>
</template>

<style scoped>
.studio {
  min-width: 0;
  padding: 16px;
}

.studio-head,
.editor-bar,
.side-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.studio-head {
  margin-bottom: 14px;
}

h2,
p {
  margin: 0;
}

h2 {
  font-size: 16px;
}

p,
span,
small {
  color: var(--text-muted);
  font-size: 12px;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.studio-grid {
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 14px;
  align-items: start;
}

.report-side,
.report-main {
  display: grid;
  gap: 12px;
  min-width: 0;
}

.version-list,
.audit-panel {
  display: grid;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface-subtle);
}

.version-row {
  display: grid;
  gap: 3px;
  width: 100%;
  padding: 9px 10px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text);
  text-align: left;
  cursor: pointer;
}

.version-row span {
  overflow: hidden;
  color: var(--text);
  font-size: 13px;
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.version-row.active,
.version-row:hover {
  border-color: var(--border-strong);
  background: var(--surface);
}

.empty-state {
  padding: 10px;
  border: 1px dashed var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
}

.empty-state strong {
  display: block;
  margin-bottom: 4px;
  font-size: 13px;
}

.audit-meter {
  height: 7px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--surface-muted);
}

.audit-meter span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
}

.audit-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 5px 10px;
}

.audit-grid strong {
  font-size: 12px;
}

.audit-note {
  padding: 9px;
  border-radius: var(--radius-sm);
  background: var(--surface);
  color: var(--text-soft);
}

.audit-note.warn {
  background: var(--err-soft);
  color: var(--err);
}

.missing-list {
  display: grid;
  gap: 5px;
}

.missing-list small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.editor-bar {
  align-items: center;
}

input,
textarea {
  width: 100%;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--paper);
  color: var(--text);
}

input {
  min-height: 38px;
  padding: 0 12px;
}

input:disabled {
  background: var(--surface-subtle);
  color: var(--text-soft);
}

.draft-editor {
  min-height: 520px;
  padding: 18px;
  resize: vertical;
  font-family: var(--serif);
  font-size: 16px;
  line-height: 1.7;
}

.error-text {
  color: var(--err);
}

@media (max-width: 900px) {
  .studio-grid {
    grid-template-columns: 1fr;
  }

  .studio-head,
  .editor-bar {
    flex-direction: column;
  }

  .actions,
  .editor-bar .secondary-button {
    width: 100%;
  }

  .actions button,
  .editor-bar .secondary-button {
    flex: 1 1 150px;
  }
}
</style>
