/* =========================================================================
 * 业务流逆向工程平台 —— 前端逻辑
 *
 * 交互原则（与后端接口划分一致）：
 *   - 与 AI 协作完成任务 → 走流式接口 POST /chat（SSE：思考/回答/工具调用实时呈现）
 *   - 对话记录、关系图谱、流程图谱、增删业务场景 → 走专用 REST 接口
 * ========================================================================= */

'use strict';

const API = '/api';

const STATE = {
  scenarios: [],
  current: null,        // 当前业务场景对象
  diagramMode: 'business', // business | relations | flow（流程/图谱标签）
  streaming: false,
  auditTypes: null,     // 规则库违规类型（来自 /audit-types）
  validations: [],      // 校验报告（来自 /validations）
  businessProcess: null, // Phase 0 业务流程文档（来自 /business-process）
};

/* ----------------------------------------------------------------- 通用请求 */
async function http(method, path, body, isForm) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    if (isForm) { opt.body = body; }
    else { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  }
  const res = await fetch(API + path, opt);
  if (!res.ok) {
    let msg = res.statusText;
    try { const e = await res.json(); msg = e.detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  if (res.status === 204) return null;
  return res.json();
}

/* ----------------------------------------------------------------- 初始化 */
document.addEventListener('DOMContentLoaded', async () => {
  setupDragDrop();
  setupDiagramPan();
  initMermaid();
  await checkHealth();
  await loadScenarios();
});

async function checkHealth() {
  try {
    const h = await http('GET', '/health');
    const badge = document.getElementById('llm-badge');
    if (h.llm_enabled) {
      badge.className = 'llm-badge llm-on';
      badge.textContent = 'LLM: ' + (h.llm_model || 'on');
    } else {
      badge.className = 'llm-badge llm-off';
      badge.textContent = 'LLM: 启发式模式';
    }
  } catch (_) {}
}

function setupDragDrop() {
  const zone = document.getElementById('upload-zone');
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.style.borderColor = 'var(--cyan)'; });
  zone.addEventListener('dragleave', () => { zone.style.borderColor = ''; });
  zone.addEventListener('drop', e => { e.preventDefault(); zone.style.borderColor = ''; handleFileSelect(e.dataTransfer.files); });
}

/* ----------------------------------------------------------------- 场景列表 */
async function loadScenarios() {
  STATE.scenarios = await http('GET', '/scenarios');
  renderScenarioList();
}

function renderScenarioList() {
  const list = document.getElementById('scenario-list');
  list.innerHTML = STATE.scenarios.map(sc => {
    const dot = ['skills_generated', 'active'].includes(sc.status) ? 'active-dot'
      : ['tables_uploaded', 'process_drafted', 'process_approved', 'relations_deduced', 'flow_deduced'].includes(sc.status) ? 'deducing' : '';
    const active = STATE.current && sc.id === STATE.current.id ? 'active' : '';
    return `<div class="scenario-item ${active}" onclick="selectScenario('${sc.id}')">
      <div class="scenario-dot ${dot}"></div>
      <span class="scenario-name">${esc(sc.name)}</span>
      <span class="scenario-del" onclick="event.stopPropagation();deleteScenario('${sc.id}')">×</span>
    </div>`;
  }).join('');
  const sc = STATE.current;
  document.getElementById('tables-count').textContent = sc ? (sc.tables_meta || []).length : 0;
  document.getElementById('skills-count').textContent = sc ? (sc.skills || []).length : 0;
}

async function selectScenario(id) {
  STATE.current = await http('GET', '/scenarios/' + id);
  document.getElementById('topbar-name').textContent = STATE.current.name;
  document.getElementById('btn-delete').style.display = '';
  updateStatusBadge(STATE.current.status);
  renderScenarioList();
  await loadHistory();
  renderDetails();
  await loadAudit();
  await loadBusinessProcess();
  refreshDiagramToolbar();
  renderDiagram();
  renderGuide();
}

/* ----------------------------------------------------------------- 流程引导 */
/* 6 步工作流（v1.0.1）：上传 → 关联 → 解析规则 → 执行审核 → 生成技能 → 使用 */
const WORKFLOW = [
  { key: 'upload',   icon: '📎', label: '上传表格', done: sc => (sc.tables_meta || []).length > 0,
    hint: '上传业务表 + 规则表 + 历史结果表（点下方上传区）。',
    go: '上传表格', act: () => document.getElementById('file-input').click() },
  { key: 'process', icon: '🧭', label: '梳理流程', done: sc => !!(sc.business_process && sc.business_process.approved),
    hint: 'Phase 0：梳理业务流程并审批（识别输入/规则/结果表、生成流程图）。',
    go: '梳理业务流程', act: () => sendQuick('梳理业务流程') },
  { key: 'relations', icon: '🔗', label: '推导关联', done: sc => !!sc.relations,
    hint: '让 AI 推导表间关联，生成 ER 关系图谱。',
    go: '推导关联关系', act: () => sendQuick('推导表关联关系') },
  { key: 'rules', icon: '📋', label: '解析规则', done: sc => !!(sc.rule_library && (sc.rule_library.templates || []).length),
    hint: '把规则表解析成规则模板库（全部可审核的违规类型）。',
    go: '解析规则库', act: () => sendQuick('解析规则库') },
  { key: 'audit', icon: '🛡', label: '执行审核', done: sc => (sc.validations || []).some(v => v.passed),
    hint: '选一条规则（如重复收费）在完整数据上执行，并与历史结果对照校验。',
    go: '执行审核', act: () => sendQuick('执行审核（重复收费）并与历史结果对照校验') },
  { key: 'skills', icon: '⚡', label: '生成技能', done: sc => (sc.skills || []).length > 0,
    hint: '固化为参数化审核技能（list_audit_types / execute_audit）。',
    go: '生成技能库', act: () => sendQuick('生成技能库') },
  { key: 'use', icon: '✅', label: '已就绪', done: sc => sc.status === 'active',
    hint: '技能已就绪：可对新数据动态执行任意违规类型的审核。',
    go: '查看技能', act: () => switchTab('skills') },
];

