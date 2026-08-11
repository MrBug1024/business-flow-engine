<template>
  <section
    v-if="currentQuestion"
    class="clarification-sheet"
    :class="{ 'has-tabs': questions.length > 1, 'is-approval': isApprovalQuestion }"
    role="dialog"
    aria-modal="false"
    :aria-labelledby="`clarification-title-${currentQuestion.id}`"
    :style="sheetStyle"
    @keydown.esc.stop.prevent="closeIfAllowed"
  >
    <header class="sheet-head">
      <div>
        <span>{{ isApprovalQuestion ? labels.approvalEyebrow : labels.eyebrow }}</span>
        <strong>{{ isApprovalQuestion ? labels.approvalTitle : labels.title }}</strong>
      </div>
      <button
        v-if="!hasApprovalQuestion"
        type="button"
        :title="labels.close"
        :aria-label="labels.close"
        @click="emit('close')"
      >
        <el-icon><Close /></el-icon>
      </button>
    </header>

    <div v-if="questions.length > 1" class="question-tabs" role="tablist" :aria-label="labels.questions">
      <button
        v-for="(question, index) in questions"
        :id="`clarification-tab-${question.id}`"
        :key="question.id"
        type="button"
        role="tab"
        :aria-controls="`clarification-panel-${question.id}`"
        :aria-selected="question.id === currentQuestion.id"
        :tabindex="question.id === currentQuestion.id ? 0 : -1"
        :class="{ active: question.id === currentQuestion.id }"
        @click="selectQuestion(question.id)"
      >
        <span>{{ index + 1 }}</span>
        {{ question.category || `${labels.question} ${index + 1}` }}
      </button>
    </div>

    <div
      :id="`clarification-panel-${currentQuestion.id}`"
      class="question-panel"
      role="tabpanel"
      :aria-labelledby="questions.length > 1
        ? `clarification-tab-${currentQuestion.id}`
        : `clarification-title-${currentQuestion.id}`"
    >
      <p v-if="currentQuestion.reason" class="question-reason">{{ currentQuestion.reason }}</p>
      <h2 :id="`clarification-title-${currentQuestion.id}`">{{ currentQuestion.question }}</h2>

      <div v-if="isApprovalQuestion" class="approval-review">
        <div>
          <span>{{ currentQuestion.approval?.authority?.display_label || currentQuestion.approval?.label || labels.approvalArtifact }}</span>
          <small v-if="currentQuestion.approval?.authority?.artifact_id || currentQuestion.approval?.artifact_id">
            {{ currentQuestion.approval?.authority?.artifact_id || currentQuestion.approval?.artifact_id }}
          </small>
        </div>
        <el-button plain size="small" @click="emit('open-review', currentQuestion)">
          {{ currentQuestion.approval?.authority?.review_target?.label || currentQuestion.review_target?.label || labels.openReview }}
        </el-button>
      </div>

      <div v-if="currentOptions.length" class="option-list" :aria-label="labels.quickChoices">
        <button
          v-for="(option, index) in currentOptions"
          :key="option.id || `${option.label}-${index}`"
          type="button"
          class="option-button"
          :class="{ selected: isOptionSelected(option) }"
          :aria-pressed="isOptionSelected(option)"
          @click="chooseOption(option)"
        >
          <span class="option-check" aria-hidden="true">
            <el-icon><CircleCheck /></el-icon>
          </span>
          <span class="option-copy">
            <strong>
              {{ option.label }}
              <small v-if="option.recommended">{{ labels.recommended }}</small>
            </strong>
            <span v-if="option.description">{{ option.description }}</span>
          </span>
        </button>
      </div>

      <label class="custom-answer">
        <span>{{ isApprovalQuestion
          ? (selectedOption?.value === 'rejected' ? labels.rejectionNote : labels.approvalNote)
          : (currentOptions.length ? labels.customLabel : labels.answerLabel) }}</span>
        <textarea
          v-model="answers[currentQuestion.id]"
          rows="3"
          :placeholder="isApprovalQuestion
            ? (selectedOption?.value === 'rejected' ? labels.rejectionPlaceholder : labels.approvalPlaceholder)
            : labels.placeholder"
          @keydown.ctrl.enter.prevent="submitCurrent"
          @keydown.meta.enter.prevent="submitCurrent"
        />
      </label>
    </div>

    <footer class="sheet-actions">
      <span>{{ currentIndex + 1 }} / {{ questions.length }}</span>
      <el-button
        type="primary"
        :loading="submitting"
        :disabled="!canSubmitCurrent"
        @click="submitCurrent"
      >
        {{ isApprovalQuestion ? labels.submitApproval : (questions.length > 1 ? labels.submitNext : labels.submit) }}
      </el-button>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, reactive, watch } from 'vue'
