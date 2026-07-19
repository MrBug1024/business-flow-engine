<template>
  <section class="mention-menu" :aria-label="title">
    <header class="mention-head">
      <span class="mention-title">
        <el-icon aria-hidden="true"><Files /></el-icon>
        {{ title }}
      </span>
      <span class="mention-count" role="status">{{ resultLabel }}</span>
    </header>

    <div :id="listId" class="mention-options" role="listbox" :aria-label="title">
      <button
        v-for="(file, index) in files"
        :id="optionId(index)"
        :key="file.path"
        class="mention-option"
        :class="{ active: index === activeIndex }"
        type="button"
        role="option"
        :aria-selected="index === activeIndex"
        :title="file.path"
        @mousemove="$emit('activate', index)"
        @mousedown.prevent
        @click="$emit('select', file)"
      >
        <span class="mention-icon" aria-hidden="true">
          <el-icon><component :is="fileIcon(file.icon)" /></el-icon>
        </span>
        <span class="mention-file-copy">
          <strong>{{ file.name }}</strong>
          <span>{{ directoryLabel(file.path) }}</span>
        </span>
        <span class="mention-add" aria-hidden="true">@</span>
      </button>

      <div v-if="!files.length" class="mention-empty">
        <el-icon aria-hidden="true"><Document /></el-icon>
        <span>{{ emptyLabel }}</span>
      </div>
    </div>

    <footer class="mention-hint">{{ hint }}</footer>
  </section>
</template>

<script setup lang="ts">
import { nextTick, watch } from 'vue'
import {
  Box,
  DataLine,
  Document,
  Files,
  Headset,
  Picture,
  Setting,
  Share,
  VideoCamera,
} from '@element-plus/icons-vue'

export type MentionFile = {
  name: string
  path: string
  icon?: string
}

const props = defineProps<{
  files: MentionFile[]
  activeIndex: number
  listId: string
  title: string
  resultLabel: string
  emptyLabel: string
  hint: string
}>()

defineEmits<{
  activate: [index: number]
  select: [file: MentionFile]
}>()

const iconMap: Record<string, any> = {
  audio: Headset,
  database: DataLine,
  file: Document,
  graph: Share,
  image: Picture,
  json: Document,
  markdown: Document,
  package: Box,
  settings: Setting,
  table: DataLine,
  video: VideoCamera,
}

function fileIcon(icon?: string) {
  return iconMap[icon || 'file'] || Document
}

function directoryLabel(path: string) {
  const parts = path.split('/')
  parts.pop()
  return parts.length ? parts.join('/') : '/'
}

function optionId(index: number) {
  return `workspace-mention-option-${index}`
}

watch(() => props.activeIndex, async (index) => {
  await nextTick()
  document.getElementById(optionId(index))?.scrollIntoView({ block: 'nearest' })
})
</script>

<style scoped>
.mention-menu {
  position: absolute;
  right: 0;
  bottom: calc(100% + 8px);
  left: 0;
  z-index: 40;
  display: grid;
  max-height: min(324px, calc(100vh - 180px));
  overflow: hidden;
  border: 1px solid var(--chat-divider);
  border-radius: 8px;
  background: var(--chat-header);
  color: var(--text-main);
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.28);
}

.mention-head,
.mention-hint {
  display: flex;
  align-items: center;
  min-height: 34px;
  padding: 0 10px;
  color: var(--text-muted);
  font-size: 11px;
}

.mention-head {
  justify-content: space-between;
  border-bottom: 1px solid var(--chat-divider);
}

.mention-title {
  display: flex;
  gap: 7px;
  align-items: center;
  color: var(--text-strong);
  font-weight: 600;
}

.mention-title .el-icon {
  color: var(--accent);
}

.mention-count {
  font-variant-numeric: tabular-nums;
}

.mention-options {
  min-height: 56px;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 4px;
}

.mention-option {
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr) 20px;
  gap: 8px;
  align-items: center;
  width: 100%;
  min-height: 48px;
  padding: 5px 7px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-main);
  text-align: left;
  cursor: pointer;
  transition: border-color 0.16s ease, background 0.16s ease;
}

.mention-option:hover,
.mention-option.active {
  border-color: color-mix(in srgb, var(--accent) 38%, transparent);
  background: var(--accent-soft);
}

.mention-option:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.mention-icon {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: 6px;
  background: var(--surface-3);
  color: var(--accent);
}

.mention-file-copy {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.mention-file-copy strong,
.mention-file-copy span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mention-file-copy strong {
  color: var(--text-strong);
  font-size: 12px;
  font-weight: 600;
}

.mention-file-copy span {
  color: var(--text-muted);
  font-size: 10px;
}

.mention-add {
  color: var(--text-muted);
  font-size: 14px;
  font-weight: 700;
  text-align: center;
}

.mention-option.active .mention-add {
  color: var(--accent);
}

.mention-empty {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: center;
  min-height: 76px;
  color: var(--text-muted);
  font-size: 12px;
}

.mention-hint {
  border-top: 1px solid var(--chat-divider);
}

@media (prefers-reduced-motion: reduce) {
  .mention-option {
    transition: none;
  }
}
</style>
