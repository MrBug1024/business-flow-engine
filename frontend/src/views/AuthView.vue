<template>
  <main class="auth-shell" :class="`theme-${themeMode}`">
    <header class="auth-titlebar">
      <span class="window-mark" aria-hidden="true"><i /><i /><i /></span>
      <strong>AI Business Studio</strong>
      <button class="language-toggle" type="button" @click="toggleLanguage">
        {{ language === 'zh' ? 'EN' : '中文' }}
      </button>
    </header>

    <section class="auth-workbench" aria-labelledby="auth-title">
      <div class="auth-panel">
        <div class="auth-brand" aria-hidden="true">
          <el-icon><MagicStick /></el-icon>
        </div>
        <h1 id="auth-title">{{ mode === 'login' ? text.loginTitle : text.registerTitle }}</h1>

        <div class="auth-mode" role="tablist" :aria-label="text.modeLabel">
          <button
            type="button"
            role="tab"
            :aria-selected="mode === 'login'"
            :class="{ active: mode === 'login' }"
            @click="switchMode('login')"
          >{{ text.login }}</button>
          <button
            type="button"
            role="tab"
            :aria-selected="mode === 'register'"
            :class="{ active: mode === 'register' }"
            @click="switchMode('register')"
          >{{ text.register }}</button>
        </div>

        <form novalidate @submit.prevent="submit">
          <label for="auth-email">{{ text.email }}</label>
          <el-input
            id="auth-email"
            v-model.trim="form.email"
            type="email"
            autocomplete="email"
            size="large"
            :placeholder="text.emailPlaceholder"
            :prefix-icon="Message"
            :disabled="busy"
          />

          <template v-if="mode === 'register'">
            <label for="auth-code">{{ text.code }}</label>
            <div class="verification-row">
              <el-input
                id="auth-code"
                v-model.trim="form.code"
                inputmode="numeric"
                autocomplete="one-time-code"
                maxlength="6"
                size="large"
                :placeholder="text.codePlaceholder"
                :prefix-icon="Key"
                :disabled="busy"
              />
              <el-button
                class="code-button"
                size="large"
                :loading="sendingCode"
                :disabled="busy || countdown > 0 || !isEmailValid"
                @click="sendCode"
              >{{ countdown > 0 ? `${countdown}s` : text.sendCode }}</el-button>
            </div>
          </template>

          <label for="auth-password">{{ text.password }}</label>
          <el-input
            id="auth-password"
            v-model="form.password"
            type="password"
            :autocomplete="mode === 'login' ? 'current-password' : 'new-password'"
            show-password
            size="large"
            :placeholder="text.passwordPlaceholder"
            :prefix-icon="Lock"
            :disabled="busy"
          />

          <template v-if="mode === 'register'">
            <label for="auth-confirm-password">{{ text.confirmPassword }}</label>
            <el-input
              id="auth-confirm-password"
              v-model="form.confirmPassword"
              type="password"
              autocomplete="new-password"
              show-password
              size="large"
              :placeholder="text.confirmPasswordPlaceholder"
              :prefix-icon="Lock"
              :disabled="busy"
            />
          </template>

          <p v-if="error" class="auth-error" role="alert">
            <el-icon><WarningFilled /></el-icon>
            <span>{{ error }}</span>
          </p>
          <p v-else-if="notice" class="auth-notice" aria-live="polite">
            <el-icon><CircleCheck /></el-icon>
            <span>{{ notice }}</span>
          </p>

          <el-button
            class="submit-button"
            type="primary"
            native-type="submit"
            size="large"
            :loading="busy"
          >{{ mode === 'login' ? text.loginAction : text.registerAction }}</el-button>
        </form>
      </div>
    </section>

    <footer class="auth-statusbar">
      <span>{{ text.secureSession }}</span>
      <span>ready</span>
    </footer>
  </main>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { CircleCheck, Key, Lock, MagicStick, Message, WarningFilled } from '@element-plus/icons-vue'
import { useAuth } from '@/composables/useAuth'
import type { Language } from '@/types/studio'

type Mode = 'login' | 'register'

const route = useRoute()
const router = useRouter()
const auth = useAuth()
const storedLanguage = localStorage.getItem('studio.language')
const storedTheme = localStorage.getItem('studio.theme')
const language = ref<Language>(storedLanguage === 'en' ? 'en' : 'zh')
const themeMode = storedTheme === 'light' || storedTheme === 'contrast' ? storedTheme : 'dark'
const mode = ref<Mode>(route.query.mode === 'register' ? 'register' : 'login')
const form = reactive({ email: '', password: '', confirmPassword: '', code: '' })
const busy = ref(false)
const sendingCode = ref(false)
const countdown = ref(0)
const error = ref('')
const notice = ref('')
let countdownTimer: number | undefined

