<template>
  <AppShell>
    <div class="distill">
      <Splitpanes class="bfe-panes">
        <!-- 左：场景栏（可收起） -->
        <Pane v-if="!collapsed" :size="leftSize" :min-size="12" :max-size="30">
          <div class="left">
            <ScenarioSidebar @select="onSelect" />
            <button class="rail-btn collapse" title="收起场景栏" @click="collapsed = true">
              <el-icon><DArrowLeft /></el-icon>
            </button>
          </div>
        </Pane>

        <!-- 中：工作区 -->
        <Pane :size="centerSize" :min-size="30">
          <div class="center">
            <button v-if="collapsed" class="rail-btn expand" title="展开场景栏" @click="collapsed = false">
              <el-icon><DArrowRight /></el-icon>
            </button>

            <header class="center-head">
              <div class="head-title">
                <span class="cur-name">{{ cur?.name || '未选择场景' }}</span>
                <span v-if="cur" class="status-pill" :class="statusTone">{{ statusLabel }}</span>
              </div>
              <div class="spacer" />
              <el-button v-if="cur" type="primary" plain :icon="Upload" @click="uploadOpen = true">上传数据</el-button>
            </header>

            <div v-if="!cur" class="no-scene">
              <el-icon :size="34"><Files /></el-icon>
              <p>请在左侧选择或新建一个业务场景</p>
            </div>
            <template v-else>
              <el-tabs v-model="tab" class="center-tabs">
                <el-tab-pane name="tables">
                  <template #label><el-icon><Grid /></el-icon>表格 &amp; 字段</template>
                </el-tab-pane>
                <el-tab-pane name="flow">
                  <template #label><el-icon><Share /></el-icon>流程 &amp; 图谱</template>
                </el-tab-pane>
                <el-tab-pane name="skills">
                  <template #label><el-icon><MagicStick /></el-icon>技能</template>
                </el-tab-pane>
                <el-tab-pane name="outputs">
                  <template #label><el-icon><Promotion /></el-icon>产出 &amp; 执行</template>
                </el-tab-pane>
              </el-tabs>
              <div class="tab-body">
                <TablesPanel v-show="tab === 'tables'" :scenario="cur" @changed="refresh" />
                <DiagramView v-if="tab === 'flow'" :scenario="cur" />
                <SkillsPanel v-show="tab === 'skills'" :scenario="cur" />
                <OutputsPanel v-if="tab === 'outputs'" :scenario="cur" />
              </div>
            </template>
          </div>
        </Pane>

        <!-- 右：AI 对话（可拖宽） -->
        <Pane :size="chatSize" :min-size="24" :max-size="55">
          <ChatPanel
            v-if="cur"
            title="AI 协作对话"
            :chat-path="`/scenarios/${cur.id}/chat`"
            :history-path="`/scenarios/${cur.id}/messages`"
            :reload-key="cur.id"
            placeholder="描述你的诉求，或让 AI「推导关联关系 / 推导业务流程 / 生成技能」…"
            empty-title="与 AI 协作完成蒸馏"
            empty-sub="上传数据后，逐步让 AI 推导关联、还原流程、生成可复用技能。"
            :quicks="quicks"
            @refresh="onRefresh"
            @status="refresh"
          />
          <div v-else class="chat-ph">
            <el-icon :size="30"><ChatDotRound /></el-icon>
            <span>选择场景后开始对话</span>
          </div>
        </Pane>
      </Splitpanes>

      <UploadDialog v-if="cur" v-model="uploadOpen" :scenario-id="cur.id" @uploaded="refresh" />
    </div>
  </AppShell>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Splitpanes, Pane } from 'splitpanes'
import {
  DArrowLeft, DArrowRight, Upload, Files, Grid, Share, MagicStick, Promotion, ChatDotRound,
} from '@element-plus/icons-vue'
import AppShell from '@/components/AppShell.vue'
import ScenarioSidebar from '@/components/ScenarioSidebar.vue'
import ChatPanel from '@/components/ChatPanel.vue'
import DiagramView from '@/components/DiagramView.vue'
import TablesPanel from '@/components/TablesPanel.vue'
import SkillsPanel from '@/components/SkillsPanel.vue'
import OutputsPanel from '@/components/OutputsPanel.vue'
import UploadDialog from '@/components/UploadDialog.vue'
import { useScenarioStore } from '@/stores/scenarios'

