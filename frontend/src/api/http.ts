import axios from 'axios'
import { ElMessage } from 'element-plus'

export const http = axios.create({ baseURL: '/api', timeout: 600000 })

function safeErrorDetail(value: unknown): string {
  if (typeof value === 'string' && value.trim()) return value
  if (Array.isArray(value)) {
    const messages = value
      .map((item: any) => (typeof item === 'string' ? item : item?.msg || item?.message))
      .filter(Boolean)
    if (messages.length) return messages.join('\n')
  }
  if (value && typeof value === 'object') {
    const detail = value as any
    if (Array.isArray(detail.servers)) {
      const messages = detail.servers
        .map((server: any) => [server?.name, server?.error || server?.message || server?.status].filter(Boolean).join(': '))
        .filter(Boolean)
      if (messages.length) return messages.join('\n')
    }
    if (typeof detail.message === 'string' && detail.message.trim()) return detail.message
  }
  return '请求失败'
}

http.interceptors.response.use(
  (res) => res,
  (err) => {
    const detail = err?.response?.data?.detail || err?.response?.data?.message || err?.message
    if (err?.response?.status !== 404) ElMessage.error(safeErrorDetail(detail))
    return Promise.reject(err)
  },
)
