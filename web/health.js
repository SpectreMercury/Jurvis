// 健康: sleep, activity, heart and recovery from Apple Health (the iPhone app), WHOOP and Google Health, and the menstrual cycle.
// This page reads them straight from the Mac. Models see only period summaries, and the cycle only the local model.
import { store, h, fill, get, post, put, del, run, action, notify, sheet } from './core.js';
import * as ui from './ui.js';
import { brandMark } from './connect.js';

let sig = null;
let loading = null;
let metric = 'sleep_min';
const els = {};
const METRICS = [['sleep_min', '睡眠', (v) => (v / 60).toFixed(1) + ' 小时'], ['steps', '步数', (v) => Math.round(v).toLocaleString('zh-CN') + ' 步'],
  ['resting_hr', '静息心率', (v) => Math.round(v) + ' 次/分'], ['hrv_ms', '心率变异性', (v) => Math.round(v) + ' 毫秒'], ['recovery', '恢复', (v) => Math.round(v) + '%']];
const today = () => new Date().toLocaleDateString('sv-SE');

export function mount(root) {
  els.accounts = h('div', { class: 'health-accounts' });
  els.summary = h('div', { class: 'card health-summary' });
  els.chart = h('div', { class: 'card' });
  els.cycle = h('div', { class: 'card health-cycle' });
  els.workouts = h('div', { class: 'card' });
  root.append(h('div', { class: 'page' },
    h('header', { class: 'page-head' }, h('h1', null, '健康')),
    h('p', { class: 'muted small' }, '睡眠、运动、心率和经期都只存在这台 Mac 上。问 Jurvis “我这周睡得怎么样”时，模型只看到汇总数字；经期记录只给本机模型看。'),
    els.accounts, els.summary, els.chart, els.cycle, els.workouts));
}

export function activate() { sig = null; refresh(); }

export function refresh() {
  if (store.tab !== 'health' || loading) return loading;
  loading = get('/api/health?days=30').then(render, () => {}).finally(() => { loading = null; });
  return loading;
}

const ago = (seconds) => {
  if (!seconds) return '';
  const m = Math.round((Date.now() / 1000 - seconds) / 60);
  return m < 2 ? '刚刚' : m < 60 ? `${m} 分钟前` : m < 1440 ? `${Math.round(m / 60)} 小时前` : `${Math.round(m / 1440)} 天前`;
};

function accounts(page) {
  fill(els.accounts, connectorRows(page.accounts || [], () => { sig = null; refresh(); }));
}

// ---------------------------------------------------------------- connectors
// Apple Health and Apple Fitness are read on the iPhone; WHOOP and Google Health are connected with an OAuth app of the user's own,
// entered here (or in .env). The same rows are on the 健康 page and under 设置 → 健康与运动.
function deviceHint(c) {
  if (c.connected) return `已同步 · ${ago(c.synced_at)}` + (c.provider === 'fitness' ? ` · 最近 30 天 ${c.workouts_30_days || 0} 次运动` : '');
  return '在 iPhone 上打开 Jurvis →「健康」→「同步 Apple 健康」，允许读取后自动同步';
}

export function connectorRows(list, changed) {
  return list.map((c) => {
    let sub, buttons = [];
    if (c.kind === 'device') sub = deviceHint(c);
    else if (c.connected) {
      sub = c.error || `已连接 · ${c.synced_at ? '同步于 ' + ago(c.synced_at) : '正在同步'}`;
      buttons = [action('立即同步', async () => { const r = await post('/api/health/sync/' + c.provider); notify(`已同步 ${r.days} 天、${r.workouts} 次运动`, 'info'); changed(); }, { class: 'btn small' }),
        action('断开', async () => { if (!await ui.ask(`断开 ${c.name}？已同步的数据会保留。`)) return; await del('/api/health/connect/' + c.provider); changed(); }, { class: 'btn small' })];
    } else {
      sub = c.configured ? (c.on_this_mac ? '未连接 · 请在这台 Mac 上点「连接」' : '未连接') : '需要先填一个你自己的开发者应用（只需一次）';
      buttons = [h('button', { class: 'btn small', type: 'button', onclick: () => setup(c, changed) }, '设置')];
      if (c.configured) buttons.push(action('连接', async () => { const r = await post('/api/health/connect/' + c.provider); window.open(r.url, '_blank', 'noopener'); }, { class: 'btn primary small' }));
    }
    return h('div', { class: 'health-account' }, brandMark(c.name, '', c.provider),
      h('div', { class: 'made-text grow' }, h('strong', null, c.name), h('span', { class: 'muted small' }, c.detail || ''), h('span', { class: (c.error ? 'error-text' : 'muted') + ' small' }, sub)),
      h('div', { class: 'row' }, buttons));
  });
}