import { CircleCheck, Close } from '@element-plus/icons-vue'

type Question = {
  id: string
  question: string
  reason?: string
  category?: string
  source?: string
  approval?: {
    phase?: string
    label?: string
    artifact_id?: string
    authority?: {
      artifact_id?: string
      display_label?: string
      review_target?: {
        kind: 'data_catalog' | 'workspace_file'
        path?: string
        label?: string
      }
    }
  }
  review_target?: {
    kind: 'data_catalog' | 'workspace_file'
    path?: string
    label?: string
  }
  options?: Array<{
    id?: string
    label: string
    description?: string
    recommended?: boolean
    value?: 'approved' | 'rejected' | string
  }>
}

const props = withDefaults(defineProps<{
  questions: Question[]
  activeId?: string
  language?: 'zh' | 'en'
  submitting?: boolean
  bottomOffset?: number
}>(), {
  activeId: '',
  language: 'zh',
  submitting: false,
  bottomOffset: 96,
})

const emit = defineEmits<{
  close: []
  submit: [payload: {
    question: Question
    answer: string
    optionId?: string
    optionValue?: string
  }]
  'open-review': [question: Question]
  'update:activeId': [id: string]
}>()

const answers = reactive<Record<string, string>>({})
const selectedOptionIds = reactive<Record<string, string>>({})
const sheetStyle = computed(() => ({ '--sheet-bottom-offset': `${Math.max(0, props.bottomOffset)}px` }))
const labels = computed(() => props.language === 'zh'
  ? {
      answerLabel: '你的回答',
      approvalArtifact: '待审批产物',
      approvalEyebrow: 'Agent 正在等待你的决定',
      approvalNote: '审批说明（可选）',
      approvalPlaceholder: '可补充批准说明，或填写驳回后的修改要求...',
      approvalTitle: '审批待办',
      close: '关闭问答面板',
      customLabel: '自定义回答或补充说明',
      eyebrow: 'Agent 正在等待',
      placeholder: '输入你的选择、约束或其他说明...',
      question: '问题',
      questions: '待回答问题',
      quickChoices: '快速选择',
      recommended: '推荐',
      rejectionNote: '退回原因（必填）',
      rejectionPlaceholder: '说明哪一处数据、关系或流程不正确，以及应如何修改...',
      openReview: '查看待审批数据/产物',
      submit: '提交回答',
      submitApproval: '提交审批',
      submitNext: '提交并继续',
      title: '需要你确认',
    }
  : {
      answerLabel: 'Your answer',
      approvalArtifact: 'Artifact awaiting review',
      approvalEyebrow: 'Agent is waiting for your decision',
      approvalNote: 'Review note (optional)',
      approvalPlaceholder: 'Add an approval note or explain what should change after rejection...',
      approvalTitle: 'Approval required',
      close: 'Close questions',
      customLabel: 'Custom answer or additional context',
      eyebrow: 'Agent is waiting',
      placeholder: 'Describe your choice, constraints, or another answer...',
      question: 'Question',
      questions: 'Questions to answer',
      quickChoices: 'Quick choices',
      recommended: 'Recommended',
      rejectionNote: 'Reason for rejection (required)',
      rejectionPlaceholder: 'Describe what is incorrect and what should be changed...',
      openReview: 'Review data or artifact',
      submit: 'Submit answer',
      submitApproval: 'Submit decision',
      submitNext: 'Submit and continue',
      title: 'Input needed',
    })

