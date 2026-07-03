/* =========================================================================
 * 业务流逆向工程平台 —— 前端逻辑（v1.0.3：5 步流程，上传时选角色）
 *
 * 工作流（5 步）：
 *   ① 选文件 → 选角色 → 上传（合并为单个动作）
 *   ② 推导关联（含字段语义）
 *   ③ 推导业务流程（节点带能力描述 + 规则结构映射）
 *   ④ 生成技能（按节点出技能 + 主技能）
 *   ⑤ 执行（含校验）
 * ========================================================================= */

'use strict';

const API = '/api';

const STATE = {
  scenarios: [],
  current: null,
  diagramMode: 'flow',          // relations | flow（v1.0.3：业务流程图替代旧 business 图）
  streaming: false,
  outputs: null,
  validations: [],
  pendingFiles: null,           // 上传待确认的文件列表
  pendingRoles: {},             // {filename: role}
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
      : ['tables_uploaded', 'relations_deduced', 'flow_deduced'].includes(sc.status) ? 'deducing' : '';
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
  await loadOutputs();
  refreshDiagramToolbar();
  renderDiagram();
  renderGuide();
}

/* ----------------------------------------------------------------- 5 步引导 */
const WORKFLOW = [
  { key: 'upload', icon: '📎', label: '① 上传(含角色)', done: sc => (sc.tables_meta || []).length > 0,
    hint: '选择文件 → 为每个文件选角色（输入/规则/结果）→ 上传。',
    go: '选择文件', act: () => document.getElementById('file-input').click() },
  { key: 'relations', icon: '🔗', label: '② 推导关联+语义', done: sc => !!sc.relations,
    hint: '推导表关联（ER）+ 每个字段的业务语义（PK/FK/DIM/METRIC/TIME/NL_TEXT/CATEGORY）。',
    go: '推导关联', act: () => sendQuick('推导关联关系（含字段语义）') },
  { key: 'flow', icon: '🔄', label: '③ 推导流程', done: sc => !!(sc.flow && (sc.flow.flow_steps || []).length),
    hint: '每个节点带「该做什么/能做什么/数据怎么变化/模板算子」描述；若有规则表则附带规则结构映射。',
    go: '推导业务流程', act: () => sendQuick('推导业务流程') },
  { key: 'skills', icon: '⚡', label: '④ 生成技能', done: sc => (sc.skills || []).length > 0,
    hint: '每个流程节点 → 一个节点子技能；外加主技能统一调度（list_outputs / produce）。',
    go: '生成技能', act: () => sendQuick('生成技能库') },
  { key: 'execute', icon: '▶', label: '⑤ 执行+校验', done: sc => (sc.validations || []).some(v => v.passed) || sc.status === 'active',
    hint: '在完整数据上执行、复刻产出文件，并与历史结果对照校验。',
    go: '执行产出', act: () => sendQuick('执行产出并对照历史结果') },
];

