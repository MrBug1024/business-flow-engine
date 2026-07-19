<template>
  <div class="workspace-preview" :aria-busy="payload?.loading ? 'true' : 'false'">
    <div v-if="payload?.loading" class="preview-loading" aria-live="polite">
      <el-skeleton :rows="8" animated />
    </div>

    <div v-else-if="payload?.error" class="preview-state" role="alert">
      <el-icon><WarningFilled /></el-icon>
      <strong>{{ text.previewFailed }}</strong>
      <p>{{ payload.error }}</p>
      <el-button :icon="Refresh" @click="$emit('retry')">{{ text.retry }}</el-button>
    </div>

    <template v-else-if="payload">
      <el-alert
        v-for="warning in payload.warnings || []"
        :key="warning"
        :title="warning"
        type="warning"
        show-icon
        :closable="false"
      />

      <section v-if="payload.kind === 'mermaid'" class="graph-preview">
        <div class="preview-tools" :aria-label="text.graphControls">
          <span>{{ text.diagram }}</span>
          <div>
            <el-button
              circle
              size="small"
              :icon="ZoomOut"
              :title="text.zoomOut"
              :aria-label="text.zoomOut"
              :disabled="graphZoom <= 0.5"
              @click="zoomGraph(-0.25)"
            />
            <el-button
              circle
              size="small"
              :icon="FullScreen"
              :title="text.fitWindow"
              :aria-label="text.fitWindow"
              :disabled="graphFit"
              @click="graphFit = true"
            />
            <el-button
              circle
              size="small"
              :icon="RefreshLeft"
              :title="text.actualSize"
              :aria-label="text.actualSize"
              :disabled="!graphFit && graphZoom === 1"
              @click="resetGraphSize"
            />
            <el-button
              circle
              size="small"
              :icon="ZoomIn"
              :title="text.zoomIn"
              :aria-label="text.zoomIn"
              :disabled="graphZoom >= 3"
              @click="zoomGraph(0.25)"
            />
          </div>
        </div>
        <div v-if="mermaidError" class="preview-state compact" role="alert">
          <el-icon><WarningFilled /></el-icon>
          <strong>{{ text.diagramFailed }}</strong>
          <p>{{ mermaidError }}</p>
        </div>
        <div v-else class="mermaid-viewport">
          <div
            class="mermaid-render"
            :style="graphStyle"
            v-html="mermaidHtml"
          />
        </div>
        <details class="source-details">
          <summary>{{ text.source }}</summary>
          <pre>{{ payload.text }}</pre>
        </details>
      </section>

      <section v-else-if="payload.kind === 'markdown'" class="document-preview">
        <MarkdownContent :content="payload.text" />
      </section>

      <section v-else-if="payload.kind === 'document'" class="document-preview extracted-document">
        <MarkdownContent :content="payload.text" />
      </section>

      <section v-else-if="isTable" class="table-preview">
        <div v-if="sheetOptions.length > 1" class="preview-tools">
          <span>{{ payload.kind === 'database' ? text.table : text.sheet }}</span>
          <el-select v-model="selectedSheet" size="small" class="sheet-select" :aria-label="text.sheet">
            <el-option v-for="sheet in sheetOptions" :key="sheet.name" :label="sheet.name" :value="sheet.name" />
          </el-select>
        </div>
        <p class="preview-note">{{ tableSummary }}</p>
        <el-table :data="activeRows" height="min(62vh, 560px)" border empty-text="No rows">
          <el-table-column
            v-for="column in activeColumns"
            :key="column"
            :prop="column"
            :label="column"
            min-width="150"
            show-overflow-tooltip
          />
        </el-table>
      </section>

      <section v-else-if="payload.kind === 'image'" class="media-preview image-preview">
        <img :src="payload.raw_url" :alt="payload.filename" @error="mediaError = true" />
        <p v-if="mediaError" role="alert">{{ text.mediaFailed }}</p>
      </section>

      <section v-else-if="payload.kind === 'pdf'" class="media-preview pdf-preview">
        <iframe :src="payload.raw_url" :title="payload.filename" />
      </section>

      <section v-else-if="payload.kind === 'video'" class="media-preview">
        <video :src="payload.raw_url" controls preload="metadata">{{ text.mediaUnsupported }}</video>
      </section>

      <section v-else-if="payload.kind === 'audio'" class="media-preview audio-preview">
        <audio :src="payload.raw_url" controls preload="metadata">{{ text.mediaUnsupported }}</audio>
      </section>

      <pre v-else-if="['text', 'json'].includes(payload.kind || '')" class="text-preview">{{ payload.text }}</pre>

      <div v-else class="preview-state">
        <el-icon><Document /></el-icon>
        <strong>{{ payload.kind === 'error' ? text.previewFailed : text.noRenderer }}</strong>
        <p>{{ payload.warnings?.[0] || text.downloadHint }}</p>
        <el-button :icon="Download" @click="openDownload">{{ text.download }}</el-button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { Document, Download, FullScreen, Refresh, RefreshLeft, WarningFilled, ZoomIn, ZoomOut } from '@element-plus/icons-vue'
