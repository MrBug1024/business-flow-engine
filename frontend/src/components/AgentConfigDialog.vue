<template>
  <el-dialog
    :model-value="modelValue"
    title="Agent 平台配置"
    width="1040px"
    class="agent-config-dialog"
    @update:model-value="close"
  >
    <div v-if="loading" class="muted">加载中...</div>
    <el-tabs v-else v-model="activeTab" class="config-tabs">
      <el-tab-pane label="LLM 配置" name="llm">
        <section class="grid-two">
          <div class="panel">
            <h4>{{ editingLlmId ? '编辑 LLM' : '新增 LLM' }}</h4>
            <p>Agent 平台自己的模型配置；可绑定给主 Agent 或任意子 Agent。</p>
            <el-input v-model="llmName" size="small" placeholder="名称，如 MiniMax / OpenAI / 本地模型" />
            <el-input v-model="llmModel" size="small" placeholder="模型名，如 MiniMax-M2 / gpt-4.1" />
            <el-input v-model="llmBaseUrl" size="small" placeholder="OpenAI 兼容 base_url" />
            <el-input v-model="llmApiKey" size="small" type="password" show-password placeholder="API Key" />
            <el-input-number v-model="llmTemperature" :min="0" :max="2" :step="0.1" size="small" />
            <div class="actions">
              <el-button v-if="editingLlmId" size="small" text @click="resetLlmForm">取消编辑</el-button>
              <el-button size="small" type="primary" :icon="Connection" @click="saveLlm">保存 LLM</el-button>
            </div>
          </div>

          <div class="panel">
            <h4>已配置 LLM</h4>
            <div v-if="sandbox.defaultLlm" class="resource-row builtin">
              <div>
                <b>{{ sandbox.defaultLlm.name }}</b>
                <span>{{ sandbox.defaultLlm.model }} · {{ sandbox.defaultLlm.api_key_set ? '已配置 Key' : '未配置 Key' }}</span>
              </div>
              <el-tag size="small">默认</el-tag>
            </div>
            <div v-if="!sandbox.llms.length" class="empty">暂无自定义 LLM。</div>
            <div v-for="item in sandbox.llms" :key="item.id" class="resource-row">
              <div>
                <b>{{ item.name }}</b>
                <span>{{ item.model }} · {{ item.base_url }}</span>
              </div>
              <div class="row-actions">
                <el-button size="small" text :icon="Edit" @click="editLlm(item)">编辑</el-button>
                <el-button size="small" text type="danger" :icon="Delete" @click="deleteLlm(item.id)">删除</el-button>
              </div>
            </div>
          </div>
        </section>
      </el-tab-pane>

      <el-tab-pane label="Agent 配置" name="agent">
        <template v-if="config">
          <section class="panel main-panel">
            <div class="panel-head">
              <div>
                <h4>主 Agent</h4>
                <p>主 Agent 默认可普通聊天；只有绑定的资源会进入可用能力。</p>
              </div>
              <el-tag type="success" effect="light">默认启用</el-tag>
            </div>

            <div class="form-grid">
              <el-input v-model="config.main_agent.name" size="small">
                <template #prepend>名称</template>
              </el-input>
              <el-select v-model="config.main_agent.llm_id" size="small" placeholder="选择 LLM">
                <el-option v-for="llm in llmOptions" :key="llm.id || 'default'" :label="llmLabel(llm)" :value="llm.id" />
              </el-select>
              <el-select v-model="config.main_agent.sandbox_id" size="small" placeholder="选择沙箱">
                <el-option v-for="box in sandboxOptions" :key="box.id || 'none'" :label="sandboxLabel(box)" :value="box.id" />
              </el-select>
            </div>
            <el-input
              v-model="config.main_agent.system_prompt"
              type="textarea"
              :autosize="{ minRows: 4, maxRows: 10 }"
              placeholder="追加给主 Agent 的 System Prompt；留空则只使用平台默认提示词。"
              class="prompt-input"
            />

            <ResourceChecks
              v-model:skills="config.main_agent.enabled_skills"
              v-model:mcps="config.main_agent.enabled_mcps"
              :skills-list="sandbox.skills"
              :mcps-list="sandbox.connectedMcps"
            />

            <div v-if="config.subagents.length" class="sub-select">
              <div class="check-title">可调度子 Agent</div>
              <el-checkbox-group v-model="config.main_agent.enabled_subagents">
                <el-checkbox v-for="sub in config.subagents" :key="sub.id" :label="sub.id">
                  {{ sub.name }}
                </el-checkbox>
              </el-checkbox-group>
            </div>
          </section>

          <section class="subagents">
            <div class="subagents-head">
              <div>
                <h4>子 Agent</h4>
                <p>每个子 Agent 可独立设置 LLM、System Prompt、Skill 和 MCP。</p>
              </div>
              <el-button size="small" type="primary" :icon="Plus" @click="addSubagent">新建子 Agent</el-button>
            </div>

            <div v-if="!config.subagents.length" class="empty-sub">暂无子 Agent。</div>

            <div v-for="(sub, idx) in config.subagents" :key="sub.id" class="panel sub-panel">
              <div class="sub-row">
                <el-input v-model="sub.name" size="small">
                  <template #prepend>名称</template>
                </el-input>
                <el-select v-model="sub.llm_id" size="small" placeholder="选择 LLM">
                  <el-option v-for="llm in llmOptions" :key="llm.id || 'default'" :label="llmLabel(llm)" :value="llm.id" />
                </el-select>
                <el-select v-model="sub.sandbox_id" size="small" placeholder="选择沙箱">
                  <el-option v-for="box in sandboxOptions" :key="box.id || 'none'" :label="sandboxLabel(box)" :value="box.id" />
                </el-select>
                <el-button size="small" text type="danger" :icon="Delete" @click="removeSubagent(idx)">删除</el-button>
              </div>
              <el-input
                v-model="sub.system_prompt"
                type="textarea"
                :autosize="{ minRows: 3, maxRows: 8 }"
                placeholder="这个子 Agent 的职责、边界、输出要求。"
                class="prompt-input"
              />
              <ResourceChecks
                v-model:skills="sub.enabled_skills"
                v-model:mcps="sub.enabled_mcps"
                :skills-list="sandbox.skills"
                :mcps-list="sandbox.connectedMcps"
              />
            </div>
          </section>

          <section class="default-prompt">
            <details>
              <summary>查看平台默认主 Agent System Prompt</summary>
              <pre>{{ config.main_agent.default_system_prompt }}</pre>
            </details>
          </section>
        </template>
      </el-tab-pane>

      <el-tab-pane label="沙箱环境" name="sandbox">
        <section class="grid-two">
          <div class="panel">
            <h4>新增沙箱</h4>
            <p>沙箱使用平台托管的独立运行环境；绑定的 Skill/MCP 依赖会安装到这里。</p>
            <el-input v-model="sandboxName" size="small" placeholder="名称，如 数据分析沙箱" />
            <div class="actions">
              <el-button size="small" type="primary" :icon="Plus" :loading="savingSandbox" @click="saveSandbox">创建沙箱</el-button>
            </div>
          </div>

          <div class="panel">
            <h4>已配置沙箱</h4>
            <div v-if="sandbox.defaultSandbox" class="resource-row builtin">
              <div>
                <b>{{ sandbox.defaultSandbox.name }}</b>
                <span>{{ sandbox.defaultSandbox.error }}</span>
              </div>
              <el-tag size="small">默认</el-tag>
            </div>
            <div v-if="!sandbox.sandboxes.length" class="empty">暂无沙箱。创建后可绑定给主 Agent 或子 Agent。</div>
            <div v-for="box in sandbox.sandboxes" :key="box.id" class="resource-row">
              <div>
                <b>{{ box.name }}</b>
                <span>{{ box.storage_label || '平台托管' }}</span>
                <span>{{ sandboxDependencySummary(box) }}</span>
                <span v-if="box.error">{{ box.error }}</span>
              </div>
              <div class="row-actions">
                <el-tag size="small" :type="sandboxTagType(box.status)">
                  {{ sandboxStatusText(box.status) }}
                </el-tag>
                <el-button
                  size="small"
                  text
                  :icon="Connection"
                  :loading="installingSandboxId === box.id"
                  @click="installSandbox(box.id)"
                >
                  安装依赖
                </el-button>
                <el-button size="small" text type="danger" :icon="Delete" @click="deleteSandbox(box.id)">删除</el-button>
              </div>
            </div>
          </div>
        </section>
      </el-tab-pane>

      <el-tab-pane label="Skill 配置" name="skill">
        <section class="panel">
          <div class="panel-head">
            <div>
              <h4>Skill 管理</h4>
              <p>上传完整 Skill 文件夹或 zip，结构应包含 SKILL.md 和可选 scripts/ 等资源。</p>
            </div>
            <div class="actions">
              <input ref="skillFolderInput" type="file" multiple webkitdirectory directory class="hidden-file" @change="uploadSkillFolder" />
              <input ref="skillZipInput" type="file" accept=".zip" class="hidden-file" @change="uploadSkillZip" />
              <el-button size="small" :icon="FolderOpened" :loading="uploadingSkill" @click="skillFolderInput?.click()">上传文件夹</el-button>
              <el-button size="small" :icon="Upload" :loading="uploadingSkill" @click="skillZipInput?.click()">上传 zip</el-button>
            </div>
          </div>
          <div v-if="!sandbox.skills.length" class="empty">暂无已上传 Skill。</div>
          <template v-else>
            <div class="bulk-toolbar">
              <el-checkbox :model-value="allSkillsSelected" :indeterminate="someSkillsSelected" @change="toggleAllSkillSelection">
                全选
              </el-checkbox>
              <el-button size="small" text type="danger" :icon="Delete" :disabled="!selectedSkillIds.length" @click="deleteSelectedSkills">
                删除选中{{ selectedSkillIds.length ? ` (${selectedSkillIds.length})` : '' }}
              </el-button>
            </div>
            <el-checkbox-group v-model="selectedSkillIds" class="resource-select-list">
              <div v-for="item in sandbox.skills" :key="item.id" class="resource-row selectable">
                <el-checkbox :label="item.id" class="row-checkbox">
                  <span class="sr-only">选择 {{ item.name }}</span>
                </el-checkbox>
                <div>
                  <b>{{ item.name }}</b>
                  <span>{{ item.description || item.id }}</span>
                  <span v-if="skillDependencySummary(item)">{{ skillDependencySummary(item) }}</span>
                  <span v-if="item.warnings?.length">{{ item.warnings.join('；') }}</span>
                </div>
                <el-button size="small" text type="danger" :icon="Delete" @click.stop="deleteSkill(item.id)">删除</el-button>
              </div>
            </el-checkbox-group>
          </template>
        </section>
      </el-tab-pane>

      <el-tab-pane label="MCP 配置" name="mcp">
        <section class="grid-two">
          <div class="panel">
            <h4>{{ editingMcpId ? '编辑 MCP' : '新增 MCP' }}</h4>
            <p>这里配置的是 Agent 平台自己的 MCP 服务，不依赖蒸馏平台发布通道。</p>
            <el-alert
              v-if="!sandbox.mcpAdapter.available"
              type="warning"
              show-icon
              :closable="false"
              title="当前 Python 环境暂不能连接 MCP"
              :description="sandbox.mcpAdapter.error || '请在运行 run.py 的同一个环境安装 langchain-mcp-adapters。'"
              class="mcp-adapter-alert"
            />
            <el-input v-model="mcpName" size="small" placeholder="MCP 名称" />
            <el-input
              v-model="mcpConfigText"
              type="textarea"
              :autosize="{ minRows: 8, maxRows: 14 }"
              placeholder='{"mcpServers":{"server":{"command":"npx","args":["-y","your-mcp"]}}}'
            />
            <div class="actions">
              <el-button v-if="editingMcpId" size="small" text @click="resetMcpForm">取消编辑</el-button>
              <el-button size="small" type="primary" :icon="Link" :loading="savingMcp" @click="saveMcp">
                {{ editingMcpId ? '重新连接' : '安装/连接' }}
              </el-button>
            </div>
          </div>

          <div class="panel">
            <h4>已配置 MCP</h4>
            <div v-if="!sandbox.mcps.length" class="empty">暂无 MCP 配置。</div>
            <div v-for="item in sandbox.mcps" :key="item.id" class="resource-row">
              <div>
                <b>{{ item.name }}</b>
                <span v-if="item.status === 'connected'">{{ item.tools?.length || 0 }} 个工具：{{ (item.tools || []).slice(0, 4).join('、') }}</span>
                <span v-else-if="item.status === 'configured'">已保存；运行时会在绑定 Agent 的沙箱中加载。{{ item.error || '' }}</span>
                <span v-else>{{ item.error || '连接异常' }}</span>
              </div>
              <div class="row-actions">
                <el-tag size="small" :type="mcpTagType(item.status)">
                  {{ mcpStatusText(item.status) }}
                </el-tag>
                <el-button size="small" text :icon="Edit" @click="editMcp(item)">编辑</el-button>
                <el-button size="small" text type="danger" :icon="Delete" @click="deleteMcp(item.id)">删除</el-button>
              </div>
            </div>
          </div>
        </section>
      </el-tab-pane>
    </el-tabs>

    <template #footer>
      <el-button @click="close(false)">关闭</el-button>
      <el-button v-if="activeTab === 'agent'" type="primary" :loading="saving" @click="saveAgentConfig">保存 Agent 配置</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, ref, watch, type PropType } from 'vue'
