<template>
  <section class="data-catalog" :aria-busy="loading ? 'true' : 'false'">
    <header class="catalog-head">
      <div>
        <p class="catalog-eyebrow">{{ text.eyebrow }}</p>
        <div class="catalog-title-row">
          <h1>{{ text.title }}</h1>
        </div>
        <p>{{ text.subtitle }}</p>
      </div>
      <el-button :icon="Refresh" :loading="loading" @click="reload(true)">{{ text.refresh }}</el-button>
    </header>

    <el-alert
      v-if="catalogError"
      :title="catalogError"
      type="warning"
      show-icon
      :closable="false"
    />

    <div v-if="loading && !catalogFiles.length" class="catalog-loading">
      <el-skeleton :rows="8" animated />
    </div>

    <div v-else-if="catalogFiles.length" class="file-list">
      <article v-for="file in catalogFiles" :key="file.key" class="file-card">
        <header class="file-card-head">
          <div class="file-identity">
            <strong>{{ file.filename }}</strong>
            <small>{{ file.workspace_path || file.filename }}</small>
          </div>
          <div class="file-badges">
            <el-tag effect="plain">{{ fileTypeLabel(file) }}</el-tag>
            <el-tag :type="file.isStructured ? 'success' : 'info'" effect="plain">
              {{ file.isStructured ? text.structured : text.unstructured }}
            </el-tag>
            <span>{{ formatBytes(file.size) }}</span>
          </div>
        </header>

        <template v-if="file.isStructured">
          <section
            v-for="table in file.tables"
            :key="table.name"
            class="table-card"
          >
            <header class="table-card-head">
              <div>
                <h2 v-if="showTableHeading(file, table)">{{ tableDisplayName(table.name) }}</h2>
                <p>{{ tableMetrics(table) }}</p>
              </div>
              <el-button
                text
                type="primary"
                :disabled="!file.workspace_path"
                @click="openArtifact(file.workspace_path)"
              >{{ text.openSource }}</el-button>
            </header>

            <div class="table-layout">
              <section class="field-section">
                <h3>{{ text.fields }}</h3>
                <div v-if="table.columns.length" class="field-tags">
                  <el-tag v-for="column in table.columns" :key="column" size="small" effect="plain">{{ column }}</el-tag>
                </div>
                <p v-else class="muted">{{ text.noFields }}</p>
              </section>

              <section class="role-section">
                <h3>{{ text.role }}</h3>
                <div class="role-controls">
                  <el-select
                    :model-value="roleValue(file, table.name)"
                    :loading="isSaving(file, table.name)"
                    :placeholder="text.chooseRole"
                    @update:model-value="saveRole(file, table.name, String($event))"
                  >
                    <el-option
                      v-for="option in roleOptionsFor(file, table.name)"
                      :key="option.value"
                      :label="option.label"
                      :value="option.value"
                    />
                  </el-select>
                  <el-tag
                    :type="isSaving(file, table.name) ? 'warning' : isRolePersisted(file, table.name) ? 'success' : 'info'"
                    effect="plain"
                  >
                    {{ roleStatusLabel(file, table.name) }}
                  </el-tag>
                </div>
                <el-input
                  :model-value="roleNote(file, table.name)"
                  :disabled="!roleValue(file, table.name)"
                  :placeholder="text.roleNotePlaceholder"
                  @update:model-value="updateRoleNote(file, table.name, String($event))"
                />
              </section>

              <section class="sample-section">
                <h3>{{ text.traceSamples }}</h3>
                <div v-if="samplesFor(file, table.name).length" class="sample-list">
                  <button
                    v-for="sample in samplesFor(file, table.name)"
                    :key="sample.path"
                    class="sample-link"
                    type="button"
                    @click="openArtifact(sample.path)"
                  >
                    <span>{{ sample.label }}</span>
                    <small>{{ sample.detail || text.openSample }}</small>
                  </button>
                </div>
                <p v-else class="muted">{{ text.noSamples }}</p>
              </section>
            </div>
          </section>
        </template>

        <template v-else>
          <section class="unstructured-card">
            <div>
              <h2>{{ text.nonTabularTitle }}</h2>
              <p>{{ nonTabularDescription(file) }}</p>
              <dl class="file-facts">
                <div>
                  <dt>{{ text.fileType }}</dt>
                  <dd>{{ fileTypeLabel(file) }}</dd>
                </div>
                <div>
                  <dt>{{ text.dataVolume }}</dt>
                  <dd>{{ formatBytes(file.size) }}</dd>
                </div>
                <div v-if="file.previewKind">
                  <dt>{{ text.previewType }}</dt>
                  <dd>{{ file.previewKind }}</dd>
                </div>
              </dl>
            </div>

            <div class="unstructured-controls">
              <el-button
                text
                type="primary"
                :disabled="!file.workspace_path"
                @click="openArtifact(file.workspace_path)"
              >{{ text.openSource }}</el-button>
              <el-select
                :model-value="roleValue(file, '__file__')"
                :loading="isSaving(file, '__file__')"
                :placeholder="text.chooseRole"
                @update:model-value="saveRole(file, '__file__', String($event))"
              >
                <el-option
                  v-for="option in roleOptionsFor(file, '__file__')"
                  :key="option.value"
                  :label="option.label"
                  :value="option.value"
                />
              </el-select>
              <el-tag
                :type="isSaving(file, '__file__') ? 'warning' : isRolePersisted(file, '__file__') ? 'success' : 'info'"
                effect="plain"
              >
                {{ roleStatusLabel(file, '__file__') }}
              </el-tag>
              <el-input
                :model-value="roleNote(file, '__file__')"
                :disabled="!roleValue(file, '__file__')"
                :placeholder="text.roleNotePlaceholder"
                @update:model-value="updateRoleNote(file, '__file__', String($event))"
              />
            </div>

            <section class="sample-section evidence-samples">
              <h3>{{ text.traceEvidence }}</h3>
              <div v-if="samplesFor(file, '__file__').length" class="sample-list">
                <button
                  v-for="sample in samplesFor(file, '__file__')"
                  :key="sample.path"
                  class="sample-link"
                  type="button"
                  @click="openArtifact(sample.path)"
                >
                  <span>{{ sample.label }}</span>
                  <small>{{ sample.detail || text.openEvidence }}</small>
                </button>
              </div>
              <p v-else class="muted">{{ text.noEvidence }}</p>
            </section>
          </section>
        </template>
      </article>
    </div>

    <section v-else class="empty-catalog">
      <strong>{{ text.emptyTitle }}</strong>
      <p>{{ text.emptyDescription }}</p>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { http } from '@/api/http'
