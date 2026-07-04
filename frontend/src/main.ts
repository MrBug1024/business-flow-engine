import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'splitpanes/dist/splitpanes.css'

import App from './App.vue'
import router from './router'
import { useThemeStore } from './stores/theme'
import './styles/theme.scss'

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.use(ElementPlus)
// 初始化主题（与 index.html 早期脚本保持一致）
useThemeStore()
for (const [key, comp] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, comp as any)
}
app.mount('#app')
