import axios from 'axios'
import { ElMessage } from 'element-plus'

const TOKEN_KEY = 'bfe_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}
export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token)
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
}

export const http = axios.create({ baseURL: '/api', timeout: 600000 })

http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (res) => res,
  (err) => {
    const status = err?.response?.status
    if (status === 401) {
      clearToken()
      // 避免在登录页重复跳转
      if (!location.hash.includes('/login')) {
        location.hash = '#/login'
      }
    } else {
      const detail = err?.response?.data?.detail || err?.message || '请求失败'
      if (status !== 404) ElMessage.error(String(detail))
    }
    return Promise.reject(err)
  },
)
