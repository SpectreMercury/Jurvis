// 连接智能家居: the brands Jurvis can talk to, the ones already connected, and the form that connects one.
// Opened from the 家居 page and from 设置, so there is one place to manage connectors.
import { h, fill, get, put, del, run, action, notify, sheet, ApiError } from './core.js';
import { icon } from './icons.js';
import * as ui from './ui.js';
import { brandLogo } from './brands.js';

let state = null;            // last /api/home: {catalog, connectors}
const watchers = new Set();

/** Calls fn(state) whenever a connector is added, changed or removed. */
export function onChange(fn) { watchers.add(fn); }
function changed() {
  for (const fn of watchers) {
    try { fn(state); } catch (err) { console.error(err); }
  }
}

export async function load() {
  state = await get('/api/home');
  return state;
}
export function use(next) { state = next; }
export function reset() { state = null; }

/** A brand's mark: its logo when we have one, otherwise a monogram of the brand name. */
export function brandMark(title, size = '', kind = '') {
  const logo = brandLogo(kind, size === 'large' ? 26 : 22);
  if (logo) return h('span', { class: 'brand-mark logo' + (size ? ' ' + size : ''), 'aria-hidden': 'true' }, logo);
  const words = String(title || '?').replace(/[^\p{L}\p{N} ]/gu, ' ').trim().split(/\s+/);
  const latin = /^[A-Za-z]/.test(words[0] || '');
  const text = latin ? (words.length > 1 ? words[0][0] + words[1][0] : words[0].slice(0, 2)) : (words[0] || '?').slice(0, 1);
  return h('span', { class: 'brand-mark' + (size ? ' ' + size : ''), 'aria-hidden': 'true' }, text.toUpperCase());
}

const specOf = (kind) => ((state && state.catalog) || []).find((c) => c.kind === kind) || null;

/** The form that adds a connector of this kind, or edits an existing one. */
export function edit(spec, connector) {
  if (!spec) { notify('这个连接器类型已不受支持'); return; }
  const taken = new Set(((state && state.connectors) || []).map((c) => c.id));
  let suggestion = spec.kind;
  for (let n = 2; taken.has(suggestion); n += 1) suggestion = spec.kind + '-' + n;
  const ident = h('input', { class: 'input', autocapitalize: 'off', autocomplete: 'off', spellcheck: false, maxLength: 32, value: connector ? connector.id : suggestion, disabled: !!connector });
  const title = h('input', { class: 'input', maxLength: 60, placeholder: spec.title, value: connector ? connector.title || '' : '' });
  const enabled = ui.toggle({ checked: connector ? !!connector.enabled : true, 'aria-label': '启用' });
  const inputs = (spec.fields || []).map((field) => {
    const saved = connector && connector.settings ? connector.settings[field.name] : undefined;
    const input = h('input', {
      class: 'input', type: field.secret ? 'password' : 'text', autocomplete: 'off', autocapitalize: 'off', spellcheck: false,
      placeholder: field.secret && connector ? '留空则保留已保存的值' : field.placeholder || '',
      value: !field.secret && saved !== undefined ? String(saved) : '',
    });
    return { field, input };
  });
  const save = h('button', { class: 'btn primary', type: 'submit' }, connector ? '保存' : '连接');
  const more = ui.disclosure('small', '更多选项', h('div', { class: 'stack connect-more' },
    h('label', { class: 'field' }, h('span', null, '标识'), ident, connector ? null : h('span', { class: 'muted small' }, '同一个品牌连接多次时用来区分。字母、数字、- 和 _')),
    h('label', { class: 'field' }, h('span', null, '显示名称'), title),
    h('label', { class: 'control-row' }, h('span', { class: 'control-label grow' }, '启用'), enabled)));
  const form = h('form', { class: 'stack' },
    h('div', { class: 'connect-brand' }, brandMark(spec.title, 'large', spec.kind),
      h('div', { class: 'connect-brand-text' }, h('strong', null, spec.title), spec.description ? h('span', { class: 'muted small' }, spec.description) : null)),
    inputs.map(({ field, input }) => h('label', { class: 'field' }, h('span', null, (field.label || field.name) + (field.optional ? '（可选）' : '')), input)),
    h('p', { class: 'muted small' }, '填写的令牌只保存在这台 Mac 上，不会发给任何其他设备。'),
    more,
    h('div', { class: 'row end' }, save));
  const close = sheet((connector ? '编辑 ' : '连接 ') + spec.title, form);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = ident.value.trim();
    if (!/^[A-Za-z0-9_-]+$/.test(id)) { more.open = true; notify('标识只能用字母、数字、- 和 _'); return; }
    const settings = {};
    for (const { field, input } of inputs) settings[field.name] = input.value.trim();
    const body = { kind: spec.kind, enabled: enabled.checked, settings };
    if (title.value.trim()) body.title = title.value.trim();
    const ok = await run(save, async () => {
      const result = await put('/api/home/connectors/' + encodeURIComponent(id), body);
      if (state) state.connectors = result.connectors || [];
      changed();
      notify(`已连接 ${spec.title}，找到 ${result.devices || 0} 台设备`, 'info');
    });
    if (ok) close();
  });
}