const currentQuestion = computed(() => (
  props.questions.find((question) => question.id === props.activeId) || props.questions[0] || null
))
const currentIndex = computed(() => Math.max(0, props.questions.findIndex((question) => question.id === currentQuestion.value?.id)))
const currentOptions = computed(() => Array.isArray(currentQuestion.value?.options) ? currentQuestion.value.options : [])
const answerForCurrent = computed(() => currentQuestion.value ? answers[currentQuestion.value.id] || '' : '')
const isApprovalQuestion = computed(() => currentQuestion.value?.source === 'distillation_approval')
const hasApprovalQuestion = computed(() => props.questions.some((question) => question.source === 'distillation_approval'))
const selectedOption = computed(() => {
  if (!currentQuestion.value) return null
  const selectedId = selectedOptionIds[currentQuestion.value.id]
  return currentOptions.value.find((option, index) => optionKey(option, index) === selectedId) || null
})
const canSubmitCurrent = computed(() => (
  isApprovalQuestion.value
    ? Boolean(
        selectedOption.value?.value
        && (selectedOption.value.value !== 'rejected' || answerForCurrent.value.trim()),
      )
    : Boolean(answerForCurrent.value.trim())
))

watch(
  () => props.questions.map((question) => question.id).join('|'),
  () => {
    const questionIds = new Set(props.questions.map((question) => question.id))
    for (const id of Object.keys(answers)) {
      if (!questionIds.has(id)) delete answers[id]
    }
    for (const id of Object.keys(selectedOptionIds)) {
      if (!questionIds.has(id)) delete selectedOptionIds[id]
    }
    if (!props.questions.length) return
    if (!props.questions.some((question) => question.id === props.activeId)) {
      emit('update:activeId', props.questions[0].id)
    }
  },
  { immediate: true },
)

function selectQuestion(id: string) {
  emit('update:activeId', id)
}

function closeIfAllowed() {
  if (!hasApprovalQuestion.value) emit('close')
}

function optionAnswer(option: NonNullable<Question['options']>[number]) {
  return [option.label, option.description].filter(Boolean).join('：')
}

function optionKey(option: NonNullable<Question['options']>[number], index = currentOptions.value.indexOf(option)) {
  return option.id || `${option.label}-${index}`
}

function isOptionSelected(option: NonNullable<Question['options']>[number]) {
  if (!currentQuestion.value) return false
  if (isApprovalQuestion.value) {
    return selectedOptionIds[currentQuestion.value.id] === optionKey(option)
  }
  return answerForCurrent.value === optionAnswer(option)
}

function chooseOption(option: NonNullable<Question['options']>[number]) {
  if (!currentQuestion.value) return
  if (isApprovalQuestion.value) {
    selectedOptionIds[currentQuestion.value.id] = optionKey(option)
    return
  }
  answers[currentQuestion.value.id] = optionAnswer(option)
}

function submitCurrent() {
  if (!currentQuestion.value || !canSubmitCurrent.value || props.submitting) return
  const option = isApprovalQuestion.value ? selectedOption.value : null
  emit('submit', {
    question: currentQuestion.value,
    answer: answerForCurrent.value.trim() || option?.label || '',
    optionId: option?.id,
    optionValue: option?.value,
  })
}
</script>

<style scoped>
.clarification-sheet {
  position: absolute;
  right: 10px;
  bottom: var(--sheet-bottom-offset, 96px);
  left: 10px;
  z-index: 20;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  max-height: min(620px, calc(100% - var(--sheet-bottom-offset, 96px) - 18px));
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--accent) 42%, var(--chat-divider));
  border-radius: 8px;
  background: var(--chat-header);
  box-shadow: 0 18px 46px rgba(0, 0, 0, 0.34);
}

.clarification-sheet.has-tabs {
  grid-template-rows: auto auto minmax(0, 1fr) auto;
}

.clarification-sheet.is-approval {
  border-color: color-mix(in srgb, var(--accent) 68%, var(--chat-divider));
}

.sheet-head,
.sheet-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.sheet-head {
  min-height: 54px;
  padding: 8px 10px 8px 14px;
  border-bottom: 1px solid var(--chat-divider);
}

.sheet-head span,
.sheet-head strong {
  display: block;
}

.sheet-head span {
  color: var(--text-muted);
  font-size: 10px;
}

.sheet-head strong {
  margin-top: 2px;
  color: var(--text-strong);
  font-size: 13px;
}

.sheet-head button {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.sheet-head button:hover,
.sheet-head button:focus-visible {
  background: var(--surface-hover);
  color: var(--text-strong);
}

.sheet-head button:focus-visible,
.question-tabs button:focus-visible,
.option-button:focus-visible,
.custom-answer textarea:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.question-tabs {
  display: flex;
  gap: 4px;
  min-height: 40px;
  overflow-x: auto;
  padding: 6px 10px;
  border-bottom: 1px solid var(--chat-divider);
  scrollbar-width: thin;
}

.question-tabs button {
  display: inline-flex;
  flex: 0 0 auto;
  gap: 6px;
  align-items: center;
  min-height: 28px;
  padding: 0 9px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--text-muted);
  font-size: 11px;
  cursor: pointer;
}

