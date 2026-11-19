// Jurvis's own controls. Nothing here uses a browser-default widget: the switch, select, slider, number field, colour
// picker, disclosure and confirm dialog are all drawn by app.css and look the same in every browser.
// Each control keeps the small surface the rest of the code already used on native elements:
// `.value` / `.checked` / `.disabled`, and `change` (plus `input` while dragging) events.
import { h } from './core.js';
import { icon } from './icons.js';

const fire = (el, name) => el.dispatchEvent(new Event(name, { bubbles: true }));
const classes = (base, extra) => (extra ? base + ' ' + extra : base);

// ---------------------------------------------------------------- popover
/** Shows `content` anchored under (or above) `anchor`. Returns close(). */
export function popover(anchor, content, onClose) {
  const box = h('div', { class: 'popover' }, content);
  document.body.append(box);
  const r = anchor.getBoundingClientRect();
  box.style.minWidth = Math.round(r.width) + 'px';
  const width = box.offsetWidth, height = box.offsetHeight;
  const below = r.bottom + 4 + height <= window.innerHeight - 8 || r.top - 4 - height < 8;
  box.style.left = Math.round(Math.min(Math.max(8, r.left), window.innerWidth - width - 8)) + 'px';
  box.style.top = Math.round(below ? r.bottom + 4 : r.top - 4 - height) + 'px';
  const items = () => [...box.querySelectorAll('button:not(:disabled), input')];
  const close = () => {
    if (!box.isConnected) return;
    box.remove();
    document.removeEventListener('pointerdown', onDown, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', close);
    window.removeEventListener('scroll', onScroll, true);
    if (onClose) onClose();
  };
  const onDown = (event) => { if (!box.contains(event.target) && !anchor.contains(event.target)) close(); };
  const onScroll = (event) => { if (!box.contains(event.target)) close(); };
  const onKey = (event) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      close();
      anchor.focus();
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      const list = items();
      if (!list.length) return;
      event.preventDefault();
      const at = list.indexOf(document.activeElement);
      list[(at + (event.key === 'ArrowDown' ? 1 : list.length - 1) + (at < 0 && event.key === 'ArrowUp' ? 1 : 0)) % list.length].focus();
    } else if (event.key === 'Tab') close();
  };
  document.addEventListener('pointerdown', onDown, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', close);
  window.addEventListener('scroll', onScroll, true);
  return close;
}

// ---------------------------------------------------------------- switch
/** toggle({checked, disabled, 'aria-label'}) -> a switch with `.checked`; fires `change`. */
export function toggle(attrs = {}) {
  const { checked, class: extra, ...rest } = attrs;
  let on = !!checked;
  const el = h('button', { type: 'button', role: 'switch', class: classes('switch', extra), ...rest }, h('span', { class: 'switch-thumb' }));
  const sync = () => el.setAttribute('aria-checked', String(on));
  Object.defineProperty(el, 'checked', { get: () => on, set: (value) => { on = !!value; sync(); } });
  el.addEventListener('click', () => { on = !on; sync(); fire(el, 'change'); });
  sync();
  return el;
}

// ---------------------------------------------------------------- select
/** select({value, placeholder, …}, [{value, label}]) -> a menu button with `.value`; fires `change`. */
export function select(attrs = {}, options = []) {
  const { value, placeholder = '请选择', class: extra, ...rest } = attrs;
  const items = options.map((o) => ({ value: String(o.value), label: String(o.label === undefined ? o.value : o.label) }));
  let current = value === undefined || value === null ? '' : String(value);
  let close = null;
  const label = h('span', { class: 'select-label' });
  const el = h('button', { type: 'button', class: classes('select', extra), 'aria-haspopup': 'listbox', 'aria-expanded': 'false', ...rest }, label, icon('down', 14));
  const sync = () => {
    const item = items.find((i) => i.value === current);
    label.textContent = item ? item.label : placeholder;
    label.classList.toggle('placeholder', !item);
  };
  Object.defineProperty(el, 'value', { get: () => current, set: (next) => { current = next === undefined || next === null ? '' : String(next); sync(); } });
  const open = () => {
    if (close) { close(); return; }
    const list = h('div', { class: 'menu', role: 'listbox' }, items.map((item) => {
      const chosen = item.value === current;
      const row = h('button', { type: 'button', class: 'menu-item', role: 'option', 'aria-selected': String(chosen) },
        h('span', { class: 'clip' }, item.label), chosen ? icon('check', 14, 1.8) : null);
      row.addEventListener('click', () => {
        close();
        el.focus();
        if (item.value === current) return;
        current = item.value;
        sync();
        fire(el, 'change');
      });
      return row;
    }));
    el.setAttribute('aria-expanded', 'true');
    close = popover(el, list, () => { close = null; el.setAttribute('aria-expanded', 'false'); });
    const first = list.querySelector('[aria-selected="true"]') || list.firstChild;
    if (first) first.focus();
  };
  el.addEventListener('click', open);
  el.addEventListener('keydown', (event) => {
    if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && !close) { event.preventDefault(); open(); }
  });
  sync();
  return el;
}

