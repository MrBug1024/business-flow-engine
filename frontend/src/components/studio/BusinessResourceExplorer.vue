<template>
  <aside class="business-resource-explorer" :aria-label="text.title">
    <header class="explorer-titlebar">
      <span>{{ text.title }}</span>
      <div class="explorer-actions">
        <button :title="text.refresh" :aria-label="text.refresh" :disabled="busy" @click="$emit('refresh')">
          <el-icon><Refresh /></el-icon>
        </button>
        <button :title="text.newBusiness" :aria-label="text.newBusiness" :disabled="busy" @click="$emit('create-business')">
          <el-icon><Plus /></el-icon>
        </button>
      </div>
    </header>

    <div class="resource-tree" role="tree">
      <template v-for="row in rows" :key="row.key">
        <button
          class="resource-row"
          :class="{
            active: !row.target.root
              && row.target.businessId === currentBusinessId
              && row.target.node.path === activePath,
            current: row.target.root && row.target.businessId === currentBusinessId,
            root: row.target.root,
          }"
          :style="{ paddingLeft: `${6 + row.depth * 14}px` }"
          role="treeitem"
          :aria-level="row.depth + 1"
          :aria-expanded="row.target.node.kind === 'folder' ? isExpanded(row.target) : undefined"
          :title="row.target.node.path || row.target.businessName"
          @click="openRow(row.target)"
          @contextmenu.prevent="openContextMenu(row.target, $event)"
          @keydown.right.prevent="expandTarget(row.target)"
          @keydown.left.prevent="collapseTarget(row.target)"
        >
          <span
            class="resource-chevron"
            :class="{ expanded: isExpanded(row.target), hidden: row.target.node.kind !== 'folder' }"
            @click.stop="toggleTarget(row.target)"
          >
            <el-icon><ArrowRight /></el-icon>
          </span>
          <el-icon class="resource-icon"><component :is="iconFor(row.target)" /></el-icon>
          <span class="resource-name">{{ row.target.root ? row.target.businessName : row.target.node.name }}</span>
          <el-icon v-if="loadingBusinessIds.has(row.target.businessId) && row.target.root" class="resource-loading"><Loading /></el-icon>
        </button>
      </template>

      <button v-if="!businesses.length" class="empty-resource-tree" :disabled="busy" @click="$emit('create-business')">
        <el-icon><FolderAdd /></el-icon>
        <span>{{ text.empty }}</span>
      </button>
    </div>

    <input ref="importInput" class="hidden-input" type="file" multiple @change="completeImport" />

    <Teleport to="body">
      <div
        v-if="contextMenu"
        ref="contextMenuElement"
        class="studio-resource-menu"
        :style="{ left: `${contextMenu.x}px`, top: `${contextMenu.y}px` }"
        role="menu"
        :aria-label="contextMenu.target.node.name || contextMenu.target.businessName"
        @pointerdown.stop
      >
        <button role="menuitem" @click="runAction('open')">
          <el-icon><Document /></el-icon><span>{{ text.open }}</span>
        </button>
        <template v-if="contextMenu.target.node.kind === 'folder'">
          <div class="menu-separator" />
          <button role="menuitem" @click="runAction('new-file')">
            <el-icon><DocumentAdd /></el-icon><span>{{ text.newFile }}</span>
          </button>
          <button role="menuitem" @click="runAction('new-folder')">
            <el-icon><FolderAdd /></el-icon><span>{{ text.newFolder }}</span>
          </button>
          <button role="menuitem" @click="beginImport">
            <el-icon><Upload /></el-icon><span>{{ text.import }}</span>
          </button>
        </template>
        <button role="menuitem" @click="runAction('export')">
          <el-icon><Download /></el-icon><span>{{ text.export }}</span>
        </button>
        <div class="menu-separator" />
        <button role="menuitem" @click="runAction('rename')">
          <el-icon><EditPen /></el-icon><span>{{ text.rename }}</span>
        </button>
        <button class="danger" role="menuitem" @click="runAction('delete')">
          <el-icon><Delete /></el-icon><span>{{ text.delete }}</span>
        </button>
      </div>
    </Teleport>
  </aside>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  ArrowRight,
  Box,
  Cpu,
  DataLine,
  Delete,
  Document,
  DocumentAdd,
  Download,
  EditPen,
  Folder,
  FolderAdd,
  FolderOpened,
  Headset,
  Loading,
  MagicStick,
  Picture,
  Plus,
  Refresh,
  Setting,
  Share,
  Upload,
  VideoCamera,
} from '@element-plus/icons-vue'
import type {
  BusinessResourceAction,
  BusinessResourceTarget,
  BusinessSummary,
  Language,
  WorkspaceNode,
} from '@/types/studio'

type ResourceRow = { key: string; target: BusinessResourceTarget; depth: number }
type ContextMenu = { target: BusinessResourceTarget; x: number; y: number }

