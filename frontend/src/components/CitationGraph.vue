<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import * as d3 from 'd3-force'
import type { GraphData, GraphNode } from '../types'

const props = defineProps<{ data: GraphData }>()
const wrapRef = ref<HTMLElement | null>(null)
const canvasRef = ref<HTMLCanvasElement | null>(null)
const selected = ref<GraphNode | null>(null)

let simulation: d3.Simulation<GraphNode & d3.SimulationNodeDatum, undefined> | null = null
let rafId = 0
let resizeObserver: ResizeObserver | null = null
let nodes: (GraphNode & d3.SimulationNodeDatum)[] = []
let links: any[] = []

const clusterPalette = ['#2f6f73', '#8b6f39', '#6e5b8f', '#7a624d', '#4f738f', '#7f5d62', '#55765d']

const stats = computed(() => ({
  nodes: props.data.nodes.length,
  edges: props.data.edges.length,
  roots: props.data.nodes.filter((node) => node.is_root).length,
  frontiers: props.data.nodes.filter((node) => node.is_frontier).length,
}))

const visibleNodes = computed(() =>
  [...props.data.nodes]
    .sort((a, b) => Number(b.is_root) - Number(a.is_root) || Number(b.is_frontier) - Number(a.is_frontier) || (b.citation_count || 0) - (a.citation_count || 0))
    .slice(0, 8),
)

function colorFor(cluster: number) {
  return clusterPalette[Math.abs(cluster || 0) % clusterPalette.length]
}

function radius(node: GraphNode) {
  const citations = node.citation_count || 0
  return Math.max(5, Math.min(17, 5 + Math.log10(citations + 1) * 4))
}

function resizeCanvas() {
  const canvas = canvasRef.value
  const wrap = wrapRef.value
  if (!canvas || !wrap) return null
  const rect = wrap.getBoundingClientRect()
  const width = Math.max(260, Math.floor(rect.width))
  const height = Math.max(360, Math.min(520, Math.floor(width * 0.58)))
  const dpr = window.devicePixelRatio || 1
  canvas.width = Math.floor(width * dpr)
  canvas.height = Math.floor(height * dpr)
  canvas.style.height = `${height}px`
  canvas.style.width = `${width}px`
  const ctx = canvas.getContext('2d')
  if (!ctx) return null
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  return { width, height, ctx }
}

function stop() {
  if (rafId) cancelAnimationFrame(rafId)
  rafId = 0
  simulation?.stop()
  simulation = null
}

async function build() {
  stop()
  selected.value = null
  await nextTick()
  const canvasState = resizeCanvas()
  if (!canvasState || !props.data.nodes.length) return
  const { width, height, ctx } = canvasState

  nodes = props.data.nodes.map((node) => ({ ...node }))
  const idMap = new Map(nodes.map((node) => [node.id, node]))
  links = props.data.edges
    .filter((edge) => idMap.has(edge.source as number) && idMap.has(edge.target as number))
    .map((edge) => ({ ...edge }))

  simulation = d3.forceSimulation(nodes)
    .force('link', d3.forceLink(links).id((node: any) => node.id).distance(68).strength(0.22))
    .force('charge', d3.forceManyBody().strength(-110))
    .force('center', d3.forceCenter(width / 2, height / 2))
    .force('collide', d3.forceCollide<GraphNode & d3.SimulationNodeDatum>().radius((node) => radius(node) + 5))

  function draw() {
    ctx.clearRect(0, 0, width, height)
    ctx.fillStyle = '#fbfaf6'
    ctx.fillRect(0, 0, width, height)

    for (const link of links) {
      const source = link.source
      const target = link.target
      if (!source?.x || !target?.x) continue
      ctx.strokeStyle = 'rgba(80, 72, 58, 0.18)'
      ctx.lineWidth = Math.max(0.6, Math.min(2, (link.weight || 1) * 0.28))
      ctx.beginPath()
      ctx.moveTo(source.x, source.y)
      ctx.lineTo(target.x, target.y)
      ctx.stroke()
    }

    for (const node of nodes) {
      const r = radius(node)
      ctx.beginPath()
      ctx.fillStyle = colorFor(node.cluster)
      ctx.arc(node.x || width / 2, node.y || height / 2, r, 0, Math.PI * 2)
      ctx.fill()
      if (node.is_root || node.is_frontier) {
        ctx.strokeStyle = node.is_root ? '#8b6f39' : '#2f6f73'
        ctx.lineWidth = 2
        ctx.stroke()
      }
    }

    rafId = requestAnimationFrame(draw)
  }

  draw()
}

function handleClick(event: MouseEvent) {
  const canvas = canvasRef.value
  if (!canvas) return
  const rect = canvas.getBoundingClientRect()
  const mx = event.clientX - rect.left
  const my = event.clientY - rect.top
  selected.value = nodes.find((node) => Math.hypot((node.x || 0) - mx, (node.y || 0) - my) <= radius(node) + 4) || null
}

function selectNode(node: GraphNode) {
  selected.value = node
}

function closeDetail() {
  selected.value = null
}

onMounted(() => {
  resizeObserver = new ResizeObserver(() => build())
  if (wrapRef.value) resizeObserver.observe(wrapRef.value)
  build()
})

