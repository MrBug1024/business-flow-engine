<template>
  <section class="skill-settings">
    <header class="panel-head">
      <div>
        <h2>{{ tr('title') }}</h2>
        <p>{{ tr('subtitle') }}</p>
      </div>
      <div class="head-actions">
        <el-button :icon="Link" :disabled="folderInstalling" @click="urlFormOpen = !urlFormOpen">{{ tr('installUrl') }}</el-button>
        <el-button :icon="FolderOpened" :loading="folderInstalling" :disabled="urlInstalling" @click="folderInput?.click()">
          {{ tr('installFolder') }}
        </el-button>
        <input
          ref="folderInput"
          class="hidden-input"
          type="file"
          multiple
          webkitdirectory=""
          directory=""
          @change="installFromFolder"
        />
      </div>
    </header>

    <p class="runtime-note">
      <el-icon><InfoFilled /></el-icon>
      {{ tr('runtimeNote') }}
    </p>

    <el-alert
      v-if="operationError"
      class="operation-alert"
      type="error"
      show-icon
      :closable="true"
      :title="operationError"
      @close="operationError = ''"
    />

    <form v-if="urlFormOpen" class="url-install-form" @submit.prevent="installFromUrl">
      <div class="url-form-copy">
        <span class="url-icon"><el-icon><Download /></el-icon></span>
        <div>
          <h3>{{ tr('urlTitle') }}</h3>
          <p>{{ tr('urlHelp') }}</p>
        </div>
      </div>
      <label>
        <span>{{ tr('zipUrl') }}</span>
        <el-input v-model="zipUrl" type="url" placeholder="https://example.com/my-skill.zip" />
      </label>
      <div class="form-actions">
        <el-button @click="closeUrlForm">{{ tr('cancel') }}</el-button>
        <el-button type="primary" native-type="submit" :loading="urlInstalling" :disabled="!zipUrl.trim()">
          {{ tr('install') }}
        </el-button>
      </div>
    </form>

    <div class="skill-scroll">
      <header class="group-head">
        <div>
          <h3>{{ tr('installedSkills') }}</h3>
          <p>{{ installedSkills.length }} {{ tr('installedCount') }}</p>
        </div>
        <span v-if="folderInstalling" class="install-progress"><el-icon class="spin"><Upload /></el-icon>{{ tr('uploading') }}</span>
      </header>

      <div v-if="skills.length" class="skill-grid">
        <article v-for="skill in skills" :key="skill.name" class="skill-card">
          <header class="skill-card-head">
            <span class="skill-icon"><el-icon><MagicStick /></el-icon></span>
            <div class="skill-title">
              <strong :title="skill.name">{{ skill.name }}</strong>
              <span>{{ skill.kind || 'skill' }}</span>
            </div>
            <el-tooltip v-if="skill.locked" :content="tr('systemSkillHelp')" placement="top">
              <span class="lock-mark" :aria-label="tr('systemSkillHelp')"><el-icon><Lock /></el-icon></span>
            </el-tooltip>
            <el-tooltip v-else :content="tr('delete')" placement="top">
              <el-button
                :icon="Delete"
                text
                circle
                class="delete-action"
                :loading="deletingName === skill.name"
                :aria-label="tr('delete')"
                @click="deleteSkill(skill.name)"
              />
            </el-tooltip>
          </header>

          <p class="skill-description">{{ skill.description }}</p>

          <footer class="skill-card-foot">
            <span class="installed-badge"><el-icon><CircleCheck /></el-icon>{{ tr('installed') }}</span>
          </footer>
        </article>
      </div>

      <div v-else class="empty-state">
        <span class="skill-icon"><el-icon><MagicStick /></el-icon></span>
        <strong>{{ tr('emptyTitle') }}</strong>
        <span>{{ tr('emptyBody') }}</span>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheck, Delete, Download, FolderOpened, InfoFilled, Link, Lock, MagicStick, Upload } from '@element-plus/icons-vue'
import { http } from '@/api/http'

type Language = 'zh' | 'en'
type SkillDefinition = {
  name: string
  description?: string
  kind?: string
  locked?: boolean
  enabled?: boolean
}

const props = withDefaults(
  defineProps<{
    skills: SkillDefinition[]
    installedSkills: string[]
    language?: Language
  }>(),
  { language: 'zh' },
)

const emit = defineEmits<{
  changed: [settings?: any]
}>()

