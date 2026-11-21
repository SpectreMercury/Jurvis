// 对话: the conversation list and the chat view. Message DOM is updated in place, keyed by message id and step/call index,
// so the composer never re-renders and scrolling/focus survive streaming.
import {
  store, h, fill, api, get, post, patch, del, run, action, notify, emit, throttle, refreshState, jobsOf, isLive, jobById,
  assetURL, releaseAssets, pickModel, modelLabel, dayGroup, shortTime, uuid, sheet, sleep, ApiError,
} from './core.js';
import { renderMarkdown } from './md.js';
import { icon, toolIcon } from './icons.js';
import { disclosure, ask } from './ui.js';
import * as voice from './voice.js';
import { bars } from './ledger.js';

const MODES = [
  ['goal', '目标', 'target', '目标：先列计划，自己一步步做完再汇报；写到一半被截断会自动续写。'],
  ['plan', '计划', 'tasks', '计划：只看不动手，先给出一份计划；你确认或修改后再开始做。'],
  ['image', '图片', 'image', '图片：描述想要的画面，在这台 Mac 上生成。'],
  ['video', '视频', 'video', '视频：描述镜头内容，生成需要几分钟。'],
  ['drama', '短剧', 'film', '短剧：先定角色和分镜，再逐个镜头生成。'],
];
const MODE_NAMES = Object.fromEntries(MODES.map(([id, label]) => [id, label]));
const MODE_HINTS = Object.fromEntries(MODES.map(([id, , , hint]) => [id, hint]));
const MAX_IMAGES = 4;
const coarse = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;

const draft = { model: '', incognito: false }; // choices for what is said next, while no stretch of the conversation is open
// The one conversation: what is loaded starts at `from` (a message time) and follows the present, unless an old part is open (`past`).
const stream = { from: null, earlier: false, past: false, fresh: false, target: null };
const STREAM_LIMIT = 400;
let mode = null;
let speech = { stt: false, tts: false };   // what the Mac can do with voice
let dictation = null;   // the recording in progress for the composer
let transcribing = false;
let attachments = [];   // {file, url, asset, ready}
let sending = false;
let lastRequest = null; // {key, id}: the same message retried after a network failure keeps its request id
let stick = true;       // follow new content only while the user is near the bottom
let listSig = '';
let loadSeq = 0;
const nodes = new Map();     // message id -> {el, sig, parts}
const extraJobs = new Map(); // jobs older than the 200 in /api/state, fetched once
const els = {};
let open = true;         // phone: the conversation is on screen (otherwise the list of its earlier stretches)
let waitingId = null;

// ---------------------------------------------------------------- mount
export function mount(root) {
  els.list = h('div', { class: 'session-list' });
  // Finding something said before: by the words, across the whole conversation.
  els.search = h('input', { class: 'side-search', type: 'search', placeholder: '搜索说过的话', 'aria-label': '搜索说过的话', enterKeyHint: 'search' });
  els.hits = h('div', { class: 'session-list', hidden: true });
  let typing = 0;
  els.search.addEventListener('input', () => { clearTimeout(typing); typing = setTimeout(findSaid, 300); });
  els.side = h('div', { class: 'side-stream' }, els.search, els.hits, els.list);
  els.waiting = h('button', { class: 'waiting-card', type: 'button', hidden: true, onclick: () => { if (waitingId) openSession(waitingId); } });

  els.title = h('div', { class: 'conv-title' }, 'Jurvis');
  els.state = h('div', { class: 'conv-state' });
  els.modelBtn = h('button', { class: 'chip', type: 'button', onclick: chooseModel, title: '对话使用的模型' });
  els.incognito = action(icon('eyeoff', 18), toggleIncognito, { class: 'icon-btn', 'aria-pressed': 'false', 'aria-label': '无痕', title: '无痕：这段对话不读取也不写入记忆' });
  els.earlier = action('更早的对话', loadEarlier, { class: 'btn small earlier', hidden: true });
  els.msgs = h('div', { class: 'msgs' }, els.earlier);
  els.note = h('div', { class: 'stream-note', hidden: true });
  els.tray = h('div', { class: 'tray', hidden: true, role: 'group', 'aria-label': '等你决定' });
  els.empty = h('div', { class: 'empty' }, h('p', { class: 'empty-title' }, '今天交给 Jurvis 什么？'),
    h('p', { class: 'muted' }, '说清楚想要的结果，它会自己列计划、分步做完，需要你时再来找你。'));
  els.scroller = h('div', { class: 'scroller' }, els.empty, els.msgs);
  els.scroller.addEventListener('scroll', () => {
    stick = els.scroller.scrollHeight - els.scroller.scrollTop - els.scroller.clientHeight < 80;
  }, { passive: true });

  els.thumbs = h('div', { class: 'attachments', hidden: true });
  els.modes = h('div', { class: 'modes', role: 'group', 'aria-label': '任务类型' }, MODES.map(([id, label, glyph]) => {
    const chip = h('button', { class: 'chip toggle', type: 'button', 'aria-pressed': 'false', dataset: { mode: id } }, icon(glyph, 15), label);
    chip.addEventListener('click', () => { mode = mode === id ? null : id; renderModes(); });
    return chip;
  }));
  els.modeHint = h('div', { class: 'mode-hint', hidden: true });
  els.file = h('input', { type: 'file', accept: 'image/*', multiple: true, hidden: true, id: 'attach-input' });
  els.file.addEventListener('change', () => { addFiles(els.file.files); els.file.value = ''; });
  els.attach = h('button', { class: 'round-btn attach', type: 'button', 'aria-label': '添加图片', title: '添加图片（最多 4 张）', onclick: () => els.file.click() }, icon('clip'));
  els.input = h('textarea', { class: 'composer-input', rows: 1, placeholder: '交给 Jurvis 一个任务，或直接提问', 'aria-label': '消息', enterKeyHint: coarse ? 'enter' : 'send', maxLength: 16000 });
  els.input.addEventListener('input', () => { autogrow(); updateSend(); });
  els.input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229 && !coarse) {
      event.preventDefault();
      primary();
    }
  });
  els.input.addEventListener('paste', (event) => {
    const files = [...((event.clipboardData && event.clipboardData.files) || [])].filter((f) => f.type.startsWith('image/'));
    if (files.length) { event.preventDefault(); addFiles(files); }
  });
  els.mic = h('button', { class: 'round-btn mic', type: 'button', hidden: true, 'aria-pressed': 'false', 'aria-label': '语音输入', title: '语音输入：说完自动转成文字', onclick: dictate }, icon('mic', 16));
  els.talk = h('button', { class: 'round-btn talk', type: 'button', hidden: true, 'aria-label': '语音对话', title: '语音对话：你说，Jurvis 读出回答', onclick: talk }, icon('voice', 16));
  els.send = h('button', { class: 'round-btn solid send', type: 'button', 'aria-label': '发送', onclick: primary }, icon('send', 16, 1.6));
  els.spacer = h('span', { class: 'spacer' });
  els.toolRow = h('div', { class: 'composer-row' }, els.attach, els.file, els.modelBtn, els.modes, els.spacer, els.mic, els.talk, els.send);
  els.modeRow = h('div', { class: 'mode-row' });
  els.composer = h('div', { class: 'composer' }, els.thumbs, els.input, els.toolRow);
  els.composer.addEventListener('dragover', (event) => { event.preventDefault(); });
  els.composer.addEventListener('drop', (event) => {
    event.preventDefault();
    addFiles((event.dataTransfer && event.dataTransfer.files) || []);
  });

  els.conv = h('section', { class: 'conv' },
    h('header', { class: 'conv-head' },
      h('button', { class: 'icon-btn back', type: 'button', 'aria-label': '对话记录', onclick: showList }, icon('left', 20, 1.6)),
      h('div', { class: 'conv-heading' }, els.title, els.state),
      h('div', { class: 'conv-actions' }, els.incognito)),
    els.scroller,
    h('div', { class: 'composer-wrap' }, els.tray, els.note, els.composer, els.modeRow, els.modeHint));

  els.screen = h('div', { class: 'chat-screen' }, els.conv);
  root.append(els.screen);
  renderHeader();
  updateSend();
  voice.onSpeaking(() => renderMessages());
  syncVoice();
}