import mermaid from 'mermaid'
import MarkdownContent from '@/components/MarkdownContent.vue'

type PreviewPayload = {
  loading?: boolean
  error?: string
  filename?: string
  kind?: string
  text?: string
  raw_url?: string
  download_url?: string
  columns?: string[]
  sample_rows?: Record<string, unknown>[]
  sheets?: Array<{
    name: string
    columns?: string[]
    sample_rows?: Record<string, unknown>[]
    row_count?: number
    column_count?: number
  }>
  warnings?: string[]
  truncated?: boolean
}

const props = defineProps<{
  payload?: PreviewPayload
  language: 'zh' | 'en'
  theme: 'dark' | 'light' | 'contrast'
}>()

defineEmits<{ retry: [] }>()

const selectedSheet = ref('')
const mermaidHtml = ref('')
const mermaidError = ref('')
const graphZoom = ref(1)
const graphFit = ref(true)
const graphNaturalWidth = ref(960)
const mediaError = ref(false)

const copy = {
  zh: {
    diagram: '关系图', graphControls: '图形缩放控制', zoomIn: '放大', zoomOut: '缩小', fitWindow: '适合窗口', actualSize: '原始大小',
    source: 'Mermaid 源码', diagramFailed: '图形渲染失败', previewFailed: '文件预览失败', retry: '重试',
    table: '数据表', sheet: '工作表', rows: '行预览', columns: '列', mediaFailed: '媒体无法加载，请下载后查看。',
    mediaUnsupported: '当前浏览器不支持此媒体格式。', noRenderer: '此格式暂不支持内嵌预览',
    downloadHint: '可以下载原文件并使用本地应用打开。', download: '下载文件',
  },
  en: {
    diagram: 'Diagram', graphControls: 'Diagram zoom controls', zoomIn: 'Zoom in', zoomOut: 'Zoom out', fitWindow: 'Fit window', actualSize: 'Actual size',
    source: 'Mermaid source', diagramFailed: 'Diagram rendering failed', previewFailed: 'File preview failed', retry: 'Retry',
    table: 'Table', sheet: 'Sheet', rows: 'rows previewed', columns: 'columns', mediaFailed: 'The media could not be loaded. Download it to inspect locally.',
    mediaUnsupported: 'This browser does not support the media format.', noRenderer: 'No embedded preview is available for this format',
    downloadHint: 'Download the original file and open it with a local application.', download: 'Download file',
  },
}

const text = computed(() => copy[props.language])
const isTable = computed(() => ['table', 'database', 'archive'].includes(props.payload?.kind || ''))
const sheetOptions = computed(() => props.payload?.sheets?.length
  ? props.payload.sheets
  : [{ name: props.payload?.filename || text.value.table, columns: props.payload?.columns || [], sample_rows: props.payload?.sample_rows || [] }])
const activeSheet = computed(() => sheetOptions.value.find((sheet) => sheet.name === selectedSheet.value) || sheetOptions.value[0])
const activeColumns = computed(() => activeSheet.value?.columns || props.payload?.columns || [])
const activeRows = computed(() => activeSheet.value?.sample_rows || props.payload?.sample_rows || [])
const tableSummary = computed(() => {
  const knownRows = activeSheet.value?.row_count
  const rowText = knownRows ? `${knownRows} / ${activeRows.value.length}` : `${activeRows.value.length}`
  return `${rowText} ${text.value.rows} · ${activeColumns.value.length} ${text.value.columns}`
})
const graphStyle = computed(() => ({
  width: graphFit.value ? '100%' : `${Math.round(graphNaturalWidth.value * graphZoom.value)}px`,
}))

watch(
  () => props.payload,
  (payload) => {
    selectedSheet.value = payload?.sheets?.[0]?.name || payload?.filename || ''
    mediaError.value = false
    graphZoom.value = 1
    graphFit.value = true
  },
  { immediate: true },
)

