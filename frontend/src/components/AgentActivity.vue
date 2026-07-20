<template>
  <details v-if="visible" class="agent-activity" :open="active || (hasSemantic && !compact)">
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
      <section v-if="latestProgress?.objective" class="task-overview">
        <span>{{ labels.objective }}</span>
        <strong>{{ latestProgress.objective }}</strong>
        <p v-if="latestProgress.summary">{{ latestProgress.summary }}</p>
      </section>

      <ol v-if="workItemRows.length" class="work-items" :aria-label="labels.workItems">
        <li v-for="item in workItemRows" :key="item.id" :class="item.status">
          <span class="work-status">
            <el-icon :class="{ spin: item.status === 'running' }">
              <component :is="statusIcon(item.status)" />
            </el-icon>
          </span>
          <div class="work-copy">
            <header>
              <strong>{{ item.title }}</strong>
              <em>{{ statusLabel(item.status) }}</em>
            </header>
            <p v-if="item.why">{{ item.why }}</p>
            <p v-if="item.expected" class="expected">
              {{ labels.expected }}：{{ item.expected }}
            </p>
            <p v-if="item.result" class="result">{{ item.result }}</p>
            <p v-if="item.verification" class="verification">
              {{ labels.verification }}：{{ item.verification }}
            </p>
          </div>
        </li>
      </ol>

      <section v-else-if="active" class="planning-state" aria-live="polite">
        <el-icon class="spin"><Loading /></el-icon>
        <span>{{ labels.planning }}</span>
      </section>

      <section v-if="continuityNotice" class="continuity-notice">
        <el-icon><Refresh /></el-icon>
        <div>
          <strong>{{ continuityNotice.title }}</strong>
          <p>{{ continuityNotice.summary }}</p>
        </div>
      </section>

      <section v-if="fileActivities.length" class="file-activity-list" :aria-label="labels.fileActivities">
        <header>
          <el-icon><Document /></el-icon>
          <strong>{{ labels.fileActivities }}</strong>
        </header>
        <ul>
          <li v-for="item in fileActivities" :key="item.key">
            <el-icon :class="{ spin: item.status === 'running' }">
              <component :is="statusIcon(item.status)" />
            </el-icon>
            <span>{{ operationLabel(item.operation) }}</span>
            <code :title="item.path">{{ item.path }}</code>
            <em :class="item.status">{{ statusLabel(item.status) }}</em>
          </li>
        </ul>
      </section>

      <section v-if="artifacts.length" class="artifact-list" :aria-label="labels.artifacts">
        <header>
          <el-icon><Document /></el-icon>
          <strong>{{ labels.artifacts }}</strong>
        </header>
        <ul>
          <li v-for="artifact in artifacts" :key="artifact"><code>{{ artifact }}</code></li>
        </ul>
      </section>

      <details v-if="technicalGroups.length" class="technical-trace">
        <summary>
          <span>{{ labels.technicalDetails }}</span>
          <em>{{ labels.mergedCalls(technicalCallCount) }}</em>
        </summary>

        <ol class="call-list">
          <li
            v-for="group in technicalGroups"
            :key="group.key"
            class="activity-item"
            :class="group.event.type"
          >
            <div class="event-row">
              <span class="call-icon" :class="group.event.type">
                <el-icon :class="{ spin: group.status === 'running' }">
                  <component :is="eventIcon(group.event)" />
                </el-icon>
              </span>
              <div class="event-copy">
                <strong :title="group.label">{{ group.label }}</strong>
                <span class="event-description">{{ group.description }}</span>
              </div>
              <em :class="group.status">{{ statusLabel(group.status) }}</em>
            </div>
          </li>
        </ol>
      </details>
    </div>
  </details>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import {
  ArrowRight,
  CircleCheck,
  Connection,
  Document,
  Loading,
  MagicStick,
  Monitor,
  Opportunity,
  Refresh,
  Tools,
  Warning,
} from '@element-plus/icons-vue'

type TraceEvent = Record<string, any> & { type: string }
type WorkItem = {
  id: string
  title: string
  status: string
  why: string
  expected: string
  result: string
  verification: string
}
type TechnicalGroup = {
  key: string
  label: string
  description: string
  count: number
  status: string
  event: TraceEvent
}
type FileActivity = {
  key: string
  path: string
  operation: string
  status: string
}

const TRACE_TYPES = new Set([
  'tool_call',
  'skill_activation',
  'skill_resource',
  'sandbox_command',
  'skill_load',
  'skill_call',
  'mcp_call',
  'context_read',
  'file_operation',
])
const HIDDEN_TECHNICAL_FUNCTIONS = new Set(['report_task_progress', 'request_user_input'])

