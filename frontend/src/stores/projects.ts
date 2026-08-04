import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { PaperIngestionJob, ProjectPaper, ProjectRun, ReportVersion, ResearchProject } from '../types'
import {
  createProject,
  getProject,
  ingestProjectPaperPdf,
  listProjectIngestionJobs,
  listProjectPapers,
  listProjects,
  listProjectRuns,
  listReports,
  removeProjectPaper,
  searchAddProjectPapers,
  seedDemoProject,
  startResearchExpandWorkflow,
  updateProjectPaper,
  createReport,
  uploadProjectPaperPdf,
} from '../types'

export const useProjectsStore = defineStore('projects', () => {
  const projects = ref<ResearchProject[]>([])
  const currentProject = ref<ResearchProject | null>(null)
  const papers = ref<ProjectPaper[]>([])
  const reports = ref<ReportVersion[]>([])
  const runs = ref<ProjectRun[]>([])
  const ingestionJobs = ref<PaperIngestionJob[]>([])
  const loading = ref(false)
  const error = ref('')

  async function loadProjects() {
    loading.value = true
    error.value = ''
    try {
      projects.value = await listProjects()
    } catch (e) {
      error.value = e instanceof Error ? e.message : '加载项目失败'
    } finally {
      loading.value = false
    }
  }

  async function addProject(title: string, description = '') {
    const project = await createProject({ title, description })
    projects.value = [project, ...projects.value]
    return project
  }

  async function seedDemo() {
    const result = await seedDemoProject()
    await loadProjects()
    return result.project
  }

  async function loadProject(projectId: number) {
    loading.value = true
    error.value = ''
    try {
      const [project, projectPapers, projectReports, jobs] = await Promise.all([
        getProject(projectId),
        listProjectPapers(projectId),
        listReports(projectId),
        listProjectIngestionJobs(projectId),
      ])
      currentProject.value = project
      papers.value = projectPapers
      reports.value = projectReports
      ingestionJobs.value = jobs
      runs.value = await listProjectRuns(projectId)
    } catch (e) {
      error.value = e instanceof Error ? e.message : '加载项目失败'
      throw e
    } finally {
      loading.value = false
    }
  }

  async function searchAdd(projectId: number, query: string) {
    const result = await searchAddProjectPapers(projectId, query)
    papers.value = await listProjectPapers(projectId)
    currentProject.value = await getProject(projectId)
    runs.value = await listProjectRuns(projectId)
    return result
  }

  async function setPaperStatus(projectId: number, paperId: number, status: ProjectPaper['status']) {
    const updated = await updateProjectPaper(projectId, paperId, { status })
    papers.value = papers.value.map((paper) => paper.paper_id === paperId ? updated : paper)
  }

  async function removePaper(projectId: number, paperId: number) {
    await removeProjectPaper(projectId, paperId)
    papers.value = papers.value.filter((paper) => paper.paper_id !== paperId)
    currentProject.value = await getProject(projectId)
  }

  async function loadRuns(projectId: number) {
    runs.value = await listProjectRuns(projectId)
  }

  async function loadIngestionJobs(projectId: number) {
    ingestionJobs.value = await listProjectIngestionJobs(projectId)
  }

  async function uploadPdf(projectId: number, paperId: number, file: File) {
    const job = await uploadProjectPaperPdf(projectId, paperId, file)
    await refreshProjectArtifacts(projectId)
    return job
  }

  async function ingestFromPdfUrl(projectId: number, paperId: number, pdfUrl?: string) {
    const job = await ingestProjectPaperPdf(projectId, paperId, pdfUrl)
    await refreshProjectArtifacts(projectId)
    return job
  }

  async function saveReport(projectId: number, payload: { title: string; content: string; source?: string }) {
    const report = await createReport(projectId, payload)
    reports.value = [report, ...reports.value]
    currentProject.value = await getProject(projectId)
    runs.value = await listProjectRuns(projectId)
    return report
  }

  async function startWorkflow(projectId: number, question: string) {
    const run = await startResearchExpandWorkflow(projectId, question)
    await Promise.all([
      loadRuns(projectId),
      listReports(projectId).then((items) => { reports.value = items }),
      listProjectPapers(projectId).then((items) => { papers.value = items }),
      listProjectIngestionJobs(projectId).then((items) => { ingestionJobs.value = items }),
    ])
    return run
  }

  async function refreshProjectArtifacts(projectId: number) {
    const [projectPapers, jobs, projectRuns] = await Promise.all([
      listProjectPapers(projectId),
      listProjectIngestionJobs(projectId),
      listProjectRuns(projectId),
    ])
    papers.value = projectPapers
    ingestionJobs.value = jobs
    runs.value = projectRuns
  }

  return {
    projects,
    currentProject,
    papers,
    reports,
    runs,
    ingestionJobs,
    loading,
    error,
    loadProjects,
    addProject,
    seedDemo,
    loadProject,
    searchAdd,
    setPaperStatus,
    removePaper,
    loadRuns,
    loadIngestionJobs,
    uploadPdf,
    ingestFromPdfUrl,
    saveReport,
    startWorkflow,
    refreshProjectArtifacts,
  }
})