watch(
  () => [props.payload?.kind, props.payload?.text, props.theme],
  async () => renderMermaid(),
  { immediate: true },
)

async function renderMermaid() {
  if (props.payload?.kind !== 'mermaid' || !props.payload.text) {
    mermaidHtml.value = ''
    mermaidError.value = ''
    return
  }
  await nextTick()
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    suppressErrorRendering: true,
    theme: props.theme === 'light' ? 'default' : 'dark',
  })
  try {
    const id = `workspace-mermaid-${Date.now()}-${Math.random().toString(36).slice(2)}`
    const { svg } = await mermaid.render(id, props.payload.text)
    mermaidHtml.value = svg
    const viewBox = svg.match(/viewBox="(?:[\d.-]+\s+){2}([\d.]+)\s+([\d.]+)"/i)
    graphNaturalWidth.value = Math.min(4000, Math.max(720, Number(viewBox?.[1]) || 960))
    mermaidError.value = ''
  } catch (error: any) {
    mermaidHtml.value = ''
    mermaidError.value = String(error?.message || error)
  }
}

function zoomGraph(delta: number) {
  graphFit.value = false
  graphZoom.value = Math.min(3, Math.max(0.5, graphZoom.value + delta))
}

function resetGraphSize() {
  graphFit.value = false
  graphZoom.value = 1
}

function openDownload() {
  if (props.payload?.download_url) window.open(props.payload.download_url, '_blank', 'noopener,noreferrer')
}
</script>

<style scoped>
.workspace-preview {
  min-height: 420px;
}

.workspace-preview :deep(.el-alert + .el-alert) {
  margin-top: 8px;
}

.workspace-preview :deep(.el-alert:last-of-type) {
  margin-bottom: 12px;
}

.preview-loading {
  min-height: 420px;
  padding: 20px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-2);
}

.preview-state {
  display: grid;
  place-items: center;
  align-content: center;
  min-height: 420px;
  padding: 24px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-2);
  color: var(--text-muted);
  text-align: center;
}

.preview-state.compact {
  min-height: 220px;
}

.preview-state .el-icon {
  margin-bottom: 10px;
  font-size: 30px;
}

.preview-state strong {
  color: var(--text-strong);
  font-size: 14px;
}

.preview-state p {
  max-width: 620px;
  margin: 6px 0 14px;
  overflow-wrap: anywhere;
}

.preview-tools {
  display: flex;
  min-height: 40px;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
  padding: 4px 8px;
  border-bottom: 1px solid var(--border);
  color: var(--text-muted);
  font-size: 12px;
}

.preview-tools > div {
  display: flex;
  gap: 6px;
}

.sheet-select {
  width: min(280px, 52vw);
}

.preview-note {
  margin: 0 0 10px;
  color: var(--text-muted);
  font-size: 12px;
}

.document-preview {
  min-height: 420px;
  padding: 18px 22px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-2);
}

.extracted-document {
  white-space: pre-wrap;
}

.text-preview {
  min-height: 420px;
  max-height: none;
  white-space: pre;
}

.mermaid-viewport {
  min-height: 420px;
  overflow: auto;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-2);
}

.mermaid-render {
  min-width: 100%;
  transition: width 180ms ease;
}

.mermaid-render :deep(svg) {
  display: block;
  width: 100% !important;
  height: auto;
  min-height: 360px;
  margin: 0 auto;
}

.source-details {
  margin-top: 12px;
}

.source-details summary {
  min-height: 36px;
  padding: 8px 4px;
  color: var(--text-muted);
  cursor: pointer;
}

.media-preview {
  display: grid;
  min-height: 420px;
  place-items: center;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-2);
}

.image-preview img {
  display: block;
  max-width: 100%;
  max-height: 72vh;
  object-fit: contain;
}

.pdf-preview iframe {
  width: 100%;
  height: max(520px, 72vh);
  border: 0;
  background: white;
}

.media-preview video {
  width: min(100%, 1100px);
  max-height: 72vh;
}

.audio-preview {
  min-height: 240px;
}

.audio-preview audio {
  width: min(640px, calc(100% - 32px));
}

@media (prefers-reduced-motion: reduce) {
  .mermaid-render {
    transition: none;
  }
}

@media (max-width: 600px) {
  .document-preview {
    padding: 14px;
  }

  .preview-tools {
    align-items: stretch;
    flex-direction: column;
  }

  .sheet-select {
    width: 100%;
  }
}
</style>
