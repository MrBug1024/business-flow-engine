<template>
  <router-view />
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
onMounted(() => {
  // 有 token 则尝试恢复登录态
  if (auth.token) auth.fetchMe().catch(() => {})
})
</script>
