// Jurvis 的工作台: what the current task is producing — the document being written, the files it touched,
// the sources it read, and its plan. Everything here is derived from the conversation's messages.
import { h, fill, get, ApiError } from './core.js';
import { icon, fileIcon } from './icons.js';
import { renderMarkdown } from './md.js';
import { disclosure } from './ui.js';
import { artifactKind, viewer } from './preview.js';

const WRITES = new Set(['save_file', 'append_file', 'edit_file']);
const TABS = [['doc', '文档', 'doc'], ['files', '文件', 'folder'], ['sources', '来源', 'link']];

const els = {};
let tab = 'doc';
let view = { files: [], sources: [], plan: [], live: false, phase: '' };
let picked = null;      // a file the user opened by hand; null follows the latest written one
let loaded = { name: null, sig: null, content: null, error: null };
let loadSeq = 0;
let listed = null;      // every file in the workspace folder
let onClose = () => {};
const pane = viewer();   // the rendered page or image; one instance, so a re-render does not reload the frame

export function mount(close) {
  onClose = close;
  els.sub = h('span', { class: 'work-sub' });
  els.tabs = h('div', { class: 'seg', role: 'tablist', 'aria-label': '工作台视图' }, TABS.map(([id, label, glyph]) =>
    h('button', { type: 'button', role: 'tab', dataset: { tab: id }, onclick: () => { tab = id; render(); } }, icon(glyph, 14), label)));
  els.body = h('div', { class: 'work-body' });
  els.plan = disclosure('work-plan', null, null);
  els.plan.hidden = true;
  els.root = h('aside', { class: 'workspace', 'aria-label': 'Jurvis 的工作台' },
    h('header', { class: 'work-head' },
      h('button', { class: 'icon-btn work-close-phone', type: 'button', 'aria-label': '收起工作台', onclick: () => onClose() }, icon('down', 20, 1.6)),
      h('div', { class: 'work-heading' }, h('span', null, 'Jurvis 的工作台'), els.sub),
      h('button', { class: 'icon-btn', type: 'button', 'aria-label': '关闭工作台', title: '关闭工作台', onclick: () => onClose() }, icon('x', 18))),
    h('div', { class: 'work-tabs' }, els.tabs),
    els.body, els.plan);
  return els.root;
}

export function reset() {
  picked = null;
  loaded = { name: null, sig: null, content: null, error: null };
  listed = null;
  tab = 'doc';
  lastSig = '';
}