function renderGuide() {
  const el = document.getElementById('workflow-guide');
  if (!el) return;
  const sc = STATE.current;
  if (!sc) { el.style.display = 'none'; return; }
  el.style.display = 'block';

  const states = WORKFLOW.map(s => s.done(sc));
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
    ? `<span>🎉 全部完成：可对新数据 <b>produce(产出, 数据)</b> 复刻并输出文件。</span>
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

/* ----------------------------------------------------------------- 产出 */
async function loadOutputs() {
  if (!STATE.current) return;
  try {
    const [og, vs] = await Promise.all([
      http('GET', `/scenarios/${STATE.current.id}/outputs`),
      http('GET', `/scenarios/${STATE.current.id}/validations`),
    ]);
    STATE.outputs = og;
    STATE.validations = vs || [];
  } catch (_) { STATE.outputs = null; STATE.validations = []; }
  renderOutputs();
}

const STATE_LABEL = {
  verified: '已校验✓', executable: '可执行', blocked: '缺数据/口径', draft: '骨架(待细化)',
};
function renderOutputs() {
  const og = STATE.outputs;
  const typesEl = document.getElementById('outputs-detail');
  if (typesEl) {
    const items = (og && og.outputs) || [];
    typesEl.innerHTML = items.length ? items.map(o => {
      const idSafe = esc(o.output_id).replace(/'/g, "\\'");
      const btn = o.has_sql
        ? `<span class="btn-run-audit" onclick="runProduce('${idSafe}')">▶ ${o.result_table ? '校验' : '执行'}</span>`
        : '';
      const cols = (o.columns || []).slice(0, 8).join('、') + ((o.columns || []).length > 8 ? '…' : '');
      return `
      <div class="audit-card">
        <div class="audit-card-head">
          <span class="audit-vt">${esc(o.name)}</span>
          <span class="audit-state st-${o.status}">${STATE_LABEL[o.status] || o.status}</span>
          <span class="audit-state st-executable">📄 ${esc(o.fmt)}</span>
          ${btn}
        </div>
        <div class="audit-meta">策略 ${esc(o.strategy || '—')}${o.pipeline_steps ? ' · 管线 ' + o.pipeline_steps + ' 节点' : ''}${o.result_table ? ' · 历史：' + esc(o.result_table) : ''}</div>
        <div class="audit-meta">输出列：${esc(cols) || '（未定）'}</div>
        ${(o.external_data_needed || []).length ? `<div class="audit-meta" style="color:var(--amber)">缺：${esc(o.external_data_needed.join('、'))}</div>` : ''}
      </div>`;
    }).join('') : '<div class="muted-tip">推导业务流程并生成技能后显示。先把历史结果样例文件上传时选择「结果」角色。</div>';
  }
  const vEl = document.getElementById('validations-detail');
  if (vEl) {
    vEl.innerHTML = (STATE.validations || []).length ? STATE.validations.map(v => {
      const statusText = v.error ? '❌ 执行失败' : (v.passed ? '✅ 已产出' : '⚠️ 结果为 0 行');
      const dl = v.artifact_url
        ? `<a class="dl-link" href="${esc(v.artifact_url)}" target="_blank" download>⬇ 下载产出文件</a>` : '';
      return `
      <div class="valid-card ${v.passed ? 'pass' : (v.error ? 'fail' : 'fail')}">
        <div><strong>${esc(v.output_name || v.output_id)}</strong> ${statusText}
          <span class="valid-count">${v.produced_count != null ? `共 ${v.produced_count} 行` : ''}</span>${dl}</div>
        ${v.error ? `<div class="valid-stat">${esc(v.error)}</div>` : ''}
      </div>`;
    }).join('') : '<div class="muted-tip">执行产出后显示（含产出文件下载）。</div>';
  }
}

async function runProduce(outputId) {
  if (!STATE.current) return;
  addUserBubble('执行产出：' + esc(outputId));
  const node = addAIStreaming();
  node.content.innerHTML = '<span class="typing-dots"><span></span><span></span><span></span></span>';
  try {
    const rep = await http('POST', `/scenarios/${STATE.current.id}/produce`, { output_id: outputId });
    await loadOutputs();
    STATE.current = await http('GET', '/scenarios/' + STATE.current.id);
    updateStatusBadge(STATE.current.status);
    const dl = rep.artifact_url ? `\n\n[⬇ 下载产出文件](${rep.artifact_url})` : '';
    const head = rep.error ? '❌ 执行失败' : (rep.passed ? '✅ 已产出' : '⚠️ 结果为 0 行');
    node.content.innerHTML = renderMarkdown(
      `**${esc(rep.output_name || outputId)}** 执行完成：${head}\n\n${rep.message}${dl}`);
    switchTab('outputs');
  } catch (e) {
    node.content.innerHTML = '❌ 执行失败：' + esc(e.message);
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
    点击下方上传区，<strong>选择文件 → 为每个文件选角色（输入/规则/结果）→ 上传</strong>。<br>
    上传完成后对我说「<strong>推导关联关系</strong>」开始第二步。`);
}

async function deleteScenario(id) {
  if (!confirm('确定删除该业务场景及其全部数据？此操作不可恢复。')) return;
  await http('DELETE', '/scenarios/' + id);
  if (STATE.current && STATE.current.id === id) { STATE.current = null; resetWorkspace(); }
  await loadScenarios();
}

function deleteCurrentScenario() { if (STATE.current) deleteScenario(STATE.current.id); }

