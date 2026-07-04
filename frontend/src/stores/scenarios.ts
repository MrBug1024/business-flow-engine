import { defineStore } from 'pinia'
import { http } from '@/api/http'
import type { Scenario } from '@/api/types'

interface State {
  list: Scenario[]
  currentId: string | null
  current: Scenario | null
}

export const useScenarioStore = defineStore('scenarios', {
  state: (): State => ({ list: [], currentId: null, current: null }),
  actions: {
    async loadList() {
      const { data } = await http.get('/scenarios')
      this.list = data
      return data
    },
    async select(id: string) {
      this.currentId = id
      const { data } = await http.get(`/scenarios/${id}`)
      this.current = data
      return data
    },
    async refreshCurrent() {
      if (!this.currentId) return
      const { data } = await http.get(`/scenarios/${this.currentId}`)
      this.current = data
      return data
    },
    async create(name: string, description = '') {
      const { data } = await http.post('/scenarios', { name, description })
      await this.loadList()
      return data as Scenario
    },
    async remove(id: string) {
      await http.delete(`/scenarios/${id}`)
      if (this.currentId === id) {
        this.currentId = null
        this.current = null
      }
      await this.loadList()
    },
  },
})
