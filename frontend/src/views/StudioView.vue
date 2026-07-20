<template>
  <div
    class="studio-shell"
    :class="[studioThemeClass, { 'settings-focus': activeTab?.kind === 'settings' }]"
    :style="studioLayoutStyle"
  >
    <header class="title-bar">
      <div class="brand-zone">
        <span class="window-dot" aria-hidden="true"></span>
        <strong>AI Business Studio</strong>
      </div>
      <div class="title-center">
        <span>{{ current?.name || t('workspaceFallback') }}</span>
        <span v-if="activeTab" class="title-file">{{ activeTab.title }}</span>
      </div>
      <div class="title-actions">
        <el-button
          :icon="Plus"
          text
          :title="t('newScene')"
          :aria-label="t('newScene')"
          :disabled="isBusy"
          @click="createOpen = true"
        />
        <el-button
          :icon="Refresh"
          text
          :title="t('refreshWorkspace')"
          :aria-label="t('refreshWorkspace')"
          :disabled="!current || isBusy"
          @click="refreshWorkspace"
        />
      </div>
    </header>

    <aside class="activity-bar" :aria-label="t('activityBar')">
      <button class="activity active" :title="t('explorer')" :aria-label="t('explorer')">
        <el-icon><Files /></el-icon>
      </button>
      <button
        class="activity"
        :title="t('graphs')"
        :aria-label="t('graphs')"
        :disabled="!current"
        @click="openGraph('entity')"
      >
        <el-icon><Share /></el-icon>
      </button>
      <button class="activity" :title="t('aiAssistant')" :aria-label="t('aiAssistant')">
        <el-icon><ChatDotRound /></el-icon>
      </button>
      <button
        class="activity bottom"
        :class="{ active: activeTab?.kind === 'settings' }"
        :title="t('settings')"
        :aria-label="t('settings')"
        @click="openSettings()"
      >
        <el-icon><Setting /></el-icon>
      </button>
    </aside>

    <BusinessResourceExplorer
      class="explorer"
      :businesses="businesses"
      :current-business-id="current?.id || ''"
      :trees="businessTrees"
      :loading-business-ids="businessTreeLoading"
      :active-path="activeTabId"
      :busy="isBusy"
      :language="uiLanguage"
      @create-business="createOpen = true"
      @refresh="refreshResourceExplorer"
      @request-tree="loadBusinessTree"
      @open="openBusinessResource"
      @action="handleBusinessResourceAction"
      @import="importBusinessResourceFiles"
    />

    <div
      class="pane-resizer explorer-resizer"
      role="separator"
      aria-orientation="vertical"
      :aria-label="t('resizeExplorer')"
      :aria-valuemin="220"
      :aria-valuemax="420"
      :aria-valuenow="explorerWidth"
      tabindex="0"
      @pointerdown="startPaneResize('explorer', $event)"
      @keydown="handlePaneResizeKey('explorer', $event)"
    />

    <main id="studio-main" class="editor">
      <header class="tabs">
        <button
          v-for="tab in tabs"
          :key="tab.id"
          class="tab"
          :class="{ active: tab.id === activeTabId }"
          @click="activeTabId = tab.id"
        >
          <span>{{ tab.title }}</span>
          <button v-if="tabs.length > 1" class="tab-close" :title="t('close')" @click.stop="closeTab(tab.id)">
            x
          </button>
        </button>
      </header>

      <nav class="breadcrumbs" aria-label="breadcrumbs">
        <span v-for="(part, index) in breadcrumbs" :key="`${part}-${index}`">{{ part }}</span>
      </nav>

      <section class="editor-body">
        <div v-if="!current && activeTab?.kind !== 'settings'" class="empty-editor">
          <el-button type="primary" :icon="Plus" @click="createOpen = true">{{ t('newBusinessScene') }}</el-button>
        </div>

        <template v-else-if="activeTab && (current || activeTab.kind === 'settings')">
          <section v-if="activeTab.kind === 'description'" class="editor-panel scenario-editor">
            <header class="editor-toolbar">
              <div>
                <strong>description.md</strong>
                <span>{{ descriptionDirty ? t('modified') : t('saved') }}</span>
              </div>
              <div class="toolbar-actions">
                <el-button :icon="Refresh" @click="loadDescription">{{ t('reload') }}</el-button>
                <el-button type="primary" :icon="CircleCheck" :disabled="!descriptionDirty" @click="saveDescription">
                  {{ t('saveAndAnalyze') }}
                </el-button>
              </div>
            </header>
            <textarea
              v-model="descriptionContent"
              class="markdown-editor"
              spellcheck="false"
              @input="descriptionDirty = true"
            />
          </section>

          <section v-else-if="activeTab.kind === 'overview'" class="editor-panel">
            <header class="section-head">
              <div>
                <h1>{{ current.name }}</h1>
                <p>{{ current.goal || t('businessWorkspace') }}</p>
              </div>
              <el-tag>{{ statusLabel(current.status) }}</el-tag>
            </header>
            <div class="metrics">
              <article><strong>{{ context.entities.length }}</strong><span>{{ t('entities') }}</span></article>
              <article><strong>{{ context.relations.length }}</strong><span>{{ t('relations') }}</span></article>
              <article><strong>{{ context.evidence.length }}</strong><span>{{ t('evidence') }}</span></article>
              <article><strong>{{ openQuestions.length }}</strong><span>{{ t('questions') }}</span></article>
            </div>
            <div class="split-content">
              <section>
                <h2>{{ t('aiUnderstanding') }}</h2>
                <p v-for="item in context.assumptions" :key="item.id">{{ item.statement }}</p>
                <p v-if="!context.assumptions.length" class="muted">{{ t('noAssumptions') }}</p>
              </section>
              <section>
                <h2>{{ t('latestPlan') }}</h2>
                <ol v-if="latestRun">
                  <li v-for="step in latestRun.plan" :key="step">{{ step }}</li>
                </ol>
                <p v-else class="muted">{{ t('noRun') }}</p>
              </section>
            </div>
          </section>

          <section v-else-if="activeTab.kind === 'context'" class="editor-panel">
            <header class="section-head">
              <div>
                <h1>{{ t('businessContext') }}</h1>
                <p>v{{ current.current_version }}</p>
              </div>
              <el-button :icon="Refresh" @click="refreshCurrent">{{ t('refresh') }}</el-button>
            </header>
            <div class="json-grid">
              <section v-for="block in contextBlocks" :key="block.key">
                <h2>{{ block.title }}</h2>
                <pre>{{ JSON.stringify(block.value, null, 2) }}</pre>
              </section>
            </div>
          </section>

          <section v-else-if="activeTab.kind === 'file'" class="editor-panel">
            <div v-if="activeTab.payload?.live_operation" class="live-file-strip" aria-live="polite">
              <el-icon class="spin"><Loading /></el-icon>
              <span>{{ fileOperationLabel(activeTab.payload.live_operation) }}</span>
            </div>
            <WorkspaceFilePreview
              :payload="activeTab.payload"
              :language="uiLanguage"
              :theme="themeMode"
              @retry="reloadWorkspacePreview"
            />
          </section>

          <section v-else-if="activeTab.kind === 'graph'" class="editor-panel graph-panel">
            <header class="section-head">
              <div>
                <h1>{{ activeTab.title }}</h1>
                <p>Mermaid / {{ activeTab.payload?.graphKind }}</p>
              </div>
              <el-button :icon="Refresh" @click="reloadGraph(activeTab.payload?.graphKind)">{{ t('refresh') }}</el-button>
            </header>
            <div class="mermaid-box" v-html="renderedMermaid" />
            <details class="source-details">
              <summary>Mermaid source</summary>
              <pre>{{ activeTab.payload?.mermaid }}</pre>
            </details>
          </section>

          <section v-else-if="activeTab.kind === 'thinking'" class="editor-panel">
            <header class="section-head">
              <div>
                <h1>{{ t('aiUnderstanding') }}</h1>
                <p>{{ openQuestions.length }} {{ t('openQuestions') }}</p>
              </div>
            </header>
            <div class="question-list">
              <article v-for="question in openQuestions" :key="question.id" class="question-item">
                <div>
                  <strong>{{ question.question }}</strong>
                  <span>{{ question.reason }}</span>
                </div>
                <el-button size="small" :icon="CircleCheck" @click="startConfirm(question)">{{ t('confirm') }}</el-button>
              </article>
            </div>
            <div class="split-content">
              <section>
                <h2>{{ t('evidence') }}</h2>
                <p v-for="item in context.evidence.slice(0, 12)" :key="item.id">{{ item.claim }}</p>
              </section>
              <section>
                <h2>{{ t('confirmations') }}</h2>
                <p v-for="item in context.confirmations" :key="item.id">{{ item.answer }}</p>
                <p v-if="!context.confirmations.length" class="muted">{{ t('noConfirmations') }}</p>
              </section>
            </div>
          </section>

          <section v-else-if="activeTab.kind === 'outputs'" class="editor-panel">
            <header class="section-head">
              <div>
                <h1>output / skill-package</h1>
                <p>{{ current.packages.length }} {{ t('packages') }}</p>
              </div>
            </header>
            <section class="output-section">
              <h2>skill-package.zip</h2>
              <article v-for="pkg in current.packages" :key="pkg.id" class="package-row">
                <span>{{ pkg.filename }} / v{{ pkg.version }}</span>
                <a :href="pkg.download_url" target="_blank">{{ t('download') }}</a>
              </article>
              <p v-if="!current.packages.length" class="muted">{{ t('noPackage') }}</p>
            </section>
          </section>

          <section v-else-if="activeTab.kind === 'capabilities'" class="editor-panel">
            <header class="section-head">
              <div>
                <h1>settings / capabilities.json</h1>
                <p>{{ t('toolSkillMcpRefs') }}</p>
              </div>
            </header>
            <pre>{{ JSON.stringify({ skills: context.skill_references, mcp: context.mcp_references, tools: context.tool_usages }, null, 2) }}</pre>
          </section>

          <section v-else-if="activeTab.kind === 'settings'" class="editor-panel settings-workbench">
            <header class="section-head settings-head">
              <div>
                <h1>{{ t('settings') }}</h1>
                <p>{{ t('settingsDescription') }}</p>
              </div>
              <el-tag>{{ selectedModelLabel }}</el-tag>
            </header>

            <el-tabs v-model="settingsTab" class="settings-tabs">
              <el-tab-pane :label="t('general')" name="general">
                <section class="settings-grid two-columns">
                  <article class="settings-card">
                    <h2>{{ t('language') }}</h2>
                    <p class="muted">{{ t('languageHelp') }}</p>
                    <el-radio-group v-model="uiLanguage">
                      <el-radio-button value="zh">中文</el-radio-button>
                      <el-radio-button value="en">English</el-radio-button>
                    </el-radio-group>
                  </article>

                  <article class="settings-card">
                    <h2>{{ t('theme') }}</h2>
                    <p class="muted">{{ t('themeHelp') }}</p>
                    <el-radio-group v-model="themeMode">
                      <el-radio-button value="dark">{{ t('themeDark') }}</el-radio-button>
                      <el-radio-button value="light">{{ t('themeLight') }}</el-radio-button>
                      <el-radio-button value="contrast">{{ t('themeContrast') }}</el-radio-button>
                    </el-radio-group>
                  </article>

                  <article class="settings-card">
                    <h2>{{ t('workspace') }}</h2>
                    <dl class="settings-facts">
                      <div><dt>{{ t('activeScene') }}</dt><dd>{{ current?.name || '-' }}</dd></div>
                      <div><dt>{{ t('activeFile') }}</dt><dd>{{ activeTab?.id || '-' }}</dd></div>
                      <div><dt>{{ t('version') }}</dt><dd>v{{ current?.current_version || 0 }}</dd></div>
                    </dl>
                  </article>

                  <article class="settings-card">
                    <h2>{{ t('activeModel') }}</h2>
                    <el-select v-model="selectedModel" size="small" @change="setActiveModel">
                      <el-option
                        v-for="item in settings.configured_models"
                        :key="item.id"
                        :label="item.name"
                        :value="item.model"
                      />
                    </el-select>
                  </article>
                </section>
              </el-tab-pane>

              <el-tab-pane :label="t('aiModels')" name="models">
                <ModelSettingsPanel
                  :models="settings.configured_models || []"
                  :active-model="selectedModel"
                  :language="uiLanguage"
                  @changed="reloadCapabilitySettings"
                />
              </el-tab-pane>

              <el-tab-pane :label="t('tools')" name="tools">
                <ToolSettingsPanel
                  :tools="tools"
                  :language="uiLanguage"
                  @refreshed="tools = $event"
                />
              </el-tab-pane>

              <el-tab-pane :label="t('skills')" name="skills">
                <SkillSettingsPanel
                  :skills="skills"
                  :installed-skills="settings.installed_skills || []"
                  :language="uiLanguage"
                  @changed="reloadCapabilitySettings"
                />
              </el-tab-pane>

              <el-tab-pane label="MCP" name="mcp">
                <McpSettingsWorkspace
                  :servers="settings.mcp_configs || []"
                  :templates="[]"
                  :language="uiLanguage"
                  @changed="reloadCapabilitySettings"
                />
              </el-tab-pane>
            </el-tabs>
          </section>
        </template>
      </section>
    </main>

    <div
      class="pane-resizer assistant-resizer"
      role="separator"
      aria-orientation="vertical"
      :aria-label="t('resizeAssistant')"
      :aria-valuemin="360"
      :aria-valuemax="720"
      :aria-valuenow="assistantWidth"
      tabindex="0"
      @pointerdown="startPaneResize('assistant', $event)"
      @keydown="handlePaneResizeKey('assistant', $event)"
    />

    <aside class="right-panel">
      <header class="assistant-head">
        <div class="assistant-identity">
          <span class="assistant-logo" aria-hidden="true">
            <el-icon><MagicStick /></el-icon>
          </span>
          <div>
            <strong>{{ t('aiAssistant') }}</strong>
            <span class="assistant-status">
              <i :class="{ active: isStreaming }"></i>
              {{ isStreaming ? t('generating') : t('ready') }}
            </span>
          </div>
        </div>
        <div class="session-controls">
          <el-select
            v-model="activeChatSessionId"
            class="session-select"
            size="small"
            :disabled="!current || !chatSessions.length || isBusy"
            :aria-label="t('chatSessions')"
            @change="activateChatSession"
          >
            <el-option
              v-for="(session, index) in chatSessions"
              :key="session.id"
              :label="sessionLabel(session, index)"
              :value="session.id"
            />
          </el-select>
          <button
            class="head-action"
            :title="t('newChat')"
            :aria-label="t('newChat')"
            :disabled="!current || isBusy"
            @click="createChatSession"
          >
            <el-icon><Plus /></el-icon>
          </button>
          <el-dropdown trigger="click" :disabled="!current || !activeChatSessionId || isBusy" @command="handleSessionCommand">
            <button class="head-action" :title="t('chatActions')" :aria-label="t('chatActions')">
              <el-icon><MoreFilled /></el-icon>
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="clear">{{ t('clearChat') }}</el-dropdown-item>
                <el-dropdown-item command="delete" divided>{{ t('deleteChat') }}</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <section ref="messagesContainer" class="messages" aria-live="polite" @scroll="handleMessagesScroll">
        <div v-if="!activeMessages.length" class="empty-chat">
          <span class="empty-chat-icon" aria-hidden="true"><el-icon><MagicStick /></el-icon></span>
          <strong>{{ t('chatEmptyTitle') }}</strong>
          <span>{{ t('chatEmptyBody') }}</span>
        </div>
        <article
          v-for="message in activeMessages"
          :key="message.id"
          v-memo="[
            message.id,
            message.content,
            message.kind,
            message.activity_events?.length,
            message.progress?.revision,
            runById[message.run_id]?.status,
          ]"
          class="chat-message"
          :class="[message.role, message.kind]"
        >
          <header class="message-author">
            <div v-if="message.role === 'user'" class="user-identity">
              <time>{{ formatMessageTime(message.created_at) }}</time>
              <strong>{{ t('you') }}</strong>
            </div>
            <div v-else class="ai-identity">
              <span class="message-avatar" aria-hidden="true"><el-icon><MagicStick /></el-icon></span>
              <div>
                <strong>AI</strong>
                <time>{{ formatMessageTime(message.created_at) }}</time>
              </div>
            </div>
            <button
              v-if="message.role === 'assistant'"
              class="message-copy"
              :title="t('copyReply')"
              :aria-label="t('copyReply')"
              @click="copyMessage(message.content)"
            >
              <el-icon><CopyDocument /></el-icon>
            </button>
          </header>
          <div class="message-body">
            <MarkdownContent :content="message.content" />
            <AgentActivity
              v-if="message.role === 'assistant' && runById[message.run_id]"
              :plan="messageActivityPlan(message)"
              :events="messageActivityEvents(message)"
              compact
              :language="uiLanguage"
            />
          </div>
        </article>
        <article v-if="(isStreaming && !streamCompleted) || streamingAssistant" class="chat-message assistant streaming">
          <header class="message-author">
            <div class="ai-identity">
              <span class="message-avatar streaming-avatar" aria-hidden="true"><el-icon><MagicStick /></el-icon></span>
              <div>
                <strong>AI</strong>
                <time>{{ t('generating') }}</time>
              </div>
            </div>
          </header>
          <div class="message-body">
            <AgentActivity
              :plan="executionPlan"
              :events="executionTrace"
              :active="isStreaming"
              :language="uiLanguage"
            />
            <MarkdownContent v-if="streamingAssistant" :content="streamingAssistant" />
          </div>
        </article>
        <button
          v-if="!autoFollowMessages"
          class="scroll-latest"
          :title="t('latestMessage')"
          :aria-label="t('latestMessage')"
          @click="scrollMessages(true)"
        >
          <el-icon><Bottom /></el-icon>
        </button>
      </section>

      <ClarificationSheet
        v-if="questionDockOpen && openQuestions.length"
        v-model:active-id="activeQuestionId"
        :questions="openQuestions"
        :language="uiLanguage"
        :submitting="isSubmittingAnswer"
        :bottom-offset="chatBoxHeight + 8"
        @close="questionDockOpen = false"
        @submit="submitConfirmation"
      />

      <footer ref="chatBoxElement" class="chat-box">
        <div
          v-if="pendingResume && pendingResume.businessId === current?.id && pendingResume.sessionId === activeChatSessionId"
          class="resume-retry"
          role="alert"
          :title="pendingResume.error"
        >
          <span><el-icon><Refresh /></el-icon>{{ t('resumeFailed') }}</span>
          <el-button type="primary" text size="small" :disabled="isBusy" @click="retryPendingResume">
            {{ t('retryContinue') }}
          </el-button>
        </div>
        <div class="composer-shell">
          <WorkspaceMentionMenu
            v-if="mentionMenuOpen"
            :files="mentionOptions"
            :active-index="mentionActiveIndex"
            list-id="workspace-mention-list"
            :title="t('mentionFile')"
            :result-label="`${matchingMentionFiles.length} ${t('mentionFileResults')}`"
            :empty-label="t('mentionFileEmpty')"
            :hint="t('mentionFileHint')"
            @activate="mentionActiveIndex = $event"
            @select="selectMentionFile"
          />
          <div v-if="mentionedFiles.length" class="composer-references" :aria-label="t('mentionFile')">
            <span v-for="file in mentionedFiles" :key="file.path" class="composer-reference" :title="file.path">
              <el-icon aria-hidden="true"><component :is="nodeIcon({ ...file, kind: 'file' })" /></el-icon>
              <span>{{ file.name }}</span>
              <button
                type="button"
                :title="`${t('mentionFileRemove')}: ${file.path}`"
                :aria-label="`${t('mentionFileRemove')}: ${file.path}`"
                @click="removeMentionFile(file.path)"
              >
                ×
              </button>
            </span>
          </div>
          <textarea
            ref="composerInput"
            v-model="chatDraft"
            :aria-label="t('chatPlaceholder')"
            :placeholder="t('chatPlaceholder')"
            :aria-expanded="mentionMenuOpen"
            :aria-controls="mentionMenuOpen ? 'workspace-mention-list' : undefined"
            :aria-activedescendant="mentionMenuOpen && mentionOptions.length ? `workspace-mention-option-${mentionActiveIndex}` : undefined"
            aria-autocomplete="list"
            rows="1"
            @input="handleComposerInput"
            @click="updateMentionFromComposer"
            @focus="updateMentionFromComposer"
            @blur="closeMentionMenu"
            @keydown="handleComposerKeydown"
          />
          <div class="composer-actions">
            <div class="composer-tools">
              <button class="composer-tool" :title="t('uploadData')" :aria-label="t('uploadData')" @click="triggerUpload">
                <el-icon><Upload /></el-icon>
              </button>
              <input ref="fileInput" class="file-input" type="file" multiple @change="uploadFiles" />
              <button
                class="composer-tool mention-tool"
                type="button"
                :title="t('mentionFile')"
                :aria-label="t('mentionFile')"
                :disabled="!current || isBusy || !workspaceMentionFiles.length"
                @mousedown.prevent
                @click="openMentionMenu"
              >
                @
              </button>
              <el-select
                v-model="selectedModel"
                class="composer-model"
                size="small"
                :disabled="!modelOptions.length || isBusy"
                :aria-label="t('model')"
              >
                <el-option v-for="item in modelOptions" :key="item.id" :label="item.name" :value="item.model" />
              </el-select>
            </div>
            <span class="composer-context" :title="current?.name || t('workspaceFallback')">
              <el-icon><CircleCheck /></el-icon>
              {{ current?.name || t('workspaceFallback') }}
            </span>
            <button
              class="send-button"
              :class="{ stop: isStreaming }"
              :disabled="!isStreaming && (isSubmittingAnswer || !current || !canSendChat)"
              :aria-label="isStreaming ? t('stop') : t('send')"
              @click="isStreaming ? stopStreaming() : sendChat()"
            >
              <el-icon v-if="isStreaming"><VideoPause /></el-icon>
              <el-icon v-else><Promotion /></el-icon>
            </button>
          </div>
        </div>
      </footer>
    </aside>

    <footer class="status-bar">
      <span>{{ selectedModelLabel }}</span>
      <span v-if="current">v{{ current.current_version }}</span>
      <span v-if="current">{{ current.files.length }} {{ t('files') }}</span>
      <button
        v-if="current && openQuestions.length"
        class="status-question"
        :title="t('needUserInput')"
        @click="openQuestionDock"
      >
        {{ openQuestions.length }} {{ t('questions') }}
      </button>
      <span v-else-if="current">0 {{ t('questions') }}</span>
      <span v-if="current">{{ current.packages.length }} {{ t('packages') }}</span>
      <span class="status-right">{{ activeTab?.id || 'ready' }}</span>
    </footer>

    <el-dialog v-model="createOpen" :title="t('newBusinessScene')" width="560px">
      <el-form label-position="top">
        <el-form-item :label="t('name')">
          <el-input v-model="createForm.name" :placeholder="t('sceneNamePlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('goal')">
          <el-input v-model="createForm.goal" :placeholder="t('goalPlaceholder')" />
        </el-form-item>
        <el-form-item :label="t('scenarioDescription')">
          <el-input v-model="createForm.description" type="textarea" :rows="4" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createOpen = false">{{ t('cancel') }}</el-button>
        <el-button type="primary" :disabled="!createForm.name.trim()" @click="createBusiness">{{ t('create') }}</el-button>
      </template>
    </el-dialog>

  </div>