function renderGuide() {
  const el = document.getElementById('workflow-guide');
  if (!el) return;
  const sc = STATE.current;
  if (!sc) { el.style.display = 'none'; return; }
  el.style.display = 'block';

  const states = WORKFLOW.map(s => s.done(sc));
  // 当前步 = 第一个未完成的步骤
  let current = states.findIndex(d => !d);
  if (current === -1) current = WORKFLOW.length - 1;

  const steps = WORKFLOW.map((s, i) => {
    const cls = states[i] ? 'done' : (i === current ? 'current' : '');
    const mark = states[i] ? '✓' : (i + 1);
    return `<div class="wf-step ${cls}" title="${esc(s.hint)}" onclick="runGuideStep(${i})">
      <div class="wf-dot">${mark}</div><div class="wf-label">${s.icon} ${esc(s.label)}</div></div>`;
  }).join('');

  const step = WORKFLOW[current];
  const allDone = states.every(Boolean);
  const hint = allDone
    ? `<span>🎉 全部完成：可对新数据执行 <b>execute_audit(类型, 数据)</b>。</span>
       <span class="wf-go" onclick="switchTab('skills')">查看技能 →</span>`
    : `<span>下一步：<b>${esc(step.label)}</b> —— ${esc(step.hint)}</span>
       <span class="wf-go" onclick="runGuideStep(${current})">${esc(step.go)} →</span>`;
  el.innerHTML = `<div class="wf-steps">${steps}</div><div class="wf-hint">${hint}</div>`;
}

function runGuideStep(i) {
  const s = WORKFLOW[i];
  if (!STATE.current) { addAIBubble('请先创建或选择一个业务场景。'); return; }
  if (s && typeof s.act === 'function') s.act();
}

/* ----------------------------------------------------------------- 审核能力 */
async function loadAudit() {
  if (!STATE.current) return;
  try {
    const [at, vs] = await Promise.all([
      http('GET', `/scenarios/${STATE.current.id}/audit-types`),
      http('GET', `/scenarios/${STATE.current.id}/validations`),
    ]);
    STATE.auditTypes = at;
    STATE.validations = vs || [];
  } catch (_) { STATE.auditTypes = null; STATE.validations = []; }
  renderAudit();
}