/** Shows the voice controls the Mac and this browser can support. */
export async function syncVoice(force = false) {
  const can = await voice.status(force);
  speech = can;
  els.mic.hidden = !(can.stt && voice.canRecord());
  els.talk.hidden = !(can.stt && can.tts && voice.canRecord());
  renderMessages();
}

// ---------------------------------------------------------------- voice
async function dictate() {
  if (dictation) { dictation.stop(); return; }
  if (transcribing) return;
  try {
    dictation = await voice.listen({ onLevel: (level) => { els.mic.style.setProperty('--level', level.toFixed(2)); } });
  } catch (err) { notify(err.message); return; }
  els.mic.setAttribute('aria-pressed', 'true');
  els.mic.setAttribute('aria-label', '停止录音');
  const blob = await dictation.result;
  dictation = null;
  els.mic.setAttribute('aria-pressed', 'false');
  els.mic.setAttribute('aria-label', '语音输入');
  if (!blob) return;
  transcribing = true;
  els.mic.classList.add('busy');
  try {
    const text = await voice.transcribe(blob);
    if (text) {
      const before = els.input.value;
      els.input.value = (before && !/\s$/.test(before) ? before + ' ' : before) + text;
      autogrow();
      updateSend();
      els.input.focus();
    } else notify('没有听清，再说一次', 'info');
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) notify(err.message);
  } finally {
    transcribing = false;
    els.mic.classList.remove('busy');
  }
}

function talk() {
  if (dictation) dictation.cancel();
  voice.converse({ ask: askAloud });
}

/** Sends what was said and resolves to the reply's text once Jurvis has finished. */
async function askAloud(text) {
  while (sending || liveJob()) await sleep(300);
  els.input.value = text;
  const sid = await send({ spoken: true });
  if (!sid) throw new Error('没有发出去');
  for (;;) {
    await sleep(400);
    const live = jobsOf(sid).find(isLive);
    if (live && String(live.phase || '').startsWith('等待你确认')) return '有一步需要你确认，请结束语音对话，在屏幕上处理。';
    if (!live && !sending) break;
  }
  await loadMessages(false);
  const last = [...store.messages].reverse().find((m) => m.role === 'assistant');
  if (!last) return '';
  if (last.status === 'failed') throw new Error((last.metadata && last.metadata.error) || '回复失败');
  return stepsOf(last).map((step) => step.text || '').filter(Boolean).pop() || '';
}

/** Sends one of the offered options as the user's reply in this conversation. */
function answer(option) {
  if (sending || liveJob()) return;
  els.input.value = option;
  autogrow();
  send();
}

/** Says this as the start of something new, as if it had been typed and sent. */
export function startTask(text) {
  newConversation(false);
  els.input.value = String(text || '').slice(0, 16000);
  autogrow();
  updateSend();
  send();
}

/** The parts of this module that live in the app's sidebar. */
export function sideParts() { return { list: els.side, waiting: els.waiting }; }

let findSeq = 0;
async function findSaid() {
  const query = els.search.value.trim();
  const seq = ++findSeq;
  if (query.length < 2) { els.hits.hidden = true; els.list.hidden = false; return; }
  let found;
  try { found = (await get('/api/stream/search?q=' + encodeURIComponent(query))).results; } catch { return; }
  if (seq !== findSeq) return;
  els.list.hidden = true;
  els.hits.hidden = false;
  fill(els.hits, found.length ? null : h('p', { class: 'muted small pad' }, '没有说到过。'), ...found.map((hit) =>
    h('div', { class: 'session-row' }, h('button', { class: 'session-open hit', type: 'button', onclick: () => openSession(hit.session_id, hit.id) },
      h('span', { class: 'session-text' }, h('span', { class: 'session-title' }, hit.snippet),
        h('span', { class: 'session-sub' }, stretchTime(hit.created_at) + ' · ' + (hit.title || '对话')))))));
}
export function where() { return { open, blank: stream.fresh }; }

export function showList() { open = false; emit('conv'); }
function showConversation() { open = true; emit('conv'); }

/** Called when signing out or pairing again. */
export function reset() {
  store.sessionId = null;
  store.messages = [];
  Object.assign(stream, { from: null, earlier: false, past: false, fresh: false, target: null });
  draft.model = '';
  draft.incognito = false;
  mode = null;
  clearAttachments();
  if (els.input) { els.input.value = ''; autogrow(); }
  listSig = '';
  if (els.msgs) { renderModes(); renderMessages(); renderHeader(); renderList(); }
}

export function onState() {
  if (stream.from == null && !stream.past) refreshMessages();  // the first state after signing in: the conversation is loaded with it
  renderTray();
  renderList();
  renderHeader();
  renderMessages();
}

// ---------------------------------------------------------------- conversation list
function sessionOf(id) {
  return ((store.state && store.state.sessions) || []).find((s) => s.id === id) || null;
}

