<template>
  <el-dialog
    :model-value="modelValue"
    :title="card?.display_name || '能力发布与配置'"
    width="760px"
    @update:model-value="close"
  >
    <div v-if="loading" class="muted">加载中...</div>
    <template v-else-if="card">
      <div class="ns mono">
        <span class="ns-chip">NS {{ card.namespace }}</span>
        <span class="ns-chip">模式 {{ card.execution_mode }}</span>
        <span v-if="release?.package_dir" class="ns-chip ok">验证使用发布包</span>
        <span class="ns-chip" :class="{ ok: publishAllowed, warn: !publishAllowed }">
          {{ publishAllowed ? '已验证' : `未验证 ${scenarioStatus || 'unknown'}` }}
        </span>
      </div>

      <section>
        <h4>能力摘要</h4>
        <p>{{ card.summary }}</p>
      </section>

      <section>
        <h4>提供的工具</h4>
        <div class="chips">
          <span v-for="t in card.tools" :key="t.name || t" class="chip mono">{{ t.name || t }}</span>
        </div>
      </section>

      <section>
        <h4>标准 Skill 发布方式</h4>
        <div class="publish-grid">
          <div class="install-card primary">
            <div class="install-head">
              <span>标准 Skill 包 / zip</span>
              <el-tag size="small" type="success" effect="light">推荐</el-tag>
            </div>
            <p class="hint">
              Skill 包只包含 <code>system_prompt.md</code> 和标准 Skill 子目录；不混入 MCP、Docker、manifest 或安装文档。
            </p>
            <div class="install-row">
              <span class="url-tag">Skill 名称</span>
              <code class="url mono">{{ skillInstall.skill_name }}</code>
              <el-button size="small" text :icon="CopyDocument" @click="copyText(skillInstall.skill_name, '已复制 Skill 名称')" />
            </div>
            <div class="install-row">
              <span class="url-tag">Skill zip</span>
              <code class="url mono">{{ skillInstall.skill_zip }}</code>
              <el-button size="small" text :icon="CopyDocument" @click="copyText(skillInstall.skill_zip, '已复制 Skill zip 地址')" />
            </div>
            <div class="install-row">
              <span class="url-tag">System Prompt</span>
              <code class="url mono">{{ skillInstall.subagent_prompt_file }}</code>
              <el-button size="small" text :icon="CopyDocument" @click="copyText(skillInstall.subagent_system_prompt, '已复制 System Prompt')" />
            </div>
            <div v-if="childSkills.length" class="child-skills">
              <div class="child-skills-title">包内子 Skill（随 zip 一起安装）</div>
              <span v-for="s in childSkills" :key="s.skill_id || s.name" class="child-skill">
                <strong>{{ s.skill_id }}</strong>
                <em>{{ s.name }}</em>
              </span>
            </div>
            <div class="actions">
              <el-button size="small" type="primary" :icon="Download" @click="downloadUrl(skillInstall.skill_zip, 'skill.zip')">
                下载 Skill zip
              </el-button>
              <el-button size="small" :icon="CopyDocument" @click="copyText(skillInstall.subagent_system_prompt, '已复制子 Agent System Prompt')">
                复制 System Prompt
              </el-button>
            </div>
          </div>

          <div class="install-card">
            <div class="install-head">
              <span>system_prompt.md</span>
              <el-tag size="small" effect="plain">Agent</el-tag>
            </div>
            <p class="hint">
              第三方平台如果支持创建专用 Agent，可直接使用这段系统提示词绑定本业务场景。
            </p>
            <pre class="prompt-box">{{ skillInstall.subagent_system_prompt || 'Skill 包内提供 system_prompt.md。' }}</pre>
          </div>
        </div>

        <div class="release-note">
          <div><strong>Skill 目录</strong><code class="mono">{{ release?.skill_dir || skillInstall.source_dir }}</code></div>
          <div><strong>MCP 目录</strong><code class="mono">{{ release?.mcp_dir || release?.package_dir || '' }}</code></div>
        </div>
      </section>

      <section>
        <h4>MCP / Docker 发布方式</h4>
        <el-segmented v-model="variant" :options="variantOptions" size="small" class="variant-seg" />
        <div class="code">
          <el-button size="small" class="copy" :icon="CopyDocument" @click="copy">复制</el-button>
          <pre>{{ snippet }}</pre>
        </div>
        <div class="hint">
          <template v-if="variant === 'remote'">
            这是平台托管测试配置，经 <code>mcp-remote</code> 连接当前平台服务；适合开发验证，不代表离线发布。
          </template>
          <template v-else>
            这是原生远程 MCP URL 配置；要求第三方平台能访问当前服务地址。
          </template>
        </div>

        <details class="advanced-box">
          <summary>MCP / Docker 发布包</summary>
          <div class="install-card legacy">
            <p class="hint">
              MCP 包与 Skill 包完全分开：它包含 MCP runtime、requirements、Dockerfile 和工具描述，不要求第三方平台识别 Skill。
            </p>
            <div class="install-row">
              <span class="url-tag">MCP 包</span>
              <code class="url mono">{{ skillInstall.mcp_zip || skillInstall.toolplane_docker_zip }}</code>
              <el-button size="small" text :icon="CopyDocument" @click="copyText(skillInstall.mcp_zip || skillInstall.toolplane_docker_zip, '已复制 MCP 包地址')" />
            </div>
            <div class="install-row">
              <span class="url-tag">Docker Image</span>
              <code class="url mono">{{ dockerImagePreview }}</code>
              <el-button size="small" text :icon="CopyDocument" @click="copyText(dockerImagePreview, '已复制镜像名')" />
            </div>
            <div class="install-row">
              <span class="url-tag">Start Command</span>
              <code class="url mono">{{ dockerMode.start_command }}</code>
              <el-button size="small" text :icon="CopyDocument" @click="copyText(dockerMode.start_command, '已复制启动命令')" />
            </div>
            <div class="publish-form">
              <el-input v-model="publishRegistry" size="small">
                <template #prepend>Registry</template>
              </el-input>
              <el-input v-model="publishRepository" size="small">
                <template #prepend>Repository</template>
              </el-input>
              <el-input v-model="publishTag" size="small">
                <template #prepend>Tag</template>
              </el-input>
            </div>
            <div class="actions">
              <el-button size="small" :icon="Download" @click="downloadUrl(skillInstall.mcp_zip || skillInstall.toolplane_docker_zip, 'mcp.zip')">
                下载 MCP 包
              </el-button>
              <el-button size="small" :loading="publishing" :disabled="!publishAllowed" @click="publishDocker">
                发布到 Harbor
              </el-button>
            </div>
            <div v-if="!publishAllowed" class="publish-gate">
              {{ publishBlockReason || '当前场景尚未记录为验证通过。' }}
            </div>
            <div v-if="publishResult" class="publish-log">
              <div class="publish-status" :class="{ ok: publishResult.ok }">
                {{ publishResult.ok ? '发布成功' : '发布失败' }}：{{ publishResult.image || publishResult.error }}
              </div>
              <pre>{{ formatPublishLog(publishResult) }}</pre>
            </div>
          </div>
        </details>
      </section>

      <section>
        <h4>何时使用</h4>
        <ul class="pos"><li v-for="(w, i) in card.when_to_use" :key="i">{{ w }}</li></ul>
      </section>

      <section>
        <h4>何时不要使用</h4>
        <ul class="neg"><li v-for="(w, i) in card.not_for" :key="i">{{ w }}</li></ul>
      </section>

      <section>
        <h4>测试数据</h4>
        <div class="hint">当前：{{ files.length ? files.join('、') : '无，沙盒会回退使用蒸馏阶段原始数据' }}</div>
        <input ref="fi" type="file" multiple style="display: none" @change="onUpload" />
        <div class="upload-row">
          <el-button size="small" :icon="Upload" @click="fi?.click()">上传测试数据</el-button>
          <span class="hint inline">文件名不含后缀应与场景表名一致。</span>
        </div>
      </section>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { CopyDocument, Download, Upload } from '@element-plus/icons-vue'
