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

    <aside class="explorer">
      <header class="pane-title">
        <span>{{ t('businessExplorer') }}</span>
        <el-button
          :icon="Plus"
          text
          circle
          :title="t('newScene')"
          :aria-label="t('newScene')"
          :disabled="isBusy"
          @click="createOpen = true"
        />
      </header>

      <section class="scene-list">
        <article
          v-for="item in businesses"
          :key="item.id"
          class="scene-item"
          :class="{ active: item.id === current?.id }"
        >
          <button class="scene-button" :disabled="isBusy" @click="selectBusiness(item.id)">
            <strong>{{ item.name }}</strong>
            <span>
              v{{ item.current_version }} / {{ item.file_count }} {{ t('files') }} /
              {{ item.open_question_count }} {{ t('questions') }}
            </span>
          </button>
          <button
            class="scene-delete"
            :title="t('deleteScene')"
            :aria-label="t('deleteScene')"
            :disabled="isBusy"
            @click="deleteBusiness(item.id, item.name)"
          >
            <el-icon><Delete /></el-icon>
          </button>
        </article>
        <button v-if="!businesses.length" class="scene-button empty" :disabled="isBusy" @click="createOpen = true">
          <strong>{{ t('newBusinessScene') }}</strong>
          <span>workspace / description.md</span>
        </button>
      </section>

      <section v-if="current" class="workspace-strip">
        <button class="workspace-root" @click="openOverview">
          <el-icon><FolderOpened /></el-icon>
          <span>{{ current.name }}</span>
        </button>
        <div class="workspace-actions">
          <button class="mini-action" :title="t('uploadData')" :aria-label="t('uploadData')" :disabled="isBusy" @click="triggerUpload">
            <el-icon><Upload /></el-icon>
          </button>
          <button
            class="mini-action"
            :title="t('refreshWorkspace')"
            :aria-label="t('refreshWorkspace')"
            :disabled="isBusy"
            @click="refreshWorkspace"
          >
            <el-icon><Refresh /></el-icon>
          </button>
          <input ref="fileInput" class="file-input" type="file" multiple @change="uploadFiles" />
        </div>
      </section>

      <section v-if="current" class="resource-tree" aria-label="workspace tree">
        <button
          v-for="row in treeRows"
          :key="row.node.path || current.id"
          class="tree-row"
          :class="{ active: isActiveNode(row.node), folder: row.node.kind === 'folder' }"
          :style="{ paddingLeft: `${8 + row.depth * 14}px` }"
          @click="openWorkspaceNode(row.node)"
          @contextmenu.prevent="openWorkspaceContextMenu(row.node, $event)"
        >
          <span class="chevron" :class="{ open: isExpanded(row.node) }">
            <el-icon v-if="row.node.kind === 'folder'"><ArrowRight /></el-icon>
          </span>
          <el-icon class="tree-icon"><component :is="nodeIcon(row.node)" /></el-icon>
          <span class="tree-label" :title="row.node.name">{{ row.node.name }}</span>
        </button>
      </section>
      <div
        v-if="workspaceContextMenu"
        ref="workspaceContextMenuElement"
        class="workspace-context-menu"
        :style="{ left: `${workspaceContextMenu.x}px`, top: `${workspaceContextMenu.y}px` }"
        role="menu"
        :aria-label="workspaceContextMenu.node.name"
        @pointerdown.stop
      >
        <button
          class="context-menu-delete"
          role="menuitem"
          :disabled="isBusy"
          @click="deleteWorkspacePath(workspaceContextMenu.node)"
        >
          <el-icon><Delete /></el-icon>
          <span>{{ t('deleteFile') }}</span>
        </button>
      </div>
    </aside>

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
            <header class="section-head">
              <div>
                <h1>{{ activeTab.title }}</h1>
                <p>{{ activeTab.payload?.path || activeTab.payload?.file?.summary }}</p>
              </div>
              <div class="toolbar-actions">
                <el-tag v-if="activeTab.payload?.live_operation" class="live-file-tag" type="warning" effect="plain">
                  <el-icon class="spin"><Loading /></el-icon>
                  {{ fileOperationLabel(activeTab.payload.live_operation) }}
                </el-tag>
                <el-tag :type="activeTab.payload?.error ? 'danger' : activeTab.payload?.loading ? 'info' : 'success'">
                  {{ activeTab.payload?.loading ? t('filePreviewLoading') : activeTab.payload?.kind || t('file') }}
                </el-tag>
                <el-tag v-if="activeTab.payload?.truncated" type="warning">{{ t('filePreviewTruncated') }}</el-tag>
                <span v-if="activeTab.payload?.size != null" class="file-size">{{ formatFileSize(activeTab.payload.size) }}</span>
                <el-button
                  v-if="activeTab.payload?.download_url"
                  :icon="Download"
                  @click="downloadWorkspaceFile(activeTab.payload.download_url)"
                >
                  {{ t('download') }}
                </el-button>
                <el-button :icon="Refresh" :disabled="activeTab.payload?.loading" @click="reloadWorkspacePreview">
                  {{ t('refresh') }}
                </el-button>
                <el-button
                  v-if="activeTab.payload?.path"
                  :icon="Delete"
                  type="danger"
                  plain
                  :disabled="isBusy"
                  @click="deleteWorkspacePath({ name: activeTab.title, path: activeTab.payload.path, kind: 'file' })"
                >
                  {{ t('deleteFile') }}
                </el-button>
              </div>
            </header>
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
  ArrowRight,
  Box,
  Bottom,
  ChatDotRound,
  CircleCheck,
  CopyDocument,
  Cpu,
  DataLine,
  Delete,
  Document,
  Download,
  Files,
  Folder,
  FolderOpened,
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

type Language = 'zh' | 'en'
type ThemeMode = 'dark' | 'light' | 'contrast'
type TabKind = 'description' | 'overview' | 'context' | 'file' | 'graph' | 'thinking' | 'outputs' | 'capabilities' | 'settings'
type Tab = { id: string; title: string; kind: TabKind; payload?: any }
type WorkspaceNode = { name: string; path: string; kind: 'file' | 'folder'; icon?: string; children?: WorkspaceNode[] }
type TreeRow = { node: WorkspaceNode; depth: number }
type WorkspaceContextMenu = { node: WorkspaceNode; x: number; y: number }
type PaneKind = 'explorer' | 'assistant'
type StreamTarget = { businessId: string; sessionId: string }
type PendingResume = StreamTarget & { runId?: string; error: string }
type MentionTrigger = { start: number; end: number; query: string }
type LiveFileDraft = {
  callId: string
  path: string
  operation: string
  text: string
  baseText: string
  oldText: string
  replacementText: string
  revealPromise?: Promise<void>
  cancelReveal?: () => void
}

const MENTION_RESULT_LIMIT = 30

