<template>
  <section class="mcp-workspace">
    <template v-if="!editorOpen">
      <header class="workspace-head">
        <div>
          <h2>{{ tr('title') }}</h2>
          <p>{{ tr('subtitle') }}</p>
        </div>
        <el-button type="primary" :icon="Plus" @click="openNewServer">{{ tr('add') }}</el-button>
      </header>

      <el-alert
        v-if="operationError"
        class="operation-alert"
        type="error"
        :closable="true"
        show-icon
        :title="operationError"
        @close="operationError = ''"
      />

      <div class="workspace-scroll">
        <section aria-labelledby="installed-mcp-heading">
          <header class="group-head">
            <div>
              <h3 id="installed-mcp-heading">{{ tr('installed') }}</h3>
              <p>{{ servers.length }} {{ tr('configured') }}</p>
            </div>
          </header>

          <div v-if="servers.length" class="server-grid">
            <article v-for="server in servers" :key="server.name" class="server-card">
              <header class="server-card-head">
                <span class="server-icon"><el-icon><Connection /></el-icon></span>
                <div class="server-title">
                  <strong :title="server.name">{{ server.name }}</strong>
                  <span><i :class="statusClass(server)"></i>{{ statusLabel(server) }}</span>
                </div>
                <div class="icon-actions">
                  <el-tooltip :content="tr('edit')" placement="top">
                    <el-button :icon="EditPen" text circle :aria-label="tr('edit')" @click="openServer(server.name)" />
                  </el-tooltip>
                  <el-tooltip :content="tr('delete')" placement="top">
                    <el-button
                      :icon="Delete"
                      text
                      circle
                      class="danger-action"
                      :loading="deletingName === server.name"
                      :aria-label="tr('delete')"
                      @click="removeServer(server)"
                    />
                  </el-tooltip>
                </div>
              </header>

              <dl class="server-facts">
                <div><dt>{{ tr('transport') }}</dt><dd>{{ serverTransport(server) }}</dd></div>
                <div><dt>URL</dt><dd :title="serverEndpoint(server)">{{ serverEndpoint(server) }}</dd></div>
                <div><dt>{{ tr('tools') }}</dt><dd>{{ serverTools(server).length }}</dd></div>
              </dl>

              <footer class="server-card-foot">
                <span>{{ server.enabled === false ? tr('disabled') : tr('enabled') }}</span>
                <el-switch
                  :model-value="server.enabled !== false"
                  :loading="togglingName === server.name"
                  :aria-label="server.enabled === false ? tr('disabled') : tr('enabled')"
                  @change="setServerEnabled(server, Boolean($event))"
                />
              </footer>
            </article>
          </div>

          <button v-else type="button" class="empty-state" @click="openNewServer">
            <span class="empty-icon"><el-icon><Connection /></el-icon></span>
            <strong>{{ tr('emptyTitle') }}</strong>
            <span>{{ tr('emptyBody') }}</span>
            <b>{{ tr('addFirst') }}</b>
          </button>
        </section>

        <section v-if="availableTemplates.length" class="template-section" aria-labelledby="mcp-template-heading">
          <header class="group-head">
            <div>
              <h3 id="mcp-template-heading">{{ tr('templates') }}</h3>
              <p>{{ tr('templatesHelp') }}</p>
            </div>
          </header>
          <div class="template-grid">
            <button
              v-for="template in availableTemplates"
              :key="template.name"
              type="button"
              class="template-card"
              @click="openTemplate(template.name)"
            >
              <span class="server-icon template"><el-icon><SetUp /></el-icon></span>
              <span class="template-copy">
                <strong>{{ template.name }}</strong>
                <small>{{ template.description || tr('noDescription') }}</small>
              </span>
              <el-icon class="template-arrow"><ArrowRight /></el-icon>
            </button>
          </div>
        </section>
      </div>
    </template>

    <template v-else>
      <header class="editor-head">
        <button type="button" class="back-button" :aria-label="tr('back')" @click="closeEditor">
          <el-icon><ArrowLeft /></el-icon>
        </button>
        <div>
          <h2>{{ editorTitle }}</h2>
          <p>{{ tr('editorHelp') }}</p>
        </div>
      </header>
      <div class="editor-host">
        <McpSettingsPanel
          :key="editorSessionKey"
          ref="editorRef"
          :servers="servers"
          :templates="templates"
          :language="language"
          @changed="handleEditorChanged"
        />
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, ArrowRight, Connection, Delete, EditPen, Plus, SetUp } from '@element-plus/icons-vue'
import { http } from '@/api/http'
import McpSettingsPanel from '@/components/McpSettingsPanel.vue'

