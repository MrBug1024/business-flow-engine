<template>
  <AppShell>
    <div class="sandbox">
      <Splitpanes class="bfe-panes">
        <Pane :size="32" :min-size="22" :max-size="44">
          <div class="caps">
            <div class="cap-banner" :class="{ empty: !mountedItems.length }">
              <el-icon><InfoFilled /></el-icon>
              <span>{{ mountedItems.length
                ? `已挂载 ${mountedItems.length} 个业务能力，沙盒会自主发现并决定何时调用。`
                : '尚未挂载任何能力——此时它就是个普通助理。请在下方安装一个能力后再试。' }}</span>
            </div>

            <div class="cap-section">
              <div class="cap-label">已挂载能力 · {{ mountedItems.length }}</div>
              <div v-if="!mountedItems.length" class="cap-empty">（空）</div>
              <div v-for="c in mountedItems" :key="c.scenario_id" class="cap-card card on">
                <div class="cap-head">
                  <span class="cap-dot on" />
                  <span class="cap-name">{{ c.display_name }}</span>
                  <span class="cap-ns mono">{{ c.namespace }}</span>
                </div>
                <div class="cap-sum">{{ c.summary }}</div>
                <div class="cap-actions">
                  <el-button size="small" :icon="Setting" @click="openConfig(c.scenario_id)">配置</el-button>
                  <el-button size="small" type="danger" plain :icon="Close" @click="sandbox.unmount(c.scenario_id)">卸载</el-button>
                </div>
              </div>
            </div>

            <div class="cap-section">
              <div class="cap-label">能力市场 · 可安装</div>
              <div v-if="!marketItems.length" class="cap-empty">
                暂无可安装能力。请先在蒸馏工作台完成「生成技能」。
              </div>
              <div v-for="c in marketItems" :key="c.scenario_id" class="cap-card card">
                <div class="cap-head">
                  <span class="cap-dot" />
                  <span class="cap-name">{{ c.display_name }}</span>
                  <span class="cap-ns mono">{{ c.namespace }}</span>
                </div>
                <div class="cap-sum">{{ c.summary }}</div>
                <div class="cap-actions">
                  <el-button size="small" type="primary" :icon="Plus" @click="sandbox.mount(c.scenario_id)">安装</el-button>
                  <el-button size="small" @click="openConfig(c.scenario_id)">详情</el-button>
                </div>
              </div>
            </div>
          </div>
        </Pane>

        <Pane :size="68" :min-size="45">
          <ChatPanel
            title="通用第三方沙盒 · 自主发现 · 自主决策"
            chat-path="/playground/chat"
            history-path="/playground/messages"
            clear-path="/playground/messages"
            :reload-key="reloadKey"
            placeholder="用自然语言描述你的诉求（业务或通用皆可）…"
            empty-title="像配置 MCP 一样挂载业务能力"
            empty-sub="左侧安装一个业务能力后，直接提出业务诉求；沙盒会自主判断是否调用、调用哪个能力。"
          />
        </Pane>
      </Splitpanes>

      <CapabilityConfigDialog v-model="configOpen" :scenario-id="configSid" />
    </div>
  </AppShell>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { Splitpanes, Pane } from 'splitpanes'
import { InfoFilled, Setting, Close, Plus } from '@element-plus/icons-vue'
import AppShell from '@/components/AppShell.vue'
import ChatPanel from '@/components/ChatPanel.vue'
import CapabilityConfigDialog from '@/components/CapabilityConfigDialog.vue'
import { useSandboxStore } from '@/stores/sandbox'

const sandbox = useSandboxStore()
const mountedItems = computed(() => sandbox.mountedItems)
const marketItems = computed(() => sandbox.marketItems)
const reloadKey = computed(() => sandbox.mounted.join(','))

const configOpen = ref(false)
const configSid = ref<string | null>(null)

onMounted(() => sandbox.loadCatalog())

function openConfig(sid: string) {
  configSid.value = sid
  configOpen.value = true
}
</script>

<style scoped lang="scss">
.sandbox { height: 100%; }
.caps { height: 100%; overflow-y: auto; background: var(--surface); padding: 16px; }

.cap-banner {
  display: flex; align-items: flex-start; gap: 8px;
  font-size: var(--text-sm); line-height: 1.55; padding: 11px 13px;
  background: var(--info-soft); border: 1px solid color-mix(in srgb, var(--info) 25%, transparent);
  border-radius: var(--r-sm); margin-bottom: 18px; color: var(--text-2);
}
.cap-banner .el-icon { color: var(--info); flex-shrink: 0; margin-top: 1px; }
.cap-banner.empty { background: var(--warning-soft); border-color: color-mix(in srgb, var(--warning) 25%, transparent); }
.cap-banner.empty .el-icon { color: var(--warning); }

.cap-section { margin-bottom: 22px; }
.cap-label { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-3); font-weight: 700; margin-bottom: 10px; }
.cap-empty { font-size: var(--text-sm); color: var(--text-3); padding: 4px 2px; }

.cap-card { padding: 12px 14px; margin-bottom: 10px; transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease); }
.cap-card:hover { border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
.cap-card.on { border-color: color-mix(in srgb, var(--success) 40%, transparent); background: var(--success-soft); }
.cap-head { display: flex; align-items: center; gap: 8px; }
.cap-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-3); flex-shrink: 0; }
.cap-dot.on { background: var(--success); box-shadow: 0 0 0 3px var(--success-soft); }
.cap-name { flex: 1; font-weight: 700; font-size: var(--text-md); color: var(--text-1); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cap-ns { font-size: var(--text-xs); color: var(--text-3); }
.cap-sum { font-size: var(--text-sm); color: var(--text-2); margin-top: 6px; line-height: 1.55; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.cap-actions { display: flex; gap: 7px; margin-top: 10px; }
</style>