const props = defineProps<{
  businesses: BusinessSummary[]
  currentBusinessId: string
  trees: Record<string, WorkspaceNode>
  loadingBusinessIds: Set<string>
  activePath: string
  busy: boolean
  language: Language
}>()

const emit = defineEmits<{
  'create-business': []
  refresh: []
  'request-tree': [businessId: string]
  open: [target: BusinessResourceTarget]
  action: [action: BusinessResourceAction, target: BusinessResourceTarget]
  import: [files: File[], target: BusinessResourceTarget]
}>()

const expanded = ref<Set<string>>(new Set())
const contextMenu = ref<ContextMenu | null>(null)
const contextMenuElement = ref<HTMLElement | null>(null)
const importInput = ref<HTMLInputElement | null>(null)
const importTarget = ref<BusinessResourceTarget | null>(null)

const copy = {
  zh: {
    title: '业务资源管理器', refresh: '刷新', newBusiness: '新建业务场景', empty: '新建业务场景', open: '打开',
    newFile: '新建文件', newFolder: '新建文件夹', import: '导入文件...', export: '导出', rename: '重命名', delete: '删除',
  },
  en: {
    title: 'Business Explorer', refresh: 'Refresh', newBusiness: 'New business', empty: 'Create business workspace', open: 'Open',
    newFile: 'New File', newFolder: 'New Folder', import: 'Import Files...', export: 'Export', rename: 'Rename', delete: 'Delete',
  },
}

const text = computed(() => copy[props.language])
const rows = computed<ResourceRow[]>(() => props.businesses.flatMap((business) => {
  const root = targetForBusiness(business)
  const result: ResourceRow[] = [{ key: `business:${business.id}`, target: root, depth: 0 }]
  if (!isExpanded(root)) return result
  const tree = props.trees[business.id]
  for (const child of tree?.children || []) flattenNode(result, business, child, 1)
  return result
}))

watch(
  () => props.currentBusinessId,
  (businessId) => {
    if (!businessId) return
    const next = new Set(expanded.value)
    next.add(`business:${businessId}`)
    expanded.value = next
    emit('request-tree', businessId)
  },
  { immediate: true },
)

onMounted(() => {
  window.addEventListener('pointerdown', handleOutsidePointer)
  window.addEventListener('keydown', handleGlobalKeydown)
  window.addEventListener('resize', closeContextMenu)
  window.addEventListener('scroll', closeContextMenu, true)
})

onBeforeUnmount(() => {
  window.removeEventListener('pointerdown', handleOutsidePointer)
  window.removeEventListener('keydown', handleGlobalKeydown)
  window.removeEventListener('resize', closeContextMenu)
  window.removeEventListener('scroll', closeContextMenu, true)
})

function targetForBusiness(business: BusinessSummary): BusinessResourceTarget {
  return {
    businessId: business.id,
    businessName: business.name,
    root: true,
    node: { name: business.name, path: '', kind: 'folder', icon: 'folder', children: [] },
  }
}

function flattenNode(
  result: ResourceRow[],
  business: BusinessSummary,
  node: WorkspaceNode,
  depth: number,
) {
  const target: BusinessResourceTarget = {
    businessId: business.id,
    businessName: business.name,
    root: false,
    node,
  }
  result.push({ key: `${business.id}:${node.path}`, target, depth })
  if (node.kind !== 'folder' || !isExpanded(target)) return
  for (const child of node.children || []) flattenNode(result, business, child, depth + 1)
}

function expansionKey(target: BusinessResourceTarget) {
  return target.root ? `business:${target.businessId}` : `${target.businessId}:${target.node.path}`
}

function isExpanded(target: BusinessResourceTarget) {
  return expanded.value.has(expansionKey(target))
}

function toggleTarget(target: BusinessResourceTarget) {
  if (target.node.kind !== 'folder') return
  const key = expansionKey(target)
  const next = new Set(expanded.value)
  if (next.has(key)) next.delete(key)
  else {
    next.add(key)
    if (target.root) emit('request-tree', target.businessId)
  }
  expanded.value = next
}

function expandTarget(target: BusinessResourceTarget) {
  if (!isExpanded(target)) toggleTarget(target)
}

function collapseTarget(target: BusinessResourceTarget) {
  if (isExpanded(target)) toggleTarget(target)
}

function openRow(target: BusinessResourceTarget) {
  if (target.node.kind === 'folder') toggleTarget(target)
  emit('open', target)
}

function openContextMenu(target: BusinessResourceTarget, event: MouseEvent) {
  if (props.busy) return
  const width = 210
  const estimatedHeight = target.node.kind === 'folder' ? 280 : 152
  const margin = 8
  contextMenu.value = {
    target,
    x: Math.min(Math.max(event.clientX, margin), window.innerWidth - width - margin),
    y: Math.min(Math.max(event.clientY, margin), window.innerHeight - estimatedHeight - margin),
  }
}

