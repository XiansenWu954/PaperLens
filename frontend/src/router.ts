import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from './views/DashboardView.vue'
import ProjectWorkspaceView from './views/ProjectWorkspaceView.vue'
import ResearchView from './views/ResearchView.vue'
import SearchView from './views/SearchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: DashboardView },
    { path: '/research', name: 'research-new', component: SearchView },
    { path: '/projects/:id', name: 'project', component: ProjectWorkspaceView, props: true },
    { path: '/research/:id', name: 'research', component: ResearchView, props: true },
  ],
})

export default router