</template>

<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Box,
  Bottom,
  ChatDotRound,
  CircleCheck,
  CopyDocument,
  Cpu,
  DataLine,
  Delete,
  Document,
  Files,
  Folder,
  Headset,
  Loading,
  MagicStick,
  MoreFilled,
  Picture,
  Plus,
  Promotion,
  Refresh,
  Setting,
  Share,
  Upload,
  VideoCamera,
  VideoPause,
} from '@element-plus/icons-vue'
import mermaid from 'mermaid'
import { http } from '@/api/http'
import AgentActivity from '@/components/AgentActivity.vue'
import ClarificationSheet from '@/components/ClarificationSheet.vue'
import MarkdownContent from '@/components/MarkdownContent.vue'
import McpSettingsWorkspace from '@/components/McpSettingsWorkspace.vue'
import ModelSettingsPanel from '@/components/ModelSettingsPanel.vue'
import SkillSettingsPanel from '@/components/SkillSettingsPanel.vue'
import ToolSettingsPanel from '@/components/ToolSettingsPanel.vue'
import WorkspaceMentionMenu, { type MentionFile } from '@/components/WorkspaceMentionMenu.vue'
import BusinessResourceExplorer from '@/components/studio/BusinessResourceExplorer.vue'
import { useBusinessResourceTrees } from '@/composables/useBusinessResourceTrees'
import { useBusinessResourceActions } from '@/composables/useBusinessResourceActions'
import { useResizableStudioPanes } from '@/composables/useResizableStudioPanes'
import { useLiveWorkspaceFiles } from '@/composables/useLiveWorkspaceFiles'
import { studioCopy } from '@/composables/studioCopy'
import type {
  Language,
  WorkspaceNode,
} from '@/types/studio'

