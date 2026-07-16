<template>
  <details v-if="events.length || plan.length || active" class="agent-activity" :open="active">
    <summary>
      <span class="activity-state" :class="{ active }">
        <el-icon v-if="active" class="spin"><Loading /></el-icon>
        <el-icon v-else><CircleCheck /></el-icon>
        {{ active ? labels.working : labels.completed }}
      </span>
      <span class="summary-detail">{{ summaryText }}</span>
      <el-icon class="chevron"><ArrowRight /></el-icon>
    </summary>

    <div class="activity-body">
      <ol v-if="plan.length" class="agent-plan">
        <li v-for="(item, index) in plan" :key="`${item}-${index}`">
          <span>{{ index + 1 }}</span>
          <p>{{ item }}</p>
        </li>
      </ol>

      <section v-if="reasoning" class="reasoning-block">
        <header>
          <el-icon><Opportunity /></el-icon>
          <strong>{{ labels.reasoning }}</strong>
        </header>
        <p>{{ reasoning }}</p>
      </section>

      <ol v-if="activityRows.length" class="call-list">
        <li
          v-for="row in activityRows"
          :key="row.key"
          class="activity-item"
          :class="[row.kind === 'skill' ? 'skill-activity' : 'standalone-event', row.event.type]"
        >
          <template v-if="row.kind === 'skill'">
            <div class="event-row skill-event-row">
              <span class="call-icon" :class="row.event.type">
                <el-icon :class="{ spin: row.event.status === 'running' }">
                  <component :is="eventIcon(row.event)" />
                </el-icon>
              </span>
              <div class="event-copy">
                <strong :title="eventLabel(row.event)">{{ eventLabel(row.event) }}</strong>
                <span class="event-description">{{ eventDescription(row.event) }}</span>
              </div>
              <em :class="row.event.status">{{ statusLabel(row.event.status) }}</em>
            </div>

            <ol v-if="row.children.length" class="skill-children" :aria-label="labels.skillSteps">
              <li
                v-for="child in row.children"
                :key="eventKey(child)"
                class="skill-child"
                :class="child.type"
              >
                <div class="event-row child-event-row">
                  <span class="call-icon" :class="child.type">
                    <el-icon :class="{ spin: child.status === 'running' }">
                      <component :is="eventIcon(child)" />
                    </el-icon>
                  </span>
                  <div class="event-copy">
                    <strong :title="eventLabel(child)">{{ eventLabel(child) }}</strong>
                    <span class="event-description">{{ eventDescription(child) }}</span>
                  </div>
                  <em :class="child.status">{{ statusLabel(child.status) }}</em>
                </div>
              </li>
            </ol>
          </template>

          <div v-else class="event-row">
            <span class="call-icon" :class="row.event.type">
              <el-icon :class="{ spin: row.event.status === 'running' }">
                <component :is="eventIcon(row.event)" />
              </el-icon>
            </span>
            <div class="event-copy">
              <strong :title="eventLabel(row.event)">{{ eventLabel(row.event) }}</strong>
              <span class="event-description">{{ eventDescription(row.event) }}</span>
            </div>
            <em :class="row.event.status">{{ statusLabel(row.event.status) }}</em>
          </div>
        </li>
      </ol>
    </div>
  </details>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  ArrowRight,
  CircleCheck,
  Connection,
  Cpu,
  Document,
  Loading,
  MagicStick,
  Monitor,
  Opportunity,
  Tools,
} from '@element-plus/icons-vue'

type TraceEvent = Record<string, any> & { type: string }
type SkillActivityRow = {
  kind: 'skill'
  key: string
  index: number
  event: TraceEvent
  children: TraceEvent[]
}
type StandaloneActivityRow = {
  kind: 'event'
  key: string
  index: number
  event: TraceEvent
}
type ActivityRow = SkillActivityRow | StandaloneActivityRow

const TRACE_TYPES = new Set([
  'tool_call',
  'skill_activation',
  'skill_resource',
  'sandbox_command',
  'skill_load',
  'skill_call',
  'mcp_call',
  'model_call',
  'context_read',
])
const SKILL_CHILD_TYPES = new Set(['skill_resource', 'sandbox_command', 'tool_call', 'mcp_call'])

const props = withDefaults(defineProps<{
  plan?: string[]
  events?: any[]
  active?: boolean
  language?: 'zh' | 'en'
}>(), {
  plan: () => [],
  events: () => [],
  active: false,
  language: 'zh',
})

