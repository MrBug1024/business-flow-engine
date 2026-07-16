import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import App from './App.vue'
import router from './router'
import './styles/theme.scss'

const app = createApp(App)
app.use(router)
app.use(ElementPlus)
for (const [key, comp] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, comp as any)
}
app.mount('#app')