const props = withDefaults(defineProps<{
  plan?: string[]
  events?: any[]
  active?: boolean
  compact?: boolean
  language?: 'zh' | 'en'
}>(), {
  plan: () => [],
  events: () => [],
  active: false,
  compact: false,
  language: 'zh',
})

const copy = {
  zh: {
    working: '正在处理',
    completed: '工作过程',
    workItems: '任务工作项',
    objective: '任务目标',
    expected: '预期结果',
    artifacts: '成果文件',
    planning: '正在理解目标并制定执行计划',
    technicalDetails: '运行明细',
    fileActivities: '文件活动',
    mergedCalls: (count: number) => `已归并 ${count} 条`,
    verification: '验收',
    nextStep: '下一步',
    noSemantic: '等待 AI 给出任务进展',
    calling: '正在调用...',
    completedCall: '调用完成',
    activatingSkill: '正在加载完整 Skill 包...',
    activatedSkill: '完整 Skill 已激活',
    readingResource: '正在读取 Skill 资源...',
    resourceRead: 'Skill 资源已读取',
    runningCommand: '正在隔离环境中执行...',
    commandCompleted: '隔离命令已完成',
    compacted: '上下文已自动压缩，继续同一任务',
    unnamed: '未命名',
    types: {
      tool_call: 'Tool',
      skill_activation: 'Skill',
      skill_resource: '资源',
      sandbox_command: 'Sandbox',
      skill_load: 'Skill',
      skill_call: 'Skill',
      mcp_call: 'MCP',
      context_read: 'Context',
      file_operation: '文件',
    } as Record<string, string>,
    statuses: {
      planned: '已计划',
      pending: '等待中',
      running: '进行中',
      succeeded: '完成',
      completed: '完成',
      loaded: '已加载',
      failed: '失败',
      cancelled: '已取消',
      blocked: '待确认',
      streaming: '生成中',
      continuing: '继续中',
    } as Record<string, string>,
    operations: {
      list: '查看目录', read: '读取', search: '搜索', create: '创建', edit: '编辑',
      create_directory: '新建目录', move: '移动', delete: '删除', manage: '管理',
    } as Record<string, string>,
  },
  en: {
    working: 'Working',
    completed: 'Process',
    workItems: 'Task work items',
    objective: 'Objective',
    expected: 'Expected',
    artifacts: 'Deliverables',
    planning: 'Understanding the objective and preparing a plan',
    technicalDetails: 'Run details',
    fileActivities: 'File activity',
    mergedCalls: (count: number) => `${count} entries merged`,
    verification: 'Verification',
    nextStep: 'Next',
    noSemantic: 'Waiting for task progress',
    calling: 'Calling...',
    completedCall: 'Completed',
    activatingSkill: 'Loading the complete Skill package...',
    activatedSkill: 'Complete Skill activated',
    readingResource: 'Reading a Skill resource...',
    resourceRead: 'Skill resource loaded',
    runningCommand: 'Running inside the isolated sandbox...',
    commandCompleted: 'Sandbox command completed',
    compacted: 'Context compacted; continuing the same task',
    unnamed: 'Unnamed',
    types: {
      tool_call: 'Tool',
      skill_activation: 'Skill',
      skill_resource: 'Resource',
      sandbox_command: 'Sandbox',
      skill_load: 'Skill',
      skill_call: 'Skill',
      mcp_call: 'MCP',
      context_read: 'Context',
      file_operation: 'File',
    } as Record<string, string>,
    statuses: {
      planned: 'Planned',
      pending: 'Pending',
      running: 'Running',
      succeeded: 'Done',
      completed: 'Done',
      loaded: 'Loaded',
      failed: 'Failed',
      cancelled: 'Cancelled',
      blocked: 'Input needed',
      streaming: 'Streaming',
      continuing: 'Continuing',
    } as Record<string, string>,
    operations: {
      list: 'List', read: 'Read', search: 'Search', create: 'Create', edit: 'Edit',
      create_directory: 'New folder', move: 'Move', delete: 'Delete', manage: 'Manage',
    } as Record<string, string>,
  },
}

const labels = computed(() => copy[props.language])

const visible = computed(() => Boolean(
  props.active
  || props.plan.length
  || progressEvents.value.length
  || technicalGroups.value.length
  || props.events.some((item) => item.type === 'context_compaction' || item.type === 'task_handoff'),
))

const progressEvents = computed<TraceEvent[]>(() => props.events
  .filter((item) => item.type === 'agent_progress'))

