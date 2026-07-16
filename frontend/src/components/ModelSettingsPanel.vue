<template>
  <section class="model-settings">
    <header class="panel-head">
      <div>
        <h2>{{ tr('title') }}</h2>
        <p>{{ tr('subtitle') }}</p>
      </div>
      <div class="head-actions">
        <el-select v-model="selectedModel" :aria-label="tr('active')" @change="activateModel">
          <el-option
            v-for="model in enabledModels"
            :key="model.id"
            :label="model.name"
            :value="model.model"
          />
        </el-select>
        <el-button v-if="!formOpen" type="primary" :icon="Plus" @click="formOpen = true">{{ tr('add') }}</el-button>
        <el-button v-else :icon="Close" circle :aria-label="tr('cancel')" @click="closeForm" />
      </div>
    </header>

    <el-alert
      v-if="operationError"
      class="operation-alert"
      type="error"
      show-icon
      :closable="true"
      :title="operationError"
      @close="operationError = ''"
    />

    <form v-if="formOpen" class="model-form" @submit.prevent="addModel">
      <header>
        <div>
          <h3>{{ tr('newTitle') }}</h3>
          <p>{{ tr('newHelp') }}</p>
        </div>
      </header>
      <div class="form-grid">
        <label>
          <span>{{ tr('name') }}</span>
          <el-input v-model="draft.name" :placeholder="tr('namePlaceholder')" />
        </label>
        <label>
          <span>{{ tr('modelId') }}</span>
          <el-input v-model="draft.model" placeholder="gpt-4.1" />
        </label>
        <label class="base-url-field">
          <span>Base URL</span>
          <el-input v-model="draft.base_url" placeholder="https://api.example.com/v1" />
        </label>
        <label class="api-key-field">
          <span>{{ tr('apiKey') }}</span>
          <el-input
            v-model="draft.api_key"
            type="password"
            show-password
            autocomplete="new-password"
            :placeholder="tr('apiKeyPlaceholder')"
          />
        </label>
      </div>
      <div class="form-actions">
        <el-button @click="closeForm">{{ tr('cancel') }}</el-button>
        <el-button type="primary" native-type="submit" :loading="saving" :disabled="!canAddModel">
          {{ tr('save') }}
        </el-button>
      </div>
    </form>

    <div class="model-scroll">
      <div class="model-grid">
        <article v-for="model in models" :key="model.id" class="model-card" :class="{ active: model.model === selectedModel }">
          <header class="model-card-head">
            <span class="model-icon"><el-icon><Cpu /></el-icon></span>
            <div class="model-title">
              <strong :title="model.name">{{ model.name }}</strong>
              <span :title="model.model">{{ model.model }}</span>
            </div>
            <el-tooltip v-if="!model.default" :content="tr('delete')" placement="top">
              <el-button
                :icon="Delete"
                text
                circle
                class="delete-button"
                :loading="deletingId === model.id"
                :aria-label="tr('delete')"
                @click="deleteModel(model)"
              />
            </el-tooltip>
            <el-tooltip v-else :content="tr('defaultLocked')" placement="top">
              <span class="locked-action" :aria-label="tr('defaultLocked')"><el-icon><Lock /></el-icon></span>
            </el-tooltip>
          </header>

          <div class="model-meta">
            <span>{{ model.provider }}</span>
            <span v-if="model.default" class="default-badge"><el-icon><Lock /></el-icon>{{ tr('systemDefault') }}</span>
            <span v-else-if="model.model === selectedModel" class="active-badge"><el-icon><CircleCheck /></el-icon>{{ tr('active') }}</span>
          </div>

          <p class="model-endpoint" :title="model.base_url || tr('inheritedEndpoint')">
            {{ model.base_url || tr('inheritedEndpoint') }}
          </p>

          <footer class="model-card-foot">
            <span>{{ model.default ? tr('managedBySystem') : model.enabled ? tr('enabled') : tr('disabled') }}</span>
            <el-switch
              :model-value="model.enabled"
              :disabled="model.default"
              :loading="togglingId === model.id"
              :aria-label="model.enabled ? tr('enabled') : tr('disabled')"
              @change="toggleModel(model, Boolean($event))"
            />
          </footer>
        </article>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheck, Close, Cpu, Delete, Lock, Plus } from '@element-plus/icons-vue'
import { http } from '@/api/http'

type Language = 'zh' | 'en'
type ModelConfig = {
  id: string
  name: string
  provider: string
  model: string
  base_url?: string
  api_key?: string
  enabled: boolean
  default?: boolean
}

const props = withDefaults(
  defineProps<{
    models: ModelConfig[]
    activeModel: string
    language?: Language
  }>(),
  { language: 'zh' },
)

