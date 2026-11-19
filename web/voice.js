// Voice: recording from the microphone, sending it to the Mac to be transcribed, and playing replies read aloud by the Mac.
// The models run on the Mac (see engine/speech.py), so this file only records, uploads and plays.
import { h, fill, api, get, ApiError } from './core.js';
import { icon } from './icons.js';

let info = null;

/** What the Mac can do: {stt, tts, speakers, speaker}. Cached; pass true to ask again. */
export async function status(force = false) {
  if (!info || force) {
    try { info = await get('/api/speech'); } catch { return { stt: false, tts: false, speakers: [], speaker: '' }; }
  }
  return info;
}

export const canRecord = () => !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);

function explain(err) {
  const name = err && err.name;
  if (name === 'NotAllowedError' || name === 'SecurityError') return '没有麦克风权限。请在浏览器的网站设置里允许这个页面使用麦克风。';
  if (name === 'NotFoundError') return '没有找到麦克风。';
  return (err && err.message) || String(err);
}

/**
 * Starts recording. Resolves to {stop, cancel, result}; result resolves to the recording, or null when nothing was said.
 * With autoStop the recording ends by itself once the speaker has been quiet for a moment.
 */
export async function listen({ onLevel, autoStop = true, quiet = 1300, patience = 8000, longest = 90000 } = {}) {
  if (!canRecord()) throw new Error('这个浏览器不能录音');
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } });
  } catch (err) { throw new Error(explain(err)); }
  const type = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/webm'].find((t) => MediaRecorder.isTypeSupported(t)) || '';
  const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  const chunks = [];
  recorder.ondataavailable = (event) => { if (event.data && event.data.size) chunks.push(event.data); };

  const Context = window.AudioContext || window.webkitAudioContext;
  const context = new Context();
  const analyser = context.createAnalyser();
  analyser.fftSize = 1024;
  context.createMediaStreamSource(stream).connect(analyser);
  const samples = new Float32Array(analyser.fftSize);

  const began = performance.now();
  let heard = false, quietAt = 0, floor = 1, cancelled = false, frame = 0, settle;
  const result = new Promise((resolve) => { settle = resolve; });
  const stop = () => { if (recorder.state !== 'inactive') recorder.stop(); };
  recorder.onstop = () => {
    cancelAnimationFrame(frame);
    for (const track of stream.getTracks()) track.stop();
    context.close().catch(() => {});
    settle(cancelled || (autoStop && !heard) || !chunks.length ? null : new Blob(chunks, { type: recorder.mimeType || type || 'audio/webm' }));
  };
  const tick = () => {
    analyser.getFloatTimeDomainData(samples);
    let sum = 0;
    for (let i = 0; i < samples.length; i += 1) sum += samples[i] * samples[i];
    const level = Math.sqrt(sum / samples.length);
    const now = performance.now();
    if (now - began < 400) floor = Math.min(floor, Math.max(level, 0.002));   // the room's own noise, measured before speech starts
    const loud = level > Math.max(0.018, floor * 3.5);
    if (loud) { heard = true; quietAt = 0; } else if (heard && !quietAt) quietAt = now;
    if (onLevel) onLevel(Math.min(1, level * 9), heard);
    if (now - began > longest || (autoStop && ((heard && quietAt && now - quietAt > quiet) || (!heard && now - began > patience)))) { stop(); return; }
    frame = requestAnimationFrame(tick);
  };
  recorder.start(250);
  frame = requestAnimationFrame(tick);
  return { stop, cancel: () => { cancelled = true; stop(); }, result };
}

/** The words in a recording. */
export async function transcribe(blob) {
  const form = new FormData();
  form.append('file', blob, blob.type.includes('mp4') ? 'speech.m4a' : 'speech.webm');
  const answer = await api('POST', '/api/speech/transcribe', form);
  return (answer.text || '').trim();
}