const hasSemantic = computed(() => progressEvents.value.length > 0 || props.plan.length > 0)

const latestProgress = computed<TraceEvent | undefined>(() => [...progressEvents.value]
  .reverse()
  .find((item) => item.type === 'agent_progress'))

const workItemRows = computed<WorkItem[]>(() => {
  const rawItems = latestProgress.value?.work_items
  if (Array.isArray(rawItems) && rawItems.length) {
    return rawItems
      .map((item: any, index: number) => ({
        id: normalized(item.id) || `work-${index + 1}`,
        title: normalized(item.title) || normalized(item.name) || labels.value.unnamed,
        status: normalized(item.status) || statusFromProgress(latestProgress.value),
        why: normalized(item.why || item.expected),
        expected: normalized(item.expected),
        result: normalized(item.result),
        verification: normalized(item.verification),
      }))
      .filter((item: WorkItem) => item.title)
  }
  return props.plan.map((title, index) => ({
    id: `plan-${index + 1}`,
    title,
    status: props.active && index === 0 ? 'running' : props.active ? 'pending' : 'completed',
    why: '',
    expected: '',
    result: '',
    verification: '',
  }))
})

const continuityNotice = computed(() => {
  const event = [...props.events]
    .reverse()
    .find((item) => item.type === 'context_compaction' || item.type === 'task_handoff')
  if (!event) return null
  return {
    title: normalized(event.title) || labels.value.compacted,
    summary: normalized(event.summary) || labels.value.compacted,
  }
})

const artifacts = computed<string[]>(() => {
  const values = latestProgress.value?.artifacts
  return Array.isArray(values)
    ? [...new Set(values.map((item: unknown) => normalized(item)).filter(Boolean))]
    : []
})