import type { WorkspaceNode } from '@/types/studio'

type BusinessFile = {
  id: string
  filename: string
  workspace_path?: string
  suffix?: string
  size?: number
  sheets?: Array<{ name?: string; columns?: string[]; row_count?: number; column_count?: number }>
  columns?: string[]
  structured?: unknown
}

type CatalogSheet = {
  name: string
  columns: string[]
  row_count?: number
  column_count?: number
  preview_rows?: number
}

type LineageSample = {
  path: string
  label: string
  detail: string
  source_file_id?: string
  source_path?: string
  source_table?: string
}

type CatalogFile = {
  key: string
  id: string
  filename: string
  workspace_path: string
  suffix: string
  size: number
  previewKind: string
  isStructured: boolean
  tables: CatalogSheet[]
  lineageSamples: LineageSample[]
}

type RoleRow = {
  role: string
  note: string
  confirmed: boolean
}

type RoleOption = { value: string; label: string }

type CatalogCacheEntry = {
  sourceSignature: string
  state: any | null
  files: CatalogFile[]
}

type QueuedRoleSave = {
  key: string
  file: CatalogFile
  tableName: string
  retryCount: number
}

// A switch between editor tabs recreates this component. Keep the bounded
// metadata response around so returning to `data` is an immediate local read.
// Imports, deletes and completed chat actions advance the refresh token; the
// refresh control intentionally bypasses this cache as well.
const DATA_CATALOG_CACHE = new Map<string, CatalogCacheEntry>()

const props = defineProps<{
  businessId: string
  files: BusinessFile[]
  language: 'zh' | 'en'
  refreshToken?: number
}>()

const emit = defineEmits<{ openArtifact: [path: string] }>()
defineExpose({ flushRoleSaves })

const loading = ref(false)
const catalogError = ref('')
const catalogFiles = ref<CatalogFile[]>([])
const state = ref<any | null>(null)
const roleDrafts = ref<Record<string, { role: string; note: string }>>({})
const savingRoles = ref<Record<string, boolean>>({})
const noteSaveTimers = new Map<string, { timer: number; file: CatalogFile; tableName: string }>()
const pendingRoleSaves = new Map<string, QueuedRoleSave>()
const dirtyRoleScopes = new Set<string>()
let loadSequence = 0
let activeLoadSignature = ''
let activeRoleSaveKey = ''
let roleSaveWorker: Promise<void> | null = null