function closeContextMenu() {
  contextMenu.value = null
}

function runAction(action: BusinessResourceAction) {
  const target = contextMenu.value?.target
  closeContextMenu()
  if (target) emit('action', action, target)
}

function beginImport() {
  importTarget.value = contextMenu.value?.target || null
  closeContextMenu()
  importInput.value?.click()
}

function completeImport(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  if (files.length && importTarget.value) emit('import', files, importTarget.value)
  input.value = ''
  importTarget.value = null
}

function handleOutsidePointer(event: PointerEvent) {
  const target = event.target as Node | null
  if (!target || !contextMenuElement.value?.contains(target)) closeContextMenu()
}

function handleGlobalKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') closeContextMenu()
}

function iconFor(target: BusinessResourceTarget) {
  const node = target.node
  const icon = node.icon || (node.kind === 'folder' ? 'folder' : 'file')
  const icons: Record<string, any> = {
    audio: Headset,
    brain: Cpu,
    database: DataLine,
    file: Document,
    folder: isExpanded(target) ? FolderOpened : Folder,
    graph: Share,
    image: Picture,
    json: Document,
    markdown: Document,
    package: Box,
    scenario: MagicStick,
    settings: Setting,
    table: DataLine,
    video: VideoCamera,
  }
  return icons[icon] || Document
}
</script>

<style scoped>
.business-resource-explorer {
  display: flex;
  min-width: 0;
  min-height: 0;
  flex-direction: column;
  border-right: 1px solid var(--border);
  background: var(--surface-1);
}

.explorer-titlebar {
  display: flex;
  min-height: 36px;
  align-items: center;
  justify-content: space-between;
  padding: 0 8px 0 14px;
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
  text-transform: uppercase;
}

.explorer-actions {
  display: flex;
  gap: 2px;
}

.explorer-actions button {
  display: grid;
  width: 28px;
  height: 28px;
  place-items: center;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.explorer-actions button:hover:not(:disabled),
.explorer-actions button:focus-visible {
  background: var(--surface-hover);
  color: var(--text-strong);
}

.resource-tree {
  min-height: 0;
  flex: 1;
  overflow: auto;
  padding: 2px 0 12px;
}

.resource-row {
  display: flex;
  width: 100%;
  min-width: 0;
  height: 24px;
  align-items: center;
  gap: 4px;
  padding-right: 8px;
  border: 0;
  border-radius: 0;
  background: transparent;
  color: var(--text-main);
  cursor: default;
  font-size: 12px;
  text-align: left;
}

.resource-row:hover,
.resource-row:focus-visible {
  background: var(--surface-hover);
  outline: none;
}

.resource-row.active {
  background: var(--accent-soft);
  color: var(--text-strong);
}

.resource-row.root {
  font-weight: 600;
}

.resource-row.root.current .resource-icon,
.resource-row.root.current .resource-name {
  color: var(--accent);
}

.resource-chevron {
  display: grid;
  width: 16px;
  height: 20px;
  flex: 0 0 16px;
  place-items: center;
  color: var(--text-muted);
}

.resource-chevron .el-icon {
  transition: transform 120ms ease;
}

.resource-chevron.expanded .el-icon {
  transform: rotate(90deg);
}

.resource-chevron.hidden {
  visibility: hidden;
}

.resource-icon {
  flex: 0 0 16px;
  color: var(--text-muted);
  font-size: 15px;
}

.resource-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.resource-loading {
  margin-left: auto;
  animation: resource-spin 900ms linear infinite;
}

.empty-resource-tree {
  display: flex;
  width: calc(100% - 16px);
  min-height: 36px;
  align-items: center;
  gap: 8px;
  margin: 8px;
  padding: 0 10px;
  border: 1px dashed var(--border);
  border-radius: 4px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.hidden-input {
  display: none;
}

@keyframes resource-spin {
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .resource-chevron .el-icon { transition: none; }
  .resource-loading { animation: none; }
}
</style>

<style>
.studio-resource-menu {
  position: fixed;
  z-index: 2400;
  width: 210px;
  padding: 4px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  background: var(--el-bg-color-overlay);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
}

.studio-resource-menu button {
  display: flex;
  width: 100%;
  height: 30px;
  align-items: center;
  gap: 9px;
  padding: 0 10px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--el-text-color-regular);
  cursor: pointer;
  font-size: 12px;
  text-align: left;
}

.studio-resource-menu button:hover,
.studio-resource-menu button:focus-visible {
  background: var(--el-fill-color-light);
  color: var(--el-text-color-primary);
  outline: none;
}

.studio-resource-menu button.danger {
  color: var(--el-color-danger);
}

.studio-resource-menu .menu-separator {
  height: 1px;
  margin: 4px 6px;
  background: var(--el-border-color-light);
}
</style>
