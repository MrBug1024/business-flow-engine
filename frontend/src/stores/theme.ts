import { defineStore } from 'pinia'
import { ref } from 'vue'

export type Theme = 'light' | 'dark'
const KEY = 'bfe-theme'

function systemPref(): Theme {
  return window.matchMedia?.('(prefers-color-scheme: light)').matches ? 'light' : 'dark'
}

function apply(theme: Theme) {
  const el = document.documentElement
  el.setAttribute('data-theme', theme)
  el.classList.toggle('dark', theme === 'dark')
}

export const useThemeStore = defineStore('theme', () => {
  const stored = localStorage.getItem(KEY) as Theme | null
  const theme = ref<Theme>(stored || systemPref())
  apply(theme.value)

  function set(t: Theme) {
    theme.value = t
    localStorage.setItem(KEY, t)
    apply(t)
  }
  function toggle() {
    set(theme.value === 'dark' ? 'light' : 'dark')
  }

  return { theme, set, toggle }
})