type Language = 'zh' | 'en'
type McpTool = { name: string; description?: string }
type McpServer = {
  name: string
  enabled?: boolean
  config?: Record<string, any>
  tools?: Array<McpTool | string>
  status?: string
  connection_status?: string
}
type McpTemplate = { name: string; description?: string; config_example?: Record<string, any> }
type EditorApi = {
  canReplaceDraft: () => Promise<boolean>
  selectServer: (name: string) => Promise<void>
  selectTemplate: (name: string) => Promise<void>
  startNewServer: () => Promise<void>
}

const props = withDefaults(
  defineProps<{
    servers: McpServer[]
    templates: McpTemplate[]
    language?: Language
  }>(),
  { language: 'zh' },
)

const emit = defineEmits<{
  changed: [settings?: any]
}>()

const copy: Record<Language, Record<string, string>> = {
  zh: {
    add: '添加 MCP',
    addFirst: '添加第一个服务',
    back: '返回 MCP 服务',
    configured: '个服务已配置',
    delete: '删除服务',
    deleteBody: '删除后，AI 将无法再调用这个 MCP 服务。',
    disabled: '已停用',
    edit: '编辑配置',
    editorHelp: '配置内容仅在添加或编辑服务时显示。',
    emptyBody: '添加远程 MCP 地址，连接成功后即可将工具交给 AI 调用。',
    emptyTitle: '还没有 MCP 服务',
    enabled: '已启用',
    installed: '已安装服务',
    noDescription: '暂无说明',
    subtitle: '管理 AI 可发现和调用的远程工具服务。',
    templates: '可用连接器',
    templatesHelp: '从预置连接器开始，再补充真实地址和鉴权信息。',
    title: 'MCP 服务',
    tools: '工具',
    transport: '传输',
  },
  en: {
    add: 'Add MCP',
    addFirst: 'Add first server',
    back: 'Back to MCP servers',
    configured: 'servers configured',
    delete: 'Delete server',
    deleteBody: 'AI will no longer be able to call this MCP server.',
    disabled: 'Disabled',
    edit: 'Edit configuration',
    editorHelp: 'Configuration is shown only while adding or editing a server.',
    emptyBody: 'Add a remote MCP endpoint and its discovered tools become available to AI.',
    emptyTitle: 'No MCP servers yet',
    enabled: 'Enabled',
    installed: 'Installed servers',
    noDescription: 'No description',
    subtitle: 'Manage remote tool services available to the AI runtime.',
    templates: 'Available connectors',
    templatesHelp: 'Start from a connector and provide its endpoint and credentials.',
    title: 'MCP servers',
    tools: 'Tools',
    transport: 'Transport',
  },
}

const editorOpen = ref(false)
const editorRef = ref<EditorApi | null>(null)
const editorSessionKey = ref(0)
const editorTitle = ref('')
const operationError = ref('')
const togglingName = ref('')
const deletingName = ref('')

const configuredNames = computed(() => new Set(props.servers.map((item) => item.name)))
const availableTemplates = computed(() => props.templates.filter((item) => !configuredNames.value.has(item.name)))

function tr(key: string) {
  return copy[props.language]?.[key] || copy.zh[key] || key
}

async function mountEditor(title: string, action: (api: EditorApi) => Promise<void>) {
  operationError.value = ''
  editorTitle.value = title
  editorSessionKey.value += 1
  editorOpen.value = true
  await nextTick()
  if (editorRef.value) await action(editorRef.value)
}

async function openNewServer() {
  await mountEditor(tr('add'), (api) => api.startNewServer())
}

async function openServer(name: string) {
  await mountEditor(name, (api) => api.selectServer(name))
}

async function openTemplate(name: string) {
  await mountEditor(name, (api) => api.selectTemplate(name))
}

async function closeEditor() {
  if (editorRef.value && !(await editorRef.value.canReplaceDraft())) return
  editorOpen.value = false
}

function handleEditorChanged(settings?: any) {
  editorOpen.value = false
  emit('changed', settings)
}

async function setServerEnabled(server: McpServer, enabled: boolean) {
  togglingName.value = server.name
  operationError.value = ''
  try {
    const response = await http.patch(`/mcp-servers/${encodeURIComponent(server.name)}`, { enabled })
    emit('changed', response.data?.settings || response.data)
  } catch (error: any) {
    operationError.value = requestError(error)
  } finally {
    togglingName.value = ''
  }
}

