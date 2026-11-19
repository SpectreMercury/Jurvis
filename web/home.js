// 家居: the home at a glance. One line says what is on, one box takes a request in plain words, and every device is a
// tile you tap to switch. A tile's "更多" opens the full controls for that device.
import { h, fill, get, post, run, action, notify, emit, sheet, ApiError } from './core.js';
import { icon, glyph } from './icons.js';
import { toggle, select, slider, number, color, ask } from './ui.js';
import * as connect from './connect.js';

const TYPES = { light: '灯', switch: '开关', outlet: '插座', climate: '空调', cover: '窗帘', lock: '门锁', fan: '风扇', media: '媒体', vacuum: '扫地机', sensor: '传感器', camera: '摄像头', scene: '场景', other: '设备' };
const BUTTONS = { open: '打开', close: '关闭', lock: '上锁', unlock: '解锁', activate: '执行' };
const EXAMPLES = ['我要睡了，把灯都关掉', '客厅有点热', '出门了，检查一下门锁'];

let devices = [];
let errors = {};
let loaded = false;
let dragging = false;
let dirty = false;
let timer = null;
let opened = null; // {id, body}: the device whose controls are showing in a sheet
const els = {};

export function mount(root) {
  els.refresh = action(icon('refresh', 16), () => load(true), { class: 'icon-btn', 'aria-label': '刷新设备状态', title: '刷新设备状态' });
  els.summary = h('p', { class: 'home-summary' }, '正在读取设备…');
  els.say = h('input', { class: 'home-ask-input', placeholder: '告诉 Jurvis：' + EXAMPLES[0], 'aria-label': '告诉 Jurvis 要做什么', autocomplete: 'off', enterKeyHint: 'send', maxLength: 400 });
  const send = h('button', { class: 'round-btn solid', type: 'submit', 'aria-label': '交给 Jurvis' }, icon('send', 16, 1.6));
  els.ask = h('form', { class: 'home-ask' }, h('span', { class: 'home-ask-mark' }, glyph('scene', 18)), els.say, send);
  els.ask.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = els.say.value.trim();
    if (!text) { els.say.focus(); return; }
    els.say.value = '';
    emit('ask', text);
  });
  els.errors = h('div', { class: 'stack' });
  els.body = h('div', { class: 'home-body' });
  root.append(h('div', { class: 'page home' },
    h('header', { class: 'home-head' },
      h('div', { class: 'home-heading' }, h('h1', null, '家居'),
        h('div', { class: 'row' }, h('button', { class: 'btn small', type: 'button', onclick: () => connect.open() }, icon('plus', 15), '连接品牌'), els.refresh)),
      els.summary),
    els.ask, els.errors, els.body));
  connect.onChange(() => { if (loaded) load(true).catch(() => {}); });
  window.addEventListener('pointerup', release);
  window.addEventListener('pointercancel', release);
}

function release() {
  if (!dragging) return;
  dragging = false;
  if (dirty) { dirty = false; render(); }
}

export function activate() {
  load(false).catch((err) => {
    if (err instanceof ApiError && err.status === 401) return;
    if (!loaded) els.summary.textContent = err.message;
    else notify(err.message);
  });
}

export function reset() {
  devices = [];
  errors = {};
  loaded = false;
  opened = null;
  connect.reset();
  clearTimeout(timer);
}

async function load(refresh) {
  const data = await get('/api/home/devices?refresh=' + (refresh ? 'true' : 'false'));
  devices = Array.isArray(data.devices) ? data.devices : [];
  errors = data.errors || {};
  loaded = true;
  render();
}

function later() {
  clearTimeout(timer);
  timer = setTimeout(() => { load(false).catch(() => {}); }, 1500);
}

async function control(device, name, value, element) {
  if ((name === 'lock' || name === 'unlock' || name === 'open') && (device.type === 'lock' || device.type === 'cover')) {
    const sure = await ask(`对“${device.name}”执行“${BUTTONS[name]}”？`, { ok: BUTTONS[name], danger: name === 'unlock' });
    if (!sure) { render(); return; }
  }
  const body = { device_id: device.id, action: name };
  if (value !== undefined) body.value = value;
  const ok = await run(element, () => post('/api/home/devices/control', body));
  if (ok) later();
  else render(); // put the control back to the real state
}

// ---------------------------------------------------------------- words
const degrees = (n) => (Math.round(Number(n) * 10) / 10) + '°';

/** The short state under a tile's name. */
function brief(d) {
  const s = d.state || {};
  if (!d.online) return '离线';
  if (d.type === 'climate') {
    const now = s.temperature !== undefined ? '室温 ' + degrees(s.temperature) : '';
    if (s.on === false) return ['关着', now].filter(Boolean).join(' · ');
    return [s.target_temperature !== undefined ? '设为 ' + degrees(s.target_temperature) : '开着', now].filter(Boolean).join(' · ');
  }
  if (d.type === 'cover') {
    if (s.position !== undefined) return Number(s.position) <= 0 ? '已关上' : `打开 ${Math.round(s.position)}%`;
    return typeof s.open === 'boolean' ? (s.open ? '开着' : '已关上') : '';
  }
  if (d.type === 'lock') return typeof s.locked === 'boolean' ? (s.locked ? '已上锁' : '未上锁') : '';
  if (typeof s.on === 'boolean') {
    if (!s.on) return '关着';
    return s.brightness !== undefined ? `亮度 ${Math.round(s.brightness)}%` : '开着';
  }
  if (s.value !== undefined && s.value !== '') return String(s.value) + (s.unit ? ' ' + s.unit : '');
  return '';
}

