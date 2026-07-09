<template>
  <div class="chat">
    <header class="chat-head">
      <el-icon class="chat-ico"><ChatDotRound /></el-icon>
      <span class="chat-title">{{ title }}</span>
      <div class="spacer" />
      <el-button
        v-if="clearPath"
        text
        size="small"
        :icon="Delete"
        @click="clearHistory"
        >清空</el-button
      >
    </header>

    <div ref="scroller" class="messages">
      <div v-if="!messages.length && !streaming" class="empty">
        <!-- <div class="empty-orb"><BrandMark :size="52" /></div>
        <div class="empty-title">{{ emptyTitle }}</div>
        <div class="empty-sub">{{ emptySub }}</div> -->
        <img src="/logo.png" alt="Brand Logo" width="52" height="52" />
        <div class="brand-text">
          <span class="empty-title">零号.奇点工坊</span>
          <!-- <div class="empty-title">{{ emptyTitle }}</div> -->
        <div class="empty-sub">{{ emptySub }}</div> 
        </div>
      </div>

      <template v-for="(m, i) in messages" :key="i">
        <div v-if="m.role === 'user'" class="row user">
          <div class="bubble user">{{ m.content }}</div>
        </div>
        <div v-else class="row ai">
          <div class="avatar"><BrandMark :size="28" /></div>
          <div class="msg-group">
            <div v-if="m.thinking" class="traces">
              <details>
                <summary>
                  <el-icon><MagicStick /></el-icon>
                  AI 思考过程
                </summary>
                <pre class="thinking-body">{{ m.thinking }}</pre>
              </details>
            </div>
            <div v-if="m.tools && m.tools.length" class="traces">
              <details>
                <summary>
                  <el-icon><Operation /></el-icon>
                  工具 / Skill / 子 Agent 调用 · {{ m.tools.length }} 次
                </summary>
                <div v-for="(t, ti) in m.tools" :key="ti" class="trace-step">
                  <span class="trace-kind">{{ traceKind(t.name) }}</span>
                  <span class="trace-name mono"
                    >{{ ti + 1 }}. {{ t.name }}({{
                      (t.args_summary || "").slice(0, 160)
                    }})</span
                  >
                  <div class="trace-res mono">
                    → {{ (t.result_summary || "").slice(0, 260) }}
                  </div>
                </div>
              </details>
            </div>
            <div
              v-if="m.content"
              class="bubble ai"
              v-html="render(m.content)"
            />
          </div>
        </div>
      </template>

      <!-- 流式中的实时气泡 -->
      <div v-if="streaming" class="row ai">
        <div class="avatar"><BrandMark :size="28" /></div>
        <div class="msg-group">
          <div v-if="liveThinking" class="traces live-trace">
            <details open>
              <summary>
                <el-icon><MagicStick /></el-icon>
                AI 思考中
              </summary>
              <pre class="thinking-body">{{ liveThinking }}</pre>
            </details>
          </div>
          <div v-if="liveTools.length" class="traces live-trace">
            <details open>
              <summary>
                <el-icon><Tools /></el-icon>
                工具 / Skill / 子 Agent 调用 · {{ liveTools.length }} 次
              </summary>
              <div
                v-for="(t, ti) in liveTools"
                :key="'lt' + ti"
                class="trace-step live"
              >
                <span class="trace-kind">{{ traceKind(t.name) }}</span>
                <span class="trace-name mono"
                  >{{ ti + 1 }}. {{ t.name }}({{
                    (t.args || "").slice(0, 160)
                  }})</span
                >
                <div class="trace-res mono">
                  <template v-if="t.result"
                    >→ {{ t.result.slice(0, 260) }}</template
                  >
                  <template v-else>执行中...</template>
                </div>
              </div>
            </details>
          </div>
          <div v-if="statusLine" class="status-line">
            <span class="spinner" />{{ statusLine }}
          </div>
          <div
            v-if="liveContent"
            class="bubble ai"
            v-html="render(liveContent)"
          />
        </div>
      </div>
    </div>

    <!-- 结构化问答面板 -->
    <QuestionPanel
      v-if="pendingInteraction"
      :interaction="pendingInteraction"
      @submit="onQuestionSubmit"
      @dismiss="pendingInteraction = null"
    />

    <div v-if="quicks && quicks.length && !pendingInteraction" class="quicks">
      <button
        v-for="q in quicks"
        :key="q.text"
        class="quick"
        @click="send(q.text)"
      >
        <el-icon><Promotion /></el-icon>{{ q.label }}
      </button>
    </div>

    <div class="composer">
      <div v-if="attachmentsUploadPath" class="attachments">
        <input
          ref="fileInput"
          type="file"
          multiple
          class="hidden-file"
          @change="onAttachmentUpload"
        />
        <el-button
          size="small"
          text
          :icon="Upload"
          :loading="uploadingAttachments"
          @click="fileInput?.click()"
        >
          附件
        </el-button>
        <span v-if="!attachments.length" class="attachment-hint"
          >当前会话无附件</span
        >
        <span v-for="f in attachments" :key="f.name" class="attachment-chip">
          {{ f.name }}
        </span>
        <el-button
          v-if="attachments.length && attachmentsClearPath"
          size="small"
          text
          :icon="Close"
          @click="clearAttachments"
        >
          清空
        </el-button>
      </div>
      <div class="composer-box" :class="{ focused }">
        <el-input
          v-model="draft"
          type="textarea"
          :autosize="{ minRows: 1, maxRows: 6 }"
          :placeholder="placeholder"
          :disabled="streaming"
          @focus="focused = true"
          @blur="focused = false"
          @keydown.enter.exact.prevent="send()"
        />
        <div class="composer-foot">
          <span class="hint">Enter 发送 · Shift+Enter 换行</span>
          <el-button
            v-if="!streaming"
            type="primary"
            class="send"
            :icon="Promotion"
            :disabled="!draft.trim()"
            @click="send()"
            >发送</el-button
          >
          <el-button
            v-else
            type="danger"
            class="send"
            :icon="VideoPause"
            @click="stop"
            >停止</el-button
          >
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick, watch, onMounted } from "vue";
import { ElMessageBox } from "element-plus";
import {
  ChatDotRound,
  Close,
  Delete,
  MagicStick,
  Operation,
  Tools,
  Promotion,
  Upload,
  VideoPause,
} from "@element-plus/icons-vue";
import { marked } from "marked";
import { http } from "@/api/http";
import { streamSSE, type SSEEvent } from "@/api/sse";
import type {
  AttachmentFile,
  ChatMessage,
  Interaction,
  ToolTrace,
} from "@/api/types";
import QuestionPanel from "./QuestionPanel.vue";
import BrandMark from "./BrandMark.vue";