async function removeServer(server: McpServer) {
  try {
    await ElMessageBox.confirm(tr('deleteBody'), `${tr('delete')} ${server.name}?`, {
      type: 'warning',
      confirmButtonText: props.language === 'en' ? 'Delete' : '删除',
      cancelButtonText: props.language === 'en' ? 'Cancel' : '取消',
      confirmButtonClass: 'el-button--danger',
    })
  } catch {
    return
  }
  deletingName.value = server.name
  operationError.value = ''
  try {
    const response = await http.delete(`/mcp-servers/${encodeURIComponent(server.name)}`)
    emit('changed', response.data?.settings || response.data)
    ElMessage.success(props.language === 'en' ? 'MCP server deleted' : 'MCP 服务已删除')
  } catch (error: any) {
    operationError.value = requestError(error)
  } finally {
    deletingName.value = ''
  }
}

function requestError(error: any) {
  const detail = error?.response?.data?.detail || error?.response?.data?.message
  return typeof detail === 'string' ? detail : JSON.stringify(detail || error?.message || 'Request failed')
}

function serverStatus(server: McpServer) {
  if (server.enabled === false) return 'disabled'
  const explicit = String(server.connection_status || server.status || '').toLowerCase()
  if (explicit) return explicit
  return serverTools(server).length ? 'connected' : 'unverified'
}

function statusClass(server: McpServer) {
  const status = serverStatus(server)
  if (['connected', 'success', 'succeeded', 'healthy', 'ready'].includes(status)) return 'status-dot connected'
  if (['failed', 'error', 'unreachable'].includes(status)) return 'status-dot failed'
  return 'status-dot neutral'
}

function statusLabel(server: McpServer) {
  const status = serverStatus(server)
  if (status === 'disabled') return tr('disabled')
  if (['connected', 'success', 'succeeded', 'healthy', 'ready'].includes(status)) {
    return props.language === 'en' ? 'Connected' : '连接正常'
  }
  if (['failed', 'error', 'unreachable'].includes(status)) return props.language === 'en' ? 'Connection failed' : '连接失败'
  return props.language === 'en' ? 'Not verified' : '待验证'
}

function serverTools(server: McpServer) {
  const tools = server.tools || server.config?.tools || []
  return Array.isArray(tools) ? tools : []
}

function serverTransport(server: McpServer) {
  return String(server.config?.type || server.config?.transport || '-').replace('streamable_http', 'http')
}

function serverEndpoint(server: McpServer) {
  return String(server.config?.url || server.config?.command || '-')
}
</script>

<style scoped>
.mcp-workspace {
  display: flex;
  height: calc(100dvh - 202px);
  min-height: 500px;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-0);
}

.workspace-head,
.editor-head,
.group-head,
.server-card-head,
.server-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.workspace-head,
.editor-head {
  flex: 0 0 auto;
  min-height: 64px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border-soft);
  background: color-mix(in srgb, var(--surface-0) 88%, var(--surface-2));
}

.workspace-head h2,
.workspace-head p,
.editor-head h2,
.editor-head p,
.group-head h3,
.group-head p {
  margin: 0;
}

.workspace-head h2,
.editor-head h2 {
  color: var(--text-strong);
  font-size: 14px;
}

.workspace-head p,
.editor-head p,
.group-head p {
  margin-top: 3px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
}

.operation-alert {
  flex: 0 0 auto;
  margin: 10px 14px 0;
}

.workspace-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 18px;
}

.group-head {
  margin-bottom: 10px;
}

.group-head h3 {
  color: var(--text-strong);
  font-size: 13px;
}

.server-grid,
.template-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 10px;
}

.server-card {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-1);
  transition: border-color 0.16s ease, background-color 0.16s ease;
}

.server-card:hover {
  border-color: color-mix(in srgb, var(--accent) 42%, var(--border));
  background: color-mix(in srgb, var(--surface-1) 90%, var(--accent-soft));
}

.server-card-head {
  min-width: 0;
  padding: 11px 10px 9px 12px;
}

.server-icon {
  display: grid;
  flex: 0 0 34px;
  place-items: center;
  width: 34px;
  height: 34px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-2);
  color: var(--accent);
}

.server-icon.template {
  color: var(--text-muted);
}

.server-title,
.template-copy {
  flex: 1;
  min-width: 0;
}

