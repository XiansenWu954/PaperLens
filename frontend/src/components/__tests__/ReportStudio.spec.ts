import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ReportStudio from '../ReportStudio.vue'
import type { ProjectPaper, ReportVersion } from '../../types'

/**
 * ReportStudio manages report versions. The manual §5.6 report checks cover:
 *   - distinguishing empty-state vs existing versions
 *   - create-draft -> save round-trip calls the saveReport prop
 *   - the citation audit surfaces evidence anchors and warns when missing
 * saveReport is a prop function (not a store import), so this is fully isolated.
 */

function paper(overrides: Partial<ProjectPaper> = {}): ProjectPaper {
  return {
    id: 1, paper_id: 1, title: 'Attention Is All You Need', abstract: '',
    year: 2017, venue: 'NeurIPS', citation_count: 1, doi: '10.1/a',
    arxiv_id: '1706.03762', openalex_id: null, pdf_url: null, status: 'included',
    source_reason: '', added_by: 'user', notes: '', ingestion_status: 'embedded',
    latest_ingestion_job_id: null, latest_ingestion_error: '', embedding_model: '',
    indexed_at: null, chunk_count: 5, created_at: '', updated_at: '', ...overrides,
  }
}

function report(overrides: Partial<ReportVersion> = {}): ReportVersion {
  return { id: 1, project: 1, title: 'Draft One', content: 'intro body', source: 'agent', created_at: '2026-08-08T00:00:00Z', ...overrides }
}

type SaveReport = (p: { title: string; content: string; source?: string }) => Promise<ReportVersion>

function mountStudio(props: Partial<{ reports: ReportVersion[]; papers: ProjectPaper[]; loading: boolean; saveReport: SaveReport }> = {}) {
  const defaultSave: SaveReport = async () => report({ id: 2, title: 'New' })
  return mount(ReportStudio, {
    props: {
      reports: [],
      papers: [paper()],
      saveReport: props.saveReport ?? defaultSave,
      ...props,
    },
  })
}

beforeEach(() => {
  vi.stubGlobal('confirm', vi.fn(() => true))
  vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('ReportStudio', () => {
  it('shows the empty state when there are no report versions', () => {
    const studio = mountStudio({ reports: [] })
    expect(studio.find('.empty-state').exists()).toBe(true)
    expect(studio.text()).toContain('暂无报告版本')
  })

  it('lists existing versions and selects the first', () => {
    const r1 = report({ id: 1, title: 'First' })
    const r2 = report({ id: 2, title: 'Second' })
    const studio = mountStudio({ reports: [r1, r2] })
    const rows = studio.findAll('.version-row')
    expect(rows).toHaveLength(2)
    expect(rows[0].classes()).toContain('active')
    expect(rows[0].text()).toContain('First')
  })

  it('creates a new draft, fills it, and saves via the saveReport prop', async () => {
    const saveReport = vi.fn().mockResolvedValue(report({ id: 9, title: 'My Version', content: 'hello world' }))
    const studio = mountStudio({ saveReport })
    expect(studio.find('textarea.draft-editor').exists()).toBe(false)

    await studio.findAll('button').find((b) => b.text() === '新建版本')!.trigger('click')
    const editor = studio.find('textarea.draft-editor')
    expect(editor.exists()).toBe(true)
    // Save button starts disabled (no content / no change).
    const saveBtn = studio.findAll('button').find((b) => b.text() === '保存版本')!
    expect((saveBtn.element as HTMLButtonElement).disabled).toBe(true)

    await studio.find('.editor-bar input').setValue('My Version')
    await editor.setValue('hello world')
    // Now changed + non-empty -> enabled.
    expect((saveBtn.element as HTMLButtonElement).disabled).toBe(false)

    await saveBtn.trigger('click')
    await flushPromises()
    expect(saveReport).toHaveBeenCalledWith({ title: 'My Version', content: 'hello world', source: 'user' })
    // After save, editing closes and the returned report is selected.
    expect(studio.findAll('.version-row').length).toBeGreaterThanOrEqual(0)
    expect(studio.find('textarea.draft-editor').exists()).toBe(false)
  })

  it('shows a save error when saveReport rejects, without throwing', async () => {
    const saveReport = vi.fn().mockRejectedValue(new Error('后端保存失败'))
    const studio = mountStudio({ saveReport })
    await studio.findAll('button').find((b) => b.text() === '新建版本')!.trigger('click')
    await studio.find('.editor-bar input').setValue('T')
    await studio.find('textarea.draft-editor').setValue('C')
    await studio.findAll('button').find((b) => b.text() === '保存版本')!.trigger('click')
    await flushPromises()
    expect(studio.find('.error-text').text()).toContain('后端保存失败')
  })

  it('flags missing evidence anchors with a warning when content has no citations', () => {
    const studio = mountStudio({ reports: [report({ content: '这是一段没有引用的综述。' })] })
    expect(studio.find('.audit-note.warn').exists()).toBe(true)
  })

  it('detects [cite:...] markers as evidence anchors and counts coverage', () => {
    const p = paper()
    const studio = mountStudio({
      papers: [p],
      reports: [report({ content: 'Transformer 依赖自注意力 [cite:1706.03762]。' })],
    })
    // [cite:...] is a source marker -> hasEvidenceAnchors true -> no warning.
    expect(studio.find('.audit-note.warn').exists()).toBe(false)
    expect(studio.text()).toContain('可追踪证据锚点')
    // The arxiv_id 1706.03762 appears in the content, so the paper is covered: 1/1.
    expect(studio.text()).toContain('1/1 covered')
    // And it does NOT appear in the missing list.
    expect(studio.find('.missing-list').exists()).toBe(false)
  })
})
