<script setup lang="ts">
import { computed } from 'vue'
import type { StepEvent } from '../types'

const props = defineProps<{ steps: StepEvent[]; status: string }>()

const NODE_LABELS: Record<string, { label: string; helper: string }> = {
  planner: { label: '规划检索', helper: '拆解研究问题' },
  fan_out_researchers: { label: '检索论文', helper: '并行搜索与入库' },
  citation_graph: { label: '引用图谱', helper: '识别根节点、前沿和主题簇' },
  synthesizer: { label: '生成综述', helper: '整理证据与来源' },
}

const expected = ['planner', 'fan_out_researchers', 'citation_graph', 'synthesizer']

const displaySteps = computed(() => {
  const byNode = new Map(props.steps.map((step) => [step.node, step]))
  return expected.map((node) => {
    const event = byNode.get(node)
    const meta = NODE_LABELS[node]
    return {
      node,
      label: meta.label,
      helper: meta.helper,
      done: Boolean(event?.done),
      active: Boolean(event) && !event?.done,
      plan: event?.plan || [],
      sources: event?.sources,
      notes: event?.notes,
      receivedAt: event?.receivedAt,
    }
  })
})

function timeLabel(value?: string) {
  if (!value) return ''
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(new Date(value))
}
</script>

<template>
  <div class="timeline">
    <div
      v-for="(step, index) in displaySteps"
      :key="step.node"
      class="step"
      :class="{ done: step.done, active: step.active, waiting: !step.done && !step.active }"
    >
      <div class="rail">
        <span class="index">{{ index + 1 }}</span>
        <span v-if="index < displaySteps.length - 1" class="line"></span>
      </div>
      <div class="body">
        <div class="title-row">
          <strong>{{ step.label }}</strong>
          <time v-if="step.receivedAt">{{ timeLabel(step.receivedAt) }}</time>
        </div>
        <p>{{ step.helper }}</p>
        <div v-if="step.plan.length" class="chips">
          <span v-for="item in step.plan.slice(0, 3)" :key="item">{{ item }}</span>
        </div>
        <div v-if="step.sources !== undefined" class="metrics">
          <span>{{ step.sources }} 篇论文</span>
          <span>{{ step.notes || 0 }} 条笔记</span>
        </div>
      </div>
    </div>

    <p v-if="!steps.length && status === 'running'" class="empty">等待后端发送第一条进度。</p>
  </div>
</template>

<style scoped>
.timeline {
  display: grid;
  gap: 2px;
}

.step {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr);
  gap: 10px;
}

.rail {
  display: grid;
  justify-items: center;
  grid-template-rows: 24px 1fr;
}

.index {
  width: 24px;
  height: 24px;
  display: grid;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface);
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 700;
}

.line {
  width: 1px;
  min-height: 42px;
  background: var(--border);
}

.done .index {
  border-color: var(--ok);
  background: #e7f1ed;
  color: var(--ok);
}

.active .index {
  border-color: var(--warn);
  background: #f6edda;
  color: var(--warn);
}

.body {
  padding-bottom: 16px;
}

.title-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.title-row strong {
  font-size: 14px;
}

time {
  color: var(--text-muted);
  font-size: 11px;
}

p {
  margin: 3px 0 8px;
  color: var(--text-muted);
  font-size: 12px;
}

.chips,
.metrics {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.chips span,
.metrics span {
  max-width: 100%;
  padding: 3px 7px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface-subtle);
  color: var(--text-soft);
  font-size: 11px;
}

.empty {
  margin: 8px 0 0;
  color: var(--text-muted);
  font-size: 13px;
}
</style>