const props = defineProps<{
  title: string;
  chatPath: string;
  historyPath?: string;
  clearPath?: string;
  reloadKey?: string | number;
  placeholder?: string;
  emptyTitle?: string;
  emptySub?: string;
  quicks?: { label: string; text: string }[];
  requestPayload?: Record<string, any>;
  doneRefreshResource?: string;
  attachmentsPath?: string;
  attachmentsUploadPath?: string;
  attachmentsClearPath?: string;
}>();
const emit = defineEmits<{
  (e: "refresh", resource: string): void;
  (e: "status", status: string): void;
}>();

const messages = ref<ChatMessage[]>([]);
const draft = ref("");
const streaming = ref(false);
const focused = ref(false);
const liveContent = ref("");
const liveThinking = ref("");
const statusLine = ref("");
const liveTools = ref<{ name: string; args?: string; result?: string }[]>([]);
const pendingInteraction = ref<Interaction | null>(null);
const attachments = ref<AttachmentFile[]>([]);
const uploadingAttachments = ref(false);
const fileInput = ref<HTMLInputElement>();
const scroller = ref<HTMLElement>();
let abort: (() => void) | null = null;

marked.setOptions({ breaks: true });
function render(text: string) {
  try {
    return marked.parse(text || "") as string;
  } catch {
    return text;
  }
}

function traceKind(name: string) {
  if (name === "task") return "子 Agent";
  if (name.includes("__")) return "Skill";
  return "工具";
}