const callEvents = computed<TraceEvent[]>(() => {
  const rows: TraceEvent[] = []
  const indexes = new Map<string, number>()
  for (const [rawIndex, event] of props.events.entries()) {
    if (!TRACE_TYPES.has(event.type)) continue
    if (HIDDEN_TECHNICAL_FUNCTIONS.has(normalized(event.function_name || event.name))) continue
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

const technicalGroups = computed<TechnicalGroup[]>(() => {
  const groups = new Map<string, TechnicalGroup & { events: TraceEvent[] }>()
  for (const event of callEvents.value.filter((item) => item.type !== 'file_operation')) {
    const key = technicalGroupKey(event)
    const current = groups.get(key)
    if (current) {
      current.count += 1
      current.events.push(event)
      current.event = event
      continue
    }
    groups.set(key, {
      key,
      label: technicalGroupLabel(event),
      description: '',
      count: 1,
      status: normalized(event.status) || 'completed',
      event,
      events: [event],
    })
  }
  return [...groups.values()].map((group) => {
    const failed = [...group.events].reverse().find((event) => event.status === 'failed')
    const running = [...group.events].reverse().find((event) => event.status === 'running')
    const event = running || failed || group.event
    return {
      key: group.key,
      label: group.label,
      description: technicalGroupDescription(event, group.count),
      count: group.count,
      status: running ? 'running' : failed ? 'failed' : 'succeeded',
      event,
    }
  })
})

const fileActivities = computed<FileActivity[]>(() => {
  const rows: FileActivity[] = []
  const seen = new Set<string>()
  for (const event of [...callEvents.value].reverse()) {
    if (event.type !== 'file_operation') continue
    const path = displayWorkspacePath(event.path || event.input?.file_path || event.input?.path)
    if (!path || seen.has(path)) continue
    seen.add(path)
    rows.push({
      key: `${normalized(event.call_id || event.id) || rows.length}:${path}`,
      path,
      operation: normalized(event.operation) || 'manage',
      status: normalized(event.status) || 'completed',
    })
    if (rows.length >= 5) break
  }
  return rows
})

const technicalCallCount = computed(() => technicalGroups.value
  .reduce((total, group) => total + group.count, 0))

const summaryText = computed(() => {
  const latest = latestProgress.value
  const semantic = normalized(latest?.summary || latest?.title || latest?.next_step || latest?.result)
  if (semantic) return semantic
  const runningWork = workItemRows.value.find((item) => item.status === 'running')
  if (runningWork) return runningWork.title
  if (props.plan.length) return props.plan[0]
  return props.active ? labels.value.planning : labels.value.noSemantic
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
    const command = displayText(event.command || event.input?.command)
    if (command) return command
    return event.status === 'running' ? labels.value.runningCommand : labels.value.commandCompleted
  }
  if (event.type === 'task_handoff') return labels.value.compacted
  return event.status === 'running' ? labels.value.calling : labels.value.completedCall
}

function statusLabel(status: string) {
  return labels.value.statuses[status] || status || ''
}

function operationLabel(operation: string) {
  return labels.value.operations[operation] || operation
}

function statusIcon(status: string) {
  if (status === 'running') return Loading
  if (status === 'blocked' || status === 'failed') return Warning
  if (status === 'completed' || status === 'succeeded' || status === 'loaded') return CircleCheck
  return Opportunity
}

function statusFromProgress(event: TraceEvent | undefined) {
  const status = normalized(event?.status)
  if (status) return status
  if (event?.action === 'complete') return 'completed'
  if (event?.action === 'block') return 'blocked'
  if (event?.action === 'plan') return 'planned'
  return 'running'
}

function traceIdentity(event: TraceEvent) {
  const id = event.call_id || event.id || event.event_id
  return id ? `${event.type}:${id}` : ''
}

function technicalGroupKey(event: TraceEvent) {
  if (event.type === 'skill_activation') {
    return `skill:${normalized(event.skill_name || event.name) || 'unknown'}`
  }
  if (event.type === 'skill_resource' || event.type === 'sandbox_command') {
    return `${event.type}:${normalized(event.skill_name) || 'workspace'}`
  }
  return `${event.type}:${normalized(event.function_name || event.name) || 'unknown'}`
}

function technicalGroupLabel(event: TraceEvent) {
  const type = labels.value.types[event.type] || 'Agent'
  const name = event.type === 'sandbox_command'
    ? normalized(event.skill_name) || labels.value.unnamed
    : eventName(event)
  return `${type} · ${name}`
}

function technicalGroupDescription(event: TraceEvent, count: number) {
  const detail = eventDescription(event)
  return count > 1 ? `${count} × · ${detail}` : detail
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

function displayWorkspacePath(value: unknown) {
  const path = normalized(value).replace(/\\/g, '/')
  return path.replace(/^\/workspace\/?/, '')
}

function eventIcon(event: TraceEvent) {
  if (event.status === 'running') return Loading
  if (['skill_activation', 'skill_load', 'skill_call'].includes(event.type)) return MagicStick
  if (event.type === 'skill_resource' || event.type === 'context_read') return Document
  if (event.type === 'file_operation') return Document
  if (event.type === 'sandbox_command') return Monitor
  if (event.type === 'mcp_call') return Connection
  if (event.type === 'task_handoff') return Refresh
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
  min-height: 34px;
  padding: 4px 6px;
  border-radius: 5px;
  color: var(--text-muted);
  cursor: pointer;
  list-style: none;
  font-size: 11.5px;
  transition: background 0.16s ease, color 0.16s ease;
}

.agent-activity summary::-webkit-details-marker,
.technical-trace summary::-webkit-details-marker {
  display: none;
}

.agent-activity summary:hover,
.agent-activity[open] > summary {
  background: color-mix(in srgb, var(--surface-hover) 54%, transparent);
  color: var(--text-main);
}

.agent-activity summary:focus-visible,
.technical-trace summary:focus-visible {
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

.agent-activity[open] > summary .chevron {
  transform: rotate(90deg);
}

.activity-body {
  display: grid;
  gap: 10px;
  margin: 6px 0 2px 10px;
  padding: 4px 0 4px 15px;
  border-left: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border));
}

.task-overview {
  display: grid;
  gap: 3px;
  padding: 2px 0 7px;
}

.task-overview > span {
  color: var(--text-muted);
  font-size: 10.5px;
  font-weight: 600;
}

.task-overview > strong {
  color: var(--text-strong);
  font-size: 12.5px;
  line-height: 1.5;
}

.task-overview > p,
.continuity-notice p {
  margin: 0;
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.planning-state,
.continuity-notice {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  min-height: 32px;
  color: var(--text-muted);
  font-size: 11.5px;
}

.planning-state {
  align-items: center;
}

.continuity-notice > .el-icon {
  flex: 0 0 auto;
  margin-top: 2px;
  color: var(--accent);
}

.continuity-notice strong {
  display: block;
  margin-bottom: 2px;
  color: var(--text-main);
  font-size: 11.5px;
}

.file-activity-list {
  min-width: 0;
}

.file-activity-list header,
.file-activity-list li {
  display: grid;
  align-items: center;
}

.file-activity-list header {
  grid-template-columns: 16px 1fr;
  gap: 6px;
  color: var(--text-main);
  font-size: 11.5px;
}

.file-activity-list ul {
  display: grid;
  gap: 3px;
  margin: 5px 0 0;
  padding: 0;
  list-style: none;
}

.file-activity-list li {
  grid-template-columns: 16px auto minmax(0, 1fr) auto;
  gap: 6px;
  min-height: 26px;
  color: var(--text-muted);
  font-size: 10.5px;
}

.file-activity-list code {
  min-width: 0;
  overflow: hidden;
  color: var(--text-main);
  font-family: var(--font-mono);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-activity-list em {
  font-style: normal;
}

.file-activity-list em.failed {
  color: var(--el-color-danger);
}

.file-activity-list em.running {
  color: var(--accent);
}

.artifact-list {
  min-width: 0;
  padding-top: 2px;
}

.artifact-list header {
  display: flex;
  gap: 6px;
  align-items: center;
  color: var(--text-main);
  font-size: 11.5px;
}

.artifact-list ul {
  display: grid;
  gap: 3px;
  margin: 5px 0 0 20px;
  padding: 0;
  list-style: none;
}

.artifact-list code {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 10.5px;
  overflow-wrap: anywhere;
  white-space: normal;
}

.work-items,
.call-list {
  display: grid;
  gap: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.work-items li {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr);
  gap: 8px;
  min-width: 0;
  padding: 6px 0;
}

.work-items li + li {
  border-top: 1px solid var(--border-soft);
}

.work-status {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--surface-0);
  color: var(--text-muted);
}

.work-items li.running .work-status {
  border-color: color-mix(in srgb, var(--accent) 72%, var(--border));
  background: var(--accent-soft);
  color: var(--accent);
}

.work-items li.completed .work-status,
.work-items li.succeeded .work-status {
  border-color: color-mix(in srgb, var(--el-color-success) 72%, var(--border));
  background: color-mix(in srgb, var(--el-color-success) 12%, transparent);
  color: var(--el-color-success);
}

.work-items li.blocked .work-status,
.work-items li.failed .work-status {
  border-color: color-mix(in srgb, var(--warning) 72%, var(--border));
  background: color-mix(in srgb, var(--warning) 12%, transparent);
  color: var(--warning);
}

.work-copy {
  min-width: 0;
}

.work-copy header,
.progress-notes header {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
  align-items: center;
}

.work-copy strong,
.progress-notes header span {
  overflow: hidden;
  color: var(--text-strong);
  font-size: 12px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.work-copy em,
.progress-notes em {
  color: var(--text-muted);
  font-size: 10.5px;
  font-style: normal;
  white-space: nowrap;
}

.work-copy p,
.progress-notes p,
.reasoning-block p {
  margin: 3px 0 0;
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.55;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.work-copy .result,
.progress-notes .result {
  color: var(--text-main);
}

.expected,
.verification,
.next-step {
  color: color-mix(in srgb, var(--accent) 70%, var(--text-muted));
}

.progress-notes {
  display: grid;
  gap: 6px;
}

.progress-notes article {
  padding: 7px 9px;
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  background: color-mix(in srgb, var(--surface-1) 76%, transparent);
}

.progress-notes article.blocked,
.progress-notes article.failed {
  border-color: color-mix(in srgb, var(--warning) 42%, var(--border-soft));
}

.reasoning-block {
  padding: 2px 0;
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.65;
}

.reasoning-block.compact {
  padding: 4px 0 8px;
}

.reasoning-block header {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-bottom: 4px;
  color: var(--text-strong);
}

.technical-trace {
  min-width: 0;
  border: 0;
}

.technical-trace summary {
  display: inline-flex;
  gap: 7px;
  align-items: center;
  min-height: 28px;
  padding: 3px 7px;
  border: 1px solid var(--border-soft);
  border-radius: 5px;
  background: color-mix(in srgb, var(--surface-1) 62%, transparent);
  color: var(--text-muted);
  cursor: pointer;
  font-size: 11px;
  list-style: none;
}

.technical-trace summary em {
  min-width: 16px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--surface-2);
  color: var(--text-muted);
  font-size: 10px;
  font-style: normal;
  text-align: center;
}

.technical-trace[open] summary {
  color: var(--text-main);
}

.technical-trace[open] .call-list {
  margin-top: 6px;
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
  min-height: 38px;
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

.event-row em.failed,
.event-row em.blocked {
  color: var(--warning);
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
  .work-copy strong,
  .work-items li,
  .event-copy strong {
    font-size: 12px;
  }
}

@media (max-width: 520px) {
  .event-row,
  .work-copy header,
  .progress-notes header {
    grid-template-columns: minmax(0, 1fr);
  }

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