import { ElButton, ElCheckbox, ElCheckboxGroup, ElMessage, ElMessageBox } from 'element-plus'
import { Connection, Delete, Edit, FolderOpened, Link, Plus, Upload } from '@element-plus/icons-vue'
import type { LlmResource, McpResource, PlaygroundAgentConfig, SandboxResource, SkillResource } from '@/api/types'
import { useSandboxStore } from '@/stores/sandbox'

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()
const sandbox = useSandboxStore()

const activeTab = ref('agent')
const loading = ref(false)
const saving = ref(false)
const config = ref<PlaygroundAgentConfig | null>(null)
const llmOptions = computed(() => sandbox.llmOptions)
const sandboxOptions = computed(() => sandbox.sandboxOptions)

const editingLlmId = ref('')
const llmName = ref('')
const llmModel = ref('')
const llmBaseUrl = ref('')
const llmApiKey = ref('')
const llmTemperature = ref(0)

const skillFolderInput = ref<HTMLInputElement>()
const skillZipInput = ref<HTMLInputElement>()
const uploadingSkill = ref(false)
const selectedSkillIds = ref<string[]>([])
const skillIds = computed(() => sandbox.skills.map((item) => item.id))
const allSkillsSelected = computed(() => skillIds.value.length > 0 && selectedSkillIds.value.length === skillIds.value.length)
const someSkillsSelected = computed(() => selectedSkillIds.value.length > 0 && !allSkillsSelected.value)

