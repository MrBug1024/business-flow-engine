<template>
  <div class="sidebar">
    <div class="side-head">
      <span class="side-title">业务场景</span>
      <button class="add-btn" title="新建场景" @click="createDialog = true">
        <el-icon><Plus /></el-icon>
      </button>
    </div>
    <div class="side-list">
      <div v-if="!store.list.length" class="empty">
        <el-icon :size="26"><FolderOpened /></el-icon>
        <span>还没有场景</span>
        <el-button size="small" type="primary" plain :icon="Plus" @click="createDialog = true">新建场景</el-button>
      </div>
      <div
        v-for="s in store.list" :key="s.id"
        class="item" :class="{ active: s.id === store.currentId }"
        @click="$emit('select', s.id)"
      >
        <span class="dot" :class="dotClass(s)" />
        <div class="item-body">
          <span class="name">{{ s.name }}</span>
          <span class="stat" :class="dotClass(s)">{{ statusLabel(s) }}</span>
        </div>
        <button class="del" title="删除" @click.stop="remove(s)">
          <el-icon><Delete /></el-icon>
        </button>
      </div>
    </div>

    <el-dialog v-model="createDialog" title="新建业务场景" width="440px">
      <el-input v-model="newName" placeholder="场景名称（如：医保审计）" size="large" style="margin-bottom: 12px" />
      <el-input v-model="newDesc" type="textarea" :rows="2" placeholder="一句话描述（可选）" />
      <template #footer>
        <el-button @click="createDialog = false">取消</el-button>
        <el-button type="primary" :disabled="!newName.trim()" @click="doCreate">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { Plus, Delete, FolderOpened } from '@element-plus/icons-vue'
import { ElMessageBox } from 'element-plus'
import { useScenarioStore } from '@/stores/scenarios'
import type { Scenario } from '@/api/types'

const store = useScenarioStore()
const emit = defineEmits<{ (e: 'select', id: string): void }>()

const createDialog = ref(false)
const newName = ref('')
const newDesc = ref('')

const STATUS: Record<string, string> = {
  created: '未开始', tables_uploaded: '已上传', trace_sampled: '已追踪', relations_deduced: '已推关联',
  flow_deduced: '已推流程', skills_generated: '已生成技能', active: '已激活',
}

function dotClass(s: Scenario) {
  if (s.skills && s.skills.length) return 'ready'
  if (['tables_uploaded', 'trace_sampled', 'relations_deduced', 'flow_deduced'].includes(s.status)) return 'deducing'
  return ''
}
function statusLabel(s: Scenario) {
  return STATUS[s.status] || s.status || ''
}

async function doCreate() {
  const sc = await store.create(newName.value.trim(), newDesc.value.trim())
  createDialog.value = false
  newName.value = ''
  newDesc.value = ''
  emit('select', sc.id)
}

async function remove(s: Scenario) {
  try {
    await ElMessageBox.confirm(`删除场景「${s.name}」？此操作不可撤销。`, '确认删除', { type: 'warning' })
    await store.remove(s.id)
  } catch { /* cancelled */ }
}
</script>

<style scoped lang="scss">
.sidebar { height: 100%; display: flex; flex-direction: column; background: var(--surface); }
.side-head {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 12px 0 16px; height: 48px; flex-shrink: 0; border-bottom: 1px solid var(--border);
}
.side-title { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.11em; color: var(--text-3); font-weight: 700; }
.add-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border-radius: var(--r-sm);
  border: none; background: transparent; color: var(--text-2); cursor: pointer;
  transition: all var(--dur) var(--ease);
}
.add-btn:hover { background: var(--brand-soft); color: var(--brand); }

.side-list { flex: 1; overflow-y: auto; padding: 8px; display: flex; flex-direction: column; gap: 2px; }
.empty { margin: auto; display: flex; flex-direction: column; align-items: center; gap: 10px; color: var(--text-3); font-size: var(--text-base); padding: 24px 12px; text-align: center; }

.item {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 10px; border-radius: var(--r-sm); cursor: pointer;
  border: 1px solid transparent;
  transition: background var(--dur) var(--ease);
}
.item:hover { background: var(--hover); }
.item:hover .del { opacity: 1; }
.item.active { background: var(--brand-soft); border-color: color-mix(in srgb, var(--brand) 30%, transparent); }
.item.active .name { color: var(--brand); }

.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-3); flex-shrink: 0; }
.dot.ready { background: var(--success); box-shadow: 0 0 0 3px var(--success-soft); }
.dot.deducing { background: var(--warning); box-shadow: 0 0 0 3px var(--warning-soft); }

.item-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.name { font-size: var(--text-base); font-weight: 600; color: var(--text-1); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stat { font-size: var(--text-xs); color: var(--text-3); }
.stat.ready { color: var(--success); }
.stat.deducing { color: var(--warning); }

.del {
  opacity: 0; flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; border-radius: var(--r-xs);
  border: none; background: transparent; color: var(--text-3); cursor: pointer;
  transition: all var(--dur) var(--ease);
}
.del:hover { background: var(--danger-soft); color: var(--danger); }
</style>
