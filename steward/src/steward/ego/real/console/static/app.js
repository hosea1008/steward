/** Poll robot state, conversations and fresh frames; all remote strings render as text. */
import {t} from './i18n.js';
const $ = id => document.getElementById(id);
for (const node of document.querySelectorAll('[data-i18n]')) node.textContent = t(node.dataset.i18n);
for (const node of document.querySelectorAll('[data-placeholder]')) node.placeholder = t(node.dataset.placeholder);
for (const node of document.querySelectorAll('[data-label]')) node.setAttribute('aria-label', t(node.dataset.label));
let profiles = [], state = null, cursor = 0, events = [], cameras = new Map(), generation = -1, busy = false, stopped = false;
let contextBase = null, contextDirty = false, contextSaving = false;
const levels = {DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3};
const time = value => new Date(value * 1000).toLocaleTimeString('zh-CN', {hour12: false});
function el(tag, className, text) { const n = document.createElement(tag); n.className = className; if (text !== undefined) n.textContent = text; return n; }
function error(message) { $('error').textContent = message; $('error').hidden = !message; }
async function api(path, body) {
  const response = await fetch(path, {cache: 'no-store', signal: AbortSignal.timeout(60000), ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || t('requestFailed'));
  return data;
}
function selected() { return profiles.find(p => p.id === $('robot').value); }
function drawConfiguration(config) {
  if (!config) return;
  $('robot-name').textContent = config.name;
  $('description').textContent = config.description;
  $('config-json').textContent = JSON.stringify(config, null, 2);
  $('camera-rate').textContent = `${t('sample')} ${config.refresh_fps} ${t('hz')}`;
}
function resetCameras(config) {
  for (const camera of cameras.values()) { camera.controller?.abort(); if (camera.url) URL.revokeObjectURL(camera.url); }
  cameras.clear(); $('cameras').replaceChildren();
  for (const spec of config?.cameras || []) {
    const card = el('div', 'camera'), title = el('div', 'camera-title', spec.name), badge = el('span', 'pill', t('waiting'));
    title.append(el('span', 'spacer'), badge);
    const viewport = el('div', 'viewport'), img = el('img', ''); img.alt = spec.name;
    const overlay = el('div', 'camera-overlay'); overlay.append(el('span', 'camera-mark'), el('span', '', t('loadFirst')));
    viewport.append(img, overlay); card.append(title, viewport, el('div', 'camera-meta', spec.topic)); $('cameras').append(card);
    cameras.set(spec.id, {spec, img, overlay, badge, loading: false, live: false, url: null});
  }
}
function drawDevices(s) {
  const rows = [...s.devices];
  if (!rows.length && selected()) for (const arm of selected().arms) rows.push({...arm, address: arm.device, status: 'unknown', detail: t('loadFirst')});
  rows.push({name: t('runtime'), address: s.config?.runtime_url || selected()?.runtime_url || '', status: s.runtime ? 'reachable' : s.runtime_error ? 'unavailable' : 'unknown', detail: s.runtime ? `${t(s.runtime.mode)} · ${t('runtimeHint')}` : (s.runtime_error ? t('runtimeUnavailable') : t('runtimeUnknown'))});
  rows.push({name: t('agent'), address: 'DeepSeek Flash · ' + t('nativeTools'), status: s.agent.connected ? 'available' : 'notRegistered', detail: s.agent.reason || t('agentDetail')});
  $('devices').replaceChildren(...rows.map(row => {
    const n = el('div', 'device'), head = el('div', 'device-head', row.name);
    head.append(el('span', `pill ${['available','reachable'].includes(row.status) ? 'good' : row.status === 'unavailable' ? 'bad' : ''}`, t(row.status)));
    n.append(head, el('div', 'device-address', row.address), el('div', 'device-detail', row.detail)); return n;
  }));
}
function details(data, label = t('details')) { const d = el('details', ''); d.append(el('summary', '', label), el('pre', '', typeof data === 'string' ? data : JSON.stringify(data, null, 2))); return d; }
function toolResult(entry) {
  if (!entry.text) return null;
  try { return JSON.parse(entry.text); }
  catch (error) { return entry.text; } // Text errors and legacy truncated results are still readable.
}
function resultSummary(result, status) {
  if (status === 'running') return t('running');
  if (status === 'failed' && typeof result === 'string') return result.split('\n')[0];
  if (result && typeof result === 'object') {
    if (result.error) return String(result.error);
    if (result.state === 'succeeded') return t('succeeded');
    if (result.state === 'failed') return t('failed');
    if (result.fault) return t('stateHasFault');
    if (result.message) return String(result.message);
  }
  return t(status === 'failed' ? 'failed' : 'toolReturned');
}
function renderChatHint(agent) {
  const fault = agent.motion?.fault, node = $('chat-hint');
  const signature = JSON.stringify([agent.reason, fault, agent.motion?.motion_enabled]);
  if (node.dataset.hint === signature) return;
  const opened = node.querySelector('details')?.open;
  node.dataset.hint = signature;
  node.replaceChildren();
  if (agent.reason) node.append(el('p', '', agent.reason));
  if (fault) {
    node.append(el('p', '', t('motionFault')), el('p', 'fault-reason', fault.error || t('failed')));
    if (fault.physical_result === 'unknown' || fault.stop?.physical_stop_confirmed === false) {
      node.append(el('p', '', t('physicalUnconfirmed')));
    }
    const detail = details(fault, t('faultDetails')); detail.open = opened || false; node.append(detail);
  }
  if (agent.motion?.motion_enabled === false) node.append(el('p', '', t('actuationDisabled')));
  node.classList.toggle('error', Boolean(fault));
  node.hidden = !node.childNodes.length;
}
function renderTools(agent) {
  const names = agent.motion?.tools || [], container = $('supported-tools'), signature = JSON.stringify(names);
  if (container.dataset.tools === signature) return;
  container.dataset.tools = signature;
  $('tool-count').textContent = names.length;
  const labels = {navigate_to_pose: ['toolNavigate', 'toolNavigateDetail'], lift_move: ['toolLift', 'toolLiftDetail'],
    clear_motion_fault: ['toolClearFault', 'toolClearFaultDetail'], get_navigation_state: ['toolNavigationState', 'toolNavigationStateDetail']};
  container.replaceChildren(...names.map(name => {
    const label = labels[name], row = el('article', 'device tool-card'), heading = el('div', 'device-head', label ? t(label[0]) : name);
    heading.append(el('span', 'pill good', t('toolRegistered')));
    row.append(heading, el('div', 'device-address', name));
    if (label) row.append(el('p', 'device-detail', t(label[1])));
    return row;
  }));
  if (!names.length) container.append(el('p', 'muted', t('toolsEmpty')));
}
function renderAgentContext(agent) {
  for (const [id, value] of [['system-prompt', agent.system_prompt || t('promptUnavailable')],
    ['operator-brief', agent.operator_brief || t('promptUnavailable')], ['task-context', agent.task_context || '']]) {
    if ($(id).textContent !== value) $(id).textContent = value;
  }
  $('context-empty').hidden = Boolean(agent.task_context);
  $('task-context').hidden = !agent.task_context;
  const saved = agent.task_context || '';
  if (contextBase === null || (!contextDirty && saved !== contextBase)) {
    contextBase = saved; $('context-editor').value = saved; contextDirty = false;
  }
  const available = state?.phase === 'connected' && typeof agent.task_context === 'string';
  const conflict = contextDirty && saved !== contextBase;
  $('context-editor').disabled = !available || agent.busy || contextSaving;
  $('context-save').disabled = !available || agent.busy || contextSaving || !contextDirty || conflict;
  $('context-reload').disabled = !available || contextSaving || !contextDirty;
  $('context-status').textContent = t(!available ? 'contextLoadFirst' : agent.busy ? 'contextBusy' : conflict ? 'contextConflict' : contextDirty ? 'contextDirty' : 'contextSaved');
}
const tabs = [...document.querySelectorAll('.agent-tabs [role="tab"]')];
function activateTab(tab) {
  $('new-session').hidden = tab.id !== 'tab-conversation';
  for (const button of tabs) {
    const active = button === tab;
    button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
    $(button.getAttribute('aria-controls')).hidden = !active;
  }
}
for (const [index, tab] of tabs.entries()) {
  tab.onclick = () => activateTab(tab);
  tab.onkeydown = event => {
    const next = {ArrowRight: (index+1)%tabs.length, ArrowLeft: (index+tabs.length-1)%tabs.length, Home: 0, End: tabs.length-1}[event.key];
    if (next === undefined) return;
    event.preventDefault(); activateTab(tabs[next]); tabs[next].focus();
  };
}
function renderLogs() {
  const query = $('filter').value.toLowerCase(), minimum = levels[$('level').value];
  const list = events.filter(e => levels[e.level] >= minimum && JSON.stringify(e).toLowerCase().includes(query));
  $('logs').replaceChildren(...list.map(e => {
    const row = el('div', `log-row ${e.level}`); row.append(el('span', 'time', time(e.time)), el('span', 'level', e.level), el('span', 'source', e.source), el('span', 'message', e.message));
    if (e.detail) row.append(details(e.detail)); return row;
  }));
  if (!list.length) $('logs').append(el('div', 'muted', t('emptyLogs')));
  $('log-count').textContent = events.length;
  if ($('follow').getAttribute('aria-pressed') === 'true') $('logs').scrollTop = $('logs').scrollHeight;
}
function appendEvents(entries) {
  if (!entries.length) return;
  events.push(...entries); events = events.slice(-500); renderLogs();
}
function render(s) {
  const config = s.config || selected();
  if (s.generation !== generation) { generation = s.generation; contextBase = null; contextDirty = false; $('context-error').hidden = true; resetCameras(config); $('messages').replaceChildren(); }
  drawConfiguration(config); drawDevices(s);
  $('phase').textContent = t(s.phase); $('phase').className = 'pill ' + (s.phase === 'connected' ? 'good' : s.phase === 'error' ? 'bad' : '');
  $('robot').disabled = busy || s.phase !== 'idle'; $('connect').disabled = busy || s.phase !== 'idle'; $('disconnect').disabled = busy || s.phase === 'idle' || s.phase === 'disconnecting';
  let live = 0;
  for (const [id, c] of cameras) {
    c.live = s.cameras.find(x => x.id === id)?.live ?? false;
    if (c.live) live++;
    c.badge.textContent = t(c.live ? 'live' : s.phase === 'idle' ? 'waiting' : 'offline'); c.badge.className = 'pill ' + (c.live ? 'good' : '');
    if (!c.live) { c.overlay.hidden = false; c.overlay.lastChild.textContent = t(s.phase === 'idle' ? 'loadFirst' : 'stale'); }
  }
  $('camera-count').textContent = `${live} / ${cameras.size} ${t('receiving')}`;
  $('updated').textContent = `${t('updated')} ${time(Date.now()/1000)}`;
  appendEvents(s.events);
  renderChat(s.agent);
  renderTools(s.agent); renderAgentContext(s.agent);
}
function renderChat(agent) {
  $('agent-status').textContent = !agent.connected ? t('agentPending') : agent.busy ? t('agentBusy') : t('agentReady');
  renderChatHint(agent);
  $('send').disabled = busy || !agent.connected || agent.busy;
  $('stop-agent').disabled = busy || !agent.connected || !agent.busy;
  $('new-session').disabled = busy || !agent.connected || agent.busy;
  const container = $('messages'), atBottom = !$('panel-conversation').hidden && container.scrollHeight-container.scrollTop-container.clientHeight < 60;
  const keep = new Set();
  for (const entry of agent.messages || []) {
    const id = `chat-${entry.id}`; keep.add(id);
    let node = document.getElementById(id);
    const signature = JSON.stringify(entry);
    if (node?.dataset.entry === signature) continue;
    const opened = new Map([...node?.querySelectorAll('details') || []].map(d => [d.dataset.kind, d.open]));
    if (!node) { node = el('div', `chat-entry ${entry.role}`); node.id = id; container.append(node); }
    node.dataset.entry = signature;
    const result = entry.role === 'tool' ? toolResult(entry) : null;
    const failed = ['failed', 'error'].includes(entry.status) || result?.state === 'failed';
    node.classList.toggle('tool-failed', entry.role === 'tool' && failed);
    node.replaceChildren(el('div', 'chat-label', `${time(entry.t)} · ${t(entry.role)}${entry.tool ? ' · '+entry.tool : ''}${entry.status ? ' · '+t(failed ? 'failed' : entry.status) : ''}`));
    if (entry.role === 'tool') {
      node.append(el('p', 'tool-summary', resultSummary(result, failed ? 'failed' : entry.status)));
      if (entry.input !== undefined && entry.input !== null) {
        const empty = typeof entry.input === 'object' && !Object.keys(entry.input).length;
        const detail = details(empty ? t('noArguments') : entry.input, t('toolArguments'));
        detail.dataset.kind = 'input'; detail.open = opened.get('input') ?? false; node.append(detail);
      }
      if (result !== null) {
        const detail = details(result, t('toolResult'));
        detail.dataset.kind = 'result'; detail.open = opened.get('result') ?? false; node.append(detail);
      }
    } else if (entry.text) node.append(el('pre', 'chat-text', entry.text));
  }
  for (const node of [...container.children]) if (!keep.has(node.id)) node.remove();
  if (atBottom) container.scrollTop = container.scrollHeight;
}
async function poll() {
  if (stopped) return;
  try {
    const s = await api(`/api/state?since=${cursor}`);
    if (s.cursor < cursor) { cursor = 0; events = []; generation = -1; return; }
    cursor = s.cursor; state = s; render(s); error('');
  } catch (e) {
    error(`${t('transportLost')}：${e.message}`);
    for (const c of cameras.values()) { c.live = false; c.overlay.hidden = false; c.overlay.lastChild.textContent = t('stale'); }
  } finally { if (!stopped) setTimeout(poll, 700); }
}
async function frameLoop() {
  if (stopped) return;
  for (const [id, c] of cameras) if (c.live && !c.loading) {
    c.loading = true; c.controller = new AbortController(); const epoch = generation;
    fetch(`/api/camera/frame?id=${encodeURIComponent(id)}&generation=${epoch}`, {cache: 'no-store', signal: c.controller.signal})
      .then(async response => {
        if (!response.ok) throw new Error(t('stale'));
        const blob = await response.blob();
        if (epoch !== generation || !c.live || stopped) return;
        const old = c.url; c.url = URL.createObjectURL(blob); c.img.src = c.url; c.overlay.hidden = true;
        if (old) URL.revokeObjectURL(old);
      }).catch(() => { if (epoch === generation) { c.overlay.hidden = false; c.overlay.lastChild.textContent = t('stale'); } })
      .finally(() => { c.loading = false; });
  }
  setTimeout(frameLoop, 1000 / (state?.config?.refresh_fps || 5));
}
async function action(path, body) {
  busy = true; if (state) render({...state, events: []});
  try { await api(path, body); error(''); } catch(e) { error(e.message); }
  finally { busy = false; if (state) render({...state, events: []}); }
}
$('connect').onclick = () => action('/api/connect', {id: $('robot').value});
$('disconnect').onclick = () => action('/api/disconnect', {});
$('robot').onchange = () => { drawConfiguration(selected()); resetCameras(selected()); };
$('filter').oninput = renderLogs; $('level').onchange = renderLogs;
$('follow').onclick = () => { $('follow').setAttribute('aria-pressed', $('follow').getAttribute('aria-pressed') !== 'true'); renderLogs(); };
$('composer').onsubmit = async e => {
  e.preventDefault();
  const text = $('message').value.trim();
  if (!text || busy || !state?.agent.connected || state.agent.busy) return;
  busy = true; $('send').disabled = true;
  try { await api('/api/chat', {text}); $('message').value = ''; error(''); }
  catch (e) { error(e.message); }
  finally { busy = false; }
};
$('stop-agent').onclick = () => action('/api/chat/stop', {});
$('new-session').onclick = async () => {
  if ($('new-session').disabled) return;
  const epoch = generation;
  busy = true; renderChat(state.agent);
  try {
    const agent = await api('/api/chat/new', {generation: epoch});
    if (epoch !== generation) return;
    state.agent = agent; $('message').value = ''; error('');
  } catch (e) { error(e.message); }
  finally { busy = false; if (state) renderChat(state.agent); }
};
$('context-editor').oninput = () => {
  contextDirty = $('context-editor').value !== contextBase;
  $('context-error').hidden = true;
  if (state) renderAgentContext(state.agent);
};
$('context-reload').onclick = () => {
  contextDirty = false; contextBase = null; $('context-error').hidden = true;
  if (state) renderAgentContext(state.agent);
};
$('context-form').onsubmit = async event => {
  event.preventDefault();
  if ($('context-save').disabled) return;
  const epoch = generation, text = $('context-editor').value, previous = contextBase;
  contextSaving = true; renderAgentContext(state.agent);
  try {
    const result = await api('/api/context', {text, previous, generation: epoch});
    if (epoch !== generation) return;
    contextBase = result.task_context; contextDirty = false;
    state.agent.task_context = result.task_context;
    $('context-error').hidden = true;
  } catch (e) {
    if (epoch === generation) { $('context-error').textContent = e.message; $('context-error').hidden = false; }
  } finally { contextSaving = false; if (state) renderAgentContext(state.agent); }
};
window.addEventListener('pagehide', () => { stopped = true; for (const c of cameras.values()) { c.controller?.abort(); if(c.url) URL.revokeObjectURL(c.url); } });
window.addEventListener('pageshow', e => { if(e.persisted) location.reload(); });
async function start() {
  try {
    profiles = (await api('/api/configs')).configs;
    $('robot').replaceChildren(...profiles.map(p => { const option = el('option', '', p.name); option.value = p.id; return option; }));
    drawConfiguration(selected()); resetCameras(selected()); poll(); frameLoop();
  } catch(e) { error(e.message); $('connect').disabled = true; }
}
start();