watch(() => props.data, () => build(), { deep: true })

onUnmounted(() => {
  resizeObserver?.disconnect()
  stop()
})
</script>

<template>
  <div class="graph-view" ref="wrapRef">
    <div v-if="data.nodes.length" class="canvas-shell">
      <canvas
        ref="canvasRef"
        class="graph-canvas"
        role="img"
        tabindex="0"
        :aria-label="`项目引用图谱，${stats.nodes} 个节点，${stats.edges} 条关系`"
        @click="handleClick"
        @keydown.escape="closeDetail"
      ></canvas>
      <div class="legend">
        <span><i class="root"></i>奠基性</span>
        <span><i class="frontier"></i>前沿</span>
        <span><i class="cluster"></i>主题簇</span>
      </div>
    </div>

    <div v-else class="empty-graph">
      <strong>暂无引用图谱</strong>
      <p>检索到足够带引用关系的论文后，这里会显示论文间的相似结构。</p>
    </div>

    <div class="graph-footer">
      <span>{{ stats.nodes }} 节点</span>
      <span>{{ stats.edges }} 边</span>
      <span>{{ stats.roots }} 奠基性</span>
      <span>{{ stats.frontiers }} 前沿</span>
    </div>

    <div v-if="visibleNodes.length" class="node-list" aria-label="图谱论文列表">
      <button
        v-for="node in visibleNodes"
        :key="node.id"
        type="button"
        :class="{ active: selected?.id === node.id }"
        :aria-pressed="selected?.id === node.id"
        @click="selectNode(node)"
      >
        <strong>{{ node.title }}</strong>
        <span>{{ node.year || 'n.d.' }} · {{ node.citation_count }} citations</span>
      </button>
    </div>

    <aside v-if="selected" class="paper-detail">
      <button type="button" class="ghost-button close" @click="closeDetail" aria-label="关闭论文详情">×</button>
      <p class="detail-kicker">Selected paper</p>
      <h4>{{ selected.title }}</h4>
      <dl>
        <div><dt>Year</dt><dd>{{ selected.year || 'Unknown' }}</dd></div>
        <div><dt>Citations</dt><dd>{{ selected.citation_count }}</dd></div>
        <div><dt>Cluster</dt><dd>{{ selected.cluster }}</dd></div>
      </dl>
      <div class="tags">
        <span v-if="selected.is_root">奠基性</span>
        <span v-if="selected.is_frontier">前沿</span>
        <span v-if="selected.doi">DOI</span>
        <span v-if="selected.arxiv_id">arXiv</span>
      </div>
      <a v-if="selected.arxiv_id" :href="`https://arxiv.org/abs/${selected.arxiv_id}`" target="_blank" rel="noreferrer">
        打开 arXiv
      </a>
    </aside>
  </div>
</template>

<style scoped>
.graph-view {
  position: relative;
  min-width: 0;
}

.canvas-shell,
.empty-graph {
  max-width: 100%;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--paper);
}

.graph-canvas {
  display: block;
  max-width: 100%;
  cursor: pointer;
}

.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  padding: 10px 12px;
  border-top: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 12px;
}

.legend span {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.legend i {
  width: 10px;
  height: 10px;
  border-radius: 999px;
  background: var(--accent);
}

.legend .root {
  border: 2px solid #8b6f39;
}

.legend .frontier {
  border: 2px solid var(--accent);
}

.legend .cluster {
  background: #6e5b8f;
}

.empty-graph {
  display: grid;
  place-items: center;
  min-height: 360px;
  padding: 24px;
  text-align: center;
}

.empty-graph strong {
  display: block;
  margin-bottom: 6px;
}

.empty-graph p {
  max-width: 360px;
  margin: 0;
  color: var(--text-muted);
}

.graph-footer {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.graph-footer span,
.tags span {
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--surface-subtle);
  color: var(--text-soft);
  font-size: 12px;
}

.node-list {
  display: grid;
  gap: 6px;
  margin-top: 10px;
}

.node-list button {
  display: grid;
  gap: 3px;
  width: 100%;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--surface-subtle);
  color: var(--text);
  cursor: pointer;
  text-align: left;
}

.node-list button.active,
.node-list button:hover {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.node-list strong {
  overflow: hidden;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.node-list span {
  color: var(--text-muted);
  font-size: 11px;
}

.paper-detail {
  position: absolute;
  top: 12px;
  right: 12px;
  width: min(300px, calc(100% - 24px));
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: rgba(255, 253, 248, 0.96);
  box-shadow: var(--shadow);
}

.close {
  position: absolute;
  top: 8px;
  right: 8px;
  min-height: 28px;
  width: 28px;
  padding: 0;
  font-size: 20px;
}

.detail-kicker {
  margin-bottom: 6px;
  color: var(--text-muted);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
}

h4 {
  margin: 0 28px 12px 0;
  font-size: 15px;
  line-height: 1.35;
}

dl {
  display: grid;
  gap: 8px;
  margin: 0 0 12px;
}

dl div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

dt {
  color: var(--text-muted);
}

dd {
  margin: 0;
  color: var(--text);
  font-weight: 700;
}

.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
</style>
