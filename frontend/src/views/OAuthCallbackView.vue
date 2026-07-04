<template>
  <div class="cb">
    <BrandMark :size="46" />
    <div class="cb-row">
      <el-icon class="spin" :size="18"><Loading /></el-icon>
      <span>正在完成登录…</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Loading } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import BrandMark from '@/components/BrandMark.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

onMounted(async () => {
  const token = route.query.token as string
  if (token) {
    auth.setTokenFromOAuth(token)
    try { await auth.fetchMe() } catch { /* ignore */ }
    router.replace('/distill')
  } else {
    router.replace('/login?error=oauth_no_token')
  }
})
</script>

<style scoped>
.cb {
  height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 18px;
  background: var(--bg-app); background-image: var(--bg-app-grad);
}
.cb-row { display: flex; align-items: center; gap: 10px; color: var(--text-2); font-size: var(--text-md); }
.spin { animation: spin 1s linear infinite; color: var(--brand); }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