type ThemeMode = 'dark' | 'light' | 'contrast'
type TabKind = 'description' | 'overview' | 'context' | 'file' | 'graph' | 'thinking' | 'outputs' | 'capabilities' | 'settings'
type Tab = { id: string; title: string; kind: TabKind; payload?: any }
type StreamTarget = { businessId: string; sessionId: string }
type PendingResume = StreamTarget & { runId?: string; error: string }
type MentionTrigger = { start: number; end: number; query: string }
const MENTION_RESULT_LIMIT = 30

const copy = studioCopy

const storedLanguage = localStorage.getItem('studio.language')
const storedTheme = localStorage.getItem('studio.theme')
const uiLanguage = ref<Language>(storedLanguage === 'en' ? 'en' : 'zh')
const themeMode = ref<ThemeMode>(storedTheme === 'light' || storedTheme === 'contrast' ? storedTheme : 'dark')
const WorkspaceFilePreview = defineAsyncComponent(() => import('@/components/WorkspaceFilePreview.vue'))

const businesses = ref<any[]>([])
const current = ref<any | null>(null)
const workspaceTree = ref<WorkspaceNode | null>(null)
const {
  trees: businessTrees,
  loading: businessTreeLoading,
  loadTree: loadBusinessTree,
  setTree: setBusinessTree,
  removeTree: removeBusinessTree,
  retainTrees: retainBusinessTrees,
} = useBusinessResourceTrees()
const tools = ref<any[]>([])
const skills = ref<any[]>([])
const settings = ref<any>({ active_model: '', configured_models: [], installed_tools: [], installed_skills: [], mcp_configs: [] })
const tabs = ref<Tab[]>([])
const activeTabId = ref('')
const renderedMermaid = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const messagesContainer = ref<HTMLElement | null>(null)
const composerInput = ref<HTMLTextAreaElement | null>(null)
const chatBoxElement = ref<HTMLElement | null>(null)
const streamAbortController = ref<AbortController | null>(null)
const autoFollowMessages = ref(true)
const descriptionContent = ref('')
const descriptionDirty = ref(false)
const chatDraft = ref('')
const mentionedFiles = ref<MentionFile[]>([])
const mentionTrigger = ref<MentionTrigger | null>(null)
const mentionActiveIndex = ref(0)
const selectedModel = ref('')
const activeChatSessionId = ref('')
const settingsTab = ref('general')
const createOpen = ref(false)
const createForm = ref({ name: '', goal: '', description: '' })
const isStreaming = ref(false)
const streamCompleted = ref(false)
const streamingAssistant = ref('')
const executionPlan = ref<string[]>([])
const executionTrace = ref<any[]>([])
const questionDockOpen = ref(false)
const activeQuestionId = ref('')
const isSubmittingAnswer = ref(false)
const activeStreamTarget = ref<StreamTarget | null>(null)
const pendingResume = ref<PendingResume | null>(null)
const chatBoxHeight = ref(96)
let chatBoxObserver: ResizeObserver | null = null

