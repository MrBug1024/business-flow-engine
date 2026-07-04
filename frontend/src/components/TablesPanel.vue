<template>
  <div class="tables">
    <div v-if="!tables.length" class="ph">
      <el-icon :size="34"><Grid /></el-icon>
      <p>尚未上传任何数据表</p>
      <span class="ph-sub">点击右上「上传数据」，上传业务表 / 知识表 / 历史结果表并标注角色。</span>
    </div>
    <div v-for="t in tables" :key="t.table_name" class="tcard card">
      <div class="tcard-head">
        <div class="tname">
          <el-icon><Grid /></el-icon>
          <span>{{ t.table_name }}</span>
        </div>
        <el-select :model-value="t.role" size="small" style="width: 132px" @change="(r:string) => setRole(t.table_name, r)">
          <el-option label="业务表 input" value="input" />
          <el-option label="知识表 rule" value="rule" />
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
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Grid } from '@element-plus/icons-vue'
import { http } from '@/api/http'
import type { Scenario } from '@/api/types'

const props = defineProps<{ scenario: Scenario | null }>()
const emit = defineEmits<{ (e: 'changed'): void }>()
const tables = computed(() => props.scenario?.tables_meta || [])

async function setRole(tableName: string, role: string) {
  if (!props.scenario) return
  await http.put(`/scenarios/${props.scenario.id}/tables/${encodeURIComponent(tableName)}/role`, { role })
  emit('changed')
}
</script>

<style scoped lang="scss">
.tables { padding: 18px; display: flex; flex-direction: column; gap: 12px; overflow-y: auto; height: 100%; }
.ph { margin: auto; display: flex; flex-direction: column; align-items: center; gap: 8px; color: var(--text-3); text-align: center; max-width: 380px; }
.ph p { margin: 4px 0 0; font-size: var(--text-md); font-weight: 600; color: var(--text-2); }
.ph-sub { font-size: var(--text-base); line-height: 1.7; }

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
</style>