import { http } from '@/api/http'
import { useSandboxStore } from '@/stores/sandbox'

const props = defineProps<{ modelValue: boolean; scenarioId: string | null }>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()
const sandbox = useSandboxStore()

const loading = ref(false)
const card = ref<any>(null)
const files = ref<string[]>([])
const fi = ref<HTMLInputElement>()

const variant = ref<'remote' | 'native'>('remote')
const variantOptions = [
  { label: 'MCP 桥接', value: 'remote' },
  { label: '远程 MCP URL', value: 'native' },
]
const cfgRemote = ref<any>({})
const cfgNative = ref<any>({})
const skillInstall = ref<any>({})
const release = ref<any>({})
const scenarioStatus = ref('')
const publishAllowed = ref(false)
const publishBlockReason = ref('')
const dockerMode = computed(() => release.value?.install_modes?.toolplane_docker || {})
const publishRegistry = ref('harbor.gshbzw.com/skills')
const publishRepository = ref('')
const publishTag = ref('1.0.0')
const publishing = ref(false)
const publishResult = ref<any>(null)
const dockerImagePreview = computed(() => {
  const registry = (publishRegistry.value || 'harbor.gshbzw.com/skills').replace(/\/+$/, '')
  const repo = publishRepository.value || dockerMode.value.repository || dockerMode.value.server_name || skillInstall.value.skill_name || ''
  const tag = publishTag.value || '1.0.0'
  return repo ? `${registry}/${repo}:${tag}` : dockerMode.value.docker_image || ''
})
const childSkills = computed(() => skillInstall.value?.child_skills || [])
const snippet = computed(() =>
  JSON.stringify((variant.value === 'remote' ? cfgRemote.value : cfgNative.value) || {}, null, 2),
)