function renderList() {
  const sessions = [...((store.state && store.state.sessions) || [])].sort((a, b) => b.updated_at - a.updated_at);
  const badges = new Map();
  for (const job of (store.state && store.state.jobs) || []) {
    if (job.kind !== 'chat' || !isLive(job) || !job.payload) continue;
    const waiting = String(job.phase || '').startsWith('等待你确认');
    if (waiting || !badges.has(job.payload.session_id)) badges.set(job.payload.session_id, { kind: waiting ? 'wait' : 'run', phase: job.phase || (job.status === 'queued' ? '排队中' : '进行中') });
  }
  const sig = JSON.stringify([store.sessionId, sessions.map((s) => [s.id, s.title, s.updated_at, s.incognito, badges.get(s.id) || '', dayGroup(s.updated_at)])]);
  if (sig === listSig) return;
  listSig = sig;

  const waiting = sessions.find((s) => (badges.get(s.id) || {}).kind === 'wait');
  waitingId = waiting ? waiting.id : null;
  els.waiting.hidden = !waiting;
  if (waiting) {
    fill(els.waiting, h('span', { class: 'live' }, '等你确认'), h('span', { class: 'waiting-title' }, waiting.title || '对话'),
      h('span', { class: 'muted small' }, 'Jurvis 停在这一步，等你允许或拒绝。'), h('span', { class: 'go' }, '去确认', icon('right', 14)));
  }

  fill(els.list);
  if (!sessions.length) {
    els.list.append(h('p', { class: 'muted small pad' }, '还没有对话。'));
    return;
  }
  const row = (s) => {
    const badge = badges.get(s.id);
    const live = !!badge;
    const sub = badge ? (badge.kind === 'wait' ? '等你确认' : badge.phase) : s.incognito ? '无痕' : '';
    const button = h('button', { class: 'session-open', type: 'button', onclick: () => openSession(s.id) },
      h('span', { class: 'session-mark' + (live ? ' live' : '') }, icon(badge ? (badge.kind === 'wait' ? 'alert' : 'clock') : 'check')),
      h('span', { class: 'session-text' }, h('span', { class: 'session-title' }, s.title || '对话'), sub ? h('span', { class: 'session-sub' }, sub) : null),
      h('span', { class: 'session-tag' + (badge && badge.kind === 'wait' ? ' green' : '') }, badge ? (badge.kind === 'wait' ? '等你' : '') : shortTime(s.updated_at)));
    const remove = action(icon('trash', 15), async () => {
      if (!await ask(`删除“${s.title || '对话'}”？这段对话的消息会一并删除。`, { ok: '删除', danger: true })) return;
      await del('/api/sessions/' + encodeURIComponent(s.id));
      if (store.state) store.state.sessions = store.state.sessions.filter((x) => x.id !== s.id);
      if (store.sessionId === s.id) store.sessionId = null;
      listSig = '';
      renderList();
      backToNow();
      refreshState();
    }, { class: 'icon-btn small session-remove', 'aria-label': '删除这段对话', title: '删除这段对话' });
    return h('div', { class: 'session-row' + (s.id === store.sessionId ? ' current' : '') }, button, remove);
  };
  const running = sessions.filter((s) => badges.has(s.id));
  if (running.length) els.list.append(h('div', { class: 'group-title' }, '进行中'), ...running.map(row));
  let group = '';
  for (const s of sessions) {
    if (badges.has(s.id)) continue;
    const name = dayGroup(s.updated_at);
    if (name !== group) { group = name; els.list.append(h('div', { class: 'group-title' }, name)); }
    els.list.append(row(s));
  }
}

function clearMessages() {
  loadSeq += 1;
  store.messages = [];
  for (const node of nodes.values()) node.el.remove();
  nodes.clear();
  stick = true;
}

/** The conversation goes on by itself; this only says that what comes next is about something else. */
export function newConversation(focus) {
  stream.fresh = true;
  if (stream.past) backToNow();
  renderNote();
  showConversation();
  if (focus && !coarse) els.input.focus();
}

/** Leaves the old part that was open and follows the present again. */
function backToNow() {
  stream.past = false;
  stream.from = null;
  clearMessages();
  renderNote();
  return loadMessages(true);
}

/** Opens the conversation at an earlier stretch, from another screen. */
export function openStretch(id) { return openSession(id); }

/** Opens the conversation at an earlier stretch, or at one message of it. */
async function openSession(id, messageId = null) {
  showConversation();
  let page;
  try {
    page = await get('/api/stream?' + (messageId ? 'at=' + encodeURIComponent(messageId) : 'start=' + encodeURIComponent(id)) + '&limit=' + STREAM_LIMIT);
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) notify(err.message);
    return;
  }
  clearMessages();
  stream.past = page.later;
  stream.from = null;
  const first = page.messages.find((m) => m.session_id === id) || page.messages[0];
  stream.target = messageId || (first && first.id) || null;
  stick = false;
  applyPage(page);
}

async function loadEarlier() {
  const first = store.messages[0];
  if (!first || !stream.earlier) return;
  const page = await get('/api/stream?before=' + first.created_at.toFixed(6));
  const height = els.scroller.scrollHeight;
  stream.from = (page.messages.length ? page.messages[0].created_at : first.created_at) - 0.00001;
  stream.earlier = page.earlier;
  store.messages = [...page.messages, ...store.messages];
  stick = false;
  renderMessages();
  els.scroller.scrollTop += els.scroller.scrollHeight - height;  // what was on screen stays where it was
}

function applyPage(page) {
  if (stream.from == null) stream.from = (page.messages.length ? page.messages[0].created_at : Date.now() / 1000) - 0.00001;
  stream.earlier = page.earlier;
  const moved = store.sessionId !== page.current;
  store.sessionId = page.current;
  store.messages = page.messages;
  renderMessages();
  renderNote();
  if (moved) { renderHeader(); listSig = ''; renderList(); }
}

// What Jurvis asked and the user has not decided yet stays above the composer, wherever they are in the conversation.
// The question the conversation ends on is left out: its options are right there under it.
let trayAt = 0;
let traySig = '';
function renderTray() {
  const last = store.messages.length ? store.messages[store.messages.length - 1].id : null;
  const items = ((store.state && store.state.questions) || []).filter((q) => q.message_id !== last);
  els.tray.hidden = !items.length;
  if (!items.length) { traySig = ''; return; }
  trayAt = Math.min(trayAt, items.length - 1);
  const sig = JSON.stringify([items, trayAt]);
  if (sig === traySig) return;
  traySig = sig;
  const q = items[trayAt];
  fill(els.tray,
    h('div', { class: 'tray-head' },
      h('span', { class: 'tray-label' }, items.length > 1 ? `等你决定 · ${trayAt + 1}/${items.length}` : '等你决定'),
      items.length > 1 ? h('button', { class: 'link', type: 'button', onclick: () => { trayAt = (trayAt + 1) % items.length; renderTray(); } }, '下一个') : null,
      action('×', async () => {
        await del('/api/questions/' + encodeURIComponent(q.id));
        if (store.state) store.state.questions = store.state.questions.filter((x) => x.id !== q.id);
        renderTray();
        refreshState();
      }, { class: 'icon-btn small', 'aria-label': '不用决定了', title: '不用决定了' })),
    h('button', { class: 'tray-question', type: 'button', title: '看看当时说到哪了', onclick: () => openSession(q.session_id, q.message_id) }, q.question),
    h('div', { class: 'tray-options' }, q.options.map((option) => h('button', { class: 'option', type: 'button', onclick: () => {
      if (sending || liveJob()) return;
      els.input.value = option;
      autogrow();
      send({ answerTo: q.id });
    } }, option))));
}

/** One line above the composer: an old part of the conversation is open, or the next message begins a new subject. */
function renderNote() {
  els.note.hidden = !(stream.past || stream.fresh);
  if (stream.past) fill(els.note, h('span', null, '这是较早的对话'), h('button', { class: 'link', type: 'button', onclick: backToNow }, '回到现在'));
  else if (stream.fresh) fill(els.note, h('span', null, '下一条消息会开始一个新话题'), h('button', { class: 'link', type: 'button', onclick: () => { stream.fresh = false; renderNote(); } }, '取消'));
  emit('conv');
}

// ---------------------------------------------------------------- header: title, model, incognito
function renderHeader() {
  const s = store.sessionId ? sessionOf(store.sessionId) : null;
  els.title.textContent = stream.past ? '较早的对话' : (s && s.title) || 'Jurvis';
  const model = store.sessionId ? ((s && s.model) || '') : draft.model;
  fill(els.modelBtn, icon('chip', 14), modelLabel(model), icon('down', 12));
  const incognito = store.sessionId ? !!(s && s.incognito) : draft.incognito;
  els.incognito.setAttribute('aria-pressed', String(incognito));
  const live = liveJob();
  els.state.className = 'conv-state' + (live ? ' live' : '');
  els.state.textContent = live ? (live.phase || (live.status === 'queued' ? '排队中' : '进行中')) : incognito ? '无痕 · 不读取也不写入记忆' : '';
  els.state.hidden = !els.state.textContent;
}

