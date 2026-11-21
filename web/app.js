// Jurvis web client: pairing, the app shell and tabs, and the live change feed.
import { store, on, h, fill, api, setToken, signOut, loadState, refreshState, setBannerHost, notify, run, sleep, modelLabel, ApiError } from './core.js';
import * as chat from './chat.js';
import * as library from './library.js';
import * as home from './home.js';
import * as memory from './memory.js';
import * as goals from './goals.js';
import * as ledger from './ledger.js';
import * as health from './health.js';
import * as settings from './settings.js';
import { icon } from './icons.js';

const TABS = [
  { id: 'chat', label: '任务', icon: 'list', module: chat },
  { id: 'library', label: '作品库', icon: 'folder', module: library },
  { id: 'home', label: '家居', icon: 'home', module: home },
  { id: 'goals', label: '目标', icon: 'target', module: goals },
  { id: 'health', label: '健康', icon: 'heart', module: health },
  { id: 'ledger', label: '账本', icon: 'doc', module: ledger },
  { id: 'memory', label: '记忆', icon: 'bookmark', module: memory },
  { id: 'settings', label: '设置', icon: 'gear', module: settings },
];

const root = document.getElementById('app');
const banners = h('div', { class: 'banners', 'aria-live': 'polite' });
document.body.append(banners);
setBannerHost(banners);

let shell = null;
let pairing = null;
let offline = null;
const views = {};
const tabButtons = {};
const navRows = {};
const status = {};
let generation = 0;   // bumps on sign-out so old loops stop
let pollTimer = null;
let lastEvent = 0;

function logo(size) {
  return h('img', { src: './icon.svg', alt: '', width: size, height: size, class: 'logo' });
}

// ---------------------------------------------------------------- pairing
function deviceName() {
  const ua = navigator.userAgent || '';
  const device = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android' : /Macintosh/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : /Linux/.test(ua) ? 'Linux' : '设备';
  const browser = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\/|CriOS\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : '浏览器';
  return `${device} · ${browser}`.slice(0, 80);
}

