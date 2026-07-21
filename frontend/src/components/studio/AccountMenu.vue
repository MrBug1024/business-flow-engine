<template>
  <el-dropdown trigger="click" placement="bottom-end" @command="handleCommand">
    <button class="account-trigger" :title="account?.email" :aria-label="text.accountMenu">
      <el-icon><User /></el-icon>
      <span>{{ account?.email }}</span>
      <el-icon class="account-chevron"><ArrowDown /></el-icon>
    </button>
    <template #dropdown>
      <el-dropdown-menu>
        <el-dropdown-item disabled class="account-summary-item">
          <div class="account-summary">
            <span>{{ text.signedInAs }}</span>
            <strong>{{ account?.email }}</strong>
          </div>
        </el-dropdown-item>
        <el-dropdown-item divided command="logout" :icon="SwitchButton">
          {{ text.logout }}
        </el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowDown, SwitchButton, User } from '@element-plus/icons-vue'
import { useAuth } from '@/composables/useAuth'
import type { Language } from '@/types/studio'

const props = defineProps<{ language: Language }>()
const router = useRouter()
const { account, logout } = useAuth()

const text = computed(() => props.language === 'zh'
  ? { accountMenu: '账户菜单', signedInAs: '当前账户', logout: '退出登录' }
  : { accountMenu: 'Account menu', signedInAs: 'Signed in as', logout: 'Sign out' })

async function handleCommand(command: string) {
  if (command !== 'logout') return
  await logout()
  await router.replace('/login')
}
</script>

<style scoped>
.account-trigger {
  display: inline-flex;
  width: min(250px, 26vw);
  height: 30px;
  align-items: center;
  gap: 7px;
  padding: 0 9px;
  border: 1px solid transparent;
  border-radius: 4px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  font: inherit;
}

.account-trigger:hover,
.account-trigger:focus-visible {
  border-color: var(--border);
  background: var(--surface-hover);
  color: var(--text-strong);
  outline: none;
}

.account-trigger span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.account-chevron {
  margin-left: auto;
  font-size: 11px;
}

.account-summary {
  display: grid;
  width: 240px;
  gap: 4px;
  padding: 8px 16px 10px;
}

.account-summary-item {
  height: auto;
  padding: 0;
  cursor: default;
}

.account-summary span {
  color: var(--el-text-color-secondary);
  font-size: 11px;
}

.account-summary strong {
  overflow: hidden;
  color: var(--el-text-color-primary);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