const copy: Record<Language, Record<string, string>> = {
  zh: {
    activityBar: '主导航',
    activeFile: '当前文件',
    activeModel: '当前模型',
    activeScene: '当前场景',
    add: '添加',
    aiAssistant: 'AI 助手',
    aiModels: 'AI 模型',
    aiUnderstanding: 'AI 理解',
    businessContext: '业务上下文',
    businessExplorer: '业务资源管理器',
    businessWorkspace: '业务工作区',
    cancel: '取消',
    chatEmptyBody: '描述业务目标、补充规则或让 AI 分析当前场景。',
    chatEmptyTitle: '从一个业务问题开始',
    chatPlaceholder: '输入业务问题或补充说明...',
    chatActions: '会话操作',
    chatSessions: '对话会话',
    clearChat: '清空当前会话',
    clearChatConfirm: '只清空当前会话的消息和执行记录，业务上下文与文件不会被删除。',
    close: '关闭',
    configuredModels: '已配置模型',
    confirm: '确认',
    confirmQuestion: '确认问题',
    confirmations: '确认记录',
    copied: '已复制',
    copyExample: '复制示例',
    copyReply: '复制回复',
    create: '创建',
    download: '下载',
    deleteChat: '删除当前会话',
    deleteChatConfirm: '删除当前会话及其消息和执行记录，业务上下文与文件不受影响。',
    entities: '实体',
    evidence: '证据',
    explorer: '资源管理器',
    files: '文件',
    general: '通用',
    generating: '生成中',
    goal: '目标',
    goalPlaceholder: '生成可复用的业务 Skill Package',
    graphs: '图谱',
    language: '语言',
    languageHelp: '切换工作台界面语言，适合不同用户使用。',
    latestPlan: '最新计划',
    latestMessage: '回到最新消息',
    mcpConfigInvalid: 'MCP 配置不是有效 JSON',
    mcpConfigSaved: 'MCP 配置已保存',
    model: '模型',
    modelId: '模型 ID',
    modelName: '名称',
    modelsHelp: '这里管理可选 AI 模型；对话区只显示已启用模型。',
    modified: '已修改',
    name: '名称',
    newBusinessScene: '新建业务场景',
    newChat: '新建会话',
    newScene: '新建场景',
    noAssumptions: '暂无假设。',
    noConfirmations: '暂无确认记录。',
    noModel: '未配置模型',
    noPackage: '暂无生成包。',
    noPreview: '暂无预览文本。',
    noRun: '暂无运行记录。',
    openQuestions: '待确认问题',
    openSettings: '打开设置',
    packages: '生成包',
    plan: '计划',
    questions: '问题',
    refresh: '刷新',
    refreshWorkspace: '刷新工作区',
    resizeAssistant: '调整 AI 对话栏宽度',
    resizeExplorer: '调整资源管理器宽度',
    ready: '就绪',
    relations: '关系',
    reload: '重新加载',
    saveAndAnalyze: '保存并分析',
    saveConfig: '保存配置',
    saved: '已保存',
    scenarioDescription: '场景描述',
    scenarioSaved: 'description.md 已保存',
    sceneNamePlaceholder: '例如：电商客服 Agent',
    send: '发送',
    sendHint: 'Ctrl + Enter 发送',
    settings: '设置',
    settingsDescription: '像编辑器一样集中管理模型、工具、Skill、MCP、语言和主题。',
    skills: '技能',
    statusAnalyzed: '已分析',
    statusConfirmed: '已确认',
    statusCreated: '已创建',
    statusFilesUploaded: '已上传文件',
    statusOutputsGenerated: '已生成输出',
    stop: '停止生成',
    theme: '主题',
    themeContrast: '高对比',
    themeDark: '深色',
    themeHelp: '切换整体工作台视觉主题。',
    themeLight: '浅色',
    toolSkillMcpRefs: 'Tool / Skill / MCP 引用',
    tools: '工具',
    uploadData: '上传资料',
    uploadSuccess: '资料已上传到 data',
    version: '版本',
    workspace: '工作区',
    workspaceFallback: '业务工作区',
    writeContext: '写入 Context',
    you: '你',
  },
  en: {
    activityBar: 'Activity Bar',
    activeFile: 'Active File',
    activeModel: 'Active Model',
    activeScene: 'Active Scene',
    add: 'Add',
    aiAssistant: 'AI Assistant',
    aiModels: 'AI Models',
    aiUnderstanding: 'AI Understanding',
    businessContext: 'Business Context',
    businessExplorer: 'Business Explorer',
    businessWorkspace: 'Business Workspace',
    cancel: 'Cancel',
    chatEmptyBody: 'Describe a goal, add rules, or ask AI to analyze this workspace.',
    chatEmptyTitle: 'Start with a business question',
    chatPlaceholder: 'Ask a business question or add context...',
    chatActions: 'Chat actions',
    chatSessions: 'Chat sessions',
    clearChat: 'Clear current chat',
    clearChatConfirm: 'This only clears messages and runs in the current chat. Business context and files are kept.',
    close: 'Close',
    configuredModels: 'Configured Models',
    confirm: 'Confirm',
    confirmQuestion: 'Confirm Question',
    confirmations: 'Confirmations',
    copied: 'Copied',
    copyExample: 'Copy Example',
    copyReply: 'Copy response',
    create: 'Create',
    download: 'Download',
    deleteChat: 'Delete current chat',
    deleteChatConfirm: 'This deletes the current chat and its runs. Business context and files are kept.',
    entities: 'Entities',
    evidence: 'Evidence',
    explorer: 'Explorer',
    files: 'files',
    general: 'General',
    generating: 'Generating',
    goal: 'Goal',
    goalPlaceholder: 'Generate a reusable business Skill Package',
    graphs: 'Graphs',
    language: 'Language',
    languageHelp: 'Switch the workbench language for different users.',
    latestPlan: 'Latest Plan',
    latestMessage: 'Jump to latest message',
    mcpConfigInvalid: 'MCP config is not valid JSON',
    mcpConfigSaved: 'MCP config saved',
    model: 'Model',
    modelId: 'Model ID',
    modelName: 'Name',
    modelsHelp: 'Manage selectable AI models here. The chat only shows enabled models.',
    modified: 'modified',
    name: 'Name',
    newBusinessScene: 'New Business Scene',
    newChat: 'New Chat',
    newScene: 'New Scene',
    noAssumptions: 'No assumptions yet.',
    noConfirmations: 'No confirmations yet.',
    noModel: 'No model',
    noPackage: 'No package yet.',
    noPreview: 'No preview text.',
    noRun: 'No run yet.',
    openQuestions: 'Open Questions',
    openSettings: 'Open Settings',
    packages: 'packages',
    plan: 'Plan',
    questions: 'questions',
    refresh: 'Refresh',
    refreshWorkspace: 'Refresh Workspace',
    resizeAssistant: 'Resize AI chat panel',
    resizeExplorer: 'Resize explorer panel',
    ready: 'Ready',
    relations: 'Relations',
    reload: 'Reload',
    saveAndAnalyze: 'Save and Analyze',
    saveConfig: 'Save Config',
    saved: 'saved',
    scenarioDescription: 'Scenario Description',
    scenarioSaved: 'description.md saved',
    sceneNamePlaceholder: 'For example: Ecommerce Support Agent',
    send: 'Send',
    sendHint: 'Ctrl + Enter to send',
    settings: 'Settings',
    settingsDescription: 'Manage models, tools, skills, MCP, language, and theme in one editor-style place.',
    skills: 'Skills',
    statusAnalyzed: 'analyzed',
    statusConfirmed: 'confirmed',
    statusCreated: 'created',
    statusFilesUploaded: 'files uploaded',
    statusOutputsGenerated: 'outputs generated',
    stop: 'Stop generating',
    theme: 'Theme',
    themeContrast: 'High Contrast',
    themeDark: 'Dark',
    themeHelp: 'Switch the entire workbench visual theme.',
    themeLight: 'Light',
    toolSkillMcpRefs: 'Tool / Skill / MCP references',
    tools: 'Tools',
    uploadData: 'Upload Data',
    uploadSuccess: 'Uploaded to data',
    version: 'Version',
    workspace: 'Workspace',
    workspaceFallback: 'Business Workspace',
    writeContext: 'Write to Context',
    you: 'You',
  },
}

Object.assign(copy.zh, {
  chatPlaceholder: '输入业务问题，使用 @ 引用场景文件...',
  deleteFile: '删除文件',
  deleteFileConfirmBody: '文件将从当前业务场景永久删除。依赖它的分析结果需要重新运行后才能继续信任。',
  deleteFileConfirmTitle: '删除这个文件？',
  deleteScene: '删除场景',
  deleteSceneConfirmBody: '这会连同该业务场景下的 description.md、data、graphs、context、output 和 settings 一起删除。',
  deleteSceneConfirmTitle: '删除业务场景？',
  deleted: '已删除',
  filePreviewLimited: '表格预览仅显示前 20 行，避免大数据文件卡顿。',
  filePreviewLoading: '加载中',
  filePreviewTruncated: '有界预览',
  mentionFile: '引用场景文件',
  mentionFileEmpty: '没有找到匹配的文件',
  mentionFileHint: '↑↓ 选择 · Enter 确认 · Esc 关闭',
  mentionFileRemove: '移除文件引用',
  mentionFileResults: '个匹配文件',
  needUserInput: '需要你确认',
  referencedFilesPrompt: '引用的业务场景文件：',
  resumeFailed: 'AI 续跑未完成，可从暂停点继续。',
  retryContinue: '继续执行',
  fileOperationCreate: '正在创建',
  fileOperationEdit: '正在编辑',
  fileOperationMove: '正在移动',
  fileOperationDelete: '正在删除',
  fileOperationManage: '正在更新',
})