.question-tabs button > span {
  display: grid;
  place-items: center;
  width: 17px;
  height: 17px;
  border-radius: 50%;
  background: var(--surface-3);
  font-size: 9px;
  font-variant-numeric: tabular-nums;
}

.question-tabs button:hover,
.question-tabs button.active {
  border-color: var(--chat-divider);
  background: var(--surface-hover);
  color: var(--text-strong);
}

.question-tabs button.active > span {
  background: var(--accent);
  color: #ffffff;
}

.question-panel {
  min-height: 0;
  overflow: auto;
  padding: 14px;
}

.question-reason {
  margin: 0 0 7px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
}

.question-panel h2 {
  margin: 0 0 12px;
  color: var(--text-strong);
  font-size: 14px;
  line-height: 1.5;
  letter-spacing: 0;
}

.approval-review {
  display: flex;
  gap: 10px;
  align-items: center;
  justify-content: space-between;
  margin: 0 0 12px;
  padding: 9px 10px;
  border: 1px solid color-mix(in srgb, var(--accent) 34%, var(--chat-divider));
  border-radius: 6px;
  background: color-mix(in srgb, var(--accent-soft) 48%, var(--chat-bg));
}

.approval-review > div,
.approval-review span,
.approval-review small {
  display: block;
  min-width: 0;
}

.approval-review span {
  color: var(--text-strong);
  font-size: 12px;
  font-weight: 650;
}

.approval-review small {
  margin-top: 3px;
  overflow-wrap: anywhere;
  color: var(--text-muted);
  font-size: 10px;
}

.option-list {
  display: grid;
  gap: 6px;
  margin-bottom: 12px;
}

.option-button {
  display: grid;
  grid-template-columns: 22px minmax(0, 1fr);
  gap: 8px;
  width: 100%;
  padding: 8px 9px;
  border: 1px solid var(--chat-divider);
  border-radius: 6px;
  background: var(--chat-bg);
  color: var(--text-main);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.16s ease, background-color 0.16s ease;
}

.option-button:hover,
.option-button.selected {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent-soft) 55%, var(--chat-bg));
}

.option-check {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  color: var(--text-muted);
}

.option-button.selected .option-check {
  color: var(--accent);
}

.option-copy,
.option-copy strong,
.option-copy > span {
  display: block;
  min-width: 0;
}

.option-copy strong {
  color: var(--text-strong);
  font-size: 12px;
  font-weight: 600;
}

.option-copy > span {
  margin-top: 3px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.45;
}

.option-copy small {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 9px;
  vertical-align: 1px;
}

.custom-answer {
  display: grid;
  gap: 6px;
  color: var(--text-muted);
  font-size: 11px;
}

.custom-answer textarea {
  width: 100%;
  min-height: 72px;
  padding: 9px 10px;
  border: 1px solid var(--chat-divider);
  border-radius: 6px;
  outline: 0;
  resize: vertical;
  background: var(--chat-bg);
  color: var(--text-main);
  font: inherit;
  font-size: 12px;
  line-height: 1.5;
}

.sheet-actions {
  position: relative;
  z-index: 1;
  flex: 0 0 auto;
  min-height: 52px;
  padding: 8px 10px max(8px, env(safe-area-inset-bottom)) 14px;
  border-top: 1px solid var(--chat-divider);
  background: var(--chat-header);
}

.sheet-actions > span {
  color: var(--text-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

@media (max-width: 1179px) {
  .clarification-sheet {
    right: 8px;
    left: 8px;
    max-height: calc(100% - var(--sheet-bottom-offset, 96px) - 14px);
  }

  .sheet-head button,
  .question-tabs button,
  .sheet-actions :deep(.el-button) {
    min-height: 44px;
  }

  .custom-answer textarea {
    min-height: 88px;
    font-size: 16px;
  }

  .approval-review {
    align-items: stretch;
    flex-direction: column;
  }
}

@media (prefers-reduced-motion: reduce) {
  .option-button {
    transition: none;
  }
}
</style>