const copy = {
  zh: {
    eyebrow: '业务场景 / data',
    title: '文件、表格与字段',
    subtitle: '在导入数据所在的位置核对业务角色、字段和可查看的链路样本。',
    refresh: '刷新',
    structured: '结构化数据',
    unstructured: '非结构化材料',
    fields: '字段',
    noFields: '暂未识别字段；可打开源文件检查。',
    role: '业务角色',
    chooseRole: '选择此表的角色',
    roleSaved: '已保存',
    roleSaving: '保存中',
    roleUnsaved: '未保存',
    rolePending: '待设置',
    roleNotePlaceholder: '补充此角色的业务依据（可选）',
    traceSamples: '链路样本',
    traceEvidence: '追踪证据样本',
    noSamples: '尚无可查看的链路样本。请在 AI 对话发送“数据链路追踪”。',
    noEvidence: '尚未在当前数据链路中引用此材料。',
    openSample: '打开可读样本',
    openEvidence: '打开可读证据',
    openSource: '打开源文件',
    nonTabularTitle: '非表格材料',
    fileType: '文件类型',
    dataVolume: '文件大小',
    previewType: '预览类型',
    emptyTitle: 'data 中还没有导入文件',
    emptyDescription: '先在 data 中导入历史结果、业务输入和规则材料；之后可在这里设置角色，并在 AI 对话里发起追踪。',
    nonTabular: '这类文件没有可直接展示的字段或行。它同样可以标为业务输入、历史结果、规则知识、参考资料、结果模板或排除；AI 会按文件内容、版面或提取到的证据推理，不能把它伪装成表间字段关联。',
    roles: {
      input: '业务输入',
      result: '历史结果 / Oracle',
      rule: '规则 / 知识',
      reference: '参考材料',
      template: '输出模板',
      ignore: '排除',
    } as Record<string, string>,
  },
  en: {
    eyebrow: 'Business scenario / data',
    title: 'Files, tables, and fields',
    subtitle: 'Review business roles, fields, and readable lineage samples where the imported data lives.',
    refresh: 'Refresh',
    structured: 'Structured data',
    unstructured: 'Non-tabular material',
    fields: 'Fields',
    noFields: 'No fields were identified; open the source file to inspect it.',
    role: 'Business role',
    chooseRole: 'Choose the role for this table',
    roleSaved: 'Saved',
    roleSaving: 'Saving',
    roleUnsaved: 'Not saved',
    rolePending: 'Not set',
    roleNotePlaceholder: 'Optional business basis for this role',
    traceSamples: 'Lineage samples',
    traceEvidence: 'Trace evidence samples',
    noSamples: 'No readable lineage sample yet. Start tracing in the AI conversation.',
    noEvidence: 'This material is not referenced by the current data lineage.',
    openSample: 'Open readable sample',
    openEvidence: 'Open readable evidence',
    openSource: 'Open source file',
    nonTabularTitle: 'Non-tabular material',
    fileType: 'File type',
    dataVolume: 'File size',
    previewType: 'Preview type',
    emptyTitle: 'No file has been imported into data',
    emptyDescription: 'Import historical results, business inputs, and rule material into data, set their roles here, then ask AI to trace.',
    nonTabular: 'This file has no directly displayable table fields or rows. It may still be a business input, historical result, rule, reference, output template, or ignored source; AI reasons from its content, layout, or extracted evidence without pretending it is a field-level table join.',
    roles: {
      input: 'Business input',
      result: 'Historical result / oracle',
      rule: 'Rule / knowledge',
      reference: 'Reference material',
      template: 'Output template',
      ignore: 'Ignore',
    } as Record<string, string>,
  },
}

const text = computed(() => copy[props.language])
const allRoleOptions = computed<RoleOption[]>(() => Object.entries(text.value.roles).map(([value, label]) => ({ value, label })))
const dataFiles = computed(() => (props.files || []).filter((file) => isDataPath(file.workspace_path || file.filename)))
const currentSourceSignature = computed(() => `${props.businessId}|${props.refreshToken || 0}|${dataFiles.value.map((file) => `${file.id}:${file.workspace_path || file.filename}:${file.size || 0}`).join('|')}`)

watch(
  () => currentSourceSignature.value,
  () => { void reload() },
  { immediate: true },
)

onBeforeUnmount(() => {
  void flushRoleSaves()
})

function roleKey(file: CatalogFile, tableName: string) {
  return `${file.id}:${tableName}`
}

function roleRecord(file: CatalogFile, tableName: string): RoleRow {
  // Roles persist on the stable file/table scope. A source revision expires
  // its signed manifest, not the user's saved label for an unchanged file.
  const active = (state.value?.table_roles || []).find((item: any) => (
    String(item?.file_id || '') === file.id
    && String(item?.table_name || '') === tableName
    && item?.status !== 'superseded'
  ))
  const inherited = tableName === '__file__' ? undefined : (state.value?.table_roles || []).find((item: any) => (
    String(item?.file_id || '') === file.id
    && String(item?.table_name || '') === '__file__'
    && item?.status !== 'superseded'
  ))
  const record = active || inherited
  return {
    role: String(record?.role || ''),
    note: String(record?.note || ''),
    confirmed: record?.status === 'confirmed',
  }
}

function roleValue(file: CatalogFile, tableName: string) {
  const key = roleKey(file, tableName)
  return roleDrafts.value[key]?.role ?? roleRecord(file, tableName).role
}

function roleNote(file: CatalogFile, tableName: string) {
  const key = roleKey(file, tableName)
  return roleDrafts.value[key]?.note ?? roleRecord(file, tableName).note
}

function updateRoleNote(file: CatalogFile, tableName: string, value: string) {
  const key = roleKey(file, tableName)
  const current = roleDrafts.value[key] || roleRecord(file, tableName)
  roleDrafts.value = { ...roleDrafts.value, [key]: { ...current, note: value } }
  scheduleRoleNoteSave(file, tableName)
}