function replaceSession(session) {
  if (!store.state || !session || !session.id) return;
  const index = store.state.sessions.findIndex((s) => s.id === session.id);
  if (index >= 0) store.state.sessions[index] = session;
  else store.state.sessions.unshift(session);
}

function chooseModel() {
  const sid = store.sessionId;
  const s = sid ? sessionOf(sid) : null;
  pickModel({
    title: '对话的模型',
    current: sid ? ((s && s.model) || '') : draft.model,
    defaultLabel: '默认' + (store.state && store.state.chat_model ? `（${modelLabel(store.state.chat_model)}）` : ''),
    onPick: async (id) => {
      if (sid) replaceSession(await patch('/api/sessions/' + encodeURIComponent(sid), { model: id }));
      else draft.model = id;
      renderHeader();
    },
  });
}

async function toggleIncognito() {
  const sid = store.sessionId;
  if (!sid) draft.incognito = !draft.incognito;
  else {
    const s = sessionOf(sid);
    replaceSession(await patch('/api/sessions/' + encodeURIComponent(sid), { incognito: !(s && s.incognito) }));
    listSig = '';
    renderList();
  }
  renderHeader();
}

// ---------------------------------------------------------------- composer
function autogrow() {
  els.input.style.height = 'auto';
  els.input.style.height = Math.min(els.input.scrollHeight, 180) + 'px';
}

function renderModes() {
  for (const chip of els.modes.children) chip.setAttribute('aria-pressed', String(chip.dataset.mode === mode));
  els.modeHint.hidden = !mode;
  els.modeHint.textContent = mode ? MODE_HINTS[mode] : '';
}

/** The reply the conversation is waiting for. Background work does not count: you can keep talking while it runs. */
function liveJob() {
  return ((store.state && store.state.jobs) || []).find((job) => job.kind === 'chat' && isLive(job) && !(job.payload && job.payload.background)) || null;
}

function updateSend() {
  if (!els.send || els.send.dataset.pending) return;
  const live = liveJob();
  const label = live ? '停止' : sending ? '发送中' : '发送';
  if (els.send.getAttribute('aria-label') !== label || !els.send.firstChild) {
    fill(els.send, icon(live ? 'stop' : 'send', 16, 1.6));
    els.send.setAttribute('aria-label', label);
    els.send.title = label;
  }
  els.send.disabled = live ? false : sending || !els.input.value.trim();
}

function primary() {
  const live = liveJob();
  if (live) {
    run(els.send, async () => {
      await post('/api/jobs/' + encodeURIComponent(live.id) + '/cancel');
      refreshState();
      refreshMessages();
    }).then(updateSend);
  } else send();
}

async function send({ spoken = false, answerTo = null } = {}) {
  const text = els.input.value.trim();
  if (!text || sending || liveJob()) return null;
  sending = true;
  updateSend();
  try {
    await Promise.all(attachments.map((a) => a.ready));
    const assetIds = attachments.filter((a) => a.asset).map((a) => a.asset.id);
    if (stream.past) await backToNow();  // what is said now belongs at the end, not in the old part that was open
    const key = JSON.stringify([text, assetIds, mode]);
    if (!lastRequest || lastRequest.key !== key) lastRequest = { key, id: uuid() };
    const body = { content: text, asset_ids: assetIds, request_id: lastRequest.id, mode, voice: spoken };
    if (answerTo) body.answer_to = answerTo;  // picked from what is waiting: it goes back to where it was asked
    else if (stream.fresh) body.fresh = true;
    if (!store.sessionId || stream.fresh) {  // no stretch is open: the choices made meanwhile go with the message
      if (draft.incognito) body.incognito = true;
      if (draft.model) body.model = draft.model;
    }
    // The engine decides which stretch of the conversation this continues, or begins a new one.
    const job = await post('/api/stream/messages', body);
    const sid = job.payload.session_id;
    lastRequest = null;
    stream.fresh = false;
    draft.model = '';
    draft.incognito = false;
    store.sessionId = sid;
    renderNote();
    renderHeader();
    if (store.state && job && job.id && !store.state.jobs.some((j) => j.id === job.id)) store.state.jobs.unshift(job);
    els.input.value = '';
    autogrow();
    clearAttachments();
    mode = null;
    renderModes();
    stick = true;
    listSig = '';
    renderList();
    await loadMessages(false);
    refreshState();
    return sid;
  } catch (err) {
    if (!(err instanceof ApiError && err.status === 401)) notify(err instanceof ApiError ? err.message : '发送失败：' + ((err && err.message) || err));
    return null;
  } finally {
    sending = false;
    updateSend();
  }
}

// ---- attachments
function addFiles(files) {
  for (const file of files) {
    if (!file.type || !file.type.startsWith('image/')) { notify('只能添加图片', 'info'); continue; }
    if (attachments.length >= MAX_IMAGES) { notify(`一条消息最多附 ${MAX_IMAGES} 张图片`, 'info'); break; }
    const item = { file, url: URL.createObjectURL(file), asset: null };
    const form = new FormData();
    form.append('file', file, file.name || 'image.png');
    item.ready = api('POST', '/api/assets', form).then((asset) => { item.asset = asset; }).catch((err) => {
      removeAttachment(item);
      if (!(err instanceof ApiError && err.status === 401)) notify('图片上传失败：' + ((err && err.message) || err));
    }).finally(renderAttachments);
    attachments.push(item);
  }
  renderAttachments();
}

function removeAttachment(item) {
  if (!attachments.includes(item)) return;
  attachments = attachments.filter((a) => a !== item);
  URL.revokeObjectURL(item.url);
  renderAttachments();
}

function clearAttachments() {
  for (const item of attachments) URL.revokeObjectURL(item.url);
  attachments = [];
  if (els.thumbs) renderAttachments();
}

function renderAttachments() {
  els.thumbs.hidden = !attachments.length;
  fill(els.thumbs, ...attachments.map((item) => h('div', { class: 'thumb' + (item.asset ? '' : ' uploading') },
    h('img', { src: item.url, alt: item.file.name || '图片' }),
    item.asset ? null : h('span', { class: 'thumb-note' }, '上传中'),
    h('button', { class: 'thumb-remove', type: 'button', 'aria-label': '移除图片', onclick: () => removeAttachment(item) }, '×'))));
  els.attach.disabled = attachments.length >= MAX_IMAGES;
}

// ---------------------------------------------------------------- messages
/** Brings the loaded part of the conversation up to date. While an old part is open it is left as it is. */
async function loadMessages(loud) {
  if (stream.past) return;
  const seq = ++loadSeq;
  let page;
  try {
    page = await get(stream.from == null ? '/api/stream' : '/api/stream?after=' + stream.from.toFixed(6) + '&limit=' + STREAM_LIMIT);
  } catch (err) {
    if (loud && !(err instanceof ApiError && err.status === 401)) notify(err.message);
    return;
  }
  if (seq !== loadSeq || stream.past) return;
  if (stream.from != null && page.later) {  // grown too long to keep whole: start over from the newest
    stream.from = null;
    return loadMessages(loud);
  }
  applyPage(page);
}