const copy = {
  zh: {
    working: '正在处理',
    completed: '工作过程',
    reasoning: '思考摘要',
    skillSteps: 'Skill 内部步骤',
    calling: '正在调用...',
    completedCall: '调用完成',
    activatingSkill: '正在加载完整 Skill 包...',
    activatedSkill: '完整 Skill 已激活',
    readingResource: '正在读取 Skill 资源...',
    resourceRead: 'Skill 资源已读取',
    runningCommand: '正在隔离沙箱中执行...',
    commandCompleted: '沙箱命令已完成',
    unnamed: '未命名',
    types: {
      tool_call: 'Tool',
      skill_activation: 'Skill',
      skill_resource: '资源',
      sandbox_command: 'Sandbox',
      skill_load: 'Skill',
      skill_call: 'Skill',
      mcp_call: 'MCP',
      model_call: 'Model',
      context_read: 'Context',
    } as Record<string, string>,
    statuses: {
      running: '进行中',
      succeeded: '完成',
      completed: '完成',
      loaded: '已加载',
      failed: '失败',
      cancelled: '已取消',
      pending: '等待中',
      streaming: '生成中',
    } as Record<string, string>,
  },
  en: {
    working: 'Working',
    completed: 'Process',
    reasoning: 'Reasoning summary',
    skillSteps: 'Steps within this Skill',
    calling: 'Calling...',
    completedCall: 'Completed',
    activatingSkill: 'Loading the complete Skill package...',
    activatedSkill: 'Complete Skill activated',
    readingResource: 'Reading a Skill resource...',
    resourceRead: 'Skill resource loaded',
    runningCommand: 'Running inside the isolated sandbox...',
    commandCompleted: 'Sandbox command completed',
    unnamed: 'Unnamed',
    types: {
      tool_call: 'Tool',
      skill_activation: 'Skill',
      skill_resource: 'Resource',
      sandbox_command: 'Sandbox',
      skill_load: 'Skill',
      skill_call: 'Skill',
      mcp_call: 'MCP',
      model_call: 'Model',
      context_read: 'Context',
    } as Record<string, string>,
    statuses: {
      running: 'Running',
      succeeded: 'Done',
      completed: 'Done',
      loaded: 'Loaded',
      failed: 'Failed',
      cancelled: 'Cancelled',
      pending: 'Pending',
      streaming: 'Streaming',
    } as Record<string, string>,
  },
}

const labels = computed(() => copy[props.language])

const reasoning = computed(() => props.events
  .filter((item) => item.type === 'reasoning')
  .map((item) => item.content || '')
  .join('')
  .trim())

const callEvents = computed<TraceEvent[]>(() => {
  const rows: TraceEvent[] = []
  const indexes = new Map<string, number>()
  for (const [rawIndex, event] of props.events.entries()) {
    if (!TRACE_TYPES.has(event.type)) continue
    const key = traceIdentity(event)
    if (key && indexes.has(key)) {
      const rowIndex = indexes.get(key)!
      rows[rowIndex] = { ...rows[rowIndex], ...event }
    }
    else {
      if (key) indexes.set(key, rows.length)
      rows.push({ ...event, __traceIndex: rawIndex })
    }
  }
  return rows
})

const activityRows = computed<ActivityRow[]>(() => {
  const skillRows = new Map<TraceEvent, SkillActivityRow>()
  const skillsById = new Map<string, SkillActivityRow>()
  const skillsByName = new Map<string, SkillActivityRow[]>()

  for (const [index, event] of callEvents.value.entries()) {
    if (event.type !== 'skill_activation') continue
    const row: SkillActivityRow = {
      kind: 'skill',
      key: `skill-${eventKey(event)}`,
      index,
      event,
      children: [],
    }
    skillRows.set(event, row)
    const skillId = normalized(event.skill_id || event.activation_id)
    if (skillId) skillsById.set(skillId, row)
    const skillName = normalized(event.skill_name || event.name)
    if (skillName) skillsByName.set(skillName, [...(skillsByName.get(skillName) || []), row])
  }

  const rows: ActivityRow[] = []
  for (const [index, event] of callEvents.value.entries()) {
    if (event.type === 'skill_activation') {
      const row = skillRows.get(event)
      if (row) rows.push(row)
      continue
    }

    const parent = SKILL_CHILD_TYPES.has(event.type)
      ? linkedSkill(event, index, skillsById, skillsByName)
      : undefined
    if (parent) {
      parent.children.push(event)
      continue
    }

    rows.push({
      kind: 'event',
      key: `event-${eventKey(event)}`,
      index,
      event,
    })
  }
  return rows.sort((left, right) => left.index - right.index)
})

