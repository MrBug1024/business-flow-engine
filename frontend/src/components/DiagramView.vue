<template>
  <div class="diagram">
    <div class="diagram-toolbar">
      <el-radio-group v-model="mode" size="default">
        <el-radio-button value="relations"><el-icon><Connection /></el-icon>关联 ER</el-radio-button>
        <el-radio-button value="flow"><el-icon><Share /></el-icon>业务流程</el-radio-button>
      </el-radio-group>
    </div>

    <div
      ref="viewport"
      class="diagram-canvas"
      :class="{ empty: !hasContent, grabbing }"
      @wheel="onWheel"
      @pointerdown="onDown"
      @dblclick="fit"
    >
      <div v-if="!hasContent" class="ph">
        <el-icon :size="34"><Share /></el-icon>
        <p>{{ mode === 'relations' ? '尚未推导关联关系' : '尚未推导业务流程' }}</p>
        <span class="ph-sub">在右侧对话让 AI「{{ mode === 'relations' ? '推导关联关系' : '推导业务流程' }}」。</span>
      </div>

      <div
        v-else
        ref="layer"
        class="pz-layer"
        :style="{ transform: `translate(${tx}px, ${ty}px) scale(${scale})`, transition: animate ? 'transform .18s var(--ease)' : 'none' }"
        v-html="svg"
      />

      <div v-if="hasContent" class="zoom-bar">
        <button title="缩小" @click="zoomBy(1 / 1.2)"><el-icon><Minus /></el-icon></button>
        <span class="zoom-val" title="重置为 100%" @click="setScale(1)">{{ Math.round(scale * 100) }}%</span>
        <button title="放大" @click="zoomBy(1.2)"><el-icon><Plus /></el-icon></button>
        <span class="zoom-div" />
        <button title="适应页面" @click="fit"><el-icon><FullScreen /></el-icon></button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, nextTick } from 'vue'
import mermaid from 'mermaid'
import { Connection, Share, Plus, Minus, FullScreen } from '@element-plus/icons-vue'
import type { Scenario } from '@/api/types'
import { useThemeStore } from '@/stores/theme'

const props = defineProps<{ scenario: Scenario | null }>()
const theme = useThemeStore()
const mode = ref<'relations' | 'flow'>('relations')
const svg = ref('')

const viewport = ref<HTMLElement>()
const layer = ref<HTMLElement>()

/* pan / zoom state */
const scale = ref(1)
const tx = ref(0)
const ty = ref(0)
const animate = ref(false)
const grabbing = ref(false)
let natW = 0
let natH = 0
const MIN = 0.15
const MAX = 5

function initMermaid() {
  mermaid.initialize({
    startOnLoad: false,
    theme: theme.theme === 'dark' ? 'dark' : 'default',
    securityLevel: 'loose',
    themeVariables: { fontFamily: 'Inter, sans-serif' },
  })
}

const relations = computed(() => props.scenario?.relations?.relations || [])
const flowMermaid = computed(() => props.scenario?.flow?.mermaid || '')

const hasContent = computed(() =>
  mode.value === 'relations' ? relations.value.length > 0 : !!flowMermaid.value.trim(),
)

function buildRelationsMermaid(): string {
  const lines = ['graph LR']
  const seen = new Set<string>()
  const id = (t: string) => 't_' + t.replace(/[^0-9A-Za-z]/g, '_')
  relations.value.forEach((r: any) => {
    ;[r.from_table, r.to_table].forEach((t: string) => {
      if (t && !seen.has(t)) { seen.add(t); lines.push(`${id(t)}["${t}"]`) }
    })
    const fc = (r.from_columns && r.from_columns.join('+')) || r.from_column || ''
    const tc = (r.to_columns && r.to_columns.join('+')) || r.to_column || ''
    lines.push(`${id(r.from_table)} -->|"${fc} = ${tc}"| ${id(r.to_table)}`)
  })
  return lines.join('\n')
}

async function renderDiagram() {
  if (!hasContent.value) { svg.value = ''; return }
  initMermaid()
  const code = mode.value === 'relations' ? buildRelationsMermaid() : flowMermaid.value
  try {
    const { svg: out } = await mermaid.render('mmd_' + Date.now(), code)
    svg.value = out
    await nextTick()
    measure()
    fit()
  } catch (e) {
    svg.value = `<pre class="muted">图渲染失败：${(e as any)?.message || e}</pre>`
  }
}

/** 读取 SVG 自然尺寸，并让它以原始大小渲染（缩放交给外层 transform）。 */
function measure() {
  const el = layer.value?.querySelector('svg') as SVGSVGElement | null
  if (!el) { natW = 0; natH = 0; return }
  const vb = el.viewBox?.baseVal
  let w = vb?.width || 0
  let h = vb?.height || 0
  if (!w || !h) {
    try { const b = el.getBBox(); w = w || b.width; h = h || b.height } catch { /* ignore */ }
  }
  natW = w || el.clientWidth || 600
  natH = h || el.clientHeight || 400
  el.style.maxWidth = 'none'
  el.style.width = natW + 'px'
  el.style.height = natH + 'px'
}