/* 事后修正表角色（一般上传时已选） */
async function setTableRole(tableName, role) {
  if (!STATE.current) return;
  try {
    await http('PUT', `/scenarios/${STATE.current.id}/tables/${encodeURIComponent(tableName)}/role`, { role });
    STATE.current = await http('GET', '/scenarios/' + STATE.current.id);
    renderDetails();
    await loadOutputs();
    renderGuide();
  } catch (e) { addAIBubble('❌ 设置角色失败：' + esc(e.message)); }
}

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
  relations_deduced: '关联+语义已推导',
  flow_deduced: '流程已推导',
  skills_generated: '技能已生成', active: '运行中'
};
function updateStatusBadge(status) {
  const badge = document.getElementById('topbar-status');
  badge.className = 'status-badge status-' + status;
  badge.textContent = STATUS_LABEL[status] || status;
  if (STATE.current) { STATE.current.status = status; renderGuide(); }
}

/* ----------------------------------------------------------------- 文件上传（v1.0.3：选文件→选角色→上传） */
function handleFileSelect(files) {
  if (!STATE.current) { addAIBubble('⚠️ 请先创建或选择一个业务场景，再上传表格。'); return; }
  if (!files || !files.length) return;
  STATE.pendingFiles = Array.from(files);
  STATE.pendingRoles = {};
  const list = document.getElementById('upload-role-list');
  list.innerHTML = STATE.pendingFiles.map((f, i) => {
    const guess = guessRole(f.name);
    STATE.pendingRoles[f.name] = guess;
    const opt = (v, label) => `<option value="${v}" ${guess === v ? 'selected' : ''}>${label}</option>`;
    return `<div class="role-pick-row">
      <span class="role-pick-name">📄 ${esc(f.name)}</span>
      <select class="role-pick-select" data-fname="${esc(f.name)}" onchange="onRolePick(this)">
        ${opt('input', '业务输入')}${opt('rule', '规则/标准')}${opt('result', '历史结果')}
      </select>
    </div>`;
  }).join('');
  showModal('modal-upload-roles');
}

function guessRole(filename) {
  const lower = filename.toLowerCase();
  if (/(rule|规则|标准|清单|口径)/.test(filename) || /rule/.test(lower)) return 'rule';
  if (/(result|结果|产出|违规|问题)/.test(filename) || /result/.test(lower)) return 'result';
  return 'input';
}

function onRolePick(sel) {
  const name = sel.dataset.fname;
  STATE.pendingRoles[name] = sel.value;
}

function cancelUpload() {
  closeModal('modal-upload-roles');
  document.getElementById('file-input').value = '';
  STATE.pendingFiles = null;
  STATE.pendingRoles = {};
}

