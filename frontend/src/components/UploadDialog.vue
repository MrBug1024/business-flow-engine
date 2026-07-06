<template>
  <el-dialog :model-value="modelValue" title="上传数据并标注角色" width="560px" @update:model-value="close">
    <div class="picker" :class="{ filled: rows.length }" @click="fileInput?.click()">
      <input ref="fileInput" type="file" multiple style="display: none" @change="onPick" />
      <el-icon :size="26"><UploadFilled /></el-icon>
      <div class="picker-text">
        <strong>点击选择文件</strong>
        <span>支持 CSV / TSV / Excel / JSON，可多选</span>
      </div>
    </div>

    <div v-if="rows.length" class="rows">
      <div v-for="(r, i) in rows" :key="i" class="row">
        <el-icon class="fico"><Document /></el-icon>
        <span class="fname">{{ r.file.name }}</span>
        <el-select v-model="r.role" size="small" style="width: 170px">
          <el-option label="业务表 input" value="input" />
          <el-option label="知识表 knowledge" value="knowledge" />
          <el-option label="结果表 result" value="result" />
        </el-select>
      </div>
    </div>

    <template #footer>
      <el-button @click="close">取消</el-button>
      <el-button type="primary" :loading="uploading" :disabled="!rows.length" @click="upload">
        上传{{ rows.length ? ` (${rows.length})` : '' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { UploadFilled, Document } from '@element-plus/icons-vue'
import { http } from '@/api/http'

const props = defineProps<{ modelValue: boolean; scenarioId: string }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void; (e: 'uploaded'): void }>()

const fileInput = ref<HTMLInputElement>()
const rows = ref<{ file: File; role: string }[]>([])
const uploading = ref(false)

function guessRole(name: string): string {
  const n = name.toLowerCase()
  if (/(规则|知识|标准|目录|rule|knowledge|standard|catalog)/.test(n)) return 'knowledge'
  if (/(结果|result|违规|输出)/.test(n)) return 'result'
  return 'input'
}
function onPick(e: Event) {
  const files = (e.target as HTMLInputElement).files
  if (!files) return
  rows.value = Array.from(files).map((f) => ({ file: f, role: guessRole(f.name) }))
}

async function upload() {
  if (!rows.value.length) return
  uploading.value = true
  try {
    const form = new FormData()
    rows.value.forEach((r) => form.append('files', r.file))
    form.append('roles', JSON.stringify(rows.value.map((r) => r.role)))
    await http.post(`/scenarios/${props.scenarioId}/uploads`, form)
    ElMessage.success(`已上传 ${rows.value.length} 个文件`)
    rows.value = []
    emit('uploaded')
    close()
  } finally {
    uploading.value = false
  }
}
function close() {
  emit('update:modelValue', false)
}
</script>

<style scoped lang="scss">
.picker {
  display: flex; align-items: center; gap: 14px;
  padding: 20px; border: 1.5px dashed var(--border-strong); border-radius: var(--r);
  background: var(--surface-2); cursor: pointer; margin-bottom: 14px;
  transition: all var(--dur) var(--ease); color: var(--text-3);
}
.picker:hover { border-color: var(--brand); background: var(--brand-soft); color: var(--brand); }
.picker.filled { padding: 14px 20px; }
.picker-text { display: flex; flex-direction: column; gap: 2px; }
.picker-text strong { font-size: var(--text-md); color: var(--text-1); }
.picker:hover .picker-text strong { color: var(--brand); }
.picker-text span { font-size: var(--text-sm); color: var(--text-3); }

.rows { display: flex; flex-direction: column; gap: 8px; }
.row { display: flex; align-items: center; gap: 10px; padding: 9px 12px; background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--r-sm); }
.fico { color: var(--brand); flex-shrink: 0; }
.fname { flex: 1; font-size: var(--text-base); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-1); }
</style>
