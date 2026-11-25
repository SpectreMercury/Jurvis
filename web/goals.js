// 目标: what Jurvis keeps doing for the user over time. Long-term goals it tracks and work it runs on a schedule.
// Both are set up in conversation; here they are listed and can be removed.
import { store, h, fill, get, del, action, sheet, emit, shortTime } from './core.js';
import * as ui from './ui.js';
import { icon } from './icons.js';

let sig = null;
let loading = null;
const els = {};

export function mount(root) {
  els.goals = h('div', { class: 'cards single' });
  els.schedules = h('div', { class: 'cards single' });
  els.watches = h('div', { class: 'cards single' });
  root.append(h('div', { class: 'page' },
    h('header', { class: 'page-head' }, h('h1', null, '目标')),
    h('h2', { class: 'group-title' }, '长期目标'), els.goals,
    h('h2', { class: 'group-title' }, '定时任务'), els.schedules,
    h('h2', { class: 'group-title' }, '事件触发'), els.watches));
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
      h('div', { class: 'row' },
        action('打开', () => openSpace(goal), { class: 'btn small' }),
        action('删除', async () => {
          if (!await ui.ask(`删除目标“${goal.title}”？`)) return;
          await del('/api/goals/' + encodeURIComponent(goal.id));
          await refresh();
        }, { class: 'btn small' })))))
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
  const watches = agenda.watches || [];
  fill(els.watches, watches.length ? watches.map((item) => h('article', { class: 'card memory' },
    h('div', { class: 'row' }, icon('spark', 15), h('strong', null, item.title), h('span', { class: 'badge' }, item.when)),
    h('p', { class: 'memory-content' }, item.brief),
    h('div', { class: 'row spread' },
      h('span', { class: 'muted small' }, item.last_fired ? `触发过 ${item.fired} 次 · 上次 ${day(item.last_fired)}` : '还没有触发过'),
      action('取消', async () => {
        if (!await ui.ask(`取消“${item.title}”？之后这件事发生时不会再处理。`)) return;
        await del('/api/watches/' + encodeURIComponent(item.id));
        await refresh();
      }, { class: 'btn small' }))))
    : empty('还没有事件触发', '告诉 Jurvis“某人来邮件时”“开会前半小时”“餐饮预算快用完时”“门打开时”要做什么。'));
}

/** A goal opened as its space: what Jurvis knows about it, how often it looks at it, and where the two of you talked about it. */
async function openSpace(goal) {
  const space = await get('/api/spaces/' + encodeURIComponent(goal.id));
  const part = (title, ...kids) => h('section', { class: 'space-part' }, h('h3', { class: 'group-title' }, title), ...kids);
  let close = () => {};
  const body = h('div', { class: 'space' },
    part('知道的', space.facts.length
      ? h('ul', { class: 'space-facts' }, space.facts.map((fact) => h('li', null, fact.content)))
      : h('p', { class: 'muted small' }, '还没有记下什么。聊到这件事时，Jurvis 会把了解到的放在这里。')),
    part('节奏', space.schedules.length
      ? h('div', { class: 'cards single' }, space.schedules.map((item) => h('article', { class: 'card memory' },
        h('div', { class: 'row' }, icon('clock', 15), h('strong', null, item.title), h('span', { class: 'badge' }, item.when)),
        h('p', { class: 'muted small' }, '下次 ' + day(item.next_run)))))
      : h('p', { class: 'muted small' }, '没有定时。可以在对话里说“每周给我一份简报”。')),
    part('聊到过', space.stretches.length
      ? h('div', { class: 'session-list' }, space.stretches.map((stretch) => h('div', { class: 'session-row' },
        h('button', { class: 'session-open hit', type: 'button', onclick: () => { close(); emit('open-stretch', stretch.id); } },
          h('span', { class: 'session-text' }, h('span', { class: 'session-title' }, stretch.title || '对话'),
            h('span', { class: 'session-sub' }, shortTime(stretch.updated_at) + (stretch.text ? ' · ' + stretch.text.replace(/\s+/g, ' ').slice(0, 60) : '')))))))
      : h('p', { class: 'muted small' }, '还没有相关的对话。')),
    space.file ? h('p', { class: 'muted small space-file' }, '笔记文件：' + space.file) : null);
  close = sheet(goal.title, body, { wide: true });
}
