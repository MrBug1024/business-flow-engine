<template>
  <div class="qpanel">
    <div class="qpanel-head">
      <span class="qp-title">
        <el-icon><QuestionFilled /></el-icon>
        {{ interaction.title || '请确认以下事项' }}
      </span>
      <span class="qp-count">{{ answeredCount }}/{{ interaction.questions.length }}</span>
    </div>

    <div class="qp-body">
      <div v-for="(q, idx) in interaction.questions" :key="q.id || idx" class="qcard">
        <div class="qcard-q"><span class="q-idx">{{ idx + 1 }}</span>{{ q.question }}</div>
        <div v-if="q.options && q.options.length" class="qcard-opts">
          <button
            v-for="opt in q.options" :key="opt"
            class="chip" :class="{ active: isSelected(idx, opt) }"
            @click="toggle(idx, opt, q.multi_select)"
          >{{ opt }}</button>
        </div>
        <el-input
          v-if="q.allow_custom"
          v-model="custom[idx]"
          type="textarea" :autosize="{ minRows: 1, maxRows: 3 }"
          :placeholder="q.options && q.options.length ? '或自行补充你的回答…' : '请输入你的回答…'"
          class="qcard-custom"
        />
      </div>
    </div>

    <div class="qpanel-foot">
      <el-button text size="small" @click="$emit('dismiss')">忽略</el-button>
      <el-button type="primary" size="small" :disabled="answeredCount === 0" @click="submit">
        提交回答（{{ answeredCount }} 项）
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, computed } from 'vue'
import { QuestionFilled } from '@element-plus/icons-vue'
import type { Interaction } from '@/api/types'

const props = defineProps<{ interaction: Interaction }>()
const emit = defineEmits<{ (e: 'submit', text: string): void; (e: 'dismiss'): void }>()

// 每题选中的选项集合
const picked = reactive<Record<number, Set<string>>>({})
const custom = reactive<Record<number, string>>({})
props.interaction.questions.forEach((_, i) => (picked[i] = new Set()))

function isSelected(idx: number, opt: string) {
  return picked[idx]?.has(opt)
}
function toggle(idx: number, opt: string, multi: boolean) {
  const set = picked[idx]
  if (set.has(opt)) set.delete(opt)
  else {
    if (!multi) set.clear()
    set.add(opt)
  }
}

const answeredCount = computed(() => {
  let n = 0
  props.interaction.questions.forEach((_, i) => {
    if (picked[i]?.size || (custom[i] && custom[i].trim())) n++
  })
  return n
})

function submit() {
  const lines: string[] = []
  props.interaction.questions.forEach((q, i) => {
    const parts: string[] = []
    if (picked[i]?.size) parts.push([...picked[i]].join('、'))
    if (custom[i] && custom[i].trim()) parts.push(custom[i].trim())
    if (parts.length) lines.push(`关于「${q.question}」：${parts.join('；补充：')}`)
  })
  if (lines.length) emit('submit', lines.join('\n'))
}
</script>

<style scoped lang="scss">
.qpanel {
  margin: 0 18px 4px;
  border: 1px solid color-mix(in srgb, var(--brand) 40%, transparent);
  background: var(--surface);
  border-radius: var(--r-lg);
  overflow: hidden;
  box-shadow: var(--shadow);
}
.qpanel-head {
  display: flex; justify-content: space-between; align-items: center;
  padding: 11px 15px; background: var(--brand-soft);
  font-size: var(--text-base); font-weight: 700; color: var(--brand);
}
.qp-title { display: flex; align-items: center; gap: 7px; }
.qp-count { font-family: var(--font-mono); font-size: var(--text-xs); color: var(--text-3); }
.qp-body { padding: 12px 15px; display: flex; flex-direction: column; gap: 14px; max-height: 44vh; overflow-y: auto; }
.qcard-q { display: flex; align-items: baseline; gap: 8px; font-size: var(--text-base); line-height: 1.55; margin-bottom: 9px; color: var(--text-1); }
.q-idx {
  flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 50%;
  background: var(--brand-soft-2); color: var(--brand);
  font-size: var(--text-xs); font-weight: 700;
}
.qcard-opts { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 7px; }
.chip {
  padding: 6px 13px; border-radius: var(--r-full); font-size: var(--text-sm); cursor: pointer;
  background: var(--surface-2); border: 1px solid var(--border); color: var(--text-2);
  transition: all var(--dur) var(--ease);
}
.chip:hover { border-color: var(--border-strong); color: var(--text-1); }
.chip.active { background: var(--brand-soft-2); border-color: var(--brand); color: var(--brand); font-weight: 600; }
.qcard-custom { margin-top: 2px; }
.qpanel-foot {
  display: flex; justify-content: flex-end; gap: 8px; padding: 10px 15px;
  border-top: 1px solid var(--border); background: var(--surface-2);
}
</style>
