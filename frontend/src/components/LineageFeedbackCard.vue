<template>
  <section class="lineage-feedback-card" aria-live="polite">
    <header class="lineage-feedback-head">
      <span class="lineage-feedback-icon" aria-hidden="true">
        <el-icon><Connection /></el-icon>
      </span>
      <div>
        <strong>{{ copy.title }}</strong>
        <span>{{ copy.subtitle }}</span>
      </div>
    </header>

    <section class="lineage-feedback-section">
      <h3>{{ copy.understood }}</h3>
      <ul>
        <li v-for="item in understoodItems" :key="item">{{ item }}</li>
      </ul>
    </section>

    <section class="lineage-feedback-section next-step">
      <h3>{{ copy.next }}</h3>
      <p>{{ nextStepDetail }}</p>
    </section>

    <section v-if="choices.length" class="lineage-feedback-section decision">
      <h3>{{ copy.needConfirm }}</h3>
      <p>{{ copy.needConfirmDetail }}</p>
      <div class="lineage-choice-list" :aria-label="copy.needConfirm">
        <button
          v-for="choice in choices"
          :key="choice.id"
          type="button"
          class="lineage-choice"
          :disabled="busy"
          @click="emit('choose', choice.reply)"
        >
          <span class="choice-copy">
            <strong>
              {{ choice.label }}
              <small v-if="choice.recommended">{{ copy.recommended }}</small>
            </strong>
            <span>{{ choice.description }}</span>
          </span>
          <span class="choice-action">{{ copy.chooseAndContinue }}</span>
        </button>
      </div>
    </section>

    <footer class="lineage-feedback-actions">
      <button type="button" :disabled="busy" @click="emit('open-samples')">
        {{ copy.openSamples }}
      </button>
      <button
        v-if="!choices.length"
        type="button"
        class="continue-button"
        :disabled="busy"
        @click="emit('choose', feedback)"
      >
        {{ copy.continue }}
      </button>
      <button type="button" :disabled="busy" @click="emit('focus-composer')">
        {{ copy.addDetail }}
      </button>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Connection } from '@element-plus/icons-vue'

type Language = 'zh' | 'en'

type Choice = {
  id: string
  label: string
  description: string
  reply: string
  recommended?: boolean
}

const props = withDefaults(defineProps<{
  feedback: string
  language?: Language
  busy?: boolean
}>(), {
  language: 'zh',
  busy: false,
})

const emit = defineEmits<{
  choose: [answer: string]
  'open-samples': []
  'focus-composer': []
}>()

const copy = computed(() => props.language === 'zh'
  ? {
      title: '我已收到你的关联调整意见',
      subtitle: '会先校验已有样本，再继续追踪；不会随机抽取数据。',
      understood: '我理解的是',
      next: '接下来我会做什么',
      nextDetail: '按上述关系在已有数据链路样本中核对可用字段；校验通过后，将这条关系应用到重追踪中。',
      needConfirm: '只需确认一个选择',
      needConfirmDetail: '“医保目录名称”和“医保目录编码”有多种使用方式。请选择最符合业务规则的一种，我会据此继续，不再要求你填写表名和字段格式。',
      recommended: '推荐',
      chooseAndContinue: '选择并继续',
      openSamples: '查看已追踪样本',
      continue: '按这个理解继续',
      addDetail: '补充说明',
    }
  : {
      title: 'Your relationship adjustment was received',
      subtitle: 'I will validate it against the existing samples before tracing again.',
      understood: 'What I understood',
      next: 'What happens next',
      nextDetail: 'I will validate the proposed relationship against the existing lineage samples, then apply it to the retrace only when it is supported.',
      needConfirm: 'One choice is needed',
      needConfirmDetail: 'Choose how the catalog name and catalog code should be used. You do not need to provide table or field syntax.',
      recommended: 'Recommended',
      chooseAndContinue: 'Choose and continue',
      openSamples: 'View traced samples',
      continue: 'Continue with this understanding',
      addDetail: 'Add detail',
    })

