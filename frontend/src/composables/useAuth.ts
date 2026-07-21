import { readonly, ref } from 'vue'
import { http } from '@/api/http'

export type Account = {
  id: string
  email: string
  created_at: number
  verified_at: number
}

const account = ref<Account | null>(null)
const initialized = ref(false)
let restorePromise: Promise<Account | null> | null = null

export function useAuth() {
  async function restore(force = false) {
    if (initialized.value && !force) return account.value
    if (restorePromise && !force) return restorePromise
    restorePromise = http.get<Account>('/auth/me', { headers: { 'X-Auth-Probe': 'true' } })
      .then((response) => {
        account.value = response.data
        return account.value
      })
      .catch(() => {
        account.value = null
        return null
      })
      .finally(() => {
        initialized.value = true
        restorePromise = null
      })
    return restorePromise
  }

  async function sendRegistrationCode(email: string) {
    return (await http.post<{ message: string; retry_after: number }>(
      '/auth/register/code',
      { email },
    )).data
  }

  async function register(email: string, password: string, code: string) {
    const response = await http.post<{ account: Account }>('/auth/register', {
      email,
      password,
      code,
    })
    account.value = response.data.account
    initialized.value = true
    return account.value
  }

  async function login(email: string, password: string) {
    const response = await http.post<{ account: Account }>('/auth/login', { email, password })
    account.value = response.data.account
    initialized.value = true
    return account.value
  }

  async function logout() {
    try {
      await http.post('/auth/logout')
    } finally {
      account.value = null
      initialized.value = true
    }
  }

  function clear() {
    account.value = null
    initialized.value = true
  }

  return {
    account: readonly(account),
    initialized: readonly(initialized),
    restore,
    sendRegistrationCode,
    register,
    login,
    logout,
    clear,
  }
}