function setup(c, changed) {
  const id = h('input', { class: 'input', autocomplete: 'off', autocapitalize: 'off', spellcheck: false, value: c.client_id || '', placeholder: 'Client ID' });
  const secret = h('input', { class: 'input', type: 'password', autocomplete: 'off', placeholder: c.configured ? '留空则保留已保存的密钥' : 'Client Secret' });
  const save = h('button', { class: 'btn primary', type: 'submit' }, '保存');
  const copy = h('button', { class: 'btn small', type: 'button', onclick: () => navigator.clipboard.writeText(c.redirect_uri).then(() => notify('已复制回调地址', 'info'), () => {}) }, '复制');
  const form = h('form', { class: 'stack' },
    h('p', { class: 'muted small' }, c.setup),
    h('a', { href: c.console, target: '_blank', rel: 'noopener', class: 'small' }, '打开 ' + c.name + ' 开发者后台 ↗'),
    h('label', { class: 'field' }, h('span', null, '回调地址'), h('div', { class: 'row' }, h('code', { class: 'break grow' }, c.redirect_uri), copy)),
    c.client_from === 'env' ? h('p', { class: 'muted small' }, '现在用的是 .env 里的应用；在这里保存的只在 .env 没填时生效。') : null,
    h('label', { class: 'field' }, h('span', null, 'Client ID'), id),
    h('label', { class: 'field' }, h('span', null, 'Client Secret'), secret),
    h('p', { class: 'muted small' }, '只保存在这台 Mac 上（data/security/health.json），不会再发回任何设备。'),
    h('div', { class: 'row end' }, save));
  const close = sheet(c.name, form);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const ok = await run(save, async () => { await put('/api/health/client/' + c.provider, { client_id: id.value, client_secret: secret.value }); });
    if (ok) { close(); changed(); notify(`已保存，可以点「连接」了`, 'info'); }
  });
}

function summary(s) {
  if (!s.days_with_data) {
    fill(els.summary, h('div', { class: 'empty' }, h('p', { class: 'empty-title' }, '还没有健康数据'),
      h('p', { class: 'muted' }, '在 iPhone 上打开 Jurvis 同步 Apple 健康，或者在上面连接 WHOOP / Google Health。')));
    return;
  }
  const facts = [];
  if (s.sleep) facts.push(['平均睡眠', s.sleep.average], ['不足 6 小时', `${s.sleep.short_nights_under_6h} 晚`]);
  if (s.activity && s.activity.steps_average) facts.push(['日均步数', s.activity.steps_average.toLocaleString('zh-CN')]);
  if (s.activity && s.activity.workouts) facts.push(['运动', `${s.activity.workouts} 次 · ${s.activity.workout_minutes} 分钟`]);
  if (s.heart && s.heart.resting_hr_average) facts.push(['静息心率', `${s.heart.resting_hr_average} 次/分`]);
  if (s.heart && s.heart.hrv_average_ms) facts.push(['心率变异性', `${s.heart.hrv_average_ms} 毫秒`]);
  if (s.recovery) facts.push(['平均恢复', `${s.recovery.average}%`]);
  if (s.mood) facts.push(['心情', `${s.mood.average_1_to_5} / 5`]);
  fill(els.summary,
    h('div', { class: 'muted small' }, `最近 7 天 · ${s.from} 至 ${s.to}`),
    h('dl', { class: 'info-rows' }, facts.map(([k, v]) => h('div', null, h('dt', null, k), h('dd', null, String(v))))),
    s.stress ? h('div', { class: 'health-stress ' + (s.stress.level === '平稳' ? 'calm' : 'high') },
      h('strong', null, `身体压力：${s.stress.level}`), s.stress.signs.length ? h('span', null, s.stress.signs.join('；')) : null,
      h('span', { class: 'muted small' }, s.stress.note)) : null);
}