function isSaving(file: CatalogFile, tableName: string) {
  return Boolean(savingRoles.value[roleKey(file, tableName)])
}

function isRolePersisted(file: CatalogFile, tableName: string) {
  const draft = roleDrafts.value[roleKey(file, tableName)] || roleRecord(file, tableName)
  const persisted = roleRecord(file, tableName)
  return Boolean(draft.role) && draft.role === persisted.role && draft.note === persisted.note
}

function roleStatusLabel(file: CatalogFile, tableName: string) {
  if (isSaving(file, tableName)) return text.value.roleSaving
  if (!roleValue(file, tableName)) return text.value.rolePending
  return isRolePersisted(file, tableName) ? text.value.roleSaved : text.value.roleUnsaved
}

function roleOptionsFor(file: CatalogFile, tableName: string) {
  const allowed = allRoleOptions.value
  const current = roleValue(file, tableName)
  if (!current || allowed.some((option) => option.value === current)) return allowed
  return [{ value: current, label: text.value.roles[current] || current }, ...allowed]
}

function samplesFor(file: CatalogFile, tableName: string) {
  const samples = file.lineageSamples.filter((sample) => sampleMatches(sample, file, tableName))
  return samples.length || tableName !== '__file__'
    ? samples
    : file.lineageSamples.filter((sample) => sampleMatches(sample, file, ''))
}

function sampleMatches(sample: LineageSample, file: CatalogFile, tableName: string) {
  if (sample.source_file_id && sample.source_file_id === file.id) {
    return !sample.source_table || !tableName || sample.source_table === tableName
  }
  if (sample.source_path && sameWorkspacePath(sample.source_path, file.workspace_path)) {
    return !sample.source_table || !tableName || sample.source_table === tableName
  }
  const path = normalizeLookup(sample.path)
  const filePath = normalizeLookup(file.workspace_path)
  const filename = normalizeLookup(file.filename)
  const basename = filename.replace(/\.[^.]+$/, '')
  const table = normalizeLookup(tableName)
  const hasFileIdentity = Boolean(
    (filePath && path.includes(filePath))
    || (filename && path.includes(filename))
    || (basename && basename.length > 2 && path.includes(basename))
  )
  if (!hasFileIdentity) return false
  return !table || table === 'file' || path.includes(table)
}

function nonTabularDescription(_file: CatalogFile) {
  return text.value.nonTabular
}

function tableDisplayName(value: string) {
  return value === '__file__' ? (props.language === 'zh' ? '文件数据' : 'File data') : value
}

function showTableHeading(file: CatalogFile, table: CatalogSheet) {
  if (file.tables.length !== 1 || table.name === '__file__') return true
  const tableName = tableDisplayName(table.name).trim().toLocaleLowerCase()
  const filename = String(file.filename || '')
    .trim()
    .replace(/\.[^.]+$/, '')
    .toLocaleLowerCase()
  return Boolean(tableName && tableName !== filename)
}

function tableMetrics(table: CatalogSheet) {
  const columnCount = table.column_count ?? table.columns.length
  const rows = table.row_count == null
    ? (table.preview_rows ? (props.language === 'zh' ? `已预览 ${table.preview_rows} 行` : `${table.preview_rows} rows previewed`) : (props.language === 'zh' ? '总行数未统计' : 'total rows unavailable'))
    : (props.language === 'zh' ? `${table.row_count} 行` : `${table.row_count} rows`)
  return props.language === 'zh'
    ? `${rows} · ${columnCount} 个字段`
    : `${rows} · ${columnCount} fields`
}

function fileTypeLabel(file: CatalogFile) {
  const suffix = String(file.suffix || '').replace(/^\./, '').toUpperCase()
  if (suffix) return suffix
  return file.previewKind || (props.language === 'zh' ? '未知类型' : 'Unknown')
}

function formatBytes(value: unknown) {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let next = bytes / 1024
  let index = 0
  while (next >= 1024 && index < units.length - 1) {
    next /= 1024
    index += 1
  }
  return `${next >= 10 ? next.toFixed(0) : next.toFixed(1)} ${units[index]}`
}

function openArtifact(path: string) {
  if (path) emit('openArtifact', path)
}

function saveRole(file: CatalogFile, tableName: string, role: string) {
  const normalizedRole = String(role || '').trim()
  const key = roleKey(file, tableName)
  if (!props.businessId || !normalizedRole) return
  const current = roleDrafts.value[key] || roleRecord(file, tableName)
  roleDrafts.value = { ...roleDrafts.value, [key]: { ...current, role: normalizedRole } }
  clearRoleNoteSave(key)
  const requested = roleDrafts.value[key]
  const persisted = roleRecord(file, tableName)
  if (
    requested.role === persisted.role
    && requested.note === persisted.note
    && activeRoleSaveKey !== key
    && !pendingRoleSaves.has(key)
  ) {
    dirtyRoleScopes.delete(key)
    setRoleSaving(key, false)
    return
  }
  dirtyRoleScopes.add(key)
  pendingRoleSaves.set(key, { key, file, tableName, retryCount: 0 })
  setRoleSaving(key, true)
  ensureRoleSaveWorker()
}

