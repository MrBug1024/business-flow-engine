<template>
  <div class="outputs">
    <div v-if="!outputs.length" class="ph">
      <el-icon :size="34"><Promotion /></el-icon>
      <p>尚无产出</p>
      <span class="ph-sub">完成流程推导后，这里会列出可复刻的历史产出。</span>
    </div>
    <div v-for="o in outputs" :key="o.output_id" class="ocard card">
      <div class="ocard-head">
        <div class="oname">
          <el-icon><Promotion /></el-icon>
          <span>{{ o.name }}</span>
        </div>
        <el-tag size="small" :type="tagType(o.status)" effect="light" round>{{ statusLabel(o.status) }}</el-tag>
      </div>
      <div class="ometa mono">
        <span class="meta-chip">{{ o.fmt }}</span>
        <span v-if="o.strategy" class="meta-chip">策略 {{ o.strategy }}</span>
        <span v-if="o.pipeline_steps" class="meta-chip">管线 {{ o.pipeline_steps }} 节点</span>
      </div>
      <div class="oact">
        <el-button size="small" type="primary" plain :icon="VideoPlay" :loading="running === o.output_id" @click="run(o.output_id)">
          {{ o.result_table ? '执行并校验' : '执行' }}
        </el-button>
      </div>
      <div v-if="reports[o.output_id]" class="oreport" v-html="renderReport(reports[o.output_id])" />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { Promotion, VideoPlay } from '@element-plus/icons-vue'
import { marked } from 'marked'
import { http } from '@/api/http'
import type { Scenario } from '@/api/types'

const props = defineProps<{ scenario: Scenario | null }>()
const outputs = ref<any[]>([])
const running = ref('')
const reports = ref<Record<string, any>>({})

async function load() {
  if (!props.scenario) { outputs.value = []; return }
  const { data } = await http.get(`/scenarios/${props.scenario.id}/outputs`)
  outputs.value = data.outputs || []
}
watch(() => props.scenario?.id, load, { immediate: true })

function statusLabel(s: string) {
  return ({ verified: '已校验', executable: '可执行', blocked: '缺数据', draft: '骨架' } as any)[s] || s
}
function tagType(s: string) {
  return ({ verified: 'success', executable: 'primary', blocked: 'danger', draft: 'info' } as any)[s] || 'info'
}

async function run(outputId: string) {
  if (!props.scenario) return
  running.value = outputId
  try {
    const { data } = await http.post(`/scenarios/${props.scenario.id}/produce`, { output_id: outputId })
    reports.value = { ...reports.value, [outputId]: data }
    await load()
  } finally {
    running.value = ''
  }
}

function renderReport(r: any) {
  const link = r.artifact_url ? `\n\n[⬇ 下载产出文件](${r.artifact_url})` : ''
  const md = `**${r.passed ? '✅ 通过' : '⚠️ 未通过'}**：产出 ${r.produced_count} 行。${r.message || ''}${link}`
  return marked.parse(md) as string
}
const _ = computed(() => outputs.value.length)
</script>

<style scoped lang="scss">
.outputs { padding: 18px; display: flex; flex-direction: column; gap: 12px; overflow-y: auto; height: 100%; }
.ph { margin: auto; display: flex; flex-direction: column; align-items: center; gap: 8px; color: var(--text-3); text-align: center; }
.ph p { margin: 4px 0 0; font-size: var(--text-md); font-weight: 600; color: var(--text-2); }
.ph-sub { font-size: var(--text-base); line-height: 1.7; }

.ocard { padding: 14px 16px; transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease); }
.ocard:hover { border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
.ocard-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.oname { display: flex; align-items: center; gap: 7px; font-weight: 700; font-size: var(--text-md); color: var(--text-1); min-width: 0; }
.oname .el-icon { color: var(--brand); flex-shrink: 0; }
.oname span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.ometa { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0; }
.meta-chip { font-size: var(--text-xs); color: var(--text-3); background: var(--surface-sunken); padding: 2px 8px; border-radius: var(--r-xs); }

.oact { margin-top: 2px; }
.oreport {
  margin-top: 12px; font-size: var(--text-base);
  background: var(--surface-sunken); border: 1px solid var(--border);
  border-radius: var(--r-sm); padding: 10px 14px; line-height: 1.6;
}
.oreport :deep(p) { margin: 0; }
</style>