Object.assign(copy.en, {
  chatPlaceholder: 'Ask a business question, or use @ to reference a scene file...',
  deleteFile: 'Delete File',
  deleteFileConfirmBody: 'This permanently removes the file from the business scene. Re-run any analysis that depends on it.',
  deleteFileConfirmTitle: 'Delete this file?',
  deleteScene: 'Delete Scene',
  deleteSceneConfirmBody: 'This deletes description.md, data, graphs, context, output, and settings in this scene.',
  deleteSceneConfirmTitle: 'Delete business scene?',
  deleted: 'Deleted',
  filePreviewLimited: 'Table previews show the first 20 rows only to keep large files responsive.',
  filePreviewLoading: 'Loading',
  filePreviewTruncated: 'Bounded preview',
  mentionFile: 'Reference scene file',
  mentionFileEmpty: 'No matching files',
  mentionFileHint: '↑↓ Select · Enter Confirm · Esc Close',
  mentionFileRemove: 'Remove file reference',
  mentionFileResults: 'matching files',
  needUserInput: 'Input Needed',
  referencedFilesPrompt: 'Referenced business scene files:',
  resumeFailed: 'AI continuation did not finish. Continue from the paused step.',
  retryContinue: 'Continue',
  fileOperationCreate: 'Creating',
  fileOperationEdit: 'Editing',
  fileOperationMove: 'Moving',
  fileOperationDelete: 'Deleting',
  fileOperationManage: 'Updating',
})

const storedLanguage = localStorage.getItem('studio.language')
const storedTheme = localStorage.getItem('studio.theme')
const uiLanguage = ref<Language>(storedLanguage === 'en' ? 'en' : 'zh')
const themeMode = ref<ThemeMode>(storedTheme === 'light' || storedTheme === 'contrast' ? storedTheme : 'dark')
const WorkspaceFilePreview = defineAsyncComponent(() => import('@/components/WorkspaceFilePreview.vue'))

const businesses = ref<any[]>([])
const current = ref<any | null>(null)
const workspaceTree = ref<WorkspaceNode | null>(null)
const tools = ref<any[]>([])
const skills = ref<any[]>([])
const settings = ref<any>({ active_model: '', configured_models: [], installed_tools: [], installed_skills: [], mcp_configs: [] })
const tabs = ref<Tab[]>([])
const activeTabId = ref('')
const renderedMermaid = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const workspaceContextMenuElement = ref<HTMLElement | null>(null)
const workspaceContextMenu = ref<WorkspaceContextMenu | null>(null)
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
const expandedFolders = ref<Set<string>>(new Set(['context', 'data', 'graphs', 'output', 'output/skill-package', 'settings']))
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
const explorerWidth = ref(clamp(Number(localStorage.getItem('studio.explorerWidth')) || 286, 220, 420))
const assistantWidth = ref(clamp(Number(localStorage.getItem('studio.assistantWidth')) || 500, 360, 720))
let paneDrag: { kind: PaneKind; startX: number; startWidth: number } | null = null
let chatBoxObserver: ResizeObserver | null = null
let workspaceReloadTimer: number | undefined
const liveFileDrafts = new Map<string, LiveFileDraft>()
const pendingFileSettlements = new Set<Promise<void>>()

const activeTab = computed(() => tabs.value.find((tab) => tab.id === activeTabId.value))
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
const studioLayoutStyle = computed(() => ({
  '--explorer-width': `${explorerWidth.value}px`,
  '--assistant-width': `${assistantWidth.value}px`,
}))
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
const treeRows = computed(() => {
  if (!workspaceTree.value?.children) return []
  return workspaceTree.value.children.flatMap((node) => flattenNode(node, 0))
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
  window.addEventListener('pointerdown', handleWorkspaceContextPointerDown)
  window.addEventListener('keydown', handleWorkspaceContextKeydown)
  window.addEventListener('resize', closeWorkspaceContextMenu)
  window.addEventListener('scroll', closeWorkspaceContextMenu, true)
  await Promise.all([loadSettings(), loadCapabilities(), loadBusinesses()])
  if (businesses.value.length) await selectBusiness(businesses.value[0].id)
})

onBeforeUnmount(() => {
  stopPaneResize()
  chatBoxObserver?.disconnect()
  streamAbortController.value?.abort()
  if (workspaceReloadTimer != null) window.clearTimeout(workspaceReloadTimer)
  liveFileDrafts.clear()
  pendingFileSettlements.clear()
  window.removeEventListener('pointerdown', handleWorkspaceContextPointerDown)
  window.removeEventListener('keydown', handleWorkspaceContextKeydown)
  window.removeEventListener('resize', closeWorkspaceContextMenu)
  window.removeEventListener('scroll', closeWorkspaceContextMenu, true)
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

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum))
}

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
  if (window.innerWidth < 1180 || activeTab.value?.kind === 'settings') return
  event.preventDefault()
  paneDrag = {
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
  if (!paneDrag) return
  const delta = event.clientX - paneDrag.startX
  setPaneWidth(
    paneDrag.kind,
    paneDrag.startWidth + (paneDrag.kind === 'explorer' ? delta : -delta),
  )
}

function stopPaneResize() {
  if (!paneDrag) return
  localStorage.setItem('studio.explorerWidth', String(Math.round(explorerWidth.value)))
  localStorage.setItem('studio.assistantWidth', String(Math.round(assistantWidth.value)))
  paneDrag = null
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
  localStorage.setItem(`studio.${kind}Width`, String(Math.round(kind === 'explorer' ? explorerWidth.value : assistantWidth.value)))
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
  liveFileDrafts.clear()
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

async function deleteWorkspacePath(node: Pick<WorkspaceNode, 'name' | 'path' | 'kind'>) {
  if (!current.value || !node.path || node.kind !== 'file' || isBusy.value) return
  closeWorkspaceContextMenu()
  try {
    await ElMessageBox.confirm(t('deleteFileConfirmBody'), `${t('deleteFileConfirmTitle')} ${node.name}`, {
      confirmButtonText: t('deleteFile'),
      cancelButtonText: t('cancel'),
      type: 'warning',
    })
  } catch {
    return
  }
  const res = await http.delete(`/businesses/${current.value.id}/workspace/file`, {
    params: { path: node.path },
  })
  if (res.data.business) current.value = res.data.business
  closeTab(node.path)
  if (node.path === 'description.md') {
    descriptionContent.value = ''
    descriptionDirty.value = false
  }
  await refreshWorkspace()
  ElMessage.success(t('deleted'))
}

function openWorkspaceContextMenu(node: WorkspaceNode, event: MouseEvent) {
  if (node.kind !== 'file' || isBusy.value) {
    closeWorkspaceContextMenu()
    return
  }
  const menuWidth = 176
  const menuHeight = 44
  const margin = 8
  workspaceContextMenu.value = {
    node,
    x: clamp(event.clientX, margin, window.innerWidth - menuWidth - margin),
    y: clamp(event.clientY, margin, window.innerHeight - menuHeight - margin),
  }
}

function closeWorkspaceContextMenu() {
  workspaceContextMenu.value = null
}

function handleWorkspaceContextPointerDown(event: PointerEvent) {
  const target = event.target as Node | null
  if (!target || !workspaceContextMenuElement.value?.contains(target)) closeWorkspaceContextMenu()
}

function handleWorkspaceContextKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') closeWorkspaceContextMenu()
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
  if (node.kind === 'folder') {
    toggleFolder(node)
    return
  }
  if (node.path === 'description.md') return openDescription()
  if (node.path === 'context/business_context.json') return openContext()
  if (node.kind === 'file') return openWorkspaceFile(node)
  if (!node.path) return openOverview()
}