const emit = defineEmits<{ changed: [settings?: any] }>()

const copy: Record<Language, Record<string, string>> = {
  zh: {
    active: '当前使用',
    add: '添加模型',
    apiKey: 'API Key（可选）',
    apiKeyPlaceholder: '留空时使用服务端环境变量',
    cancel: '取消',
    defaultLocked: '系统默认模型不可删除',
    delete: '删除模型',
    deleteBody: '删除后，该模型将不再出现在对话模型列表中。',
    disabled: '已停用',
    enabled: '已启用',
    inheritedEndpoint: '使用系统 Base URL',
    managedBySystem: '系统管理',
    modelId: '模型 ID',
    name: '显示名称',
    namePlaceholder: '例如：业务分析模型',
    newHelp: '添加 OpenAI 兼容模型；独立密钥只在服务端保存，返回前会自动掩码。',
    newTitle: '新模型',
    save: '保存并使用',
    subtitle: '选择对话模型，管理自定义 OpenAI 兼容模型。',
    systemDefault: '系统默认',
    title: 'AI 模型',
  },
  en: {
    active: 'Active',
    add: 'Add model',
    apiKey: 'API Key (optional)',
    apiKeyPlaceholder: 'Leave empty to use the server environment key',
    cancel: 'Cancel',
    defaultLocked: 'The system default model cannot be deleted',
    delete: 'Delete model',
    deleteBody: 'This model will no longer appear in the chat model selector.',
    disabled: 'Disabled',
    enabled: 'Enabled',
    inheritedEndpoint: 'Uses system Base URL',
    managedBySystem: 'System managed',
    modelId: 'Model ID',
    name: 'Display name',
    namePlaceholder: 'For example: Business analysis',
    newHelp: 'Add an OpenAI-compatible model. Its key is stored server-side and always returned masked.',
    newTitle: 'New model',
    save: 'Save and use',
    subtitle: 'Choose the chat model and manage OpenAI-compatible models.',
    systemDefault: 'System default',
    title: 'AI models',
  },
}

const selectedModel = ref(props.activeModel)
const formOpen = ref(false)
const saving = ref(false)
const deletingId = ref('')
const togglingId = ref('')
const operationError = ref('')
const draft = reactive({ name: '', model: '', base_url: '', api_key: '' })

const enabledModels = computed(() => props.models.filter((model) => model.enabled))
const canAddModel = computed(() => Boolean(draft.name.trim() && draft.model.trim()))

watch(
  () => props.activeModel,
  (value) => {
    selectedModel.value = value
  },
)

function tr(key: string) {
  return copy[props.language]?.[key] || copy.zh[key] || key
}

async function activateModel() {
  const saved = await patchSettings({ active_model: selectedModel.value })
  if (!saved) selectedModel.value = props.activeModel
}

async function toggleModel(model: ModelConfig, enabled: boolean) {
  if (model.default) return
  togglingId.value = model.id
  const configuredModels = props.models.map((item) => (item.id === model.id ? { ...item, enabled } : item))
  try {
    await patchSettings({ configured_models: configuredModels, active_model: selectedModel.value })
  } finally {
    togglingId.value = ''
  }
}

async function addModel() {
  if (!canAddModel.value) return
  saving.value = true
  operationError.value = ''
  const model = {
    id: slug(draft.model),
    name: draft.name.trim(),
    provider: 'openai-compatible',
    model: draft.model.trim(),
    base_url: draft.base_url.trim(),
    api_key: draft.api_key.trim(),
    enabled: true,
    default: false,
  }
  try {
    const saved = await patchSettings({ configured_models: [...props.models, model], active_model: model.model })
    if (!saved) return
    selectedModel.value = model.model
    closeForm()
    ElMessage.success(props.language === 'en' ? 'Model added' : '模型已添加')
  } finally {
    saving.value = false
  }
}

async function deleteModel(model: ModelConfig) {
  if (model.default) return
  try {
    await ElMessageBox.confirm(tr('deleteBody'), `${tr('delete')} ${model.name}?`, {
      type: 'warning',
      confirmButtonText: props.language === 'en' ? 'Delete' : '删除',
      cancelButtonText: tr('cancel'),
      confirmButtonClass: 'el-button--danger',
    })
  } catch {
    return
  }
  deletingId.value = model.id
  operationError.value = ''
  try {
    const response = await http.delete(`/models/${encodeURIComponent(model.id)}`)
    const settings = response.data?.settings || response.data
    selectedModel.value = settings?.active_model || selectedModel.value
    emit('changed', settings)
    ElMessage.success(props.language === 'en' ? 'Model deleted' : '模型已删除')
  } catch (error: any) {
    operationError.value = requestError(error)
  } finally {
    deletingId.value = ''
  }
}

