// 目标: what Jurvis keeps doing for the user over time. Long-term goals it tracks and work it runs on a schedule.
// Both are set up in conversation; here they are listed and can be removed.
import { store, h, fill, get, del, action } from './core.js';
import * as ui from './ui.js';
import { icon } from './icons.js';

let sig = null;
let loading = null;
const els = {};

export function mount(root) {
  els.goals = h('div', { class: 'cards single' });
  els.schedules = h('div', { class: 'cards single' });
  root.append(h('div', { class: 'page' },
    h('header', { class: 'page-head' }, h('h1', null, '目标')),
    h('h2', { class: 'group-title' }, '长期目标'), els.goals,
    h('h2', { class: 'group-title' }, '定时任务'), els.schedules));
}

export function activate() { refresh(); }

/** Reloads the lists; called when the tab opens and when the engine reports a change. */
export function refresh() {
  if (store.tab !== 'goals' || loading) return loading;
  loading = get('/api/agenda').then(render, () => {}).finally(() => { loading = null; });
  return loading;
}

/** A date with its weekday and time: a schedule's next run is usually days away, so the time alone says too little. */
function day(seconds) {
  return new Date(seconds * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', weekday: 'short', hour: '2-digit', minute: '2-digit' });
}

function empty(title, hint) {
  return h('div', { class: 'empty' }, h('p', { class: 'empty-title' }, title), h('p', { class: 'muted' }, hint));
}

function render(agenda) {
  const next = JSON.stringify(agenda);
  if (next === sig) return;
  sig = next;
  const goals = agenda.goals || [];
  const schedules = agenda.schedules || [];
  fill(els.goals, goals.length ? goals.map((goal) => h('article', { class: 'card memory' },
    h('div', { class: 'row' }, icon('target', 15), h('strong', null, goal.title)),
    goal.detail ? h('p', { class: 'memory-content' }, goal.detail) : null,
    h('div', { class: 'row spread' },
      h('span', { class: 'muted small' }, '记于 ' + day(goal.created_at)),
      action('删除', async () => {
        if (!await ui.ask(`删除目标“${goal.title}”？`)) return;
        await del('/api/goals/' + encodeURIComponent(goal.id));
        await refresh();
      }, { class: 'btn small' }))))
    : empty('还没有长期目标', '告诉 Jurvis 你想长期跟踪或坚持的事，它会记在这里。'));
  fill(els.schedules, schedules.length ? schedules.map((item) => h('article', { class: 'card memory' },
    h('div', { class: 'row' }, icon('clock', 15), h('strong', null, item.title), h('span', { class: 'badge' }, item.when)),
    h('p', { class: 'memory-content' }, item.brief),
    h('div', { class: 'row spread' },
      h('span', { class: 'muted small' }, ['下次 ' + day(item.next_run), item.last_run ? '上次 ' + day(item.last_run) : ''].filter(Boolean).join(' · ')),
      action('取消', async () => {
        if (!await ui.ask(`取消定时任务“${item.title}”？之后不会再运行。`)) return;
        await del('/api/schedules/' + encodeURIComponent(item.id));
        await refresh();
      }, { class: 'btn small' }))))
    : empty('还没有定时任务', '让 Jurvis 按周或按月帮你做一件事，或者到点提醒你。'));
}