const activeTab = computed(() => tabs.value.find((tab) => tab.id === activeTabId.value))
const {
  explorerWidth,
  assistantWidth,
  studioLayoutStyle,
  startPaneResize,
  stopPaneResize,
  handlePaneResizeKey,
} = useResizableStudioPanes(computed(() => activeTab.value?.kind === 'settings'))
const {
  refreshResourceExplorer,
  openBusinessResource,
  handleBusinessResourceAction,
  importBusinessResourceFiles,
} = useBusinessResourceActions({
  current,
  tabs,
  activeTabId,
  businessTrees,
  workspaceTree,
  t,
  loadBusinesses,
  loadBusinessTree,
  selectBusiness,
  openWorkspaceNode,
  openWorkspaceFile,
  deleteBusiness,
})
const {
  handleFileOperationEvent,
  settleLiveFileOperations,
  renderExistingLiveDraft,
  clearLiveFileDrafts,
  disposeLiveFileDrafts,
  fileOperationLabel,
} = useLiveWorkspaceFiles({
  current,
  tabs,
  activeTabId,
  t,
  reloadWorkspaceTree,
  openWorkspaceFile,
  closeTab,
})
const context = computed(() => current.value?.context || emptyContext())
const chatSessions = computed<any[]>(() => current.value?.chat_sessions || [])
const activeMessages = computed<any[]>(() => {
  const messages = current.value?.messages || []
  if (!activeChatSessionId.value) return messages
  return messages.filter((message: any) => message.session_id === activeChatSessionId.value)
})
const latestRun = computed(() => {
  const runs = (current.value?.runs || []).filter((run: any) => (
    !activeChatSessionId.value || run.session_id === activeChatSessionId.value
  ))
  return runs.length ? runs[runs.length - 1] : null
})
const runById = computed<Record<string, any>>(() => Object.fromEntries(
  (current.value?.runs || []).map((run: any) => [run.id, run]),
))
const openQuestions = computed(() => (context.value.questions || []).filter((item: any) => (
  item.status !== 'answered'
  && (!item.session_id || item.session_id === activeChatSessionId.value)
)))
const isBusy = computed(() => isStreaming.value || isSubmittingAnswer.value)
const modelOptions = computed(() => (settings.value.configured_models || []).filter((item: any) => item.enabled))
const selectedModelLabel = computed(() => {
  const match = (settings.value.configured_models || []).find((item: any) => item.model === selectedModel.value)
  return match?.name || selectedModel.value || t('noModel')
})
const breadcrumbs = computed(() => {
  const root = current.value?.name || 'AI Business Studio'
  if (!activeTab.value) return [root]
  if (activeTab.value.id === 'overview') return [root]
  return [root, ...activeTab.value.id.split('/')]
})
const workspaceMentionFiles = computed<MentionFile[]>(() => {
  if (!workspaceTree.value) return []
  return collectWorkspaceFiles(workspaceTree.value).sort(compareMentionFiles)
})
const matchingMentionFiles = computed<MentionFile[]>(() => {
  const query = normalizeMentionQuery(mentionTrigger.value?.query || '')
  if (!query) return workspaceMentionFiles.value
  return workspaceMentionFiles.value
    .map((file) => ({ file, score: mentionMatchScore(file, query) }))
    .filter((item) => item.score >= 0)
    .sort((left, right) => left.score - right.score || compareMentionFiles(left.file, right.file))
    .map((item) => item.file)
})
const mentionOptions = computed(() => matchingMentionFiles.value.slice(0, MENTION_RESULT_LIMIT))
const mentionMenuOpen = computed(() => Boolean(
  current.value && !isBusy.value && mentionTrigger.value,
))
const canSendChat = computed(() => Boolean(chatDraft.value.trim() || mentionedFiles.value.length))
const contextBlocks = computed(() => [
  { key: 'requirements', title: 'Requirements', value: context.value.user_requirements },
  { key: 'files', title: 'Source Files', value: context.value.source_files },
  { key: 'entities', title: t('entities'), value: context.value.entities },
  { key: 'relations', title: t('relations'), value: context.value.relations },
  { key: 'rules', title: 'Rules', value: context.value.rules },
  { key: 'evidence', title: t('evidence'), value: context.value.evidence },
  { key: 'skills', title: 'Skills', value: context.value.skill_references },
  { key: 'mcp', title: 'MCP', value: context.value.mcp_references },
])
const studioThemeClass = computed(() => `theme-${themeMode.value}`)

