<template>
  <div class="shell">
    <header class="topbar">
      <div class="brand">
        <!-- <BrandMark :size="30" /> -->
         <img src="/logo.png" alt="Brand Logo" width="40" height="40" />
        <div class="brand-text">
          <span class="brand-name">零号.奇点工坊</span>
          <span class="brand-tag">Zero Singularity Workshop</span>
        </div>
      </div>

      <nav class="nav">
        <router-link to="/distill" class="nav-item" active-class="active">
          <el-icon><Cpu /></el-icon><span>蒸馏工作台</span>
        </router-link>
        <router-link to="/sandbox" class="nav-item" active-class="active">
          <el-icon><Box /></el-icon><span>Agent 平台</span>
        </router-link>
      </nav>

      <div class="spacer" />

      <button class="icon-btn" :title="isDark ? '切换到浅色' : '切换到深色'" @click="theme.toggle()">
        <el-icon :size="17"><Moon v-if="isDark" /><Sunny v-else /></el-icon>
      </button>
      <a class="icon-btn legacy" href="/legacy" target="_blank" title="旧版界面">
        <el-icon :size="16"><Back /></el-icon>
      </a>

      <div class="divider" />

      <el-dropdown trigger="click" @command="onCommand">
        <span class="user">
          <el-avatar :size="28" :src="auth.user?.avatar || undefined" class="user-avatar">
            {{ (auth.user?.name || 'U').charAt(0).toUpperCase() }}
          </el-avatar>
          <span class="user-name">{{ auth.user?.name || auth.user?.email || '用户' }}</span>
          <el-icon class="caret"><ArrowDown /></el-icon>
        </span>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item disabled>{{ auth.user?.email }}</el-dropdown-item>
            <el-dropdown-item divided command="logout">
              <el-icon><SwitchButton /></el-icon>退出登录
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>
    </header>

    <main class="content">
      <slot />
    </main>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { ArrowDown, Cpu, Box, Moon, Sunny, Back, SwitchButton } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useThemeStore } from '@/stores/theme'
import BrandMark from '@/components/BrandMark.vue'

const auth = useAuthStore()
const theme = useThemeStore()
const router = useRouter()
const isDark = computed(() => theme.theme === 'dark')

function onCommand(cmd: string) {
  if (cmd === 'logout') {
    auth.logout()
    router.replace('/login')
  }
}
</script>

<style scoped lang="scss">
.shell { height: 100%; display: flex; flex-direction: column; background: var(--bg-app); }

.topbar {
  height: 58px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 18px;
  padding: 0 18px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}

/* Brand ------------------------------------------------------------------- */
.brand { display: flex; align-items: center; gap: 11px; }
.brand-text { display: flex; flex-direction: column; line-height: 1.15; }
.brand-name { font-weight: 800; font-size: var(--text-md); letter-spacing: 0.01em; }
.brand-tag { font-size: 10px; color: var(--text-3); font-family: var(--font-mono); letter-spacing: 0.02em; }

/* Nav --------------------------------------------------------------------- */
.nav { display: flex; gap: 4px; margin-left: 6px; }
.nav-item {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 8px 14px;
  border-radius: var(--r-sm);
  font-size: var(--text-base);
  font-weight: 600;
  color: var(--text-2);
  transition: all var(--dur) var(--ease);
}
.nav-item .el-icon { font-size: 16px; }
.nav-item:hover { color: var(--text-1); background: var(--hover); }
.nav-item.active { color: var(--brand); background: var(--brand-soft); }

.spacer { flex: 1; }

/* Icon buttons ------------------------------------------------------------ */
.icon-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: var(--r-sm);
  color: var(--text-2);
  background: transparent;
  border: none;
  cursor: pointer;
  transition: all var(--dur) var(--ease);
}
.icon-btn:hover { color: var(--text-1); background: var(--hover); }

.divider { width: 1px; height: 24px; background: var(--border); margin: 0 4px; }

/* User -------------------------------------------------------------------- */
.user {
  display: flex;
  align-items: center;
  gap: 9px;
  cursor: pointer;
  padding: 4px 10px 4px 4px;
  border-radius: var(--r-full);
  color: var(--text-1);
  transition: background var(--dur) var(--ease);
}
.user:hover { background: var(--hover); }
.user-avatar { background: linear-gradient(140deg, var(--brand), var(--brand-strong)); color: #fff; font-weight: 700; }
.user-name { font-size: var(--text-base); font-weight: 600; max-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.caret { font-size: 13px; color: var(--text-3); }

.content { flex: 1; overflow: hidden; }

.el-dropdown-menu__item .el-icon { margin-right: 6px; }
</style>
