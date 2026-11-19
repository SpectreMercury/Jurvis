// Shared state, the API client, and small DOM helpers. No framework: one store object plus render functions.

export const TOKEN_KEY = 'jurvis.token';

function readToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; } catch { return ''; }
}

export const store = {
  token: readToken(),
  state: null,        // last /api/state
  settings: null,     // last /api/settings view
  tab: 'chat',
  sessionId: null,    // null = a conversation that has not been created yet
  messages: [],
};

// ---- tiny event bus ----
const listeners = new Map();
export function on(name, fn) {
  if (!listeners.has(name)) listeners.set(name, new Set());
  listeners.get(name).add(fn);
}
export function emit(name, detail) {
  for (const fn of listeners.get(name) || []) {
    try { fn(detail); } catch (err) { console.error(err); }
  }
}

// ---- auth ----
export function setToken(token) {
  store.token = token || '';
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* private mode: the token lives for this page only */ }
}

export function signOut() {
  if (!store.token && !store.state) return;
  setToken('');
  store.state = null;
  store.settings = null;
  store.sessionId = null;
  store.messages = [];
  releaseAssets(new Set());
  emit('auth');
}

// ---- API ----
export class ApiError extends Error {
  constructor(detail, status) {
    super(detail);
    this.status = status;
  }
}

function detailOf(body, status) {
  const d = body && body.detail;
  if (typeof d === 'string' && d) return d;
  if (Array.isArray(d)) {
    const parts = d.map((item) => (item && item.msg) || '').filter(Boolean);
    if (parts.length) return '请求内容有误：' + parts.join('；');
  }
  return `请求失败（${status}）`;
}

export async function api(method, path, body, options = {}) {
  const headers = {};
  if (store.token && path !== '/pair') headers.Authorization = 'Bearer ' + store.token;
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined && body !== null) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(location.origin + path, { method, headers, body: payload, cache: 'no-store', signal: options.signal });
  } catch (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError('连不上 Jurvis，请确认 Mac 上的 Jurvis 已打开，并且在同一个网络里', 0);
  }
  if (response.status === 401 && path !== '/pair') {
    signOut();
    throw new ApiError('请先与 Mac 配对', 401);
  }
  if (!response.ok) {
    let data = null;
    try { data = await response.json(); } catch { /* not JSON */ }
    throw new ApiError(detailOf(data, response.status), response.status);
  }
  if (options.blob) return response.blob();
  return response.json();
}

export const get = (path, options) => api('GET', path, undefined, options);
export const post = (path, body) => api('POST', path, body === undefined ? {} : body);
export const put = (path, body) => api('PUT', path, body);
export const patch = (path, body) => api('PATCH', path, body);
export const del = (path) => api('DELETE', path);

// ---- state ----
/** Runs fn at most once per `ms`, never concurrently, and always once more after the last request. */
export function throttle(fn, ms) {
  let running = false, again = false, last = 0, timer = null;
  const fire = async () => {
    timer = null;
    if (running) { again = true; return; }
    running = true;
    last = Date.now();
    try { await fn(); } catch (err) { if (!(err instanceof ApiError)) console.error(err); }
    running = false;
    if (again) { again = false; schedule(); }
  };
  const schedule = () => {
    if (timer) return;
    if (running) { again = true; return; }
    timer = setTimeout(fire, Math.max(0, ms - (Date.now() - last)));
  };
  return schedule;
}

export async function loadState() {
  if (!store.token) return;
  const state = await get('/api/state');
  if (!store.token) return;
  store.state = state;
  emit('state');
}
export const refreshState = throttle(loadState, 500);

export async function loadSettings() {
  const view = await get('/api/settings');
  store.settings = view;
  emit('settings');
  return view;
}

const modelLists = new Map(); // provider -> Promise<[{id,name}]>
export function providerModels(name, force = false) {
  if (force || !modelLists.has(name)) {
    const promise = get('/api/providers/' + encodeURIComponent(name) + '/models').then((r) => r.models || []);
    promise.catch(() => { if (modelLists.get(name) === promise) modelLists.delete(name); });
    modelLists.set(name, promise);
  }
  return modelLists.get(name);
}
export function forgetModels() { modelLists.clear(); }

export function modelLabel(id) {
  if (!id) return '默认';
  if (id === 'local') return '本机';
  const at = id.indexOf(':');
  return at < 0 ? id : id.slice(at + 1);
}

