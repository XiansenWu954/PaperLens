<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ report: string; status: string }>()

const html = computed(() => {
  if (!props.report) return ''
  const escape = (value: string) =>
    value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  const lines = escape(props.report).split('\n')
  let output = ''
  let inList = false

  for (const rawLine of lines) {
    const line = rawLine.trimEnd()
    if (/^#{1,4}\s/.test(line)) {
      if (inList) { output += '</ul>'; inList = false }
      const level = Math.min(line.match(/^#+/)![0].length, 4)
      output += `<h${level}>${line.replace(/^#+\s/, '')}</h${level}>`
    } else if (/^[-*]\s/.test(line) || /^\d+\.\s/.test(line)) {
      if (!inList) { output += '<ul>'; inList = true }
      output += `<li>${line.replace(/^[-*]\s/, '').replace(/^\d+\.\s/, '')}</li>`
    } else if (line === '' || line === '---') {
      if (inList) { output += '</ul>'; inList = false }
      if (line === '---') output += '<hr/>'
    } else {
      if (inList) { output += '</ul>'; inList = false }
      output += `<p>${line}</p>`
    }
  }
  if (inList) output += '</ul>'
  return output
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(pqac-[a-zA-Z0-9]{8})/g, '<span class="cite">$1</span>')
    .replace(/\[cite:([^\]]+)\]/gi, '<span class="cite verified" title="来自项目证据">$1</span>')
})
</script>

<template>
  <article class="report-reader">
    <div v-if="!report && status === 'running'" class="report-state">
      <span class="pulse"></span>
      <div>
        <strong>正在生成综述</strong>
        <p>报告会随着模型输出逐段出现。</p>
      </div>
    </div>
    <div v-else-if="!report" class="report-state">
      <div>
        <strong>暂无报告</strong>
        <p>任务完成后，这里会显示结构化研究综述。</p>
      </div>
    </div>
    <div v-else class="report-content" v-html="html"></div>
  </article>
</template>

<style scoped>
.report-reader {
  min-height: 360px;
  background: var(--paper);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.report-state {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 28px;
  color: var(--text-soft);
}

.report-state strong {
  display: block;
  margin-bottom: 4px;
  color: var(--text);
}

.report-state p {
  margin: 0;
  color: var(--text-muted);
}

.pulse {
  width: 10px;
  height: 10px;
  margin-top: 6px;
  border-radius: 999px;
  background: var(--warn);
  animation: pulse 1.2s ease-in-out infinite;
}

@keyframes pulse {
  50% { opacity: 0.35; transform: scale(0.72); }
}

.report-content {
  max-width: 780px;
  margin: 0 auto;
  padding: clamp(24px, 5vw, 52px);
  color: var(--text);
  font-family: var(--serif);
  font-size: 17px;
  line-height: 1.78;
}

.report-content :deep(h1),
.report-content :deep(h2),
.report-content :deep(h3),
.report-content :deep(h4) {
  font-family: var(--sans);
  line-height: 1.25;
  letter-spacing: 0;
}

.report-content :deep(h1) {
  margin: 0 0 20px;
  font-size: 28px;
}

.report-content :deep(h2) {
  margin: 30px 0 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  font-size: 21px;
}

.report-content :deep(h3) {
  margin: 22px 0 8px;
  font-size: 17px;
}

.report-content :deep(p) {
  margin: 12px 0;
}

.report-content :deep(ul) {
  margin: 12px 0;
  padding-left: 22px;
}

.report-content :deep(li) {
  margin: 7px 0;
}

.report-content :deep(hr) {
  margin: 24px 0;
  border: 0;
  border-top: 1px solid var(--border);
}

.report-content :deep(strong) {
  color: var(--text);
  font-weight: 700;
}

.report-content :deep(.cite) {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  border: 1px solid #c9d9d7;
  border-radius: 5px;
  background: var(--accent-soft);
  color: var(--accent-strong);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.45;
}
</style>
