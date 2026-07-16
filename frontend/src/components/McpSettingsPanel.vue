<template>
  <section class="mcp-settings" :class="{ 'detail-open': mobileDetailOpen }">
    <aside class="mcp-sidebar" aria-label="MCP servers">
      <header class="mcp-sidebar-head">
        <div>
          <strong>{{ tr('servers') }}</strong>
          <span>{{ configuredServers.length }} {{ tr('configured') }}</span>
        </div>
        <el-tooltip :content="tr('newServer')" placement="bottom">
          <el-button :icon="Plus" circle aria-label="Add MCP server" @click="startNewServer" />
        </el-tooltip>
      </header>

      <div class="mcp-list-scroll">
        <section class="mcp-list-group">
          <h3>{{ tr('myServers') }}</h3>
          <button
            v-for="server in configuredServers"
            :key="server.name"
            type="button"
            class="mcp-server-item"
            :class="{ active: selectedKey === server.name }"
            @click="selectServer(server.name)"
          >
            <span class="server-glyph"><el-icon><Connection /></el-icon></span>
            <span class="server-copy">
              <strong>{{ server.name }}</strong>
              <small>
                <i :class="statusClass(server)"></i>
                {{ statusLabel(server) }}
                <template v-if="serverTools(server).length"> · {{ serverTools(server).length }} tools</template>
              </small>
            </span>
          </button>
          <div v-if="!configuredServers.length" class="mcp-empty-list">
            <span>{{ tr('emptyServers') }}</span>
            <el-button text type="primary" @click="startNewServer">{{ tr('addFirst') }}</el-button>
          </div>
        </section>

        <section v-if="availableTemplates.length" class="mcp-list-group templates">
          <h3>{{ tr('templates') }}</h3>
          <button
            v-for="template in availableTemplates"
            :key="template.name"
            type="button"
            class="mcp-server-item"
            :class="{ active: selectedKey === templateKey(template.name) }"
            @click="selectTemplate(template.name)"
          >
            <span class="server-glyph template"><el-icon><SetUp /></el-icon></span>
            <span class="server-copy">
              <strong>{{ template.name }}</strong>
              <small>{{ template.description }}</small>
            </span>
          </button>
        </section>
      </div>
    </aside>

    <main class="mcp-detail">
      <header class="mcp-detail-head">
        <button class="mobile-back" type="button" :aria-label="tr('back')" @click="mobileDetailOpen = false">
          <el-icon><ArrowLeft /></el-icon>
        </button>
        <span class="detail-glyph"><el-icon><Connection /></el-icon></span>
        <div class="detail-title">
          <strong>{{ detailName }}</strong>
          <span>
            <i :class="activeStatusClass"></i>
            {{ activeStatusLabel }}
          </span>
        </div>
        <div v-if="activeSavedServer" class="detail-actions">
          <label class="enable-control">
            <span>{{ activeSavedServer.enabled ? tr('enabled') : tr('disabled') }}</span>
            <el-switch
              :model-value="Boolean(activeSavedServer.enabled)"
              :loading="toggleLoading"
              @change="toggleServer(Boolean($event))"
            />
          </label>
          <el-tooltip :content="tr('deleteServer')" placement="bottom">
            <el-button
              :icon="Delete"
              circle
              class="delete-button"
              :loading="deleteLoading"
              :aria-label="tr('deleteServer')"
              @click="deleteServer"
            />
          </el-tooltip>
        </div>
      </header>

      <div class="mcp-detail-scroll">
        <section class="config-section">
          <header class="section-toolbar">
            <div>
              <h2>{{ tr('configuration') }}</h2>
              <p>{{ tr('configurationHelp') }}</p>
            </div>
            <el-button :icon="DocumentCopy" @click="formatDraft">{{ tr('format') }}</el-button>
          </header>

          <div class="json-editor-shell" :class="{ invalid: Boolean(validationError) }">
            <div class="json-editor-title">
              <span>mcp.json</span>
              <span v-if="draftDirty" class="dirty-mark">{{ tr('unsaved') }}</span>
            </div>
            <textarea
              v-model="draft"
              class="json-editor"
              :aria-label="tr('configuration')"
              autocomplete="off"
              autocapitalize="off"
              spellcheck="false"
              data-1p-ignore="true"
              @input="onDraftInput"
              @blur="validateDraft"
            ></textarea>
          </div>

          <p class="secret-note">
            <el-icon><Key /></el-icon>
            {{ tr('secretNote') }}
          </p>

          <el-alert
            v-if="validationError || operationError"
            class="inline-alert"
            type="error"
            :closable="false"
            show-icon
            :title="validationError || operationError"
          />

          <div class="config-actions">
            <el-button :icon="VideoPlay" :loading="testLoading" @click="testConnection">
              {{ testLoading ? tr('testing') : tr('testConnection') }}
            </el-button>
            <el-button type="primary" :loading="saveLoading" @click="saveConfiguration">
              {{ saveLoading ? tr('saving') : tr('save') }}
            </el-button>
          </div>
        </section>

        <section v-if="testResult || activeSavedServer" class="connection-section">
          <header class="section-toolbar compact">
            <div>
              <h2>{{ tr('connection') }}</h2>
              <p>{{ connectionSummary }}</p>
            </div>
            <span class="connection-badge" :class="connectionBadgeClass">
              <el-icon><CircleCheck v-if="connectionSucceeded" /><Warning v-else /></el-icon>
              {{ connectionSucceeded ? tr('reachable') : tr('notVerified') }}
            </span>
          </header>
          <dl class="connection-facts">
            <div>
              <dt>{{ tr('transport') }}</dt>
              <dd>{{ connectionTransport }}</dd>
            </div>
            <div>
              <dt>{{ tr('latency') }}</dt>
              <dd>{{ connectionLatency }}</dd>
            </div>
            <div>
              <dt>{{ tr('toolsFound') }}</dt>
              <dd>{{ visibleTools.length }}</dd>
            </div>
          </dl>
        </section>

        <section class="tools-section">
          <header class="section-toolbar compact">
            <div>
              <h2>{{ tr('discoveredTools') }}</h2>
              <p>{{ tr('toolsHelp') }}</p>
            </div>
            <span class="tool-count">{{ visibleTools.length }}</span>
          </header>

          <div v-if="visibleTools.length" class="tool-list">
            <details v-for="tool in visibleTools" :key="`${tool.server || detailName}:${tool.name}`" class="tool-row">
              <summary>
                <span class="tool-icon"><el-icon><SetUp /></el-icon></span>
                <span class="tool-copy">
                  <strong>{{ tool.name }}</strong>
                  <small>{{ tool.description || tr('noDescription') }}</small>
                </span>
                <code v-if="tool.server">{{ tool.server }}</code>
              </summary>
              <pre>{{ formatSchema(tool) }}</pre>
            </details>
          </div>
          <div v-else class="tools-empty">
            <el-icon><Connection /></el-icon>
            <strong>{{ tr('noTools') }}</strong>
            <span>{{ tr('noToolsHelp') }}</span>
          </div>
        </section>
      </div>
    </main>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  ArrowLeft,
  CircleCheck,
  Connection,
  Delete,
  DocumentCopy,
  Key,
  Plus,
  SetUp,
  VideoPlay,
  Warning,
} from '@element-plus/icons-vue'
import { http } from '@/api/http'

