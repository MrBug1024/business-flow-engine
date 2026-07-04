<template>
  <div class="skills">
    <div v-if="!skills.length" class="ph">
      <el-icon :size="34"><MagicStick /></el-icon>
      <p>尚未生成技能</p>
      <span class="ph-sub">
        完成「推导关联 → 推导流程」后，在右侧对话让 AI「生成技能」，
        即可产出可被第三方零改动挂载的 MCP 能力包。
      </span>
    </div>
    <div v-for="s in skills" :key="s.skill_id" class="scard card" :class="{ main: s.is_main }">
      <div class="scard-head">
        <div class="sname">
          <el-icon><MagicStick /></el-icon>
          <span>{{ s.name }}</span>
        </div>
        <el-tag v-if="s.is_main" size="small" type="warning" effect="light" round>主技能</el-tag>
        <el-tag v-else size="small" effect="plain" round>{{ s.operation }}</el-tag>
      </div>
      <div class="sdesc">{{ s.description || s.capability }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { MagicStick } from '@element-plus/icons-vue'
import type { Scenario } from '@/api/types'
const props = defineProps<{ scenario: Scenario | null }>()
const skills = computed(() => props.scenario?.skills || [])
</script>

<style scoped lang="scss">
.skills { padding: 18px; display: flex; flex-direction: column; gap: 10px; overflow-y: auto; height: 100%; }
.ph { margin: auto; display: flex; flex-direction: column; align-items: center; gap: 8px; color: var(--text-3); text-align: center; max-width: 400px; }
.ph p { margin: 4px 0 0; font-size: var(--text-md); font-weight: 600; color: var(--text-2); }
.ph-sub { font-size: var(--text-base); line-height: 1.7; }

.scard { padding: 13px 16px; transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease); }
.scard:hover { border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
.scard.main { border-color: color-mix(in srgb, var(--warning) 45%, transparent); background: var(--warning-soft); }
.scard-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.sname { display: flex; align-items: center; gap: 7px; flex: 1; font-weight: 700; font-size: var(--text-md); color: var(--text-1); min-width: 0; }
.sname .el-icon { color: var(--brand); flex-shrink: 0; }
.scard.main .sname .el-icon { color: var(--warning); }
.sname span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sdesc { font-size: var(--text-base); line-height: 1.6; color: var(--text-2); }
</style>