export async function remove(connector) {
  if (!await ui.ask(`移除“${connector.title || connector.id}”？它的设备会从家居里消失。`, { ok: '移除', danger: true })) return;
  const result = await del('/api/home/connectors/' + encodeURIComponent(connector.id));
  if (state) state.connectors = result.connectors || [];
  changed();
}

/** One connected connector as a row: mark, name, status, edit and remove. */
export function connectedRow(c) {
  const spec = specOf(c.kind);
  const status = !c.enabled ? h('span', { class: 'badge' }, '已停用')
    : c.error ? h('span', { class: 'badge error' }, '连接有问题') : h('span', { class: 'badge ok' }, `${c.devices || 0} 台设备`);
  return h('div', { class: 'connect-row' }, brandMark((spec && spec.title) || c.title || c.kind, '', c.kind),
    h('div', { class: 'connect-row-text' },
      h('span', null, c.title || (spec && spec.title) || c.id),
      h('span', { class: (c.error ? 'error-text' : 'muted') + ' small' }, c.error ? String(c.error) : ((spec && spec.title) || c.kind) + (c.id !== c.kind ? ' · ' + c.id : ''))),
    status,
    h('button', { class: 'icon-btn small', type: 'button', 'aria-label': `编辑 ${c.title || c.id}`, title: '编辑', onclick: () => edit(spec, c) }, icon('pen', 15)),
    action(icon('trash', 15), () => remove(c), { class: 'icon-btn small', 'aria-label': `移除 ${c.title || c.id}`, title: '移除' }));
}

const GROUPS = [['all', '全部'], ['hub', '中枢平台'], ['light', '灯光'], ['appliance', '家电与控制器']];

/** Opens the gallery: what is connected, and every brand that can be. */
export function open() {
  let group = 'all';
  const search = h('input', { type: 'search', class: 'connect-search-input', placeholder: '搜索品牌或平台', 'aria-label': '搜索品牌或平台', autocomplete: 'off' });
  const tabs = h('div', { class: 'seg', role: 'tablist', 'aria-label': '类别' }, GROUPS.map(([id, label]) =>
    h('button', { type: 'button', role: 'tab', dataset: { group: id }, onclick: () => { group = id; render(); } }, label)));
  const tools = h('div', { class: 'connect-tools' }, h('label', { class: 'connect-search' }, icon('search', 16), search), tabs);
  const list = h('div', { class: 'connect-body' }, h('p', { class: 'muted' }, '正在读取…'));
  const body = h('div', { class: 'connect' },
    h('p', { class: 'connect-lead' }, '连接家里在用的品牌或平台，设备会出现在家居页，也可以直接用一句话让 Jurvis 去操作。令牌只保存在这台 Mac 上。'),
    tools, list);
  const render = () => {
    if (!body.isConnected && body.dataset.shown) { watchers.delete(render); return; }
    if (!state) return;
    const q = search.value.trim().toLowerCase();
    for (const button of tabs.children) button.setAttribute('aria-selected', String(button.dataset.group === group));
    const connectors = state.connectors || [];
    const brands = (state.catalog || []).filter((c) => (group === 'all' || (c.category || 'appliance') === group)
      && (!q || (c.title + ' ' + c.kind + ' ' + (c.description || '')).toLowerCase().includes(q)));
    fill(list,
      connectors.length && !q && group === 'all' ? h('section', { class: 'connect-section' },
        h('h3', { class: 'connect-title' }, '已连接', h('span', null, String(connectors.length))),
        h('div', { class: 'connect-list' }, connectors.map(connectedRow))) : null,
      h('section', { class: 'connect-section' },
        h('h3', { class: 'connect-title' }, q ? '搜索结果' : '可以连接', h('span', null, String(brands.length))),
        brands.length ? h('div', { class: 'brand-grid' }, brands.map((spec) => {
          const count = connectors.filter((c) => c.kind === spec.kind).length;
          return h('button', { class: 'brand-card', type: 'button', 'aria-label': `连接 ${spec.title}`, onclick: () => edit(spec, null) },
            h('span', { class: 'brand-top' }, brandMark(spec.title, 'large', spec.kind),
              count ? h('span', { class: 'brand-tag on' }, icon('check', 12, 1.8), '已连接') : h('span', { class: 'brand-tag' }, spec.local ? '局域网' : '云端')),
            h('span', { class: 'brand-name' }, spec.title),
            h('span', { class: 'brand-note' }, spec.description || ''));
        })) : h('p', { class: 'connect-none' }, '没有匹配的品牌。可以先把它接进 Home Assistant，再连接 Home Assistant。')),
      h('p', { class: 'connect-foot' }, icon('link', 15), h('span', null, '没找到你的品牌？把它接进 Home Assistant 再连接 Home Assistant，或者在“设置 → MCP 服务”里添加它的 MCP 服务。')));
    body.dataset.shown = '1';
  };
  search.addEventListener('input', render);
  watchers.add(render);
  sheet('连接智能家居', body, { wide: true });
  const panel = body.closest('.sheet');
  if (panel) panel.classList.add('xl');
  load().then(render).catch((err) => {
    if (!(err instanceof ApiError && err.status === 401)) fill(list, h('p', { class: 'error-text' }, err.message));
  });
}