const sandboxName = ref('')
const savingSandbox = ref(false)
const installingSandboxId = ref('')

const editingMcpId = ref('')
const mcpName = ref('')
const mcpConfigText = ref('')
const savingMcp = ref(false)

const ResourceChecks = defineComponent({
  props: {
    skills: { type: Array as PropType<string[]>, required: true },
    mcps: { type: Array as PropType<string[]>, required: true },
    skillsList: { type: Array as PropType<SkillResource[]>, required: true },
    mcpsList: { type: Array as PropType<McpResource[]>, required: true },
  },
  emits: ['update:skills', 'update:mcps'],
  setup(p, { emit }) {
    const updateList = (event: 'update:skills' | 'update:mcps', value: unknown) => {
      emit(event, (Array.isArray(value) ? value : [value]).filter(Boolean).map(String))
    }
    const resourceHeader = (
      title: string,
      event: 'update:skills' | 'update:mcps',
      ids: string[],
    ) => h('div', { class: 'check-head' }, [
      h('div', { class: 'check-title' }, title),
      ids.length
        ? h('div', { class: 'check-actions' }, [
            h(ElButton, { size: 'small', text: true, onClick: () => updateList(event, ids) }, () => '全选'),
            h(ElButton, { size: 'small', text: true, onClick: () => updateList(event, []) }, () => '清空'),
          ])
        : null,
    ])
    const option = (name: string, desc: string) => [
      h('span', { class: 'resource-option-label' }, name),
      desc ? h('span', { class: 'resource-option-desc' }, desc) : null,
    ]
    return () => h('div', { class: 'resource-checks' }, [
      h('div', { class: 'check-block' }, [
        resourceHeader('Skill', 'update:skills', p.skillsList.map((item) => item.id)),
        p.skillsList.length
          ? h(ElCheckboxGroup, {
              modelValue: p.skills,
              'onUpdate:modelValue': (v: unknown) => updateList('update:skills', v),
            }, () => p.skillsList.map((item) => h(ElCheckbox, {
              key: `skill-${item.id}`,
              label: item.id,
            }, () => option(item.name || item.id, item.description || ''))))
          : h('div', { class: 'check-empty' }, '暂无已上传 Skill'),
      ]),
      h('div', { class: 'check-block' }, [
        resourceHeader('MCP', 'update:mcps', p.mcpsList.map((item) => item.id)),
        p.mcpsList.length
          ? h(ElCheckboxGroup, {
              modelValue: p.mcps,
              'onUpdate:modelValue': (v: unknown) => updateList('update:mcps', v),
            }, () => p.mcpsList.map((item) => h(ElCheckbox, {
              key: `mcp-${item.id}`,
              label: item.id,
            }, () => option(item.name || item.id, `${item.tools?.length || 0} 个工具`))))
          : h('div', { class: 'check-empty' }, '暂无已连接 MCP'),
      ]),
    ])
  },
})

