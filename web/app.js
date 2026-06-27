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
  graphMode: 'relations', // relations | flow
  streaming: false,
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
  refreshGraphToolbar();
  renderGraph();
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
    然后对我说「<strong>推导关联关系</strong>」即可开始逆向工程。`);
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
  document.getElementById('graph-toolbar').style.display = 'none';
  updateStatusBadge('created');
  clearMessages();
  document.getElementById('empty-graph').style.display = 'flex';
  document.getElementById('graph-svg').style.display = 'none';
}

/* ----------------------------------------------------------------- 状态徽标 */
const STATUS_LABEL = {
  created: '未开始', tables_uploaded: '表格已上传', relations_deduced: '关系已推导',
  flow_deduced: '流程已推导', skills_generated: '技能已生成', active: '运行中'
};
function updateStatusBadge(status) {
  const badge = document.getElementById('topbar-status');
  badge.className = 'status-badge status-' + status;
  badge.textContent = STATUS_LABEL[status] || status;
  if (STATE.current) STATE.current.status = status;
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
    renderDetails(); renderScenarioList();
    const rows = (r.tables_meta || []).map(t =>
      `📊 <strong>${esc(t.table_name)}</strong>：${t.row_count.toLocaleString()} 行 × ${t.col_count} 列`).join('<br>');
    node.content.innerHTML = `✅ 已上传并扫描 ${r.tables_meta.length} 张表：<br><br>${rows}<br><br>
      切到「表格」标签可看字段详情；或对我说「<strong>推导关联关系</strong>」开始分析。`;
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
    addAIBubble('👋 你好！我是业务流逆向工程助手。上传业务表格后，对我说「推导关联关系」即可开始。');
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
  refreshGraphToolbar();
  if (resource === 'relations') { STATE.graphMode = 'relations'; setGraphActive(); }
  if (resource === 'flow') { STATE.graphMode = 'flow'; setGraphActive(); }
  renderGraph();
  renderScenarioList();
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

/* ----------------------------------------------------------------- 图谱 */
function refreshGraphToolbar() {
  const sc = STATE.current;
  const hasRel = sc && sc.relations && (sc.relations.graph_data.nodes || []).length;
  const hasFlow = sc && sc.flow && (sc.flow.flow_graph.nodes || []).length;
  document.getElementById('graph-toolbar').style.display = (hasRel || hasFlow) ? 'flex' : 'none';
  document.getElementById('toggle-flow').style.display = hasFlow ? '' : 'none';
  document.getElementById('toggle-relations').style.display = hasRel ? '' : 'none';
  if (!hasRel && hasFlow) STATE.graphMode = 'flow';
  setGraphActive();
}
function setGraphActive() {
  document.getElementById('toggle-relations').classList.toggle('active', STATE.graphMode === 'relations');
  document.getElementById('toggle-flow').classList.toggle('active', STATE.graphMode === 'flow');
}
function setGraphMode(mode) { STATE.graphMode = mode; setGraphActive(); renderGraph(); }

function renderGraph() {
  const sc = STATE.current;
  const empty = document.getElementById('empty-graph');
  const svg = document.getElementById('graph-svg');
  let graph = null;
  if (sc) {
    if (STATE.graphMode === 'flow' && sc.flow) graph = sc.flow.flow_graph;
    else if (sc.relations) graph = sc.relations.graph_data;
    else if (sc.flow) graph = sc.flow.flow_graph;
  }
  if (!graph || !(graph.nodes || []).length) {
    empty.style.display = 'flex'; svg.style.display = 'none'; return;
  }
  empty.style.display = 'none'; svg.style.display = 'block';
  drawGraph(svg, graph.nodes, graph.edges);
}

function drawGraph(svg, nodes, edges) {
  const W = svg.clientWidth || 700, H = svg.clientHeight || 500;
  const cols = Math.ceil(Math.sqrt(nodes.length));
  const nodeW = 140, nodeH = 44;
  const gapX = 60, gapY = 70;
  const rows = Math.ceil(nodes.length / cols);
  const totalW = cols * nodeW + (cols - 1) * gapX;
  const totalH = rows * nodeH + (rows - 1) * gapY;
  const padX = Math.max((W - totalW) / 2, 20);
  const padY = Math.max((H - totalH) / 2, 30);

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
  document.querySelectorAll('.chat-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  document.querySelectorAll('.tab-content').forEach(c => c.style.display = 'none');
  const el = document.getElementById('tab-' + tab);
  el.style.display = tab === 'chat' ? 'flex' : 'block';
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

/* 极简 Markdown：转义后处理 ```代码块```、`行内代码`、**粗体**、换行 */
function renderMarkdown(text) {
  let t = esc(text);
  t = t.replace(/```([\s\S]*?)```/g, (_, code) => `<pre><code>${code.replace(/^\n/, '')}</code></pre>`);
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
  t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(/\n/g, '<br>');
  return t;
}