// ---------------------------------------------------------------- slider
/** slider({min, max, step, value, disabled}) -> `.value` (a string, like a range input); `input` while moving, `change` at the end. */
export function slider(attrs = {}) {
  const { min = '0', max = '100', step = '1', value, disabled, class: extra, ...rest } = attrs;
  const low = Number(min), high = Number(max), unit = Number(step) || 1;
  const clamp = (n) => Math.min(high, Math.max(low, n));
  const snap = (n) => clamp(Number((Math.round((n - low) / unit) * unit + low).toFixed(6)));
  let current = snap(Number.isFinite(Number(value)) ? Number(value) : low);
  let off = !!disabled;
  const bar = h('span', { class: 'slider-fill' });
  const thumb = h('span', { class: 'slider-thumb' });
  const el = h('div', { class: classes('slider', extra), role: 'slider', 'aria-valuemin': String(low), 'aria-valuemax': String(high), ...rest },
    h('span', { class: 'slider-track' }, bar), thumb);
  const sync = () => {
    const percent = high > low ? ((current - low) / (high - low)) * 100 : 0;
    bar.style.width = percent + '%';
    el.style.setProperty('--p', String(percent / 100));
    el.setAttribute('aria-valuenow', String(current));
    el.tabIndex = off ? -1 : 0;
    if (off) el.setAttribute('aria-disabled', 'true');
    else el.removeAttribute('aria-disabled');
  };
  Object.defineProperty(el, 'value', { get: () => String(current), set: (next) => { current = snap(Number(next) || 0); sync(); } });
  Object.defineProperty(el, 'disabled', { get: () => off, set: (next) => { off = !!next; sync(); } });
  const fromPointer = (event) => {
    const r = el.getBoundingClientRect();
    const next = snap(low + ((event.clientX - r.left - 8) / Math.max(1, r.width - 16)) * (high - low));
    if (next !== current) { current = next; sync(); fire(el, 'input'); }
  };
  let before = current;
  el.addEventListener('pointerdown', (event) => {
    if (off) return;
    before = current;
    el.setPointerCapture(event.pointerId);
    el.classList.add('dragging');
    fromPointer(event);
  });
  el.addEventListener('pointermove', (event) => { if (el.hasPointerCapture(event.pointerId)) fromPointer(event); });
  const end = (event) => {
    if (!el.hasPointerCapture(event.pointerId)) return;
    el.releasePointerCapture(event.pointerId);
    el.classList.remove('dragging');
    if (current !== before) fire(el, 'change');
  };
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);
  el.addEventListener('keydown', (event) => {
    if (off) return;
    const big = Math.max(unit, (high - low) / 10);
    const move = { ArrowRight: unit, ArrowUp: unit, ArrowLeft: -unit, ArrowDown: -unit, PageUp: big, PageDown: -big }[event.key];
    const next = event.key === 'Home' ? low : event.key === 'End' ? high : move === undefined ? null : snap(current + move);
    if (next === null) return;
    event.preventDefault();
    if (next === current) return;
    current = next;
    sync();
    fire(el, 'input');
    fire(el, 'change');
  });
  sync();
  return el;
}

// ---------------------------------------------------------------- number
/** number({min, max, step, value, 'aria-label'}) -> − field + with `.value`; fires `change`. */
export function number(attrs = {}) {
  const { min, max, step = '1', value = '', class: extra, disabled, ...rest } = attrs;
  const unit = Number(step) || 1;
  const digits = (String(step).split('.')[1] || '').length;
  const clamp = (n) => Math.min(max === undefined ? Infinity : Number(max), Math.max(min === undefined ? -Infinity : Number(min), n));
  const input = h('input', { class: 'number-input', inputMode: digits ? 'decimal' : 'numeric', autocomplete: 'off', value: String(value), disabled: !!disabled, ...rest });
  const bump = (delta) => () => {
    const now = Number(input.value);
    input.value = String(Number(clamp((Number.isFinite(now) ? now : 0) + delta).toFixed(digits)));
    fire(el, 'change');
  };
  const less = h('button', { type: 'button', class: 'number-step', 'aria-label': '减少', disabled: !!disabled, onclick: bump(-unit) }, icon('minus', 14));
  const more = h('button', { type: 'button', class: 'number-step', 'aria-label': '增加', disabled: !!disabled, onclick: bump(unit) }, icon('plus', 14));
  const el = h('div', { class: classes('number', extra) }, less, input, more);
  Object.defineProperty(el, 'value', { get: () => input.value, set: (next) => { input.value = String(next); } });
  Object.defineProperty(el, 'disabled', { get: () => input.disabled, set: (next) => { input.disabled = less.disabled = more.disabled = !!next; } });
  return el;
}