const summaryText = computed(() => {
  const running = [...callEvents.value].reverse().find((item) => item.status === 'running')
  if (running) return eventLabel(running)
  if (callEvents.value.length) {
    const counts = callEvents.value.reduce((result: Record<string, number>, item: any) => {
      const key = labels.value.types[item.type] || 'Agent'
      result[key] = (result[key] || 0) + 1
      return result
    }, {})
    return Object.entries(counts).map(([name, count]) => `${name} ${count}`).join(' · ')
  }
  return reasoning.value ? labels.value.reasoning : ''
})

function eventKey(event: any) {
  return event.call_id || event.id || event.event_id || `${event.type}-${event.__traceIndex ?? event.name ?? 'event'}`
}

function eventLabel(event: TraceEvent) {
  const prefix = labels.value.types[event.type] || 'Agent'
  return `${prefix} · ${eventName(event)}`
}

function eventDescription(event: TraceEvent) {
  const explicit = displayText(event.error || event.output || event.reason || event.summary || event.description)
  if (explicit) return explicit
  if (event.type === 'skill_activation') {
    return event.status === 'running' ? labels.value.activatingSkill : labels.value.activatedSkill
  }
  if (event.type === 'skill_resource') {
    return event.status === 'running' ? labels.value.readingResource : labels.value.resourceRead
  }
  if (event.type === 'sandbox_command') {
    const command = displayText(event.command)
    if (command) return command
    return event.status === 'running' ? labels.value.runningCommand : labels.value.commandCompleted
  }
  return event.status === 'running' ? labels.value.calling : labels.value.completedCall
}

function statusLabel(status: string) {
  return labels.value.statuses[status] || status || ''
}

function traceIdentity(event: TraceEvent) {
  const id = event.call_id || event.id || event.event_id
  return id ? `${event.type}:${id}` : ''
}

function linkedSkill(
  event: TraceEvent,
  eventIndex: number,
  skillsById: Map<string, SkillActivityRow>,
  skillsByName: Map<string, SkillActivityRow[]>,
) {
  const parentSkillId = normalized(event.parent_skill_id)
  if (parentSkillId) return skillsById.get(parentSkillId)

  const skillName = normalized(event.skill_name)
  if (!skillName) return undefined
  const candidates = skillsByName.get(skillName) || []
  const previous = [...candidates].reverse().find((row) => row.index <= eventIndex)
  return previous || (candidates.length === 1 ? candidates[0] : undefined)
}

function normalized(value: unknown) {
  return typeof value === 'string' ? value.trim() : value == null ? '' : String(value)
}

function eventName(event: TraceEvent) {
  if (event.type === 'skill_activation') {
    return normalized(event.skill_name || event.name) || labels.value.unnamed
  }
  if (event.type === 'skill_resource') {
    return normalized(event.name || event.resource_path || event.path || event.title) || labels.value.unnamed
  }
  return normalized(event.name || event.title || event.command) || labels.value.unnamed
}

function displayText(value: unknown) {
  if (typeof value === 'string') return value.trim()
  if (value == null) return ''
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function eventIcon(event: TraceEvent) {
  if (event.status === 'running') return Loading
  if (['skill_activation', 'skill_load', 'skill_call'].includes(event.type)) return MagicStick
  if (event.type === 'skill_resource' || event.type === 'context_read') return Document
  if (event.type === 'sandbox_command') return Monitor
  if (event.type === 'mcp_call') return Connection
  if (event.type === 'model_call') return Cpu
  return Tools
}
</script>

<style scoped>
.agent-activity {
  margin: 0 0 12px;
  border: 0;
  background: transparent;
}

.agent-activity summary {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) 18px;
  gap: 8px;
  align-items: center;
  min-height: 32px;
  padding: 4px 6px;
  border-radius: 5px;
  color: var(--text-muted);
  cursor: pointer;
  list-style: none;
  font-size: 11.5px;
  transition: background 0.16s ease, color 0.16s ease;
}

.agent-activity summary::-webkit-details-marker {
  display: none;
}

.agent-activity summary:hover,
.agent-activity[open] summary {
  background: color-mix(in srgb, var(--surface-hover) 54%, transparent);
  color: var(--text-main);
}

