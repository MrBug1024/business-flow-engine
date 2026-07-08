<template>
  <div class="tables">
    <div v-if="!tables.length" class="ph">
      <el-icon :size="34"><Grid /></el-icon>
      <p>尚未上传任何数据表</p>
      <span class="ph-sub">点击右上「上传数据」，上传业务表 / 知识表 / 历史结果表并标注角色。</span>
    </div>
    <div v-else class="trace-toolbar">
      <div class="trace-toolbar-text">
        <span class="trace-title">数据链路追踪</span>
        <span class="trace-desc">{{ overallTraceSummary }}</span>
      </div>
      <el-button
        size="small"
        type="primary"
        plain
        :icon="Connection"
        :loading="traceRunning"
        @click="runTrace"
      >
        {{ hasSavedTrace ? '重新追踪' : '开始追踪' }}
      </el-button>
    </div>
    <div v-for="t in tables" :key="t.table_name" class="tcard card">
      <div class="tcard-head">
        <div class="tname">
          <el-icon><Grid /></el-icon>
          <span>{{ t.table_name }}</span>
        </div>
        <el-select :model-value="t.role" size="small" style="width: 170px" @change="(r:string) => setRole(t.table_name, r)">
          <el-option label="业务表 input" value="input" />
          <el-option label="知识表 knowledge" value="knowledge" />
          <el-option label="知识表 rule（旧）" value="rule" />
          <el-option label="结果表 result" value="result" />
          <el-option label="未定 unknown" value="unknown" />
        </el-select>
      </div>
      <div class="tmeta mono">
        <span class="meta-chip">{{ t.row_count?.toLocaleString?.() ?? t.row_count }} 行</span>
        <span class="meta-chip">{{ t.col_count }} 列</span>
      </div>
      <div v-if="t.columns && t.columns.length" class="cols">
        <span v-for="c in t.columns.slice(0, 40)" :key="c.name" class="col mono" :title="c.semantic || ''">
          {{ c.name }}<span v-if="c.semantic_role && c.semantic_role !== 'UNKNOWN'" class="crole">·{{ c.semantic_role }}</span>
        </span>
        <span v-if="t.columns.length > 40" class="more">…共 {{ t.columns.length }} 列</span>
      </div>

      <div class="trace-panel">
        <button class="trace-toggle" @click="toggleTrace(t.table_name)">
          <el-icon><Connection /></el-icon>
          <span>追踪链路样本</span>
          <span class="trace-state" :class="traceTone(t.table_name)">{{ traceLabel(t.table_name) }}</span>
          <el-icon class="chev" :class="{ open: traceOpen[t.table_name] }"><ArrowDown /></el-icon>
        </button>

        <div v-if="traceOpen[t.table_name]" class="trace-body">
          <div v-if="traceLoading" class="trace-empty">正在读取追踪链路样本…</div>
          <template v-else>
            <div class="trace-summary">
              <span>{{ traceSummary(t.table_name) }}</span>
              <button class="trace-action" @click="askAi(t.table_name)">让 AI 检查/调整</button>
            </div>
            <div v-if="traceWarning(t.table_name)" class="trace-warning">{{ traceWarning(t.table_name) }}</div>

            <div v-if="traceRows(t.table_name).length" class="sample-wrap">
              <table class="sample-table">
                <thead>
                  <tr>
                    <th v-for="c in sampleColumns(traceRows(t.table_name))" :key="c">{{ c }}</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(row, ri) in traceRows(t.table_name).slice(0, 5)" :key="ri">
                    <td v-for="c in sampleColumns(traceRows(t.table_name))" :key="c">{{ row[c] ?? '' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-else class="trace-empty">这张表当前没有追踪到链路样本。</div>
          </template>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowDown, Connection, Grid } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { http } from '@/api/http'
import type { Scenario } from '@/api/types'

const props = defineProps<{ scenario: Scenario | null }>()
const emit = defineEmits<{ (e: 'changed'): void; (e: 'ask-ai', text: string): void }>()
const tables = computed(() => props.scenario?.tables_meta || [])
const traceData = ref<any | null>(null)
const traceLoading = ref(false)
const traceRunning = ref(false)
const traceOpen = ref<Record<string, boolean>>({})
const hasSavedTrace = computed(() => !!traceData.value && traceData.value.source !== 'not_traced')
const overallTraceSummary = computed(() => {
  if (traceRunning.value) return '正在以结果表样本为锚点追踪业务表和知识表…'
  if (!traceData.value || traceData.value.source === 'not_traced') return '尚未执行。上传只解析表结构，追踪需手动启动。'
  return traceData.value.trace_summary || '已保存追踪链路样本。'
})

async function setRole(tableName: string, role: string) {
  if (!props.scenario) return
  await http.put(`/scenarios/${props.scenario.id}/tables/${encodeURIComponent(tableName)}/role`, { role })
  emit('changed')
}

async function loadTrace() {
  if (!props.scenario?.id || !props.scenario.tables_meta?.length) {
    traceData.value = null
    return
  }
  traceLoading.value = true
  try {
    const { data } = await http.get(`/scenarios/${props.scenario.id}/trace-sample`)
    traceData.value = data
  } catch {
    traceData.value = null
  } finally {
    traceLoading.value = false
  }
}

watch(() => [props.scenario?.id, props.scenario?.updated_at], loadTrace, { immediate: true })

async function runTrace() {
  if (!props.scenario?.id) return
  traceRunning.value = true
  try {
    const { data } = await http.post(`/scenarios/${props.scenario.id}/trace-sample`)
    traceData.value = data
    const level = data?.connectivity_check?.level
    if (level === 'fail') ElMessage.warning(data?.connectivity_check?.message || '链路追踪完成，但存在问题')
    else ElMessage.success('数据链路追踪完成')
    emit('changed')
  } finally {
    traceRunning.value = false
  }
}

function toggleTrace(tableName: string) {
  traceOpen.value[tableName] = !traceOpen.value[tableName]
}

function traceInfo(tableName: string) {
  if (!traceData.value) return null
  if (traceData.value.result_table === tableName) {
    return {
      matched_by: '结果入口',
      trace_confidence: 'high',
      matched_rows: traceData.value.result_sample || [],
      warning: '',
    }
  }
  return traceData.value.trace_map?.[tableName] || null
}

function traceRows(tableName: string): Record<string, any>[] {
  return traceInfo(tableName)?.matched_rows || []
}

function traceWarning(tableName: string): string {
  if (!traceData.value || traceData.value.source === 'not_traced') return '尚未执行数据链路追踪。'
  const info = traceInfo(tableName)
  if (!info) return '这张表没有出现在当前追踪链路里。'
  return info.warning || ''
}

function traceLabel(tableName: string): string {
  const info = traceInfo(tableName)
  if (!traceData.value || traceData.value.source === 'not_traced') return '未追踪'
  if (!info) return '未追踪'
  if (!info.matched_by) return '未追踪'
  if (info.matched_by === 'random') return '未进入链路'
  if (info.matched_by === '结果入口') return '结果入口'
  return `${info.matched_by || '已追踪'} · ${info.trace_confidence || '?'}`
}

function traceTone(tableName: string): string {
  const info = traceInfo(tableName)
  if (!info || info.matched_by === 'random' || info.warning) return 'warn'
  return 'ok'
}

function traceSummary(tableName: string): string {
  const info = traceInfo(tableName)
  if (!traceData.value || traceData.value.source === 'not_traced') return '尚未执行数据链路追踪。'
  if (!info) return '这张表未能匹配到追踪链路，请确认它是否参与当前业务流程。'
  if (info.matched_by === '结果入口') return '这是追踪入口表，下面展示用于反推的结果样本行。'
  if (!info.matched_by) return '这张表没有追到因果关联，不会作为后续推导样本。'
  if (info.matched_by === 'random') return '这张表没有追到因果关联；这些行不会作为后续推导样本。'
  return `通过「${info.matched_by}」追踪到 ${traceRows(tableName).length} 行，置信度 ${info.trace_confidence || 'unknown'}。`
}

function sampleColumns(rows: Record<string, any>[]): string[] {
  const cols: string[] = []
  rows.slice(0, 5).forEach((row) => {
    Object.keys(row || {}).forEach((k) => {
      if (!cols.includes(k) && cols.length < 8) cols.push(k)
    })
  })
  return cols
}

function askAi(tableName: string) {
  const info = traceInfo(tableName)
  const summary = traceSummary(tableName)
  const warning = traceWarning(tableName)
  emit('ask-ai',
    `请检查表「${tableName}」的追踪链路样本是否正确。\n` +
    `当前链路情况：${summary}\n` +
    (warning ? `问题提示：${warning}\n` : '') +
    `如果尚未追踪或需要重新追踪，请调用 trace_data_links；如果链路不正确，请先说明需要用户确认的业务键；如果用户已经给出明确字段对应关系，请调用 correct_relation 修正。不要继续推导后续步骤。` +
    (info ? `\n当前 matched_by=${info.matched_by || ''}, confidence=${info.trace_confidence || ''}` : ''),
  )
}
</script>

<style scoped lang="scss">
.tables { padding: 18px; display: flex; flex-direction: column; gap: 12px; overflow-y: auto; height: 100%; }
.ph { margin: auto; display: flex; flex-direction: column; align-items: center; gap: 8px; color: var(--text-3); text-align: center; max-width: 380px; }
.ph p { margin: 4px 0 0; font-size: var(--text-md); font-weight: 600; color: var(--text-2); }
.ph-sub { font-size: var(--text-base); line-height: 1.7; }

.trace-toolbar {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--surface); flex-shrink: 0;
}
.trace-toolbar-text { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.trace-title { font-size: var(--text-sm); font-weight: 800; color: var(--text-1); }
.trace-desc {
  font-size: var(--text-xs); color: var(--text-3);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}

.tcard { padding: 14px 16px; transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease); }
.tcard:hover { border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
.tcard-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.tname { display: flex; align-items: center; gap: 7px; font-weight: 700; font-size: var(--text-md); color: var(--text-1); min-width: 0; }
.tname .el-icon { color: var(--brand); flex-shrink: 0; }
.tname span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.tmeta { display: flex; gap: 6px; margin: 10px 0; }
.meta-chip { font-size: var(--text-xs); color: var(--text-3); background: var(--surface-sunken); padding: 2px 8px; border-radius: var(--r-xs); }

.cols { display: flex; flex-wrap: wrap; gap: 5px; }
.col { font-size: var(--text-xs); padding: 3px 8px; border-radius: var(--r-xs); background: var(--surface); border: 1px solid var(--border); color: var(--text-2); }
.crole { color: var(--warning); }
.more { font-size: var(--text-xs); color: var(--text-3); align-self: center; }

.trace-panel { margin-top: 12px; border-top: 1px solid var(--border); padding-top: 10px; }
.trace-toggle {
  width: 100%; display: flex; align-items: center; gap: 7px;
  border: none; background: transparent; color: var(--text-2);
  padding: 4px 0; cursor: pointer; font-size: var(--text-sm); font-weight: 700;
}
.trace-state {
  margin-left: auto; font-size: var(--text-xs); font-weight: 700;
  padding: 2px 7px; border-radius: var(--r-full);
}
.trace-state.ok { background: var(--success-soft); color: var(--success); }
.trace-state.warn { background: var(--warning-soft); color: var(--warning); }
.chev { transition: transform var(--dur) var(--ease); }
.chev.open { transform: rotate(180deg); }
.trace-body { margin-top: 8px; display: flex; flex-direction: column; gap: 8px; }
.trace-summary {
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  font-size: var(--text-sm); color: var(--text-2);
}
.trace-action {
  border: 1px solid var(--border); background: var(--surface);
  color: var(--brand); border-radius: var(--r-xs); padding: 4px 9px;
  cursor: pointer; font-size: var(--text-xs); font-weight: 700; flex-shrink: 0;
}
.trace-action:hover { border-color: var(--brand); background: var(--brand-soft); }
.trace-warning {
  font-size: var(--text-sm); color: var(--warning);
  background: var(--warning-soft); border: 1px solid color-mix(in srgb, var(--warning) 22%, transparent);
  padding: 7px 9px; border-radius: var(--r-xs);
}
.trace-empty { font-size: var(--text-sm); color: var(--text-3); padding: 8px 0; }
.sample-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: var(--r-xs); }
.sample-table { width: 100%; border-collapse: collapse; font-size: var(--text-xs); }
.sample-table th, .sample-table td {
  border-bottom: 1px solid var(--border); border-right: 1px solid var(--border);
  padding: 6px 8px; text-align: left; white-space: nowrap; max-width: 220px;
  overflow: hidden; text-overflow: ellipsis;
}
.sample-table th { background: var(--surface-sunken); color: var(--text-2); font-weight: 700; }
.sample-table td { color: var(--text-2); }
.sample-table tr:last-child td { border-bottom: none; }
.sample-table th:last-child, .sample-table td:last-child { border-right: none; }
</style>