export function jobsOf(sessionId) {
  return ((store.state && store.state.jobs) || []).filter((j) => j.kind === 'chat' && j.payload && j.payload.session_id === sessionId);
}
export const isLive = (job) => job.status === 'queued' || job.status === 'running';
export function jobById(id) {
  return ((store.state && store.state.jobs) || []).find((j) => j.id === id) || null;
}

// ---- assets: bytes need the auth header, so they are fetched as blobs and shown through object URLs ----
const assetURLs = new Map(); // id -> {promise, url}
export function assetURL(id) {
  let entry = assetURLs.get(id);
  if (!entry) {
    entry = { url: null, dropped: false };
    entry.promise = get('/api/assets/' + encodeURIComponent(id) + '/file', { blob: true }).then((blob) => {
      const url = URL.createObjectURL(blob);
      if (entry.dropped) { URL.revokeObjectURL(url); throw new ApiError('已取消', 0); }
      entry.url = url;
      return url;
    });
    entry.promise.catch(() => { if (assetURLs.get(id) === entry) assetURLs.delete(id); });
    assetURLs.set(id, entry);
  }
  return entry.promise;
}
/** Revokes every cached object URL whose asset is not in `keep`. */
export function releaseAssets(keep) {
  for (const [id, entry] of assetURLs) {
    if (keep.has(id)) continue;
    entry.dropped = true;
    if (entry.url) URL.revokeObjectURL(entry.url);
    assetURLs.delete(id);
  }
}

// ---- DOM helpers ----
const PROPS = new Set(['value', 'checked', 'disabled', 'hidden', 'selected', 'open', 'type', 'placeholder', 'rows', 'min', 'max', 'step', 'href', 'src', 'title', 'id', 'name', 'htmlFor', 'multiple', 'accept', 'autocomplete', 'required', 'readOnly', 'tabIndex', 'maxLength', 'controls', 'playsInline', 'loop', 'muted', 'alt', 'inputMode', 'enterKeyHint', 'spellcheck', 'autocapitalize', 'target', 'rel']);