/** One sentence for the whole home. */
function summarize() {
  if (!devices.length) return '';
  const parts = [];
  const on = (type) => devices.filter((d) => d.type === type && d.online && d.state && d.state.on === true).length;
  const lights = on('light');
  parts.push(lights ? `${lights} 盏灯开着` : devices.some((d) => d.type === 'light') ? '灯都关着' : '');
  const climates = devices.filter((d) => d.type === 'climate');
  if (climates.length) parts.push(on('climate') ? '空调开着' : '空调关着');
  const locks = devices.filter((d) => d.type === 'lock' && d.state && typeof d.state.locked === 'boolean');
  if (locks.length) parts.push(locks.every((d) => d.state.locked) ? '门已上锁' : '有门没锁');
  const offline = devices.filter((d) => !d.online).length;
  if (offline) parts.push(`${offline} 台设备离线`);
  return parts.filter(Boolean).join('，') + '。';
}

// ---------------------------------------------------------------- full controls (in a sheet)
function row(label, ...kids) {
  return h('div', { class: 'ctl' }, h('span', { class: 'ctl-label' }, label), h('div', { class: 'ctl-body' }, kids));
}

function range(device, label, name, value, min, max, step, unit) {
  const start = Number.isFinite(Number(value)) ? Number(value) : min;
  const out = h('span', { class: 'ctl-value' }, Math.round(start) + unit);
  const input = slider({ min, max, step, value: start, disabled: !device.online, 'aria-label': `${device.name} ${label}` });
  input.addEventListener('pointerdown', () => { dragging = true; });
  input.addEventListener('input', () => { out.textContent = input.value + unit; });
  input.addEventListener('change', () => control(device, name, Number(input.value), null));
  return row(label, input, out);
}

