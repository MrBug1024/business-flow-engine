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
        <h4>作为 MCP Server 挂载到第三方（粘贴即用）</h4>
        <div class="code">
          <el-button size="small" class="copy" :icon="CopyDocument" @click="copy">复制</el-button>
          <pre>{{ snippet }}</pre>
        </div>
        <div class="hint">
          把以上片段粘进 Claude Desktop / Cursor / Cline 等宿主的 MCP 配置即可，宿主会自动发现
          <code>{{ card.namespace }}__*</code> 工具，无需改动任何第三方代码。
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
import { ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { CopyDocument, Upload } from '@element-plus/icons-vue'
import { http } from '@/api/http'
import { useSandboxStore } from '@/stores/sandbox'

const props = defineProps<{ modelValue: boolean; scenarioId: string | null }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()
const sandbox = useSandboxStore()

const loading = ref(false)
const card = ref<any>(null)
const snippet = ref('')
const files = ref<string[]>([])
const fi = ref<HTMLInputElement>()

watch(
  () => [props.modelValue, props.scenarioId],
  async () => {
    if (!props.modelValue || !props.scenarioId) return
    loading.value = true
    try {
      const cfg = await sandbox.config(props.scenarioId)
      card.value = cfg.card
      snippet.value = JSON.stringify(cfg.config_example || {}, null, 2)
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
  ElMessage.success('已复制 MCP 配置片段')
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
.code { position: relative; background: var(--code-bg); border: 1px solid var(--border); border-radius: var(--r-sm); padding: 12px 14px; overflow-x: auto; }
.code pre { margin: 0; font-family: var(--font-mono); font-size: var(--text-sm); color: var(--text-2); line-height: 1.6; }
.copy { position: absolute; top: 8px; right: 8px; }
.hint { font-size: var(--text-xs); color: var(--text-3); margin-top: 8px; line-height: 1.6; }
.hint.inline { margin-top: 0; }
.upload-row { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
code { font-family: var(--font-mono); color: var(--info); background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
</style>