async function patchSettings(payload: Record<string, any>) {
  operationError.value = ''
  try {
    const response = await http.patch('/settings', payload)
    const settings = response.data?.settings || response.data
    selectedModel.value = settings?.active_model || selectedModel.value
    emit('changed', settings)
    return true
  } catch (error: any) {
    operationError.value = requestError(error)
    return false
  }
}

function closeForm() {
  formOpen.value = false
  draft.name = ''
  draft.model = ''
  draft.base_url = ''
  draft.api_key = ''
}

function requestError(error: any) {
  const detail = error?.response?.data?.detail || error?.response?.data?.message
  return typeof detail === 'string' ? detail : JSON.stringify(detail || error?.message || 'Request failed')
}

function slug(value: string) {
  return value.replace(/[^a-zA-Z0-9_]+/g, '_').replace(/^_+|_+$/g, '').toLowerCase() || `model_${Date.now()}`
}
</script>

<style scoped>
.model-settings {
  display: flex;
  min-height: 100%;
  flex-direction: column;
}

.panel-head,
.head-actions,
.model-card-head,
.model-card-foot,
.model-meta,
.form-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.panel-head {
  justify-content: space-between;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border-soft);
}

.panel-head h2,
.panel-head p,
.model-form h3,
.model-form p {
  margin: 0;
}

.panel-head h2,
.model-form h3 {
  color: var(--text-strong);
  font-size: 14px;
}

.panel-head p,
.model-form p {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
}

.head-actions :deep(.el-select) {
  width: min(240px, 34vw);
}

.operation-alert {
  margin-top: 12px;
}

.model-form {
  margin-top: 14px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-1);
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 12px;
}

.form-grid label {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.form-grid label > span {
  color: var(--text-muted);
  font-size: 11px;
}

.base-url-field,
.api-key-field {
  grid-column: 1 / -1;
}

.form-actions {
  justify-content: flex-end;
  margin-top: 12px;
}

.model-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding-top: 14px;
}

.model-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
}

.model-card {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-1);
  transition: border-color 0.16s ease, background-color 0.16s ease;
}

.model-card.active {
  border-color: color-mix(in srgb, var(--accent) 52%, var(--border));
  background: color-mix(in srgb, var(--surface-1) 90%, var(--accent-soft));
}

.model-card-head {
  min-width: 0;
  padding: 11px 10px 8px 12px;
}

.model-icon {
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

.model-title {
  flex: 1;
  min-width: 0;
}

.model-title strong,
.model-title span {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.model-title strong {
  color: var(--text-strong);
  font-size: 12px;
}

.model-title span {
  margin-top: 4px;
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 10px;
}

.delete-button {
  flex: 0 0 32px;
  width: 32px;
  height: 32px;
}

.delete-button:hover {
  color: var(--el-color-danger);
}

.locked-action {
  display: grid;
  flex: 0 0 32px;
  place-items: center;
  width: 32px;
  height: 32px;
  color: var(--text-muted);
}

.model-meta {
  flex-wrap: wrap;
  min-height: 29px;
  padding: 0 12px 8px;
  color: var(--text-muted);
  font-size: 10px;
}

.default-badge,
.active-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  min-height: 20px;
  padding: 0 6px;
  border-radius: 4px;
}

.default-badge {
  background: color-mix(in srgb, var(--warning) 14%, transparent);
  color: var(--warning);
}

.active-badge {
  background: var(--accent-soft);
  color: var(--accent);
}

.model-endpoint {
  min-height: 33px;
  margin: 0;
  overflow: hidden;
  padding: 9px 12px;
  border-top: 1px solid var(--border-soft);
  border-bottom: 1px solid var(--border-soft);
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.model-card-foot {
  justify-content: space-between;
  min-height: 44px;
  padding: 6px 11px 6px 12px;
  color: var(--text-muted);
  font-size: 11px;
}

@media (max-width: 700px) {
  .panel-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .head-actions {
    width: 100%;
  }

  .head-actions :deep(.el-select) {
    width: auto;
    min-width: 0;
    flex: 1;
  }

  .head-actions :deep(.el-button) {
    min-height: 44px;
  }

  .form-grid,
  .model-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .base-url-field {
    grid-column: auto;
  }

  .form-grid :deep(.el-input__wrapper) {
    min-height: 44px;
  }

  .form-grid :deep(.el-input__inner) {
    font-size: 16px;
  }

  .delete-button,
  .locked-action {
    flex-basis: 44px;
    width: 44px;
    height: 44px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .model-card {
    transition: none;
  }
}
</style>
