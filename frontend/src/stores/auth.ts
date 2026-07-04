import { defineStore } from 'pinia'
import { http, getToken, setToken, clearToken } from '@/api/http'
import type { User } from '@/api/types'

interface State {
  token: string | null
  user: User | null
  providers: Record<string, boolean>
}

export const useAuthStore = defineStore('auth', {
  state: (): State => ({
    token: getToken(),
    user: null,
    providers: { password: true },
  }),
  getters: {
    isAuthed: (s) => !!s.token,
  },
  actions: {
    async loadProviders() {
      const { data } = await http.get('/auth/providers')
      this.providers = data
    },
    async register(email: string, password: string, name: string) {
      const { data } = await http.post('/auth/register', { email, password, name })
      this._setSession(data.token, data.user)
    },
    async login(email: string, password: string) {
      const { data } = await http.post('/auth/login', { email, password })
      this._setSession(data.token, data.user)
    },
    async fetchMe() {
      const { data } = await http.get('/auth/me')
      this.user = data
      return data
    },
    _setSession(token: string, user: User) {
      this.token = token
      this.user = user
      setToken(token)
    },
    setTokenFromOAuth(token: string) {
      this.token = token
      setToken(token)
    },
    logout() {
      this.token = null
      this.user = null
      clearToken()
    },
  },
})