watch(
  () => [props.modelValue, props.scenarioId],
  async () => {
    if (!props.modelValue || !props.scenarioId) return
    loading.value = true
    try {
      const cfg = await sandbox.config(props.scenarioId)
      card.value = cfg.card
      cfgRemote.value = cfg.config_example || {}
      cfgNative.value = cfg.config_example_native || {}
      skillInstall.value = cfg.skill_install || {}
      release.value = cfg.release || {}
      scenarioStatus.value = cfg.scenario_status || ''
      publishAllowed.value = !!cfg.publish_allowed
      publishBlockReason.value = cfg.publish_block_reason || ''
      const dm = cfg.release?.install_modes?.toolplane_docker || {}
      publishRegistry.value = dm.registry || 'harbor.gshbzw.com/skills'
      publishRepository.value = dm.repository || dm.server_name || cfg.skill_install?.skill_name || ''
      publishTag.value = dm.tag || '1.0.0'
      publishResult.value = null
      await loadFiles()
    } finally {
      loading.value = false
    }
  },
)

async function loadFiles() {
  if (!props.scenarioId) return
  const { data } = await http.get(`/playground/scenarios/${props.scenarioId}/uploads`)
  files.value = data.files || []
}

function copy() {
  navigator.clipboard.writeText(snippet.value)
  ElMessage.success('已复制配置片段')
}

function copyText(text: string, msg: string) {
  if (!text) return
  navigator.clipboard.writeText(text)
  ElMessage.success(msg)
}

async function downloadUrl(url: string, filename: string) {
  if (!url) return
  const { data } = await http.get(url, { responseType: 'blob' })
  const blobUrl = URL.createObjectURL(data)
  const a = document.createElement('a')
  a.href = blobUrl
  a.download = filename
  a.click()
  URL.revokeObjectURL(blobUrl)
}

async function onUpload(e: Event) {
  const fl = (e.target as HTMLInputElement).files
  if (!fl || !fl.length || !props.scenarioId) return
  const form = new FormData()
  Array.from(fl).forEach((f) => form.append('files', f))
  await http.post(`/playground/scenarios/${props.scenarioId}/uploads`, form)
  ElMessage.success('测试数据已上传')
  await loadFiles()
}

async function publishDocker() {
  if (!props.scenarioId) return
  if (!publishAllowed.value) {
    ElMessage.warning(publishBlockReason.value || '当前场景尚未记录为验证通过。')
    return
  }
  publishing.value = true
  publishResult.value = null
  try {
    const { data } = await http.post(`/scenarios/${props.scenarioId}/release/docker/publish`, {
      registry: publishRegistry.value,
      repository: publishRepository.value,
      tag: publishTag.value,
    })
    publishResult.value = data
    if (data.ok) ElMessage.success('Docker 镜像已发布')
    else ElMessage.error(data.error || 'Docker 发布失败')
  } finally {
    publishing.value = false
  }
}

function formatPublishLog(result: any) {
  const steps = result?.steps || []
  return steps.map((s: any) => [
    `$ ${s.command}`,
    s.stdout || '',
    s.stderr || '',
    `exit ${s.returncode}`,
  ].filter(Boolean).join('\n')).join('\n\n')
}

function close() {
  emit('update:modelValue', false)
}
</script>