function ensureRoleSaveWorker() {
  if (roleSaveWorker) return
  roleSaveWorker = drainRoleSaveQueue().finally(() => {
    roleSaveWorker = null
    if (pendingRoleSaves.size) ensureRoleSaveWorker()
  })
}

async function flushRoleSaves() {
  const pendingNotes = [...noteSaveTimers.values()]
  noteSaveTimers.forEach(({ timer }) => window.clearTimeout(timer))
  noteSaveTimers.clear()
  for (const item of pendingNotes) {
    const role = roleValue(item.file, item.tableName)
    if (role) saveRole(item.file, item.tableName, role)
  }
  while (roleSaveWorker || pendingRoleSaves.size) {
    ensureRoleSaveWorker()
    const worker = roleSaveWorker
    if (worker) await worker
  }
}

async function drainRoleSaveQueue() {
  while (pendingRoleSaves.size) {
    const first = pendingRoleSaves.entries().next().value as [string, QueuedRoleSave] | undefined
    if (!first) return
    const [key, job] = first
    pendingRoleSaves.delete(key)
    activeRoleSaveKey = key

    const requested = { ...(roleDrafts.value[key] || roleRecord(job.file, job.tableName)) }
    const persisted = roleRecord(job.file, job.tableName)
    if (!requested.role || (requested.role === persisted.role && requested.note === persisted.note)) {
      dirtyRoleScopes.delete(key)
      activeRoleSaveKey = ''
      if (!pendingRoleSaves.has(key)) setRoleSaving(key, false)
      synchronizeRoleDrafts()
      continue
    }

    let completed = false
    let shouldReportError = true
    try {
      state.value = (await http.put(`/businesses/${props.businessId}/distillation/table-roles`, {
        file_id: job.file.id,
        table_name: job.tableName,
        role: requested.role,
        note: requested.note || '',
        expected_revision: state.value?.revision,
      })).data
      completed = true
      updateCatalogCache()
    } catch (error: any) {
      const status = Number(error?.response?.status || 0)
      await refreshDistillationState()
      const newerSaveAlreadyQueued = pendingRoleSaves.has(key)
      if (status === 409 && (newerSaveAlreadyQueued || job.retryCount < 1)) {
        if (!newerSaveAlreadyQueued) {
          pendingRoleSaves.set(key, { ...job, retryCount: job.retryCount + 1 })
        }
        shouldReportError = false
      }
      if (shouldReportError) {
        ElMessage.error(error?.response?.data?.detail || error?.message || (props.language === 'zh' ? '保存文件角色失败' : 'Unable to save the file role'))
      }
    } finally {
      activeRoleSaveKey = ''
    }

    const latest = roleDrafts.value[key]
    const latestPersisted = roleRecord(job.file, job.tableName)
    const hasUnsavedLatest = Boolean(latest?.role) && (
      latest?.role !== latestPersisted.role
      || latest?.note !== latestPersisted.note
    )
    if (completed && hasUnsavedLatest && !pendingRoleSaves.has(key)) {
      pendingRoleSaves.set(key, { ...job, retryCount: 0 })
    }
    if (!hasUnsavedLatest) dirtyRoleScopes.delete(key)
    if (!pendingRoleSaves.has(key)) setRoleSaving(key, false)
    synchronizeRoleDrafts()
    updateCatalogCache()
  }
}

function setRoleSaving(key: string, saving: boolean) {
  if (saving) {
    if (!savingRoles.value[key]) savingRoles.value = { ...savingRoles.value, [key]: true }
    return
  }
  if (!savingRoles.value[key]) return
  const { [key]: _ignored, ...rest } = savingRoles.value
  savingRoles.value = rest
}

function scheduleRoleNoteSave(file: CatalogFile, tableName: string) {
  const key = roleKey(file, tableName)
  clearRoleNoteSave(key)
  if (!roleValue(file, tableName)) return
  const timer = window.setTimeout(() => {
    noteSaveTimers.delete(key)
    void saveRole(file, tableName, roleValue(file, tableName))
  }, 520)
  noteSaveTimers.set(key, { timer, file, tableName })
}

function clearRoleNoteSave(key: string) {
  const pending = noteSaveTimers.get(key)
  if (pending) window.clearTimeout(pending.timer)
  noteSaveTimers.delete(key)
}

async function refreshDistillationState() {
  if (!props.businessId) return
  try {
    state.value = (await http.get(`/businesses/${props.businessId}/distillation`)).data
    synchronizeRoleDrafts()
    updateCatalogCache()
  } catch {
    // The original save error is more useful than a secondary state-refresh error.
  }
}

