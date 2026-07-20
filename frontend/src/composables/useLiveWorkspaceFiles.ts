import type { Ref } from 'vue'
import { http } from '@/api/http'

type WorkspaceTab = { id: string; title: string; kind: string; payload?: any }

type LiveFileDraft = {
  callId: string
  path: string
  operation: string
  text: string
  baseText: string
  oldText: string
  replacementText: string
  revealPromise?: Promise<void>
  cancelReveal?: () => void
}

type LiveWorkspaceOptions = {
  current: Ref<any | null>
  tabs: Ref<WorkspaceTab[]>
  activeTabId: Ref<string>
  t: (key: string) => string
  reloadWorkspaceTree: () => Promise<void>
  openWorkspaceFile: (node: { name: string; path: string }) => Promise<void>
  closeTab: (path: string) => void
}

export function useLiveWorkspaceFiles(options: LiveWorkspaceOptions) {
  const { current, tabs, activeTabId, t, reloadWorkspaceTree, openWorkspaceFile, closeTab } = options
  const drafts = new Map<string, LiveFileDraft>()
  const pendingSettlements = new Set<Promise<void>>()
  let reloadTimer: number | undefined

  function handleFileOperationEvent(event: any) {
    if (!current.value || !event?.mutating) return
    const operation = String(event.operation || 'manage')
    const path = workspaceRelativePath(event.path)
    const destination = workspaceRelativePath(event.destination)
    if (!path) return

    if (event.status === 'streaming' && ['create', 'edit'].includes(operation)) {
      applyStreamedFileDelta(event, path, operation)
      return
    }
    if (event.status === 'running' && ['create', 'edit'].includes(operation)) {
      applyRunningFileOperation(event, path, operation)
      return
    }
    if (event.status === 'failed') {
      const draft = drafts.get(path)
      if (draft && event.call_id && draft.callId && draft.callId !== String(event.call_id)) return
      draft?.cancelReveal?.()
      drafts.delete(path)
      const tab = tabs.value.find((item) => item.id === path)
      if (tab?.kind === 'file') {
        tab.payload = {
          ...tab.payload,
          loading: false,
          live_operation: '',
          error: event.error || t('noPreview'),
        }
      }
      return
    }
    if (event.status !== 'succeeded') return

    if (operation === 'delete') {
      drafts.delete(path)
      closeTab(path)
      trackSettlement(reloadWorkspaceTree())
      return
    }
    if (operation === 'move') {
      drafts.delete(path)
      closeTab(path)
      trackSettlement((async () => {
        await reloadWorkspaceTree()
        if (destination) {
          await openWorkspaceFile({
            name: destination.split('/').pop() || destination,
            path: destination,
          })
        }
      })())
      return
    }
    if (operation === 'create_directory') {
      trackSettlement(reloadWorkspaceTree())
      return
    }
    const draft = drafts.get(path)
    if (event.auto_open === false && !draft) {
      scheduleWorkspaceTreeReload()
      return
    }
    trackSettlement(finalizeLiveFile(path, String(event.call_id || '')))
  }

  function applyStreamedFileDelta(event: any, path: string, operation: string) {
    const callId = String(event.call_id || '')
    let draft = drafts.get(path)
    if (!draft || (callId && draft.callId && draft.callId !== callId)) {
      const existingText = String(tabs.value.find((tab) => tab.id === path)?.payload?.text || '')
      draft = createDraft(callId, path, operation, existingText)
      drafts.set(path, draft)
    }
    if (callId) draft.callId = callId
    if (operation === 'create') {
      if (event.content_reset) draft.text = ''
      draft.text += String(event.content_delta || '')
    } else {
      if (event.old_text) draft.oldText = String(event.old_text)
      if (event.content_reset) draft.replacementText = ''
      draft.replacementText += String(event.content_delta || '')
      updateEditedDraftText(draft)
      if (!draft.baseText) void hydrateLiveEditDraft(path, draft.callId)
    }
    renderLiveFileDraft(draft, 'streaming', true)
  }

  function applyRunningFileOperation(event: any, path: string, operation: string) {
    const callId = String(event.call_id || '')
    const existingText = String(tabs.value.find((tab) => tab.id === path)?.payload?.text || '')
    let draft = drafts.get(path)
    if (!draft || (callId && draft.callId && draft.callId !== callId)) {
      draft = createDraft(callId, path, operation, existingText)
      drafts.set(path, draft)
    }
    if (callId) draft.callId = callId
    const input = event.input || {}
    if (operation === 'create' && typeof input.content === 'string') {
      if (!draft.text && input.content.length > 240) startLiveContentReveal(draft, input.content)
      else draft.text = input.content
    } else if (operation === 'edit') {
      if (typeof input.old_string === 'string') draft.oldText = input.old_string
      if (typeof input.new_string === 'string') draft.replacementText = input.new_string
      updateEditedDraftText(draft)
      if (!draft.baseText) void hydrateLiveEditDraft(path, draft.callId)
    }
    renderLiveFileDraft(draft, 'streaming', true)
  }

  function startLiveContentReveal(draft: LiveFileDraft, targetText: string) {
    draft.cancelReveal?.()
    const callId = draft.callId
    const steps = Math.min(42, Math.max(18, Math.ceil(targetText.length / 180)))
    const chunkSize = Math.max(1, Math.ceil(targetText.length / steps))
    let cursor = 0
    let resolveReveal: () => void = () => undefined
    draft.text = ''
    draft.revealPromise = new Promise<void>((resolve) => { resolveReveal = resolve })
    const timer = window.setInterval(() => {
      const currentDraft = drafts.get(draft.path)
      if (currentDraft !== draft || (callId && currentDraft.callId !== callId)) {
        window.clearInterval(timer)
        resolveReveal()
        return
      }
      cursor = Math.min(targetText.length, cursor + chunkSize)
      draft.text = targetText.slice(0, cursor)
      renderLiveFileDraft(draft, 'streaming', false)
      if (cursor >= targetText.length) {
        window.clearInterval(timer)
        draft.cancelReveal = undefined
        resolveReveal()
      }
    }, 28)
    draft.cancelReveal = () => {
      window.clearInterval(timer)
      draft.text = targetText
      draft.cancelReveal = undefined
      resolveReveal()
    }
  }

  function updateEditedDraftText(draft: LiveFileDraft) {
    if (draft.baseText && draft.oldText && draft.baseText.includes(draft.oldText)) {
      draft.text = draft.baseText.replace(draft.oldText, draft.replacementText)
    } else if (draft.replacementText) {
      draft.text = draft.replacementText
    }
  }

  async function hydrateLiveEditDraft(path: string, callId: string) {
    if (!current.value) return
    try {
      const payload = await fetchWorkspacePreview(path)
      const draft = drafts.get(path)
      if (!draft || (callId && draft.callId !== callId)) return
      draft.baseText = String(payload.text || '')
      updateEditedDraftText(draft)
      renderLiveFileDraft(draft, 'streaming', false, payload)
    } catch {
      // Completion retries after the runtime has persisted the file.
    }
  }

  function renderLiveFileDraft(
    draft: LiveFileDraft,
    phase: 'streaming' | 'saving',
    activate: boolean,
    sourcePayload: any = {},
  ) {
    const title = draft.path.split('/').pop() || draft.path
    const existing = tabs.value.find((tab) => tab.id === draft.path)?.payload || {}
    setFileTab({
      id: draft.path,
      title,
      kind: 'file',
      payload: {
        ...existing,
        ...sourcePayload,
        path: draft.path,
        filename: title,
        kind: previewKindForPath(draft.path),
        text: draft.text,
        size: draft.text.length,
        loading: false,
        error: '',
        live_operation: draft.operation,
        live_phase: phase,
      },
    }, activate)
  }

  async function finalizeLiveFile(path: string, callId: string) {
    try {
      const activeDraft = drafts.get(path)
      if (activeDraft?.revealPromise) await activeDraft.revealPromise
      if (activeDraft) renderLiveFileDraft(activeDraft, 'saving', false)
      await reloadWorkspaceTree()
      const payload = await fetchWorkspacePreview(path)
      const draft = drafts.get(path)
      if (draft && callId && draft.callId && draft.callId !== callId) return
      drafts.delete(path)
      const title = path.split('/').pop() || path
      setFileTab({ id: path, title, kind: 'file', payload }, false)
    } catch (error: any) {
      const draft = drafts.get(path)
      if (draft && callId && draft.callId && draft.callId !== callId) return
      drafts.delete(path)
      const tab = tabs.value.find((item) => item.id === path)
      if (tab?.kind === 'file') {
        tab.payload = {
          ...tab.payload,
          loading: false,
          live_operation: '',
          live_phase: '',
          error: error?.response?.data?.detail || error?.message || t('noPreview'),
        }
      }
    }
  }

  async function settleLiveFileOperations() {
    if (pendingSettlements.size) await Promise.allSettled([...pendingSettlements])
    const remaining = [...drafts.values()]
    if (remaining.length) {
      await Promise.allSettled(
        remaining.map((draft) => finalizeLiveFile(draft.path, draft.callId)),
      )
    }
  }

  function renderExistingLiveDraft(path: string) {
    const draft = drafts.get(path)
    if (!draft) return false
    renderLiveFileDraft(draft, 'streaming', true)
    return true
  }

  function clearLiveFileDrafts() {
    drafts.forEach((draft) => draft.cancelReveal?.())
    drafts.clear()
    pendingSettlements.clear()
  }

  function disposeLiveFileDrafts() {
    if (reloadTimer != null) window.clearTimeout(reloadTimer)
    reloadTimer = undefined
    clearLiveFileDrafts()
  }

  function trackSettlement(task: Promise<unknown>) {
    const tracked = Promise.resolve(task).then(() => undefined, () => undefined)
    pendingSettlements.add(tracked)
    void tracked.finally(() => pendingSettlements.delete(tracked))
  }

  function scheduleWorkspaceTreeReload() {
    if (reloadTimer != null) window.clearTimeout(reloadTimer)
    reloadTimer = window.setTimeout(() => {
      reloadTimer = undefined
      void reloadWorkspaceTree()
    }, 120)
  }

  async function fetchWorkspacePreview(path: string) {
    if (!current.value) throw new Error(t('noPreview'))
    return (await http.get(`/businesses/${current.value.id}/workspace/preview`, {
      params: { path },
    })).data
  }

  function setFileTab(tab: WorkspaceTab, activate: boolean) {
    const index = tabs.value.findIndex((item) => item.id === tab.id)
    if (index >= 0) tabs.value[index] = tab
    else tabs.value.push(tab)
    if (activate) activeTabId.value = tab.id
  }

  function fileOperationLabel(operation: string) {
    const labels: Record<string, string> = {
      create: t('fileOperationCreate'),
      edit: t('fileOperationEdit'),
      move: t('fileOperationMove'),
      delete: t('fileOperationDelete'),
    }
    return labels[operation] || t('fileOperationManage')
  }

  return {
    handleFileOperationEvent,
    settleLiveFileOperations,
    renderExistingLiveDraft,
    clearLiveFileDrafts,
    disposeLiveFileDrafts,
    fileOperationLabel,
  }
}

function createDraft(callId: string, path: string, operation: string, existingText: string) {
  return {
    callId,
    path,
    operation,
    text: operation === 'edit' ? existingText : '',
    baseText: existingText,
    oldText: '',
    replacementText: '',
  }
}

function previewKindForPath(path: string) {
  const suffix = path.split('.').pop()?.toLowerCase()
  if (suffix === 'json') return 'json'
  if (suffix === 'md') return 'markdown'
  if (suffix === 'mmd' || suffix === 'mermaid') return 'mermaid'
  return 'text'
}

function workspaceRelativePath(value: unknown) {
  const normalized = String(value || '').trim().replace(/\\/g, '/')
  if (
    !normalized
    || normalized === '/workspace'
    || normalized.startsWith('/skills')
    || normalized.startsWith('/tmp')
  ) return ''
  return normalized.replace(/^\/workspace\/?/, '').replace(/^\/+/, '')
}