const store = useScenarioStore()
const cur = computed(() => store.current)
const tab = ref('tables')
const collapsed = ref(false)
const uploadOpen = ref(false)

const leftSize = 18
const chatSize = 34
const centerSize = computed(() => 100 - (collapsed.value ? 0 : leftSize) - chatSize)

const STATUS: Record<string, string> = {
  created: '未开始', tables_uploaded: '已上传', relations_deduced: '已推关联',
  flow_deduced: '已推流程', skills_generated: '已生成技能', active: '已激活',
}
const statusLabel = computed(() => STATUS[cur.value?.status || ''] || cur.value?.status || '')
const statusTone = computed(() => {
  const s = cur.value?.status || ''
  if (['skills_generated', 'active'].includes(s)) return 'ready'
  if (['tables_uploaded', 'relations_deduced', 'flow_deduced'].includes(s)) return 'progress'
  return 'idle'
})

const quicks = [
  { label: '推导关联关系', text: '请推导关联关系' },
  { label: '推导业务流程', text: '请推导业务流程' },
  { label: '生成技能', text: '请生成技能' },
]

onMounted(async () => {
  await store.loadList()
  if (store.list.length && !store.currentId) await onSelect(store.list[0].id)
})

async function onSelect(id: string) {
  await store.select(id)
  tab.value = 'tables'
}
async function refresh() {
  await store.refreshCurrent()
}
async function onRefresh(resource: string) {
  await store.refreshCurrent()
  await store.loadList()
  if (resource === 'flow') tab.value = 'flow'
  else if (resource === 'skills') tab.value = 'skills'
  else if (['outputs', 'validations'].includes(resource)) tab.value = 'outputs'
  else if (resource === 'relations') tab.value = 'flow'
}
</script>

<style scoped lang="scss">
.distill { height: 100%; }
.left { height: 100%; position: relative; }

/* Rail collapse/expand buttons ------------------------------------------- */
.rail-btn {
  position: absolute; z-index: 6;
  display: inline-flex; align-items: center; justify-content: center;
  width: 22px; height: 46px;
  border: 1px solid var(--border); background: var(--surface); color: var(--text-3);
  cursor: pointer; box-shadow: var(--shadow-sm);
  transition: all var(--dur) var(--ease);
}
.rail-btn:hover { color: var(--brand); border-color: var(--brand); }
.rail-btn.collapse { top: 8px; right: -1px; border-radius: var(--r-sm) 0 0 var(--r-sm); }
.rail-btn.expand { top: 12px; left: 0; border-radius: 0 var(--r-sm) var(--r-sm) 0; }

/* Center ------------------------------------------------------------------ */
.center { height: 100%; display: flex; flex-direction: column; position: relative; background: var(--bg-app); }
.center-head {
  display: flex; align-items: center; gap: 12px;
  padding: 0 18px 0 24px; height: 58px; flex-shrink: 0;
  border-bottom: 1px solid var(--border); background: var(--surface);
}
.head-title { display: flex; align-items: center; gap: 10px; min-width: 0; }
.cur-name { font-weight: 700; font-size: var(--text-md); color: var(--text-1); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.status-pill {
  flex-shrink: 0; font-size: var(--text-xs); font-weight: 600;
  padding: 3px 10px; border-radius: var(--r-full);
}
.status-pill.idle { background: var(--surface-sunken); color: var(--text-3); }
.status-pill.progress { background: var(--warning-soft); color: var(--warning); }
.status-pill.ready { background: var(--success-soft); color: var(--success); }
.spacer { flex: 1; }

.no-scene { margin: auto; display: flex; flex-direction: column; align-items: center; gap: 12px; color: var(--text-3); font-size: var(--text-md); }
.no-scene p { margin: 0; }

/* Tabs -------------------------------------------------------------------- */
.center-tabs { padding: 0 20px; flex-shrink: 0; }
.center-tabs :deep(.el-tabs__header) { margin: 0; }
.center-tabs :deep(.el-tabs__nav-wrap::after) { height: 1px; background: var(--border); }
.center-tabs :deep(.el-tabs__item) { display: inline-flex; align-items: center; gap: 6px; height: 46px; font-size: var(--text-base); }
.center-tabs :deep(.el-tabs__item .el-icon) { margin: 0; }

.tab-body { flex: 1; overflow: hidden; }
.tab-body > * { height: 100%; }

.chat-ph { height: 100%; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; color: var(--text-3); background: var(--bg-app); }
</style>