initializeMermaid()

onMounted(async () => {
  applyDocumentTheme()
  startChatBoxObserver()
  await Promise.all([loadSettings(), loadCapabilities(), loadBusinesses()])
  if (businesses.value.length) await selectBusiness(businesses.value[0].id)
})

onBeforeUnmount(() => {
  stopPaneResize()
  chatBoxObserver?.disconnect()
  streamAbortController.value?.abort()
  disposeLiveFileDrafts()
})

watch(activeTab, async (tab) => {
  if (tab?.kind === 'graph' && tab.payload?.mermaid) {
    await renderMermaid(tab.payload.mermaid)
  } else {
    renderedMermaid.value = ''
  }
})

watch(uiLanguage, () => {
  localStorage.setItem('studio.language', uiLanguage.value)
  const tab = tabs.value.find((item) => item.id === 'settings')
  if (tab) tab.title = t('settings')
})

watch(themeMode, async () => {
  localStorage.setItem('studio.theme', themeMode.value)
  applyDocumentTheme()
  initializeMermaid()
  if (activeTab.value?.kind === 'graph' && activeTab.value.payload?.mermaid) {
    await renderMermaid(activeTab.value.payload.mermaid)
  }
})

watch(openQuestions, (questions) => {
  if (!questions.length) {
    questionDockOpen.value = false
    activeQuestionId.value = ''
  } else if (!questions.some((question: any) => question.id === activeQuestionId.value)) {
    activeQuestionId.value = questions[0].id
  }
})

watch(() => current.value?.id, resetComposerFileReferences)
watch(activeChatSessionId, resetComposerFileReferences)

watch(workspaceMentionFiles, (files) => {
  const availablePaths = new Set(files.map((file) => file.path))
  mentionedFiles.value = mentionedFiles.value.filter((file) => availablePaths.has(file.path))
})

watch(mentionOptions, (files) => {
  mentionActiveIndex.value = Math.min(mentionActiveIndex.value, Math.max(0, files.length - 1))
})

function t(key: string) {
  return copy[uiLanguage.value][key] || copy.en[key] || key
}

function formatMessageTime(value?: number) {
  if (!value) return ''
  const timestamp = value < 1_000_000_000_000 ? value * 1000 : value
  return new Intl.DateTimeFormat(uiLanguage.value === 'zh' ? 'zh-CN' : 'en', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(timestamp))
}

function applyDocumentTheme() {
  document.documentElement.dataset.studioTheme = themeMode.value
}

function startChatBoxObserver() {
  if (!chatBoxElement.value || typeof ResizeObserver === 'undefined') return
  chatBoxObserver?.disconnect()
  chatBoxObserver = new ResizeObserver(([entry]) => {
    if (entry) chatBoxHeight.value = Math.ceil(entry.target.getBoundingClientRect().height)
  })
  chatBoxObserver.observe(chatBoxElement.value)
  chatBoxHeight.value = Math.ceil(chatBoxElement.value.getBoundingClientRect().height)
}

function initializeMermaid() {
  mermaid.initialize({
    startOnLoad: false,
    theme: themeMode.value === 'light' ? 'default' : 'dark',
    securityLevel: 'loose',
  })
}

async function loadBusinesses() {
  businesses.value = (await http.get('/businesses')).data
  retainBusinessTrees(businesses.value.map((item: any) => item.id))
}

async function loadCapabilities() {
  const [toolRes, skillRes] = await Promise.all([
    http.get('/tools'),
    http.get('/skills'),
  ])
  tools.value = toolRes.data
  skills.value = skillRes.data
}

async function loadSettings() {
  settings.value = (await http.get('/settings')).data
  selectedModel.value = settings.value.active_model
}

async function reloadCapabilitySettings() {
  await Promise.all([loadSettings(), loadCapabilities()])
}

function chatSessionExists(record: any, sessionId: string) {
  return Boolean(sessionId && (record?.chat_sessions || []).some((session: any) => session.id === sessionId))
}

function sessionLabel(session: any, index: number) {
  return String(session?.title || '').trim() || `${t('newChat')} ${index + 1}`
}

function preferredChatSessionId(record: any) {
  const sessions = record?.chat_sessions || []
  if (!sessions.length) return ''
  const stored = localStorage.getItem(`studio.chatSession.${record.id}`) || ''
  if (sessions.some((session: any) => session.id === stored)) return stored
  return sessions[sessions.length - 1].id
}

async function activateChatSession(sessionId: string) {
  if (!current.value || isBusy.value || !chatSessionExists(current.value, sessionId)) return
  activeChatSessionId.value = sessionId
  localStorage.setItem(`studio.chatSession.${current.value.id}`, sessionId)
  resetAgentActivity()
  await scrollMessages(true)
}

async function createChatSession() {
  if (!current.value || isBusy.value) return
  const existing = new Set(chatSessions.value.map((session: any) => session.id))
  const response = await http.post(`/businesses/${current.value.id}/chat/sessions`, {})
  current.value = response.data.record || response.data
  const created = chatSessions.value.find((session: any) => !existing.has(session.id))
    || chatSessions.value[chatSessions.value.length - 1]
  if (created) await activateChatSession(created.id)
}

async function handleSessionCommand(command: string) {
  if (command === 'clear') await clearChatSession()
  if (command === 'delete') await deleteChatSession()
}

async function clearChatSession() {
  if (!current.value || !activeChatSessionId.value || isBusy.value) return
  try {
    await ElMessageBox.confirm(t('clearChatConfirm'), t('clearChat'), {
      confirmButtonText: t('clearChat'),
      cancelButtonText: t('cancel'),
      type: 'warning',
    })
  } catch {
    return
  }
  const response = await http.delete(
    `/businesses/${current.value.id}/chat/sessions/${activeChatSessionId.value}/messages`,
  )
  current.value = response.data.record || response.data
  resetAgentActivity()
  await scrollMessages(true)
}

async function deleteChatSession() {
  if (!current.value || !activeChatSessionId.value || isBusy.value) return
  try {
    await ElMessageBox.confirm(t('deleteChatConfirm'), t('deleteChat'), {
      confirmButtonText: t('deleteChat'),
      cancelButtonText: t('cancel'),
      type: 'warning',
    })
  } catch {
    return
  }
  const deletedId = activeChatSessionId.value
  const response = await http.delete(`/businesses/${current.value.id}/chat/sessions/${deletedId}`)
  current.value = response.data.record || response.data
  activeChatSessionId.value = preferredChatSessionId(current.value)
  if (activeChatSessionId.value) {
    localStorage.setItem(`studio.chatSession.${current.value.id}`, activeChatSessionId.value)
  }
  resetAgentActivity()
  await scrollMessages(true)
}

async function selectBusiness(id: string) {
  if (isBusy.value) return
  clearLiveFileDrafts()
  current.value = (await http.get(`/businesses/${id}`)).data
  activeChatSessionId.value = preferredChatSessionId(current.value)
  tabs.value = []
  await refreshWorkspace()
  await loadDescription()
  openDescription()
  await loadBusinesses()
  await scrollMessages(true)
}

async function refreshCurrent() {
  if (!current.value) return
  const previousSessionId = activeChatSessionId.value
  current.value = (await http.get(`/businesses/${current.value.id}`)).data
  activeChatSessionId.value = chatSessionExists(current.value, previousSessionId)
    ? previousSessionId
    : preferredChatSessionId(current.value)
  await loadBusinesses()
}

async function refreshWorkspace() {
  if (!current.value) return
  await refreshCurrent()
  await reloadWorkspaceTree()
}

async function reloadWorkspaceTree() {
  if (!current.value) return
  workspaceTree.value = (await http.get(`/businesses/${current.value.id}/workspace/tree`)).data
  if (workspaceTree.value) setBusinessTree(current.value.id, workspaceTree.value)
}

async function loadDescription() {
  if (!current.value) return
  const res = await http.get(`/businesses/${current.value.id}/description`)
  descriptionContent.value = res.data.content
  descriptionDirty.value = false
}

async function saveDescription() {
  if (!current.value) return
  current.value = (await http.patch(`/businesses/${current.value.id}/description`, { content: descriptionContent.value })).data
  descriptionDirty.value = false
  await refreshWorkspace()
  if (openQuestions.value.length) openThinking()
  ElMessage.success(t('scenarioSaved'))
}

