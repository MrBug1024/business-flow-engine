<template>
  <section class="tool-settings">
    <header class="panel-head">
      <div>
        <h2>{{ tr('title') }}</h2>
        <p>{{ tr('subtitle') }}</p>
      </div>
      <el-button :icon="Refresh" :loading="refreshing" @click="refreshTools">
        {{ tr('rescan') }}
      </el-button>
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

    <dl class="catalog-summary">
      <div>
        <dt>{{ tr('mounted') }}</dt>
        <dd>{{ mountedCount }}</dd>
      </div>
      <div>
        <dt>{{ tr('issues') }}</dt>
        <dd :class="{ danger: issueCount > 0 }">{{ issueCount }}</dd>
      </div>
      <div class="directory-fact">
        <dt>{{ tr('directory') }}</dt>
        <dd><code>tools/</code></dd>
      </div>
    </dl>

    <div class="tool-scroll">
      <div v-if="tools.length" class="tool-grid">
        <article
          v-for="item in tools"
          :key="`${item.source}:${item.name}`"
          class="tool-card"
          :class="{ invalid: item.status !== 'ready' }"
        >
          <header class="tool-card-head">
            <span class="tool-icon" :class="item.status">
              <el-icon v-if="item.status === 'ready'"><CircleCheck /></el-icon>
              <el-icon v-else><Warning /></el-icon>
            </span>
            <div class="tool-title">
              <strong :title="item.name">{{ item.name }}</strong>
              <span :class="['status-label', item.status]">{{ statusLabel(item.status) }}</span>
            </div>
          </header>

          <p class="tool-description">{{ item.description || tr('noDescription') }}</p>

          <p v-if="item.error" class="tool-error" role="alert">{{ item.error }}</p>

          <footer class="tool-card-foot">
            <span :title="item.source"><el-icon><Document /></el-icon>{{ item.source }}</span>
            <span v-if="item.record_type === 'tool'">
              {{ propertyCount(item.input_schema) }} {{ tr('arguments') }}
            </span>
          </footer>
        </article>
      </div>

      <div v-else class="empty-state">
        <span class="empty-icon"><el-icon><FolderOpened /></el-icon></span>
        <strong>{{ tr('emptyTitle') }}</strong>
        <span>{{ tr('emptyBody') }}</span>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { CircleCheck, Document, FolderOpened, Refresh, Warning } from '@element-plus/icons-vue'
import { http } from '@/api/http'

type Language = 'zh' | 'en'
type ToolRecord = {
  name: string
  description?: string
  input_schema?: Record<string, any>
  source: string
  status: 'ready' | 'duplicate' | 'error'
  error?: string | null
  mounted?: boolean
  record_type?: 'tool' | 'module_error'
}

const props = withDefaults(defineProps<{ tools: ToolRecord[]; language?: Language }>(), {
  language: 'zh',
})

const emit = defineEmits<{ refreshed: [tools: ToolRecord[]] }>()

const copy: Record<Language, Record<string, string>> = {
  zh: {
    arguments: '个参数',
    directory: '扫描目录',
    duplicate: '名称冲突',
    emptyBody: '目录中还没有可挂载的 LangChain Tool。',
    emptyTitle: '未发现 Tool',
    error: '加载失败',
    issues: '异常',
    mounted: '已挂载',
    noDescription: '暂无描述',
    ready: '可调用',
    rescan: '重新扫描',
    subtitle: '目录中的 LangChain Tool 会自动发现并挂载到 AI 运行时。',
    title: 'Tools',
  },
  en: {
    arguments: 'arguments',
    directory: 'Scan directory',
    duplicate: 'Name conflict',
    emptyBody: 'No mountable LangChain Tools were found in the directory.',
    emptyTitle: 'No Tools discovered',
    error: 'Load failed',
    issues: 'Issues',
    mounted: 'Mounted',
    noDescription: 'No description',
    ready: 'Callable',
    rescan: 'Rescan',
    subtitle: 'LangChain Tools in the directory are discovered and mounted automatically.',
    title: 'Tools',
  },
}

const refreshing = ref(false)
const operationError = ref('')
const mountedCount = computed(() => props.tools.filter((item) => item.mounted).length)
const issueCount = computed(() => props.tools.filter((item) => item.status !== 'ready').length)

function tr(key: string) {
  return copy[props.language]?.[key] || copy.zh[key] || key
}

function statusLabel(status: ToolRecord['status']) {
  return tr(status === 'duplicate' ? 'duplicate' : status === 'ready' ? 'ready' : 'error')
}