async function loadHistory() {
  if (!props.historyPath) {
    messages.value = [];
    return;
  }
  try {
    const { data } = await http.get(props.historyPath);
    messages.value = data;
    await scrollBottom();
  } catch {
    messages.value = [];
  }
}

async function scrollBottom() {
  await nextTick();
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
}

watch(
  () => [props.reloadKey, props.historyPath, props.attachmentsPath],
  () => {
    loadHistory();
    loadAttachments();
  },
);
onMounted(() => {
  loadHistory();
  loadAttachments();
});

async function loadAttachments() {
  if (!props.attachmentsPath) return;
  try {
    const { data } = await http.get(props.attachmentsPath);
    attachments.value = data.files || [];
  } catch {
    attachments.value = [];
  }
}

function send(preset?: string) {
  const text = (preset ?? draft.value).trim();
  if (!text || streaming.value) return;
  draft.value = "";
  pendingInteraction.value = null;
  messages.value.push({ id: "u" + Date.now(), role: "user", content: text });
  streaming.value = true;
  liveContent.value = "";
  liveThinking.value = "";
  statusLine.value = "已提交，AI 正在处理…";
  liveTools.value = [];
  scrollBottom();

  abort = streamSSE(
    props.chatPath,
    { message: text, ...(props.requestPayload || {}) },
    onEvent,
    {
      onDone: finalize,
      onError: () => {
        statusLine.value = "";
        finalize();
      },
    },
  );
}

function onEvent(ev: SSEEvent) {
  switch (ev.type) {
    case "thinking":
      liveThinking.value += ev.delta || "";
      statusLine.value = "AI 推理中…";
      scrollBottom();
      break;
    case "content":
      liveContent.value += ev.delta || "";
      statusLine.value = "";
      scrollBottom();
      break;
    case "heartbeat":
      statusLine.value = ev.message || `执行中…（已 ${ev.elapsed || "?"}s）`;
      break;
    case "tool_call":
      liveTools.value.push({ name: ev.name || "", args: ev.args || "" });
      statusLine.value = `正在执行 ${ev.name || "工具"}…`;
      scrollBottom();
      break;
    case "tool_result": {
      const last = liveTools.value[liveTools.value.length - 1];
      if (last && last.name === ev.name) last.result = ev.result || "";
      statusLine.value = "";
      break;
    }
    case "interaction":
      pendingInteraction.value = ev.interaction as Interaction;
      scrollBottom();
      break;
    case "refresh":
      emit("refresh", ev.resource || "");
      break;
    case "status":
      emit("status", ev.status || "");
      break;
    case "error":
      liveContent.value += "\n\n⚠️ " + (ev.message || "出错");
      break;
  }
}

function finalize() {
  if (liveContent.value || liveThinking.value || liveTools.value.length) {
    messages.value.push({
      id: "a" + Date.now(),
      role: "assistant",
      content: liveContent.value,
      thinking: liveThinking.value,
      tools: liveTools.value.map(
        (t) =>
          ({
            name: t.name,
            args_summary: t.args || "",
            result_summary: t.result || "",
          }) as ToolTrace,
      ),
    });
  }
  liveContent.value = "";
  liveThinking.value = "";
  statusLine.value = "";
  liveTools.value = [];
  streaming.value = false;
  abort = null;
  if (props.doneRefreshResource) emit("refresh", props.doneRefreshResource);
  scrollBottom();
}

function stop() {
  if (abort) abort();
  statusLine.value = "";
  finalize();
}

function onQuestionSubmit(text: string) {
  pendingInteraction.value = null;
  send(text);
}

async function clearHistory() {
  if (!props.clearPath) return;
  try {
    await ElMessageBox.confirm("确定清空对话历史？", "提示", {
      type: "warning",
    });
    await http.delete(props.clearPath);
    messages.value = [];
    if (props.doneRefreshResource) emit("refresh", props.doneRefreshResource);
  } catch {
    /* cancelled */
  }
}

