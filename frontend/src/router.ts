import { createRouter, createWebHistory } from 'vue-router'
import { h } from 'vue'
import DashboardView from './views/DashboardView.vue'
import ProjectWorkspaceView from './views/ProjectWorkspaceView.vue'
import ResearchView from './views/ResearchView.vue'
import SearchView from './views/SearchView.vue'

const NotFound = () =>
  h('section', { class: 'shell-panel', style: 'padding:48px;text-align:center' }, [
    h('h1', { style: 'font-family:var(--serif);font-size:32px;margin:0 0 8px' }, '页面不存在'),
    h('p', { style: 'color:var(--text-muted);margin:0 0 16px' }, '你访问的路径没有对应的工作区。'),
    h(
      'a',
      { href: '/', style: 'color:var(--accent-strong);font-weight:650' },
      '返回首页',
    ),
  ])

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: DashboardView },
    { path: '/research', name: 'research-new', component: SearchView },
    { path: '/projects/:id', name: 'project', component: ProjectWorkspaceView, props: true },
    { path: '/research/:id', name: 'research', component: ResearchView, props: true },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: NotFound },
  ],
})

export default router