watch(
  () => props.modelValue,
  async (open) => {
    if (!open) return
    loading.value = true
    try {
      await sandbox.loadResources()
      await reloadAgentConfig()
    } finally {
      loading.value = false
    }
  },
)

watch(
  skillIds,
  (ids) => {
    const allowed = new Set(ids)
    selectedSkillIds.value = selectedSkillIds.value.filter((id) => allowed.has(id))
  },
)

async function reloadAgentConfig() {
  const cfg = await sandbox.loadAgentConfig()
  config.value = normalizeConfig(JSON.parse(JSON.stringify(cfg)))
}

function normalizeConfig(raw: PlaygroundAgentConfig): PlaygroundAgentConfig {
  raw.main_agent.llm_id = raw.main_agent.llm_id || ''
  raw.main_agent.sandbox_id = raw.main_agent.sandbox_id || ''
  raw.main_agent.enabled_subagents = raw.main_agent.enabled_subagents || []
  raw.subagents = (raw.subagents || []).map((sub) => ({
    ...sub,
    llm_id: sub.llm_id || '',
    sandbox_id: sub.sandbox_id || '',
    enabled_skills: sub.enabled_skills || [],
    enabled_mcps: sub.enabled_mcps || [],
  }))
  return raw
}

function llmLabel(llm: LlmResource) {
  return `${llm.name} · ${llm.model || '未设置模型'}`
}