async function onAttachmentUpload(e: Event) {
  const fl = (e.target as HTMLInputElement).files;
  if (!fl || !fl.length || !props.attachmentsUploadPath) return;
  uploadingAttachments.value = true;
  try {
    const form = new FormData();
    Array.from(fl).forEach((f) => form.append("files", f));
    await http.post(props.attachmentsUploadPath, form);
    await loadAttachments();
  } finally {
    uploadingAttachments.value = false;
    if (fileInput.value) fileInput.value.value = "";
  }
}

async function clearAttachments() {
  if (!props.attachmentsClearPath) return;
  await http.delete(props.attachmentsClearPath);
  await loadAttachments();
}

defineExpose({ reload: loadHistory, send });
</script>

<style scoped lang="scss">
.chat {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--bg-app);
  background-image: var(--bg-app-grad);
}

/* Head -------------------------------------------------------------------- */
.chat-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 14px;
  height: 52px;
  flex-shrink: 0;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.chat-ico {
  color: var(--brand);
  font-size: 17px;
}
.chat-title {
  font-size: var(--text-base);
  font-weight: 700;
  color: var(--text-1);
}
.spacer {
  flex: 1;
}

/* Messages ---------------------------------------------------------------- */
.messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px 18px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.empty {
  margin: auto;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 20px;
}
.empty-orb {
  margin-bottom: 8px;
  opacity: 0.95;
}
.empty-title {
  font-size: var(--text-lg);
  font-weight: 700;
  color: var(--text-1);
}
.empty-sub {
  font-size: var(--text-base);
  color: var(--text-3);
  max-width: 320px;
  line-height: 1.65;
}

.row {
  display: flex;
  gap: 10px;
}
.row.user {
  justify-content: flex-end;
}
.row.ai {
  justify-content: flex-start;
}
.avatar {
  flex-shrink: 0;
  margin-top: 2px;
}

.bubble {
  border-radius: var(--r-lg);
  padding: 11px 15px;
  font-size: var(--text-md);
  line-height: 1.65;
  word-break: break-word;
}
.bubble.user {
  max-width: 82%;
  background: var(--brand);
  color: var(--brand-ink);
  border-bottom-right-radius: var(--r-xs);
  white-space: pre-wrap;
  box-shadow: var(--shadow-sm);
}
.msg-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 88%;
  min-width: 0;
}
.bubble.ai {
  background: var(--surface);
  border: 1px solid var(--border);
  border-bottom-left-radius: var(--r-xs);
  box-shadow: var(--shadow-sm);
  color: var(--text-1);
}
.bubble.ai :deep(p) {
  margin: 0 0 8px;
}
.bubble.ai :deep(p:last-child) {
  margin-bottom: 0;
}
.bubble.ai :deep(h1),
.bubble.ai :deep(h2),
.bubble.ai :deep(h3) {
  font-size: var(--text-md);
  margin: 12px 0 6px;
}
.bubble.ai :deep(ul),
.bubble.ai :deep(ol) {
  margin: 4px 0;
  padding-left: 20px;
}
.bubble.ai :deep(li) {
  margin: 2px 0;
}
.bubble.ai :deep(table) {
  border-collapse: collapse;
  font-size: var(--text-sm);
  display: block;
  overflow-x: auto;
  margin: 6px 0;
}
.bubble.ai :deep(th),
.bubble.ai :deep(td) {
  border: 1px solid var(--border);
  padding: 5px 9px;
}
.bubble.ai :deep(th) {
  background: var(--surface-sunken);
}
.bubble.ai :deep(code) {
  background: var(--code-bg);
  padding: 1.5px 5px;
  border-radius: 5px;
  font-family: var(--font-mono);
  font-size: var(--text-sm);
}
.bubble.ai :deep(pre) {
  background: var(--code-bg);
  border: 1px solid var(--border);
  padding: 12px;
  border-radius: var(--r-sm);
  overflow-x: auto;
}
.bubble.ai :deep(pre code) {
  background: none;
  padding: 0;
}
.bubble.ai :deep(a) {
  color: var(--brand);
}