const copy = {
  zh: {
    loginTitle: '登录工作台', registerTitle: '创建账户', modeLabel: '账户操作', login: '登录', register: '注册',
    email: '邮箱', emailPlaceholder: 'name@example.com', code: '邮箱验证码', codePlaceholder: '6 位验证码',
    sendCode: '发送验证码', password: '密码', passwordPlaceholder: '至少 8 个字符', confirmPassword: '确认密码',
    confirmPasswordPlaceholder: '再次输入密码', loginAction: '登录', registerAction: '注册并进入 Studio',
    passwordMismatch: '两次输入的密码不一致。', incomplete: '请完整填写账户信息。', invalidEmail: '请输入有效的邮箱地址。',
    codeSent: '验证码已发送，请查看邮箱。', requestFailed: '请求失败，请稍后重试。', secureSession: '安全会话',
  },
  en: {
    loginTitle: 'Sign in to Studio', registerTitle: 'Create account', modeLabel: 'Account action', login: 'Sign in', register: 'Register',
    email: 'Email', emailPlaceholder: 'name@example.com', code: 'Email verification code', codePlaceholder: '6-digit code',
    sendCode: 'Send code', password: 'Password', passwordPlaceholder: 'At least 8 characters', confirmPassword: 'Confirm password',
    confirmPasswordPlaceholder: 'Enter password again', loginAction: 'Sign in', registerAction: 'Register and enter Studio',
    passwordMismatch: 'The passwords do not match.', incomplete: 'Complete all account fields.', invalidEmail: 'Enter a valid email address.',
    codeSent: 'Verification code sent. Check your inbox.', requestFailed: 'Request failed. Try again later.', secureSession: 'Secure session',
  },
}

const text = computed(() => copy[language.value])
const isEmailValid = computed(() => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email.trim()))

function toggleLanguage() {
  language.value = language.value === 'zh' ? 'en' : 'zh'
  localStorage.setItem('studio.language', language.value)
}

function switchMode(next: Mode) {
  mode.value = next
  error.value = ''
  notice.value = ''
  form.password = ''
  form.confirmPassword = ''
  form.code = ''
}

async function sendCode() {
  error.value = ''
  notice.value = ''
  if (!isEmailValid.value) {
    error.value = text.value.invalidEmail
    return
  }
  sendingCode.value = true
  try {
    const response = await auth.sendRegistrationCode(form.email)
    notice.value = text.value.codeSent
    startCountdown(response.retry_after)
  } catch (requestError: any) {
    error.value = errorDetail(requestError)
  } finally {
    sendingCode.value = false
  }
}

async function submit() {
  error.value = ''
  notice.value = ''
  if (!isEmailValid.value) {
    error.value = text.value.invalidEmail
    return
  }
  if (!form.password || (mode.value === 'register' && !form.code)) {
    error.value = text.value.incomplete
    return
  }
  if (mode.value === 'register' && form.password !== form.confirmPassword) {
    error.value = text.value.passwordMismatch
    return
  }
  busy.value = true
  try {
    if (mode.value === 'login') await auth.login(form.email, form.password)
    else await auth.register(form.email, form.password, form.code)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/studio'
    await router.replace(redirect.startsWith('/') ? redirect : '/studio')
  } catch (requestError: any) {
    error.value = errorDetail(requestError)
  } finally {
    busy.value = false
  }
}

function startCountdown(seconds: number) {
  window.clearInterval(countdownTimer)
  countdown.value = Math.max(1, Math.ceil(seconds))
  countdownTimer = window.setInterval(() => {
    countdown.value = Math.max(0, countdown.value - 1)
    if (countdown.value === 0) window.clearInterval(countdownTimer)
  }, 1000)
}

function errorDetail(requestError: any) {
  return requestError?.response?.data?.detail || requestError?.message || text.value.requestFailed
}

onBeforeUnmount(() => window.clearInterval(countdownTimer))
</script>

