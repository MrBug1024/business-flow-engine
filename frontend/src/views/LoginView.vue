<template>
  <div class="login-wrap">
    <button class="theme-toggle" :title="isDark ? '切换到浅色' : '切换到深色'" @click="theme.toggle()">
      <el-icon :size="18"><Moon v-if="isDark" /><Sunny v-else /></el-icon>
    </button>

    <div class="login-card">
      <div class="brand">
        <BrandMark :size="52" />
        <div class="brand-title">业务流逆向工程引擎</div>
        <div class="brand-sub mono">Business Flow Reverse-Engineering Engine</div>
      </div>

      <el-segmented v-model="mode" :options="modeOptions" block class="mode-seg" />

      <el-form :model="form" @submit.prevent class="form">
        <el-form-item>
          <el-input v-model="form.email" placeholder="邮箱" size="large" :prefix-icon="Message" />
        </el-form-item>
        <el-form-item v-if="mode === 'register'">
          <el-input v-model="form.name" placeholder="昵称（可选）" size="large" :prefix-icon="User" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.password" type="password" show-password placeholder="密码（至少 6 位）"
            size="large" :prefix-icon="Lock" @keyup.enter="submit" />
        </el-form-item>
        <el-button type="primary" size="large" class="submit-btn" :loading="loading" @click="submit">
          {{ mode === 'login' ? '登录' : '注册并登录' }}
        </el-button>
      </el-form>

      <div v-if="hasOAuth" class="oauth">
        <div class="divider"><span>或使用第三方账号</span></div>
        <div class="oauth-btns">
          <el-button v-if="auth.providers.google" size="large" class="oauth-btn" @click="oauth('google')">
            <span class="g">G</span> Google 登录
          </el-button>
          <el-button v-if="auth.providers.github" size="large" class="oauth-btn" @click="oauth('github')">
            <el-icon><Platform /></el-icon> GitHub 登录
          </el-button>
        </div>
      </div>

      <div class="tip">系统数据（用户）本地自管理于 <code>system/</code>，不依赖外部数据库。</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref, computed, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { Message, Lock, User, Platform, Moon, Sunny } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { useThemeStore } from '@/stores/theme'
import BrandMark from '@/components/BrandMark.vue'

const auth = useAuthStore()
const theme = useThemeStore()
const router = useRouter()
const route = useRoute()
const isDark = computed(() => theme.theme === 'dark')

const mode = ref<'login' | 'register'>('login')
const modeOptions = [
  { label: '登录', value: 'login' },
  { label: '注册', value: 'register' },
]
const form = reactive({ email: '', password: '', name: '' })
const loading = ref(false)

const hasOAuth = computed(() => auth.providers.google || auth.providers.github)

onMounted(() => {
  auth.loadProviders().catch(() => {})
  if (route.query.error) ElMessage.error('第三方登录失败：' + route.query.error)
})

async function submit() {
  if (!form.email || !form.password) return ElMessage.warning('请填写邮箱和密码')
  loading.value = true
  try {
    if (mode.value === 'login') await auth.login(form.email, form.password)
    else await auth.register(form.email, form.password, form.name)
    const redirect = (route.query.redirect as string) || '/distill'
    router.replace(redirect)
  } catch {
    /* 错误已由拦截器提示 */
  } finally {
    loading.value = false
  }
}

function oauth(provider: string) {
  window.location.href = `/api/auth/oauth/${provider}/login`
}
</script>

<style scoped lang="scss">
.login-wrap {
  height: 100%; display: flex; align-items: center; justify-content: center;
  background: var(--bg-app); background-image: var(--bg-app-grad);
  position: relative;
}
.theme-toggle {
  position: absolute; top: 20px; right: 20px;
  display: inline-flex; align-items: center; justify-content: center;
  width: 38px; height: 38px; border-radius: var(--r-sm);
  border: 1px solid var(--border); background: var(--surface); color: var(--text-2);
  cursor: pointer; transition: all var(--dur) var(--ease); box-shadow: var(--shadow-sm);
}
.theme-toggle:hover { color: var(--brand); border-color: var(--brand); }

.login-card {
  width: 400px; padding: 38px 34px;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r-xl); box-shadow: var(--shadow-lg);
}
.brand { display: flex; flex-direction: column; align-items: center; text-align: center; margin-bottom: 26px; }
.brand-title { font-size: var(--text-xl); font-weight: 800; letter-spacing: 0.01em; margin-top: 16px; }
.brand-sub { font-size: 11px; color: var(--text-3); margin-top: 6px; }
.mode-seg { margin-bottom: 20px; }
.form { margin-top: 6px; }
.submit-btn { width: 100%; margin-top: 4px; }
.oauth { margin-top: 20px; }
.divider { display: flex; align-items: center; gap: 12px; color: var(--text-3); font-size: var(--text-sm); margin: 8px 0 14px; }
.divider::before, .divider::after { content: ''; flex: 1; height: 1px; background: var(--border); }
.oauth-btns { display: flex; flex-direction: column; gap: 9px; }
.oauth-btn { width: 100%; }
.oauth-btn .g { font-weight: 800; color: #ea4335; margin-right: 6px; }
.tip { margin-top: 22px; font-size: 11px; text-align: center; color: var(--text-3); line-height: 1.6; }
code { font-family: var(--font-mono); color: var(--brand); background: var(--brand-soft); padding: 1px 5px; border-radius: 4px; }
</style>
