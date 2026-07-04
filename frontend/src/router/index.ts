import { createRouter, createWebHashHistory } from 'vue-router'
import { getToken } from '@/api/http'

const routes = [
  { path: '/', redirect: '/distill' },
  { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { public: true } },
  { path: '/oauth/callback', name: 'oauth', component: () => import('@/views/OAuthCallbackView.vue'), meta: { public: true } },
  { path: '/distill', name: 'distill', component: () => import('@/views/DistillView.vue') },
  { path: '/sandbox', name: 'sandbox', component: () => import('@/views/SandboxView.vue') },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach((to) => {
  if (to.meta.public) return true
  if (!getToken()) return { name: 'login', query: { redirect: to.fullPath } }
  return true
})

export default router
