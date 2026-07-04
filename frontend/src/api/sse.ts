import { fetchEventSource } from '@microsoft/fetch-event-source'
import { getToken, clearToken } from './http'

export interface SSEEvent {
  type?: string
  delta?: string
  name?: string
  args?: string
  result?: string
  elapsed?: number
  message?: string
  status?: string
  resource?: string
  interaction?: any
  steps?: any[]
  total_elapsed?: number
  [k: string]: any
}

/**
 * 以 POST + JWT 头发起 SSE 流式请求（原生 EventSource 不支持 POST/Header，故用 fetch-event-source）。
 * onEvent 收到每一帧解析后的 JSON；done/error 生命周期回调。返回一个可调用的中止函数。
 */
export function streamSSE(
  path: string,
  body: any,
  onEvent: (ev: SSEEvent) => void,
  opts: { onDone?: () => void; onError?: (e: any) => void } = {},
): () => void {
  const ctrl = new AbortController()
  const token = getToken()

  fetchEventSource(`/api${path}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
    signal: ctrl.signal,
    openWhenHidden: true,
    onmessage(msg) {
      const raw = (msg.data || '').trim()
      if (!raw || raw === '[DONE]') return
      try {
        onEvent(JSON.parse(raw) as SSEEvent)
      } catch {
        /* 忽略非 JSON 帧 */
      }
    },
    onclose() {
      opts.onDone?.()
    },
    onerror(err) {
      // fetch-event-source 默认会重试；这里直接抛出以停止
      if ((err as any)?.status === 401) clearToken()
      opts.onError?.(err)
      throw err
    },
  }).catch((e) => {
    if (e?.name !== 'AbortError') opts.onError?.(e)
  })

  return () => ctrl.abort()
}
