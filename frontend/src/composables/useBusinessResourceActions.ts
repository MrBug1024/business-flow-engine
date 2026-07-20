import type { Ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { http } from '@/api/http'
import type {
  BusinessResourceAction,
  BusinessResourceTarget,
  WorkspaceNode,
} from '@/types/studio'

type WorkspaceTab = { id: string; title: string; kind: string; payload?: any }

type ResourceActionOptions = {
  current: Ref<any | null>
  tabs: Ref<WorkspaceTab[]>
  activeTabId: Ref<string>
  businessTrees: Ref<Record<string, WorkspaceNode>>
  workspaceTree: Ref<WorkspaceNode | null>
  t: (key: string) => string
  loadBusinesses: () => Promise<void>
  loadBusinessTree: (businessId: string, force?: boolean) => Promise<WorkspaceNode | undefined>
  selectBusiness: (businessId: string) => Promise<void>
  openWorkspaceNode: (node: WorkspaceNode) => Promise<void> | void
  openWorkspaceFile: (node: Pick<WorkspaceNode, 'name' | 'path'>) => Promise<void>
  deleteBusiness: (businessId: string, name: string) => Promise<void>
}

export function useBusinessResourceActions(options: ResourceActionOptions) {
  const {
    current,
    tabs,
    activeTabId,
    businessTrees,
    workspaceTree,
    t,
    loadBusinesses,
    loadBusinessTree,
    selectBusiness,
    openWorkspaceNode,
    openWorkspaceFile,
    deleteBusiness,
  } = options

  async function refreshResourceExplorer() {
    await loadBusinesses()
    await Promise.all(
      Object.keys(businessTrees.value).map((businessId) => loadBusinessTree(businessId, true)),
    )
    if (current.value?.id) workspaceTree.value = businessTrees.value[current.value.id] || null
  }

  async function openBusinessResource(target: BusinessResourceTarget) {
    if (target.businessId !== current.value?.id) await selectBusiness(target.businessId)
    if (!target.root) await openWorkspaceNode(target.node)
  }

  async function handleBusinessResourceAction(
    action: BusinessResourceAction,
    target: BusinessResourceTarget,
  ) {
    if (action === 'open') return openBusinessResource(target)
    if (action === 'export') return exportBusinessResource(target)
    if (action === 'new-file' || action === 'new-folder') {
      return createBusinessResource(target, action === 'new-file' ? 'file' : 'folder')
    }
    if (action === 'rename') return renameBusinessResource(target)
    if (action === 'delete') return deleteBusinessResource(target)
  }

  async function createBusinessResource(
    target: BusinessResourceTarget,
    kind: 'file' | 'folder',
  ) {
    const name = await promptResourceName(kind === 'file' ? t('newFile') : t('newFolder'))
    if (!name) return
    const path = joinWorkspacePath(target.node.path, name)
    try {
      const response = await http.post(`/businesses/${target.businessId}/workspace/entry`, {
        path,
        kind,
        content: '',
      })
      await applyResourceMutation(target.businessId, response.data.business)
      if (kind === 'file' && target.businessId === current.value?.id) {
        await openWorkspaceFile({ name, path })
      }
      ElMessage.success(kind === 'file' ? t('fileCreated') : t('folderCreated'))
    } catch (error: any) {
      showResourceError(error)
    }
  }

  async function renameBusinessResource(target: BusinessResourceTarget) {
    const currentName = target.root ? target.businessName : target.node.name
    const name = await promptResourceName(t('rename'), currentName)
    if (!name || name === currentName) return
    try {
      if (target.root) {
        const response = await http.patch(`/businesses/${target.businessId}`, { name })
        if (current.value?.id === target.businessId) current.value = response.data
        await loadBusinesses()
        ElMessage.success(t('renamed'))
        return
      }
      const parent = workspaceParent(target.node.path)
      const destination = joinWorkspacePath(parent, name)
      const response = await http.patch(`/businesses/${target.businessId}/workspace/entry`, {
        path: target.node.path,
        destination,
      })
      if (target.businessId === current.value?.id) {
        remapWorkspaceTabs(target.node.path, destination)
      }
      await applyResourceMutation(target.businessId, response.data.business)
      ElMessage.success(t('renamed'))
    } catch (error: any) {
      showResourceError(error)
    }
  }

  async function deleteBusinessResource(target: BusinessResourceTarget) {
    if (target.root) return deleteBusiness(target.businessId, target.businessName)
    try {
      await ElMessageBox.confirm(
        target.node.kind === 'folder' ? t('deleteFolderConfirm') : t('deleteFileConfirmBody'),
        `${t('delete')} ${target.node.name}?`,
        {
          confirmButtonText: t('delete'),
          cancelButtonText: t('cancel'),
          type: 'warning',
        },
      )
    } catch {
      return
    }
    try {
      const response = await http.delete(`/businesses/${target.businessId}/workspace/entry`, {
        params: { path: target.node.path, recursive: target.node.kind === 'folder' },
      })
      if (target.businessId === current.value?.id) closeWorkspaceTabsUnder(target.node.path)
      await applyResourceMutation(target.businessId, response.data.business)
      ElMessage.success(t('deleted'))
    } catch (error: any) {
      showResourceError(error)
    }
  }

  async function importBusinessResourceFiles(
    files: File[],
    target: BusinessResourceTarget,
  ) {
    if (!files.length) return
    const form = new FormData()
    files.forEach((file) => form.append('files', file))
    form.append('target_path', target.root ? '' : target.node.path)
    try {
      const response = await http.post(
        `/businesses/${target.businessId}/workspace/import`,
        form,
      )
      await applyResourceMutation(target.businessId, response.data.business)
      ElMessage.success(`${response.data.imported.length} ${t('filesImported')}`)
    } catch (error: any) {
      showResourceError(error)
    }
  }

  function exportBusinessResource(target: BusinessResourceTarget) {
    const query = new URLSearchParams({ path: target.root ? '' : target.node.path })
    window.open(
      `/api/businesses/${target.businessId}/workspace/export?${query}`,
      '_blank',
      'noopener,noreferrer',
    )
  }

  async function applyResourceMutation(businessId: string, business?: any) {
    if (business && current.value?.id === businessId) current.value = business
    const tree = await loadBusinessTree(businessId, true)
    if (businessId === current.value?.id) workspaceTree.value = tree || null
    await loadBusinesses()
  }

  async function promptResourceName(title: string, initialValue = '') {
    try {
      const result = await ElMessageBox.prompt('', title, {
        inputValue: initialValue,
        inputPlaceholder: t('resourceName'),
        confirmButtonText: t('confirm'),
        cancelButtonText: t('cancel'),
        inputValidator: (value: string) => isValidResourceName(value) || t('invalidResourceName'),
      })
      return result.value.trim()
    } catch {
      return ''
    }
  }

  function remapWorkspaceTabs(source: string, destination: string) {
    let remappedActiveId = activeTabId.value
    tabs.value = tabs.value.map((tab) => {
      if (!isWorkspaceFileTab(tab) || (tab.id !== source && !tab.id.startsWith(`${source}/`))) {
        return tab
      }
      const nextId = `${destination}${tab.id.slice(source.length)}`
      if (activeTabId.value === tab.id) remappedActiveId = nextId
      return {
        ...tab,
        id: nextId,
        title: nextId.split('/').pop() || tab.title,
        payload: tab.payload ? { ...tab.payload, path: nextId } : tab.payload,
      }
    })
    activeTabId.value = remappedActiveId
  }

  function closeWorkspaceTabsUnder(path: string) {
    const removed = new Set(
      tabs.value
        .filter((tab) => (
          isWorkspaceFileTab(tab)
          && (tab.id === path || tab.id.startsWith(`${path}/`))
        ))
        .map((tab) => tab.id),
    )
    tabs.value = tabs.value.filter((tab) => !removed.has(tab.id))
    if (removed.has(activeTabId.value)) {
      activeTabId.value = tabs.value[tabs.value.length - 1]?.id || ''
    }
  }

  function showResourceError(error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || t('workspaceOperationFailed'))
  }

  return {
    refreshResourceExplorer,
    openBusinessResource,
    handleBusinessResourceAction,
    importBusinessResourceFiles,
  }
}

function isWorkspaceFileTab(tab: WorkspaceTab) {
  return ['description', 'context', 'file'].includes(tab.kind)
}

function isValidResourceName(value: string) {
  const normalized = value.trim()
  return Boolean(
    normalized
    && normalized !== '.'
    && normalized !== '..'
    && !/[\\/:*?"<>|\u0000-\u001f]/.test(normalized),
  )
}

function joinWorkspacePath(parent: string, name: string) {
  return [parent.replace(/\/$/, ''), name].filter(Boolean).join('/')
}

function workspaceParent(path: string) {
  const parts = path.split('/')
  parts.pop()
  return parts.join('/')
}
