/**
 * Phase 1 ingestion lifecycle RED tests (Tasks 1.5 / Evidence Board).
 *
 * These are RED: the current EvidenceBoard only renders the 4-state
 * lifecycle (pending/parsing/embedded/failed) and has no duplicate-command
 * disabling, retry, upload-required or fulltext_ready semantics. After
 * Tasks 5.5 these assertions must all pass unchanged.
 */
import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import EvidenceBoard from '../EvidenceBoard.vue'
import type { ProjectPaper } from '../../types'

function makePaper(overrides: Record<string, unknown> = {}): ProjectPaper {
  return {
    id: 1, paper_id: 1, title: 'Paper', abstract: 'a', year: 2024,
    venue: '', citation_count: 0, doi: null, arxiv_id: null, openalex_id: null,
    pdf_url: null, status: 'candidate', source_reason: '', added_by: 'user',
    notes: '', ingestion_status: 'pending', latest_ingestion_job_id: null,
    latest_ingestion_error: '', embedding_model: '', indexed_at: null,
    chunk_count: 0, fulltext_ready: false, latest_job_retryable: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...(overrides as Partial<ProjectPaper>),
  }
}

describe('EvidenceBoard ingestion lifecycle (RED baseline)', () => {
  it('renders the downloading intermediate state', () => {
    const wrapper = mount(EvidenceBoard, {
      props: { papers: [makePaper({ ingestion_status: 'downloading' })] },
    })
    expect(wrapper.text()).toContain('下载中')
  })

  it('renders the embedding intermediate state', () => {
    const wrapper = mount(EvidenceBoard, {
      props: { papers: [makePaper({ ingestion_status: 'embedding' })] },
    })
    expect(wrapper.text()).toContain('向量化中')
  })

  it('renders the committing intermediate state', () => {
    const wrapper = mount(EvidenceBoard, {
      props: { papers: [makePaper({ ingestion_status: 'committing' })] },
    })
    expect(wrapper.text()).toContain('提交中')
  })

  it('disables duplicate upload/ingest commands while a job is active', () => {
    for (const state of ['pending', 'downloading', 'parsing', 'embedding', 'committing']) {
      const wrapper = mount(EvidenceBoard, {
        props: { papers: [makePaper({ ingestion_status: state })] },
      })
      const buttons = wrapper.findAll('button')
      // Vue renders boolean attributes as valueless (getAttribute -> ''),
      // so "disabled" presence is attributes('disabled') !== undefined.
      const actionable = buttons.filter((b) => b.attributes('disabled') === undefined)
      // every upload/ingest action button must be disabled while active
      expect(actionable.length, `${state}: duplicate commands must be disabled`)
        .toBeLessThan(buttons.length)
    }
  })

  it('offers a retry action for a failed retryable job', () => {
    const wrapper = mount(EvidenceBoard, {
      props: {
        papers: [makePaper({
          ingestion_status: 'failed',
          latest_ingestion_error: 'pdf_parse_failed',
        })],
      },
    })
    expect(wrapper.text()).toContain('重试')
  })

  it('shows upload-required copy for a paper without a trusted PDF URL', () => {
    const wrapper = mount(EvidenceBoard, {
      props: { papers: [makePaper({ pdf_url: null, ingestion_status: 'pending' })] },
    })
    expect(wrapper.text()).toContain('上传 PDF')
  })

  it('does not render full-text readiness from a URL or membership alone', () => {
    // fulltext_ready=false (metadata-only): the row must NOT claim readiness
    const wrapper = mount(EvidenceBoard, {
      props: {
        papers: [makePaper({
          pdf_url: 'https://arxiv.org/pdf/x',
          ingestion_status: 'embedded',
          chunk_count: 0,
          fulltext_ready: false,
        })],
      },
    })
    expect(wrapper.text()).not.toContain('全文已就绪')
  })

  it('renders full-text readiness only for an active index with chunks', () => {
    const wrapper = mount(EvidenceBoard, {
      props: {
        papers: [makePaper({
          ingestion_status: 'embedded',
          chunk_count: 12,
          fulltext_ready: true,
        })],
      },
    })
    expect(wrapper.text()).toContain('全文已就绪')
  })
})