type Language = 'zh' | 'en'
type McpTool = {
  name: string
  description?: string
  server?: string
  inputSchema?: Record<string, unknown>
  input_schema?: Record<string, unknown>
  schema?: Record<string, unknown>
}
type McpServer = {
  name: string
  enabled?: boolean
  config?: Record<string, any>
  tools?: Array<McpTool | string>
  status?: string
  connection_status?: string
  latency_ms?: number
  last_error?: string
}
type McpTemplate = {
  name: string
  description?: string
  config_example?: Record<string, any>
}

const props = withDefaults(
  defineProps<{
    servers: McpServer[]
    templates: McpTemplate[]
    language?: Language
  }>(),
  {
    language: 'zh',
  },
)

const emit = defineEmits<{
  changed: [settings?: any]
}>()

const labels: Record<Language, Record<string, string>> = {
  zh: {
    addFirst: '添加第一个服务',
    back: '返回服务列表',
    configured: '个已配置',
    configuration: '服务配置',
    configurationHelp: '支持完整 mcpServers JSON，也兼容单个服务配置。',
    connection: '连接状态',
    deleteConfirm: '删除后，AI 将无法再调用这个 MCP 服务。',
    deleteServer: '删除服务',
    disabled: '已停用',
    discoveredTools: '可调用工具',
    emptyServers: '还没有配置 MCP 服务',
    enabled: '已启用',
    format: '格式化',
    latency: '延迟',
    myServers: '我的服务',
    newServer: '新建或导入 MCP 服务',
    noDescription: '暂无工具说明',
    noTools: '尚未发现工具',
    noToolsHelp: '测试连接后会显示服务实际提供的工具与参数结构。',
    notVerified: '待验证',
    reachable: '连接正常',
    save: '保存配置',
    saving: '保存中',
    secretNote: 'Authorization 等敏感请求头由服务端遮蔽；遮蔽值再次保存时不会覆盖原密钥。',
    servers: 'MCP 服务',
    templates: '可用模板',
    testConnection: '测试连接',
    testing: '连接中',
    toolsFound: '发现工具',
    toolsHelp: 'AI 运行时只会看到服务真实返回的工具。',
    transport: '传输方式',
    unsaved: '未保存',
  },
  en: {
    addFirst: 'Add first server',
    back: 'Back to server list',
    configured: 'configured',
    configuration: 'Server configuration',
    configurationHelp: 'Accepts a complete mcpServers document or a single server config.',
    connection: 'Connection status',
    deleteConfirm: 'AI will no longer be able to call this MCP server.',
    deleteServer: 'Delete server',
    disabled: 'Disabled',
    discoveredTools: 'Callable tools',
    emptyServers: 'No MCP servers configured',
    enabled: 'Enabled',
    format: 'Format',
    latency: 'Latency',
    myServers: 'My servers',
    newServer: 'Add or import MCP server',
    noDescription: 'No description',
    noTools: 'No tools discovered',
    noToolsHelp: 'Test the connection to load the tools and input schemas exposed by the server.',
    notVerified: 'Not verified',
    reachable: 'Connected',
    save: 'Save configuration',
    saving: 'Saving',
    secretNote: 'Sensitive headers are redacted by the server; saving a redacted value preserves the stored secret.',
    servers: 'MCP servers',
    templates: 'Templates',
    testConnection: 'Test connection',
    testing: 'Connecting',
    toolsFound: 'Tools found',
    toolsHelp: 'The AI runtime only receives tools actually reported by the server.',
    transport: 'Transport',
    unsaved: 'Unsaved',
  },
}