.server-title strong,
.server-title span,
.template-copy strong,
.template-copy small {
  display: block;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.server-title strong,
.template-copy strong {
  color: var(--text-strong);
  font-size: 12px;
}

.server-title span,
.template-copy small {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 10px;
}

.icon-actions {
  display: flex;
  flex: 0 0 auto;
  gap: 1px;
}

.icon-actions :deep(.el-button) {
  width: 32px;
  height: 32px;
  margin: 0;
}

.danger-action:hover {
  color: var(--el-color-danger);
}

.server-facts {
  display: grid;
  grid-template-columns: 0.72fr 1.6fr 0.52fr;
  margin: 0;
  border-top: 1px solid var(--border-soft);
  border-bottom: 1px solid var(--border-soft);
}

.server-facts div {
  min-width: 0;
  padding: 9px 10px;
}

.server-facts div + div {
  border-left: 1px solid var(--border-soft);
}

.server-facts dt,
.server-facts dd {
  margin: 0;
}

.server-facts dt {
  color: var(--text-muted);
  font-size: 9px;
}

.server-facts dd {
  margin-top: 4px;
  overflow: hidden;
  color: var(--text-main);
  font-family: var(--font-mono);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.server-card-foot {
  min-height: 42px;
  padding: 6px 11px;
  color: var(--text-muted);
  font-size: 11px;
}

.status-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 5px;
  border-radius: 50%;
  background: var(--text-muted);
}

.status-dot.connected {
  background: var(--el-color-success);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--el-color-success) 13%, transparent);
}

.status-dot.failed {
  background: var(--el-color-danger);
}

.empty-state {
  display: grid;
  width: 100%;
  min-height: 190px;
  place-content: center;
  justify-items: center;
  gap: 7px;
  padding: 24px;
  border: 1px dashed var(--border);
  border-radius: 7px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  text-align: center;
}

.empty-state:hover {
  border-color: var(--accent);
  background: var(--surface-hover);
}

.empty-state strong {
  color: var(--text-strong);
  font-size: 13px;
}

.empty-state > span:not(.empty-icon) {
  max-width: 430px;
  font-size: 11px;
  line-height: 1.6;
}

.empty-state b {
  color: var(--accent);
  font-size: 11px;
}

.empty-icon {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  margin-bottom: 3px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-2);
  color: var(--accent);
}

.template-section {
  margin-top: 24px;
  padding-top: 18px;
  border-top: 1px solid var(--border-soft);
}

.template-card {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) 18px;
  gap: 10px;
  align-items: center;
  min-height: 62px;
  padding: 9px 11px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-1);
  color: var(--text-main);
  cursor: pointer;
  text-align: left;
}

.template-card:hover {
  border-color: var(--accent);
  background: var(--surface-hover);
}

.template-arrow {
  color: var(--text-muted);
}

.editor-head {
  justify-content: flex-start;
}

.back-button {
  display: grid;
  flex: 0 0 40px;
  place-items: center;
  width: 40px;
  height: 40px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-main);
  cursor: pointer;
}

.back-button:hover {
  background: var(--surface-hover);
  color: var(--text-strong);
}

.back-button:focus-visible,
.template-card:focus-visible,
.empty-state:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.editor-host {
  flex: 1;
  min-height: 0;
  overflow: hidden;
}

.editor-host :deep(.mcp-settings) {
  grid-template-columns: minmax(0, 1fr);
  height: 100%;
  min-height: 0;
  border: 0;
  border-radius: 0;
}

.editor-host :deep(.mcp-sidebar) {
  display: none;
}

.editor-host :deep(.mcp-detail) {
  display: flex;
}

.editor-host :deep(.mcp-detail-head) {
  display: none;
}

@media (max-width: 700px) {
  .mcp-workspace {
    height: calc(100dvh - 172px);
    min-height: 460px;
    border-right: 0;
    border-left: 0;
    border-radius: 0;
  }

  .workspace-head {
    align-items: flex-start;
  }

  .workspace-head :deep(.el-button) {
    min-height: 44px;
  }

  .workspace-scroll {
    padding: 12px;
  }

  .server-grid,
  .template-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .icon-actions :deep(.el-button) {
    width: 44px;
    height: 44px;
  }

  .server-card-head {
    flex-wrap: wrap;
  }

  .icon-actions {
    width: 100%;
    justify-content: flex-end;
    border-top: 1px solid var(--border-soft);
    padding-top: 5px;
  }

  .editor-host :deep(.mcp-detail) {
    height: 100%;
  }

  .editor-host :deep(.json-editor) {
    font-size: 16px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .server-card {
    transition: none;
  }
}
</style>