async function reload(force = false) {
  if (!props.businessId) {
    catalogFiles.value = []
    state.value = null
    return
  }
  const sourceSignature = currentSourceSignature.value
  const cached = DATA_CATALOG_CACHE.get(props.businessId)
  if (
    !force
    && cached?.sourceSignature === sourceSignature
  ) {
    state.value = cached.state
    catalogFiles.value = cached.files
    catalogError.value = ''
    synchronizeRoleDrafts()
    return
  }
  if (loading.value && activeLoadSignature === sourceSignature) return
  const sequence = ++loadSequence
  activeLoadSignature = sourceSignature
  loading.value = true
  catalogError.value = ''
  try {
    const [stateResult, catalogResult] = await Promise.allSettled([
      http.get(`/businesses/${props.businessId}/distillation`),
      http.get(`/businesses/${props.businessId}/data/catalog`),
    ])
    if (sequence !== loadSequence) return
    if (stateResult.status === 'fulfilled') state.value = stateResult.value.data

    const catalog = catalogResult.status === 'fulfilled' ? catalogResult.value.data : null
    const hasCatalogFiles = Array.isArray(catalog?.files)
    const samples = normalizeSamples(catalog?.lineage_samples || catalog?.samples)
    const merged = hasCatalogFiles
      ? mergeCatalogFiles(catalog.files, samples)
      : await buildFallbackCatalog(samples, sequence)
    if (sequence !== loadSequence) return

    if (!samples.length && !merged.some((file) => file.lineageSamples.length)) {
      const treeSamples = await discoverTreeSamples(props.businessId)
      if (sequence !== loadSequence) return
      catalogFiles.value = merged.map((file) => ({ ...file, lineageSamples: samplesForFile(treeSamples, file) }))
    } else {
      catalogFiles.value = merged
    }
    synchronizeRoleDrafts()
    updateCatalogCache()
    if (stateResult.status === 'rejected') {
      catalogError.value = stateResult.reason?.response?.data?.detail || stateResult.reason?.message || ''
    }
  } catch (error: any) {
    if (sequence !== loadSequence) return
    catalogError.value = error?.response?.data?.detail || error?.message || (props.language === 'zh' ? '无法读取 data 中的文件信息。' : 'Unable to read the data catalog.')
  } finally {
    if (sequence === loadSequence) {
      loading.value = false
      activeLoadSignature = ''
    }
  }
}

function updateCatalogCache() {
  if (!props.businessId || !currentSourceSignature.value) return
  DATA_CATALOG_CACHE.set(props.businessId, {
    sourceSignature: currentSourceSignature.value,
    state: state.value,
    files: catalogFiles.value,
  })
}

async function buildFallbackCatalog(samples: LineageSample[], sequence: number) {
  const files = dataFiles.value
  const previews = await Promise.all(files.map(async (file) => {
    const path = String(file.workspace_path || file.filename || '').trim()
    if (!path) return { file, preview: null }
    try {
      const preview = (await http.get(`/businesses/${props.businessId}/workspace/preview`, { params: { path } })).data
      return { file, preview }
    } catch {
      return { file, preview: null }
    }
  }))
  if (sequence !== loadSequence) return []
  return previews.map(({ file, preview }) => catalogFileFrom(file, preview, samples))
}

function mergeCatalogFiles(rawFiles: unknown[], topLevelSamples: LineageSample[]) {
  const registered = dataFiles.value
  const used = new Set<string>()
  const merged = rawFiles
    .map((raw) => {
      const item = raw && typeof raw === 'object' ? raw as Record<string, any> : {}
      const id = String(item.file_id || item.id || item.file?.id || '')
      const path = String(item.workspace_path || item.path || item.file?.workspace_path || '')
      const file = registered.find((candidate) => candidate.id === id || sameWorkspacePath(candidate.workspace_path || candidate.filename, path))
      if (file) used.add(file.id)
      return catalogFileFrom(file || {
        id,
        filename: String(item.filename || item.file?.filename || path.split('/').pop() || ''),
        workspace_path: path,
        suffix: String(item.suffix || item.file?.suffix || ''),
        size: Number(item.size || item.file?.size || 0),
      }, item, normalizeSamples(item.lineage_samples || item.samples).concat(topLevelSamples))
    })
    .filter((file) => file.id && isDataPath(file.workspace_path || file.filename))
  for (const file of registered) {
    if (!used.has(file.id)) merged.push(catalogFileFrom(file, null, topLevelSamples))
  }
  return dedupeCatalogFiles(merged)
}