const selectedKey = ref('')
const draft = ref('')
const draftDirty = ref(false)
const mobileDetailOpen = ref(false)
const validationError = ref('')
const operationError = ref('')
const testLoading = ref(false)
const saveLoading = ref(false)
const toggleLoading = ref(false)
const deleteLoading = ref(false)
const testResult = ref<any | null>(null)

const configuredServers = computed(() => props.servers || [])
const configuredNames = computed(() => new Set(configuredServers.value.map((item) => item.name)))
const availableTemplates = computed(() => (props.templates || []).filter((item) => !configuredNames.value.has(item.name)))
const activeSavedServer = computed(() => configuredServers.value.find((item) => item.name === selectedKey.value))
const activeTemplate = computed(() => {
  const name = selectedKey.value.startsWith('template:') ? selectedKey.value.slice('template:'.length) : ''
  return availableTemplates.value.find((item) => item.name === name)
})
const draftServerNames = computed(() => extractServerNames(parseDraftSilently()))
const detailName = computed(() => {
  if (selectedKey.value === '__new__') return draftServerNames.value.join(', ') || tr('newServer')
  return activeSavedServer.value?.name || activeTemplate.value?.name || tr('newServer')
})
const activeStatusLabel = computed(() => {
  if (testLoading.value) return tr('testing')
  if (testResult.value) return connectionSucceeded.value ? tr('reachable') : tr('notVerified')
  if (activeSavedServer.value) return statusLabel(activeSavedServer.value)
  return tr('notVerified')
})
const activeStatusClass = computed(() => {
  if (testLoading.value) return 'status-dot connecting'
  if (testResult.value) return connectionSucceeded.value ? 'status-dot connected' : 'status-dot failed'
  return statusClass(activeSavedServer.value)
})
const testedTools = computed<McpTool[]>(() => collectTools(testResult.value))
const visibleTools = computed<McpTool[]>(() => {
  if (testResult.value) return testedTools.value
  return serverTools(activeSavedServer.value)
})
const connectionSucceeded = computed(() => {
  if (!testResult.value) return ['connected', 'success', 'succeeded', 'healthy', 'ready'].includes(serverStatus(activeSavedServer.value))
  if (Array.isArray(testResult.value.servers)) {
    return (
      testResult.value.servers.length > 0 &&
      testResult.value.servers.every((server: any) => {
        const status = String(server?.status || server?.connection_status || '').toLowerCase()
        return !server?.error && server?.ok !== false && server?.success !== false && ['connected', 'success', 'succeeded', 'healthy', 'ready'].includes(status)
      })
    )
  }
  const status = String(testResult.value.status || testResult.value.connection_status || '').toLowerCase()
  return (
    testResult.value.ok !== false &&
    testResult.value.success !== false &&
    !testResult.value.error &&
    ['connected', 'success', 'succeeded', 'healthy', 'ready'].includes(status)
  )
})
const connectionBadgeClass = computed(() => (connectionSucceeded.value ? 'success' : 'neutral'))
const connectionSummary = computed(() => {
  const value = testResult.value || activeSavedServer.value
  if (Array.isArray(value?.servers)) {
    const failed = value.servers.find((server: any) => server?.error || ['failed', 'error', 'unreachable'].includes(String(server?.status || '').toLowerCase()))
    if (failed) return String(failed.error || failed.message || failed.status)
    const names = value.servers.map((server: any) => server?.name).filter(Boolean)
    if (names.length) return props.language === 'en' ? `Connected to ${names.join(', ')}` : `已连接 ${names.join('、')}`
  }
  return String(value?.message || value?.summary || value?.last_error || activeStatusLabel.value)
})
const connectionLatency = computed(() => {
  const testedServer = Array.isArray(testResult.value?.servers) ? testResult.value.servers[0] : null
  const value = testResult.value?.latency_ms ?? testedServer?.latency_ms ?? activeSavedServer.value?.latency_ms
  return typeof value === 'number' ? `${Math.round(value)} ms` : '-'
})
const connectionTransport = computed(() => {
  const parsed = parseDraftSilently()
  const config = firstServerConfig(parsed)
  const testedServer = Array.isArray(testResult.value?.servers) ? testResult.value.servers[0] : null
  return String(
    testedServer?.transport ||
      testedServer?.type ||
      config?.type ||
      config?.transport ||
      activeSavedServer.value?.config?.type ||
      activeSavedServer.value?.config?.transport ||
      '-',
  )
})

