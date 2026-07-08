<template>
  <AppShell>
    <div class="sandbox">
      <Splitpanes class="bfe-panes">
        <Pane :size="24" :min-size="18" :max-size="34">
          <aside class="conversation-side">
            <div class="side-title">
              <div class="brand">零号.奇点工坊</div>
              <div class="muted">Agent 平台</div>
            </div>

            <el-button class="new-chat" type="primary" :icon="Plus" @click="newConversation">
              新建对话
            </el-button>

            <div class="conversation-list">
              <div
                v-for="conv in sandbox.conversations"
                :key="conv.id"
                class="conversation"
                :class="{ active: conv.id === sandbox.activeConversationId }"
                role="button"
                tabindex="0"
                @click="selectConversation(conv.id)"
                @keydown.enter.prevent="selectConversation(conv.id)"
              >
                <div class="conversation-copy">
                  <span class="conversation-name">{{ conv.title || '新对话' }}</span>
                  <span class="conversation-meta">{{ conv.message_count }} 条消息</span>
                </div>
                <el-button
                  class="conversation-delete"
                  text
                  size="small"
                  :icon="Delete"
                  @click.stop="removeConversation(conv.id)"
                />
              </div>
            </div>

            <div class="side-spacer" />

            <div class="resource-summary">
              <div class="summary-row"><span>LLM</span><b>{{ sandbox.llmOptions.length }}</b></div>
              <div class="summary-row"><span>Skill</span><b>{{ sandbox.skills.length }}</b></div>
              <div class="summary-row"><span>MCP</span><b>{{ sandbox.connectedMcps.length }}/{{ sandbox.mcps.length }}</b></div>
              <div class="summary-row"><span>子 Agent</span><b>{{ sandbox.agentConfig?.subagents.length || 0 }}</b></div>
            </div>

            <el-button class="config-button" :icon="Setting" @click="configOpen = true">
              打开配置面板
            </el-button>
          </aside>
        </Pane>

        <Pane :size="76" :min-size="50">
          <ChatPanel
            ref="chatRef"
            :title="chatTitle"
            chat-path="/playground/chat"
            :history-path="historyPath"
            :clear-path="clearPath"
            :reload-key="reloadKey"
            :request-payload="requestPayload"
            done-refresh-resource="conversation"
            placeholder="输入你的请求；未绑定资源时就是普通聊天，绑定后按配置调用 LLM / Skill / MCP / 子 Agent。"
            empty-title="普通 Agent 平台"
            empty-sub="主 Agent 默认可直接对话；需要能力时，在配置面板里添加并绑定 LLM、Skill、MCP 或子 Agent。"
            :attachments-path="attachmentsPath"
            :attachments-upload-path="attachmentsPath"
            :attachments-clear-path="attachmentsPath"
            @refresh="onChatRefresh"
          />
        </Pane>
      </Splitpanes>

      <AgentConfigDialog v-model="configOpen" />
    </div>
  </AppShell>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { Delete, Plus, Setting } from '@element-plus/icons-vue'
import { Splitpanes, Pane } from 'splitpanes'
import AppShell from '@/components/AppShell.vue'
import ChatPanel from '@/components/ChatPanel.vue'
import AgentConfigDialog from '@/components/AgentConfigDialog.vue'
import { useSandboxStore } from '@/stores/sandbox'

const sandbox = useSandboxStore()
const configOpen = ref(false)
const chatRef = ref<InstanceType<typeof ChatPanel>>()
const reloadKey = computed(() => sandbox.resourceVersion)
const conversationQuery = computed(() => (
  sandbox.activeConversationId
    ? `?conversation_id=${encodeURIComponent(sandbox.activeConversationId)}`
    : ''
))
const historyPath = computed(() => `/playground/messages${conversationQuery.value}`)
const clearPath = computed(() => `/playground/messages${conversationQuery.value}`)
const attachmentsPath = computed(() => `/playground/attachments${conversationQuery.value}`)
const requestPayload = computed(() => ({ conversation_id: sandbox.activeConversationId }))
const chatTitle = computed(() => `Agent 平台 · ${sandbox.activeConversation?.title || '普通对话'}`)

onMounted(async () => {
  await Promise.all([sandbox.loadResources(), sandbox.loadAgentConfig(), sandbox.loadConversations()])
})

async function newConversation() {
  await sandbox.createConversation()
  await chatRef.value?.reload()
}

function selectConversation(id: string) {
  sandbox.setActiveConversation(id)
}

async function removeConversation(id: string) {
  try {
    await ElMessageBox.confirm('确定删除这个对话？该对话的消息和附件会一起删除。', '删除对话', { type: 'warning' })
    await sandbox.deleteConversation(id)
    await chatRef.value?.reload()
  } catch { /* cancelled */ }
}

async function onChatRefresh(resource: string) {
  if (resource === 'conversation') await sandbox.loadConversations()
}
</script>

<style scoped lang="scss">
.sandbox { height: 100%; }
.conversation-side {
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: 12px;
  background: var(--surface);
  border-right: 1px solid var(--border);
  padding: 16px;
}
.side-title {
  padding: 4px 2px 8px;
}
.brand {
  font-weight: 800;
  color: var(--text-1);
  font-size: var(--text-base);
}
.muted {
  color: var(--text-3);
  font-size: var(--text-sm);
  margin-top: 3px;
}
.new-chat,
.config-button {
  width: 100%;
  justify-content: center;
}
.conversation-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-height: 0;
  overflow-y: auto;
}
.conversation {
  width: 100%;
  min-height: 58px;
  display: flex;
  align-items: center;
  gap: 8px;
  border: 1px solid var(--border);
  background: var(--surface-2);
  border-radius: var(--r-sm);
  padding: 10px 8px 10px 12px;
  cursor: pointer;
}
.conversation.active {
  border-color: color-mix(in srgb, var(--brand) 42%, transparent);
  background: var(--brand-soft);
}
.conversation-copy {
  min-width: 0;
  flex: 1;
}
.conversation-name {
  display: block;
  color: var(--text-1);
  font-weight: 700;
  font-size: var(--text-sm);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.conversation-meta {
  display: block;
  color: var(--text-3);
  font-size: var(--text-xs);
  margin-top: 3px;
}
.conversation-delete {
  flex-shrink: 0;
  opacity: 0.7;
}
.conversation:hover .conversation-delete {
  opacity: 1;
}
.side-spacer { flex: 1; }
.resource-summary {
  border: 1px solid var(--border);
  background: var(--surface-2);
  border-radius: var(--r-sm);
  padding: 10px 12px;
}
.summary-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--text-2);
  font-size: var(--text-sm);
  padding: 4px 0;
}
.summary-row b {
  color: var(--text-1);
}
</style>