async function confirmUpload() {
  if (!STATE.pendingFiles || !STATE.pendingFiles.length) { closeModal('modal-upload-roles'); return; }
  closeModal('modal-upload-roles');
  const files = STATE.pendingFiles;
  const roleMap = {...STATE.pendingRoles};
  STATE.pendingFiles = null;
  STATE.pendingRoles = {};

  const form = new FormData();
  files.forEach(f => form.append('files', f));
  form.append('roles', JSON.stringify(roleMap));

  addUserBubble('上传表格：' + files.map(f => `${f.name}(${roleMap[f.name] || '?'})`).join('、'));
  const node = addAIStreaming();
  node.content.innerHTML = '<span class="typing-dots"><span></span><span></span><span></span></span>';
  try {
    const r = await http('POST', `/scenarios/${STATE.current.id}/uploads`, form, true);
    STATE.current = await http('GET', '/scenarios/' + STATE.current.id);
    updateStatusBadge(STATE.current.status);
    renderDetails(); renderScenarioList(); await loadOutputs(); renderGuide();
    const rows = (r.tables_meta || []).map(t =>
      `📊 <strong>${esc(t.table_name)}</strong>[${esc(t.role)}]：${t.row_count.toLocaleString()} 行 × ${t.col_count} 列`).join('<br>');
    node.content.innerHTML = `✅ 已上传 ${r.tables_meta.length} 张表（含角色）：<br><br>${rows}<br><br>
      下一步：对我说「<strong>推导关联关系</strong>」（会一并推断字段的业务语义）。`;
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
    addAIBubble('👋 你好！业务流逆向工程助手就绪。<br>5 步流程：① 上传(选角色) → ② 推关联+字段语义 → ③ 推流程(节点带能力) → ④ 生成技能 → ⑤ 执行+校验。');
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

async function onResourceRefresh(resource) {
  if (!STATE.current) return;
  STATE.current = await http('GET', '/scenarios/' + STATE.current.id);
  renderDetails();
  if (resource === 'relations') STATE.diagramMode = 'relations';
  if (resource === 'flow') STATE.diagramMode = 'flow';
  if (resource === 'tables') { renderDetails(); }
  if (['outputs', 'validations'].includes(resource)) { await loadOutputs(); }
  refreshDiagramToolbar();
  if (resource === 'flow') {
    switchTab('flow');   // 自动切到流程图谱 tab，switchTab 内部已调用 renderDiagram()
  } else if (resource === 'skills') {
    switchTab('skills'); // 自动切到技能 tab
  } else {
    renderDiagram();
  }
  renderScenarioList();
  renderGuide();
}

/* 结构化交互：把 interaction 块渲染成表单 */
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

/* ----------------------------------------------------------------- 详情渲染（v1.0.3：字段语义 + 节点能力） */
function renderDetails() {
  const sc = STATE.current;
  if (!sc) return;

  document.getElementById('tables-detail').innerHTML = (sc.tables_meta || []).map(t => {
    const nameSafe = esc(t.table_name).replace(/'/g, "\\'");
    const role = t.role || 'unknown';
    const opt = (v, label) => `<option value="${v}" ${role === v ? 'selected' : ''}>${label}</option>`;
    const roleSel = `<select class="role-select role-${role}" title="表角色（上传时已选，事后修正用）"
        onchange="setTableRole('${nameSafe}', this.value)">
        ${opt('unknown', '· 未标注')}${opt('input', '输入表')}${opt('rule', '规则表')}${opt('result', '结果表')}
      </select>`;
    return `
    <div class="table-card">
      <div class="table-card-header">
        <span class="table-card-name">📊 ${esc(t.table_name)}</span>
        ${roleSel}
        <span class="table-card-meta">${(t.row_count || 0).toLocaleString()} 行 · ${t.col_count} 列</span>
        ${t.header_row > 0 ? `<span class="table-card-meta" style="color:var(--amber)">表头@第${t.header_row + 1}行</span>` : ''}
      </div>
      <div class="col-chips">${(t.columns || []).map(col => {
        const role = col.semantic_role || 'UNKNOWN';
        const sem = col.semantic && col.semantic !== col.name ? `<span class="col-chip-sem">· ${esc(col.semantic)}</span>` : '';
        return `<span class="col-chip role-${role}" title="${esc(col.semantic || '')} (${role}, ${esc(col.dtype || '')})">${esc(col.name)}${sem}</span>`;
      }).join('')}</div>
    </div>`;
  }).join('') || '<div class="muted-tip">暂无表格，请上传业务数据（上传时选择角色）。</div>';

  const rels = (sc.relations && sc.relations.relations) || [];
  document.getElementById('relations-detail').innerHTML = rels.map(r => {
    const fromCols = (r.from_columns && r.from_columns.length > 1) ? r.from_columns.join('+') : r.from_column;
    const toCols = (r.to_columns && r.to_columns.length > 1) ? r.to_columns.join('+') : r.to_column;
    const payload = JSON.stringify({
      from_table: r.from_table, from_column: r.from_column, to_table: r.to_table, to_column: r.to_column,
      from_columns: r.from_columns || [r.from_column], to_columns: r.to_columns || [r.to_column],
      relation_type: r.relation_type,
    }).replace(/'/g, '&#39;').replace(/"/g, '&quot;');
    return `
    <div class="relation-card ${r.confirmed ? 'confirmed' : ''}">
      <span class="rel-from">${esc(r.from_table)}.${esc(fromCols)}</span>
      <span class="rel-arrow">→</span>
      <span class="rel-to">${esc(r.to_table)}.${esc(toCols)}</span>
      <span style="font-size:10px;color:var(--muted)">${esc(r.relation_type)}</span>
      <span class="rel-conf">${Math.round((r.confidence || 0) * 100)}%</span>
      ${r.confirmed
        ? `<span class="rel-confirmed-badge">✓ 已人工确认</span>
           <span class="rel-confirm-btn" onclick='toggleRelationConfirm(${payload}, false)'>取消确认</span>`
        : `<span class="rel-confirm-btn" onclick='toggleRelationConfirm(${payload}, true)'>✓ 确认此关联</span>`}
    </div>`;
  }).join('') || '<div class="muted-tip">推导关联关系后显示。</div>';

  const steps = (sc.flow && sc.flow.flow_steps) || [];
  document.getElementById('flow-detail').innerHTML = steps.map(s => {
    const statusCls = s.status === 'blocked' ? 'blocked' : (s.status === 'executable' ? '' : 'draft');
    return `
    <div class="flow-step-card">
      <div class="flow-step-header">
        <div class="flow-step-num">${s.step_id}</div>
        <div class="flow-step-name">${esc(s.step_name)}</div>
        <div class="flow-step-op">${esc(s.operation || '')}</div>
        ${s.template_kind ? `<div class="flow-step-tpl">${esc(s.template_kind)}</div>` : ''}
        <div class="flow-step-status ${statusCls}">${esc(s.status || 'draft')}</div>
      </div>
      <div class="flow-cap-grid">
        ${s.purpose ? `<div class="flow-cap-key">该做什么</div><div class="flow-cap-val">${esc(s.purpose)}</div>` : ''}
        ${s.capability ? `<div class="flow-cap-key">能做什么</div><div class="flow-cap-val">${esc(s.capability)}</div>` : ''}
        ${(s.data_in || []).length ? `<div class="flow-cap-key">数据输入</div><div class="flow-cap-val">${(s.data_in || []).map(esc).join('<br>')}</div>` : ''}
        ${(s.data_out || []).length ? `<div class="flow-cap-key">数据输出</div><div class="flow-cap-val">${(s.data_out || []).map(esc).join('<br>')}</div>` : ''}
        ${(s.external_data_needed || []).length ? `<div class="flow-cap-key" style="color:var(--amber)">缺</div><div class="flow-cap-val" style="color:var(--amber)">${(s.external_data_needed || []).map(esc).join('；')}</div>` : ''}
      </div>
      ${s.sql ? `<div class="flow-sql">${esc(s.sql.slice(0, 500))}${s.sql.length > 500 ? '…' : ''}</div>` : ''}
    </div>`;
  }).join('') || '<div class="muted-tip">推导业务流程后显示（节点带能力描述）。</div>';

  document.getElementById('skills-detail').innerHTML = (sc.skills || []).map(s => {
    // const cls = s.is_main ? 'main' : (s.is_evolved ? 'evolved' : '');
    return `
    <div class="skill-card">
      <div class="skill-card-header">
        <span>${s.is_main ? '⚙️' : s.is_evolved ? '🧬' : '🔧'}</span>
        <span class="skill-name">${esc(s.name)}</span>
        <span class="skill-op">${esc(s.operation || '')}</span>
      </div>
      <div class="skill-desc">${esc(s.description || '')}</div>
      ${s.capability ? `<div class="skill-cap">→ ${esc(s.capability)}</div>` : ''}
    </div>`;
  }).join('') || '<div class="muted-tip">生成技能后显示。</div>';

  renderScenarioList();
}

/* ----------------------------------------------------------------- 关联关系人工确认 */
async function toggleRelationConfirm(payload, confirm) {
  if (!STATE.current) return;
  try {
    if (confirm) {
      await http('POST', `/scenarios/${STATE.current.id}/relations/confirm`, payload);
    } else {
      await http('DELETE', `/scenarios/${STATE.current.id}/relations/confirm`, payload);
    }
    STATE.current = await http('GET', '/scenarios/' + STATE.current.id);
    renderDetails();
  } catch (e) {
    alert('操作失败：' + e.message);
  }
}

/* ----------------------------------------------------------------- 追踪驱动采样（因果链核对） */
async function loadTraceSample() {
  if (!STATE.current) return;
  const el = document.getElementById('trace-sample-detail');
  if (!el) return;
  el.innerHTML = '<div class="muted-tip">正在以结果表为入口逆向追踪各表关联行…</div>';
  try {
    const r = await http('GET', `/scenarios/${STATE.current.id}/trace-sample`);
    renderTraceSample(el, r);
  } catch (e) {
    el.innerHTML = `<div class="muted-tip">追踪采样失败：${esc(e.message)}</div>`;
  }
}

function renderTraceSample(el, r) {
  if (r.degraded) {
    el.innerHTML = `<div class="trace-summary-bar" style="color:var(--rose)">⚠️ ${esc(r.trace_summary || '无结果表，已降级为各表独立随机采样，样本间无因果关联')}</div>`;
    return;
  }
  const check = r.connectivity_check || {};
  const checkColor = check.level === 'pass' ? 'var(--green)' : (check.level === 'warning' ? 'var(--amber)' : 'var(--rose)');
  let html = `<div class="trace-summary-bar" style="color:${checkColor}">
    ${check.level === 'pass' ? '✅' : check.level === 'warning' ? '⚠️' : '❌'} ${esc(check.message || r.trace_summary || '')}
  </div>`;

  if ((r.result_sample || []).length) {
    html += `<div class="trace-card" style="border-left-color:var(--green)">
      <div class="trace-card-head">
        <span class="trace-name">🎯 ${esc(r.result_table || '结果表')}</span>
        <span class="trace-badge high">追踪入口（结果行）</span>
      </div>
      <div class="trace-rows">${esc(JSON.stringify(r.result_sample[0], null, 2))}</div>
    </div>`;
  }

  const map = r.trace_map || {};
  const names = Object.keys(map);
  if (!names.length) {
    html += '<div class="muted-tip">没有其它表参与追踪。</div>';
  }
  names.forEach(name => {
    const info = map[name] || {};
    const isRandom = info.matched_by === 'random';
    const rows = info.matched_rows || [];
    html += `<div class="trace-card ${isRandom ? 'random' : ''}">
      <div class="trace-card-head">
        <span class="trace-name">${esc(name)}</span>
        <span class="trace-badge ${isRandom ? 'random' : (info.trace_confidence || 'low')}">${isRandom ? '随机兜底（未追到关联）' : '置信度:' + esc(info.trace_confidence || '?')}</span>
        ${!isRandom ? `<span class="trace-by">关联键：${esc(info.matched_by || '?')}</span>` : ''}
        <span class="trace-by">共 ${rows.length} 行</span>
      </div>
      ${info.warning ? `<div class="trace-warning">${esc(info.warning)}</div>` : ''}
      ${isRandom ? '<div class="trace-warning">这张表没有追到与结果行相关的真实数据，下面只是该表的随机前几行——AI 若据此推导关联/流程，可信度低，请留意结果里对应的待确认问题。</div>' : ''}
      ${rows.length ? `<div class="trace-rows">${esc(JSON.stringify(rows.slice(0, 2), null, 2))}</div>` : ''}
    </div>`;
  });

  const unmatched = r.unmatched_tables || [];
  if (unmatched.length) {
    html += `<div class="muted-tip">完全追不上的表：${esc(unmatched.join('、'))}</div>`;
  }
  el.innerHTML = html;
}

/* ----------------------------------------------------------------- 图谱 */
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
    relations: !!(sc && sc.relations && (sc.relations.graph_data.nodes || []).length),
    flow: !!(sc && sc.flow && ((sc.flow.mermaid && sc.flow.mermaid.trim()) || (sc.flow.flow_graph.nodes || []).length)),
  };
}

function refreshDiagramToolbar() {
  const av = diagramAvailability();
  ['relations', 'flow'].forEach(m => {
    const el = document.getElementById('dg-' + m);
    if (el) el.style.display = av[m] ? '' : 'none';
  });
  if (!av[STATE.diagramMode]) {
    STATE.diagramMode = av.flow ? 'flow' : av.relations ? 'relations' : 'flow';
  }
  ['relations', 'flow'].forEach(m => {
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
  if (STATE.diagramMode === 'flow' && sc.flow && sc.flow.mermaid && sc.flow.mermaid.trim()) {
    await renderMermaidInto(host, sc.flow.mermaid);
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
    host.innerHTML = '<pre style="color:var(--text-secondary);padding:10px">' + esc(code) + '</pre>';
    return;
  }
  try {
    const { svg } = await mermaid.render('mmd_' + Date.now(), code);
    host.innerHTML = svg;
  } catch (e) {
    host.innerHTML = '<pre style="color:var(--rose);padding:10px">Mermaid 渲染失败：' + esc(e.message) + '\n\n' + esc(code) + '</pre>';
  }
}

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

/* ----------------------------------------------------------------- Tab 切换 */
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

function mdInline(s) {
  let t = esc(s);
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/\[([^\]]+)\]\((\/[^)\s]+|https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" class="dl-link" download>$1</a>');
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