/* Thinking / traces ------------------------------------------------------- */
.thinking {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--text-sm);
  color: var(--text-3);
  font-style: italic;
  background: var(--surface-2);
  border: 1px solid var(--border);
  padding: 7px 11px;
  border-radius: var(--r-sm);
}
.traces {
  font-size: var(--text-sm);
}
.traces summary {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  color: var(--text-2);
  font-weight: 600;
  padding: 5px 10px;
  border-radius: var(--r-xs);
  background: var(--surface-2);
  border: 1px solid var(--border);
  list-style: none;
  user-select: none;
}
.traces summary::-webkit-details-marker {
  display: none;
}
.traces summary:hover {
  color: var(--text-1);
  border-color: var(--border-strong);
}
.trace-step {
  padding: 6px 10px;
  border-left: 2px solid var(--brand-soft-2);
  margin: 6px 0 6px 8px;
}
.trace-step.live {
  border-left-color: var(--info);
}
.trace-kind {
  display: inline-flex;
  align-items: center;
  height: 18px;
  padding: 0 6px;
  margin-right: 6px;
  border-radius: var(--r-xs);
  background: var(--brand-soft);
  color: var(--brand);
  font-size: var(--text-xs);
  font-weight: 700;
}
.trace-name {
  font-size: var(--text-sm);
  color: var(--text-2);
}
.trace-res {
  color: var(--success);
  margin-top: 3px;
  font-size: var(--text-xs);
}
.thinking-body {
  margin: 6px 0 0 8px;
  max-height: 240px;
  overflow: auto;
  white-space: pre-wrap;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.6;
  color: var(--text-2);
  background: var(--code-bg);
  border-left: 2px solid var(--warning);
  border-radius: var(--r-xs);
  padding: 8px 10px;
}
.live-trace summary {
  border-color: color-mix(in srgb, var(--info) 30%, transparent);
}

/* Live -------------------------------------------------------------------- */
.live-tool {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
  font-size: var(--text-sm);
  color: var(--info);
  background: var(--info-soft);
  border: 1px solid color-mix(in srgb, var(--info) 30%, transparent);
  padding: 6px 11px;
  border-radius: var(--r-sm);
}
.live-ok {
  color: var(--success);
}
.status-line {
  display: flex;
  align-items: center;
  gap: 9px;
  font-size: var(--text-sm);
  color: var(--warning);
  font-weight: 500;
}
.spinner {
  width: 13px;
  height: 13px;
  border-radius: 50%;
  border: 2px solid var(--warning);
  border-top-color: transparent;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

/* Quicks ------------------------------------------------------------------ */
.quicks {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  padding: 10px 18px 0;
}
.quick {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 6px 13px;
  border-radius: var(--r-full);
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
  background: var(--surface);
  border: 1px solid var(--border);
  color: var(--text-2);
  transition: all var(--dur) var(--ease);
}
.quick .el-icon {
  font-size: 13px;
}
.quick:hover {
  border-color: var(--brand);
  color: var(--brand);
  background: var(--brand-soft);
}

/* Composer ---------------------------------------------------------------- */
.composer {
  padding: 12px 18px 16px;
  flex-shrink: 0;
}
.attachments {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.hidden-file {
  display: none;
}
.attachment-hint {
  font-size: var(--text-xs);
  color: var(--text-3);
}
.attachment-chip {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-xs);
  color: var(--info);
  background: var(--info-soft);
  border: 1px solid color-mix(in srgb, var(--info) 24%, transparent);
  border-radius: var(--r-full);
  padding: 3px 9px;
}
.composer-box {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  padding: 6px 6px 6px;
  transition:
    border-color var(--dur) var(--ease),
    box-shadow var(--dur) var(--ease);
}
.composer-box.focused {
  border-color: var(--brand);
  box-shadow: var(--ring);
}
.composer-box :deep(.el-textarea__inner) {
  background: transparent;
  border: none;
  box-shadow: none !important;
  resize: none;
  padding: 8px 10px;
  font-size: var(--text-md);
  line-height: 1.6;
  color: var(--text-1);
}
.composer-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 2px 6px 2px 12px;
}
.hint {
  font-size: var(--text-xs);
  color: var(--text-3);
}
.send {
  border-radius: var(--r-sm);
}
</style>