const normalizedFeedback = computed(() => String(props.feedback || '').replace(/\s+/g, ' ').trim())
const hasProjectDetail = computed(() => /项目明细表|item\s*detail/i.test(normalizedFeedback.value))
const hasVisitId = computed(() => /就诊\s*ID|visit\s*id/i.test(normalizedFeedback.value))
const hasCatalogName = computed(() => /医保目录名称|医疗目录名称|catalog\s*name/i.test(normalizedFeedback.value))
const hasCatalogCode = computed(() => /医保目录编码|医疗目录编码|catalog\s*code/i.test(normalizedFeedback.value))
const hasResultRule = computed(() => /结果表/.test(normalizedFeedback.value) && /规则表/.test(normalizedFeedback.value))
const explicitlyNeedsChoice = computed(() => (
  /二选一|只(?:能|可)使用.{0,12}(?:名称|编码)|(?:名称|编码).{0,8}(?:必须|只能).{0,12}(?:名称|编码)/.test(normalizedFeedback.value)
))

const understoodItems = computed(() => {
  const items: string[] = []
  if (hasProjectDetail.value) {
    const fields = [
      hasVisitId.value ? '就诊ID' : '',
      hasCatalogName.value ? '医保目录名称' : '',
      hasCatalogCode.value ? '医保目录编码' : '',
    ].filter(Boolean)
    if (props.language === 'zh') {
      items.push(`以历史结果记录为锚点，用“项目明细表”中的${fields.length ? fields.join('、') : '可验证业务标识'}确认是否属于同一笔业务。`)
    } else {
      items.push(`Include the item-detail table in a composite relationship${fields.length ? ` using ${fields.join(', ')}` : ''}.`)
    }
  }
  if (hasResultRule.value && props.language === 'zh') {
    items.push('将结果中的“违规类型”和“违规说明”作为定位审计规则的线索；若样本缺少说明，保留已验证的规则证据，不伪造字段关联。')
  } else if (hasResultRule.value) {
    items.push('Use the result type and explanation together with the rule information to locate the applicable business rule.')
  }
  if (!items.length) {
    items.push(props.language === 'zh'
      ? '按你刚才补充的关联规则重新校验链路样本。'
      : 'Revalidate the lineage samples using the relationship rule you just supplied.')
  }
  return items
})

const choices = computed<Choice[]>(() => {
  if (!hasCatalogName.value || !hasCatalogCode.value || !explicitlyNeedsChoice.value) return []
  if (props.language === 'zh') {
    return [
      {
        id: 'catalog-code-preferred',
        label: '优先按医保目录编码匹配',
        description: '编码一致即可匹配；名称只用于辅助核对。',
        reply: replayWithChoice('在项目明细表的复合关联中，请保留就诊ID，并优先按医保目录编码匹配；医保目录名称只用于辅助核对。'),
        recommended: true,
      },
      {
        id: 'catalog-both-required',
        label: '名称和编码都必须一致',
        description: '两项同时一致，才视为同一目录。',
        reply: replayWithChoice('在项目明细表的复合关联中，请保留就诊ID，并要求医保目录名称和医保目录编码都一致。'),
      },
      {
        id: 'catalog-either-allowed',
        label: '名称或编码任一匹配即可',
        description: '适用于其中一项可能缺失的历史数据。',
        reply: replayWithChoice('在项目明细表的复合关联中，请保留就诊ID，医保目录名称或医保目录编码任一匹配即可。'),
      },
    ]
  }
  return [
    {
      id: 'catalog-code-preferred',
      label: 'Prefer catalog code',
      description: 'Match on code; use the name only as a cross-check.',
      reply: replayWithChoice('For the item-detail composite relationship, retain the visit ID and match primarily by catalog code. Use the catalog name only as a cross-check.'),
      recommended: true,
    },
    {
      id: 'catalog-both-required',
      label: 'Require both name and code',
      description: 'Treat it as the same catalog item only when both match.',
      reply: replayWithChoice('For the item-detail composite relationship, retain the visit ID and require both catalog name and catalog code to match.'),
    },
    {
      id: 'catalog-either-allowed',
      label: 'Allow either name or code',
      description: 'Use this when one attribute may be absent in historical data.',
      reply: replayWithChoice('For the item-detail composite relationship, retain the visit ID and allow either catalog name or catalog code to match.'),
    },
  ]
})