// ---------------------------------------------------------------- colour
const SWATCHES = ['#ffffff', '#ffe2b8', '#ffd08a', '#cfe4ff', '#ff453a', '#ff9f0a', '#ffd60a', '#30d158', '#40c8e0', '#0a84ff', '#5e5ce6', '#bf5af2'];
/** color({value}) -> a swatch that opens a palette; `.value` is #rrggbb; fires `change`. */
export function color(attrs = {}) {
  const { value = '#ffffff', class: extra, ...rest } = attrs;
  let current = String(value).toLowerCase();
  let close = null;
  const dot = h('span', { class: 'swatch-dot' });
  const el = h('button', { type: 'button', class: classes('swatch', extra), 'aria-haspopup': 'dialog', ...rest }, dot);
  const sync = () => { dot.style.background = current; };
  Object.defineProperty(el, 'value', { get: () => current, set: (next) => { current = String(next).toLowerCase(); sync(); } });
  const pick = (next) => {
    if (close) close();
    el.focus();
    if (next === current) return;
    current = next;
    sync();
    fire(el, 'change');
  };
  el.addEventListener('click', () => {
    if (close) { close(); return; }
    const hex = h('input', { class: 'input', value: current, maxLength: 7, spellcheck: false, autocapitalize: 'off', autocomplete: 'off', 'aria-label': '十六进制颜色' });
    hex.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      const text = hex.value.trim().toLowerCase();
      if (/^#[0-9a-f]{6}$/.test(text)) pick(text);
    });
    const body = h('div', { class: 'palette' },
      h('div', { class: 'palette-grid' }, SWATCHES.map((c) => {
        const cell = h('button', { type: 'button', class: 'palette-cell', 'aria-label': c, 'aria-pressed': String(c === current), onclick: () => pick(c) });
        cell.style.background = c;
        return cell;
      })), hex);
    close = popover(el, body, () => { close = null; });
  });
  sync();
  return el;
}

// ---------------------------------------------------------------- disclosure
/** A row that opens to show more. `.open` works like <details>; `.head` and `.panel` can be refilled. */
export function disclosure(cls, head, body, open = false) {
  const button = h('button', { type: 'button', class: 'disc-head' }, head);
  const panel = h('div', { class: 'disc-body' }, body);
  const el = h('div', { class: classes('disc', cls) }, button, panel);
  let shown = false;
  const set = (next) => {
    shown = !!next;
    button.setAttribute('aria-expanded', String(shown));
    panel.hidden = !shown;
    if (shown) el.setAttribute('open', '');
    else el.removeAttribute('open');
  };
  Object.defineProperty(el, 'open', { get: () => shown, set });
  el.head = button;
  el.panel = panel;
  button.addEventListener('click', () => set(!shown));
  set(open);
  return el;
}

// ---------------------------------------------------------------- confirm
/** ask('删除这条记忆？', {ok: '删除', danger: true}) -> Promise<boolean>. Replaces window.confirm. */
export function ask(message, { title = '', ok = '确定', cancel = '取消', danger = false } = {}) {
  return new Promise((resolve) => {
    const previous = document.activeElement;
    const done = (answer) => {
      overlay.remove();
      document.removeEventListener('keydown', onKey, true);
      if (previous && previous.focus && previous.isConnected) previous.focus();
      resolve(answer);
    };
    const onKey = (event) => {
      if (event.key === 'Escape') { event.stopPropagation(); done(false); }
      else if (event.key === 'Tab') {
        event.preventDefault();
        (document.activeElement === yes ? no : yes).focus();
      }
    };
    const no = h('button', { type: 'button', class: 'btn', onclick: () => done(false) }, cancel);
    const yes = h('button', { type: 'button', class: 'btn primary' + (danger ? ' destructive' : ''), onclick: () => done(true) }, ok);
    const box = h('div', { class: 'dialog', role: 'alertdialog', 'aria-modal': 'true', 'aria-label': title || message },
      title ? h('h2', null, title) : null, h('p', { class: title ? 'muted' : '' }, message), h('div', { class: 'dialog-actions' }, no, yes));
    const overlay = h('div', { class: 'overlay centered', onclick: (event) => { if (event.target === overlay) done(false); } }, box);
    document.body.append(overlay);
    document.addEventListener('keydown', onKey, true);
    (danger ? no : yes).focus();
  });
}