watch(
  [() => props.servers, () => props.templates],
  () => {
    const stillExists =
      selectedKey.value === '__new__' ||
      configuredServers.value.some((item) => item.name === selectedKey.value) ||
      availableTemplates.value.some((item) => templateKey(item.name) === selectedKey.value)
    if (!stillExists) {
      selectedKey.value = configuredServers.value[0]?.name || templateKey(availableTemplates.value[0]?.name || '') || '__new__'
    }
    if (!draftDirty.value) hydrateDraft()
  },
  { deep: true, immediate: true },
)

function tr(key: string) {
  return labels[props.language]?.[key] || labels.zh[key] || key
}

function templateKey(name: string) {
  return name ? `template:${name}` : ''
}

async function selectServer(name: string) {
  if (!(await canReplaceDraft())) return
  selectedKey.value = name
  hydrateDraft()
  mobileDetailOpen.value = true
}

async function selectTemplate(name: string) {
  if (!(await canReplaceDraft())) return
  selectedKey.value = templateKey(name)
  hydrateDraft()
  mobileDetailOpen.value = true
}

async function startNewServer() {
  if (!(await canReplaceDraft())) return
  selectedKey.value = '__new__'
  draft.value = JSON.stringify(
    {
      mcpServers: {
        'my-mcp-server': {
          type: 'http',
          url: 'https://example.com/mcp',
          headers: {
            Authorization: 'Bearer ${MCP_TOKEN}',
          },
        },
      },
    },
    null,
    2,
  )
  draftDirty.value = true
  validationError.value = ''
  operationError.value = ''
  testResult.value = null
  mobileDetailOpen.value = true
}

async function canReplaceDraft() {
  if (!draftDirty.value) return true
  try {
    await ElMessageBox.confirm(
      props.language === 'en' ? 'Discard the unsaved MCP changes?' : '放弃尚未保存的 MCP 配置修改？',
      props.language === 'en' ? 'Unsaved changes' : '未保存修改',
      { type: 'warning', confirmButtonText: props.language === 'en' ? 'Discard' : '放弃', cancelButtonText: props.language === 'en' ? 'Keep editing' : '继续编辑' },
    )
    return true
  } catch {
    return false
  }
}

function hydrateDraft() {
  const source = activeSavedServer.value || activeTemplate.value
  if (!source) return
  const config = activeSavedServer.value?.config || activeTemplate.value?.config_example || {}
  draft.value = JSON.stringify(toMcpDocument(source.name, config), null, 2)
  draftDirty.value = false
  validationError.value = ''
  operationError.value = ''
  testResult.value = null
}

function toMcpDocument(name: string, rawConfig: Record<string, any>) {
  const cloned = JSON.parse(JSON.stringify(rawConfig || {}))
  if (cloned.mcpServers && typeof cloned.mcpServers === 'object') return cloned
  const config = { ...cloned }
  if (!config.type) {
    config.type = config.transport === 'streamable_http' ? 'http' : config.transport || 'http'
  }
  delete config.transport
  delete config.tools
  delete config.status
  delete config.connection_status
  delete config.latency_ms
  return { mcpServers: { [name]: config } }
}

function onDraftInput() {
  draftDirty.value = true
  validationError.value = ''
  operationError.value = ''
  testResult.value = null
}

