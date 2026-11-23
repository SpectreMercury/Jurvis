// 日志: what Jurvis did (a work record the engine writes itself) and what it said to itself while doing it.
// The second part belongs to a character the user makes: its lines come up as bubbles beside it, and tapping it
// opens a conversation in which it can be asked what troubles it and what the user could do better.
import { store, h, fill, get, post, put, del, api, action, notify, sheet, emit, on, ApiError } from './core.js';
import * as ui from './ui.js';
import { icon } from './icons.js';

const VIEWS = [['work', '做了什么'], ['mind', '心里话'], ['improve', '可以更好']];
const OWNERS = [['user', '你可以帮我的', '说得再清楚一点，我就不用猜'], ['model', '我自己要改的', '我犯的错和绕的路'],
  ['jurvis', 'Jurvis 该修的', '工具、应用或给我的说明挡了路'], ['external', '外面的服务', '网站、接口这些不在我们手里的']];
const ORIGINS = { chat: '对话', goal: '目标', task: '后台任务', schedule: '定时任务', checkin: '主动问候' };
const KINDS = { say: '边做边说', thought: '随口一句', doubt: '拿不准', friction: '被挡了一下', slip: '我的失误', glad: '挺顺的' };
const ASKS = ['你最近有什么困扰？', '我哪里说得不够清楚？', '你自己哪里没做好？'];
const BUBBLE_MS = 5200;

let view = 'work';
let day = null;          // null = today
let avatar = null;       // /api/avatar
let lastNote = 0;        // newest mind line already seen
let recent = [];         // the latest few lines, for the page
let queue = [];          // lines waiting to come up beside the character
let showing = false;
let mood = 'calm';
let calmTimer = null;
let loading = null;
let lastPoll = 0;
const opened = new Set(); // task cards the user unfolded: they stay open when a new line arrives
let editor = null;       // the open "make a character" sheet: {render, close}
const els = {};
const figures = new Set();   // every drawn character on screen, so a change of mood reaches all of them

