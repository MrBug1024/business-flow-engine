<template>
  <div class="skills">
    <div v-if="!skills.length" class="ph">
      <el-icon :size="34"><MagicStick /></el-icon>
      <p>尚未生成技能</p>
      <span class="ph-sub">
        完成「数据链路追踪 → 推导关联 → 推导流程」后，在右侧对话让 AI「生成技能」，
        即可产出可像普通 Skill 一样安装的业务能力包，MCP 挂载作为可选增强。
      </span>
    </div>
    <div v-else class="release-panel card">
      <div>
        <div class="release-title">发布配置</div>
        <div class="release-sub">查看标准 Skill 包、MCP 配置、Docker 发布信息和子 Agent System Prompt。</div>
      </div>
      <el-button type="primary" plain :icon="Setting" @click="configOpen = true">打开发布配置</el-button>
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
    <CapabilityConfigDialog v-model="configOpen" :scenario-id="props.scenario?.id || null" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { MagicStick, Setting } from '@element-plus/icons-vue'
import type { Scenario } from '@/api/types'
import CapabilityConfigDialog from './CapabilityConfigDialog.vue'
const props = defineProps<{ scenario: Scenario | null }>()
const skills = computed(() => props.scenario?.skills || [])
const configOpen = ref(false)
</script>

<style scoped lang="scss">
.skills { padding: 18px; display: flex; flex-direction: column; gap: 10px; overflow-y: auto; height: 100%; }
.ph { margin: auto; display: flex; flex-direction: column; align-items: center; gap: 8px; color: var(--text-3); text-align: center; max-width: 400px; }
.ph p { margin: 4px 0 0; font-size: var(--text-md); font-weight: 600; color: var(--text-2); }
.ph-sub { font-size: var(--text-base); line-height: 1.7; }

.release-panel {
  display: flex; align-items: center; justify-content: space-between; gap: 14px;
  padding: 14px 16px;
  border-color: color-mix(in srgb, var(--brand) 30%, transparent);
  background: var(--brand-soft);
}
.release-title { font-weight: 800; color: var(--text-1); font-size: var(--text-md); }
.release-sub { margin-top: 4px; color: var(--text-2); font-size: var(--text-sm); line-height: 1.55; }

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