/** h('button', {class:'x', onclick: fn}, 'text', node, ...). Strings always become text nodes. */
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null) continue;
      if (value === false && !PROPS.has(key)) continue;
      if (key === 'class') el.className = value;
      else if (key === 'text') el.textContent = value;
      else if (key === 'dataset') Object.assign(el.dataset, value);
      else if (key.startsWith('on') && typeof value === 'function') el.addEventListener(key.slice(2), value);
      else if (PROPS.has(key)) el[key] = value;
      else el.setAttribute(key, value === true ? '' : String(value));
    }
  }
  append(el, kids);
  return el;
}
/** Replaces el's children; like h(), skips null/false and turns strings into text nodes. */
export function fill(el, ...kids) {
  el.textContent = '';
  append(el, kids);
  return el;
}
function append(el, kids) {
  for (const kid of kids) {
    if (kid === null || kid === undefined || kid === false) continue;
    if (Array.isArray(kid)) append(el, kid);
    else el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
}

// ---- banners ----
let bannerHost = null;
export function setBannerHost(el) { bannerHost = el; }
export function notify(message, kind = 'error') {
  if (!bannerHost) return;
  const text = String(message || '出错了');
  for (const old of bannerHost.children) {
    if (old.dataset.text === text) old.remove();
  }
  const node = h('div', { class: 'banner ' + kind, role: kind === 'error' ? 'alert' : 'status', dataset: { text } },
    h('span', { class: 'banner-text' }, text),
    h('button', { class: 'banner-close', type: 'button', 'aria-label': '关闭', onclick: () => node.remove() }, '×'));
  bannerHost.append(node);
  while (bannerHost.children.length > 3) bannerHost.firstChild.remove();
  if (kind !== 'error') setTimeout(() => node.remove(), 4000);
}

/** Runs an async action for a button: pending state, no double clicks, failures as a banner. Returns true on success. */
export async function run(button, action) {
  if (button) {
    if (button.dataset.pending) return false;
    button.dataset.pending = '1';
    button.classList.add('pending');
    button.setAttribute('aria-busy', 'true');
  }
  const wasDisabled = button ? button.disabled : false;
  if (button) button.disabled = true;
  try {
    await action();
    return true;
  } catch (err) {
    if (!(err && err.name === 'AbortError') && !(err instanceof ApiError && err.status === 401)) {
      notify(err instanceof ApiError ? err.message : '出错了：' + ((err && err.message) || err));
      if (!(err instanceof ApiError)) console.error(err);
    }
    return false;
  } finally {
    if (button) {
      delete button.dataset.pending;
      button.classList.remove('pending');
      button.removeAttribute('aria-busy');
      button.disabled = wasDisabled;
    }
  }
}

/** A button wired through run(). */
export function action(label, handler, attrs = {}) {
  const button = h('button', { type: 'button', ...attrs }, label);
  button.addEventListener('click', (event) => { event.stopPropagation(); run(button, () => handler(button)); });
  return button;
}

// ---- modal sheet ----
export function sheet(title, body, { wide = false } = {}) {
  const previous = document.activeElement;
  const close = () => {
    overlay.remove();
    document.removeEventListener('keydown', onKey);
    if (previous && previous.focus && previous.isConnected) previous.focus();
  };
  const onKey = (event) => { if (event.key === 'Escape') close(); };
  const panel = h('div', { class: 'sheet' + (wide ? ' wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-label': title },
    h('header', { class: 'sheet-head' },
      h('h2', null, title),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': '关闭', onclick: close }, '×')),
    h('div', { class: 'sheet-body' }, body));
  const overlay = h('div', { class: 'overlay', onclick: (event) => { if (event.target === overlay) close(); } }, panel);
  document.body.append(overlay);
  document.addEventListener('keydown', onKey);
  return close;
}

/** Picks a model: 默认 / 本机 / each configured provider's models (loaded when the sheet opens). */
export function pickModel({ title = '选择模型', current = '', allowDefault = true, defaultLabel = '默认', onPick }) {
  const list = h('div', { class: 'model-list' });
  const filter = h('input', { type: 'search', class: 'input', placeholder: '筛选模型', 'aria-label': '筛选模型', autocomplete: 'off' });
  const close = sheet(title, h('div', { class: 'stack' }, filter, list));
  const rows = [];
  const row = (id, label, hint) => {
    const button = h('button', { type: 'button', class: 'model-row' + (id === (current || '') ? ' current' : '') },
      h('span', { class: 'model-name' }, label), hint ? h('span', { class: 'muted small' }, hint) : null);
    button.addEventListener('click', async () => {
      const ok = await run(button, () => onPick(id));
      if (ok) close();
    });
    rows.push({ button, text: (label + ' ' + id).toLowerCase() });
    return button;
  };
  filter.addEventListener('input', () => {
    const q = filter.value.trim().toLowerCase();
    for (const item of rows) item.button.hidden = !!q && !item.text.includes(q);
  });
  const show = (view) => {
    fill(list);
    rows.length = 0;
    if (allowDefault) list.append(row('', defaultLabel));
    list.append(row('local', '本机', view.local && view.local.installed ? (view.local.title || '') : '本机模型还没有安装'));
    for (const provider of view.providers || []) {
      const group = h('div', { class: 'model-group' }, h('div', { class: 'group-title' }, provider.title || provider.id));
      list.append(group);
      if (!provider.configured) {
        group.append(h('p', { class: 'muted small' }, '还没有填写 API Key，请到“设置 → 模型”里填写'));
        continue;
      }
      const status = h('p', { class: 'muted small' }, '正在读取模型列表…');
      group.append(status);
      providerModels(provider.id).then((models) => {
        status.remove();
        if (!models.length) group.append(h('p', { class: 'muted small' }, '这个服务没有返回模型'));
        for (const model of models) group.append(row(model.id, model.name || model.id, model.name && model.name !== modelLabel(model.id) ? modelLabel(model.id) : ''));
        filter.dispatchEvent(new Event('input'));
      }).catch((err) => {
        status.textContent = '读取失败：' + err.message;
        status.classList.add('error-text');
      });
    }
    if (current && current !== 'local' && !(view.providers || []).some((p) => current.startsWith(p.id + ':'))) {
      list.append(h('p', { class: 'muted small' }, '当前：' + current));
    }
  };
  if (store.settings) show(store.settings);
  else list.append(h('p', { class: 'muted small' }, '正在读取…'));
  loadSettings().then(show).catch((err) => { if (!store.settings) fill(list, h('p', { class: 'error-text' }, err.message)); });
}

// ---- formatting ----
export function dayGroup(seconds) {
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  if (seconds >= start) return '今天';
  if (seconds >= start - 86400) return '昨天';
  return '更早';
}
export function shortTime(seconds) {
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  if (dayGroup(seconds) === '今天') return pad(date.getHours()) + ':' + pad(date.getMinutes());
  return (date.getMonth() + 1) + '月' + date.getDate() + '日';
}
export function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
