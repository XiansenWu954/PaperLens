import { beforeEach, describe, expect, it, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { useProjectsStore } from '../projects'
import type { ProjectPaper, ReportVersion, ResearchProject } from '../../types'

/**
 * The projects store owns the workspace state machine: load project + papers +
 * reports + runs + ingestion jobs, and mutate papers/reports. The manual §5.7
 * requires covering loading / error / refresh for the store layer. All network
 * helpers are mocked so nothing depends on a live backend.
 */

const listProjects = vi.fn()
const createProject = vi.fn()
const getProject = vi.fn()
const listProjectPapers = vi.fn()
const updateProjectPaper = vi.fn()
const removeProjectPaper = vi.fn()
const listReports = vi.fn()
const createReport = vi.fn()
const listProjectRuns = vi.fn()
const listProjectIngestionJobs = vi.fn()
const uploadProjectPaperPdf = vi.fn()
const ingestProjectPaperPdf = vi.fn()
const searchAddProjectPapers = vi.fn()
const importProjectPapers = vi.fn()
const seedDemoProjectApi = vi.fn()
const startResearchExpandWorkflow = vi.fn()

vi.mock('../../types', async () => {
  const actual = await vi.importActual<typeof import('../../types')>('../../types')
  return {
    ...actual,
    listProjects: (...a: unknown[]) => listProjects(...a),
    createProject: (...a: unknown[]) => createProject(...a),
    getProject: (...a: unknown[]) => getProject(...a),
    listProjectPapers: (...a: unknown[]) => listProjectPapers(...a),
    updateProjectPaper: (...a: unknown[]) => updateProjectPaper(...a),
    removeProjectPaper: (...a: unknown[]) => removeProjectPaper(...a),
    listReports: (...a: unknown[]) => listReports(...a),
    createReport: (...a: unknown[]) => createReport(...a),
    listProjectRuns: (...a: unknown[]) => listProjectRuns(...a),
    listProjectIngestionJobs: (...a: unknown[]) => listProjectIngestionJobs(...a),
    uploadProjectPaperPdf: (...a: unknown[]) => uploadProjectPaperPdf(...a),
    ingestProjectPaperPdf: (...a: unknown[]) => ingestProjectPaperPdf(...a),
    searchAddProjectPapers: (...a: unknown[]) => searchAddProjectPapers(...a),
    importProjectPapers: (...a: unknown[]) => importProjectPapers(...a),
    seedDemoProject: (...a: unknown[]) => seedDemoProjectApi(...a),
    startResearchExpandWorkflow: (...a: unknown[]) => startResearchExpandWorkflow(...a),
  }
})

function project(overrides: Partial<ResearchProject> = {}): ResearchProject {
  return { id: 1, title: 'Mamba research', description: '', status: 'active', paper_count: 0, run_count: 0, latest_report_id: null, created_at: '', updated_at: '', ...overrides }
}
function paper(overrides: Partial<ProjectPaper> = {}): ProjectPaper {
  return { id: 1, paper_id: 1, title: 'Mamba', abstract: '', year: 2023, venue: '', citation_count: 0, doi: null, arxiv_id: null, openalex_id: null, pdf_url: null, status: 'candidate', source_reason: '', added_by: 'user', notes: '', ingestion_status: 'pending', latest_ingestion_job_id: null, latest_ingestion_error: '', embedding_model: '', indexed_at: null, chunk_count: 0, fulltext_ready: false,
    latest_job_retryable: false, created_at: '', updated_at: '', ...overrides }
}

beforeEach(() => {
  setActivePinia(createPinia())
  ;[
    listProjects, createProject, getProject, listProjectPapers, updateProjectPaper,
    removeProjectPaper, listReports, createReport, listProjectRuns,
    listProjectIngestionJobs, uploadProjectPaperPdf, ingestProjectPaperPdf,
    searchAddProjectPapers, importProjectPapers, seedDemoProjectApi, startResearchExpandWorkflow,
  ].forEach((m) => m.mockReset())
})

describe('useProjectsStore', () => {
  it('loadProjects fills the list and clears error', async () => {
    listProjects.mockResolvedValue([project({ id: 1 }), project({ id: 2, title: 'RAG eval' })])
    const store = useProjectsStore()
    await store.loadProjects()
    expect(store.projects).toHaveLength(2)
    expect(store.error).toBe('')
    expect(store.loading).toBe(false)
  })

  it('loadProjects surfaces a friendly error on network failure (no throw)', async () => {
    listProjects.mockRejectedValue(new TypeError('fetch failed'))
    const store = useProjectsStore()
    await store.loadProjects()
    expect(store.projects).toHaveLength(0)
    expect(store.error.length).toBeGreaterThan(0)
    expect(store.loading).toBe(false)
  })

  it('addProject prepends the new project', async () => {
    createProject.mockResolvedValue(project({ id: 9, title: 'New' }))
    const store = useProjectsStore()
    store.projects = [project({ id: 1 })]
    const created = await store.addProject('New', 'desc')
    expect(created.id).toBe(9)
    expect(store.projects[0].id).toBe(9)
    expect(store.projects).toHaveLength(2)
  })

  it('loadProject fans out to all artifacts and fills state', async () => {
    getProject.mockResolvedValue(project({ id: 7, title: 'Graph lit' }))
    listProjectPapers.mockResolvedValue([paper({ paper_id: 1 })])
    listReports.mockResolvedValue([])
    listProjectIngestionJobs.mockResolvedValue([])
    listProjectRuns.mockResolvedValue([{ id: 1, project: 7 } as never])
    const store = useProjectsStore()
    await store.loadProject(7)
    expect(store.currentProject?.id).toBe(7)
    expect(store.papers).toHaveLength(1)
    expect(store.runs).toHaveLength(1)
    expect(store.loading).toBe(false)
  })

  it('loadProject sets error and rethrows on failure', async () => {
    getProject.mockRejectedValue(new Error('404'))
    const store = useProjectsStore()
    await expect(store.loadProject(99)).rejects.toThrow('404')
    expect(store.error.length).toBeGreaterThan(0)
    expect(store.loading).toBe(false)
  })

  it('setPaperStatus maps the updated paper into the list', async () => {
    const updated = paper({ paper_id: 2, status: 'core' })
    updateProjectPaper.mockResolvedValue(updated)
    const store = useProjectsStore()
    store.papers = [paper({ paper_id: 1, status: 'candidate' }), paper({ paper_id: 2, status: 'candidate' })]
    await store.setPaperStatus(1, 2, 'core')
    expect(store.papers[1].status).toBe('core')
    expect(store.papers[0].status).toBe('candidate')
  })

  it('removePaper filters the paper out and refreshes the project', async () => {
    removeProjectPaper.mockResolvedValue(undefined)
    getProject.mockResolvedValue(project({ id: 1 }))
    const store = useProjectsStore()
    store.papers = [paper({ paper_id: 1 }), paper({ paper_id: 2 })]
    await store.removePaper(1, 2)
    expect(store.papers).toHaveLength(1)
    expect(store.papers[0].paper_id).toBe(1)
  })

  it('saveReport prepends the report and refreshes project/runs', async () => {
    createReport.mockResolvedValue({ id: 5, project: 1, title: 'Related work', content: '...', source: 'agent', created_at: '' } as ReportVersion)
    getProject.mockResolvedValue(project({ id: 1 }))
    listProjectRuns.mockResolvedValue([])
    const store = useProjectsStore()
    store.reports = [{ id: 1, project: 1, title: 'Old', content: '', source: 'user', created_at: '' }]
    const saved = await store.saveReport(1, { title: 'Related work', content: '...' })
    expect(saved.id).toBe(5)
    expect(store.reports[0].id).toBe(5)
    expect(store.reports).toHaveLength(2)
  })

  it('importPapers returns the import result and refreshes papers', async () => {
    importProjectPapers.mockResolvedValue({ format: 'bibtex', count: 3, added: 3 })
    listProjectPapers.mockResolvedValue([paper({ paper_id: 1 }), paper({ paper_id: 2 }), paper({ paper_id: 3 })])
    getProject.mockResolvedValue(project({ id: 1 }))
    const store = useProjectsStore()
    const result = await store.importPapers(1, '@misc{...}', 'bibtex')
    expect(result.count).toBe(3)
    expect(store.papers).toHaveLength(3)
  })
})