// ---------------------------------------------------------------- playback
const SILENCE = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=';
const player = new Audio();
player.preload = 'auto';
let current = null;   // the reading in progress
const watchers = new Set();
export function onSpeaking(fn) { watchers.add(fn); return () => watchers.delete(fn); }
function tell() { for (const fn of watchers) { try { fn(current ? current.key : null); } catch (err) { console.error(err); } } }
export const speakingKey = () => (current ? current.key : null);

/** Call from a tap: phones only let a page play sound later if it has played once in response to a touch. */
export function unlock() {
  if (player.dataset.unlocked || current) return;
  player.src = SILENCE;
  // by the time this resolves a real reading may have started on the same element, so only pause our own silence
  player.play().then(() => { player.dataset.unlocked = '1'; if (player.src === SILENCE) player.pause(); }).catch(() => {});
}

/** What to read for a Markdown reply, in pieces short enough that the first one starts quickly. */
export function pieces(text, limit = 140) {
  const clean = String(text || '').replace(/```[\s\S]*?(```|$)/g, ' ').replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/https?:\/\/\S+/g, ' ').replace(/^\s{0,3}(#{1,6}|[-*+]|\d+[.)]|>)\s+/gm, '')
    .replace(/^\s*\|?[\s:|-]{3,}\|?\s*$/gm, ' ').replace(/[*_`~|]+/g, ' ');
  const out = [];
  let now = '';
  for (let piece of clean.match(/[^。！？!?；;\n]+[。！？!?；;\n]*/g) || []) {
    piece = piece.replace(/\s+/g, ' ');
    while (piece.length > limit) {
      const cut = Math.max(...['，', ',', '、', ' '].map((mark) => piece.lastIndexOf(mark, limit - 1))) + 1 || limit;
      if (now.trim()) out.push(now.trim());
      out.push(piece.slice(0, cut).trim());
      now = '';
      piece = piece.slice(cut);
    }
    if (now.length + piece.length > limit || (now.length >= 40 && /[。！？!?]\s*$/.test(now))) { out.push(now.trim()); now = ''; }
    now += piece;
  }
  if (now.trim()) out.push(now.trim());
  return out.filter((p) => /[\p{L}\p{N}]/u.test(p));
}

export function stopSpeaking() {
  if (!current) return;
  const job = current;
  current = null;
  job.cancelled = true;
  player.pause();
  if (job.release) job.release();
  tell();
}

function play(blob, job) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const done = (err) => {
      player.onended = player.onerror = null;
      URL.revokeObjectURL(url);
      job.release = null;
      if (err) reject(err); else resolve();
    };
    job.release = () => done();
    player.onended = () => done();
    player.onerror = () => done(new Error('声音播放不了'));
    player.src = url;
    player.play().catch((err) => done(err && err.name === 'NotAllowedError' ? new Error('浏览器拦住了自动播放，请点一下朗读按钮') : err));
  });
}

/** Reads text aloud. Resolves when it has finished or been stopped. key tells listeners which message is being read. */
export async function speak(text, key = 'voice') {
  stopSpeaking();
  const parts = pieces(text);
  if (!parts.length) return;
  const job = { key, cancelled: false, release: null };
  current = job;
  tell();
  const fetchPart = (i) => {
    const request = api('POST', '/api/speech/speak', { text: parts[i] }, { blob: true });
    request.catch(() => {});   // a piece fetched ahead may never be awaited
    return request;
  };
  try {
    let next = fetchPart(0);
    for (let i = 0; i < parts.length && !job.cancelled; i += 1) {
      const blob = await next;
      if (job.cancelled) break;
      next = i + 1 < parts.length ? fetchPart(i + 1) : null;
      await play(blob, job);
    }
  } finally {
    if (current === job) { current = null; tell(); }
  }
}

/** Warms the models up so the first sentence does not wait for them to load. */
export function warm() { api('POST', '/api/speech/warm', {}).catch(() => {}); }

// ---------------------------------------------------------------- voice conversation
const STATES = {
  listen: ['在听', '说完会自动发送'],
  hear: ['在听', '说完会自动发送'],
  transcribe: ['正在识别', ''],
  think: ['正在想', '需要一点时间'],
  speak: ['正在说', '点一下圆圈可以打断'],
  paused: ['已暂停', '点一下圆圈继续说'],
};

/**
 * Opens the voice conversation screen. ask(text) sends what was said and resolves to the reply.
 * The loop is: listen, transcribe, ask, read the reply aloud, listen again, until the user ends it.
 */
export function converse({ ask }) {
  unlock();
  warm();
  let active = true, state = '', recording = null, misses = 0, resume = null;
  const ring = h('span', { class: 'voice-ring' });
  const orb = h('button', { class: 'voice-orb', type: 'button', 'aria-label': '打断或继续' }, ring, h('span', { class: 'voice-glyph' }, icon('voice', 30, 1.5)));
  const title = h('p', { class: 'voice-title', role: 'status', 'aria-live': 'polite' });
  const hint = h('p', { class: 'voice-hint' });
  const said = h('p', { class: 'voice-said', hidden: true });
  const reply = h('p', { class: 'voice-reply', hidden: true });
  const end = h('button', { class: 'btn', type: 'button' }, icon('x', 16), '结束');
  const screen = h('div', { class: 'voice-screen', role: 'dialog', 'aria-modal': 'true', 'aria-label': '语音对话' },
    h('div', { class: 'voice-stage' }, orb, title, hint), h('div', { class: 'voice-words' }, said, reply), h('div', { class: 'voice-foot' }, end));

  const set = (next) => {
    state = next;
    screen.dataset.state = next;
    title.textContent = STATES[next][0];
    hint.textContent = STATES[next][1];
    if (next !== 'listen' && next !== 'hear') ring.style.transform = '';
  };
  const show = (el, label, text) => { el.hidden = !text; fill(el, h('span', null, label), text); };
  const close = () => {
    if (!active) return;
    active = false;
    if (recording) recording.cancel();
    stopSpeaking();
    if (resume) resume();
    document.removeEventListener('keydown', onKey);
    screen.remove();
  };
  const onKey = (event) => { if (event.key === 'Escape') close(); };
  end.addEventListener('click', close);
  orb.addEventListener('click', () => {
    if (state === 'speak') stopSpeaking();
    else if (state === 'paused' && resume) resume();
    else if ((state === 'listen' || state === 'hear') && recording) recording.stop();
  });
  document.addEventListener('keydown', onKey);
  document.body.append(screen);
  end.focus();

  (async () => {
    while (active) {
      try {
        set('listen');
        recording = await listen({ onLevel: (level, heard) => {
          ring.style.transform = `scale(${1 + level * 0.55})`;
          if (heard && state === 'listen') set('hear');
        } });
        const blob = await recording.result;
        recording = null;
        if (!active) break;
        if (!blob) {
          misses += 1;
          if (misses >= 2) {   // nobody is talking: stop holding the microphone open until they tap
            set('paused');
            await new Promise((resolve) => { resume = resolve; });
            resume = null;
            misses = 0;
          }
          continue;
        }
        misses = 0;
        set('transcribe');
        const text = await transcribe(blob);
        if (!active) break;
        if (!text) continue;
        show(said, '你', text);
        show(reply, 'Jurvis', '');
        set('think');
        const answer = await ask(text);
        if (!active) break;
        show(reply, 'Jurvis', pieces(answer, 400).join(' ') || answer || '');
        if (answer) {
          set('speak');
          await speak(answer, 'conversation');
        }
      } catch (err) {
        recording = null;
        if (!active) break;
        if (err instanceof ApiError && err.status === 401) { close(); break; }
        set('paused');
        title.textContent = '出了点问题';
        hint.textContent = ((err && err.message) || String(err)) + ' 点一下圆圈重试。';
        await new Promise((resolve) => { resume = resolve; });
        resume = null;
      }
    }
  })();
  return close;
}