/** Reads the conversation and returns what the workspace should show. */
export function derive(messages, live, phase) {
  const files = new Map(); // name -> {name, writes, last, running}
  const sources = [];
  const seen = new Set();
  let plan = [];
  for (const m of messages) {
    if (m.role === 'user') continue;
    const meta = m.metadata || {};
    if (Array.isArray(meta.plan) && meta.plan.length) plan = meta.plan;
    for (const s of Array.isArray(meta.sources) ? meta.sources : []) {
      const url = String((s && s.url) || '');
      if (!/^https?:\/\//i.test(url) || seen.has(url)) continue;
      seen.add(url);
      sources.push({ url, title: String(s.title || url) });
    }
    for (const step of Array.isArray(meta.steps) ? meta.steps : []) {
      for (const call of (step && step.calls) || []) {
        const name = call && call.arguments && typeof call.arguments.name === 'string' ? call.arguments.name.trim() : '';
        if (!name || !(WRITES.has(call.name) || call.name === 'read_file')) continue;
        const entry = files.get(name) || { name, writes: 0, running: false, written: false };
        if (WRITES.has(call.name)) {
          entry.written = true;
          if (call.status === 'ok') entry.writes += 1;
          entry.running = call.status === 'running';
          files.delete(name); // keep the most recently written file last
        }
        files.set(name, entry);
      }
    }
  }
  return { files: [...files.values()], sources, plan, live: !!live, phase: phase || '' };
}

export const hasContent = (v) => v.files.length > 0 || v.sources.length > 0 || v.plan.length > 0;

let lastSig = '';
export function update(next, force) {
  const sig = JSON.stringify(next);
  if (sig === lastSig && !force && els.body && els.body.firstChild) return;
  lastSig = sig;
  view = next;
  if (picked && !view.files.some((f) => f.name === picked) && !(listed || []).some((f) => f.name === picked)) picked = null;
  render();
}

function current() {
  if (picked) return view.files.find((f) => f.name === picked) || { name: picked, writes: 0, running: false };
  const written = view.files.filter((f) => f.written);
  return written[written.length - 1] || view.files[view.files.length - 1] || null;
}

function count(text) {
  const n = text.replace(/\s/g, '').length;
  return n.toLocaleString('en-US') + ' 字';
}

function loadDoc(file) {
  const sig = file.name + '|' + file.writes + '|' + (file.running ? Math.floor(Date.now() / 4000) : '');
  if (loaded.name === file.name && loaded.sig === sig) return;
  const fresh = loaded.name !== file.name;
  loaded = { name: file.name, sig, content: fresh ? null : loaded.content, error: null };
  const seq = ++loadSeq;
  get('/api/files/' + encodeURIComponent(file.name)).then((result) => {
    if (seq !== loadSeq) return;
    loaded.content = String(result.content || '');
    if (tab === 'doc') render();
  }).catch((err) => {
    if (seq !== loadSeq || (err instanceof ApiError && err.status === 401)) return;
    if (loaded.content === null) loaded.error = file.running ? '' : err.message;
    if (tab === 'doc') render();
  });
}

function open(name) {
  picked = name;
  tab = 'doc';
  render();
}

function renderDoc() {
  const file = current();
  if (!file) {
    return h('div', { class: 'work-empty' }, h('span', { class: 'tile round' }, icon('doc')),
      h('p', null, '这个任务还没有写下文件。Jurvis 开始写文档后，内容会实时显示在这里。'));
  }
  loadDoc(file);
  const text = loaded.name === file.name ? loaded.content : null;
  const bar = h('div', { class: 'work-file' }, icon(fileIcon(file.name), 15), h('span', { class: 'file-name clip' }, file.name),
    file.running ? h('span', { class: 'live' }, '正在写入') : h('span', { class: 'small' }, text === null ? '' : count(text)));
  let body;
  if (text === null) body = h('p', { class: 'muted small' }, loaded.error || (file.running ? '正在写入第一段…' : '正在读取…'));
  else if (artifactKind(file.name)) {
    pane.show(file.name, text, { live: file.running });
    return [bar, pane.el];
  }
  else if (/\.(md|markdown|txt)$/i.test(file.name) || !/\./.test(file.name)) body = h('div', { class: 'md' }, renderMarkdown(text));
  else body = h('pre', { class: 'plain' }, text);
  const scroll = h('div', { class: 'work-scroll' }, body);
  if (file.running) requestAnimationFrame(() => { scroll.scrollTop = scroll.scrollHeight; });
  return [bar, scroll];
}

function size(bytes) {
  return bytes < 1000 ? bytes + ' B' : bytes < 1e6 ? (bytes / 1000).toFixed(1) + ' KB' : (bytes / 1e6).toFixed(1) + ' MB';
}

function renderFiles() {
  if (listed === null) {
    listed = [];
    get('/api/files').then((result) => { listed = Array.isArray(result.files) ? result.files : []; if (tab === 'files') render(); }).catch(() => {});
  }
  const mine = [...view.files].reverse();
  const names = new Set(mine.map((f) => f.name));
  const others = (listed || []).filter((f) => !names.has(f.name));
  const row = (name, note) => h('button', { class: 'work-item', type: 'button', onclick: () => open(name) },
    icon(fileIcon(name)), h('span', { class: 'file-name' }, name), h('span', { class: 'small' }, note));
  if (!mine.length && !others.length) {
    return h('div', { class: 'work-empty' }, h('span', { class: 'tile round' }, icon('folder')), h('p', null, '工作区里还没有文件。'));
  }
  return h('div', { class: 'work-scroll' },
    mine.length ? [h('div', { class: 'group-title' }, '这个任务用到的'), h('div', { class: 'work-list' },
      mine.map((f) => row(f.name, f.running ? '正在写入' : f.written ? (f.writes > 1 ? `改了 ${f.writes} 次` : '已写入') : '读取过')))] : null,
    others.length ? [h('div', { class: 'group-title' }, '工作区里的其他文件'), h('div', { class: 'work-list' }, others.map((f) => row(f.name, size(Number(f.bytes) || 0))))] : null);
}

function renderSources() {
  if (!view.sources.length) {
    return h('div', { class: 'work-empty' }, h('span', { class: 'tile round' }, icon('link')), h('p', null, '这个任务还没有引用网页。联网搜索到的来源会列在这里。'));
  }
  return h('div', { class: 'work-scroll' }, h('div', { class: 'work-list' }, view.sources.map((s) => {
    let host = '';
    try { host = new URL(s.url).host.replace(/^www\./, ''); } catch { /* keep empty */ }
    return h('a', { class: 'work-item', href: s.url, target: '_blank', rel: 'noopener noreferrer' },
      icon('globe'), h('span', { class: 'work-item-text' }, s.title), h('span', { class: 'small' }, host));
  })));
}

/** The plan as rows with status marks; shared with the conversation view. */
export function planRows(plan, live) {
  const next = plan.findIndex((item) => item.status !== 'done');
  return h('div', { class: 'plan' }, plan.map((item, index) => {
    const done = item.status === 'done';
    const now = !done && index === next && live;
    return h('div', { class: 'plan-row' + (done ? '' : now ? ' now' : ' pending') },
      h('span', { class: 'mark' + (done ? ' ok' : now ? ' running' : ''), role: 'img', 'aria-label': done ? '完成' : now ? '进行中' : '未开始' }, done ? icon('check', 11, 1.8) : null),
      h('span', null, String(item.step || '')));
  }));
}

function render() {
  if (!els.root) return;
  const file = current();
  els.sub.className = 'work-sub' + (view.live ? ' live' : '');
  els.sub.textContent = view.live ? (view.phase || '进行中') : file ? file.name : '文档、文件和来源';
  for (const button of els.tabs.children) button.setAttribute('aria-selected', String(button.dataset.tab === tab));
  const before = els.body.querySelector('.work-scroll');
  const top = before ? before.scrollTop : 0;
  const kids = tab === 'doc' ? renderDoc() : tab === 'files' ? renderFiles() : renderSources();
  // The preview pane stays where it is: taking an iframe out of the page and putting it back reloads it.
  if (Array.isArray(kids) && kids[1] === pane.el && pane.el.parentNode === els.body && pane.el.previousSibling) pane.el.previousSibling.replaceWith(kids[0]);
  else fill(els.body, kids);
  const after = els.body.querySelector('.work-scroll');
  if (after && top && !(file && file.running && tab === 'doc')) after.scrollTop = top;

  const plan = view.plan;
  els.plan.hidden = !plan.length;
  if (plan.length) {
    const done = plan.filter((item) => item.status === 'done').length;
    const next = plan.find((item) => item.status !== 'done');
    fill(els.plan.head, icon('list'),
      h('span', { class: 'work-bar-text' }, h('span', null, `计划 · 已完成 ${done} / ${plan.length}`),
        h('span', null, next ? '接下来：' + next.step : '全部完成')),
      icon('down', 16));
    fill(els.plan.panel, planRows(plan, view.live));
  }
}

/** One line for the bar above the composer on a phone. */
export function summary(v) {
  const written = v.files.filter((f) => f.written);
  const file = written[written.length - 1] || v.files[v.files.length - 1];
  if (file) return { title: file.running ? '正在写 ' + file.name : file.name, sub: v.files.length > 1 ? `${v.files.length} 个文件` + (v.sources.length ? ` · ${v.sources.length} 个来源` : '') : (v.sources.length ? `${v.sources.length} 个来源` : '点开查看内容'), glyph: file.running ? 'pen' : 'doc' };
  if (v.plan.length) return { title: `计划 · 已完成 ${v.plan.filter((i) => i.status === 'done').length} / ${v.plan.length}`, sub: '点开查看', glyph: 'list' };
  return { title: `${v.sources.length} 个来源`, sub: '点开查看', glyph: 'link' };
}