const STATE_LABEL = {
  verified: '已校验✓', unverified: '可执行(未校验)', blocked: '缺数据/口径', parsed: '未细化',
};
function renderAudit() {
  const at = STATE.auditTypes;
  const typesEl = document.getElementById('audit-types-detail');
  if (typesEl) {
    const types = (at && at.violation_types) || [];
    typesEl.innerHTML = types.length ? types.map(t => {
      const vtSafe = esc(t.violation_type).replace(/'/g, "\\'");
      const btn = t.has_sql
        ? `<span class="btn-run-audit" onclick="runAudit('${vtSafe}')">▶ ${t.has_historical ? '校验' : '执行'}</span>`
        : '';
      return `
      <div class="audit-card">
        <div class="audit-card-head">
          <span class="audit-vt">${esc(t.violation_type)}</span>
          <span class="audit-state st-${t.state}">${STATE_LABEL[t.state] || t.state}</span>
          ${btn}
        </div>
        <div class="audit-meta">${t.rule_count} 条细则${t.has_sql ? ' · 有 SQL' : ''}${t.has_historical ? ' · 有历史结果表' : ''}</div>
      </div>`;
    }).join('') : '<div class="muted-tip">解析规则库后显示。对 AI 说「解析规则库」。</div>';
  }
  const vEl = document.getElementById('validations-detail');
  if (vEl) {
    vEl.innerHTML = (STATE.validations || []).length ? STATE.validations.map(v => `
      <div class="valid-card ${v.passed ? 'pass' : 'fail'}">
        <div><strong>${esc(v.violation_type)}</strong> ${v.passed ? '✅ 通过' : '⚠️ 未达标'}
          <span style="color:var(--green)">命中率 ${Math.round((v.match_rate || 0) * 100)}%</span></div>
        <div class="valid-stat">历史 ${v.historical_count} · 复刻 ${v.produced_count} · 命中 ${v.matched} · 缺失 ${v.missing} · 多出 ${v.extra}</div>
        ${v.key_columns && v.key_columns.length ? `<div class="audit-meta">主键：${esc(v.key_columns.join(', '))}</div>` : ''}
      </div>`).join('') : '<div class="muted-tip">执行审核校验后显示。</div>';
  }
}

async function runAudit(violationType) {
  if (!STATE.current) return;
  addUserBubble('执行审核校验：' + esc(violationType));
  const node = addAIStreaming();
  node.content.innerHTML = '<span class="typing-dots"><span></span><span></span><span></span></span>';
  try {
    const rep = await http('POST', `/scenarios/${STATE.current.id}/audit`, { violation_type: violationType });
    await loadAudit();
    STATE.current = await http('GET', '/scenarios/' + STATE.current.id);
    updateStatusBadge(STATE.current.status);
    node.content.innerHTML = renderMarkdown(
      `**${violationType}** 审核校验完成：${rep.passed ? '✅ 通过' : '⚠️ 未达标'}\n\n${rep.message}`);
    switchTab('audit');
  } catch (e) {
    node.content.innerHTML = '❌ 校验失败：' + esc(e.message);
  }
  scrollChat();
}

/* ----------------------------------------------------------------- 新建/删除 */
function openNewScenarioModal() {
  document.getElementById('new-scenario-name').value = '';
  document.getElementById('new-scenario-desc').value = '';
  showModal('modal-new-scenario');
}

async function createScenario() {
  const name = document.getElementById('new-scenario-name').value.trim();
  const description = document.getElementById('new-scenario-desc').value.trim();
  if (!name) { alert('请输入场景名称'); return; }
  const sc = await http('POST', '/scenarios', { name, description });
  closeModal('modal-new-scenario');
  await loadScenarios();
  await selectScenario(sc.id);
  clearMessages();
  addAIBubble(`🎉 业务场景「<strong>${esc(name)}</strong>」创建成功！<br><br>
    请上传业务数据表格（CSV/Excel），我会快速扫描结构（仅读表头+少量样本）。<br>
    然后对我说「<strong>梳理业务流程</strong>」进入 Phase 0：先把业务讲清楚并经你批准，再开始逆向工程。`);
}

async function deleteScenario(id) {
  if (!confirm('确定删除该业务场景及其全部数据？此操作不可恢复。')) return;
  await http('DELETE', '/scenarios/' + id);
  if (STATE.current && STATE.current.id === id) { STATE.current = null; resetWorkspace(); }
  await loadScenarios();
}

function deleteCurrentScenario() { if (STATE.current) deleteScenario(STATE.current.id); }

function resetWorkspace() {
  document.getElementById('topbar-name').textContent = '请选择或创建业务场景';
  document.getElementById('btn-delete').style.display = 'none';
  updateStatusBadge('created');
  clearMessages();
  const empty = document.getElementById('diagram-empty');
  const host = document.getElementById('mermaid-host');
  const svg = document.getElementById('graph-svg');
  if (empty) empty.style.display = 'flex';
  if (host) host.style.display = 'none';
  if (svg) svg.style.display = 'none';
  renderGuide();
}

/* ----------------------------------------------------------------- 状态徽标 */
const STATUS_LABEL = {
  created: '未开始', tables_uploaded: '表格已上传',
  process_drafted: '流程待审批', process_approved: '流程已批准',
  relations_deduced: '关系已推导',
  rules_parsed: '规则已解析', flow_deduced: '流程已推导', validated: '已校验',
  skills_generated: '技能已生成', active: '运行中'
};
function updateStatusBadge(status) {
  const badge = document.getElementById('topbar-status');
  badge.className = 'status-badge status-' + status;
  badge.textContent = STATUS_LABEL[status] || status;
  if (STATE.current) { STATE.current.status = status; renderGuide(); }
}

/* ----------------------------------------------------------------- 文件上传 */
async function handleFileSelect(files) {
  if (!STATE.current) { addAIBubble('⚠️ 请先创建或选择一个业务场景，再上传表格。'); return; }
  if (!files || !files.length) return;
  const form = new FormData();
  Array.from(files).forEach(f => form.append('files', f));
  addUserBubble('上传表格：' + Array.from(files).map(f => f.name).join('、'));
  const node = addAIStreaming();
  node.content.innerHTML = '<span class="typing-dots"><span></span><span></span><span></span></span>';
  try {
    const r = await http('POST', `/scenarios/${STATE.current.id}/uploads`, form, true);
    STATE.current = await http('GET', '/scenarios/' + STATE.current.id);
    updateStatusBadge(STATE.current.status);
    renderDetails(); renderScenarioList(); await loadAudit(); renderGuide();
    const rows = (r.tables_meta || []).map(t =>
      `📊 <strong>${esc(t.table_name)}</strong>：${t.row_count.toLocaleString()} 行 × ${t.col_count} 列`).join('<br>');
    node.content.innerHTML = `✅ 已上传并扫描 ${r.tables_meta.length} 张表：<br><br>${rows}<br><br>
      切到「表格」标签可看字段详情；接着对我说「<strong>梳理业务流程</strong>」开始 Phase 0（业务流程发现与审批）。`;
  } catch (e) {
    node.content.innerHTML = '❌ 上传失败：' + esc(e.message);
  }
  document.getElementById('file-input').value = '';
}

/* ----------------------------------------------------------------- 历史记录 */
async function loadHistory() {
  clearMessages();
  const msgs = await http('GET', `/scenarios/${STATE.current.id}/messages`);
  if (!msgs.length) {
    addAIBubble('👋 你好！我是业务流逆向工程助手。上传业务表格后，对我说「梳理业务流程」开始 Phase 0（业务流程发现与审批），随后再逐阶段推导。');
    return;
  }
  msgs.forEach(m => {
    if (m.role === 'user') { addUserBubble(esc(m.content)); return; }
    const node = addAIStreaming();
    if (m.thinking) { node.ensureThink(); node.thinkBody.textContent = m.thinking; node.collapseThink(); }
    (m.tools || []).forEach(t => {
      const tt = node.addTool(t.name, t.args_summary);
      tt.done(t.result_summary);
    });
    node.content.innerHTML = renderMarkdown(m.content || '');
  });
  scrollChat();
}

/* ----------------------------------------------------------------- 发送消息 / SSE */
function sendQuick(text) { document.getElementById('chat-input').value = text; sendMessage(); }
function handleKeydown(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }
function autoResize(el) { el.style.height = ''; el.style.height = Math.min(el.scrollHeight, 120) + 'px'; }

async function sendMessage() {
  const input = document.getElementById('chat-input');
  const msg = input.value.trim();
  if (!msg) return;
  if (!STATE.current) { addAIBubble('请先创建或选择一个业务场景。'); return; }
  if (STATE.streaming) return;

  input.value = ''; input.style.height = '';
  addUserBubble(esc(msg));
  await streamChat(msg);
}

async function streamChat(message) {
  STATE.streaming = true;
  document.getElementById('btn-send').disabled = true;
  const node = addAIStreaming();
  node.content.innerHTML = '<span class="typing-dots"><span></span><span></span><span></span></span>';
  let contentStarted = false;
  let rawContent = '';

  try {
    const res = await fetch(`${API}/scenarios/${STATE.current.id}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message }),
    });
    if (!res.ok || !res.body) throw new Error('请求失败：' + res.status);

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE 帧以空行分隔
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const line = frame.split('\n').find(l => l.startsWith('data:'));
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(5).trim()); } catch (_) { continue; }
        handleEvent(ev, node, () => {
          if (!contentStarted) { contentStarted = true; node.content.innerHTML = ''; }
        }, (delta) => { rawContent += delta; node.content.innerHTML = renderMarkdown(rawContent); });
      }
    }
  } catch (e) {
    node.content.innerHTML = '❌ ' + esc(e.message);
  } finally {
    STATE.streaming = false;
    document.getElementById('btn-send').disabled = false;
    if (!rawContent && contentStarted) node.content.innerHTML = '<span class="muted-tip">（无文本回复）</span>';
    scrollChat();
  }
}

function handleEvent(ev, node, onContentStart, onContentDelta) {
  switch (ev.type) {
    case 'thinking':
      node.ensureThink();
      node.thinkBody.textContent += ev.delta;
      scrollChat();
      break;
    case 'content':
      onContentStart();
      onContentDelta(ev.delta);
      scrollChat();
      break;
    case 'tool_call':
      node.collapseThink();
      node._activeTool = node.addTool(ev.name, ev.args);
      scrollChat();
      break;
    case 'tool_result':
      if (node._activeTool) { node._activeTool.done(ev.result); node._activeTool = null; }
      break;
    case 'refresh':
      onResourceRefresh(ev.resource);
      break;
    case 'status':
      updateStatusBadge(ev.status);
      renderScenarioList();
      break;
    case 'interaction':
      renderInteraction(node, ev.interaction);
      scrollChat();
      break;
    case 'error':
      onContentStart(); onContentDelta('\n\n⚠️ ' + ev.message);
      break;
    case 'done':
      node.collapseThink();
      break;
  }
}

/* 资源更新：重新拉取并刷新对应视图（走专用 REST 接口） */
async function onResourceRefresh(resource) {
  if (!STATE.current) return;
  STATE.current = await http('GET', '/scenarios/' + STATE.current.id);
  renderDetails();
  if (resource === 'relations') STATE.diagramMode = 'relations';
  if (resource === 'flow') STATE.diagramMode = 'flow';
  if (resource === 'business_process') { STATE.diagramMode = 'business'; await loadBusinessProcess(); }
  if (resource === 'rules' || resource === 'validations') { await loadAudit(); }
  refreshDiagramToolbar();
  renderDiagram();
  renderScenarioList();
  renderGuide();
}

/* ----------------------------------------------------------------- Phase 0：业务流程文档 */
async function loadBusinessProcess() {
  if (!STATE.current) { STATE.businessProcess = null; renderBusinessProcess(); return; }
  let bp = null;
  try { bp = await http('GET', `/scenarios/${STATE.current.id}/business-process`); } catch (_) {}
  STATE.businessProcess = (bp && bp.markdown) ? bp : null;
  renderBusinessProcess();
}

function renderBusinessProcess() {
  const el = document.getElementById('business-process-detail');
  if (!el) return;
  const bp = STATE.businessProcess;
  if (!bp || !bp.markdown) {
    el.innerHTML = '<div class="muted-tip">梳理业务流程后显示。对 AI 说「梳理业务流程」生成文档。</div>';
    return;
  }
  const badge = bp.approved
    ? '<span class="bp-badge approved">已批准 ✓</span>'
    : '<span class="bp-badge pending">待审批</span>';
  const actions = bp.approved
    ? '<button class="btn btn-ghost" onclick="approveProcess(false)">撤销批准</button>'
    : `<button class="btn btn-primary" onclick="approveProcess(true)">✅ 批准</button>
       <button class="btn btn-ghost" onclick="sendQuick('梳理业务流程')">重新梳理</button>`;
  el.innerHTML = `<div class="bp-approval">${badge}<span style="flex:1"></span>${actions}</div>
    <div class="bp-doc">${renderMarkdown(bp.markdown)}</div>`;
}

async function approveProcess(approved, feedback) {
  if (!STATE.current) return;
  try {
    await http('POST', `/scenarios/${STATE.current.id}/business-process/approve`,
      { approved: !!approved, feedback: feedback || '' });
    STATE.current = await http('GET', '/scenarios/' + STATE.current.id);
    updateStatusBadge(STATE.current.status);
    await loadBusinessProcess();
    renderGuide(); renderScenarioList();
    addAIBubble(approved
      ? '✅ 业务流程文档已批准（Phase 0 完成）。下一步可说「<strong>推导关联关系</strong>」进入 Phase 1。'
      : '↩️ 已撤销批准。可「重新梳理」业务流程，或补充修改意见。');
  } catch (e) { addAIBubble('❌ 审批失败：' + esc(e.message)); }
}

/* 结构化交互（Section 5）：把 interaction 块渲染成表单 */
function renderInteraction(node, it) {
  if (!it || !node) return;
  const box = document.createElement('div');
  box.className = 'interaction-block';
  const opts = (it.options || []).map((o, i) =>
    `<button class="interaction-opt" data-i="${i}">${esc(o)}</button>`).join('');
  box.innerHTML = `<div class="interaction-q">🧩 ${esc(it.question || '请选择')}</div>
    <div class="interaction-opts">${opts}</div>
    ${it.allow_custom ? `<div class="interaction-custom">
      <input placeholder="或输入你的回应…">
      <button class="btn btn-ghost" data-send="1">发送</button></div>` : ''}`;
  node.body.appendChild(box);

  box.querySelectorAll('.interaction-opt').forEach(btn => {
    btn.onclick = () => answerInteraction(box, it, (it.options[+btn.dataset.i] || ''), +btn.dataset.i);
  });
  const sendBtn = box.querySelector('[data-send]');
  if (sendBtn) {
    const inp = box.querySelector('.interaction-custom input');
    sendBtn.onclick = () => { const v = inp.value.trim(); if (v) answerInteraction(box, it, v, -1); };
    inp.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); sendBtn.click(); } });
  }
}

async function answerInteraction(box, it, text, idx) {
  box.classList.add('answered');
  // Phase 0 审批：首个选项＝批准（走 REST，不当作普通对话）
  if (it.context === 'phase0_approval' && idx === 0) {
    await approveProcess(true);
    return;
  }
  if (!text) return;
  addUserBubble(esc(text));
  await streamChat(text);
}

/* ----------------------------------------------------------------- 消息 DOM */
function clearMessages() { document.getElementById('chat-messages').innerHTML = ''; }
function scrollChat() { const c = document.getElementById('chat-messages'); c.scrollTop = c.scrollHeight; }

function addUserBubble(html) {
  const c = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'msg user';
  div.innerHTML = `<div class="msg-avatar user-avatar">我</div><div class="msg-body"><div class="msg-bubble">${html}</div></div>`;
  c.appendChild(div); scrollChat();
}

function addAIBubble(html) {
  const c = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'msg ai';
  div.innerHTML = `<div class="msg-avatar ai-avatar">AI</div><div class="msg-body"><div class="msg-bubble">${html}</div></div>`;
  c.appendChild(div); scrollChat();
}

/* 创建一个可流式更新的 AI 消息节点，返回操作句柄 */
function addAIStreaming() {
  const c = document.getElementById('chat-messages');
  const wrap = document.createElement('div');
  wrap.className = 'msg ai';
  wrap.innerHTML = `<div class="msg-avatar ai-avatar">AI</div><div class="msg-body"></div>`;
  c.appendChild(wrap);
  const body = wrap.querySelector('.msg-body');

  const content = document.createElement('div');
  content.className = 'msg-bubble';

  const node = {
    body, content,
    thinkBlock: null, thinkBody: null, _activeTool: null,
    ensureThink() {
      if (this.thinkBlock) return;
      const block = document.createElement('div');
      block.className = 'think-block';
      block.innerHTML = `<div class="think-head" onclick="this.parentNode.classList.toggle('collapsed')">
        🧠 思考过程 <span class="chev">▾</span></div><div class="think-body"></div>`;
      this.thinkBlock = block;
      this.thinkBody = block.querySelector('.think-body');
      body.insertBefore(block, content.parentNode ? content : null);
      if (!content.parentNode) body.appendChild(content);
    },
    collapseThink() { if (this.thinkBlock) this.thinkBlock.classList.add('collapsed'); },
    addTool(name, args) {
      const t = document.createElement('div');
      t.className = 'tool-trace';
      t.innerHTML = `<div class="tool-trace-head"><span class="tool-spinner"></span>
        正在调用：${esc(name)}</div>
        <div class="tool-trace-args">${esc(args || '')}</div>`;
      body.insertBefore(t, content.parentNode ? content : null);
      if (!content.parentNode) body.appendChild(content);
      return {
        el: t,
        done(result) {
          t.querySelector('.tool-trace-head').innerHTML = `✅ 已完成：${esc(name)}`;
          if (result) {
            const r = document.createElement('div');
            r.className = 'tool-trace-result';
            r.textContent = result;
            t.appendChild(r);
          }
        }
      };
    }
  };
  body.appendChild(content);
  scrollChat();
  return node;
}

/* ----------------------------------------------------------------- 详情渲染 */
function renderDetails() {
  const sc = STATE.current;
  if (!sc) return;

  document.getElementById('tables-detail').innerHTML = (sc.tables_meta || []).map(t => `
    <div class="table-card">
      <div class="table-card-header">
        <span class="table-card-name">📊 ${esc(t.table_name)}</span>
        <span class="table-card-meta">${(t.row_count || 0).toLocaleString()} 行 · ${t.col_count} 列</span>
        ${t.header_row > 0 ? `<span class="table-card-meta" style="color:var(--amber)" title="已自动跳过上方 ${t.header_row} 行标题/空行">表头@第${t.header_row + 1}行</span>` : ''}
      </div>
      <div class="col-chips">${(t.columns || []).map(col =>
        `<span class="col-chip">${esc(col.name)}<span style="color:var(--muted);margin-left:2px">${esc(col.dtype)}</span></span>`).join('')}</div>
    </div>`).join('') || '<div class="muted-tip">暂无表格，请上传业务数据。</div>';

  const rels = (sc.relations && sc.relations.relations) || [];
  document.getElementById('relations-detail').innerHTML = rels.map(r => `
    <div class="relation-card">
      <span class="rel-from">${esc(r.from_table)}.${esc(r.from_column)}</span>
      <span class="rel-arrow">→</span>
      <span class="rel-to">${esc(r.to_table)}.${esc(r.to_column)}</span>
      <span style="font-size:10px;color:var(--muted)">${esc(r.relation_type)}</span>
      <span class="rel-conf">${Math.round((r.confidence || 0) * 100)}%</span>
    </div>`).join('') || '<div class="muted-tip">推导关联关系后显示。</div>';

  const steps = (sc.flow && sc.flow.flow_steps) || [];
  document.getElementById('flow-detail').innerHTML = steps.map(s => `
    <div class="flow-step-card">
      <div class="flow-step-header">
        <div class="flow-step-num">${s.step_id}</div>
        <div class="flow-step-name">${esc(s.step_name)}</div>
        <div class="flow-step-op">${esc(s.operation)}</div>
      </div>
      <div class="flow-step-desc">${esc(s.description)}</div>
      ${s.pseudo_sql ? `<div class="flow-sql">${esc(s.pseudo_sql)}</div>` : ''}
    </div>`).join('') || '<div class="muted-tip">推导业务流程后显示。</div>';

  document.getElementById('skills-detail').innerHTML = (sc.skills || []).map(s => `
    <div class="skill-card ${s.is_evolved ? 'evolved' : ''}">
      <div class="skill-card-header">
        <span>${s.is_main ? '⚙️' : s.is_evolved ? '🧬' : '🔧'}</span>
        <span class="skill-name">${esc(s.name)}</span>
        <span class="skill-op">${esc(s.operation)}</span>
      </div>
      <div class="skill-desc">${esc(s.description)}</div>
    </div>`).join('') || '<div class="muted-tip">生成技能库后显示。</div>';

  renderScenarioList();
}

/* ----------------------------------------------------------------- 图谱 / 图表 */
let _mermaidReady = false;
function initMermaid() {
  if (_mermaidReady || typeof mermaid === 'undefined') return;
  try {
    mermaid.initialize({ startOnLoad: false, theme: 'dark', securityLevel: 'loose' });
    _mermaidReady = true;
  } catch (_) {}
}

function diagramAvailability() {
  const sc = STATE.current;
  return {
    business: !!(sc && sc.business_process && sc.business_process.mermaid),
    relations: !!(sc && sc.relations && (sc.relations.graph_data.nodes || []).length),
    flow: !!(sc && sc.flow && (sc.flow.flow_graph.nodes || []).length),
  };
}

function refreshDiagramToolbar() {
  const av = diagramAvailability();
  ['business', 'relations', 'flow'].forEach(m => {
    const el = document.getElementById('dg-' + m);
    if (el) el.style.display = av[m] ? '' : 'none';
  });
  if (!av[STATE.diagramMode]) {
    STATE.diagramMode = av.business ? 'business' : av.relations ? 'relations' : av.flow ? 'flow' : 'business';
  }
  ['business', 'relations', 'flow'].forEach(m => {
    const el = document.getElementById('dg-' + m);
    if (el) el.classList.toggle('active', STATE.diagramMode === m);
  });
}

function setDiagram(mode) { STATE.diagramMode = mode; renderDiagram(); }

async function renderDiagram() {
  refreshDiagramToolbar();
  const sc = STATE.current;
  const empty = document.getElementById('diagram-empty');
  const host = document.getElementById('mermaid-host');
  const svg = document.getElementById('graph-svg');
  if (!empty || !host || !svg) return;
  host.style.display = 'none'; svg.style.display = 'none';
  resetView();
  const av = diagramAvailability();
  if (!sc || !av[STATE.diagramMode]) { empty.style.display = 'flex'; return; }
  empty.style.display = 'none';
  if (STATE.diagramMode === 'business') {
    await renderMermaidInto(host, sc.business_process.mermaid);
    host.style.display = 'block';
  } else {
    const graph = STATE.diagramMode === 'flow' ? sc.flow.flow_graph : sc.relations.graph_data;
    svg.style.display = 'block';
    drawGraph(svg, graph.nodes, graph.edges);
  }
}

async function renderMermaidInto(host, code) {
  initMermaid();
  if (typeof mermaid === 'undefined' || !_mermaidReady) {
    host.innerHTML = '<pre style="color:var(--text-secondary);padding:10px">（Mermaid 库未加载，显示源码）\n\n' + esc(code) + '</pre>';
    return;
  }
  try {
    const { svg } = await mermaid.render('mmd_' + Date.now(), code);
    host.innerHTML = svg;
  } catch (e) {
    host.innerHTML = '<pre style="color:var(--rose);padding:10px">Mermaid 渲染失败：' + esc(e.message) + '\n\n' + esc(code) + '</pre>';
  }
}

/* 缩放 / 平移 */
const VIEW = { scale: 1, x: 12, y: 12, drag: false, sx: 0, sy: 0 };
function applyView() {
  const pan = document.getElementById('diagram-pan');
  if (pan) pan.style.transform = `translate(${VIEW.x}px,${VIEW.y}px) scale(${VIEW.scale})`;
}
function resetView() { VIEW.scale = 1; VIEW.x = 12; VIEW.y = 12; applyView(); }
function setupDiagramPan() {
  const stage = document.getElementById('diagram-stage');
  if (!stage) return;
  stage.addEventListener('wheel', e => {
    e.preventDefault();
    const f = e.deltaY < 0 ? 1.1 : 0.9;
    const rect = stage.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    VIEW.x = mx - (mx - VIEW.x) * f; VIEW.y = my - (my - VIEW.y) * f;
    VIEW.scale = Math.min(4, Math.max(0.2, VIEW.scale * f));
    applyView();
  }, { passive: false });
  stage.addEventListener('mousedown', e => {
    VIEW.drag = true; VIEW.sx = e.clientX - VIEW.x; VIEW.sy = e.clientY - VIEW.y;
    stage.classList.add('grabbing');
  });
  window.addEventListener('mousemove', e => {
    if (!VIEW.drag) return;
    VIEW.x = e.clientX - VIEW.sx; VIEW.y = e.clientY - VIEW.sy; applyView();
  });
  window.addEventListener('mouseup', () => {
    VIEW.drag = false; stage.classList.remove('grabbing');
  });
}

function drawGraph(svg, nodes, edges) {
  const cols = Math.ceil(Math.sqrt(nodes.length));
  const nodeW = 140, nodeH = 44;
  const gapX = 60, gapY = 70;
  const rows = Math.ceil(nodes.length / cols);
  const padX = 20, padY = 30;
  const totalW = cols * nodeW + (cols - 1) * gapX + padX * 2;
  const totalH = rows * nodeH + (rows - 1) * gapY + padY * 2;
  svg.setAttribute('width', totalW);
  svg.setAttribute('height', totalH);

  const pos = {};
  nodes.forEach((n, i) => {
    const c = i % cols, r = Math.floor(i / cols);
    pos[n.id] = { x: padX + c * (nodeW + gapX) + nodeW / 2, y: padY + r * (nodeH + gapY) + nodeH / 2 };
  });

  const colors = {
    input: ['rgba(88,200,227,.1)', '#58c8e3'], output: ['rgba(57,214,131,.1)', '#39d683'],
    process: ['rgba(139,110,245,.1)', '#8b6ef5'], result: ['rgba(57,214,131,.1)', '#39d683'],
    rule: ['rgba(245,166,35,.1)', '#f5a623'], table: ['rgba(88,200,227,.07)', '#2a8ca3'],
  };

  let s = `<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
    <path d="M0,0 L0,6 L8,3 z" fill="#3d5280"/></marker></defs>`;

  edges.forEach(e => {
    const a = pos[e.source], b = pos[e.target];
    if (!a || !b) return;
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    s += `<path d="M${a.x},${a.y} Q${mx},${my - 24} ${b.x},${b.y}" fill="none" stroke="#3d5280"
      stroke-width="1.5" stroke-dasharray="5,3" marker-end="url(#arrow)" opacity=".75"/>`;
    if (e.label) s += `<text x="${mx}" y="${my - 8}" text-anchor="middle" fill="#8b99b0" font-size="9">${escSvg(e.label)}</text>`;
  });

  nodes.forEach(n => {
    const p = pos[n.id]; if (!p) return;
    const [fill, stroke] = colors[n.type] || colors.table;
    s += `<rect x="${p.x - nodeW / 2}" y="${p.y - nodeH / 2}" width="${nodeW}" height="${nodeH}" rx="8"
      fill="${fill}" stroke="${stroke}" stroke-width="1.5"/>
      <text x="${p.x}" y="${p.y - 4}" text-anchor="middle" fill="#e6edf3" font-size="11" font-weight="600">${escSvg(n.label)}</text>
      <text x="${p.x}" y="${p.y + 11}" text-anchor="middle" fill="${stroke}" font-size="9">${escSvg(n.type || 'table')}</text>`;
  });

  svg.innerHTML = s;
}

/* ----------------------------------------------------------------- 技能进化 */
function openEvolveModal() {
  if (!STATE.current) { alert('请先选择业务场景'); return; }
  document.getElementById('evolve-skill-name').value = '';
  document.getElementById('evolve-skill-desc').value = '';
  showModal('modal-evolve');
}
async function evolveSkill() {
  const name = document.getElementById('evolve-skill-name').value.trim();
  const description = document.getElementById('evolve-skill-desc').value.trim();
  if (!name || !description) { alert('请填写技能名称和描述'); return; }
  try {
    await http('POST', `/scenarios/${STATE.current.id}/skills/evolve`, { name, description });
    closeModal('modal-evolve');
    STATE.current = await http('GET', '/scenarios/' + STATE.current.id);
    renderDetails();
    addAIBubble(`🧬 进化技能「<strong>${esc(name)}</strong>」已加入技能库。`);
  } catch (e) { alert('添加失败：' + e.message); }
}

/* ----------------------------------------------------------------- Tab 切换（中央面板） */
function switchTab(tab) {
  document.querySelectorAll('.center-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.center-content').forEach(c => c.style.display = 'none');
  const el = document.getElementById('tab-' + tab);
  if (el) el.style.display = (tab === 'flow') ? 'flex' : 'block';
  if (tab === 'flow') renderDiagram();
}

/* ----------------------------------------------------------------- 工具函数 */
function showModal(id) { document.getElementById(id).classList.add('show'); }
function closeModal(id) { document.getElementById(id).classList.remove('show'); }
document.querySelectorAll('.modal-overlay').forEach(m =>
  m.addEventListener('click', e => { if (e.target === m) m.classList.remove('show'); }));

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function escSvg(s) { return esc(s); }

/* 轻量 Markdown：支持标题、有序/无序列表、代码块、引用、行内代码与粗体（全程转义，安全） */
function mdInline(s) {
  let t = esc(s);
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  return t;
}
function renderMarkdown(text) {
  const lines = String(text == null ? '' : text).split('\n');
  let html = '', inCode = false, codeBuf = [], list = null;
  const flush = () => { if (list) { html += `</${list}>`; list = null; } };
  for (const raw of lines) {
    if (raw.trim().startsWith('```')) {
      if (!inCode) { flush(); inCode = true; codeBuf = []; }
      else { inCode = false; html += `<pre><code>${esc(codeBuf.join('\n'))}</code></pre>`; }
      continue;
    }
    if (inCode) { codeBuf.push(raw); continue; }
    const h = raw.match(/^(#{1,3})\s+(.*)$/);
    if (h) { flush(); const n = h[1].length; html += `<h${n}>${mdInline(h[2])}</h${n}>`; continue; }
    const ol = raw.match(/^\s*\d+\.\s+(.*)$/);
    if (ol) { if (list !== 'ol') { flush(); html += '<ol>'; list = 'ol'; } html += `<li>${mdInline(ol[1])}</li>`; continue; }
    const ul = raw.match(/^\s*[-*]\s+(.*)$/);
    if (ul) { if (list !== 'ul') { flush(); html += '<ul>'; list = 'ul'; } html += `<li>${mdInline(ul[1])}</li>`; continue; }
    flush();
    const bq = raw.match(/^>\s?(.*)$/);
    if (bq) { html += `<div class="muted-tip">${mdInline(bq[1])}</div>`; continue; }
    if (raw.trim() === '---') { html += '<hr style="border:none;border-top:1px solid var(--border);margin:8px 0">'; continue; }
    if (raw.trim() === '') { html += '<br>'; continue; }
    html += `<div>${mdInline(raw)}</div>`;
  }
  flush();
  if (inCode) html += `<pre><code>${esc(codeBuf.join('\n'))}</code></pre>`;
  return html;
}
