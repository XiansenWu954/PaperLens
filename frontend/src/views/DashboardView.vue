<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useProjectsStore } from '../stores/projects'
import { describeError } from '../types'

const router = useRouter()
const store = useProjectsStore()
const title = ref('')
const description = ref('')
const busy = ref(false)
const localError = ref('')

onMounted(() => store.loadProjects())

async function create() {
  if (!title.value.trim()) return
  busy.value = true
  localError.value = ''
  try {
    const project = await store.addProject(title.value.trim(), description.value.trim())
    router.push(`/projects/${project.id}`)
  } catch (error) {
    localError.value = describeError(error, '创建项目失败')
  } finally {
    busy.value = false
  }
}

async function seed() {
  busy.value = true
  localError.value = ''
  try {
    const project = await store.seedDemo()
    router.push(`/projects/${project.id}`)
  } catch (error) {
    localError.value = describeError(error, '创建演示项目失败')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="dashboard">
    <div class="hero">
      <p class="eyebrow">PaperLens V2</p>
      <h1>Agent 文献研究工作台</h1>
      <p>项目知识库、DBLP 默认检索、项目 RAG Chat、引用图谱和报告产出。</p>
    </div>

    <div class="grid">
      <section class="shell-panel create-panel">
        <h2>创建研究项目</h2>
        <label for="project-title">项目标题</label>
        <input id="project-title" v-model="title" placeholder="例如 Mamba 后续研究" autocomplete="off" />
        <label for="project-description">项目说明</label>
        <textarea id="project-description" v-model="description" rows="3" placeholder="可选：研究范围、目标会议、时间窗口" />
        <div class="actions">
          <button type="button" class="primary-button" :disabled="busy || !title.trim()" @click="create">
            {{ busy ? '处理中' : '创建项目' }}
          </button>
          <button type="button" class="secondary-button" :disabled="busy" @click="seed">创建演示项目</button>
        </div>
        <p v-if="localError" class="error-note">{{ localError }}</p>
      </section>

      <section class="shell-panel projects-panel">
        <div class="panel-head">
          <h2>项目列表</h2>
          <span>{{ store.projects.length }}</span>
        </div>
        <div v-if="store.loading && !store.projects.length" class="empty">正在加载项目...</div>
        <div v-else-if="store.error" class="error-note">
          <span>{{ store.error }}</span>
          <button type="button" class="ghost-button" @click="store.loadProjects">重试</button>
        </div>
        <div v-else-if="!store.projects.length" class="empty">还没有项目。可以创建一个真实项目，或使用演示项目快速展示。</div>
        <button v-for="project in store.projects" :key="project.id" type="button" class="project-card" @click="router.push(`/projects/${project.id}`)">
          <strong>{{ project.title }}</strong>
          <span>{{ project.paper_count }} papers · {{ project.run_count }} runs</span>
          <p>{{ project.description || '暂无说明' }}</p>
        </button>
      </section>
    </div>
  </section>
</template>

<style scoped>
.dashboard {
  display: grid;
  gap: 22px;
}

.hero {
  max-width: 820px;
}

.eyebrow {
  margin: 0 0 8px;
  color: var(--accent-strong);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

h1 {
  margin: 0 0 10px;
  font-family: var(--serif);
  font-size: clamp(34px, 5vw, 58px);
  line-height: 1.08;
}

.hero p,
.empty {
  color: var(--text-soft);
}

.grid {
  display: grid;
  grid-template-columns: 380px minmax(0, 1fr);
  gap: 16px;
}

.create-panel,
.projects-panel {
  padding: 18px;
}

h2 {
  margin: 0 0 14px;
  font-size: 16px;
}

input,
textarea {
  width: 100%;
  margin-bottom: 10px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--paper);
}

label {
  display: block;
  margin: 8px 0 5px;
  color: var(--text-soft);
  font-size: 12px;
  font-weight: 700;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.panel-head {
  display: flex;
  justify-content: space-between;
}

.error-note {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin: 12px 0 0;
  padding: 10px 12px;
  border: 1px solid #e7bab3;
  border-radius: var(--radius-sm);
  background: var(--err-soft);
  color: var(--err);
  font-size: 13px;
}

.project-card {
  display: grid;
  width: 100%;
  gap: 5px;
  padding: 14px 0;
  border-top: 1px solid var(--border);
  background: transparent;
  color: var(--text);
  cursor: pointer;
  text-align: left;
}

.project-card span,
.project-card p {
  margin: 0;
  color: var(--text-muted);
  font-size: 12px;
}

@media (max-width: 900px) {
  .grid {
    grid-template-columns: 1fr;
  }
}
</style>