<style scoped lang="scss">
.ns { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 6px; }
.ns-chip {
  font-size: var(--text-xs); color: var(--brand); background: var(--brand-soft);
  padding: 3px 9px; border-radius: var(--r-xs);
}
.ns-chip.ok { color: var(--success); background: var(--success-soft); }
.ns-chip.warn { color: var(--warning); background: var(--warning-soft); }
section { margin-top: 18px; }
h4 {
  font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.07em;
  color: var(--text-3); font-weight: 700; margin: 0 0 8px;
}
p { font-size: var(--text-base); line-height: 1.65; margin: 0; color: var(--text-2); }
ul { margin: 0; padding-left: 20px; }
li { font-size: var(--text-base); line-height: 1.7; color: var(--text-2); }
ul.pos li::marker { color: var(--success); }
ul.neg li::marker { color: var(--danger); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }
.chip {
  display: inline-block; font-size: var(--text-xs); padding: 4px 9px;
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: var(--r-xs); color: var(--info);
}
.publish-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.install-card {
  background: var(--surface-2); border: 1px solid var(--border);
  border-radius: var(--r-sm); padding: 12px;
}
.install-card.primary { border-color: color-mix(in srgb, var(--success) 40%, transparent); }
.install-card.legacy { margin-top: 10px; background: var(--surface-1); }
.install-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-weight: 700; color: var(--text-1); }
.install-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.url-tag { flex-shrink: 0; font-size: var(--text-xs); font-weight: 700; color: var(--text-3); }
.url {
  flex: 1; min-width: 0; font-size: var(--text-sm); color: var(--brand);
  background: var(--brand-soft); border: 1px solid color-mix(in srgb, var(--brand) 24%, transparent);
  padding: 6px 10px; border-radius: var(--r-xs);
  overflow-x: auto; white-space: nowrap;
}
.actions { margin-top: 10px; }
.child-skills { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.child-skills-title {
  width: 100%; font-size: var(--text-xs); font-weight: 700; color: var(--text-3);
}
.child-skill {
  display: inline-flex; flex-direction: column; gap: 1px; max-width: 180px;
  border: 1px solid var(--border); background: var(--surface-1);
  border-radius: var(--r-xs); padding: 6px 8px;
}
.child-skill strong {
  font-size: var(--text-xs); color: var(--text-1); font-family: var(--font-mono);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.child-skill em {
  font-size: var(--text-xs); color: var(--text-3); font-style: normal;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.prompt-box {
  margin: 10px 0 0; max-height: 260px; overflow: auto; white-space: pre-wrap;
  font-family: var(--font-mono); font-size: var(--text-xs); line-height: 1.6;
  color: var(--text-2); background: var(--code-bg); border: 1px solid var(--border);
  border-radius: var(--r-sm); padding: 10px;
}
.publish-form { display: grid; gap: 7px; margin-top: 10px; }
.publish-log {
  margin-top: 10px; background: var(--code-bg); border: 1px solid var(--border);
  border-radius: var(--r-sm); padding: 9px 10px;
}
.publish-gate {
  margin-top: 10px; color: var(--warning); background: var(--warning-soft);
  border: 1px solid color-mix(in srgb, var(--warning) 24%, transparent);
  border-radius: var(--r-sm); padding: 8px 10px; font-size: var(--text-xs);
  line-height: 1.6;
}
.publish-status { font-size: var(--text-xs); color: var(--danger); font-weight: 700; margin-bottom: 6px; }
.publish-status.ok { color: var(--success); }
.publish-log pre {
  margin: 0; max-height: 180px; overflow: auto; white-space: pre-wrap;
  font-size: var(--text-xs); line-height: 1.5; color: var(--text-2);
}
.release-note {
  margin-top: 10px; padding: 9px 11px; background: var(--info-soft);
  border: 1px solid color-mix(in srgb, var(--info) 22%, transparent);
  border-radius: var(--r-sm); color: var(--text-2); font-size: var(--text-xs); line-height: 1.7;
}
.release-note code { margin-left: 6px; }
.variant-seg { margin-bottom: 10px; }
.advanced-box { margin-top: 12px; }
.advanced-box summary {
  cursor: pointer; color: var(--text-2); font-size: var(--text-sm); font-weight: 700;
}
.code { position: relative; background: var(--code-bg); border: 1px solid var(--border); border-radius: var(--r-sm); padding: 12px 14px; overflow-x: auto; }
.code pre { margin: 0; font-family: var(--font-mono); font-size: var(--text-sm); color: var(--text-2); line-height: 1.6; }
.copy { position: absolute; top: 8px; right: 8px; }
.hint { font-size: var(--text-xs); color: var(--text-3); margin-top: 8px; line-height: 1.6; }
.hint.inline { margin-top: 0; }
.upload-row { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
code { font-family: var(--font-mono); color: var(--info); background: var(--code-bg); padding: 1px 5px; border-radius: 4px; }
@media (max-width: 760px) {
  .publish-grid { grid-template-columns: 1fr; }
}
</style>