async function createBusiness() {
  if (isBusy.value) return
  const res = await http.post('/businesses', createForm.value)
  createOpen.value = false
  createForm.value = { name: '', goal: '', description: '' }
  await loadBusinesses()
  await selectBusiness(res.data.id)
}

async function deleteBusiness(id: string, name: string) {
  if (isBusy.value) return
  try {
    await ElMessageBox.confirm(t('deleteSceneConfirmBody'), `${t('deleteSceneConfirmTitle')} ${name}`, {
      confirmButtonText: t('deleteScene'),
      cancelButtonText: t('cancel'),
      type: 'warning',
    })
  } catch {
    return
  }
  await http.delete(`/businesses/${id}`)
  removeBusinessTree(id)
  if (current.value?.id === id) {
    current.value = null
    activeChatSessionId.value = ''
    workspaceTree.value = null
    tabs.value = []
    activeTabId.value = ''
    resetAgentActivity()
  }
  await loadBusinesses()
  if (!current.value && businesses.value.length) await selectBusiness(businesses.value[0].id)
  ElMessage.success(t('deleted'))
}

function triggerUpload() {
  if (isBusy.value) return
  fileInput.value?.click()
}

async function uploadFiles(event: Event) {
  if (!current.value || isBusy.value) return
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files || [])
  if (!files.length) return
  const form = new FormData()
  files.forEach((file) => form.append('files', file))
  current.value = (await http.post(`/businesses/${current.value.id}/files`, form)).data
  input.value = ''
  await refreshWorkspace()
  openContext()
  ElMessage.success(t('uploadSuccess'))
}

function openDescription() {
  openTab({ id: 'description.md', title: 'description.md', kind: 'description' })
}

function openOverview() {
  openTab({ id: 'overview', title: current.value?.name || t('workspace'), kind: 'overview' })
}

function openContext() {
  openTab({ id: 'context/business_context.json', title: 'business_context.json', kind: 'context' })
}

async function openFile(file: any) {
  await openWorkspaceFile({ name: file.filename, path: `data/${file.filename}` })
}

async function openGraph(kind: string) {
  if (!current.value) return
  const payload = (await http.get(`/businesses/${current.value.id}/graphs/${kind}`)).data
  payload.graphKind = kind
  const titleMap: Record<string, string> = { entity: 'entity.mmd', flow: 'flow.mmd', lineage: 'lineage.mmd', evidence: 'evidence.mmd' }
  openTab({ id: `graphs/${titleMap[kind] || `${kind}.mmd`}`, title: titleMap[kind] || 'graph.mmd', kind: 'graph', payload })
}

async function reloadGraph(kind: string) {
  if (kind) await openGraph(kind)
}

function openThinking() {
  openTab({ id: 'ai-understanding', title: t('aiUnderstanding'), kind: 'thinking' })
}

function openOutputs() {
  openTab({ id: 'output/skill-package', title: 'skill-package', kind: 'outputs' })
}

function openCapabilities() {
  openTab({ id: 'settings/capabilities.json', title: 'capabilities.json', kind: 'capabilities' })
}

function openSettings(tab = 'general') {
  settingsTab.value = tab
  openTab({ id: 'settings', title: t('settings'), kind: 'settings' })
}

function openWorkspaceNode(node: WorkspaceNode) {
  if (node.kind === 'folder') return
  if (node.path === 'description.md') return openDescription()
  if (node.path === 'context/business_context.json') return openContext()
  if (node.kind === 'file') return openWorkspaceFile(node)
  if (!node.path) return openOverview()
}

async function openWorkspaceFile(node: Pick<WorkspaceNode, 'name' | 'path'>) {
  if (!current.value || !node.path) return
  if (renderExistingLiveDraft(node.path)) return
  const pending = { path: node.path, filename: node.name, loading: true }
  openTab({ id: node.path, title: node.name, kind: 'file', payload: pending })
  try {
    const payload = (await http.get(`/businesses/${current.value.id}/workspace/preview`, {
      params: { path: node.path },
    })).data
    openTab({ id: node.path, title: node.name, kind: 'file', payload })
  } catch (error: any) {
    openTab({
      id: node.path,
      title: node.name,
      kind: 'file',
      payload: {
        ...pending,
        loading: false,
        error: error?.response?.data?.detail || error?.message || t('noPreview'),
      },
    })
  }
}

async function reloadWorkspacePreview() {
  if (activeTab.value?.kind !== 'file' || !activeTab.value.payload?.path) return
  await openWorkspaceFile({ name: activeTab.value.title, path: activeTab.value.payload.path })
}

function openTab(tab: Tab) {
  const index = tabs.value.findIndex((item) => item.id === tab.id)
  if (index >= 0) tabs.value[index] = tab
  else tabs.value.push(tab)
  activeTabId.value = tab.id
}

function closeTab(id: string) {
  const index = tabs.value.findIndex((tab) => tab.id === id)
  if (index < 0) return
  tabs.value.splice(index, 1)
  if (activeTabId.value === id) activeTabId.value = tabs.value[Math.max(0, index - 1)]?.id || ''
}

function collectWorkspaceFiles(node: WorkspaceNode, result: MentionFile[] = []) {
  if (node.kind === 'file') {
    result.push({ name: node.name, path: node.path, icon: node.icon })
    return result
  }
  for (const child of node.children || []) collectWorkspaceFiles(child, result)
  return result
}

function compareMentionFiles(left: MentionFile, right: MentionFile) {
  return left.path.localeCompare(right.path, uiLanguage.value === 'zh' ? 'zh-CN' : 'en', {
    numeric: true,
    sensitivity: 'base',
  })
}

function normalizeMentionQuery(value: string) {
  return value.trim().toLocaleLowerCase()
}

function mentionMatchScore(file: MentionFile, query: string) {
  const name = file.name.toLocaleLowerCase()
  const path = file.path.toLocaleLowerCase()
  if (name === query) return 0
  if (name.startsWith(query)) return 1
  if (path.startsWith(query)) return 2
  if (name.includes(query)) return 3
  if (path.includes(query)) return 4
  return -1
}

