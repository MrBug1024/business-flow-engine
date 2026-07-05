<template>
  <el-dialog :model-value="modelValue" :title="card?.display_name || '能力配置'" width="640px" @update:model-value="close">
    <div v-if="loading" class="muted">加载中…</div>
    <template v-else-if="card">
      <div class="ns mono">
        <span class="ns-chip">NS {{ card.namespace }}</span>
        <span class="ns-chip">模式 {{ card.execution_mode }}</span>
      </div>

      <section><h4>能力摘要</h4><p>{{ card.summary }}</p></section>
      <section>
        <h4>何时使用（when_to_use）</h4>
        <ul class="pos"><li v-for="(w, i) in card.when_to_use" :key="i">{{ w }}</li></ul>
      </section>
      <section>
        <h4>何时不要使用（not_for）</h4>
        <ul class="neg"><li v-for="(w, i) in card.not_for" :key="i">{{ w }}</li></ul>
      </section>
      <section>
        <h4>提供的工具（命名空间隔离，多场景不冲突）</h4>
        <div class="chips">
          <span v-for="t in card.tools" :key="t.name || t" class="chip mono">{{ t.name || t }}</span>
        </div>
      </section>

      <section>
        <h4>作为远程 MCP Server 挂载到第三方（粘贴即用）</h4>

        <div class="url-row">
          <span class="url-tag">安装地址</span>
          <code class="url mono">{{ sseUrl }}</code>
          <el-button size="small" text :icon="CopyDocument" @click="copyText(sseUrl, '已复制安装地址')" />
        </div>
        <div class="src-tags">
          <span class="src-tag" :class="baseFromEnv ? 'env' : 'host'">
            {{ baseFromEnv ? '基址来自 .env 固定域名（正式）' : '基址取自当前访问地址（开发/测试）' }}
          </span>
          <span v-if="requiresToken" class="src-tag token">已启用访问令牌 · 配置已内置</span>
        </div>

        <el-segmented v-model="variant" :options="variantOptions" size="small" class="variant-seg" />

        <div class="code">
          <el-button size="small" class="copy" :icon="CopyDocument" @click="copy">复制</el-button>
          <pre>{{ snippet }}</pre>
        </div>
        <div class="hint">
          <template v-if="variant === 'remote'">
            适配几乎所有宿主（含仅支持本地命令的 Claude Desktop）：经 <code>mcp-remote</code> 桥接到远程地址。
          </template>
          <template v-else>
            适用于原生支持「远程 MCP」的宿主（Cursor / Cline / Windsurf 等），直接填 <code>url</code>。
          </template>
          宿主会自动发现 <code>{{ card.namespace }}__*</code> 工具，无需改动任何第三方代码。
        </div>
        <div v-if="!baseFromEnv" class="hint warn">
          当前基址取自你打开本页所用的地址。若第三方在其它网络，请用可达的服务 IP/域名访问本平台，
          或在 <code>.env</code> 配置 <code>MCP_PUBLIC_BASE_URL</code> 为固定域名。
        </div>
      </section>

      <section>
        <h4>测试数据（沙盒执行时优先使用）</h4>
        <div class="hint">当前：{{ files.length ? files.join('、') : '（无，回退使用蒸馏原始数据）' }}</div>
        <input ref="fi" type="file" multiple style="display: none" @change="onUpload" />
        <div class="upload-row">
          <el-button size="small" :icon="Upload" @click="fi?.click()">上传测试数据</el-button>
          <span class="hint inline">文件名（不含后缀）须与场景表名一致</span>
        </div>
      </section>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { CopyDocument, Upload } from '@element-plus/icons-vue'
import { http } from '@/api/http'
import { useSandboxStore } from '@/stores/sandbox'

const props = defineProps<{ modelValue: boolean; scenarioId: string | null }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()
const sandbox = useSandboxStore()

const loading = ref(false)
const card = ref<any>(null)
const files = ref<string[]>([])
const fi = ref<HTMLInputElement>()

