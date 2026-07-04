import { defineStore } from 'pinia'
import { http } from '@/api/http'
import type { CapabilityItem } from '@/api/types'

interface State {
  items: CapabilityItem[]
  mounted: string[]
}

export const useSandboxStore = defineStore('sandbox', {
  state: (): State => ({ items: [], mounted: [] }),
  getters: {
    mountedItems: (s) => s.items.filter((i) => i.mounted),
    marketItems: (s) => s.items.filter((i) => !i.mounted),
  },
  actions: {
    async loadCatalog() {
      const { data } = await http.get('/playground/catalog')
      this.items = data.items
      this.mounted = data.mounted
    },
    async mount(sid: string) {
      await http.post(`/playground/mounts/${sid}`)
      await this.loadCatalog()
    },
    async unmount(sid: string) {
      await http.delete(`/playground/mounts/${sid}`)
      await this.loadCatalog()
    },
    async config(sid: string) {
      const { data } = await http.get(`/playground/mounts/${sid}/config`)
      return data
    },
  },
})