function chart(days) {
  const [key, label, format] = METRICS.find((m) => m[0] === metric);
  const values = days.map((d) => (typeof d[key] === 'number' ? d[key] : null));
  const top = Math.max(...values.filter((v) => v !== null), 1);
  const tabs = h('div', { class: 'row', role: 'group', 'aria-label': '指标' }, METRICS.filter(([k]) => days.some((d) => typeof d[k] === 'number')).map(([k, name]) =>
    h('button', { class: 'chip toggle', type: 'button', 'aria-pressed': String(k === metric), onclick: () => { metric = k; sig = null; refresh(); } }, name)));
  fill(els.chart, h('div', { class: 'page-subhead' }, h('strong', null, '最近 30 天'), tabs),
    h('div', { class: 'health-chart', role: 'img', 'aria-label': `最近 30 天${label}` }, days.map((d, i) => {
      const v = values[i];
      return h('div', { class: 'health-bar' + (d.day === today() ? ' today' : ''), title: `${d.day} · ${v === null ? '无数据' : format(v)}` },
        h('span', { style: `height:${v === null ? 0 : Math.max(3, v / top * 100).toFixed(1)}%` }));
    })),
    h('div', { class: 'health-axis muted small' }, h('span', null, days[0].day.slice(5)), h('span', null, '今天')));
}

function cycle(page) {
  const c = page.cycle || {};
  const flowDays = new Set((page.flow_days || []).map((f) => f.day));
  const started = action('今天来了', async () => { await post('/api/health/period', { start: today() }); sig = null; refresh(); }, { class: 'btn primary small' });
  const ended = action('今天结束了', async () => { await post('/api/health/period', { ended: true, day: today() }); sig = null; refresh(); }, { class: 'btn small' });
  const head = h('div', { class: 'page-subhead' }, h('strong', null, '经期'), h('div', { class: 'row' }, flowDays.has(today()) ? ended : started));
  if (!c.periods_recorded) {
    fill(els.cycle, head, h('p', { class: 'muted small' }, '还没有记录。对 Jurvis 说“今天来例假了”，或者点上面的按钮；iPhone 上的 Apple 健康经期记录也会同步过来。'));
    return;
  }
  const strip = [];
  const now = new Date();
  for (let i = -20; i <= 20; i += 1) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + i).toLocaleDateString('sv-SE');
    const predicted = c.next_period_estimate && d >= c.next_period_estimate && d < shift(c.next_period_estimate, c.average_period_days);
    strip.push(h('span', { class: 'cycle-day' + (flowDays.has(d) ? ' flow' : predicted ? ' predicted' : '') + (i === 0 ? ' today' : ''), title: d + (flowDays.has(d) ? ' · 经期' : predicted ? ' · 预计' : '') }));
  }
  fill(els.cycle, head,
    h('div', { class: 'info-value' }, c.days_until_next >= 0 ? `${c.days_until_next} 天后` : '随时可能来', h('span', { class: 'muted small' }, ` · 预计 ${c.next_period_estimate.slice(5).replace('-', '/')}`)),
    h('div', { class: 'muted small' }, `周期第 ${c.cycle_day_today} 天 · ${c.phase} · 平均周期 ${c.average_cycle_days} 天，经期 ${c.average_period_days} 天${c.irregular ? ' · 最近不太规律' : ''}`),
    h('div', { class: 'cycle-strip', role: 'img', 'aria-label': '前后 20 天的经期和预计' }, strip),
    c.common_symptoms_90_days && c.common_symptoms_90_days.length ? h('div', { class: 'chips' }, c.common_symptoms_90_days.map((x) => h('span', { class: 'chip static' }, x))) : null,
    h('p', { class: 'muted small' }, '根据你自己的记录估算，不能用于避孕。'));
}

function shift(day, n) {
  const d = new Date(day + 'T12:00:00');
  d.setDate(d.getDate() + n);
  return d.toLocaleDateString('sv-SE');
}

function workouts(list) {
  if (!list.length) { fill(els.workouts, h('div', { class: 'page-subhead' }, h('strong', null, '运动')), h('p', { class: 'muted small' }, '最近 30 天没有运动记录。')); return; }
  fill(els.workouts, h('div', { class: 'page-subhead' }, h('strong', null, '运动')), list.slice(0, 20).map((w) => h('div', { class: 'ledger-row' },
    h('div', { class: 'made-text' }, h('strong', null, w.kind_label),
      h('span', { class: 'muted small' }, new Date(w.start * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }))),
    h('span', { class: 'muted small' }, [w.distance_km ? `${w.distance_km} 公里` : '', w.kcal ? `${Math.round(w.kcal)} 千卡` : '', w.avg_hr ? `${Math.round(w.avg_hr)} 次/分` : ''].filter(Boolean).join(' · ')),
    h('span', { class: 'ledger-amount' }, `${Math.round(w.minutes)} 分钟`))));
}

function render(page) {
  const next = JSON.stringify([metric, page]);
  if (next === sig) return;
  sig = next;
  accounts(page);
  summary(page.summary || {});
  chart(page.days || []);
  cycle(page);
  workouts(page.workouts || []);
}