const clock = (seconds) => new Date(seconds * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
const dated = (seconds) => new Date(seconds * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const today = () => { const d = new Date(); return [d.getFullYear(), String(d.getMonth() + 1).padStart(2, '0'), String(d.getDate()).padStart(2, '0')].join('-'); };

// ---------------------------------------------------------------- the character
// Its pictures are kept apart from the conversation's: those are let go whenever the messages on screen change.
const pictures = new Map();
function assetURL(id) {
  if (!pictures.has(id)) {
    const loading = get('/api/assets/' + encodeURIComponent(id) + '/file', { blob: true }).then((blob) => URL.createObjectURL(blob));
    loading.catch(() => pictures.delete(id));
    pictures.set(id, loading);
  }
  return pictures.get(id);
}
const SVG = 'http://www.w3.org/2000/svg';
function shape(tag, attrs) {
  const el = document.createElementNS(SVG, tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, value);
  return el;
}
/** The character everyone starts with, until they make their own: a small round fellow whose face shows the mood. */
function plainFigure(current) {
  const svg = shape('svg', { viewBox: '0 0 100 100', class: 'figure-plain', 'aria-hidden': 'true' });
  const ink = { fill: 'none', stroke: 'currentColor', 'stroke-width': 3, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' };
  svg.append(shape('path', { d: 'M26 34 L20 14 L40 24 M74 34 L80 14 L60 24', ...ink, fill: 'var(--fill-strong)' }),
    shape('ellipse', { cx: 50, cy: 56, rx: 36, ry: 34, ...ink, fill: 'var(--fill-strong)' }));
  const eyes = {
    calm: [['circle', { cx: 37, cy: 52, r: 3.2, fill: 'currentColor' }], ['circle', { cx: 63, cy: 52, r: 3.2, fill: 'currentColor' }]],
    thinking: [['circle', { cx: 39, cy: 48, r: 3.2, fill: 'currentColor' }], ['circle', { cx: 65, cy: 48, r: 3.2, fill: 'currentColor' }]],
    troubled: [['path', { d: 'M31 46 L42 49 M69 46 L58 49', ...ink }], ['circle', { cx: 37, cy: 55, r: 3, fill: 'currentColor' }], ['circle', { cx: 63, cy: 55, r: 3, fill: 'currentColor' }]],
    happy: [['path', { d: 'M31 53 Q37 46 43 53 M57 53 Q63 46 69 53', ...ink }]],
    guilty: [['circle', { cx: 41, cy: 53, r: 3.2, fill: 'currentColor' }], ['circle', { cx: 67, cy: 53, r: 3.2, fill: 'currentColor' }]],
    sleepy: [['path', { d: 'M31 52 H43 M57 52 H69', ...ink }]],
  };
  const mouths = {
    calm: 'M44 66 Q50 70 56 66', thinking: 'M46 68 H55', troubled: 'M43 70 Q50 64 57 70', happy: 'M41 64 Q50 76 59 64 Z',
    guilty: 'M42 68 Q46 64 50 68 T58 68', sleepy: 'M47 67 a3 4 0 1 0 6 0 a3 4 0 1 0 -6 0',
  };
  for (const [tag, attrs] of eyes[current] || eyes.calm) svg.append(shape(tag, attrs));
  svg.append(shape('path', { d: mouths[current] || mouths.calm, ...ink, ...(current === 'happy' ? { fill: 'currentColor' } : {}) }));
  if (current === 'troubled') svg.append(shape('path', { d: 'M78 34 q4 7 0 10 q-4 -3 0 -10', fill: 'none', stroke: 'currentColor', 'stroke-width': 2 }));
  return svg;
}

/** A place the character stands. It redraws itself when the character or its mood changes. */
function figure(size) {
  const el = h('span', { class: 'figure', style: `width:${size}px;height:${size}px` });
  el.draw = () => {
    const faces = (avatar && avatar.faces) || {};
    const face = faces[mood] || faces.calm;
    const key = face ? 'a:' + face : 'p:' + mood;
    if (el.dataset.key === key) return;
    el.dataset.key = key;
    if (!face) { fill(el, plainFigure(mood)); return; }
    assetURL(face).then((url) => {
      if (el.dataset.key !== key) return;
      fill(el, h('img', { src: url, alt: '', draggable: false }));
    }, () => { el.dataset.key = ''; });
  };
  figures.add(el);
  el.draw();
  return el;
}
function setMood(next) {
  mood = next;
  for (const el of figures) { if (el.isConnected || el === els.buddyFigure || el === els.stageFigure) el.draw(); else figures.delete(el); }
}
const name = () => (avatar && avatar.name) || '小维';

// ---------------------------------------------------------------- bubbles beside the character (on every screen)
/** The character in the sidebar, with what it just said to itself. */
export function buddy() {
  els.buddyFigure = figure(52);
  els.bubbles = h('div', { class: 'buddy-bubbles', 'aria-live': 'polite' });
  els.buddy = h('button', { class: 'buddy', type: 'button', title: '看看它做了什么、在想什么', onclick: () => emit('goto', 'journal') }, els.buddyFigure, els.bubbles);
  return els.buddy;
}

function bubble(note, cls = '') {
  return h('span', { class: 'bubble ' + (note.owner ? 'flag ' : '') + cls, title: KINDS[note.kind] || '' }, note.text);
}

function pump() {
  if (showing || !queue.length || !els.bubbles) return;
  showing = true;
  if (queue.length > 4) queue = queue.slice(-4);  // it has been busy: only the latest few are worth a bubble
  const note = queue.shift();
  const el = bubble(note, 'pop');
  els.bubbles.append(el);
  els.buddy.classList.add('talking');
  while (els.bubbles.children.length > 2) els.bubbles.firstChild.remove();
  setMood(note.mood || 'calm');
  clearTimeout(calmTimer);
  setTimeout(() => {
    showing = false;
    if (queue.length) { pump(); return; }
    calmTimer = setTimeout(() => {
      fill(els.bubbles);
      els.buddy.classList.remove('talking');
      setMood(working() ? 'thinking' : 'calm');
    }, BUBBLE_MS);
  }, queue.length ? 2600 : BUBBLE_MS / 2);
}
const working = () => ((store.state && store.state.jobs) || []).some((j) => j.status === 'running' && j.kind === 'chat');

/** The engine wrote a line of the journal. */
export async function onNote() {
  try {
    const { notes } = await get('/api/journal/mind?after=' + lastNote);
    if (!notes.length) { if (store.tab === 'journal') refresh(true); return; }
    lastNote = notes[notes.length - 1].id;
    recent = [...recent, ...notes].slice(-4);
    queue.push(...notes);
    pump();
  } catch { /* the next line will bring it */ }
  if (store.tab === 'journal') { renderStage(); refresh(true); }
}

/** The character changed: a new drawing, a new face, a new name. */
export async function onAvatar() {
  try { avatar = await get('/api/avatar'); } catch { return; }
  for (const el of figures) { el.dataset.key = ''; if (el.isConnected) el.draw(); }
  if (els.stage) renderStage();
  if (editor) editor.render();
}

export function onState() {
  // While a character is being drawn its job reports progress; the state feed says so, and the page follows.
  if (avatar && (avatar.status === 'drafting' || avatar.status === 'making') && Date.now() - lastPoll > 1500) { lastPoll = Date.now(); onAvatar(); }
  if (!showing && !queue.length && (!els.bubbles || !els.bubbles.children.length)) setMood(working() ? 'thinking' : 'calm');
}

/** Called once the app is in: learn who the character is, and where the journal stands so old lines do not replay. */
export async function start() {
  try {
    const [who, mind] = await Promise.all([get('/api/avatar'), get('/api/journal/mind?limit=4')]);
    avatar = who;
    recent = mind.notes;
    lastNote = recent.length ? recent[recent.length - 1].id : 0;
    for (const el of figures) { el.dataset.key = ''; el.draw(); }
    if (els.stage) renderStage();
  } catch { /* offline: the page loads it again when opened */ }
}

export function reset() { avatar = null; recent = []; queue = []; lastNote = 0; day = null; }

// ---------------------------------------------------------------- the page
export function mount(root) {
  els.stage = h('section', { class: 'card stage' });
  els.tabs = h('div', { class: 'seg', role: 'tablist' }, VIEWS.map(([id, label]) =>
    h('button', { type: 'button', role: 'tab', 'data-view': id, onclick: () => { view = id; refresh(true); } }, label)));
  els.body = h('div', { class: 'journal-body' });
  root.append(h('div', { class: 'page journal' },
    h('header', { class: 'page-head' }, h('h1', null, '日志')),
    els.stage, els.tabs, els.body));
  renderStage();
}

export function activate() { refresh(true); if (!avatar) start(); }

function renderStage() {
  if (!els.stage) return;
  els.stageFigure = els.stageFigure || figure(132);
  const making = avatar && (avatar.status === 'drafting' || avatar.status === 'making');
  const said = recent.slice(-3);
  fill(els.stage,
    h('div', { class: 'stage-figure' }, els.stageFigure),
    h('div', { class: 'stage-side' },
      h('div', { class: 'row spread' },
        h('div', { class: 'stage-name' }, h('strong', null, name()),
          making ? h('span', { class: 'badge' }, (avatar.progress && avatar.progress.phase) || '正在画') : avatar && avatar.status === 'choosing' ? h('span', { class: 'badge ok' }, '画好了，等你选') : null),
        h('div', { class: 'row' },
          action('和它聊聊', () => talk(), { class: 'btn primary small' }),
          action(avatar && avatar.base ? '换个形象' : '捏个形象', openEditor, { class: 'btn small' }))),
      h('div', { class: 'stage-said' }, said.length
        ? said.map((note) => bubble(note))
        : h('span', { class: 'muted small' }, `${name()}干活时自言自语的话会出现在这里。它觉得哪里不对劲，也会说。`)),
      h('div', { class: 'row wrap' }, ASKS.map((text) => action(text, () => talk(text), { class: 'chip' })))));
}

async function talk(text) {
  try {
    const session = await post('/api/journal/talk');
    emit('talk', { id: session.id, text: text || '' });
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) notify(err.message);
  }
}

/** Reloads the open view; called when the tab opens, the view or day changes, and the engine writes a line. */
export function refresh(force = false) {
  if (store.tab !== 'journal') return null;
  if (loading && !force) return loading;
  for (const button of els.tabs.children) button.setAttribute('aria-selected', String(button.dataset.view === view));
  const mine = loading = (view === 'work' ? get('/api/journal' + (day ? '?day=' + day : '')).then(renderWork)
    : view === 'mind' ? get('/api/journal/mind?limit=100').then(renderMind)
      : get('/api/journal/improvements').then(renderImprove))
    .catch(() => {}).finally(() => { if (loading === mine) loading = null; });
  return mine;
}

function empty(title, hint) {
  return h('div', { class: 'empty short' }, h('p', { class: 'empty-title' }, title), h('p', { class: 'muted' }, hint));
}

function mark(row) {
  if (row.stream === 'mind') return h('span', { class: 'dot-mark mind', title: KINDS[row.kind] || '' });
  const cls = row.kind === 'harness' ? (row.owner ? 'warn' : 'quiet') : row.status === 'error' ? 'bad' : row.status === 'declined' ? 'quiet' : row.effect && row.effect !== 'read' ? 'did' : 'quiet';
  return h('span', { class: 'dot-mark ' + cls });
}

function line(row) {
  const tag = row.stream === 'mind' ? KINDS[row.kind] : row.kind === 'harness' ? (row.owner ? '被纠正' : '') : row.status === 'error' ? '没成' : row.status === 'declined' ? '你没同意' : '';
  return h('li', { class: 'log-line ' + row.stream + (row.kind === 'harness' ? ' harness' : '') },
    h('time', null, clock(row.at)), mark(row),
    h('span', { class: 'log-text' }, row.stream === 'mind' ? '“' + row.text + '”' : row.text),
    tag ? h('span', { class: 'badge' + (row.status === 'error' || (row.kind === 'harness' && row.owner) ? ' flag' : '') }, tag) : null);
}

function renderWork(page) {
  const days = page.days.includes(today()) ? page.days : [today(), ...page.days];
  const at = days.indexOf(page.day);
  const step = (offset) => { const next = days[at + offset]; if (next) { day = next === today() ? null : next; refresh(true); } };
  const t = page.totals;
  const head = h('div', { class: 'row spread' },
    h('div', { class: 'row' },
      h('button', { class: 'icon-btn small', type: 'button', 'aria-label': '前一天', disabled: at < 0 || at >= days.length - 1, onclick: () => step(1) }, icon('left', 16)),
      h('strong', null, page.day === today() ? '今天' : page.day),
      h('button', { class: 'icon-btn small', type: 'button', 'aria-label': '后一天', disabled: at <= 0, onclick: () => step(-1) }, icon('right', 16))),
    h('div', { class: 'row wrap' },
      h('span', { class: 'chip static' }, `${t.tasks} 件事`),
      h('span', { class: 'chip static' }, `${t.acted} 次改动或对外`),
      t.failed ? h('span', { class: 'chip static flag' }, `${t.failed} 次没成`) : null,
      t.corrected ? h('span', { class: 'chip static flag' }, `被纠正 ${t.corrected} 次`) : null));
  const cards = page.tasks.slice().reverse().map((task) => {
    const rows = [...task.work, ...task.mind].sort((a, b) => a.id - b.id);
    const calls = task.work.filter((r) => r.kind === 'tool');
    const facts = [`${calls.length} 步`, task.acted ? `${task.acted} 次改动或对外` : '', task.failed ? `${task.failed} 次没成` : '', task.corrected ? `被纠正 ${task.corrected} 次` : ''].filter(Boolean).join(' · ');
    const span = clock(task.started_at) + (task.ended_at - task.started_at > 60 ? ' – ' + clock(task.ended_at) : '');
    const headRow = h('span', { class: 'task-head' },
      h('span', { class: 'task-title' }, task.title || ORIGINS[task.origin] || '一件事'),
      h('span', { class: 'task-sub' }, h('span', null, span), h('span', { class: 'badge' }, ORIGINS[task.origin] || task.origin), h('span', null, facts)));
    const body = h('div', null, h('ul', { class: 'log' }, rows.map(line)),
      task.session_id ? h('div', { class: 'row end' }, action('看这段对话', () => emit('open-stretch', task.session_id), { class: 'btn small' })) : null);
    const card = ui.disclosure('card task' + (task.failed || task.corrected ? ' flagged' : ''), headRow, body, opened.has(task.key));
    card.head.addEventListener('click', () => { if (card.open) opened.add(task.key); else opened.delete(task.key); });
    return card;
  });
  fill(els.body, head, cards.length ? h('div', { class: 'cards single' }, cards)
    : empty('这一天还没有记录', 'Jurvis 每动用一次工具，这里就多一行。这是引擎自己记的，模型改不了。'));
}

function renderMind(page) {
  const notes = page.notes.slice().reverse();
  fill(els.body, notes.length ? h('ul', { class: 'log mind-list' }, notes.map((note) =>
    h('li', { class: 'log-line mind' }, h('time', null, dated(note.at)), mark(note),
      h('span', { class: 'log-text' }, '“' + note.text + '”'),
      h('span', { class: 'badge' + (note.owner ? ' flag' : '') }, KINDS[note.kind] || note.kind))))
    : empty('它还没说过什么', `${name()}在动手前说的话、做事时记下的疑虑，都会留在这里。`));
}

function renderImprove(groups) {
  const total = OWNERS.reduce((n, [id]) => n + (groups[id] || []).length, 0);
  if (!total) { fill(els.body, empty('最近 30 天没什么要改的', '工具出错、被引擎纠正、或者它自己觉得不合理的地方，会按“该谁改”归在这里。')); return; }
  fill(els.body, OWNERS.filter(([id]) => (groups[id] || []).length).map(([id, title, hint]) =>
    h('section', { class: 'improve' },
      h('h2', { class: 'group-title' }, title, h('span', { class: 'muted' }, ' · ' + hint)),
      h('div', { class: 'cards single' }, groups[id].map((item) => h('article', { class: 'card improve-item' },
        h('div', { class: 'row spread' },
          h('span', { class: 'log-text' }, item.stream === 'mind' ? '“' + item.text + '”' : item.text),
          h('span', { class: 'row' }, item.count > 1 ? h('span', { class: 'badge flag' }, item.count + ' 次') : null,
            h('span', { class: 'muted small' }, dated(item.last_at))))))))),
  h('p', { class: 'muted small' }, `想知道细节，直接问${name()}：它会指着这些记录告诉你。`));
}

// ---------------------------------------------------------------- making a character
function openEditor() {
  const nameInput = h('input', { class: 'input', maxLength: 20, value: name(), 'aria-label': '名字' });
  const describe = h('textarea', { class: 'input', rows: 3, maxLength: 300, placeholder: '比如：一只戴红围巾的橘猫 / 短发戴圆眼镜、穿黄色开衫的女生 / 一只白色的萨摩耶', 'aria-label': '它长什么样' });
  describe.value = (avatar && avatar.description) || '';
  const file = h('input', { type: 'file', accept: 'image/*', hidden: true });
  let photo = null;
  const photoNote = h('span', { class: 'muted small' });
  const status = h('div', { class: 'maker-status' });
  const grid = h('div', { class: 'maker-grid' });
  file.addEventListener('change', async () => {
    const picked = file.files[0];
    file.value = '';
    if (!picked) return;
    const form = new FormData();
    form.append('file', picked);
    try {
      photo = (await api('POST', '/api/assets', form)).id;
      photoNote.textContent = '照着这张画：' + picked.name;
    } catch (err) { notify(err.message); }
  });
  const draw = action('画几张看看', async () => {
    avatar = await post('/api/avatar/draft', { description: describe.value.trim(), photo });
    render();
    renderStage();
  }, { class: 'btn primary' });
  const render = () => {
    const state = avatar || {};
    const busy = state.status === 'drafting' || state.status === 'making';
    draw.disabled = busy;
    fill(status,
      state.status === 'drafting' ? h('p', null, (state.progress && state.progress.phase) || '排队中，马上开始画', h('span', { class: 'muted' }, ' · 每张大约一分半钟'))
        : state.status === 'choosing' ? h('p', null, '画好了，点一张你喜欢的。')
          : state.status === 'making' ? h('p', null, `已经换上了。${(state.progress && state.progress.phase) || '正在画表情'}`, h('span', { class: 'muted' }, ' · 五个表情大约十几分钟，可以先去做别的'))
            : state.status === 'ready' && state.base ? h('p', { class: 'muted' }, `现在的形象有 ${Object.keys(state.faces || {}).length} 个表情。想换就再描述一次。`)
              : null,
      state.error ? h('p', { class: 'maker-error' }, state.error) : null);
    fill(grid, (state.candidates || []).map((id) => {
      const img = h('img', { alt: '候选形象', draggable: false });
      assetURL(id).then((url) => { img.src = url; }, () => {});
      return h('button', { class: 'maker-pick', type: 'button', disabled: state.status !== 'choosing', onclick: async () => {
        try { avatar = await post('/api/avatar/choose', { asset_id: id }); render(); onAvatar(); } catch (err) { notify(err.message); }
      } }, img);
    }));
  };
  const body = h('div', { class: 'maker stack' },
    h('label', { class: 'field' }, h('span', null, '名字'),
      h('div', { class: 'row' }, nameInput, action('保存', async () => { avatar = await put('/api/avatar', { name: nameInput.value.trim() || '小维' }); renderStage(); }, { class: 'btn' }))),
    h('label', { class: 'field' }, h('span', null, '它长什么样'), describe),
    h('div', { class: 'row wrap' }, draw, h('button', { class: 'btn', type: 'button', onclick: () => file.click() }, '照着一张照片画'), file, photoNote),
    h('p', { class: 'muted small' }, '形象由这台 Mac 上的画图模型画，不会传到别处。画的时候本机模型要让出显卡，用本机模型的对话会等它画完。'),
    status, grid,
    avatar && avatar.base ? h('div', { class: 'row end' }, action('用回默认形象', async () => {
      if (!await ui.ask('用回默认的小圆脸？现在的形象会被删掉。', { ok: '用回默认', danger: true })) return;
      avatar = await del('/api/avatar');
      render();
      onAvatar();
    }, { class: 'btn small' })) : null);
  const close = sheet('捏一个形象', body);
  editor = { render, close };
  render();
  const watch = setInterval(() => { if (!body.isConnected) { clearInterval(watch); editor = null; } }, 1000);
}

on('state', onState);