const copy: Record<Language, Record<string, string>> = {
  zh: {
    cancel: '取消',
    delete: '删除 Skill',
    deleteBody: '删除后，AI 将无法在后续任务中发现和加载这个 Skill。',
    emptyBody: '可以通过 HTTPS ZIP 链接或本地文件夹安装一个 Skill。',
    emptyTitle: '还没有可用 Skill',
    install: '安装',
    installFolder: '上传文件夹',
    installUrl: '从 URL 安装',
    installed: '已安装',
    installedCount: '个 Skill 已安装',
    installedSkills: 'Skill 能力包',
    runtimeNote: '完整 Skill 包以只读方式加载；依赖和脚本在 Studio 系统级共享 venv 中运行，不会写入业务场景或系统全局 Python。',
    subtitle: '标准 SKILL.md 作为完整能力包被发现并渐进加载；Skill 不是 Tool。',
    systemSkillHelp: '系统 Skill 由项目维护，不可在这里删除。',
    title: 'Skills',
    uploading: '正在上传文件夹',
    urlHelp: '地址必须使用 HTTPS 并返回 ZIP 包；安装后会校验目录结构和 SKILL.md。',
    urlTitle: '通过链接安装 Skill',
    zipUrl: 'HTTPS ZIP 地址',
  },
  en: {
    cancel: 'Cancel',
    delete: 'Delete Skill',
    deleteBody: 'AI will no longer be able to discover or load this Skill in future tasks.',
    emptyBody: 'Install a Skill from an HTTPS ZIP URL or a local folder.',
    emptyTitle: 'No Skills available',
    install: 'Install',
    installFolder: 'Upload folder',
    installUrl: 'Install from URL',
    installed: 'Installed',
    installedCount: 'Skills installed',
    installedSkills: 'Skill packages',
    runtimeNote: 'The complete Skill package is loaded read-only. Dependencies and scripts run in Studio\'s shared system-level venv, outside business workspaces and global Python.',
    subtitle: 'Standard SKILL.md packages are discovered and progressively loaded as complete capabilities. A Skill is not a Tool.',
    systemSkillHelp: 'System Skills are project-managed and cannot be removed here.',
    title: 'Skills',
    uploading: 'Uploading folder',
    urlHelp: 'The HTTPS URL must return a ZIP package. The server validates its structure and SKILL.md.',
    urlTitle: 'Install Skill from URL',
    zipUrl: 'HTTPS ZIP URL',
  },
}

const folderInput = ref<HTMLInputElement | null>(null)
const urlFormOpen = ref(false)
const zipUrl = ref('')
const urlInstalling = ref(false)
const folderInstalling = ref(false)
const deletingName = ref('')
const operationError = ref('')

function tr(key: string) {
  return copy[props.language]?.[key] || copy.zh[key] || key
}

async function installFromUrl() {
  let url: URL
  try {
    url = new URL(zipUrl.value.trim())
  } catch {
    operationError.value = props.language === 'en' ? 'Enter a valid HTTPS ZIP URL.' : '请输入有效的 HTTPS ZIP 地址。'
    return
  }
  if (url.protocol !== 'https:') {
    operationError.value = props.language === 'en' ? 'Only HTTPS URLs are accepted.' : '仅支持 HTTPS 地址。'
    return
  }
  urlInstalling.value = true
  operationError.value = ''
  try {
    const response = await http.post(
      '/skills/install/url',
      { url: url.toString() },
      { headers: { 'X-Studio-Install-Consent': 'true' } },
    )
    emit('changed', response.data?.settings || response.data)
    closeUrlForm()
    ElMessage.success(props.language === 'en' ? 'Skill installed' : 'Skill 已安装')
  } catch (error: any) {
    operationError.value = requestError(error)
  } finally {
    urlInstalling.value = false
  }
}

async function installFromFolder(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  if (!files.length) return
  const form = new FormData()
  for (const file of files) {
    const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
    form.append('files', file, relativePath)
    form.append('paths', relativePath)
  }
  folderInstalling.value = true
  operationError.value = ''
  try {
    const response = await http.post('/skills/install/upload', form, {
      headers: { 'X-Studio-Install-Consent': 'true' },
    })
    emit('changed', response.data?.settings || response.data)
    ElMessage.success(props.language === 'en' ? 'Skill folder installed' : 'Skill 文件夹已安装')
  } catch (error: any) {
    operationError.value = requestError(error)
  } finally {
    folderInstalling.value = false
    input.value = ''
  }
}

async function deleteSkill(name: string) {
  try {
    await ElMessageBox.confirm(tr('deleteBody'), `${tr('delete')} ${name}?`, {
      type: 'warning',
      confirmButtonText: props.language === 'en' ? 'Delete' : '删除',
      cancelButtonText: tr('cancel'),
      confirmButtonClass: 'el-button--danger',
    })
  } catch {
    return
  }
  deletingName.value = name
  operationError.value = ''
  try {
    const response = await http.delete(`/skills/${encodeURIComponent(name)}`)
    emit('changed', response.data?.settings || response.data)
    ElMessage.success(props.language === 'en' ? 'Skill deleted' : 'Skill 已删除')
  } catch (error: any) {
    operationError.value = requestError(error)
  } finally {
    deletingName.value = ''
  }
}

function closeUrlForm() {
  urlFormOpen.value = false
  zipUrl.value = ''
}

function requestError(error: any) {
  const detail = error?.response?.data?.detail || error?.response?.data?.message
  return typeof detail === 'string' ? detail : JSON.stringify(detail || error?.message || 'Request failed')
}
</script>

<style scoped>
.skill-settings {
  display: flex;
  min-height: 100%;
  flex-direction: column;
}