function controls(d) {
  const s = d.state || {};
  const has = (name) => (d.actions || []).includes(name);
  const off = !d.online;
  const out = [];
  if (off) out.push(h('p', { class: 'note' }, '这台设备现在离线，暂时无法控制。'));
  if (has('turn_on') || has('turn_off') || has('toggle')) {
    const box = toggle({ checked: !!s.on, disabled: off, 'aria-label': `${d.name} 开关` });
    box.addEventListener('change', () => {
      const want = box.checked ? 'turn_on' : 'turn_off';
      control(d, has(want) ? want : 'toggle', undefined, box);
    });
    out.push(row('开关', h('span', { class: 'ctl-text' }, s.on ? '开着' : '关着'), box));
  }
  if (has('set_brightness')) out.push(range(d, '亮度', 'set_brightness', s.brightness, 0, 100, 1, '%'));
  if (has('set_color_temperature')) out.push(range(d, '色温', 'set_color_temperature', s.color_temperature === undefined ? 4000 : s.color_temperature, 2000, 6500, 100, 'K'));
  if (has('set_color')) {
    const picker = color({ value: /^#[0-9a-f]{6}$/i.test(String(s.color || '')) ? s.color : '#ffffff', disabled: off, 'aria-label': `${d.name} 颜色` });
    picker.addEventListener('change', () => control(d, 'set_color', picker.value, picker));
    out.push(row('颜色', h('span', { class: 'ctl-text' }, '点色块选择'), picker));
  }
  if (has('set_position')) out.push(range(d, '位置', 'set_position', s.position, 0, 100, 1, '%'));
  if (has('set_temperature')) {
    const start = Number(s.target_temperature !== undefined ? s.target_temperature : s.temperature);
    const field = number({ min: 5, max: 40, step: '0.5', value: Number.isFinite(start) ? start : 24, disabled: off, 'aria-label': `${d.name} 目标温度` });
    let wait = null;
    field.addEventListener('change', () => {
      clearTimeout(wait);
      wait = setTimeout(() => {
        const now = Number(field.value);
        if (!Number.isFinite(now) || now < 5 || now > 40) { notify('温度需在 5 到 40 度之间'); return; }
        control(d, 'set_temperature', now, null);
      }, 700);
    });
    out.push(row('温度', h('span', { class: 'ctl-text' }, s.temperature !== undefined ? '室温 ' + degrees(s.temperature) : '目标温度'), field));
  }
  if (has('set_mode') && Array.isArray(s.modes) && s.modes.length) {
    const menu = select({ value: s.modes.includes(s.mode) ? String(s.mode) : '', placeholder: s.mode ? String(s.mode) : '选择模式', disabled: off, 'aria-label': `${d.name} 模式` },
      s.modes.map((m) => ({ value: String(m), label: String(m) })));
    menu.addEventListener('change', () => { if (menu.value) control(d, 'set_mode', menu.value, menu); });
    out.push(row('模式', menu));
  }
  const buttons = Object.keys(BUTTONS).filter(has);
  if (buttons.length) {
    out.push(h('div', { class: 'ctl-actions' }, buttons.map((name) => {
      const button = h('button', { class: 'btn', type: 'button', disabled: off }, BUTTONS[name]);
      button.addEventListener('click', () => control(d, name, undefined, button));
      return button;
    })));
  }
  if (!out.length) out.push(h('p', { class: 'muted' }, '这台设备没有可以操作的项目。'));
  return out;
}

function details(d) {
  const body = h('div', { class: 'ctl-list' }, controls(d));
  opened = { id: d.id, body };
  sheet(d.name + (d.room ? ' · ' + d.room : ''), body);
}

// ---------------------------------------------------------------- tiles
function tile(d) {
  const s = d.state || {};
  const has = (name) => (d.actions || []).includes(name);
  const switchable = d.online && (has('turn_on') || has('turn_off') || has('toggle'));
  const on = d.online && s.on === true;
  const level = d.online && (on || d.type === 'cover') ? (s.brightness !== undefined ? s.brightness : s.position) : undefined;
  const kind = d.type === 'lock' && s.locked === false ? 'unlocked' : d.type;
  const main = h('button', { class: 'tile-main', type: 'button', 'aria-label': `${d.name}，${brief(d) || TYPES[d.type] || ''}` + (switchable ? '，点一下' + (on ? '关闭' : '打开') : '，点开查看') },
    h('span', { class: 'tile-glyph' }, glyph(kind, 24)),
    h('span', { class: 'tile-text' }, h('span', { class: 'tile-name' }, d.name), h('span', { class: 'tile-state' }, brief(d) || TYPES[d.type] || TYPES.other)),
    level !== undefined ? h('span', { class: 'tile-level', 'aria-hidden': 'true' }, h('span', { style: `width:${Math.max(0, Math.min(100, Number(level) || 0))}%` })) : null);
  if (switchable) {
    main.setAttribute('aria-pressed', String(on));
    main.addEventListener('click', () => {
      const want = on ? 'turn_off' : 'turn_on';
      control(d, has(want) ? want : 'toggle', undefined, main);
    });
  } else main.addEventListener('click', () => details(d));
  const more = h('button', { class: 'tile-more', type: 'button', 'aria-label': `${d.name} 的更多控制`, title: '更多控制', onclick: () => details(d) }, icon('dots', 16, 2));
  return h('div', { class: 'tile-dev' + (on ? ' on' : '') + (d.online ? '' : ' offline') }, main, switchable ? more : null);
}

function render() {
  if (dragging) { dirty = true; return; }
  fill(els.errors, ...Object.entries(errors).filter(([, text]) => text).map(([id, text]) =>
    h('div', { class: 'note error' }, `连接器 ${id}：${text}`)));
  els.ask.hidden = !devices.length;
  if (!devices.length) {
    els.summary.textContent = loaded ? '还没有接入设备。' : '正在读取设备…';
    fill(els.body, loaded ? h('div', { class: 'empty' },
      h('span', { class: 'tile round' }, glyph('other', 18)),
      h('p', { class: 'empty-title' }, '把家接进来'),
      h('p', { class: 'muted' }, '连接你家里在用的品牌或平台，设备就会出现在这里，之后可以点按控制，也可以直接用一句话让 Jurvis 去操作。'),
      h('button', { class: 'btn primary', type: 'button', onclick: () => connect.open() }, icon('plus', 15), '连接品牌')) : null);
    return;
  }
  els.summary.textContent = summarize();

  const out = [];
  const scenes = devices.filter((d) => d.type === 'scene');
  if (scenes.length) {
    out.push(h('section', { class: 'scenes', 'aria-label': '场景' }, scenes.map((d) => {
      const button = h('button', { class: 'scene', type: 'button', disabled: !d.online }, glyph('scene', 16), d.name);
      button.addEventListener('click', () => control(d, 'activate', undefined, button));
      return button;
    })));
  }
  const rooms = new Map();
  for (const d of devices) {
    if (d.type === 'scene') continue;
    const room = d.room || '其他';
    if (!rooms.has(room)) rooms.set(room, []);
    rooms.get(room).push(d);
  }
  const order = [...rooms].sort((a, b) => (a[0] === '其他') - (b[0] === '其他') || a[0].localeCompare(b[0], 'zh'));
  for (const [room, list] of order) {
    const sensors = list.filter((d) => d.type === 'sensor' || !(d.actions || []).length);
    const others = list.filter((d) => !sensors.includes(d));
    const readings = sensors.map((d) => `${d.name} ${brief(d) || '—'}`).join(' · ');
    out.push(h('section', { class: 'room', 'aria-label': room },
      h('div', { class: 'room-head' }, h('h2', null, room), readings ? h('span', { class: 'room-meta' }, readings) : null),
      others.length ? h('div', { class: 'tiles' }, others.map(tile)) : null));
  }
  fill(els.body, ...out);

  if (opened && opened.body.isConnected) {
    const device = devices.find((d) => d.id === opened.id);
    if (device) fill(opened.body, ...controls(device));
  } else opened = null;
}
