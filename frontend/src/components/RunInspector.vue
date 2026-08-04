<script setup lang="ts">
import type { ProjectRun } from '../types'

defineProps<{ runs: ProjectRun[] }>()

const statusLabels: Record<ProjectRun['status'], string> = {
  pending: '排队',
  running: '运行中',
  done: '完成',
  error: '失败',
}

function summarize(payload: Record<string, unknown>) {
  const pairs = Object.entries(payload)
    .filter(([key]) => !['arguments', 'evidence', 'nodes', 'edges'].includes(key))
    .slice(0, 4)
    .map(([key, value]) => `${key}: ${Array.isArray(value) ? value.length : String(value)}`)
  return pairs.join(' · ')
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
}
</script>

<template>
  <section class="runs shell-panel">
    <div class="runs-head">
      <div>
        <h2>Run Inspector</h2>
        <p>Agent 运行、意图识别和工具调用的持久化轨迹。</p>
      </div>
      <span>{{ runs.length }} runs</span>
    </div>

    <div v-if="!runs.length" class="empty">
      <strong>还没有运行记录</strong>
      <p>在右侧 Agent Chat 发起一次追问后，这里会记录意图和工具调用。</p>
    </div>

    <article v-for="(run, index) in runs" :key="run.id" class="run-card">
      <header>
        <div>
          <strong>#{{ run.id }} · {{ run.kind }}</strong>
          <p>{{ run.question || 'No question' }}</p>
          <small>{{ formatDate(run.created_at) }} · {{ run.events.length }} events</small>
        </div>
        <span :class="['status', run.status]">{{ statusLabels[run.status] || run.status }}</span>
      </header>

      <details class="event-details" :open="index === 0">
        <summary>查看事件轨迹</summary>
        <ol>
          <li v-for="event in run.events" :key="event.id">
            <b>{{ event.event_type }}</b>
            <small>{{ summarize(event.payload) }}</small>
          </li>
        </ol>
      </details>
    </article>
  </section>
</template>

<style scoped>
.runs {
  padding: 16px;
}

.runs-head {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}

h2,
p {
  margin: 0;
}

.runs-head h2 {
  font-size: 16px;
}

.runs-head p,
.runs-head span,
.run-card p,
.run-card small,
li small,
.event-details summary {
  color: var(--text-muted);
  font-size: 12px;
}

.empty,
.run-card {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface-subtle);
}

.empty {
  padding: 28px;
  text-align: center;
}

.run-card {
  display: grid;
  gap: 12px;
  padding: 12px;
}

.run-card + .run-card {
  margin-top: 10px;
}

.run-card header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.status {
  height: 24px;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--text-muted);
  background: var(--surface);
  font-size: 11px;
}

.status.done {
  color: var(--ok);
}

.status.error {
  color: var(--err);
}

ol {
  display: grid;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.event-details summary {
  cursor: pointer;
  font-weight: 650;
}

.event-details ol {
  margin-top: 8px;
}

li {
  display: grid;
  gap: 2px;
  padding: 7px 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface);
}

li b {
  color: var(--text-soft);
  font-size: 12px;
}

li small {
  overflow-wrap: anywhere;
}
</style>