function parseDraftSilently(): Record<string, any> | null {
  try {
    const parsed = JSON.parse(draft.value || '{}')
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

function validateDraft() {
  try {
    parseAndValidateDraft()
    validationError.value = ''
    return true
  } catch (error: any) {
    validationError.value = String(error?.message || error)
    return false
  }
}

function parseAndValidateDraft() {
  let parsed: Record<string, any>
  try {
    parsed = JSON.parse(draft.value || '{}')
  } catch (error: any) {
    throw new Error(props.language === 'en' ? `Invalid JSON: ${error.message}` : `JSON 格式错误：${error.message}`)
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(props.language === 'en' ? 'Configuration must be a JSON object.' : 'MCP 配置必须是 JSON 对象。')
  }
  const configs = parsed.mcpServers
  if (configs !== undefined) {
    if (!configs || typeof configs !== 'object' || Array.isArray(configs) || !Object.keys(configs).length) {
      throw new Error(props.language === 'en' ? 'mcpServers must contain at least one server.' : 'mcpServers 至少需要包含一个服务。')
    }
    for (const [name, config] of Object.entries<any>(configs)) validateServerConfig(name, config)
  } else {
    if (!String(parsed.name || '').trim()) {
      throw new Error(props.language === 'en' ? 'A single server config must include name.' : '单服务配置必须包含 name。')
    }
    validateServerConfig(String(parsed.name), parsed)
  }
  return parsed
}

function validateServerConfig(name: string, config: any) {
  if (!name.trim()) throw new Error(props.language === 'en' ? 'Server name is required.' : '服务名称不能为空。')
  if (!config || typeof config !== 'object' || Array.isArray(config)) {
    throw new Error(props.language === 'en' ? `${name} must be an object.` : `${name} 的配置必须是对象。`)
  }
  const transport = String(config.type || config.transport || '').toLowerCase()
  if (!transport) throw new Error(props.language === 'en' ? `${name} is missing type.` : `${name} 缺少 type。`)
  if (['http', 'streamable_http', 'streamable-http'].includes(transport) && !String(config.url || '').trim()) {
    throw new Error(props.language === 'en' ? `${name} is missing url.` : `${name} 缺少 url。`)
  }
  if (transport === 'stdio' && !String(config.command || '').trim()) {
    throw new Error(props.language === 'en' ? `${name} is missing command.` : `${name} 缺少 command。`)
  }
}

function formatDraft() {
  if (!validateDraft()) return
  draft.value = JSON.stringify(parseDraftSilently(), null, 2)
}

async function testConnection() {
  let parsed: Record<string, any>
  try {
    parsed = parseAndValidateDraft()
  } catch (error: any) {
    validationError.value = String(error?.message || error)
    return
  }
  testLoading.value = true
  operationError.value = ''
  try {
    const response = await http.post('/mcp-servers/test', { config: parsed })
    testResult.value = response.data?.result || response.data
    if (!connectionSucceeded.value) {
      operationError.value = connectionSummary.value
    }
  } catch (error: any) {
    testResult.value = null
    operationError.value = requestError(error)
  } finally {
    testLoading.value = false
  }
}

async function saveConfiguration() {
  let parsed: Record<string, any>
  try {
    parsed = parseAndValidateDraft()
  } catch (error: any) {
    validationError.value = String(error?.message || error)
    return
  }
  saveLoading.value = true
  operationError.value = ''
  try {
    const response = await http.post('/mcp-servers', { config: parsed })
    draftDirty.value = false
    const names = extractServerNames(parsed)
    if (names[0]) selectedKey.value = names[0]
    emit('changed', response.data?.settings || response.data)
    ElMessage.success(props.language === 'en' ? 'MCP configuration saved' : 'MCP 配置已保存')
  } catch (error: any) {
    operationError.value = requestError(error)
  } finally {
    saveLoading.value = false
  }
}

async function toggleServer(enabled: boolean) {
  if (!activeSavedServer.value) return
  toggleLoading.value = true
  operationError.value = ''
  try {
    const response = await http.patch(`/mcp-servers/${encodeURIComponent(activeSavedServer.value.name)}`, { enabled })
    emit('changed', response.data?.settings || response.data)
  } catch (error: any) {
    operationError.value = requestError(error)
  } finally {
    toggleLoading.value = false
  }
}

async function deleteServer() {
  if (!activeSavedServer.value) return
  try {
    await ElMessageBox.confirm(tr('deleteConfirm'), `${tr('deleteServer')} ${activeSavedServer.value.name}?`, {
      type: 'warning',
      confirmButtonText: props.language === 'en' ? 'Delete' : '删除',
      cancelButtonText: props.language === 'en' ? 'Cancel' : '取消',
      confirmButtonClass: 'el-button--danger',
    })
  } catch {
    return
  }
  deleteLoading.value = true
  operationError.value = ''
  try {
    const name = activeSavedServer.value.name
    const response = await http.delete(`/mcp-servers/${encodeURIComponent(name)}`)
    selectedKey.value = ''
    draftDirty.value = false
    emit('changed', response.data?.settings || response.data)
    ElMessage.success(props.language === 'en' ? 'MCP server deleted' : 'MCP 服务已删除')
  } catch (error: any) {
    operationError.value = requestError(error)
  } finally {
    deleteLoading.value = false
  }
}

function requestError(error: any) {
  const detail = error?.response?.data?.detail || error?.response?.data?.message
  if (detail && typeof detail === 'object') {
    if (Array.isArray(detail.servers)) {
      const messages = detail.servers
        .map((server: any) => [server?.name, server?.error || server?.message || server?.status].filter(Boolean).join(': '))
        .filter(Boolean)
      if (messages.length) return messages.join('\n')
    }
    return JSON.stringify(detail)
  }
  return String(detail || error?.message || (props.language === 'en' ? 'Request failed' : '请求失败'))
}

function extractServerNames(parsed: Record<string, any> | null) {
  if (!parsed) return []
  if (parsed.mcpServers && typeof parsed.mcpServers === 'object' && !Array.isArray(parsed.mcpServers)) {
    return Object.keys(parsed.mcpServers)
  }
  if (String(parsed.name || '').trim()) return [String(parsed.name).trim()]
  return []
}

function firstServerConfig(parsed: Record<string, any> | null) {
  if (!parsed) return null
  if (parsed.mcpServers && typeof parsed.mcpServers === 'object') return Object.values<any>(parsed.mcpServers)[0] || null
  return parsed
}

function serverStatus(server?: McpServer) {
  if (!server) return 'unconfigured'
  if (!server.enabled) return 'disabled'
  const explicitStatus = String(server.connection_status || server.status || '').toLowerCase()
  if (explicitStatus) return explicitStatus
  if (serverTools(server).length) return 'connected'
  return 'unverified'
}

function statusClass(server?: McpServer) {
  const status = serverStatus(server)
  if (['connected', 'success', 'succeeded', 'healthy', 'ready'].includes(status)) return 'status-dot connected'
  if (['failed', 'error', 'unreachable'].includes(status)) return 'status-dot failed'
  if (status === 'connecting') return 'status-dot connecting'
  return 'status-dot neutral'
}

function statusLabel(server?: McpServer) {
  const status = serverStatus(server)
  if (status === 'disabled') return tr('disabled')
  if (['connected', 'success', 'succeeded', 'healthy', 'ready'].includes(status)) return tr('reachable')
  if (['failed', 'error', 'unreachable'].includes(status)) return props.language === 'en' ? 'Connection failed' : '连接失败'
  if (status === 'connecting') return tr('testing')
  return tr('notVerified')
}

function serverTools(server?: McpServer): McpTool[] {
  const tools = server?.tools || server?.config?.tools || []
  if (!Array.isArray(tools)) return []
  return tools.map((item: McpTool | string) => (typeof item === 'string' ? { name: item } : item)).filter((item: McpTool) => Boolean(item.name))
}

function collectTools(value: any): McpTool[] {
  if (!value) return []
  const collected: McpTool[] = []
  const append = (items: any, server?: string) => {
    if (!Array.isArray(items)) return
    for (const item of items) {
      const normalized = typeof item === 'string' ? { name: item } : { ...item }
      if (normalized.name) collected.push({ ...normalized, server: normalized.server || server })
    }
  }
  append(value.tools, value.name || value.server)
  if (Array.isArray(value.servers)) {
    for (const server of value.servers) append(server?.tools, server?.name)
  } else if (value.servers && typeof value.servers === 'object') {
    for (const [name, server] of Object.entries<any>(value.servers)) append(server?.tools || server, name)
  }
  return collected
}

function formatSchema(tool: McpTool) {
  return JSON.stringify(tool.inputSchema || tool.input_schema || tool.schema || { type: 'object', properties: {} }, null, 2)
}

defineExpose({ canReplaceDraft, selectServer, selectTemplate, startNewServer })
</script>

<style scoped>
.mcp-settings {
  display: grid;
  grid-template-columns: 272px minmax(0, 1fr);
  height: calc(100dvh - 202px);
  min-height: 500px;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-0);
}

.mcp-sidebar {
  display: flex;
  min-width: 0;
  flex-direction: column;
  border-right: 1px solid var(--border);
  background: var(--surface-1);
}

.mcp-sidebar-head,
.mcp-detail-head,
.section-toolbar,
.config-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.mcp-sidebar-head {
  min-height: 62px;
  padding: 10px 12px 10px 14px;
  border-bottom: 1px solid var(--border-soft);
}

.mcp-sidebar-head strong,
.mcp-sidebar-head span {
  display: block;
}

.mcp-sidebar-head strong {
  color: var(--text-strong);
  font-size: 13px;
}

.mcp-sidebar-head span {
  margin-top: 3px;
  color: var(--text-muted);
  font-size: 11px;
}

.mcp-sidebar-head :deep(.el-button) {
  width: 34px;
  height: 34px;
  border-color: var(--border);
  background: var(--surface-2);
}

.mcp-list-scroll,
.mcp-detail-scroll {
  min-height: 0;
  overflow: auto;
}

.mcp-list-scroll {
  padding: 8px;
}

.mcp-list-group + .mcp-list-group {
  margin-top: 18px;
}

.mcp-list-group h3 {
  margin: 0 8px 6px;
  color: var(--text-muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0;
  text-transform: uppercase;
}

.mcp-server-item {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr);
  gap: 9px;
  align-items: center;
  width: 100%;
  min-height: 54px;
  padding: 7px 9px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-main);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.16s ease, background-color 0.16s ease;
}

