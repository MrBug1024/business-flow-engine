import { ref } from 'vue'
import { http } from '@/api/http'
import type { WorkspaceNode } from '@/types/studio'

export function useBusinessResourceTrees() {
  const trees = ref<Record<string, WorkspaceNode>>({})
  const loading = ref<Set<string>>(new Set())

  async function loadTree(businessId: string, force = false) {
    if (!force && trees.value[businessId]) return trees.value[businessId]
    if (loading.value.has(businessId)) return trees.value[businessId]
    loading.value = new Set(loading.value).add(businessId)
    try {
      const response = await http.get(`/businesses/${businessId}/workspace/tree`)
      trees.value = { ...trees.value, [businessId]: response.data }
      return response.data as WorkspaceNode
    } finally {
      const next = new Set(loading.value)
      next.delete(businessId)
      loading.value = next
    }
  }

  function setTree(businessId: string, tree: WorkspaceNode) {
    trees.value = { ...trees.value, [businessId]: tree }
  }

  function removeTree(businessId: string) {
    const next = { ...trees.value }
    delete next[businessId]
    trees.value = next
  }

  function retainTrees(businessIds: string[]) {
    const available = new Set(businessIds)
    trees.value = Object.fromEntries(
      Object.entries(trees.value).filter(([businessId]) => available.has(businessId)),
    )
  }

  return { trees, loading, loadTree, setTree, removeTree, retainTrees }
}