const nextStepDetail = computed(() => {
  if (hasCatalogName.value && hasCatalogCode.value) {
    return props.language === 'zh'
      ? `${hasResultRule.value ? '先核验“结果—项目明细”和“结果—规则”的证据；' : ''}保留就诊ID，并将目录编码作为主匹配条件、目录名称作为样本核对线索；验证通过后会自动继续重追踪。只有证据相互矛盾时，才会请你做一个业务选择。`
      : 'I will retain the visit ID and validate catalog name and catalog code as candidate conditions against existing samples. The stable, unique, non-conflicting combination will be used to continue the retrace automatically. You will only be asked to choose if the two combinations conflict.'
  }
  return copy.value.nextDetail
})

function replayWithChoice(choice: string) {
  const original = String(props.feedback || '').trim()
  const prefix = props.language === 'zh' ? '补充选择：' : 'Additional choice:'
  return original ? `${original}\n\n${prefix}${choice}` : choice
}
</script>

<style scoped>
.lineage-feedback-card {
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--chat-divider));
  border-radius: 8px;
  background: color-mix(in srgb, var(--accent-soft) 23%, var(--chat-bg));
}

.lineage-feedback-head {
  display: flex;
  gap: 9px;
  align-items: flex-start;
  padding: 12px;
  border-bottom: 1px solid color-mix(in srgb, var(--accent) 25%, var(--chat-divider));
}

.lineage-feedback-icon {
  display: grid;
  flex: 0 0 auto;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: var(--accent-soft);
  color: var(--accent);
}

.lineage-feedback-head strong,
.lineage-feedback-head span {
  display: block;
}

.lineage-feedback-head strong {
  color: var(--text-strong);
  font-size: 13px;
}

.lineage-feedback-head span:not(.lineage-feedback-icon) {
  margin-top: 3px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.45;
}

.lineage-feedback-section {
  padding: 11px 12px 0;
}

.lineage-feedback-section h3 {
  margin: 0 0 5px;
  color: var(--text-strong);
  font-size: 12px;
}

.lineage-feedback-section p,
.lineage-feedback-section li {
  color: var(--text-main);
  font-size: 12px;
  line-height: 1.55;
}

.lineage-feedback-section p {
  margin: 0;
}

.lineage-feedback-section ul {
  display: grid;
  gap: 4px;
  margin: 0;
  padding-left: 18px;
}

.next-step {
  padding-bottom: 2px;
}

.decision {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--chat-divider);
}

.lineage-choice-list {
  display: grid;
  gap: 7px;
  margin-top: 9px;
}

.lineage-choice {
  display: flex;
  gap: 9px;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 9px 10px;
  border: 1px solid var(--chat-divider);
  border-radius: 6px;
  background: var(--chat-bg);
  color: var(--text-main);
  text-align: left;
  cursor: pointer;
}

.lineage-choice:hover:not(:disabled),
.lineage-choice:focus-visible {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent-soft) 55%, var(--chat-bg));
  outline: none;
}

.lineage-choice:disabled,
.lineage-feedback-actions button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.choice-copy,
.choice-copy strong,
.choice-copy span {
  display: block;
}

.choice-copy strong {
  color: var(--text-strong);
  font-size: 12px;
}

.choice-copy strong small {
  display: inline-block;
  margin-left: 5px;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 9px;
  vertical-align: 1px;
}

.choice-copy span {
  margin-top: 3px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.4;
}

.choice-action {
  flex: 0 0 auto;
  color: var(--accent);
  font-size: 10px;
  font-weight: 600;
}

.lineage-feedback-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
  padding: 10px 12px;
  border-top: 1px solid var(--chat-divider);
}

.lineage-feedback-actions button {
  min-height: 28px;
  padding: 0 8px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--text-muted);
  font-size: 11px;
  cursor: pointer;
}

.lineage-feedback-actions button:hover:not(:disabled),
.lineage-feedback-actions button:focus-visible {
  border-color: var(--chat-divider);
  background: var(--surface-hover);
  color: var(--text-strong);
  outline: none;
}

.lineage-feedback-actions .continue-button {
  border-color: var(--accent);
  background: var(--accent);
  color: #ffffff;
}

@media (max-width: 1179px) {
  .lineage-choice,
  .lineage-feedback-actions button {
    min-height: 44px;
  }
}
</style>