<style scoped lang="scss">
.auth-shell {
  --auth-bg: #181a1f;
  --auth-surface: #202329;
  --auth-surface-strong: #262a31;
  --auth-border: #353a43;
  --auth-text: #f1f3f5;
  --auth-muted: #9ba3af;
  --auth-accent: #4da3f7;
  display: grid;
  min-width: 320px;
  min-height: 100dvh;
  grid-template-rows: 34px minmax(0, 1fr) 24px;
  background: var(--auth-bg);
  color: var(--auth-text);
  font-family: Inter, "Segoe UI", "Microsoft YaHei", sans-serif;
}

.auth-shell.theme-light {
  --auth-bg: #f5f6f8;
  --auth-surface: #ffffff;
  --auth-surface-strong: #f0f2f5;
  --auth-border: #d8dde5;
  --auth-text: #20242b;
  --auth-muted: #667085;
  --auth-accent: #1976d2;
}

.auth-titlebar,
.auth-statusbar {
  display: flex;
  align-items: center;
  border-color: var(--auth-border);
  background: color-mix(in srgb, var(--auth-bg) 92%, black);
}

.auth-titlebar {
  gap: 10px;
  padding: 0 12px;
  border-bottom: 1px solid var(--auth-border);
  font-size: 12px;
}

.window-mark { display: inline-flex; gap: 5px; }
.window-mark i { width: 8px; height: 8px; border-radius: 50%; background: var(--auth-muted); }
.window-mark i:first-child { background: #ef6a62; }
.window-mark i:nth-child(2) { background: #e8b54c; }
.window-mark i:nth-child(3) { background: #54b77b; }

.language-toggle {
  min-width: 44px;
  height: 28px;
  margin-left: auto;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--auth-muted);
  cursor: pointer;
}

.language-toggle:hover,
.language-toggle:focus-visible {
  background: var(--auth-surface-strong);
  color: var(--auth-text);
  outline: 2px solid var(--auth-accent);
  outline-offset: -2px;
}

.auth-workbench {
  display: grid;
  min-height: 0;
  place-items: center;
  padding: 32px 20px;
}

.auth-panel {
  width: min(100%, 420px);
  padding: 28px;
  border: 1px solid var(--auth-border);
  border-radius: 6px;
  background: var(--auth-surface);
  box-shadow: 0 16px 44px rgb(0 0 0 / 22%);
}

.auth-brand {
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  border: 1px solid color-mix(in srgb, var(--auth-accent) 45%, transparent);
  border-radius: 6px;
  background: color-mix(in srgb, var(--auth-accent) 12%, transparent);
  color: var(--auth-accent);
  font-size: 18px;
}

h1 { margin: 18px 0 20px; font-size: 22px; font-weight: 650; letter-spacing: 0; }

.auth-mode {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px;
  padding: 2px;
  border: 1px solid var(--auth-border);
  border-radius: 5px;
  background: var(--auth-bg);
}

.auth-mode button {
  min-height: 36px;
  border: 0;
  border-radius: 3px;
  background: transparent;
  color: var(--auth-muted);
  cursor: pointer;
  font: inherit;
}

.auth-mode button.active { background: var(--auth-surface-strong); color: var(--auth-text); }
.auth-mode button:focus-visible { outline: 2px solid var(--auth-accent); outline-offset: -2px; }
form { display: grid; gap: 9px; margin-top: 22px; }
label { margin-top: 6px; color: var(--auth-muted); font-size: 12px; font-weight: 600; }
.verification-row { display: grid; grid-template-columns: minmax(0, 1fr) 112px; gap: 8px; }
.code-button, .submit-button { min-height: 40px; }
.submit-button { width: 100%; margin-top: 10px; }

.auth-error,
.auth-notice {
  display: flex;
  min-height: 36px;
  align-items: flex-start;
  gap: 7px;
  margin: 7px 0 0;
  padding: 8px 10px;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1.5;
}

.auth-error { background: color-mix(in srgb, var(--el-color-danger) 13%, transparent); color: var(--el-color-danger); }
.auth-notice { background: color-mix(in srgb, var(--el-color-success) 12%, transparent); color: var(--el-color-success); }

.auth-statusbar {
  justify-content: space-between;
  padding: 0 10px;
  border-top: 1px solid var(--auth-border);
  color: var(--auth-muted);
  font-size: 11px;
}

@media (max-width: 520px) {
  .auth-workbench { padding: 16px; }
  .auth-panel { padding: 22px 18px; box-shadow: none; }
  .verification-row { grid-template-columns: minmax(0, 1fr) 104px; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { transition-duration: 0.01ms !important; }
}
</style>