.mcp-server-item:hover {
  background: var(--surface-hover);
}

.mcp-server-item.active {
  border-color: color-mix(in srgb, var(--accent) 42%, var(--border));
  background: var(--accent-soft);
}

.mcp-server-item:focus-visible,
.mobile-back:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.server-glyph,
.detail-glyph,
.tool-icon {
  display: grid;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-2);
  color: var(--accent);
}

.server-glyph {
  width: 30px;
  height: 30px;
}

.server-glyph.template {
  color: var(--text-muted);
}

.server-copy,
.server-copy strong,
.server-copy small {
  min-width: 0;
}

.server-copy strong,
.server-copy small {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.server-copy strong {
  color: var(--text-strong);
  font-size: 12px;
  font-weight: 650;
}

.server-copy small {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 10px;
}

.status-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 4px;
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

.status-dot.connecting {
  background: var(--accent);
  animation: mcp-pulse 1.2s ease-in-out infinite;
}

.mcp-empty-list {
  display: grid;
  justify-items: start;
  gap: 2px;
  padding: 14px 9px;
  color: var(--text-muted);
  font-size: 12px;
}

.mcp-detail {
  display: flex;
  min-width: 0;
  flex-direction: column;
  background: var(--surface-0);
}

.mcp-detail-head {
  min-height: 62px;
  padding: 9px 14px;
  border-bottom: 1px solid var(--border-soft);
  background: color-mix(in srgb, var(--surface-0) 88%, var(--surface-2));
}

.detail-glyph {
  flex: 0 0 34px;
  width: 34px;
  height: 34px;
}

.detail-title {
  flex: 1;
  min-width: 0;
}

.detail-title strong,
.detail-title span {
  display: block;
}

.detail-title strong {
  overflow: hidden;
  color: var(--text-strong);
  font-size: 14px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-title span {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 11px;
}

.detail-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.enable-control {
  display: flex;
  gap: 7px;
  align-items: center;
  color: var(--text-muted);
  font-size: 11px;
}

.delete-button:hover {
  border-color: var(--el-color-danger);
  color: var(--el-color-danger);
}

.mobile-back {
  display: none;
  place-items: center;
  width: 38px;
  height: 38px;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-main);
  cursor: pointer;
}

.mcp-detail-scroll {
  padding: 18px;
}

.config-section,
.connection-section,
.tools-section {
  max-width: 980px;
}

.connection-section,
.tools-section {
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid var(--border-soft);
}

.section-toolbar {
  align-items: flex-start;
  margin-bottom: 12px;
}

.section-toolbar.compact {
  align-items: center;
}

.section-toolbar h2,
.section-toolbar p {
  margin: 0;
}

.section-toolbar h2 {
  color: var(--text-strong);
  font-size: 14px;
}

.section-toolbar p {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
}

.json-editor-shell {
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-1);
  transition: border-color 0.16s ease, box-shadow 0.16s ease;
}

