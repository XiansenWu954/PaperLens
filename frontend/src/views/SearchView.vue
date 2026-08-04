<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { API_BASE } from '../types'
import { useResearchStore } from '../stores/research'

const router = useRouter()
const store = useResearchStore()
const question = ref('')
const submitting = ref(false)
const localError = ref('')

const examples = [
  'Mamba 状态空间模型的最新进展',
  '检索增强生成 RAG 的关键技术路线',
  'Vision Transformer 在医学影像中的演进',
  '大模型思维链推理的方法与局限',
]

const statusText = computed(() => {
  if (store.status === 'running') return `任务 #${store.taskId} 正在研究`
  if (store.status === 'done') return `任务 #${store.taskId} 已完成`
  if (store.status === 'error') return '上次任务未完成'
  return '准备就绪'
})

async function submit() {
  const value = question.value.trim()
  if (!value || submitting.value) return
  submitting.value = true
  localError.value = ''
  try {
    await store.start(value)
    if (store.taskId) router.push(`/research/${store.taskId}`)
  } catch {
    localError.value = store.errorMsg || '无法创建研究任务。'
  } finally {
    submitting.value = false
  }
}

function useExample(value: string) {
  question.value = value
}
</script>

<template>
  <section class="workspace">
    <div class="intro">
      <p class="eyebrow">Research workspace</p>
      <h1>从一个研究问题开始，整理证据、图谱和综述。</h1>
      <p class="summary">
        PaperLens 会检索论文、抽取证据、构建引用关系，并把过程实时呈现在任务页中。
      </p>
    </div>

    <div class="search-grid">
      <form class="query-panel shell-panel" @submit.prevent="submit">
        <div class="panel-head">
          <div>
            <h2>新建研究任务</h2>
            <p>问题越具体，检索和综述越稳定。</p>
          </div>
          <span class="api-chip" :title="API_BASE">{{ API_BASE.replace(/^https?:\/\//, '') }}</span>
        </div>

        <label class="query-label" for="question">研究问题</label>
        <textarea
          id="question"
          v-model="question"
          placeholder="例如：Mamba 状态空间模型的最新进展"
          rows="5"
          @keydown.ctrl.enter.prevent="submit"
          @keydown.meta.enter.prevent="submit"
        />

        <div class="query-actions">
          <p class="shortcut">创建后会进入任务页查看实时进度。</p>
          <button type="submit" class="primary-button" :disabled="submitting || !question.trim()">
            <span v-if="submitting" class="spinner"></span>
            <span>{{ submitting ? '创建中' : '开始研究' }}</span>
          </button>
        </div>

        <p v-if="localError" class="error-note" role="alert">{{ localError }}</p>
      </form>

      <aside class="status-panel quiet-panel">
        <div class="status-row">
          <span class="status-dot" :class="store.status"></span>
          <div>
            <strong>{{ statusText }}</strong>
            <p>当前任务状态会在研究页持续更新。</p>
          </div>
        </div>
        <div class="runbook">
          <h3>执行过程</h3>
          <ol>
            <li>规划子查询</li>
            <li>并行检索论文并入库</li>
            <li>构建引用相似图</li>
            <li>生成带证据引用的综述</li>
          </ol>
        </div>
      </aside>
    </div>

    <section class="examples">
      <div class="section-head">
        <h2>示例问题</h2>
        <p>可以直接点选后微调。</p>
      </div>
      <div class="example-list">
        <button v-for="item in examples" :key="item" type="button" class="example-card" @click="useExample(item)">
          <span>{{ item }}</span>
        </button>
      </div>
    </section>
  </section>
</template>

<style scoped>
.workspace {
  display: grid;
  gap: 24px;
}

.intro {
  max-width: 760px;
  padding-top: 16px;
}

.eyebrow {
  margin-bottom: 8px;
  color: var(--accent-strong);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.intro h1 {
  max-width: 720px;
  margin-bottom: 12px;
  font-family: var(--serif);
  font-size: clamp(34px, 5vw, 58px);
  font-weight: 650;
  line-height: 1.08;
  letter-spacing: 0;
}

.summary {
  max-width: 640px;
  color: var(--text-soft);
  font-size: 16px;
}

.search-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
  gap: 18px;
  align-items: stretch;
}

.query-panel,
.status-panel {
  padding: 22px;
}

.panel-head,
.section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.panel-head h2,
.section-head h2,
.runbook h3 {
  margin-bottom: 4px;
  font-size: 16px;
}

.panel-head p,
.section-head p,
.status-row p {
  margin: 0;
  color: var(--text-muted);
  font-size: 13px;
}

.api-chip {
  max-width: 180px;
  overflow: hidden;
  padding: 5px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-muted);
  font-family: var(--mono);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.query-label {
  display: block;
  margin-bottom: 8px;
  color: var(--text-soft);
  font-size: 13px;
  font-weight: 700;
}

textarea {
  width: 100%;
  min-height: 150px;
  resize: vertical;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  outline: none;
  background: var(--paper);
  color: var(--text);
  font-size: 16px;
  line-height: 1.55;
}

textarea:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}

.query-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 14px;
}

.shortcut {
  margin: 0;
  color: var(--text-muted);
  font-size: 12px;
}

.spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(255,255,255,0.45);
  border-top-color: #fff;
  border-radius: 999px;
  animation: spin 0.7s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.error-note {
  margin: 14px 0 0;
  padding: 10px 12px;
  border: 1px solid #e7bab3;
  border-radius: var(--radius-sm);
  background: var(--err-soft);
  color: var(--err);
  font-size: 13px;
}

.status-panel {
  display: grid;
  align-content: start;
  gap: 22px;
}

.status-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.status-row strong {
  display: block;
  margin-bottom: 3px;
}

.runbook ol {
  margin: 0;
  padding-left: 20px;
  color: var(--text-soft);
}

.runbook li + li {
  margin-top: 8px;
}

.examples {
  display: grid;
  gap: 12px;
}

.example-list {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.example-card {
  min-height: 82px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
  color: var(--text);
  cursor: pointer;
  text-align: left;
  line-height: 1.45;
}

.example-card:hover {
  border-color: var(--accent);
  background: var(--accent-soft);
}

@media (max-width: 980px) {
  .search-grid {
    grid-template-columns: 1fr;
  }

  .example-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 620px) {
  .panel-head,
  .section-head,
  .query-actions {
    flex-direction: column;
    align-items: stretch;
  }

  .example-list {
    grid-template-columns: 1fr;
  }
}
</style>