/** 适应页面并居中。 */
function fit() {
  const vp = viewport.value
  if (!vp || !natW || !natH) return
  const pad = 48
  const vw = vp.clientWidth
  const vh = vp.clientHeight
  const s = Math.min((vw - pad) / natW, (vh - pad) / natH)
  const next = clamp(s, MIN, 1.5)
  animate.value = true
  scale.value = next
  tx.value = (vw - natW * next) / 2
  ty.value = (vh - natH * next) / 2
}

function clamp(v: number, lo: number, hi: number) { return Math.min(hi, Math.max(lo, v)) }

/** 以视口中心为锚点缩放。 */
function zoomBy(factor: number) {
  const vp = viewport.value
  if (!vp) return
  zoomAt(vp.clientWidth / 2, vp.clientHeight / 2, factor)
}
function setScale(target: number) {
  const vp = viewport.value
  if (!vp) return
  zoomAt(vp.clientWidth / 2, vp.clientHeight / 2, target / scale.value)
}
/** 以 (cx,cy)（相对视口）为锚点按 factor 缩放，锚点下的内容保持不动。 */
function zoomAt(cx: number, cy: number, factor: number) {
  const next = clamp(scale.value * factor, MIN, MAX)
  const k = next / scale.value
  tx.value = cx - (cx - tx.value) * k
  ty.value = cy - (cy - ty.value) * k
  scale.value = next
}

function onWheel(e: WheelEvent) {
  if (!hasContent.value) return
  e.preventDefault()
  const rect = viewport.value!.getBoundingClientRect()
  const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12
  animate.value = false
  zoomAt(e.clientX - rect.left, e.clientY - rect.top, factor)
}

/* 拖拽平移 */
let startX = 0, startY = 0, startTx = 0, startTy = 0
function onDown(e: PointerEvent) {
  if (!hasContent.value || e.button !== 0) return
  const target = e.target as HTMLElement
  if (target.closest('.zoom-bar')) return
  grabbing.value = true
  animate.value = false
  startX = e.clientX; startY = e.clientY
  startTx = tx.value; startTy = ty.value
  ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}
function onMove(e: PointerEvent) {
  tx.value = startTx + (e.clientX - startX)
  ty.value = startTy + (e.clientY - startY)
}
function onUp() {
  grabbing.value = false
  window.removeEventListener('pointermove', onMove)
  window.removeEventListener('pointerup', onUp)
}

watch([mode, () => props.scenario, () => theme.theme], renderDiagram, { deep: true })
onMounted(renderDiagram)
</script>

<style scoped lang="scss">
.diagram { height: 100%; display: flex; flex-direction: column; background: var(--bg-app); }
.diagram-toolbar { padding: 14px 20px; border-bottom: 1px solid var(--border); flex-shrink: 0; background: var(--surface); }
.diagram-toolbar :deep(.el-radio-button__inner) { display: inline-flex; align-items: center; gap: 6px; }

.diagram-canvas {
  position: relative; flex: 1; overflow: hidden;
  touch-action: none; cursor: grab; user-select: none;
  background-image:
    radial-gradient(circle at 1px 1px, color-mix(in srgb, var(--text-3) 22%, transparent) 1px, transparent 0);
  background-size: 22px 22px;
}
.diagram-canvas.grabbing { cursor: grabbing; }
.diagram-canvas.empty { cursor: default; background-image: none; display: flex; }

.pz-layer { position: absolute; top: 0; left: 0; transform-origin: 0 0; will-change: transform; }
.pz-layer :deep(svg) { display: block; }

.ph { margin: auto; display: flex; flex-direction: column; align-items: center; gap: 8px; color: var(--text-3); text-align: center; }
.ph p { margin: 4px 0 0; font-size: var(--text-md); font-weight: 600; color: var(--text-2); }
.ph-sub { font-size: var(--text-base); }

/* Zoom control ------------------------------------------------------------ */
.zoom-bar {
  position: absolute; right: 16px; bottom: 16px; z-index: 4;
  display: flex; align-items: center; gap: 2px;
  padding: 4px; border-radius: var(--r-full);
  background: var(--surface); border: 1px solid var(--border); box-shadow: var(--shadow);
}
.zoom-bar button {
  display: inline-flex; align-items: center; justify-content: center;
  width: 30px; height: 30px; border: none; background: transparent; color: var(--text-2);
  border-radius: var(--r-full); cursor: pointer; transition: all var(--dur) var(--ease);
}
.zoom-bar button:hover { background: var(--brand-soft); color: var(--brand); }
.zoom-val {
  min-width: 46px; text-align: center; font-size: var(--text-xs); font-weight: 600;
  color: var(--text-2); font-variant-numeric: tabular-nums; cursor: pointer; user-select: none;
}
.zoom-val:hover { color: var(--brand); }
.zoom-div { width: 1px; height: 18px; background: var(--border); margin: 0 3px; }
</style>