.json-editor-shell:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}

.json-editor-shell.invalid {
  border-color: var(--el-color-danger);
}

.json-editor-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 32px;
  padding: 0 12px;
  border-bottom: 1px solid var(--border-soft);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 11px;
}

.dirty-mark {
  color: var(--warning);
}

.json-editor {
  display: block;
  width: 100%;
  height: 264px;
  padding: 13px 14px;
  border: 0;
  outline: 0;
  resize: vertical;
  background: transparent;
  color: var(--text-main);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.65;
  tab-size: 2;
}

.secret-note {
  display: flex;
  gap: 7px;
  align-items: flex-start;
  margin: 9px 0 0;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
}

.secret-note .el-icon {
  flex: 0 0 auto;
  margin-top: 2px;
  color: var(--warning);
}

.inline-alert {
  margin-top: 12px;
}

.config-actions {
  justify-content: flex-end;
  margin-top: 14px;
}

.connection-badge {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  min-height: 28px;
  padding: 0 9px;
  border: 1px solid var(--border);
  border-radius: 5px;
  color: var(--text-muted);
  font-size: 11px;
}

.connection-badge.success {
  border-color: color-mix(in srgb, var(--el-color-success) 40%, var(--border));
  background: color-mix(in srgb, var(--el-color-success) 9%, transparent);
  color: var(--el-color-success);
}