// Messages Jurvis wrote on its own (a check-in when body data turned) are announced once each: a system notification when the
// browser allows it, otherwise a note in the page. Nothing is announced for the conversation already open in front of the user.
const DELIVERED = 'jurvis.outreach.delivered';
export function announce() {
  const list = (store.state && store.state.outreach) || [];
  if (!list.length) return;
  let done = [];
  try { done = JSON.parse(localStorage.getItem(DELIVERED) || '[]'); } catch { done = []; }
  const fresh = list.filter((o) => !done.includes(o.id) && Date.now() / 1000 - o.created_at < 6 * 3600).reverse();
  if (!fresh.length) return;
  for (const o of fresh) {
    done.push(o.id);
    if (store.tab === 'chat' && !stream.past && document.visibilityState === 'visible') continue;  // it is on screen as it arrives
    const text = o.content.length > 180 ? o.content.slice(0, 180) + '…' : o.content;
    if ('Notification' in window && Notification.permission === 'granted') {
      try {
        const n = new Notification('Jurvis', { body: text, tag: o.id });
        n.onclick = () => { window.focus(); emit('goto', 'chat'); backToNow(); n.close(); };
        continue;
      } catch { /* some mobile browsers only notify from a service worker: fall back to the page */ }
    }
    notify('Jurvis：' + text, 'info');
  }
  try { localStorage.setItem(DELIVERED, JSON.stringify(done.slice(-200))); } catch { /* private mode */ }
}

export const refreshMessages = throttle(() => loadMessages(false), 250);

function scrollBottom() { els.scroller.scrollTop = els.scroller.scrollHeight; }

function lookupJob(id) {
  const job = jobById(id) || extraJobs.get(id);
  if (job === undefined && store.state) {
    extraJobs.set(id, null);
    get('/api/jobs/' + encodeURIComponent(id)).then((found) => { extraJobs.set(id, found); renderMessages(); }).catch(() => {});
  }
  return job || null;
}

function jobSig(job) {
  return job ? [job.id, job.status, job.phase, Math.round((job.progress || 0) * 100), job.error, (job.result && job.result.asset_ids) || []] : null;
}

function stepsOf(m) {
  const steps = m.metadata && Array.isArray(m.metadata.steps) ? m.metadata.steps.filter((s) => s && (s.text || (s.calls && s.calls.length))) : [];
  if (steps.length) return steps;
  return m.content ? [{ text: m.content, calls: [] }] : [];
}

function chatJob(m) {
  return jobsOf(m.session_id).find((j) => j.payload.message_id === m.id) || null;
}

function renderMessages() {
  const list = store.messages;
  // One conversation, in stretches: where a new one begins, the time says how long ago that was.
  const begins = (m, index) => index === 0 || list[index - 1].session_id !== m.session_id;
  const ids = new Set(list.map((m) => m.id));
  list.forEach((m, index) => { if (begins(m, index)) ids.add('mark:' + m.id); });
  for (const [id, node] of nodes) {
    if (!ids.has(id)) { node.el.remove(); nodes.delete(id); }
  }
  els.earlier.hidden = !stream.earlier;
  const used = new Set();
  let previous = els.earlier;
  const place = (el) => {
    if (previous.nextSibling !== el) els.msgs.insertBefore(el, previous.nextSibling);
    previous = el;
  };
  list.forEach((m, index) => {
    const last = index === list.length - 1;
    if (begins(m, index)) {
      let mark = nodes.get('mark:' + m.id);
      if (!mark) {
        mark = { el: h('div', { class: 'stretch-mark', role: 'separator' }, stretchTime(m.created_at)), sig: null, parts: null };
        nodes.set('mark:' + m.id, mark);
      }
      place(mark.el);
    }
    let node = nodes.get(m.id);
    if (!node) {
      node = { el: h('article', { class: 'msg ' + (m.role === 'user' ? 'user' : 'assistant') }), sig: null, parts: null };
      nodes.set(m.id, node);
    }
    place(node.el);
    let sig;
    if (m.role === 'user') {
      for (const id of (m.metadata && m.metadata.asset_ids) || []) used.add(id);
      sig = JSON.stringify(m);
    } else {
      const related = [];
      for (const step of stepsOf(m)) {
        for (const call of step.calls || []) {
          if (!call.job_id) continue;
          const job = lookupJob(call.job_id);
          related.push(jobSig(job));
          if (job && job.status === 'succeeded') for (const id of (job.result && job.result.asset_ids) || []) used.add(id);
        }
        for (const call of step.calls || []) {
          if (call.card && call.card.image) used.add(call.card.image);
        }
      }
      const after = list[index + 1];
      // What the user answered, when this reply ended on a question: the message picked for it later, or the one that follows it.
      const picked = list.find((x) => x.role === 'user' && x.metadata && x.metadata.answer && x.metadata.answer.message_id === m.id);
      node.answer = picked ? picked.content : after && after.role === 'user' && after.session_id === m.session_id ? after.content : null;
      sig = JSON.stringify([m, last, node.answer, jobSig(chatJob(m)), related, speech.tts, voice.speakingKey() === m.id]);
    }
    if (sig !== node.sig) {
      node.sig = sig;
      if (m.role === 'user') renderUser(node.el, m);
      else renderAssistant(node, m, last);
    }
  });
  els.empty.hidden = list.length > 0 || stream.earlier;
  const blank = !els.empty.hidden;
  if (els.conv.classList.contains('blank') !== blank || !els.modes.isConnected) {
    els.conv.classList.toggle('blank', blank);
    // a new task shows the task types under the box; a running one keeps them inside it
    if (blank) els.modeRow.append(els.modes);
    else els.toolRow.insertBefore(els.modes, els.spacer);
    autogrow();
  }
  releaseAssets(used);
  renderTray();
  const target = stream.target && nodes.get(stream.target);
  if (target) {  // an old stretch or a search result was opened: bring it to the top, once
    stream.target = null;
    els.scroller.scrollTop = Math.max(0, target.el.offsetTop - 72);
  } else if (stick) scrollBottom();
  updateSend();
}

const BLOCK = /^(\s*([-*+•]|\d+[.)、])\s|#{1,6}\s|>|\||\s{4,}\S)/;
/** A reply's text as the few messages it would be in a chat: split between plain paragraphs. Lists, tables and quotes stay with
 *  the paragraph that leads into them; long or structured writing (code, a document) stays whole. */