function sandboxLabel(box: SandboxResource) {
  if (!box.id) return box.name
  return `${box.name} · ${sandboxStatusText(box.status)}`
}

function sandboxStatusText(status = '') {
  if (status === 'ready') return '已就绪'
  if (status === 'installing') return '安装中'
  if (status === 'error') return '异常'
  if (status === 'new') return '未安装'
  return status || '未知'
}

function sandboxTagType(status = '') {
  if (status === 'ready') return 'success'
  if (status === 'installing' || status === 'new') return 'warning'
  if (status === 'error') return 'danger'
  return 'info'
}

function mcpStatusText(status = '') {
  if (status === 'connected') return '已连接'
  if (status === 'configured') return '已保存'
  if (status === 'error') return '异常'
  return status || '未知'
}

function mcpTagType(status = '') {
  if (status === 'connected') return 'success'
  if (status === 'configured') return 'warning'
  if (status === 'error') return 'danger'
  return 'info'
}

function skillDependencySummary(item: SkillResource) {
  const py = item.dependencies?.python?.length || 0
  const node = Object.keys(item.dependencies?.node || {}).length
  const files = item.dependencies?.files || []
  if (!py && !node && !files.length) return ''
  return `依赖：Python ${py} 个，Node ${node} 个；声明文件：${files.join('、') || '无'}`
}

function sandboxDependencySummary(box: SandboxResource) {
  const deps = box.dependencies || {}
  const py = deps.python?.length || 0
  const node = Object.keys(deps.node || {}).length
  const skills = deps.skills?.length || 0
  const mcps = deps.mcps?.length || 0
  return `已绑定依赖：Skill ${skills} 个，MCP ${mcps} 个；Python ${py} 个，Node ${node} 个`
}

function addSubagent() {
  if (!config.value) return
  const id = `sub_${Date.now().toString(36)}`
  config.value.subagents.push({
    id,
    name: `子 Agent ${config.value.subagents.length + 1}`,
    system_prompt: '',
    llm_id: '',
    sandbox_id: '',
    enabled_skills: [],
    enabled_mcps: [],
  })
  config.value.main_agent.enabled_subagents = [
    ...(config.value.main_agent.enabled_subagents || []),
    id,
  ]
}

