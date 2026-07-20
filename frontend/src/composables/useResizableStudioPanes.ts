import { computed, onBeforeUnmount, ref, type Ref } from 'vue'

export type PaneKind = 'explorer' | 'assistant'

export function useResizableStudioPanes(settingsFocus: Ref<boolean>) {
  const explorerWidth = ref(clamp(Number(localStorage.getItem('studio.explorerWidth')) || 286, 220, 420))
  const assistantWidth = ref(clamp(Number(localStorage.getItem('studio.assistantWidth')) || 500, 360, 720))
  let drag: { kind: PaneKind; startX: number; startWidth: number } | null = null

  const studioLayoutStyle = computed(() => ({
    '--explorer-width': `${explorerWidth.value}px`,
    '--assistant-width': `${assistantWidth.value}px`,
  }))

  function paneMaximum(kind: PaneKind) {
    const editorMinimum = 460
    const activityWidth = 52
    if (kind === 'explorer') {
      return Math.min(420, window.innerWidth - activityWidth - assistantWidth.value - editorMinimum)
    }
    return Math.min(720, window.innerWidth - activityWidth - explorerWidth.value - editorMinimum)
  }

  function setPaneWidth(kind: PaneKind, value: number) {
    if (kind === 'explorer') explorerWidth.value = clamp(value, 220, paneMaximum(kind))
    else assistantWidth.value = clamp(value, 360, paneMaximum(kind))
  }

  function startPaneResize(kind: PaneKind, event: PointerEvent) {
    if (window.innerWidth < 1180 || settingsFocus.value) return
    event.preventDefault()
    drag = {
      kind,
      startX: event.clientX,
      startWidth: kind === 'explorer' ? explorerWidth.value : assistantWidth.value,
    }
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    window.addEventListener('pointermove', handlePanePointerMove)
    window.addEventListener('pointerup', stopPaneResize, { once: true })
    window.addEventListener('pointercancel', stopPaneResize, { once: true })
  }

  function handlePanePointerMove(event: PointerEvent) {
    if (!drag) return
    const delta = event.clientX - drag.startX
    setPaneWidth(
      drag.kind,
      drag.startWidth + (drag.kind === 'explorer' ? delta : -delta),
    )
  }

  function stopPaneResize() {
    if (!drag) return
    localStorage.setItem('studio.explorerWidth', String(Math.round(explorerWidth.value)))
    localStorage.setItem('studio.assistantWidth', String(Math.round(assistantWidth.value)))
    drag = null
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    window.removeEventListener('pointermove', handlePanePointerMove)
    window.removeEventListener('pointerup', stopPaneResize)
    window.removeEventListener('pointercancel', stopPaneResize)
  }

  function handlePaneResizeKey(kind: PaneKind, event: KeyboardEvent) {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return
    event.preventDefault()
    const direction = event.key === 'ArrowRight' ? 1 : -1
    const widthDirection = kind === 'assistant' ? -direction : direction
    const currentWidth = kind === 'explorer' ? explorerWidth.value : assistantWidth.value
    setPaneWidth(kind, currentWidth + widthDirection * (event.shiftKey ? 40 : 10))
    localStorage.setItem(
      `studio.${kind}Width`,
      String(Math.round(kind === 'explorer' ? explorerWidth.value : assistantWidth.value)),
    )
  }

  onBeforeUnmount(stopPaneResize)

  return {
    explorerWidth,
    assistantWidth,
    studioLayoutStyle,
    startPaneResize,
    stopPaneResize,
    handlePaneResizeKey,
  }
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum))
}
