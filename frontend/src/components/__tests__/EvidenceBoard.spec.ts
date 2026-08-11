import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import EvidenceBoard from '../EvidenceBoard.vue'
import type { ProjectPaper } from '../../types'

/**
 * EvidenceBoard is the project paper library view. The manual §5.3 key assertion
 * is "never render 'added to project', 'has PDF URL', 'fully ingested', and
 * 'RAG-retrievable' as the same status." These tests cover that status
 * distinction plus the filter, remove-confirmation, upload and empty states.
 */

function makePaper(overrides: Partial<ProjectPaper> = {}): ProjectPaper {
  return {
    id: 1,
    paper_id: 1,
    title: 'Attention Is All You Need',
    abstract: 'A novel architecture.',
    year: 2017,
    venue: 'NeurIPS',
    citation_count: 100000,
    doi: '10.1/a',
    arxiv_id: '1706.03762',
    openalex_id: null,
    pdf_url: 'https://arxiv.org/pdf/1706.03762',
    status: 'candidate',
    source_reason: '',
    added_by: 'user',
    notes: '',
    ingestion_status: 'pending',
    latest_ingestion_job_id: null,
    latest_ingestion_error: '',
    embedding_model: '',
    indexed_at: null,
    chunk_count: 0,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

describe('EvidenceBoard', () => {
  it('renders the empty state when there are no papers', () => {
    const wrapper = mount(EvidenceBoard, { props: { papers: [] } })
    expect(wrapper.text()).toContain('项目库为空')
    expect(wrapper.findAll('.paper-row')).toHaveLength(0)
  })

  it('renders the loading state over the empty state', () => {
    const wrapper = mount(EvidenceBoard, { props: { papers: [], loading: true } })
    expect(wrapper.text()).toContain('正在加载项目论文')
    expect(wrapper.text()).not.toContain('项目库为空')
  })

  it('distinguishes ingestion_status with different meta classes (not one shared state)', () => {
    // Manual §5.3: pending/embedded/failed must each render distinctly.
    const pending = makePaper({ paper_id: 1, id: 1, title: 'Pending Paper', ingestion_status: 'pending', chunk_count: 0 })
    const embedded = makePaper({ paper_id: 2, id: 2, title: 'Embedded Paper', ingestion_status: 'embedded', chunk_count: 12 })
    const failed = makePaper({
      paper_id: 3, id: 3, title: 'Failed Paper', ingestion_status: 'failed',
      latest_ingestion_error: 'PDF 解析失败',
    })
    const wrapper = mount(EvidenceBoard, { props: { papers: [pending, embedded, failed] } })

    const rows = wrapper.findAll('.paper-row .meta')
    // pending -> warn, embedded -> good, failed -> bad (manual §5.3 key assertion)
    expect(rows[0].find('.warn').exists()).toBe(true)
    expect(rows[1].find('.good').exists()).toBe(true)
    expect(rows[2].find('.bad').exists()).toBe(true)
    // failed shows the ingest-error with the stored message + retry hint
    expect(wrapper.text()).toContain('入库失败：PDF 解析失败')
    expect(wrapper.text()).toContain('可重新上传或从链接重试')
  })

  it('filters to needs-rag papers only', async () => {
    const embedded = makePaper({ paper_id: 1, id: 1, title: 'Ready', ingestion_status: 'embedded', chunk_count: 5 })
    const pending = makePaper({ paper_id: 2, id: 2, title: 'Needs Ingest', ingestion_status: 'pending', chunk_count: 0 })
    const wrapper = mount(EvidenceBoard, { props: { papers: [embedded, pending] } })

    expect(wrapper.findAll('.paper-row')).toHaveLength(2)
    await wrapper.findAll('.filter-button').find((b) => b.text() === '待向量化')!.trigger('click')
    // Only the pending paper matches needs-rag; embedded is excluded.
    expect(wrapper.findAll('.paper-row')).toHaveLength(1)
    expect(wrapper.find('.paper-row h3').text()).toBe('Needs Ingest')
  })

  it('shows a clear-filters button only when a filter/query is active and clears it', async () => {
    const paper = makePaper()
    const wrapper = mount(EvidenceBoard, { props: { papers: [paper] } })
    expect(wrapper.find('.filter-button.clear').exists()).toBe(false)

    await wrapper.find('#evidence-filter').setValue('Attention')
    expect(wrapper.find('.filter-button.clear').exists()).toBe(true)
    await wrapper.find('.filter-button.clear').trigger('click')
    expect(wrapper.find('.filter-button.clear').exists()).toBe(false)
  })

  it('emits setStatus when the status select changes', async () => {
    const paper = makePaper()
    const wrapper = mount(EvidenceBoard, { props: { papers: [paper] } })
    await wrapper.find('select').setValue('core')
    const events = wrapper.emitted('setStatus')
    expect(events).toBeTruthy()
    expect(events![0]).toEqual([1, 'core'])
  })

  it('emits uploadPdf when a file is selected', async () => {
    const paper = makePaper()
    const wrapper = mount(EvidenceBoard, { props: { papers: [paper] } })
    const file = new File(['pdf-bytes'], 'paper.pdf', { type: 'application/pdf' })
    const input = wrapper.find('input[type="file"]')
    // Vue Test Utils setValue only works for text/select; for file input set files then trigger change.
    Object.defineProperty(input.element, 'files', { value: [file], configurable: true })
    await input.trigger('change')
    const events = wrapper.emitted('uploadPdf')
    expect(events).toBeTruthy()
    expect(events![0]).toEqual([1, file])
  })

  it('disables ingest-from-url when there is no pdf_url', () => {
    const noUrl = makePaper({ paper_id: 9, id: 9, pdf_url: null })
    const wrapper = mount(EvidenceBoard, { props: { papers: [noUrl] } })
    const ingestBtn = wrapper.findAll('button').find((b) => b.text().includes('从链接入库'))!
    expect((ingestBtn.element as HTMLButtonElement).disabled).toBe(true)
  })

  it('emits ingestPdf when the ingest button is clicked (paper has pdf_url)', async () => {
    const paper = makePaper()
    const wrapper = mount(EvidenceBoard, { props: { papers: [paper] } })
    const ingestBtn = wrapper.findAll('button').find((b) => b.text().includes('从链接入库'))!
    await ingestBtn.trigger('click')
    expect(wrapper.emitted('ingestPdf')?.[0]).toEqual([1])
  })

  it('requires two steps to remove a paper: confirm is a separate action', async () => {
    const paper = makePaper({ paper_id: 7, id: 7 })
    const wrapper = mount(EvidenceBoard, { props: { papers: [paper] } })

    // Step 1: click "移出项目" -> shows confirm/cancel, does NOT emit yet.
    await wrapper.findAll('button').find((b) => b.text() === '移出项目')!.trigger('click')
    expect(wrapper.emitted('remove')).toBeUndefined()
    expect(wrapper.find('.danger-button').text()).toContain('确认移出')

    // Cancel returns to the single-button state without emitting.
    await wrapper.findAll('button').find((b) => b.text() === '取消')!.trigger('click')
    expect(wrapper.emitted('remove')).toBeUndefined()
    expect(wrapper.find('.danger-button').exists()).toBe(false)

    // Re-request then confirm -> emits remove with the paper id.
    await wrapper.findAll('button').find((b) => b.text() === '移出项目')!.trigger('click')
    await wrapper.find('.danger-button').trigger('click')
    expect(wrapper.emitted('remove')?.[0]).toEqual([7])
  })
})