export function bubbles(text) {
  if (!text || text.length > 1500 || /```|~~~/.test(text)) return [text];
  const parts = text.split(/\n\s*\n/).map((part) => part.trim()).filter(Boolean);
  const plain = (part) => !part.split('\n').some((line) => BLOCK.test(line));
  const out = [];
  let before = true;
  for (const part of parts) {
    const now = plain(part);
    if (out.length && !(now && before)) out[out.length - 1] += '\n\n' + part;
    else out.push(part);
    before = now;
  }
  while (out.length > 4) { const tail = out.pop(); out[out.length - 1] += '\n\n' + tail; }
  return out.length ? out : [text];
}

function stretchTime(seconds) {
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  const clock = pad(date.getHours()) + ':' + pad(date.getMinutes());
  const day = dayGroup(seconds);
  if (day === '今天') return clock;
  if (day === '昨天') return '昨天 ' + clock;
  return (date.getFullYear() === new Date().getFullYear() ? '' : date.getFullYear() + '年') + (date.getMonth() + 1) + '月' + date.getDate() + '日 ' + clock;
}

function media(id) {
  const asset = ((store.state && store.state.assets) || []).find((a) => a.id === id);
  const video = !!(asset && String(asset.mime || '').startsWith('video'));
  const box = h('div', { class: 'media' + (video ? ' video' : '') }, h('span', { class: 'muted small' }, '正在载入…'));
  assetURL(id).then((url) => {
    let el;
    if (video) el = h('video', { src: url, controls: true, playsInline: true, preload: 'metadata' });
    else {
      el = h('img', { src: url, alt: (asset && asset.name) || '图片' });
      el.addEventListener('click', () => sheet((asset && asset.name) || '图片', h('img', { class: 'lightbox', src: url, alt: (asset && asset.name) || '图片' }), { wide: true }));
    }
    el.addEventListener(video ? 'loadedmetadata' : 'load', () => { if (stick) scrollBottom(); });
    fill(box, el);
  }).catch((err) => {
    if (box.isConnected) fill(box, h('span', { class: 'error-text small' }, '素材读取失败：' + ((err && err.message) || '')));
  });
  return box;
}

function renderUser(el, m) {
  const meta = m.metadata || {};
  const ids = Array.isArray(meta.asset_ids) ? meta.asset_ids : [];
  fill(el, 
    ids.length ? h('div', { class: 'media-row' }, ids.map(media)) : null,
    h('div', { class: 'bubble' }, m.content || ''),
    meta.mode && MODE_NAMES[meta.mode] ? h('span', { class: 'mode-tag' }, MODE_NAMES[meta.mode] + '模式') : null);
}

const DOT_LABEL = { running: '进行中', awaiting: '等待确认', ok: '完成', error: '失败', skipped: '已跳过' };
function dot(status) {
  return h('span', { class: 'mark ' + (DOT_LABEL[status] ? status : 'idle'), role: 'img', 'aria-label': DOT_LABEL[status] || '未运行' },
    status === 'ok' ? icon('check', 10, 1.8) : status === 'error' ? icon('x', 10, 1.8) : null);
}

function clip(text, limit = 20000) {
  const s = typeof text === 'string' ? text : JSON.stringify(text, null, 2);
  return s.length > limit ? s.slice(0, limit) + '\n…（内容过长，已省略）' : s;
}

function goalLabel(goal, live) {
  const steps = Number(goal.steps) || 0;
  const missing = goal.missing ? String(goal.missing) : '';
  switch (goal.status) {
    case 'done': return '已完成 · 已复核';
    case 'blocked': return '需要你回复';
    case 'unverified': return '已结束 · 未能确认完成' + (missing ? '：' + missing : '');
    case 'incomplete': return `未完成 · 用了 ${steps} 步`;
    default: return (live ? `进行中 · 第 ${steps} 步` : '已中断') + (missing ? ' · 复核：' + missing : '');
  }
}

function renderAssistant(node, m, last) {
  if (!node.parts) {
    node.parts = {
      goal: h('div', { class: 'goal', hidden: true }),
      plan: h('div', { hidden: true }),
      steps: h('div', { class: 'steps' }),
      ask: h('div', { class: 'options', hidden: true, role: 'group', 'aria-label': '可选的回答' }),
      status: h('div', { class: 'msg-status' }),
      sources: h('div', { class: 'sources', hidden: true }),
      stepNodes: [],
      sourceSig: '',
    };
    node.el.append(node.parts.goal, node.parts.plan, node.parts.steps, node.parts.ask, node.parts.status, node.parts.sources);
  }
  const parts = node.parts;
  const meta = m.metadata || {};
  const live = m.status === 'queued' || m.status === 'streaming';
  const job = chatJob(m);

  // goal
  parts.goal.hidden = !meta.goal;
  if (meta.goal) {
    parts.goal.className = 'goal ' + (meta.goal.status || 'running');
    const title = meta.task && meta.task.title ? `后台任务「${meta.task.title}」· ` : '目标 · ';
    fill(parts.goal, icon('target', 15), h('span', null, title + goalLabel(meta.goal, live)));
  }
  const plan = Array.isArray(meta.plan) ? meta.plan : [];
  parts.plan.hidden = !plan.length;
  if (plan.length) fill(parts.plan, planRows(plan, live));

  // steps, in place
  const steps = stepsOf(m);
  steps.forEach((step, i) => {
    let sn = parts.stepNodes[i];
    if (!sn) {
      sn = { text: null, textEl: h('div', { class: 'said-group', hidden: true }), callsEl: h('div', { class: 'calls' }), calls: [] };
      sn.el = h('div', { class: 'step' }, sn.textEl, sn.callsEl);
      parts.stepNodes[i] = sn;
      parts.steps.append(sn.el);
    }
    const text = step.text || '';
    if (text !== sn.text) {
      sn.text = text;
      sn.textEl.hidden = !text;
      // Said the way a person messages: a few short messages, not one block.
      fill(sn.textEl, ...bubbles(text).map((part) => h('div', { class: 'md said' }, renderMarkdown(part))));
    }
    // The question a reply ends on is shown as its options, not as a tool call.
    const calls = (Array.isArray(step.calls) ? step.calls : []).filter((call) => call && call.name !== 'ask_user');
    sn.el.hidden = !text && !calls.length;
    calls.forEach((call, j) => {
      let cn = sn.calls[j];
      if (!cn) {
        cn = { sig: null, details: null, mediaKey: '', main: h('div'), jobEl: h('div', { class: 'call-job', hidden: true }), mediaEl: h('div', { class: 'media-row', hidden: true }) };
        cn.el = h('div', { class: 'call' }, cn.main, cn.jobEl, cn.mediaEl);
        sn.calls[j] = cn;
        sn.callsEl.append(cn.el);
      }
      const linked = call.job_id ? lookupJob(call.job_id) : null;
      const sig = JSON.stringify([call, jobSig(linked)]);
      if (sig !== cn.sig) { cn.sig = sig; updateCall(cn, call, m, linked); }
    });
    while (sn.calls.length > calls.length) sn.calls.pop().el.remove();
    sn.callsEl.hidden = !calls.length;
  });
  while (parts.stepNodes.length > steps.length) parts.stepNodes.pop().el.remove();

  // the options of the question this reply ended on: live for the latest reply, a record of the choice for older ones
  const options = !live && meta.ask && Array.isArray(meta.ask.options) ? meta.ask.options.map(String) : [];
  parts.ask.hidden = !options.length;
  fill(parts.ask, options.map((option) => {
    const chosen = node.answer === option;
    return h('button', { class: 'option' + (chosen ? ' chosen' : ''), type: 'button', disabled: !last, 'aria-pressed': last ? null : String(chosen),
      onclick: () => answer(option) }, h('span', null, option), chosen ? icon('check', 14, 1.8) : null);
  }));

  // status line
  fill(parts.status);
  if (live) {
    parts.status.append(h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { class: 'muted' }, (job && job.phase) || (m.status === 'queued' ? '排队中' : '正在回复')));
    if (job && job.payload && job.payload.background) {
      // The composer's stop button belongs to the reply being waited for; background work is stopped here.
      if (meta.background) parts.status.append(h('span', { class: 'muted' }, '· 已转到后台，做完发在这里，你可以接着聊'));
      parts.status.append(action('停止', async () => { await post('/api/jobs/' + encodeURIComponent(job.id) + '/cancel'); refreshState(); }, { class: 'btn small' }));
    }
  } else if (m.status === 'failed' || m.status === 'cancelled' || m.status === 'interrupted') {
    const fallback = m.status === 'failed' ? '回复失败' : m.status === 'cancelled' ? '已停止' : '回复被中断';
    parts.status.append(h('span', { class: m.status === 'failed' ? 'error-text' : 'muted' }, meta.error || (m.status === 'failed' && job && job.error) || fallback));
    if (last && job && !isLive(job) && job.status !== 'succeeded') {
      parts.status.append(action('重试', async () => {
        const next = await post('/api/jobs/' + encodeURIComponent(job.id) + '/retry');
        if (store.state && next && next.id && !store.state.jobs.some((j) => j.id === next.id)) store.state.jobs.unshift(next);
        stick = true;
        await loadMessages(false);
        refreshState();
      }, { class: 'btn small' }));
    }
  } else {
    if (meta.truncated) parts.status.append(h('span', { class: 'muted small' }, '回复达到长度上限，可能不完整。'));
    if (meta.model) parts.status.append(h('span', { class: 'muted small' }, modelLabel(meta.model)));
    const words = steps.map((step) => step.text || '').filter(Boolean).join('\n\n');
    if (speech.tts && m.status === 'complete' && words) {
      const reading = voice.speakingKey() === m.id;
      parts.status.append(h('button', { class: 'icon-btn small read', type: 'button', 'aria-pressed': String(reading), 'aria-label': reading ? '停止朗读' : '朗读', title: reading ? '停止朗读' : '朗读',
        onclick: () => { if (voice.speakingKey() === m.id) voice.stopSpeaking(); else { voice.unlock(); voice.speak(words, m.id).catch((err) => notify(err.message)); } } }, icon(reading ? 'stop' : 'volume', 15)));
    }
  }
  parts.status.hidden = !parts.status.childNodes.length;

  // sources
  const sources = Array.isArray(meta.sources) ? meta.sources.filter((s) => s && /^https?:\/\//i.test(String(s.url || ''))) : [];
  const sourceSig = JSON.stringify(sources);
  if (sourceSig !== parts.sourceSig) {
    parts.sourceSig = sourceSig;
    parts.sources.hidden = !sources.length;
    fill(parts.sources, h('div', { class: 'group-title' }, '来源'),
      h('ol', null, sources.map((s) => h('li', null, h('a', { href: String(s.url), target: '_blank', rel: 'noopener noreferrer' }, s.title || s.url)))));
  }
}

function callHead(call) {
  return [call.status === 'ok' ? null : dot(call.status), icon(toolIcon(call.name), 14), h('span', { class: 'call-label' }, call.label || call.name || '工具'),
    call.summary ? h('span', { class: 'call-summary' }, ' · ' + call.summary) : null];
}

function labelled(label, text, cls = '') {
  return h('div', { class: 'call-block' }, h('div', { class: 'muted small' }, label), h('pre', { class: 'mono ' + cls }, clip(text)));
}

function callBody(call) {
  const body = [];
  if (call.title) body.push(h('div', { class: 'small' }, String(call.title)));
  if (call.command) body.push(labelled(call.cwd ? '命令 · ' + call.cwd : '命令', call.command));
  if (call.console) body.push(labelled('输出', call.console, 'console'));
  if (call.output !== undefined && call.output !== null && call.output !== '') body.push(labelled('结果', call.output));
  if (typeof call.exit_code === 'number' && call.exit_code !== 0) body.push(h('div', { class: 'error-text small' }, '退出码 ' + call.exit_code));
  if (Array.isArray(call.steps) && call.steps.length) {
    body.push(h('div', { class: 'substeps' }, call.steps.map((step) => h('div', { class: 'substep' },
      step && step.text ? h('div', { class: 'md small' }, renderMarkdown(step.text)) : null,
      ((step && step.calls) || []).map((sub) => h('div', { class: 'call-row flat' }, callHead(sub)))))));
  }
  return body;
}

function approval(call, m) {
  const buttons = [['run', '允许', 'btn primary', 'check'], ['always', '本任务内都允许', 'btn', null], ['deny', '拒绝', 'btn', 'x']].map(([decision, label, cls, glyph]) => {
    const button = h('button', { class: cls, type: 'button' }, glyph ? icon(glyph) : null, label);
    button.addEventListener('click', async () => {
      const others = buttons.filter((b) => b !== button);
      const ok = await run(button, async () => {
        others.forEach((b) => { b.disabled = true; });
        try {
          await post('/api/messages/' + encodeURIComponent(m.id) + '/commands/' + encodeURIComponent(call.id), { decision });
        } catch (err) {
          others.forEach((b) => { b.disabled = false; });
          throw err;
        }
      });
      if (ok) {
        buttons.forEach((b) => { b.disabled = true; });
        refreshMessages();
        refreshState();
      }
    });
    return button;
  });
  return h('div', { class: 'approval' },
    h('div', { class: 'approval-head' }, h('span', { class: 'tile round' }, icon(call.command ? 'terminal' : toolIcon(call.name))),
      h('strong', null, call.command ? 'Jurvis 想运行一条命令' : (call.label || '需要你确认'))),
    call.command ? h('pre', { class: 'mono' }, clip(call.command, 4000)) : null,
    call.reason ? h('p', { class: 'approval-reason' }, String(call.reason)) : null,
    call.cwd ? h('div', { class: 'muted small' }, '目录：' + call.cwd) : null,
    h('div', { class: 'approval-actions' }, buttons));
}

// Cards: what a tool call made or found, shown as the thing itself instead of as a tool call.
// A made thing (goal, schedule, memory, document, task) is one row that opens where it lives; a lookup (weather, price…) shows its figures.
const MADE = { goal: ['target', 'goals'], schedule: ['clock', 'goals'], memory: ['bookmark', 'memory'], file: ['doc', 'library'], task: ['tasks', null], session: ['leaf', null] };
const GLYPH = { sun: '☀️', moon: '🌙', partly: '⛅', cloud: '☁️', fog: '🌫️', drizzle: '🌦️', rain: '🌧️', snow: '❄️', storm: '⛈️', coin: '🪙', exchange: '💱', calendar: '📅', wallet: '🧾', heart: '❤️', drop: '🌸' };

function sparkline(series, trend, label) {
  const points = series.map(Number).filter(Number.isFinite);
  if (points.length < 3) return null;
  const low = Math.min(...points), span = Math.max(...points) - low || 1;
  const path = points.map((p, i) => `${(i / (points.length - 1) * 100).toFixed(2)},${(30 - (p - low) / span * 28 - 1).toFixed(2)}`).join(' ');
  const box = h('div', { class: 'spark ' + (trend || 'flat'), role: 'img', 'aria-label': label || '近 7 天走势' });
  box.innerHTML = `<svg viewBox="0 0 100 30" preserveAspectRatio="none"><polyline points="${path}" fill="none" stroke="currentColor" stroke-width="1.5" vector-effect="non-scaling-stroke" stroke-linejoin="round"/></svg>`;
  return box;
}

/** The outfit illustration of a weather card: the picture once it is drawn, a quiet note while it is being drawn. */
function cardArt(card, job) {
  const done = job && job.status === 'succeeded' && job.result && Array.isArray(job.result.asset_ids) ? job.result.asset_ids[0] : null;
  const id = card.image || done;
  if (id) {
    const box = h('div', { class: 'info-art' });
    assetURL(id).then((url) => fill(box, h('img', { src: url, alt: '今天的穿搭插画' })), () => box.remove());
    return box;
  }
  if (card.image_job && job && isLive(job)) return h('div', { class: 'info-art drawing' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('span', { class: 'muted small' }, '正在画穿搭'));
  return null;
}

function cardView(card, job) {
  const made = MADE[card.kind];
  if (made) {
    const [glyph, tab] = made;
    const inner = [h('span', { class: 'made-icon' }, icon(glyph, 18)),
      h('span', { class: 'made-text' }, h('strong', null, card.title), h('span', { class: 'muted small' }, card.subtitle || ''),
        card.note ? h('span', { class: 'made-note' }, card.note) : null),
      tab ? icon('right', 16) : null];
    if (card.kind === 'session' && card.session_id) {
      inner[2] = icon('right', 16);
      return h('button', { class: 'made-card companion', type: 'button', onclick: () => { Promise.resolve(refreshState()).finally(() => openSession(card.session_id)); } }, inner);
    }
    return tab ? h('button', { class: 'made-card', type: 'button', onclick: () => emit('goto', tab) }, inner) : h('div', { class: 'made-card still' }, inner);
  }
  const rows = Array.isArray(card.rows) ? card.rows : [];
  const days = Array.isArray(card.days) ? card.days : [];
  const money = card.kind === 'ledger' || card.kind === 'expense';
  const body = card.kind === 'health' || card.kind === 'cycle' || card.kind === 'mood';
  const go = money ? 'ledger' : body ? 'health' : null;
  return h(go ? 'button' : 'div', { class: 'info-card ' + String(card.kind || ''), type: go ? 'button' : null, onclick: go ? () => emit('goto', go) : null },
    h('div', { class: 'info-head' },
      h('div', { class: 'made-text' }, h('strong', null, card.title), card.subtitle ? h('span', { class: 'muted small' }, card.subtitle) : null),
      GLYPH[card.icon] ? h('span', { class: 'info-glyph', 'aria-hidden': 'true' }, GLYPH[card.icon]) : null),
    // The figure and what to wear sit beside the temperature: the picture is the advice at a glance.
    h('div', { class: 'info-split' },
      h('div', { class: 'info-left' },
        card.value ? h('div', { class: 'info-value ' + (card.trend || '') }, String(card.value), card.kind === 'countdown' && card.value !== '今天' ? h('span', { class: 'info-unit' }, '天') : null) : null,
        card.outfit && Array.isArray(card.outfit.wear) ? h('div', { class: 'info-outfit' },
          h('div', { class: 'chips' }, card.outfit.wear.map((item) => h('span', { class: 'chip static' }, String(item)))),
          card.outfit.tip ? h('div', { class: 'small' }, card.outfit.tip) : null) : null),
      card.kind === 'weather' ? cardArt(card, job) : null),
    Array.isArray(card.series) ? sparkline(card.series, card.trend, card.series_label) : null,
    Array.isArray(card.bars) && card.bars.length ? bars(card.bars) : null,
    rows.length ? h('dl', { class: 'info-rows' }, rows.map((row) => h('div', { class: String(row.label || '').length + String(row.value).length > 13 ? 'wide' : null }, row.label ? h('dt', null, row.label) : null, h('dd', null, String(row.value))))) : null,
    days.length ? h('div', { class: 'info-days' }, days.map((day) => h('div', { class: 'info-day' },
      h('span', { class: 'muted small' }, day.label), h('span', { 'aria-hidden': 'true' }, GLYPH[day.icon] || ''),
      h('span', { class: 'small' }, `${day.high}° `, h('span', { class: 'muted' }, `${day.low}°`))))) : null,
    card.note ? h('div', { class: 'muted small info-note' }, card.note) : null);
}

function updateCall(cn, call, m, job) {
  const wasOpen = !!(cn.details && cn.details.open);
  cn.details = null;
  if (call.status === 'awaiting') fill(cn.main, approval(call, m));
  else if (call.card && call.card.title) fill(cn.main, cardView(call.card, job));
  else {
    const body = callBody(call);
    if (body.length) {
      cn.details = disclosure('call-row', callHead(call), h('div', { class: 'call-body' }, body), wasOpen);
      fill(cn.main, cn.details);
      if (wasOpen && call.status === 'running') {
        const view = cn.details.querySelector('pre.console');
        if (view) view.scrollTop = view.scrollHeight;
      }
    } else fill(cn.main, h('div', { class: 'call-row flat' }, callHead(call)));
  }
  // the linked job: live progress, then its output
  fill(cn.jobEl);
  if (call.card && call.card.image_job) {  // the drawing for a card is shown inside the card
    cn.jobEl.hidden = true;
    cn.mediaEl.hidden = true;
    return;
  }
  if (job && isLive(job)) {
    const percent = Math.max(0, Math.min(100, Math.round((job.progress || 0) * 100)));
    cn.jobEl.append(h('span', { class: 'muted small' }, (job.phase || (job.status === 'queued' ? '排队中' : '进行中')) + ' · ' + percent + '%'),
      h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(percent) }, h('div', { class: 'progress-bar', style: `width:${percent}%` })));
  } else if (job && job.status === 'failed') cn.jobEl.append(h('span', { class: 'error-text small' }, job.error || '任务失败'));
  else if (job && job.status === 'cancelled') cn.jobEl.append(h('span', { class: 'muted small' }, '任务已取消'));
  else if (job && job.status === 'interrupted') cn.jobEl.append(h('span', { class: 'muted small' }, '任务被中断'));
  cn.jobEl.hidden = !cn.jobEl.childNodes.length;
  const ids = job && job.status === 'succeeded' && job.result && Array.isArray(job.result.asset_ids) ? job.result.asset_ids : [];
  const key = ids.join(',');
  if (key !== cn.mediaKey) {
    cn.mediaKey = key;
    cn.mediaEl.hidden = !ids.length;
    fill(cn.mediaEl, ...ids.map(media));
  }
}

/** The plan as rows with status marks. */
function planRows(plan, live) {
  const next = plan.findIndex((item) => item.status !== 'done');
  return h('div', { class: 'plan' }, plan.map((item, index) => {
    const done = item.status === 'done';
    const now = !done && index === next && live;
    return h('div', { class: 'plan-row' + (done ? '' : now ? ' now' : ' pending') },
      h('span', { class: 'mark' + (done ? ' ok' : now ? ' running' : ''), role: 'img', 'aria-label': done ? '完成' : now ? '进行中' : '未开始' }, done ? icon('check', 11, 1.8) : null),
      h('span', null, String(item.step || '')));
  }));
}