function removeSubagent(index: number) {
  if (!config.value) return
  const [removed] = config.value.subagents.splice(index, 1)
  config.value.main_agent.enabled_subagents = (config.value.main_agent.enabled_subagents || [])
    .filter((id) => id !== removed?.id)
}

async function saveAgentConfig() {
  if (!config.value) return
  saving.value = true
  try {
    await sandbox.saveAgentConfig(config.value)
    ElMessage.success('Agent 配置已保存')
  } finally {
    saving.value = false
  }
}

function editLlm(item: LlmResource) {
  editingLlmId.value = item.id
  llmName.value = item.name
  llmModel.value = item.model
  llmBaseUrl.value = item.base_url
  llmApiKey.value = item.api_key || ''
  llmTemperature.value = item.temperature ?? 0
}

function resetLlmForm() {
  editingLlmId.value = ''
  llmName.value = ''
  llmModel.value = ''
  llmBaseUrl.value = ''
  llmApiKey.value = ''
  llmTemperature.value = 0
}

async function saveLlm() {
  if (!llmName.value.trim() || !llmModel.value.trim() || !llmBaseUrl.value.trim()) {
    ElMessage.warning('请填写 LLM 名称、模型和 base_url')
    return
  }
  await sandbox.saveLlm({
    id: editingLlmId.value || undefined,
    name: llmName.value.trim(),
    model: llmModel.value.trim(),
    base_url: llmBaseUrl.value.trim(),
    api_key: llmApiKey.value.trim(),
    temperature: llmTemperature.value,
  })
  await reloadAgentConfig()
  resetLlmForm()
  ElMessage.success('LLM 已保存')
}

async function deleteLlm(id: string) {
  try {
    await ElMessageBox.confirm('确定删除这个 LLM？已绑定的 Agent 会回落到平台默认 LLM。', '提示', { type: 'warning' })
    await sandbox.deleteLlm(id)
    await reloadAgentConfig()
    if (editingLlmId.value === id) resetLlmForm()
    ElMessage.success('LLM 已删除')
  } catch { /* cancelled */ }
}

type SkillUploadEntry = {
  file: File
  path: string
}

type SkillUploadGroup = {
  name: string
  entries: SkillUploadEntry[]
}

function splitPath(path: string) {
  return path.replace(/\\/g, '/').split('/').filter(Boolean)
}

function dirname(path: string) {
  const parts = splitPath(path)
  parts.pop()
  return parts.join('/')
}

function basename(path: string) {
  const parts = splitPath(path)
  return parts[parts.length - 1] || ''
}

function pathInRoot(path: string, root: string) {
  return !root || path === root || path.startsWith(`${root}/`)
}

function pathRelativeToRoot(path: string, root: string) {
  return root ? path.slice(root.length).replace(/^\/+/, '') : path
}

