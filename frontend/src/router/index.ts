import { createRouter, createWebHashHistory } from 'vue-router'

const routes = [
  { path: '/', redirect: '/studio' },
  { path: '/studio', name: 'studio', component: () => import('@/views/StudioView.vue') },
  { path: '/:pathMatch(.*)*', redirect: '/studio' },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

export default router
