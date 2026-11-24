/** Poll read-only state and fresh frames; all remote strings render as text. */
import {t} from './i18n.js';
const $ = id => document.getElementById(id);
for (const node of document.querySelectorAll('[data-i18n]')) node.textContent = t(node.dataset.i18n);
for (const node of document.querySelectorAll('[data-placeholder]')) node.placeholder = t(node.dataset.placeholder);
let profiles = [], state = null, cursor = 0, events = [], cameras = new Map(), generation = -1, busy = false, stopped = false;
const levels = {DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3};
const time = value => new Date(value * 1000).toLocaleTimeString('zh-CN', {hour12: false});
function el(tag, className, text) { const n = document.createElement(tag); n.className = className; if (text !== undefined) n.textContent = text; return n; }
function error(message) { $('error').textContent = message; $('error').hidden = !message; }
async function api(path, body) {
  const response = await fetch(path, {cache: 'no-store', signal: AbortSignal.timeout(15000), ...(body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})});
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
  rows.push({name: t('agent'), address: 'Ego robot', status: 'notRegistered', detail: t('agentDetail')});
  $('devices').replaceChildren(...rows.map(row => {
    const n = el('div', 'device'), head = el('div', 'device-head', row.name);
    head.append(el('span', `pill ${['available','reachable'].includes(row.status) ? 'good' : row.status === 'unavailable' ? 'bad' : ''}`, t(row.status)));
    n.append(head, el('div', 'device-address', row.address), el('div', 'device-detail', row.detail)); return n;
  }));
}
function details(data) { const d = el('details', ''); d.append(el('summary', '', t('details')), el('pre', '', JSON.stringify(data, null, 2))); return d; }
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
  if (s.generation !== generation) { generation = s.generation; resetCameras(config); }
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
$('composer').onsubmit = e => { e.preventDefault(); error(t('pendingMessage')); };
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