.panel-head,
.head-actions,
.group-head,
.skill-card-head,
.skill-card-foot,
.card-actions,
.form-actions,
.url-form-copy,
.install-progress {
  display: flex;
  align-items: center;
  gap: 10px;
}

.panel-head,
.group-head,
.skill-card-foot {
  justify-content: space-between;
}

.panel-head {
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border-soft);
}

.panel-head h2,
.panel-head p,
.group-head h3,
.group-head p,
.url-install-form h3,
.url-install-form p {
  margin: 0;
}

.panel-head h2,
.url-install-form h3 {
  color: var(--text-strong);
  font-size: 14px;
}

.panel-head p,
.group-head p,
.url-install-form p {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
}

.hidden-input {
  display: none;
}

.runtime-note {
  display: flex;
  gap: 7px;
  align-items: flex-start;
  margin: 9px 0 0;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.5;
}

.runtime-note .el-icon {
  flex: 0 0 auto;
  margin-top: 2px;
  color: var(--accent);
}

.operation-alert {
  margin-top: 12px;
}

.url-install-form {
  display: grid;
  grid-template-columns: minmax(210px, 0.8fr) minmax(260px, 1.5fr) auto;
  gap: 14px;
  align-items: end;
  margin-top: 14px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-1);
}

.url-icon,
.skill-icon {
  display: grid;
  flex: 0 0 34px;
  place-items: center;
  width: 34px;
  height: 34px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-2);
  color: var(--accent);
}

.url-install-form label {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.url-install-form label > span {
  color: var(--text-muted);
  font-size: 11px;
}

.skill-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding-top: 14px;
}

.group-head {
  margin-bottom: 10px;
}

.group-head h3 {
  color: var(--text-strong);
  font-size: 13px;
}

.install-progress {
  color: var(--accent);
  font-size: 11px;
}

.skill-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
}

.skill-card {
  display: flex;
  min-width: 0;
  min-height: 164px;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: 7px;
  background: var(--surface-1);
  transition: border-color 0.16s ease, background-color 0.16s ease;
}

.skill-card:hover {
  border-color: color-mix(in srgb, var(--accent) 42%, var(--border));
  background: color-mix(in srgb, var(--surface-1) 90%, var(--accent-soft));
}

.skill-card-head {
  min-width: 0;
  padding: 11px 11px 8px 12px;
}

.skill-title {
  flex: 1;
  min-width: 0;
}

.skill-title strong,
.skill-title span {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-title strong {
  color: var(--text-strong);
  font-size: 12px;
}

.skill-title span {
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 10px;
  text-transform: uppercase;
}

.lock-mark {
  display: grid;
  flex: 0 0 32px;
  place-items: center;
  width: 32px;
  height: 32px;
  color: var(--text-muted);
}

.delete-action {
  flex: 0 0 32px;
  width: 32px;
  height: 32px;
}

.delete-action:hover {
  color: var(--el-color-danger);
}

.skill-description {
  display: -webkit-box;
  flex: 1;
  margin: 0;
  overflow: hidden;
  padding: 0 12px 10px;
  color: var(--text-main);
  font-size: 11px;
  line-height: 1.55;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}

.skill-card-foot {
  min-height: 48px;
  padding: 7px 10px 7px 12px;
  border-top: 1px solid var(--border-soft);
}

.installed-badge,
.available-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-height: 22px;
  padding: 0 7px;
  border-radius: 4px;
  font-size: 10px;
}

.installed-badge {
  background: color-mix(in srgb, var(--el-color-success) 12%, transparent);
  color: var(--el-color-success);
}

.available-badge {
  background: var(--surface-2);
  color: var(--text-muted);
}

.empty-state {
  display: grid;
  min-height: 180px;
  place-content: center;
  justify-items: center;
  gap: 7px;
  padding: 24px;
  border: 1px dashed var(--border);
  border-radius: 7px;
  color: var(--text-muted);
  text-align: center;
}

.empty-state strong {
  color: var(--text-strong);
  font-size: 13px;
}

.empty-state > span:last-child {
  max-width: 420px;
  font-size: 11px;
  line-height: 1.6;
}

.spin {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 820px) {
  .url-install-form {
    grid-template-columns: minmax(0, 1fr);
    align-items: stretch;
  }

  .form-actions {
    justify-content: flex-end;
  }
}

@media (max-width: 700px) {
  .panel-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .head-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    width: 100%;
  }

  .head-actions :deep(.el-button),
  .form-actions :deep(.el-button),
  .card-actions :deep(.el-button) {
    min-height: 44px;
  }

  .skill-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .url-install-form :deep(.el-input__wrapper) {
    min-height: 44px;
  }

  .url-install-form :deep(.el-input__inner) {
    font-size: 16px;
  }

  .skill-card-foot {
    align-items: flex-start;
    flex-direction: column;
  }

  .delete-action {
    flex-basis: 44px;
    width: 44px;
    height: 44px;
  }

  .card-actions,
  .card-actions :deep(.el-button) {
    width: 100%;
  }
}

@media (prefers-reduced-motion: reduce) {
  .skill-card {
    transition: none;
  }

  .spin {
    animation: none;
  }
}
</style>