function findMentionTrigger(value: string, cursor: number): MentionTrigger | null {
  const beforeCursor = value.slice(0, cursor)
  const start = beforeCursor.lastIndexOf('@')
  if (start < 0) return null
  const previous = start > 0 ? beforeCursor[start - 1] : ''
  if (previous && !/[\s(\[{"'“‘，。！？、；：]/.test(previous)) return null
  const query = beforeCursor.slice(start + 1)
  if (query.includes('\n') || query.length > 120) return null
  return { start, end: cursor, query }
}

function updateMentionFromComposer(event?: Event) {
  const textarea = (event?.target as HTMLTextAreaElement | null) || composerInput.value
  if (!textarea || isBusy.value) {
    mentionTrigger.value = null
    return
  }
  const nextTrigger = findMentionTrigger(chatDraft.value, textarea.selectionStart ?? chatDraft.value.length)
  const queryChanged = nextTrigger?.query !== mentionTrigger.value?.query
  mentionTrigger.value = nextTrigger
  if (queryChanged) mentionActiveIndex.value = 0
}

function handleComposerInput(event: Event) {
  resizeComposer(event)
  updateMentionFromComposer(event)
}

function closeMentionMenu() {
  mentionTrigger.value = null
}

async function openMentionMenu() {
  if (!current.value || isBusy.value || !workspaceMentionFiles.value.length) return
  const textarea = composerInput.value
  if (!textarea) return
  const cursor = textarea.selectionStart ?? chatDraft.value.length
  const existingTrigger = findMentionTrigger(chatDraft.value, cursor)
  if (!existingTrigger) {
    chatDraft.value = `${chatDraft.value.slice(0, cursor)}@${chatDraft.value.slice(cursor)}`
  }
  await nextTick()
  const nextCursor = existingTrigger ? cursor : cursor + 1
  textarea.focus()
  textarea.setSelectionRange(nextCursor, nextCursor)
  updateMentionFromComposer()
  resizeComposer()
}

async function selectMentionFile(file: MentionFile) {
  const trigger = mentionTrigger.value
  if (!trigger) return
  if (!mentionedFiles.value.some((item) => item.path === file.path)) {
    mentionedFiles.value = [...mentionedFiles.value, file]
  }
  const before = chatDraft.value.slice(0, trigger.start)
  const after = chatDraft.value.slice(trigger.end)
  const separator = before && after && !/\s$/.test(before) && !/^\s/.test(after) ? ' ' : ''
  chatDraft.value = `${before}${separator}${after}`
  mentionTrigger.value = null
  await nextTick()
  const cursor = before.length + separator.length
  composerInput.value?.focus()
  composerInput.value?.setSelectionRange(cursor, cursor)
  resizeComposer()
}

function removeMentionFile(path: string) {
  mentionedFiles.value = mentionedFiles.value.filter((file) => file.path !== path)
}

function resetComposerFileReferences() {
  mentionedFiles.value = []
  mentionTrigger.value = null
  mentionActiveIndex.value = 0
}

function composeChatMessage() {
  const prompt = chatDraft.value.trim()
  if (!mentionedFiles.value.length) return prompt
  const references = mentionedFiles.value
    .map((file) => `- @${file.path}`)
    .join('\n')
  return `${prompt}${prompt ? '\n\n' : ''}${t('referencedFilesPrompt')}\n${references}`
}

async function sendChat() {
  if (!current.value || !activeChatSessionId.value || !canSendChat.value || isBusy.value) return
  const target: StreamTarget = {
    businessId: current.value.id,
    sessionId: activeChatSessionId.value,
  }
  const message = composeChatMessage()
  chatDraft.value = ''
  resetComposerFileReferences()
  await nextTick()
  resizeComposer()
  current.value.messages = [
    ...(current.value.messages || []),
    {
      id: `local-${Date.now()}`,
      role: 'user',
      content: message,
      created_at: Date.now() / 1000,
      session_id: target.sessionId,
    },
  ]
  resetAgentActivity()
  isStreaming.value = true
  activeStreamTarget.value = target
  autoFollowMessages.value = true
  streamAbortController.value = new AbortController()
  await scrollMessages(true)
  try {
    await streamChat(message, target, streamAbortController.value.signal)
  } catch (error: any) {
    if (error?.name !== 'AbortError') {
      ElMessage.error(error?.message || 'AI stream failed')
    }
  } finally {
    streamAbortController.value = null
    isStreaming.value = false
    if (isCurrentStreamTarget(target)) {
      await refreshWorkspace()
      await settleLiveFileOperations()
    }
    if (activeStreamTarget.value?.businessId === target.businessId
      && activeStreamTarget.value?.sessionId === target.sessionId) {
      activeStreamTarget.value = null
    }
    streamingAssistant.value = ''
    await scrollMessages(true)
  }
}

function stopStreaming() {
  streamAbortController.value?.abort()
}

function handleComposerKeydown(event: KeyboardEvent) {
  if (event.isComposing) return
  if (mentionMenuOpen.value) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (!mentionOptions.value.length) return
      const direction = event.key === 'ArrowDown' ? 1 : -1
      mentionActiveIndex.value = (
        mentionActiveIndex.value + direction + mentionOptions.value.length
      ) % mentionOptions.value.length
      return
    }
    if ((event.key === 'Enter' || event.key === 'Tab') && !event.shiftKey && !event.altKey) {
      event.preventDefault()
      const selected = mentionOptions.value[mentionActiveIndex.value]
      if (selected) void selectMentionFile(selected)
      else closeMentionMenu()
      return
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      closeMentionMenu()
      return
    }
  }
  if (event.key !== 'Enter' || event.shiftKey || event.altKey) return
  event.preventDefault()
  void sendChat()
}

function resizeComposer(event?: Event) {
  const textarea = (event?.target as HTMLTextAreaElement | null) || composerInput.value
  if (!textarea) return
  textarea.style.height = 'auto'
  textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`
}

function resetAgentActivity() {
  streamCompleted.value = false
  streamingAssistant.value = ''
  executionPlan.value = []
  executionTrace.value = []
  activeQuestionId.value = ''
  questionDockOpen.value = false
}

async function streamChat(message: string, target: StreamTarget, signal: AbortSignal) {
  const response = await fetch(`/api/businesses/${target.businessId}/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message,
      model: selectedModel.value,
      session_id: target.sessionId,
    }),
    signal,
  })
  await consumeAgentStream(response)
}

async function streamResume(target: StreamTarget, runId: string | undefined, signal: AbortSignal) {
  const response = await fetch(
    `/api/businesses/${target.businessId}/chat/sessions/${encodeURIComponent(target.sessionId)}/resume/stream`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: selectedModel.value,
        run_id: runId || undefined,
      }),
      signal,
    },
  )
  await consumeAgentStream(response)
}

async function consumeAgentStream(response: Response) {
  if (!response.ok || !response.body) {
    let detail = `stream failed: ${response.status}`
    try {
      const body = await response.json()
      if (typeof body?.detail === 'string') detail = body.detail
    } catch {
      // Keep the status-based fallback when the server does not return JSON.
    }
    throw Object.assign(new Error(detail), { status: response.status })
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let terminalEventReceived = false
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const events = buffer.split('\n\n')
    buffer = events.pop() || ''
    for (const raw of events) {
      if (handleSseEvent(raw) === 'done') terminalEventReceived = true
    }
  }
  if (buffer.trim() && handleSseEvent(buffer) === 'done') terminalEventReceived = true
  if (!terminalEventReceived) throw new Error('Agent stream ended before a terminal event was received.')
}

async function resumeAgent(runId?: string, requestedTarget?: StreamTarget) {
  if (!current.value || !activeChatSessionId.value || isBusy.value) return
  const target = requestedTarget || {
    businessId: current.value.id,
    sessionId: activeChatSessionId.value,
  }
  if (!isCurrentStreamTarget(target)) return
  resetAgentActivity()
  isStreaming.value = true
  activeStreamTarget.value = target
  autoFollowMessages.value = true
  streamAbortController.value = new AbortController()
  let resumeConflict = false
  if (pendingResume.value?.businessId === target.businessId
    && pendingResume.value?.sessionId === target.sessionId) {
    pendingResume.value = null
  }
  await scrollMessages(true)
  try {
    await streamResume(target, runId, streamAbortController.value.signal)
  } catch (error: any) {
    if (error?.name === 'AbortError') {
      pendingResume.value = {
        ...target,
        runId,
        error: 'Agent continuation was stopped before completion.',
      }
    } else {
      resumeConflict = error?.status === 409
      if (!resumeConflict) {
        pendingResume.value = {
          ...target,
          runId,
          error: error?.message || 'Agent resume failed',
        }
      }
      ElMessage.error(error?.message || 'Agent resume failed')
    }
  } finally {
    streamAbortController.value = null
    isStreaming.value = false
    if (isCurrentStreamTarget(target)) {
      await refreshWorkspace()
      await settleLiveFileOperations()
      if (resumeConflict) {
        pendingResume.value = null
        if (openQuestions.value.length) openQuestionDock()
      }
    }
    if (activeStreamTarget.value?.businessId === target.businessId
      && activeStreamTarget.value?.sessionId === target.sessionId) {
      activeStreamTarget.value = null
    }
    streamingAssistant.value = ''
    await scrollMessages(true)
  }
}

function handleSseEvent(raw: string) {
  const dataLine = raw
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trim())
    .join('\n')
  if (!dataLine) return undefined
  const event = JSON.parse(dataLine)
  handleRunEvent(event)
  return event.type as string
}