.connection-facts {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  margin: 0;
  border-top: 1px solid var(--border-soft);
  border-bottom: 1px solid var(--border-soft);
}

.connection-facts div {
  min-width: 0;
  padding: 11px 12px;
}

.connection-facts div + div {
  border-left: 1px solid var(--border-soft);
}

.connection-facts dt,
.connection-facts dd {
  margin: 0;
}

.connection-facts dt {
  color: var(--text-muted);
  font-size: 10px;
}

.connection-facts dd {
  margin-top: 4px;
  overflow: hidden;
  color: var(--text-strong);
  font-family: var(--font-mono);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-count {
  display: grid;
  place-items: center;
  min-width: 26px;
  height: 24px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-family: var(--font-mono);
  font-size: 11px;
}

.tool-list {
  border-top: 1px solid var(--border-soft);
}

.tool-row {
  border-bottom: 1px solid var(--border-soft);
}

.tool-row summary {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  min-height: 56px;
  padding: 7px 8px;
  cursor: pointer;
  list-style: none;
}

.tool-row summary::-webkit-details-marker {
  display: none;
}

.tool-row summary:hover {
  background: var(--surface-hover);
}

.tool-icon {
  width: 30px;
  height: 30px;
  color: var(--el-color-success);
}

.tool-copy strong,
.tool-copy small {
  display: block;
}

.tool-copy strong {
  color: var(--text-strong);
  font-family: var(--font-mono);
  font-size: 12px;
}

.tool-copy small {
  margin-top: 3px;
  color: var(--text-muted);
  font-size: 11px;
}

.tool-row summary code {
  color: var(--text-muted);
  font-size: 10px;
}

.tool-row pre {
  max-height: 240px;
  margin: 0 8px 10px 48px;
  overflow: auto;
  padding: 12px;
  border: 1px solid var(--border-soft);
  border-radius: 5px;
  background: var(--surface-1);
  color: var(--text-main);
  font-size: 11px;
  line-height: 1.6;
}

.tools-empty {
  display: grid;
  justify-items: center;
  gap: 6px;
  min-height: 128px;
  padding: 20px;
  border-top: 1px solid var(--border-soft);
  color: var(--text-muted);
  text-align: center;
}

.tools-empty .el-icon {
  font-size: 24px;
}

.tools-empty strong {
  color: var(--text-main);
  font-size: 12px;
}

.tools-empty span {
  max-width: 440px;
  font-size: 11px;
  line-height: 1.6;
}

@keyframes mcp-pulse {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 1; }
}

@media (max-width: 1024px) {
  .mcp-settings {
    grid-template-columns: 236px minmax(0, 1fr);
    height: calc(100dvh - 194px);
    min-height: 460px;
  }

  .mcp-detail-scroll {
    padding: 14px;
  }

  .detail-actions > :deep(.el-button span) {
    display: none;
  }
}

@media (max-width: 700px) {
  .mcp-settings {
    display: block;
    height: calc(100dvh - 172px);
    min-height: 460px;
    border-right: 0;
    border-left: 0;
    border-radius: 0;
  }

  .mcp-sidebar,
  .mcp-detail {
    height: 100%;
  }

  .mcp-detail {
    display: none;
  }

  .mcp-settings.detail-open .mcp-sidebar {
    display: none;
  }

  .mcp-settings.detail-open .mcp-detail {
    display: flex;
  }

  .mobile-back {
    display: grid;
    flex: 0 0 38px;
  }

  .detail-glyph {
    display: none;
  }

  .mcp-detail-head {
    min-height: 58px;
    padding: 8px;
  }

  .detail-actions {
    gap: 5px;
  }

  .detail-actions :deep(.el-button),
  .mcp-sidebar-head :deep(.el-button) {
    width: 44px;
    height: 44px;
    padding: 0;
  }

  .enable-control > span {
    display: none;
  }

  .mcp-detail-scroll {
    padding: 12px;
  }

  .section-toolbar {
    gap: 8px;
  }

  .json-editor {
    height: 238px;
    font-size: 12px;
  }

  .config-actions :deep(.el-button) {
    min-height: 44px;
  }

  .connection-facts {
    grid-template-columns: 1fr;
  }

  .connection-facts div + div {
    border-top: 1px solid var(--border-soft);
    border-left: 0;
  }

  .tool-row summary {
    grid-template-columns: 30px minmax(0, 1fr);
  }

  .tool-row summary code {
    display: none;
  }

  .tool-row pre {
    margin-left: 8px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .status-dot.connecting {
    animation: none;
  }
}
</style>