function catalogFileFrom(file: BusinessFile, source: any, topLevelSamples: LineageSample[]): CatalogFile {
  const sourceItem = source && typeof source === 'object' ? source : {}
  const workspacePath = String(sourceItem.workspace_path || sourceItem.path || file.workspace_path || file.filename || '')
  const filename = String(sourceItem.filename || file.filename || workspacePath.split('/').pop() || '')
  const sheets = normalizeSheets(sourceItem.sheets || file.sheets, sourceItem.columns || file.columns, sourceItem.sample_rows)
  const previewKind = String(sourceItem.kind || sourceItem.preview_kind || '')
  const isStructured = sourceItem.structured === true || ['table', 'database'].includes(previewKind) || sheets.length > 0 || Boolean((sourceItem.columns || file.columns || []).length)
  const ownSamples = normalizeSamples(sourceItem.lineage_samples || sourceItem.samples)
  const base: CatalogFile = {
    key: String(sourceItem.file_id || sourceItem.id || file.id || workspacePath),
    id: String(sourceItem.file_id || sourceItem.id || file.id || ''),
    filename,
    workspace_path: workspacePath,
    suffix: String(sourceItem.suffix || file.suffix || filename.slice(filename.lastIndexOf('.')) || ''),
    size: Number(sourceItem.size ?? file.size ?? 0),
    previewKind,
    isStructured,
    tables: isStructured ? (sheets.length ? sheets : [{ name: '__file__', columns: normalizeStrings(sourceItem.columns || file.columns), preview_rows: Array.isArray(sourceItem.sample_rows) ? sourceItem.sample_rows.length : undefined }]) : [],
    lineageSamples: [],
  }
  base.lineageSamples = samplesForFile(ownSamples.concat(topLevelSamples), base)
  return base
}

function normalizeSheets(value: unknown, fallbackColumns: unknown, sampleRows: unknown): CatalogSheet[] {
  if (!Array.isArray(value)) return []
  const sheets = value.map((item, index) => {
    const sheet = item && typeof item === 'object' ? item as Record<string, any> : {}
    const columns = normalizeStrings(sheet.columns || (index === 0 ? fallbackColumns : []))
    return {
      name: String(sheet.name || '').trim() || '__file__',
      columns,
      row_count: numberOrUndefined(sheet.row_count),
      column_count: numberOrUndefined(sheet.column_count) ?? (columns.length || undefined),
      preview_rows: Array.isArray(sheet.sample_rows) ? sheet.sample_rows.length : (index === 0 && Array.isArray(sampleRows) ? sampleRows.length : undefined),
    }
  })
  const seen = new Set<string>()
  return sheets.filter((sheet) => {
    if (seen.has(sheet.name)) return false
    seen.add(sheet.name)
    return true
  })
}

function normalizeSamples(value: unknown): LineageSample[] {
  if (!Array.isArray(value)) return []
  const seen = new Set<string>()
  return value.flatMap((item): LineageSample[] => {
    const source = typeof item === 'string' ? { path: item } : (item && typeof item === 'object' ? item as Record<string, any> : {})
    const path = normalizeWorkspacePath(source.path || source.sample_path || source.artifact_path || source.file)
    if (!path || path.endsWith('/index.json') || path.toLowerCase().endsWith('.json') || seen.has(path)) return []
    seen.add(path)
    return [{
      path,
      label: String(source.label || source.name || source.source_table || path.split('/').pop() || path),
      detail: String(source.detail || source.summary || source.rows || ''),
      source_file_id: String(source.source_file_id || source.file_id || ''),
      source_path: normalizeWorkspacePath(source.source_path || source.source_file || ''),
      source_table: String(source.source_table || source.table || ''),
    }]
  })
}

async function discoverTreeSamples(businessId: string) {
  try {
    const tree = (await http.get(`/businesses/${businessId}/workspace/tree`)).data as WorkspaceNode
    return flattenTree(tree)
      .filter((node) => node.kind === 'file' && normalizeWorkspacePath(node.path).startsWith('outputs/data-lineage-samples/'))
      .filter((node) => !normalizeWorkspacePath(node.path).toLowerCase().endsWith('.json'))
      .map((node) => ({
        path: normalizeWorkspacePath(node.path),
        label: node.name,
        detail: '',
      }))
  } catch {
    return []
  }
}

function flattenTree(node: WorkspaceNode | null | undefined): WorkspaceNode[] {
  if (!node) return []
  return [node, ...(node.children || []).flatMap((child) => flattenTree(child))]
}

function samplesForFile(samples: LineageSample[], file: CatalogFile) {
  const seen = new Set<string>()
  return samples.filter((sample) => {
    if (seen.has(sample.path) || !sampleMatches(sample, file, '')) return false
    seen.add(sample.path)
    return true
  })
}

