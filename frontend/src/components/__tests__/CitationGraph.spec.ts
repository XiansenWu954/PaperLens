import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import CitationGraph from '../CitationGraph.vue'
import type { GraphData, GraphNode } from '../../types'

/**
 * CitationGraph renders the project citation graph. The manual §5.6 requires:
 *   - empty graph renders a clear "暂无引用图谱" message, not a broken container
 *   - node/edge/seminal/frontier counts are shown
 *   - "推荐先读" ties back to real papers (not just a centrality number)
 *
 * happy-dom has no ResizeObserver and no real canvas 2d context, so we stub
 * ResizeObserver. The empty-state path returns early before touching d3/canvas.
 */

beforeEach(() => {
  // CitationGraph constructs a ResizeObserver in onMounted.
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
  // requestAnimationFrame: d3-force tick may schedule one; make it a no-op.
  vi.stubGlobal('requestAnimationFrame', (() => 0) as typeof requestAnimationFrame)
  vi.stubGlobal('cancelAnimationFrame', (() => {}) as typeof cancelAnimationFrame)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('CitationGraph', () => {
  it('renders the empty state with no broken container when there are no nodes', () => {
    const wrapper = mount(CitationGraph, { props: { data: { nodes: [], edges: [] } } })
    expect(wrapper.find('.empty-graph').exists()).toBe(true)
    expect(wrapper.text()).toContain('暂无引用图谱')
    // No canvas shell in empty state.
    expect(wrapper.find('.canvas-shell').exists()).toBe(false)
    // Footer still renders counts (all zero).
    expect(wrapper.find('.graph-footer').text()).toContain('0 节点')
    expect(wrapper.find('.graph-footer').text()).toContain('0 边')
  })

  it('renders node/edge/seminal/frontier counts in the footer', () => {
    const data: GraphData = {
      nodes: [
        { id: 1, title: 'Mamba', year: 2023, citation_count: 100, seminal: 0.9, cluster: 1, is_root: true, is_frontier: false } as GraphNode,
        { id: 2, title: 'Transformer', year: 2017, citation_count: 100000, seminal: 0.95, cluster: 1, is_root: true, is_frontier: false } as GraphNode,
        { id: 3, title: 'New Frontier', year: 2025, citation_count: 5, seminal: 0.1, cluster: 2, is_root: false, is_frontier: true } as GraphNode,
      ],
      edges: [{ source: 1, target: 2, weight: 1 }],
    }
    const wrapper = mount(CitationGraph, { props: { data } })
    const footer = wrapper.find('.graph-footer')
    expect(footer.text()).toContain('3 节点')
    expect(footer.text()).toContain('1 边')
    expect(footer.text()).toContain('2 奠基性')
    expect(footer.text()).toContain('1 前沿')
  })

  it('lists recommended reading by seminal (pagerank) then citations, tied to real paper titles', () => {
    const data: GraphData = {
      nodes: [
        { id: 1, title: 'Low-cite seminal', year: 2020, citation_count: 3, seminal: 0.8, cluster: 1 } as GraphNode,
        { id: 2, title: 'High-cite seminal', year: 2017, citation_count: 100000, seminal: 0.95, cluster: 1 } as GraphNode,
      ],
      edges: [],
    }
    const wrapper = mount(CitationGraph, { props: { data } })
    const list = wrapper.find('.node-list')
    expect(list.exists()).toBe(true)
    const titles = list.findAll('button strong').map((b) => b.text())
    // Higher seminal ranks first.
    expect(titles[0]).toBe('High-cite seminal')
    expect(titles[1]).toBe('Low-cite seminal')
  })

  it('opens a detail panel when a recommended-reading node is selected', async () => {
    const data: GraphData = {
      nodes: [
        { id: 5, title: 'Selectable Paper', year: 2024, citation_count: 42, seminal: 0.5, cluster: 1 } as GraphNode,
      ],
      edges: [],
    }
    const wrapper = mount(CitationGraph, { props: { data } })
    expect(wrapper.find('.paper-detail').exists()).toBe(false)
    await wrapper.find('.node-list button').trigger('click')
    const detail = wrapper.find('.paper-detail')
    expect(detail.exists()).toBe(true)
    expect(detail.find('h4').text()).toBe('Selectable Paper')
  })

  it('renders topic groups only when there is more than one cluster', () => {
    const oneCluster: GraphData = {
      nodes: [
        { id: 1, title: 'A', cluster: 1, cluster_label: '主题 A', seminal: 0.1 } as GraphNode,
        { id: 2, title: 'B', cluster: 1, cluster_label: '主题 A', seminal: 0.1 } as GraphNode,
      ],
      edges: [],
    }
    const wrapper1 = mount(CitationGraph, { props: { data: oneCluster } })
    expect(wrapper1.find('.topic-groups').exists()).toBe(false)

    const twoClusters: GraphData = {
      nodes: [
        { id: 1, title: 'A', cluster: 1, cluster_label: 'SSM', seminal: 0.5 } as GraphNode,
        { id: 2, title: 'B', cluster: 2, cluster_label: 'Attention', seminal: 0.5 } as GraphNode,
      ],
      edges: [],
    }
    const wrapper2 = mount(CitationGraph, { props: { data: twoClusters } })
    const groups = wrapper2.findAll('.topic-groups .topic-row')
    expect(groups).toHaveLength(2)
  })
})
