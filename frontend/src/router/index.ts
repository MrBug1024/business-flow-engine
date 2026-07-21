import { createRouter, createWebHashHistory } from 'vue-router'
import { useAuth } from '@/composables/useAuth'

const routes = [
  { path: '/', redirect: '/studio' },
  { path: '/login', name: 'login', component: () => import('@/views/AuthView.vue') },
  {
    path: '/studio',
    name: 'studio',
    component: () => import('@/views/StudioView.vue'),
    meta: { requiresAuth: true },
  },
  { path: '/:pathMatch(.*)*', redirect: '/studio' },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const account = await useAuth().restore()
  if (to.meta.requiresAuth && !account) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.path === '/login' && account) return '/studio'
  return true
})

export default router