function dedupeCatalogFiles(files: CatalogFile[]) {
  const seen = new Set<string>()
  return files.filter((file) => {
    const key = file.id || file.workspace_path
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function synchronizeRoleDrafts() {
  const next: Record<string, { role: string; note: string }> = {}
  for (const file of catalogFiles.value) {
    const tables = file.isStructured ? file.tables.map((table) => table.name) : ['__file__']
    for (const tableName of tables) {
      const key = roleKey(file, tableName)
      if (savingRoles.value[key] || dirtyRoleScopes.has(key)) {
        next[key] = roleDrafts.value[key] || roleRecord(file, tableName)
      } else {
        const record = roleRecord(file, tableName)
        next[key] = { role: record.role, note: record.note }
      }
    }
  }
  roleDrafts.value = next
}

function normalizeStrings(value: unknown) {
  if (!Array.isArray(value)) return []
  return [...new Set(value.map((item) => String(item || '').trim()).filter(Boolean))]
}

function numberOrUndefined(value: unknown) {
  const number = Number(value)
  return Number.isFinite(number) && number >= 0 ? number : undefined
}

function normalizeWorkspacePath(value: unknown) {
  return String(value || '').trim().replace(/\\/g, '/').replace(/^\/+/, '')
}

function normalizeLookup(value: unknown) {
  return normalizeWorkspacePath(value)
    .toLowerCase()
    .replace(/\.[a-z0-9]+$/i, '')
    .replace(/[^a-z0-9\u4e00-\u9fff]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

function sameWorkspacePath(left: unknown, right: unknown) {
  const first = normalizeWorkspacePath(left)
  const second = normalizeWorkspacePath(right)
  return first === second || first.replace(/^data\//, '') === second.replace(/^data\//, '')
}

function isDataPath(value: unknown) {
  const path = normalizeWorkspacePath(value)
  return !path || path === 'data' || path.startsWith('data/')
}
</script>

<style scoped>
.data-catalog { display: grid; gap: 16px; min-height: 100%; container-type: inline-size; }
.catalog-head, .file-card-head, .table-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; }
.catalog-head { padding: 4px 2px 2px; }
.catalog-head h1, .table-card h2, .unstructured-card h2, .file-card h3 { margin: 0; }
.catalog-title-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.catalog-head p, .table-card p, .unstructured-card p { margin: 5px 0 0; color: var(--text-muted); }
.catalog-eyebrow { color: var(--accent) !important; font-size: 12px; font-weight: 700; letter-spacing: 0; text-transform: uppercase; }
.catalog-loading, .empty-catalog { padding: 22px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
.empty-catalog { color: var(--text-muted); }
.empty-catalog strong { color: var(--text-strong); }
.empty-catalog p { margin: 6px 0 0; }
.file-list { display: grid; gap: 14px; }
.file-card { display: grid; gap: 14px; padding: 16px; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
.file-identity { display: grid; min-width: 0; gap: 3px; }
.file-identity strong { overflow-wrap: anywhere; }
.file-identity small, .file-badges > span { color: var(--text-muted); font-size: 12px; overflow-wrap: anywhere; }
.file-badges { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-end; gap: 7px; }
.table-card { display: grid; gap: 12px; padding: 14px; border: 1px solid color-mix(in srgb, var(--line) 86%, var(--accent) 14%); border-radius: 8px; background: color-mix(in srgb, var(--panel) 94%, var(--accent) 3%); }
.table-card-head h2, .unstructured-card h2 { font-size: 15px; }
.table-card-head p { font-size: 12px; }
.table-layout { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
.field-section { grid-column: 1 / -1; }
.field-section, .role-section, .sample-section { display: grid; align-content: start; gap: 8px; min-width: 0; }
.field-section h3, .role-section h3, .sample-section h3 { margin: 0; color: var(--text-muted); font-size: 12px; font-weight: 700; }
.field-tags { display: flex; flex-wrap: wrap; gap: 6px; max-height: 112px; overflow: auto; padding-right: 2px; }
.muted { margin: 0; color: var(--text-muted); font-size: 12px; line-height: 1.55; }
.role-controls { display: grid; gap: 7px; }
.role-controls .el-select { min-width: 0; }
.role-controls .el-tag { justify-self: start; }
.sample-list { display: grid; gap: 6px; }
.sample-link { display: grid; gap: 2px; width: 100%; padding: 8px 9px; border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--line)); border-radius: 7px; background: color-mix(in srgb, var(--panel) 89%, var(--accent) 5%); color: var(--text-strong); cursor: pointer; text-align: left; }
.sample-link:hover { border-color: var(--accent); background: color-mix(in srgb, var(--panel) 83%, var(--accent) 10%); }
.sample-link span { overflow-wrap: anywhere; font-size: 12px; font-weight: 650; }
.sample-link small { color: var(--text-muted); font-size: 11px; }
.unstructured-card { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px; padding: 14px; border: 1px solid color-mix(in srgb, var(--line) 88%, var(--warning) 12%); border-radius: 8px; background: color-mix(in srgb, var(--panel) 96%, var(--warning) 3%); }
.unstructured-card > :first-child { grid-column: 1 / -1; }
.file-facts { display: flex; flex-wrap: wrap; gap: 9px 18px; margin: 12px 0 0; }
.file-facts div { display: grid; gap: 2px; }
.file-facts dt { color: var(--text-muted); font-size: 11px; }
.file-facts dd { margin: 0; color: var(--text-strong); font-size: 12px; }
.unstructured-controls { display: grid; align-content: start; gap: 9px; }
.unstructured-controls > .el-button { justify-self: start; padding-left: 0; }
.evidence-samples { align-content: start; }
@container (max-width: 520px) { .table-layout, .unstructured-card { grid-template-columns: 1fr; } .sample-section { grid-column: auto; } }
@media (max-width: 680px) { .catalog-head, .file-card-head, .table-card-head { align-items: stretch; flex-direction: column; } .file-badges { justify-content: flex-start; } }
</style>