const variant = ref<'remote' | 'native'>('remote')
const variantOptions = [
  { label: '远程桥接（mcp-remote）', value: 'remote' },
  { label: '原生远程 URL', value: 'native' },
]
const cfgRemote = ref<any>({})
const cfgNative = ref<any>({})
const sseUrl = ref('')
const baseFromEnv = ref(false)
const requiresToken = ref(false)
const snippet = computed(() =>
  JSON.stringify((variant.value === 'remote' ? cfgRemote.value : cfgNative.value) || {}, null, 2),
)

watch(
  () => [props.modelValue, props.scenarioId],
  async () => {
    if (!props.modelValue || !props.scenarioId) return
    loading.value = true
    try {
      const cfg = await sandbox.config(props.scenarioId)
      card.value = cfg.card
      cfgRemote.value = cfg.config_example || {}
      cfgNative.value = cfg.config_example_native || {}
      sseUrl.value = cfg.sse_url || ''
      baseFromEnv.value = !!cfg.base_from_env
      requiresToken.value = !!cfg.requires_token
      await loadFiles()
    } finally {
      loading.value = false
    }
  },
)

async function loadFiles() {
  const { data } = await http.get(`/playground/scenarios/${props.scenarioId}/uploads`)
  files.value = data.files || []
}
function copy() {
  navigator.clipboard.writeText(snippet.value)
  ElMessage.success('已复制配置片段')
}
function copyText(text: string, msg: string) {
  if (!text) return
  navigator.clipboard.writeText(text)
  ElMessage.success(msg)
}
async function onUpload(e: Event) {
  const fl = (e.target as HTMLInputElement).files
  if (!fl || !fl.length || !props.scenarioId) return
  const form = new FormData()
  Array.from(fl).forEach((f) => form.append('files', f))
  await http.post(`/playground/scenarios/${props.scenarioId}/uploads`, form)
  ElMessage.success('测试数据已上传')
  await loadFiles()
}
function close() {
  emit('update:modelValue', false)
}
</script>

<style scoped lang="scss">
.ns { display: flex; gap: 8px; margin-bottom: 6px; }
.ns-chip { font-size: var(--text-xs); color: var(--brand); background: var(--brand-soft); padding: 3px 9px; border-radius: var(--r-xs); }
section { margin-top: 18px; }
h4 { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.07em; color: var(--text-3); font-weight: 700; margin: 0 0 8px; }
p { font-size: var(--text-base); line-height: 1.65; margin: 0; color: var(--text-2); }
ul { margin: 0; padding-left: 20px; }
li { font-size: var(--text-base); line-height: 1.7; color: var(--text-2); }
ul.pos li::marker { content: '▸ '; color: var(--success); }
ul.neg li::marker { content: '✕ '; color: var(--danger); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { display: inline-block; font-size: var(--text-xs); padding: 4px 9px; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--r-xs); color: var(--info); }
.url-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.url-tag { flex-shrink: 0; font-size: var(--text-xs); font-weight: 700; color: var(--text-3); }
.url {
  flex: 1; min-width: 0; font-size: var(--text-sm); color: var(--brand);
  background: var(--brand-soft); border: 1px solid color-mix(in srgb, var(--brand) 24%, transparent);
  padding: 6px 10px; border-radius: var(--r-xs);
  overflow-x: auto; white-space: nowrap;
}
.src-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
.src-tag { font-size: var(--text-xs); padding: 2px 9px; border-radius: var(--r-full); }
.src-tag.host { background: var(--warning-soft); color: var(--warning); }
.src-tag.env { background: var(--success-soft); color: var(--success); }
.src-tag.token { background: var(--info-soft); color: var(--info); }
.variant-seg { margin-bottom: 10px; }
.code { position: relative; background: var(--code-bg); border: 1px solid var(--border); border-radius: var(--r-sm); padding: 12px 14px; overflow-x: auto; }
.code pre { margin: 0; font-family: var(--font-mono); font-size: var(--text-sm); color: var(--text-2); line-height: 1.6; }
.copy { position: absolute; top: 8px; right: 8px; }
.hint { font-size: var(--text-xs); color: var(--text-3); margin-top: 8px; line-height: 1.6; }
.hint.warn { color: var(--warning); background: var(--warning-soft); border-radius: var(--r-xs); padding: 8px 10px; }
.hint.inline { margin-top: 0; }
.upload-row { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
code { font-family: var(--font-mono); color: var(--info); background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
</style>