/** Accepts a jurvis://pair?… link, a web link ending in #pair=…, or a bare code. */
export function parseInvitation(text) {
  const raw = String(text || '').trim();
  if (!raw) return { code: '', url: '' };
  const fragment = /#pair=([^&\s]+)/.exec(raw);
  if (fragment) return { code: safeDecode(fragment[1]), url: '' };
  const code = /[?&]code=([^&\s#]+)/.exec(raw);
  if (code) {
    const url = /[?&]url=([^&\s#]+)/.exec(raw);
    return { code: safeDecode(code[1]), url: url ? safeDecode(url[1]) : '' };
  }
  return { code: raw.replace(/\s+/g, ''), url: '' };
}
function safeDecode(value) {
  try { return decodeURIComponent(value.replace(/\+/g, ' ')); } catch { return value; }
}

async function pair(code, name) {
  if (code.length < 16 || code.length > 128) throw new ApiError('这不像是有效的邀请，请粘贴完整的邀请链接或配对码', 400);
  const result = await api('POST', '/pair', { code, name: (name || deviceName()).slice(0, 80) || '浏览器' });
  if (!result || !result.token) throw new ApiError('配对没有成功，请在 Mac 上重新生成邀请', 400);
  try { if (result.device_id) localStorage.setItem('jurvis.device', result.device_id); } catch { /* unavailable */ }
  setToken(result.token);
}

function showPairing() {
  if (shell) shell.hidden = true;
  if (pairing) pairing.remove();
  const input = h('textarea', { class: 'input', rows: 3, placeholder: 'jurvis://pair?… 或配对码', autocapitalize: 'off', autocomplete: 'off', spellcheck: false, 'aria-label': '邀请链接或配对码' });
  const name = h('input', { class: 'input', maxLength: 80, value: deviceName(), 'aria-label': '这台设备的名称' });
  const submit = h('button', { class: 'btn primary', type: 'submit' }, '配对');
  const form = h('form', { class: 'stack' },
    h('label', { class: 'field' }, h('span', null, '邀请链接或配对码'), input),
    h('label', { class: 'field' }, h('span', null, '这台设备的名称'), name),
    submit);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const invitation = parseInvitation(input.value);
    if (!invitation.code) { notify('请先粘贴邀请链接或配对码'); return; }
    if (invitation.url) {
      try {
        if (new URL(invitation.url).origin !== location.origin) notify(`这份邀请来自 ${new URL(invitation.url).host}，当前页面是 ${location.host}。如果配对失败，请打开邀请里的地址。`, 'info');
      } catch { /* ignore a malformed url */ }
    }
    const ok = await run(submit, () => pair(invitation.code, name.value.trim()));
    if (ok) enter();
  });
  pairing = h('main', { class: 'pairing' },
    h('div', { class: 'pairing-card' },
      h('img', { src: './icon.svg', alt: '', width: 56, height: 56, class: 'pairing-icon' }),
      h('h1', null, '连接 Jurvis'),
      h('p', { class: 'muted' }, '在 Mac 的 Jurvis 里生成一份邀请，然后用这台设备扫码打开，或把邀请链接、配对码粘贴到下面。邀请 5 分钟内有效，只能用一次。'),
      form));
  root.append(pairing);
}

// ---------------------------------------------------------------- shell
function buildShell() {
  const tabs = h('nav', { class: 'tabs', 'aria-label': '主导航' });
  const main = h('main', { class: 'views' });
  const nav = h('nav', { class: 'nav', 'aria-label': '导航' });
  navRows.new = h('button', { class: 'nav-row', type: 'button', onclick: () => { go('chat', false); chat.newConversation(true); } }, icon('plus'), '新任务');
  nav.append(navRows.new);
  for (const tab of TABS) {
    const view = h('section', { class: 'view view-' + tab.id, hidden: true, 'aria-label': tab.label });
    views[tab.id] = view;
    tab.module.mount(view);
    main.append(view);
    const button = h('button', { class: 'tab', type: 'button', onclick: () => go(tab.id, true) }, icon(tab.icon, 20), h('span', null, tab.label));
    tabButtons[tab.id] = button;
    tabs.append(button);
    if (tab.id !== 'chat') {
      navRows[tab.id] = h('button', { class: 'nav-row', type: 'button', onclick: () => go(tab.id, true) }, icon(tab.icon), tab.label);
      nav.append(navRows[tab.id]);
    }
  }
  const side = chat.sideParts();
  status.pill = h('span', { class: 'model-pill' });
  status.name = h('span');
  status.note = h('span');
  status.box = h('div', { class: 'model-status' }, h('span', { class: 'dot', 'aria-hidden': 'true' }), icon('chip'), h('div', { class: 'model-status-text' }, status.name, status.note));
  const sidebar = h('aside', { class: 'sidebar' },
    h('div', { class: 'brand' }, logo(24), h('span', null, 'Jurvis'), status.pill),
    nav, side.waiting,
    h('div', { class: 'side-tasks' }, h('div', { class: 'side-title' }, '任务'), side.list),
    h('div', { class: 'side-foot' },
      h('button', { class: 'new-task', type: 'button', onclick: () => chat.newConversation(true) }, icon('plus', 18), '交给 Jurvis 一个新任务')),
    status.box);
  offline = h('div', { class: 'offline', hidden: true, role: 'status' }, '连接中断，正在重试…');
  shell = h('div', { class: 'shell' }, sidebar, h('div', { class: 'main-col' }, offline, main), tabs);
  root.append(shell);
}

function syncNav() {
  if (!shell) return;
  const conv = chat.where();
  shell.dataset.tab = store.tab;
  shell.classList.toggle('in-conv', conv.open);
  for (const tab of TABS) {
    const current = tab.id === store.tab;
    if (current) tabButtons[tab.id].setAttribute('aria-current', 'page');
    else tabButtons[tab.id].removeAttribute('aria-current');
    if (navRows[tab.id]) {
      if (current) navRows[tab.id].setAttribute('aria-current', 'page');
      else navRows[tab.id].removeAttribute('aria-current');
    }
  }
  if (store.tab === 'chat' && conv.blank) navRows.new.setAttribute('aria-current', 'page');
  else navRows.new.removeAttribute('aria-current');
}

function renderStatus() {
  if (!status.box || !store.state) return;
  const jobs = (store.state.jobs || []).filter((j) => j.status === 'queued' || j.status === 'running');
  const running = jobs.find((j) => j.status === 'running');
  const model = modelLabel(store.state.chat_model || 'local');
  const busy = jobs.length > 0;
  status.name.textContent = model === '本机' ? '本机模型' : model;
  status.note.textContent = running ? (running.phase || '正在工作') : busy ? `${jobs.length} 个任务排队中` : '空闲';
  status.box.classList.toggle('busy', busy);
  status.pill.textContent = status.name.textContent;
  status.pill.classList.toggle('busy', busy);
}

function go(id, fromTap) {
  if (store.tab === id && fromTap && id === 'chat') chat.showList();
  store.tab = id;
  for (const tab of TABS) views[tab.id].hidden = tab.id !== id;
  const module = TABS.find((t) => t.id === id).module;
  if (module.activate) module.activate();
  syncNav();
}

function enter() {
  if (pairing) { pairing.remove(); pairing = null; }
  fill(banners);
  if (!shell) buildShell();
  shell.hidden = false;
  go('chat', false);
  const mine = ++generation;
  loadState().catch(() => {});
  feed(mine);
}

on('auth', () => {
  generation += 1;
  stopPolling();
  for (const tab of TABS) if (tab.module.reset) tab.module.reset();
  if (offline) offline.hidden = true;
  showPairing();
  notify('这台设备尚未配对，或配对已失效。', 'info');
});
on('goto', (id) => go(id, false));
on('conv', syncNav);
on('ask', (text) => { go('chat', false); chat.startTask(text); });
on('state', () => {
  if (offline) offline.hidden = true;
  chat.onState();
  chat.announce();
  renderStatus();
  if (store.tab === 'library') library.onState();
  if (store.tab === 'memory') memory.onState();
  if (store.tab === 'settings') settings.onState();
});

// ---------------------------------------------------------------- live changes
function handleEvent(event) {
  if (!event || typeof event !== 'object') return;
  if (event.entity === 'messages') {
    chat.refreshMessages();
    return;
  }
  if (event.entity === 'goals' || event.entity === 'schedules') { goals.refresh(); return; }
  if (event.entity === 'ledger') { ledger.refresh(); return; }
  if (event.entity === 'health') { health.refresh(); return; }
  if (event.entity === 'settings' && store.tab === 'settings') settings.onSettingsEvent();
  refreshState();
}

function parseBlock(block) {
  let dataText = '';
  for (const line of block.split('\n')) {
    if (line.startsWith('id:')) {
      const id = Number(line.slice(3).trim());
      if (Number.isFinite(id) && id > lastEvent) lastEvent = id;
    } else if (line.startsWith('data:')) dataText += (dataText ? '\n' : '') + line.slice(5).replace(/^ /, '');
  }
  if (!dataText) return;
  try { handleEvent(JSON.parse(dataText)); } catch { /* skip a malformed event */ }
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(() => {
    refreshState();
    if (store.tab === 'chat') chat.refreshMessages();
  }, 2000);
}
function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

/** EventSource cannot send the Authorization header, so the stream is read with fetch and parsed here. */
async function feed(mine) {
  let backoff = 1000;
  let failures = 0;
  while (store.token && mine === generation) {
    let connected = false;
    try {
      if (!store.state) await loadState();
      if (mine !== generation) return;
      lastEvent = Math.max(lastEvent, Number(store.state && store.state.last_event) || 0);
      const control = new AbortController();
      let watchdog = null;
      const alive = () => {
        clearTimeout(watchdog);
        watchdog = setTimeout(() => control.abort(), 45000); // the server sends a keep-alive every ~15s
      };
      alive();
      try {
        const response = await fetch(location.origin + '/api/events?after=' + lastEvent, {
          headers: { Authorization: 'Bearer ' + store.token, Accept: 'text/event-stream' }, cache: 'no-store', signal: control.signal,
        });
        if (response.status === 401) { signOut(); return; }
        if (!response.ok || !response.body || !response.body.getReader) throw new Error('stream unavailable');
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          if (mine !== generation) { control.abort(); return; }
          alive();
          if (!connected) {
            connected = true;
            failures = 0;
            backoff = 1000;
            stopPolling();
            if (offline) offline.hidden = true;
          }
          buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/g, '\n');
          let end;
          while ((end = buffer.indexOf('\n\n')) >= 0) {
            parseBlock(buffer.slice(0, end));
            buffer = buffer.slice(end + 2);
          }
        }
      } finally {
        clearTimeout(watchdog);
      }
    } catch { /* fall through to retry */ }
    if (mine !== generation || !store.token) return;
    failures += 1;
    startPolling(); // keep the screen fresh while the stream is down (or unsupported)
    if (!connected && failures >= 2) {
      try {
        await loadState();
      } catch (err) {
        if (offline && !(err instanceof ApiError && err.status === 401)) offline.hidden = false;
      }
    }
    await sleep(backoff);
    backoff = Math.min(backoff * 2, 15000);
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible' || !store.token || !shell || shell.hidden) return;
  refreshState();
  chat.refreshMessages();
  if (store.tab === 'home') home.activate();
});

// ---------------------------------------------------------------- start
function registerServiceWorker() {
  // Best effort: with the engine's self-signed certificate browsers may refuse it, and the app works without it.
  try {
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('./sw.js').catch(() => {});
  } catch { /* unsupported or blocked */ }
}

async function start() {
  let invited = '';
  const match = /^#pair=([^&]+)/.exec(location.hash || '');
  if (match) {
    invited = safeDecode(match[1]);
    try { history.replaceState(null, '', location.pathname + location.search); } catch { /* keep going */ }
  }
  if (invited) {
    try {
      await pair(invited, deviceName());
    } catch (err) {
      if (store.token) notify('新的邀请没有生效：' + err.message + ' 继续使用原来的连接。', 'info');
      else {
        showPairing();
        notify(err.message);
        registerServiceWorker();
        return;
      }
    }
  }
  if (store.token) enter();
  else showPairing();
  registerServiceWorker();
}

start();