.agent-activity summary:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.summary-detail {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.activity-state {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  color: var(--text-strong);
  font-size: 11.5px;
  font-weight: 600;
}

.activity-state.active {
  color: var(--accent);
}

.chevron {
  transition: transform 0.18s ease;
}

.agent-activity[open] .chevron {
  transform: rotate(90deg);
}

.activity-body {
  display: grid;
  gap: 10px;
  margin: 6px 0 2px 10px;
  padding: 4px 0 4px 15px;
  border-left: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border));
}

.agent-plan,
.call-list {
  display: grid;
  gap: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.agent-plan li {
  display: grid;
  grid-template-columns: 18px minmax(0, 1fr);
  gap: 8px;
  align-items: start;
  padding: 3px 0;
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.55;
}

.agent-plan li > span {
  display: grid;
  place-items: center;
  width: 18px;
  height: 18px;
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--surface-0);
  color: var(--text-muted);
  font-size: 9px;
}

.agent-plan p,
.reasoning-block p {
  margin: 0;
  white-space: pre-wrap;
}

.reasoning-block {
  padding: 2px 0;
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.65;
}

.reasoning-block header {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-bottom: 4px;
  color: var(--text-strong);
}

.activity-item {
  min-width: 0;
  padding: 5px 0;
}

.activity-item + .activity-item {
  border-top: 1px solid var(--border-soft);
}

.event-row {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
  min-height: 40px;
}

.skill-activity {
  margin: 3px 0;
  padding: 6px 8px;
  border-left: 2px solid color-mix(in srgb, var(--el-color-success) 72%, var(--border));
  border-radius: 0 6px 6px 0;
  background: color-mix(in srgb, var(--el-color-success) 5%, transparent);
}

.skill-activity + .activity-item {
  border-top-color: transparent;
}

.skill-children {
  display: grid;
  margin: 4px 0 1px 31px;
  padding: 0 0 0 10px;
  border-left: 1px solid color-mix(in srgb, var(--el-color-success) 28%, var(--border));
  list-style: none;
}

.skill-child {
  min-width: 0;
  padding: 4px 0;
}

.skill-child + .skill-child {
  border-top: 1px solid var(--border-soft);
}

.child-event-row {
  min-height: 34px;
}

.call-icon {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border-radius: 5px;
  background: var(--accent-soft);
  color: var(--accent);
}

.call-icon.skill_activation,
.call-icon.skill_load,
.call-icon.skill_call {
  background: color-mix(in srgb, var(--el-color-success) 15%, transparent);
  color: var(--el-color-success);
}

.call-icon.skill_resource {
  background: color-mix(in srgb, var(--el-color-success) 9%, var(--surface-2));
  color: color-mix(in srgb, var(--el-color-success) 72%, var(--text-main));
}

.call-icon.mcp_call {
  background: color-mix(in srgb, var(--warning) 15%, transparent);
  color: var(--warning);
}

.call-icon.model_call,
.call-icon.context_read {
  background: color-mix(in srgb, var(--text-muted) 12%, transparent);
  color: var(--text-muted);
}

.event-copy {
  min-width: 0;
}

.event-copy strong,
.event-copy span {
  display: block;
}

.event-copy strong {
  overflow: hidden;
  color: var(--text-strong);
  font-size: 11.5px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.event-description {
  margin-top: 2px;
  display: -webkit-box;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.45;
  overflow-wrap: anywhere;
  white-space: pre-line;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.event-row em {
  color: var(--text-muted);
  font-size: 10.5px;
  font-style: normal;
  white-space: nowrap;
}

.event-row em.failed {
  color: var(--el-color-danger);
}

.event-row em.succeeded,
.event-row em.completed,
.event-row em.loaded {
  color: var(--el-color-success);
}

.spin {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .spin { animation: none; }
  .chevron { transition: none; }
}

@media (max-width: 1179px) {
  .agent-activity summary {
    min-height: 40px;
    font-size: 12px;
  }

  .activity-body {
    margin-left: 8px;
  }

  .reasoning-block,
  .agent-plan li,
  .event-copy strong {
    font-size: 12px;
  }
}

@media (max-width: 520px) {
  .event-row {
    grid-template-columns: 24px minmax(0, 1fr);
  }

  .event-row em {
    grid-column: 2;
    justify-self: start;
    margin-top: -3px;
  }

  .skill-children {
    margin-left: 12px;
  }
}
</style>