async function openWorkspaceFile(node: Pick<WorkspaceNode, 'name' | 'path'>) {
  if (!current.value || !node.path) return
  const liveDraft = liveFileDrafts.get(node.path)
  if (liveDraft) {
    renderLiveFileDraft(liveDraft, 'streaming', true)
    return
  }
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

function downloadWorkspaceFile(url: string) {
  window.open(url, '_blank', 'noopener,noreferrer')
}

function formatFileSize(value: number) {
  if (!Number.isFinite(value) || value < 0) return ''
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = value / 1024
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size >= 10 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`
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

function handleFileOperationEvent(event: any) {
  if (!current.value || !event?.mutating) return
  const operation = String(event.operation || 'manage')
  const path = workspaceRelativePath(event.path)
  const destination = workspaceRelativePath(event.destination)
  if (!path) return

  if (event.status === 'streaming' && ['create', 'edit'].includes(operation)) {
    applyStreamedFileDelta(event, path, operation)
    return
  }

  if (event.status === 'running' && ['create', 'edit'].includes(operation)) {
    applyRunningFileOperation(event, path, operation)
    return
  }

  if (event.status === 'failed') {
    const draft = liveFileDrafts.get(path)
    if (draft && event.call_id && draft.callId && draft.callId !== String(event.call_id)) return
    draft?.cancelReveal?.()
    liveFileDrafts.delete(path)
    const tab = tabs.value.find((item) => item.id === path)
    if (tab?.kind === 'file') {
      tab.payload = {
        ...tab.payload,
        loading: false,
        live_operation: '',
        error: event.error || t('noPreview'),
      }
    }
    return
  }
  if (event.status !== 'succeeded') return

  if (operation === 'delete') {
    liveFileDrafts.delete(path)
    closeTab(path)
    trackFileSettlement(reloadWorkspaceTree())
    return
  }
  if (operation === 'move') {
    liveFileDrafts.delete(path)
    closeTab(path)
    trackFileSettlement((async () => {
      await reloadWorkspaceTree()
      if (destination) await openWorkspaceFile({ name: destination.split('/').pop() || destination, path: destination })
    })())
    return
  }
  if (operation === 'create_directory') {
    trackFileSettlement(reloadWorkspaceTree())
    return
  }
  const draft = liveFileDrafts.get(path)
  if (event.auto_open === false && !draft) {
    scheduleWorkspaceTreeReload()
    return
  }
  trackFileSettlement(finalizeLiveFile(path, String(event.call_id || '')))
}

function applyStreamedFileDelta(event: any, path: string, operation: string) {
  const callId = String(event.call_id || '')
  let draft = liveFileDrafts.get(path)
  if (!draft || (callId && draft.callId && draft.callId !== callId)) {
    const existingText = String(tabs.value.find((tab) => tab.id === path)?.payload?.text || '')
    draft = {
      callId,
      path,
      operation,
      text: operation === 'edit' ? existingText : '',
      baseText: existingText,
      oldText: '',
      replacementText: '',
    }
    liveFileDrafts.set(path, draft)
  }
  if (callId) draft.callId = callId
  if (operation === 'create') {
    if (event.content_reset) draft.text = ''
    draft.text += String(event.content_delta || '')
  } else {
    if (event.old_text) draft.oldText = String(event.old_text)
    if (event.content_reset) draft.replacementText = ''
    draft.replacementText += String(event.content_delta || '')
    updateEditedDraftText(draft)
    if (!draft.baseText) void hydrateLiveEditDraft(path, draft.callId)
  }
  renderLiveFileDraft(draft, 'streaming', true)
}

function applyRunningFileOperation(event: any, path: string, operation: string) {
  const callId = String(event.call_id || '')
  const existingText = String(tabs.value.find((tab) => tab.id === path)?.payload?.text || '')
  let draft = liveFileDrafts.get(path)
  if (!draft || (callId && draft.callId && draft.callId !== callId)) {
    draft = {
      callId,
      path,
      operation,
      text: operation === 'edit' ? existingText : '',
      baseText: existingText,
      oldText: '',
      replacementText: '',
    }
    liveFileDrafts.set(path, draft)
  }
  if (callId) draft.callId = callId
  const input = event.input || {}
  if (operation === 'create' && typeof input.content === 'string') {
    if (!draft.text && input.content.length > 240) startLiveContentReveal(draft, input.content)
    else draft.text = input.content
  } else if (operation === 'edit') {
    if (typeof input.old_string === 'string') draft.oldText = input.old_string
    if (typeof input.new_string === 'string') draft.replacementText = input.new_string
    updateEditedDraftText(draft)
    if (!draft.baseText) void hydrateLiveEditDraft(path, draft.callId)
  }
  renderLiveFileDraft(draft, 'streaming', true)
}

function startLiveContentReveal(draft: LiveFileDraft, targetText: string) {
  draft.cancelReveal?.()
  const callId = draft.callId
  const steps = Math.min(42, Math.max(18, Math.ceil(targetText.length / 180)))
  const chunkSize = Math.max(1, Math.ceil(targetText.length / steps))
  let cursor = 0
  let resolveReveal: () => void = () => undefined
  draft.text = ''
  draft.revealPromise = new Promise<void>((resolve) => { resolveReveal = resolve })
  const timer = window.setInterval(() => {
    const current = liveFileDrafts.get(draft.path)
    if (current !== draft || (callId && current.callId !== callId)) {
      window.clearInterval(timer)
      resolveReveal()
      return
    }
    cursor = Math.min(targetText.length, cursor + chunkSize)
    draft.text = targetText.slice(0, cursor)
    renderLiveFileDraft(draft, 'streaming', false)
    if (cursor >= targetText.length) {
      window.clearInterval(timer)
      draft.cancelReveal = undefined
      resolveReveal()
    }
  }, 28)
  draft.cancelReveal = () => {
    window.clearInterval(timer)
    draft.text = targetText
    draft.cancelReveal = undefined
    resolveReveal()
  }
}

function updateEditedDraftText(draft: LiveFileDraft) {
  if (draft.baseText && draft.oldText && draft.baseText.includes(draft.oldText)) {
    draft.text = draft.baseText.replace(draft.oldText, draft.replacementText)
  } else if (draft.replacementText) {
    draft.text = draft.replacementText
  }
}

async function hydrateLiveEditDraft(path: string, callId: string) {
  if (!current.value) return
  try {
    const payload = await fetchWorkspacePreview(path)
    const draft = liveFileDrafts.get(path)
    if (!draft || (callId && draft.callId !== callId)) return
    draft.baseText = String(payload.text || '')
    updateEditedDraftText(draft)
    renderLiveFileDraft(draft, 'streaming', false, payload)
  } catch {
    // The runtime completion event will retry once the file exists on disk.
  }
}

function renderLiveFileDraft(
  draft: LiveFileDraft,
  phase: 'streaming' | 'saving',
  activate: boolean,
  sourcePayload: any = {},
) {
  const title = draft.path.split('/').pop() || draft.path
  const existing = tabs.value.find((tab) => tab.id === draft.path)?.payload || {}
  setFileTab({
    id: draft.path,
    title,
    kind: 'file',
    payload: {
      ...existing,
      ...sourcePayload,
      path: draft.path,
      filename: title,
      kind: previewKindForPath(draft.path),
      text: draft.text,
      size: draft.text.length,
      loading: false,
      error: '',
      live_operation: draft.operation,
      live_phase: phase,
    },
  }, activate)
}

async function finalizeLiveFile(path: string, callId: string) {
  try {
    const activeDraft = liveFileDrafts.get(path)
    if (activeDraft?.revealPromise) await activeDraft.revealPromise
    if (activeDraft) renderLiveFileDraft(activeDraft, 'saving', false)
    await reloadWorkspaceTree()
    const payload = await fetchWorkspacePreview(path)
    const draft = liveFileDrafts.get(path)
    if (draft && callId && draft.callId && draft.callId !== callId) return
    liveFileDrafts.delete(path)
    const title = path.split('/').pop() || path
    setFileTab({ id: path, title, kind: 'file', payload }, false)
  } catch (error: any) {
    const draft = liveFileDrafts.get(path)
    if (draft && callId && draft.callId && draft.callId !== callId) return
    liveFileDrafts.delete(path)
    const tab = tabs.value.find((item) => item.id === path)
    if (tab?.kind === 'file') {
      tab.payload = {
        ...tab.payload,
        loading: false,
        live_operation: '',
        live_phase: '',
        error: error?.response?.data?.detail || error?.message || t('noPreview'),
      }
    }
  }
}

async function settleLiveFileOperations() {
  if (pendingFileSettlements.size) {
    await Promise.allSettled([...pendingFileSettlements])
  }
  const remaining = [...liveFileDrafts.values()]
  if (!remaining.length) return
  await Promise.allSettled(remaining.map((draft) => finalizeLiveFile(draft.path, draft.callId)))
}

function trackFileSettlement(task: Promise<unknown>) {
  const tracked = Promise.resolve(task).then(() => undefined, () => undefined)
  pendingFileSettlements.add(tracked)
  void tracked.finally(() => pendingFileSettlements.delete(tracked))
}

function scheduleWorkspaceTreeReload() {
  if (workspaceReloadTimer != null) window.clearTimeout(workspaceReloadTimer)
  workspaceReloadTimer = window.setTimeout(() => {
    workspaceReloadTimer = undefined
    void reloadWorkspaceTree()
  }, 120)
}

async function fetchWorkspacePreview(path: string) {
  if (!current.value) throw new Error(t('noPreview'))
  return (await http.get(`/businesses/${current.value.id}/workspace/preview`, {
    params: { path },
  })).data
}

function setFileTab(tab: Tab, activate: boolean) {
  const index = tabs.value.findIndex((item) => item.id === tab.id)
  if (index >= 0) tabs.value[index] = tab
  else tabs.value.push(tab)
  if (activate) activeTabId.value = tab.id
}

function previewKindForPath(path: string) {
  const suffix = path.split('.').pop()?.toLowerCase()
  if (suffix === 'json') return 'json'
  if (suffix === 'md') return 'markdown'
  if (suffix === 'mmd' || suffix === 'mermaid') return 'mermaid'
  return 'text'
}

function workspaceRelativePath(value: unknown) {
  const normalized = String(value || '').trim().replace(/\\/g, '/')
  if (!normalized || normalized === '/workspace' || normalized.startsWith('/skills') || normalized.startsWith('/tmp')) return ''
  return normalized.replace(/^\/workspace\/?/, '').replace(/^\/+/, '')
}

function fileOperationLabel(operation: string) {
  const labels: Record<string, string> = {
    create: t('fileOperationCreate'),
    edit: t('fileOperationEdit'),
    move: t('fileOperationMove'),
    delete: t('fileOperationDelete'),
  }
  return labels[operation] || t('fileOperationManage')
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

function flattenNode(node: WorkspaceNode, depth: number): TreeRow[] {
  const rows: TreeRow[] = [{ node, depth }]
  if (node.kind === 'folder' && !isExpanded(node)) return rows
  for (const child of node.children || []) rows.push(...flattenNode(child, depth + 1))
  return rows
}

function toggleFolder(node: WorkspaceNode) {
  const key = node.path || node.name
  const next = new Set(expandedFolders.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  expandedFolders.value = next
}

function isExpanded(node: WorkspaceNode) {
  return expandedFolders.value.has(node.path || node.name)
}

function nodeIcon(node: WorkspaceNode) {
  const icon = node.icon || (node.kind === 'folder' ? 'folder' : 'file')
  const map: Record<string, any> = {
    audio: Headset,
    brain: Cpu,
    database: DataLine,
    file: Document,
    folder: node.kind === 'folder' && isExpanded(node) ? FolderOpened : Folder,
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

function isActiveNode(node: WorkspaceNode) {
  return node.path === activeTabId.value || Boolean(node.path && (activeTab.value?.id || '').startsWith(node.path))
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

<style scoped lang="scss">
.studio-shell {
  --activity-width: 52px;
  --explorer-width: 286px;
  --assistant-width: 500px;
  --surface-0: #1e1e1e;
  --surface-1: #181818;
  --surface-2: #252526;
  --surface-3: #2d2d2d;
  --surface-hover: #37373d;
  --surface-code: #111111;
  --border: #333333;
  --border-soft: #2b2b2b;
  --text-strong: #ffffff;
  --text-main: #d4d4d4;
  --text-muted: #9d9d9d;
  --accent: #4aa3ff;
  --accent-strong: #0e639c;
  --accent-soft: rgba(74, 163, 255, 0.16);
  --warning: #d7a642;
  --chat-bg: #1b1c20;
  --chat-header: #202126;
  --chat-divider: #2d2f35;
  --chat-user: #20364a;
  --chat-user-border: #2e506b;
  --chat-avatar: #24384b;
  --chat-composer: #22242a;

  display: grid;
  position: relative;
  grid-template-columns: var(--activity-width) var(--explorer-width) minmax(342px, 1fr) var(--assistant-width);
  grid-template-rows: 34px minmax(0, 1fr) 24px;
  height: 100dvh;
  min-width: 1180px;
  background: var(--surface-0);
  color: var(--text-main);
}

.studio-shell.theme-light {
  --surface-0: #f7f8fb;
  --surface-1: #ffffff;
  --surface-2: #f0f2f6;
  --surface-3: #ffffff;
  --surface-hover: #e7edf7;
  --surface-code: #ffffff;
  --border: #d8dee9;
  --border-soft: #e5e9f0;
  --text-strong: #172033;
  --text-main: #273247;
  --text-muted: #667085;
  --accent: #2563eb;
  --accent-strong: #1d4ed8;
  --accent-soft: rgba(37, 99, 235, 0.12);
  --warning: #a16207;
  --chat-bg: #f8f9fc;
  --chat-header: #ffffff;
  --chat-divider: #e4e8ef;
  --chat-user: #eaf2ff;
  --chat-user-border: #c9dcf7;
  --chat-avatar: #e1edff;
  --chat-composer: #ffffff;
}

.studio-shell.theme-contrast {
  --surface-0: #080808;
  --surface-1: #000000;
  --surface-2: #121212;
  --surface-3: #1b1b1b;
  --surface-hover: #272727;
  --surface-code: #000000;
  --border: #4b5563;
  --border-soft: #374151;
  --text-strong: #ffffff;
  --text-main: #f9fafb;
  --text-muted: #d1d5db;
  --accent: #60a5fa;
  --accent-strong: #1d4ed8;
  --accent-soft: rgba(96, 165, 250, 0.22);
  --warning: #fbbf24;
  --chat-bg: #090a0c;
  --chat-header: #111216;
  --chat-divider: #374151;
  --chat-user: #172c3d;
  --chat-user-border: #4b6f8d;
  --chat-avatar: #172c3d;
  --chat-composer: #15171b;
}

.studio-shell.settings-focus {
  grid-template-columns: var(--activity-width) var(--explorer-width) minmax(720px, 1fr);
}

.studio-shell.settings-focus .editor {
  grid-column: 3 / -1;
}

.studio-shell.settings-focus .right-panel {
  display: none;
}

.studio-shell.settings-focus .pane-resizer {
  display: none;
}

.studio-shell.settings-focus .title-bar {
  grid-template-columns: calc(var(--activity-width) + var(--explorer-width)) minmax(0, 1fr) 120px;
}

.title-bar {
  grid-column: 1 / -1;
  grid-row: 1;
  display: grid;
  grid-template-columns: calc(var(--activity-width) + var(--explorer-width)) minmax(0, 1fr) var(--assistant-width);
  align-items: center;
  height: 34px;
  border-bottom: 1px solid var(--border-soft);
  background: var(--surface-1);
}

.brand-zone,
.title-center,
.title-actions,
.status-bar {
  display: flex;
  align-items: center;
}

.brand-zone {
  gap: 8px;
  padding: 0 13px;
  color: var(--text-strong);
}

.brand-zone strong {
  font-size: 12px;
}

.window-dot {
  position: relative;
  flex: 0 0 37px;
  width: 37px;
  height: 9px;
  background: transparent;
}

.window-dot::before {
  position: absolute;
  inset: 0 auto auto 0;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 14px 0 0 #4ec986, 28px 0 0 var(--warning);
  content: '';
}

.title-center {
  justify-content: center;
  gap: 10px;
  min-width: 0;
  color: var(--text-main);
  font-size: 12px;
}

.title-center span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.title-file,
.muted {
  color: var(--text-muted);
}

.title-actions {
  justify-content: flex-end;
  padding-right: 8px;
}

.activity-bar {
  grid-column: 1;
  grid-row: 2;
  display: flex;
  flex-direction: column;
  align-items: center;
  background: var(--surface-1);
  border-right: 1px solid var(--border-soft);
}

.activity {
  display: grid;
  place-items: center;
  width: 100%;
  height: 48px;
  border: 0;
  border-left: 2px solid transparent;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  transition: background 0.16s ease, color 0.16s ease, border-color 0.16s ease;
}

.activity:hover,
.activity.active {
  color: var(--text-strong);
}

.activity.active {
  border-left-color: var(--accent);
}

.activity:focus-visible,
.scene-button:focus-visible,
.scene-delete:focus-visible,
.tree-row:focus-visible,
.workspace-root:focus-visible,
.mini-action:focus-visible,
.tab:focus-visible,
.tab-close:focus-visible,
.context-chip:focus-visible,
.question-pop:focus-visible,
.dock-toggle:focus-visible,
.composer-tool:focus-visible,
.send-button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.activity:disabled {
  cursor: default;
  opacity: 0.35;
}

.activity.bottom {
  margin-top: auto;
}

.explorer,
.right-panel {
  min-width: 0;
  background: var(--surface-2);
  border-right: 1px solid var(--border);
  overflow: hidden;
}

.explorer {
  grid-column: 2;
  grid-row: 2;
  display: flex;
  min-height: 0;
  flex-direction: column;
}

.pane-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 38px;
  padding: 0 10px;
  border-bottom: 1px solid var(--border);
}

.pane-title span,
.field-label {
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 700;
}

.scene-list {
  max-height: 176px;
  overflow: auto;
  padding: 7px;
  border-bottom: 1px solid var(--border);
}

.scene-item {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 30px;
  gap: 3px;
  align-items: stretch;
  border-radius: 4px;
}

.scene-item + .scene-item {
  margin-top: 2px;
}

.scene-button,
.scene-delete,
.tree-row,
.workspace-root,
.mini-action,
.question-pop {
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
}

.scene-button {
  display: grid;
  gap: 3px;
  width: 100%;
  min-height: 42px;
  padding: 7px 8px;
  border-radius: 4px;
  text-align: left;
}

.scene-delete {
  display: grid;
  place-items: center;
  width: 30px;
  min-height: 42px;
  border-radius: 4px;
  color: var(--text-muted);
  opacity: 0;
  transition: opacity 0.16s ease, background 0.16s ease, color 0.16s ease;
}

.scene-button:hover,
.scene-item.active .scene-button,
.scene-item:hover .scene-button,
.scene-delete:hover,
.tree-row:hover,
.tree-row.active,
.workspace-root:hover,
.mini-action:hover,
.question-pop:hover {
  background: var(--surface-hover);
}

.scene-item:hover .scene-delete,
.scene-delete:focus-visible {
  opacity: 1;
}

.scene-delete:hover {
  color: var(--el-color-danger);
}

.scene-button:disabled,
.scene-delete:disabled,
.mini-action:disabled {
  cursor: default;
  opacity: 0.45;
}

.scene-button strong,
.scene-button span,
.tree-row span,
.workspace-root span,
.config-row span,
.mcp-row span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.scene-button strong {
  color: var(--text-strong);
  font-size: 12px;
}

.scene-button span {
  color: var(--text-muted);
  font-size: 11px;
}

.workspace-strip {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 66px;
  gap: 6px;
  padding: 8px;
  border-bottom: 1px solid var(--border);
}

.workspace-root,
.mini-action {
  display: inline-flex;
  align-items: center;
  width: 100%;
  height: 32px;
  border-radius: 4px;
  color: var(--text-main);
}

.workspace-root {
  gap: 7px;
  padding: 0 8px;
}

.workspace-actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px;
}

.mini-action {
  justify-content: center;
}

.file-input {
  display: none;
}

.resource-tree {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 4px;
}

.tree-row {
  display: grid;
  grid-template-columns: 12px 16px minmax(0, 1fr);
  align-items: center;
  justify-items: start;
  gap: 6px;
  min-width: 0;
  width: 100%;
  height: 26px;
  padding-right: 6px;
  border-radius: 3px;
  color: var(--text-main);
  font-size: 12px;
  text-align: left;
}

.tree-row.folder {
  color: var(--text-strong);
  font-weight: 700;
}

.chevron {
  display: grid;
  place-items: center;
  width: 12px;
  height: 12px;
  color: var(--text-muted);
  transition: transform 0.16s ease;
}

.tree-icon {
  width: 16px;
  height: 16px;
}

.tree-label {
  width: 100%;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.workspace-context-menu {
  position: fixed;
  z-index: 100;
  width: 176px;
  padding: 4px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--surface-3);
  box-shadow: 0 10px 28px rgb(0 0 0 / 28%);
}

.workspace-context-menu button {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 36px;
  padding: 0 10px;
  border-radius: 3px;
  color: var(--el-color-danger);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}

.workspace-context-menu button:hover,
.workspace-context-menu button:focus-visible {
  background: color-mix(in srgb, var(--el-color-danger) 14%, transparent);
  outline: 1px solid color-mix(in srgb, var(--el-color-danger) 55%, transparent);
}

.workspace-context-menu button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.pane-resizer {
  position: absolute;
  top: 34px;
  bottom: 24px;
  z-index: 12;
  width: 7px;
  padding: 0;
  border: 0;
  outline: 0;
  background: transparent;
  cursor: col-resize;
  touch-action: none;
}

.pane-resizer::after {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 3px;
  width: 1px;
  background: transparent;
  content: '';
  transition: background-color 0.16s ease;
}

.pane-resizer:hover::after,
.pane-resizer:focus-visible::after {
  background: var(--accent);
}

.pane-resizer:focus-visible {
  box-shadow: 0 0 0 2px var(--accent-soft);
}

.explorer-resizer {
  left: calc(var(--activity-width) + var(--explorer-width) - 3px);
}

.assistant-resizer {
  right: calc(var(--assistant-width) - 3px);
}

.chevron.open {
  transform: rotate(90deg);
}

.editor {
  grid-column: 3;
  grid-row: 2;
  display: flex;
  min-width: 0;
  flex-direction: column;
  background: var(--surface-0);
}

.tabs {
  display: flex;
  height: 36px;
  overflow-x: auto;
  border-bottom: 1px solid var(--border-soft);
  background: var(--surface-1);
}

.tab {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: 220px;
  min-width: 104px;
  height: 36px;
  padding: 0 10px;
  border: 0;
  border-right: 1px solid var(--border-soft);
  background: var(--surface-3);
  color: var(--text-main);
  cursor: pointer;
}

.tab.active {
  background: var(--surface-0);
  color: var(--text-strong);
}

.tab span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tab-close {
  flex: 0 0 18px;
  width: 18px;
  height: 18px;
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.tab-close:hover {
  background: var(--surface-hover);
  color: var(--text-strong);
}

.breadcrumbs {
  display: flex;
  align-items: center;
  gap: 7px;
  height: 30px;
  padding: 0 14px;
  border-bottom: 1px solid var(--border-soft);
  background: color-mix(in srgb, var(--surface-0) 82%, var(--surface-2));
  color: var(--text-muted);
  font-size: 12px;
}

.breadcrumbs span:not(:last-child)::after {
  content: '/';
  margin-left: 7px;
  color: var(--border);
}

.editor-body {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.empty-editor {
  display: grid;
  place-items: center;
  height: 100%;
}

.editor-panel {
  min-height: 100%;
  padding: 16px;
}

.editor-toolbar,
.section-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  margin-bottom: 14px;
}

.live-file-tag {
  display: inline-flex;
  gap: 5px;
  align-items: center;
  min-width: 96px;
  justify-content: center;
}

.editor-toolbar strong,
.section-head h1 {
  margin: 0;
  color: var(--text-strong);
  font-size: 18px;
  letter-spacing: 0;
}

.editor-toolbar span,
.section-head p {
  color: var(--text-muted);
}

.toolbar-actions,
.row-buttons {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.file-size {
  color: var(--text-muted);
  font-family: var(--font-mono);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.markdown-editor {
  width: 100%;
  height: calc(100vh - 34px - 24px - 36px - 30px - 62px);
  min-height: 480px;
  padding: 18px 22px;
  border: 0;
  outline: 0;
  resize: none;
  background: var(--surface-0);
  color: var(--text-main);
  font-family: var(--font-mono);
  font-size: 13px;
  line-height: 1.75;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(110px, 1fr));
  gap: 10px;
  margin-bottom: 16px;
}

.metrics article,
.split-content section,
.json-grid section,
.output-section,
.question-item,
.package-row,
.config-row,
.mcp-row,
.settings-card {
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-2);
}

.metrics article {
  padding: 13px;
}

.metrics strong {
  display: block;
  color: var(--text-strong);
  font-size: 24px;
}

.metrics span {
  color: var(--text-muted);
}

.split-content,
.json-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.split-content section,
.json-grid section,
.output-section {
  min-width: 0;
  padding: 13px;
}

h2 {
  margin: 0 0 10px;
  color: var(--text-strong);
  font-size: 13px;
  letter-spacing: 0;
}

p,
li {
  line-height: 1.65;
}

pre {
  margin: 0;
  max-height: 480px;
  overflow: auto;
  padding: 12px;
  border-radius: 4px;
  background: var(--surface-code);
  color: var(--text-main);
  font-family: var(--font-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.text-preview {
  min-height: 420px;
}

.preview-note {
  margin: 0 0 10px;
  color: var(--text-muted);
  font-size: 12px;
}

.mermaid-box {
  min-height: 420px;
  overflow: auto;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-2);
}

.source-details {
  margin-top: 12px;
}

.question-list {
  display: grid;
  gap: 10px;
  margin-bottom: 14px;
}

.question-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px;
}

.question-item strong {
  display: block;
}

.question-item span {
  display: block;
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 12px;
}

.package-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 11px;
}

.settings-workbench {
  padding: 18px;
}

.settings-head {
  margin-bottom: 10px;
}

.settings-tabs {
  height: calc(100vh - 34px - 24px - 36px - 30px - 88px);
}

.settings-tabs :deep(.el-tabs__content) {
  height: calc(100% - 40px);
  overflow: auto;
}

.settings-tabs :deep(.el-tab-pane) {
  min-height: 100%;
}

.settings-grid {
  display: grid;
  gap: 12px;
}

.settings-grid.two-columns {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.settings-card {
  display: grid;
  align-content: start;
  gap: 10px;
  min-height: 132px;
  padding: 14px;
}

.settings-facts {
  display: grid;
  gap: 8px;
  margin: 0;
}

.settings-facts div {
  display: grid;
  grid-template-columns: 96px minmax(0, 1fr);
  gap: 10px;
}

.settings-facts dt {
  color: var(--text-muted);
}

.settings-facts dd {
  min-width: 0;
  margin: 0;
  overflow: hidden;
  color: var(--text-main);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.config-toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.config-block {
  display: grid;
  gap: 10px;
  max-width: 980px;
}

.config-row,
.mcp-row {
  display: grid;
  gap: 10px;
  padding: 10px;
}

.config-row {
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
}

.config-row strong,
.mcp-row strong {
  display: flex;
  align-items: center;
  gap: 5px;
  color: var(--text-strong);
}

.config-row span,
.mcp-row span {
  display: block;
  margin-top: 3px;
  color: var(--text-muted);
  font-size: 12px;
}

.model-form {
  display: grid;
  grid-template-columns: 1fr 1fr auto;
  gap: 8px;
}

.mcp-row header {
  display: flex;
  justify-content: space-between;
  gap: 10px;
}

.right-panel {
  position: relative;
  grid-column: 4;
  grid-row: 2;
  display: flex;
  min-width: 0;
  flex-direction: column;
  border-right: 0;
  border-left: 1px solid var(--chat-divider);
  background: var(--chat-bg);
}

.assistant-head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  min-height: 54px;
  padding: 8px 10px 8px 12px;
  border-bottom: 1px solid var(--chat-divider);
  background: var(--chat-header);
}

.assistant-identity {
  display: flex;
  gap: 9px;
  align-items: center;
  min-width: 0;
}

.assistant-logo {
  display: grid;
  flex: 0 0 30px;
  place-items: center;
  width: 30px;
  height: 30px;
  border: 1px solid color-mix(in srgb, var(--accent) 36%, var(--chat-divider));
  border-radius: 7px;
  background: var(--chat-avatar);
  color: var(--accent);
}

.assistant-identity strong {
  display: block;
  color: var(--text-strong);
  font-size: 13px;
  font-weight: 650;
}

.assistant-status {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-top: 3px;
  color: var(--text-muted);
  font-size: 11px;
}

.assistant-status i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--el-color-success);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--el-color-success) 14%, transparent);
}

.assistant-status i.active {
  background: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
  animation: status-pulse 1.4s ease-in-out infinite;
}

.session-controls {
  display: grid;
  grid-template-columns: minmax(112px, 172px) 32px 32px;
  gap: 5px;
  align-items: center;
  min-width: 0;
}

.session-select {
  min-width: 0;
}

.assistant-head :deep(.el-select__wrapper) {
  min-height: 32px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--chat-header) 72%, var(--surface-3));
  box-shadow: 0 0 0 1px var(--chat-divider) inset;
}

.assistant-head :deep(.el-select__wrapper.is-focused) {
  box-shadow: 0 0 0 1px var(--accent) inset, 0 0 0 3px var(--accent-soft);
}

.head-action {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}

.head-action:hover,
.head-action:focus-visible {
  border-color: var(--chat-divider);
  background: var(--surface-hover);
  color: var(--text-strong);
}

.head-action:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.head-action:disabled {
  cursor: default;
  opacity: 0.4;
}

@keyframes status-pulse {
  0%, 100% { opacity: 0.55; }
  50% { opacity: 1; }
}

.assist-strip {
  display: flex;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
}

.context-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 30px;
  padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--surface-3);
  color: var(--text-main);
  cursor: pointer;
}

.context-chip:hover {
  border-color: var(--accent);
  color: var(--text-strong);
}

.context-chip strong {
  display: grid;
  place-items: center;
  min-width: 20px;
  height: 20px;
  border-radius: 999px;
  background: var(--accent-soft);
  color: var(--accent);
  font-size: 11px;
}

.context-chip.warning strong {
  background: color-mix(in srgb, var(--warning) 24%, transparent);
  color: var(--warning);
}

.popover-content h3 {
  margin: 0 0 8px;
  color: var(--text-strong);
  font-size: 13px;
}

.popover-content ol {
  margin: 0;
  padding-left: 18px;
}

.question-pop {
  display: grid;
  gap: 3px;
  width: 100%;
  margin-bottom: 8px;
  padding: 9px;
  border: 1px solid var(--border);
  border-radius: 4px;
}

.question-pop strong {
  color: var(--text-strong);
  font-size: 12px;
}

.question-pop span {
  color: var(--text-muted);
  font-size: 12px;
}

.messages {
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
  gap: 14px;
  overflow: auto;
  padding: 12px 14px 20px;
  background: var(--chat-bg);
  scroll-behavior: smooth;
}

.empty-chat {
  display: grid;
  flex: 1;
  place-content: center;
  justify-items: center;
  gap: 8px;
  max-width: 300px;
  margin: auto;
  padding: 40px 20px;
  color: var(--text-muted);
  text-align: center;
  line-height: 1.6;
}

.empty-chat-icon {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  margin-bottom: 4px;
  border: 1px solid var(--chat-divider);
  border-radius: 9px;
  background: var(--chat-header);
  color: var(--accent);
}

.empty-chat strong {
  color: var(--text-strong);
  font-size: 14px;
}

.chat-message {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 5px;
}

.chat-message.assistant {
  width: 100%;
}

.chat-message.user {
  align-self: flex-end;
  width: fit-content;
  max-width: 84%;
}

.message-author {
  display: flex;
  align-items: center;
  justify-content: space-between;
  min-height: 24px;
}

.user .message-author {
  justify-content: flex-end;
}

.ai-identity,
.user-identity {
  display: flex;
  align-items: center;
}

.ai-identity {
  gap: 8px;
}

.user-identity {
  gap: 7px;
}

.message-avatar {
  display: grid;
  flex: 0 0 24px;
  place-items: center;
  width: 24px;
  height: 24px;
  border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--chat-divider));
  border-radius: 7px;
  background: var(--chat-avatar);
  color: var(--accent);
}

.streaming-avatar {
  box-shadow: 0 0 0 3px var(--accent-soft);
  animation: avatar-pulse 1.6s ease-in-out infinite;
}

.message-author strong {
  display: block;
  color: var(--text-strong);
  font-size: 12px;
  font-weight: 650;
}

.message-author time {
  display: block;
  margin-top: 1px;
  color: var(--text-muted);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
}

.user-identity time {
  margin: 0;
}

.message-body {
  min-width: 0;
}

.assistant .message-body {
  padding-left: 32px;
}

.user .message-body {
  padding: 7px 10px;
  border: 1px solid var(--chat-user-border);
  border-radius: 10px 10px 3px 10px;
  background: var(--chat-user);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}

.message-copy {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.16s ease, color 0.16s ease, background 0.16s ease;
}

.chat-message:hover .message-copy,
.message-copy:focus-visible {
  opacity: 1;
}

.message-copy:hover {
  background: var(--surface-hover);
  color: var(--text-strong);
}

.message-copy:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.scroll-latest {
  position: sticky;
  bottom: 0;
  display: grid;
  flex: 0 0 32px;
  place-items: center;
  align-self: center;
  width: 32px;
  height: 32px;
  margin-top: -10px;
  border: 1px solid var(--chat-divider);
  border-radius: 50%;
  background: var(--chat-header);
  color: var(--text-main);
  box-shadow: 0 5px 14px rgba(0, 0, 0, 0.2);
  cursor: pointer;
  z-index: 2;
}

.scroll-latest:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.chat-box {
  padding: 8px 10px 10px;
  border-top: 1px solid var(--chat-divider);
  background: var(--chat-bg);
}

.resume-retry {
  display: flex;
  gap: 10px;
  align-items: center;
  justify-content: space-between;
  min-height: 36px;
  margin-bottom: 8px;
  padding: 5px 7px 5px 10px;
  border: 1px solid color-mix(in srgb, var(--warning) 48%, var(--chat-divider));
  border-radius: 6px;
  background: color-mix(in srgb, var(--warning) 9%, var(--chat-composer));
  color: var(--text-main);
  font-size: 11px;
}

.resume-retry > span {
  display: flex;
  gap: 7px;
  align-items: center;
  min-width: 0;
  line-height: 1.4;
}

.resume-retry > span .el-icon {
  flex: 0 0 auto;
  color: var(--warning);
}

.resume-retry :deep(.el-button) {
  flex: 0 0 auto;
}

.composer-shell {
  position: relative;
  display: grid;
  gap: 6px;
  padding: 8px 9px 7px;
  border: 1px solid var(--chat-divider);
  border-radius: 9px;
  background: var(--chat-composer);
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.15);
  transition: border-color 0.16s ease, box-shadow 0.16s ease;
}

.composer-shell:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft), 0 8px 22px rgba(0, 0, 0, 0.18);
}

.composer-shell textarea {
  width: 100%;
  min-height: 40px;
  max-height: 160px;
  padding: 2px;
  border: 0;
  outline: 0;
  resize: none;
  background: transparent;
  color: var(--text-main);
  font-size: 13px;
  line-height: 1.55;
  overflow-y: auto;
}

.composer-references {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  max-height: 76px;
  overflow-y: auto;
  padding-bottom: 1px;
}

.composer-reference {
  display: inline-flex;
  gap: 5px;
  align-items: center;
  min-width: 0;
  max-width: 100%;
  height: 30px;
  padding: 0 3px 0 8px;
  border: 1px solid color-mix(in srgb, var(--accent) 38%, var(--chat-divider));
  border-radius: 6px;
  background: var(--accent-soft);
  color: var(--text-strong);
  font-size: 11px;
}

.composer-reference > .el-icon {
  flex: 0 0 auto;
  color: var(--accent);
}

.composer-reference > span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-reference button {
  display: grid;
  flex: 0 0 auto;
  place-items: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--text-muted);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
}

.composer-reference button:hover,
.composer-reference button:focus-visible {
  background: var(--surface-hover);
  color: var(--text-strong);
}

.composer-reference button:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}

.composer-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.composer-tools {
  display: flex;
  flex: 0 1 auto;
  gap: 4px;
  align-items: center;
  min-width: 0;
}

.composer-model {
  width: 144px;
  min-width: 92px;
}

.composer-model :deep(.el-select__wrapper) {
  min-height: 32px;
  padding: 0 8px;
  border-radius: 6px;
  background: transparent;
  box-shadow: none;
}

.composer-model :deep(.el-select__wrapper:hover),
.composer-model :deep(.el-select__wrapper.is-focused) {
  background: var(--surface-hover);
  box-shadow: 0 0 0 1px var(--chat-divider) inset;
}

.composer-context {
  flex: 1;
  display: flex;
  gap: 5px;
  align-items: center;
  min-width: 0;
  overflow: hidden;
  color: var(--text-muted);
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.composer-context .el-icon {
  flex: 0 0 auto;
  color: var(--el-color-success);
}

.composer-tool,
.send-button {
  display: grid;
  place-items: center;
  border: 0;
  border-radius: 7px;
  cursor: pointer;
  transition: background 0.16s ease, color 0.16s ease, opacity 0.16s ease;
}

.composer-tool {
  width: 34px;
  height: 34px;
  background: transparent;
  color: var(--text-muted);
}

.composer-tool:hover {
  color: var(--text-strong);
  background: var(--surface-hover);
}

.composer-tool:disabled {
  cursor: default;
  opacity: 0.42;
}

.mention-tool {
  font-size: 16px;
  font-weight: 700;
}

.send-button {
  width: 34px;
  height: 34px;
  background: var(--accent);
  color: #ffffff;
}

.send-button:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 84%, #ffffff);
}

.send-button.stop {
  border: 1px solid var(--chat-divider);
  background: var(--surface-3);
  color: var(--text-strong);
}

.send-button.stop:hover {
  border-color: var(--text-muted);
  background: var(--surface-hover);
}

.send-button:disabled {
  cursor: default;
  opacity: 0.45;
}

@keyframes avatar-pulse {
  0%, 100% { box-shadow: 0 0 0 2px var(--accent-soft); }
  50% { box-shadow: 0 0 0 5px transparent; }
}

.spin {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

.status-bar {
  grid-column: 1 / -1;
  grid-row: 3;
  gap: 14px;
  height: 24px;
  padding: 0 10px;
  border-top: 1px solid color-mix(in srgb, var(--accent-strong) 80%, #000000);
  background: var(--accent-strong);
  color: #ffffff;
  font-size: 12px;
}

.status-right {
  margin-left: auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  opacity: 0.9;
}

.status-question {
  min-height: 20px;
  padding: 0 4px;
  border: 0;
  border-radius: 3px;
  background: transparent;
  color: inherit;
  font: inherit;
  cursor: pointer;
}

.status-question:hover,
.status-question:focus-visible {
  background: rgba(255, 255, 255, 0.16);
}

.status-question:focus-visible {
  outline: 2px solid #ffffff;
  outline-offset: 1px;
}

@media (max-width: 1360px) {
  .split-content,
  .json-grid,
  .metrics,
  .settings-grid.two-columns {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 1179px) {
  .studio-shell,
  .studio-shell.settings-focus {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: 42px minmax(0, 1fr) 24px;
    min-width: 0;
  }

  .title-bar {
    grid-template-columns: minmax(0, 1fr) auto;
    height: 42px;
  }

  .brand-zone {
    padding-left: 12px;
  }

  .title-center,
  .activity-bar,
  .explorer,
  .editor,
  .pane-resizer {
    display: none;
  }

  .title-actions {
    grid-column: 2;
    padding-right: 6px;
  }

  .right-panel {
    grid-column: 1;
    grid-row: 2;
    border: 0;
  }

  .studio-shell.settings-focus .editor {
    display: flex;
    grid-column: 1;
    grid-row: 2;
  }

  .studio-shell.settings-focus .right-panel {
    display: none;
  }

  .studio-shell.settings-focus .settings-workbench {
    padding: 12px;
  }

  .assistant-head {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .messages {
    gap: 12px;
    padding: 14px 12px 20px;
  }

  .chat-message.user {
    max-width: 90%;
  }

  .assistant .message-body {
    padding-left: 32px;
  }

  .message-copy {
    opacity: 0.75;
  }

  .chat-box {
    padding: 8px 10px max(8px, env(safe-area-inset-bottom));
  }

  .resume-retry :deep(.el-button) {
    min-height: 44px;
  }

  .composer-shell textarea {
    min-height: 48px;
    font-size: 16px;
  }

  .composer-actions {
    gap: 6px;
  }

  .composer-tool,
  .send-button {
    width: 44px;
    height: 44px;
  }

  .composer-model {
    width: 132px;
  }

  .composer-model :deep(.el-select__wrapper) {
    min-height: 44px;
  }

  .session-controls {
    grid-template-columns: minmax(92px, 132px) 44px 44px;
  }

  .head-action {
    width: 44px;
    height: 44px;
  }

  .status-bar {
    grid-column: 1;
    grid-row: 3;
    overflow: hidden;
    white-space: nowrap;
  }

  .status-bar > span:not(:first-child):not(.status-right) {
    display: none;
  }
}

@media (max-width: 700px) {
  .studio-shell.settings-focus .settings-workbench {
    padding: 0;
  }

  .settings-head {
    margin: 0;
    padding: 10px 12px 4px;
  }

  .settings-head p,
  .settings-head > .el-tag {
    display: none;
  }

  .settings-tabs :deep(.el-tabs__header) {
    margin: 0;
    padding: 0 12px;
  }

  .settings-tabs :deep(.el-tabs__nav-wrap) {
    overflow-x: auto;
  }

  .settings-tabs :deep(.el-tabs__content) {
    height: calc(100dvh - 188px);
  }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
</style>
