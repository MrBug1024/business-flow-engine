<template>
  <span class="brand-mark" :style="{ width: size + 'px', height: size + 'px', borderRadius: radius + 'px' }">
    <svg viewBox="0 0 24 24" fill="none" :width="size * 0.62" :height="size * 0.62" aria-hidden="true">
      <!-- nodes -->
      <circle cx="6" cy="6" r="2.4" fill="#fff" />
      <circle cx="18" cy="6" r="2.4" fill="#fff" fill-opacity="0.85" />
      <circle cx="12" cy="18" r="2.4" fill="#fff" />
      <!-- reverse-flow links -->
      <path d="M8.1 7.2 L15.9 7.2" stroke="#fff" stroke-width="1.7" stroke-linecap="round" stroke-opacity="0.9" />
      <path d="M6.6 8.2 C7.4 13, 9.6 15.4, 11 16.2" stroke="#fff" stroke-width="1.7" stroke-linecap="round" />
      <path d="M17.4 8.2 C16.6 13, 14.4 15.4, 13 16.2" stroke="#fff" stroke-width="1.7" stroke-linecap="round" stroke-opacity="0.85" />
    </svg>
  </span>
</template>

<script setup lang="ts">
const props = withDefaults(defineProps<{ size?: number }>(), { size: 30 })
const radius = Math.round(props.size * 0.28)
</script>

<style scoped lang="scss">
.brand-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  background: linear-gradient(140deg, #8b7bff 0%, #6d5efc 55%, #5a49f0 100%);
  box-shadow: 0 4px 14px rgba(109, 94, 252, 0.4), inset 0 1px 0 rgba(255, 255, 255, 0.25);
}
</style>