function propertyCount(schema?: Record<string, any>) {
  return Object.keys(schema?.properties || {}).length
}

async function refreshTools() {
  refreshing.value = true
  operationError.value = ''
  try {
    const response = await http.post('/tools/rescan')
    const refreshed = response.data?.tools || []
    emit('refreshed', refreshed)
    ElMessage.success(props.language === 'en' ? 'Tool catalog refreshed' : 'Tool 目录已重新扫描')
  } catch (error: any) {
    const detail = error?.response?.data?.detail || error?.message || 'Request failed'
    operationError.value = typeof detail === 'string' ? detail : JSON.stringify(detail)
  } finally {
    refreshing.value = false
  }
}
</script>

<style scoped>
.tool-settings {
  display: flex;
  min-height: 100%;
  flex-direction: column;
}

.panel-head,
.tool-card-head,
.tool-card-foot {
  display: flex;
  align-items: center;
}

.panel-head {
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border-soft);
}

.panel-head h2,
.panel-head p {
  margin: 0;
}

.panel-head h2 {
  color: var(--text-strong);
  font-size: 14px;
}

.panel-head p {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
}

.operation-alert {
  margin-top: 12px;
}

.catalog-summary {
  display: grid;
  grid-template-columns: minmax(110px, 0.45fr) minmax(110px, 0.45fr) minmax(180px, 1fr);
  gap: 1px;
  margin: 14px 0 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--border);
}

.catalog-summary > div {
  min-width: 0;
  padding: 10px 12px;
  background: var(--surface-1);
}

.catalog-summary dt {
  color: var(--text-muted);
  font-size: 10px;
}

.catalog-summary dd {
  margin: 3px 0 0;
  color: var(--text-strong);
  font-size: 15px;
  font-weight: 650;
}

.catalog-summary dd.danger {
  color: var(--danger, #dc2626);
}

.catalog-summary code {
  font-size: 12px;
  font-weight: 500;
}

.tool-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding-top: 14px;
}

.tool-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 10px;
}

.tool-card {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-1);
}

.tool-card.invalid {
  border-color: color-mix(in srgb, var(--danger, #dc2626) 38%, var(--border));
}

.tool-card-head {
  gap: 10px;
  padding: 12px 12px 0;
}

.tool-icon,
.empty-icon {
  display: grid;
  width: 32px;
  height: 32px;
  flex: 0 0 32px;
  place-items: center;
  border-radius: 6px;
  color: var(--success, #16a34a);
  background: color-mix(in srgb, var(--success, #16a34a) 12%, transparent);
}

.tool-icon.error,
.tool-icon.duplicate {
  color: var(--danger, #dc2626);
  background: color-mix(in srgb, var(--danger, #dc2626) 10%, transparent);
}

.tool-title {
  min-width: 0;
}

.tool-title strong,
.tool-title span {
  display: block;
}

.tool-title strong {
  overflow: hidden;
  color: var(--text-strong);
  font-family: var(--font-mono, monospace);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.status-label {
  margin-top: 3px;
  color: var(--success, #16a34a);
  font-size: 10px;
}

.status-label.error,
.status-label.duplicate {
  color: var(--danger, #dc2626);
}

.tool-description,
.tool-error {
  margin: 10px 12px 0;
  font-size: 11px;
  line-height: 1.55;
}

.tool-description {
  min-height: 34px;
  color: var(--text-secondary);
}

.tool-error {
  padding: 8px;
  overflow-wrap: anywhere;
  border-radius: 5px;
  color: var(--danger, #dc2626);
  background: color-mix(in srgb, var(--danger, #dc2626) 8%, transparent);
}

.tool-card-foot {
  justify-content: space-between;
  gap: 10px;
  margin-top: 12px;
  padding: 9px 12px;
  border-top: 1px solid var(--border-soft);
  color: var(--text-muted);
  font-size: 10px;
}

.tool-card-foot span {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 5px;
}

.tool-card-foot span:first-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.empty-state {
  display: grid;
  min-height: 190px;
  place-items: center;
  align-content: center;
  gap: 8px;
  border: 1px dashed var(--border);
  border-radius: 7px;
  color: var(--text-muted);
  text-align: center;
}

.empty-state strong {
  color: var(--text-strong);
  font-size: 13px;
}

.empty-state > span:last-child {
  max-width: 360px;
  font-size: 11px;
}

@media (max-width: 760px) {
  .panel-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .catalog-summary {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .directory-fact {
    grid-column: 1 / -1;
  }
}
</style>