function handleRunEvent(event: any) {
  if (activeStreamTarget.value && !isCurrentStreamTarget(activeStreamTarget.value)) return
  if (event.type === 'message' && event.message && current.value) {
    const localIndex = (current.value.messages || []).findIndex((message: any) => (
      String(message.id).startsWith('local-')
      && message.session_id === activeChatSessionId.value
      && message.content === event.message.content
    ))
    if (localIndex >= 0) current.value.messages[localIndex] = event.message
    else if (!(current.value.messages || []).some((message: any) => message.id === event.message.id)) {
      current.value.messages.push(event.message)
    }
  } else if (event.type === 'run_start' && event.run && current.value) {
    streamCompleted.value = false
    const index = (current.value.runs || []).findIndex((run: any) => run.id === event.run.id)
    if (index >= 0) current.value.runs[index] = event.run
    else current.value.runs.push(event.run)
  } else if (event.type === 'progress_message' && event.message && current.value) {
    const index = (current.value.messages || []).findIndex((message: any) => message.id === event.message.id)
    if (index >= 0) current.value.messages[index] = event.message
    else current.value.messages.push(event.message)
    streamingAssistant.value = ''
    executionPlan.value = []
    executionTrace.value = []
    void scrollMessages()
  } else if (event.type === 'plan') {
    executionPlan.value = event.items || []
  } else if (event.type === 'agent_progress') {
    const items = Array.isArray(event.work_items) ? event.work_items : []
    if (items.length) {
      executionPlan.value = items
        .map((item: any) => String(item?.title || item?.name || '').trim())
        .filter(Boolean)
    }
    upsertTraceEvent(event)
  } else if (event.type === 'task_handoff') {
    streamingAssistant.value = ''
    upsertTraceEvent(event)
  } else if (event.type === 'context_compaction') {
    upsertTraceEvent(event)
  } else if (event.type === 'file_operation') {
    upsertTraceEvent(event)
    handleFileOperationEvent(event)
  } else if ([
    'tool_call',
    'skill_activation',
    'skill_resource',
    'sandbox_command',
    'skill_load',
    'skill_call',
    'mcp_call',
    'context_read',
    'agent_progress',
  ].includes(event.type)) {
    upsertTraceEvent(event)
  } else if (event.type === 'question') {
    if (event.question && !context.value.questions.some((question: any) => question.id === event.question.id)) {
      current.value.context.questions.push(event.question)
    }
    activeQuestionId.value = event.question?.id || activeQuestionId.value
    questionDockOpen.value = true
  } else if (event.type === 'token') {
    streamingAssistant.value += event.content || ''
    void scrollMessages()
  } else if (event.type === 'done') {
    streamCompleted.value = true
    streamingAssistant.value = ''
    if (current.value && event.context) current.value.context = event.context
    if (current.value && event.assistant_message) {
      const index = (current.value.messages || []).findIndex((message: any) => message.id === event.assistant_message.id)
      if (index >= 0) current.value.messages[index] = event.assistant_message
      else current.value.messages.push(event.assistant_message)
    }
    if (current.value && event.run) {
      const index = (current.value.runs || []).findIndex((run: any) => run.id === event.run.id)
      if (index >= 0) current.value.runs[index] = event.run
      else current.value.runs.push(event.run)
    }
  } else if (event.type === 'error') {
    streamCompleted.value = true
    streamingAssistant.value = ''
    if (current.value && event.assistant_message) {
      const index = (current.value.messages || []).findIndex((message: any) => message.id === event.assistant_message.id)
      if (index >= 0) current.value.messages[index] = event.assistant_message
      else current.value.messages.push(event.assistant_message)
    }
    if (current.value && event.run) {
      const index = (current.value.runs || []).findIndex((run: any) => run.id === event.run.id)
      if (index >= 0) current.value.runs[index] = event.run
      else current.value.runs.push(event.run)
    }
    throw new Error(event.message || 'AI stream failed')
  }
}

function messageActivityEvents(message: any) {
  if (['progress', 'final', 'error'].includes(String(message?.kind || ''))) {
    return Array.isArray(message?.activity_events) ? message.activity_events : []
  }
  return runById.value[message?.run_id]?.events || []
}

function messageActivityPlan(message: any) {
  const workItems = message?.progress?.work_items
  if (Array.isArray(workItems) && workItems.length) {
    return workItems
      .map((item: any) => String(item?.title || item?.name || '').trim())
      .filter(Boolean)
  }
  return runById.value[message?.run_id]?.plan || []
}

function isCurrentStreamTarget(target: StreamTarget) {
  return current.value?.id === target.businessId && activeChatSessionId.value === target.sessionId
}

async function retryPendingResume() {
  const retry = pendingResume.value
  if (!retry || !isCurrentStreamTarget(retry) || isBusy.value) return
  await resumeAgent(retry.runId, retry)
}

function upsertTraceEvent(event: any) {
  const index = executionTrace.value.findIndex((item: any) => item.call_id && item.call_id === event.call_id)
  if (index >= 0) executionTrace.value[index] = event
  else executionTrace.value.push(event)
}

function handleMessagesScroll() {
  const container = messagesContainer.value
  if (!container) return
  autoFollowMessages.value = container.scrollHeight - container.scrollTop - container.clientHeight < 96
}

async function scrollMessages(force = false) {
  if (!force && !autoFollowMessages.value) return
  await nextTick()
  const container = messagesContainer.value
  if (container) {
    container.scrollTop = container.scrollHeight
    autoFollowMessages.value = true
  }
}

function startConfirm(question: any) {
  activeQuestionId.value = question.id
  questionDockOpen.value = true
}

function openQuestionDock() {
  if (!openQuestions.value.length) return
  activeQuestionId.value = openQuestions.value[0].id
  questionDockOpen.value = true
}

async function submitConfirmation(payload: { question: any; answer: string }) {
  if (!current.value || !payload.answer.trim() || isSubmittingAnswer.value) return
  const target: StreamTarget = {
    businessId: current.value.id,
    sessionId: activeChatSessionId.value,
  }
  isSubmittingAnswer.value = true
  const answeredId = payload.question.id
  let resumeRunId = payload.question.run_id as string | undefined
  let shouldResume = false
  try {
    const response = await http.post(`/businesses/${target.businessId}/confirmations`, {
      question_id: answeredId,
      session_id: target.sessionId || undefined,
      answer: payload.answer.trim(),
      accepted: true,
    })
    if (!isCurrentStreamTarget(target)) return
    current.value.context = response.data.context
    resumeRunId = response.data.resume?.run_id || resumeRunId
    const remaining = openQuestions.value
    const resumeReady = response.data.resume?.ready === true
      || (response.data.resume == null && !remaining.length)
    if (resumeReady) {
      questionDockOpen.value = false
      shouldResume = true
    } else {
      const sameRunQuestions = resumeRunId
        ? remaining.filter((question: any) => question.run_id === resumeRunId)
        : remaining
      const nextQuestion = sameRunQuestions[0] || remaining[0]
      if (nextQuestion) {
        activeQuestionId.value = nextQuestion.id
        questionDockOpen.value = true
      }
    }
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || error?.message || 'Answer submission failed')
  } finally {
    isSubmittingAnswer.value = false
  }
  if (shouldResume) await resumeAgent(resumeRunId, target)
}

async function setActiveModel() {
  await updateSettings({ active_model: selectedModel.value })
}

async function updateSettings(payload: Record<string, any>) {
  settings.value = (await http.patch('/settings', payload)).data
  selectedModel.value = settings.value.active_model
}

async function copyMessage(content: string) {
  await navigator.clipboard.writeText(content)
  ElMessage.success(t('copied'))
}

async function renderMermaid(code: string) {
  await nextTick()
  try {
    const id = `mmd-${Date.now()}`
    const { svg } = await mermaid.render(id, code)
    renderedMermaid.value = svg
  } catch (error: any) {
    renderedMermaid.value = `<pre>${String(error?.message || error)}</pre>`
  }
}

function nodeIcon(node: WorkspaceNode) {
  const icon = node.icon || (node.kind === 'folder' ? 'folder' : 'file')
  const map: Record<string, any> = {
    audio: Headset,
    brain: Cpu,
    database: DataLine,
    file: Document,
    folder: Folder,
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
  return map[icon] || Document
}

function statusLabel(status: string) {
  const map: Record<string, string> = {
    created: t('statusCreated'),
    files_uploaded: t('statusFilesUploaded'),
    analyzed: t('statusAnalyzed'),
    confirmed: t('statusConfirmed'),
    outputs_generated: t('statusOutputsGenerated'),
  }
  return map[status] || status
}


function emptyContext() {
  return {
    user_requirements: [],
    source_files: [],
    entities: [],
    relations: [],
    rules: [],
    evidence: [],
    assumptions: [],
    questions: [],
    confirmations: [],
    skill_references: [],
    mcp_references: [],
    tool_usages: [],
  }
}
</script>

<style scoped lang="scss" src="@/styles/studio-view.scss"></style>