function skillUploadGroups(files: File[]): SkillUploadGroup[] {
  const entries = files.map((file) => ({
    file,
    path: ((file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name).replace(/\\/g, '/'),
  }))
  const roots = Array.from(new Set(
    entries
      .filter((entry) => basename(entry.path).toLowerCase() === 'skill.md')
      .map((entry) => dirname(entry.path)),
  )).sort((a, b) => b.length - a.length)

  if (!roots.length) {
    return [{ name: '', entries }]
  }

  const groups = new Map<string, SkillUploadEntry[]>()
  for (const root of roots) groups.set(root, [])

  for (const entry of entries) {
    const root = roots.find((candidate) => pathInRoot(entry.path, candidate))
    if (!root) continue
    groups.get(root)?.push({
      file: entry.file,
      path: pathRelativeToRoot(entry.path, root),
    })
  }

  return Array.from(groups.entries())
    .map(([root, groupEntries]) => ({
      name: basename(root),
      entries: groupEntries,
    }))
    .filter((group) => group.entries.length)
}

async function uploadSkillFolder(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  if (!files.length) return
  uploadingSkill.value = true
  try {
    const groups = skillUploadGroups(files)
    for (const group of groups) {
      const form = new FormData()
      for (const entry of group.entries) {
        form.append('files', entry.file)
        form.append('paths', entry.path)
      }
      if (group.name) form.append('name', group.name)
      await sandbox.uploadSkill(form)
    }
    await reloadAgentConfig()
    ElMessage.success(groups.length > 1 ? `已上传 ${groups.length} 个 Skill` : 'Skill 已上传')
  } finally {
    uploadingSkill.value = false
    input.value = ''
  }
}

async function uploadSkillZip(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploadingSkill.value = true
  try {
    const form = new FormData()
    form.append('files', file)
    await sandbox.uploadSkill(form)
    await reloadAgentConfig()
    ElMessage.success('Skill 已上传')
  } finally {
    uploadingSkill.value = false
    input.value = ''
  }
}

function toggleAllSkillSelection(checked: string | number | boolean) {
  selectedSkillIds.value = checked ? [...skillIds.value] : []
}

async function deleteSkill(id: string) {
  try {
    await ElMessageBox.confirm('确定删除这个 Skill？已绑定的 Agent 会自动解绑。', '提示', { type: 'warning' })
    await sandbox.deleteSkill(id)
    selectedSkillIds.value = selectedSkillIds.value.filter((selectedId) => selectedId !== id)
    await reloadAgentConfig()
    ElMessage.success('Skill 已删除')
  } catch { /* cancelled */ }
}

async function deleteSelectedSkills() {
  const ids = [...selectedSkillIds.value]
  if (!ids.length) return
  try {
    await ElMessageBox.confirm(`确定删除选中的 ${ids.length} 个 Skill？已绑定的 Agent 会自动解绑。`, '提示', { type: 'warning' })
    for (const id of ids) {
      await sandbox.deleteSkill(id)
    }
    selectedSkillIds.value = []
    await reloadAgentConfig()
    ElMessage.success(`已删除 ${ids.length} 个 Skill`)
  } catch { /* cancelled */ }
}

async function saveSandbox() {
  if (!sandboxName.value.trim()) {
    ElMessage.warning('请填写沙箱名称')
    return
  }
  savingSandbox.value = true
  try {
    await sandbox.saveSandbox({
      name: sandboxName.value.trim(),
    })
    sandboxName.value = ''
    await reloadAgentConfig()
    ElMessage.success('沙箱已创建')
  } finally {
    savingSandbox.value = false
  }
}

async function installSandbox(id: string) {
  installingSandboxId.value = id
  try {
    const item = await sandbox.installSandbox(id, config.value)
    await reloadAgentConfig()
    if (item.status === 'ready') {
      ElMessage.success('沙箱依赖已安装')
    } else {
      ElMessage.warning(item.error || '沙箱依赖安装失败')
    }
  } finally {
    installingSandboxId.value = ''
  }
}

async function deleteSandbox(id: string) {
  try {
    await ElMessageBox.confirm('确定删除这个沙箱？已绑定的 Agent 会自动取消选择。', '提示', { type: 'warning' })
    await sandbox.deleteSandbox(id)
    await reloadAgentConfig()
    ElMessage.success('沙箱已删除')
  } catch { /* cancelled */ }
}

function editMcp(item: McpResource) {
  editingMcpId.value = item.id
  mcpName.value = item.name
  mcpConfigText.value = JSON.stringify(item.config || {}, null, 2)
}

function resetMcpForm() {
  editingMcpId.value = ''
  mcpName.value = ''
  mcpConfigText.value = ''
}

async function saveMcp() {
  if (!mcpName.value.trim()) {
    ElMessage.warning('请填写 MCP 名称')
    return
  }
  let parsed: any
  try {
    parsed = JSON.parse(mcpConfigText.value || '{}')
  } catch {
    ElMessage.error('MCP 配置不是合法 JSON')
    return
  }
  savingMcp.value = true
  try {
    const item = await sandbox.saveMcp({
      id: editingMcpId.value || undefined,
      name: mcpName.value.trim(),
      config: parsed,
    })
    await reloadAgentConfig()
    if (item.status === 'connected') {
      ElMessage.success('MCP 已连接')
      resetMcpForm()
    } else if (item.status === 'configured') {
      ElMessage.warning(item.error ? `MCP 已保存，运行时按沙箱加载：${item.error}` : 'MCP 已保存，运行时按沙箱加载')
      resetMcpForm()
    } else {
      ElMessage.warning(item.error || 'MCP 连接异常，已保存状态')
    }
  } finally {
    savingMcp.value = false
  }
}

async function deleteMcp(id: string) {
  try {
    await ElMessageBox.confirm('确定删除这个 MCP？已绑定的 Agent 会自动解绑。', '提示', { type: 'warning' })
    await sandbox.deleteMcp(id)
    await reloadAgentConfig()
    if (editingMcpId.value === id) resetMcpForm()
    ElMessage.success('MCP 已删除')
  } catch { /* cancelled */ }
}

function close(v = false) {
  emit('update:modelValue', v)
}
</script>

<style scoped lang="scss">
.config-tabs { min-height: 560px; }
.panel {
  border: 1px solid var(--border);
  background: var(--surface-2);
  border-radius: var(--r-sm);
  padding: 13px;
}
.panel + .panel { margin-top: 10px; }
.grid-two {
  display: grid;
  grid-template-columns: minmax(280px, 0.8fr) minmax(320px, 1.2fr);
  gap: 14px;
}
.panel-head,
.subagents-head,
.sub-row,
.resource-row,
.actions,
.row-actions {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
h4 {
  margin: 0;
  color: var(--text-1);
  font-size: var(--text-base);
}
p {
  margin: 4px 0 10px;
  color: var(--text-3);
  font-size: var(--text-sm);
  line-height: 1.55;
}
.panel :deep(.el-input),
.panel :deep(.el-input-number),
.panel :deep(.el-textarea) {
  margin-top: 8px;
  width: 100%;
}
.prompt-input { margin-top: 10px; }
.mcp-adapter-alert { margin: 8px 0 2px; }
.actions {
  justify-content: flex-end;
  align-items: center;
  margin-top: 10px;
  flex-wrap: wrap;
}
.hidden-file { display: none; }
.resource-row {
  align-items: center;
  border: 1px solid var(--border);
  background: var(--surface);
  border-radius: var(--r-sm);
  padding: 10px 12px;
  margin-top: 8px;
}
.bulk-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 10px;
  padding: 0 2px;
}
.resource-select-list {
  display: flex;
  flex-direction: column;
}
.resource-row.selectable {
  justify-content: flex-start;
}
.resource-row.selectable > div:nth-child(2) {
  flex: 1;
  min-width: 0;
}
.row-checkbox {
  flex-shrink: 0;
  margin-right: 0;
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
.resource-row.builtin { background: var(--info-soft); }
.resource-row b {
  display: block;
  color: var(--text-1);
  font-size: var(--text-sm);
}
.resource-row span {
  display: block;
  color: var(--text-3);
  font-size: var(--text-xs);
  margin-top: 3px;
  word-break: break-all;
}
.row-actions {
  align-items: center;
  flex-shrink: 0;
}
.empty,
.empty-sub {
  color: var(--text-3);
  font-size: var(--text-sm);
  border: 1px dashed var(--border);
  border-radius: var(--r-sm);
  padding: 13px;
  margin-top: 10px;
}
.subagents { margin-top: 14px; }
.sub-panel { margin-top: 10px; background: var(--surface); }
.sub-row {
  align-items: center;
}
.sub-row :deep(.el-input),
.sub-row :deep(.el-select) {
  margin-top: 0;
}
.sub-select { margin-top: 12px; }
.default-prompt { margin-top: 14px; }
.default-prompt summary {
  cursor: pointer;
  color: var(--text-2);
  font-size: var(--text-sm);
  font-weight: 700;
}
.default-prompt pre {
  margin: 10px 0 0;
  max-height: 220px;
  overflow: auto;
  white-space: pre-wrap;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.55;
  background: var(--code-bg);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  padding: 10px;
}
:deep(.resource-checks) {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  margin-top: 12px;
}
:deep(.check-block) {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  padding: 10px;
}
:deep(.check-head) {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}
:deep(.check-title) {
  font-size: var(--text-xs);
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 800;
}
:deep(.check-actions) {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
}
:deep(.el-checkbox-group) {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
:deep(.el-checkbox__label) {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
:deep(.resource-option-label) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
:deep(.resource-option-desc) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-3);
  font-size: var(--text-xs);
}
:deep(.check-empty) {
  color: var(--text-3);
  font-size: var(--text-sm);
}
@media (max-width: 900px) {
  .grid-two,
  .form-grid,
  :deep(.resource-checks) {
    grid-template-columns: 1fr;
  }
  .sub-row {
    flex-direction: column;
    align-items: stretch;
  }
}
</style>
