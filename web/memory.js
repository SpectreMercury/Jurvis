// 记忆: what Jurvis remembers about the user. The list comes from /api/state; search is a client-side filter.
import { store, h, fill, post, put, del, run, action, notify, loadState, sheet, shortTime } from './core.js';
import * as ui from './ui.js';

const SOURCES = { manual: '手动', chat: '对话中提取' };
let sig = '';
const els = {};

export function mount(root) {
  els.search = h('input', { type: 'search', class: 'input', placeholder: '搜索记忆', 'aria-label': '搜索记忆', autocomplete: 'off' });
  els.search.addEventListener('input', render);
  els.count = h('span', { class: 'muted small' });
  els.list = h('div', { class: 'cards single' });
  root.append(h('div', { class: 'page' },
    h('header', { class: 'page-head' }, h('h1', null, '记忆'),
      h('div', { class: 'row' },
        h('button', { class: 'btn primary small', type: 'button', onclick: () => edit(null) }, '添加'),
        action('重建索引', async () => {
          const result = await post('/api/memories/reindex');
          notify(`已重新索引 ${result.indexed === undefined ? '' : result.indexed + ' 条'}记忆` + (result.embed_model ? `（${result.embed_model}）` : '（关键词匹配）'), 'info');
        }, { class: 'btn small' }),
        action('清空', async () => {
          const total = ((store.state && store.state.memories) || []).length;
          if (!total) { notify('没有可清空的记忆', 'info'); return; }
          if (!await ui.ask(`确定清空全部 ${total} 条记忆吗？这个操作不能撤销。`)) return;
          await del('/api/memories');
          await loadState();
        }, { class: 'btn danger small' }))),
    h('div', { class: 'row' }, els.search, els.count),
    els.list));
}

export function activate() { sig = ''; render(); }
export function onState() { render(); }

function render() {
  const all = (store.state && store.state.memories) || [];
  const q = els.search.value.trim().toLowerCase();
  const shown = q ? all.filter((m) => [m.title, m.content, ...(m.tags || [])].join('\n').toLowerCase().includes(q)) : all;
  const next = JSON.stringify([q, shown]);
  if (next === sig) return;
  sig = next;
  els.count.textContent = q ? `${shown.length} / ${all.length} 条` : `${all.length} 条`;
  if (!shown.length) {
    fill(els.list, h('div', { class: 'empty' },
      h('p', { class: 'empty-title' }, all.length ? '没有匹配的记忆' : '还没有记忆'),
      all.length ? null : h('p', { class: 'muted' }, '聊天时 Jurvis 会记住值得长期保留的信息，你也可以手动添加。')));
    return;
  }
  fill(els.list, ...shown.map((m) => h('article', { class: 'card memory' + (m.enabled ? '' : ' disabled') },
    h('div', { class: 'device-head' },
      h('strong', null, m.title || '（无标题）'),
      m.enabled ? null : h('span', { class: 'badge' }, '已停用')),
    h('p', { class: 'memory-content' }, m.content || ''),
    (m.tags || []).length ? h('div', { class: 'chips' }, m.tags.map((tag) => h('span', { class: 'chip static' }, String(tag)))) : null,
    h('div', { class: 'row spread' },
      h('span', { class: 'muted small' }, [
        `重要度 ${Math.round((Number(m.importance) || 0) * 100)}%`,
        m.durable ? '长期' : '短期',
        SOURCES[m.source] || (m.source ? String(m.source) : ''),
        m.updated_at ? shortTime(m.updated_at) : '',
      ].filter(Boolean).join(' · ')),
      h('div', { class: 'row' },
        h('button', { class: 'btn small', type: 'button', onclick: () => edit(m) }, '编辑'),
        action('删除', async () => {
          if (!await ui.ask(`删除记忆“${m.title}”？`)) return;
          await del('/api/memories/' + encodeURIComponent(m.id));
          await loadState();
        }, { class: 'btn small' }))))));
}

function edit(memory) {
  const title = h('input', { class: 'input', maxLength: 100, placeholder: '一句话标题', value: memory ? memory.title : '' });
  const content = h('textarea', { class: 'input', rows: 6, maxLength: 4000, placeholder: '要记住的内容' });
  content.value = memory ? memory.content : '';
  const tags = h('input', { class: 'input', placeholder: '用逗号分隔，最多 8 个', value: memory ? (memory.tags || []).join('，') : '' });
  const importance = ui.slider({ min: '0', max: '1', step: '0.05', value: memory ? Number(memory.importance) || 0 : 0.7, 'aria-label': '重要度' });
  const shown = h('span', { class: 'muted small slider-value' });
  const show = () => { shown.textContent = Math.round(Number(importance.value) * 100) + '%'; };
  importance.addEventListener('input', show);
  show();
  const enabled = ui.toggle({ checked: memory ? !!memory.enabled : true, 'aria-label': '启用' });
  const durable = ui.toggle({ checked: memory ? !!memory.durable : true, 'aria-label': '长期保留' });
  const save = h('button', { class: 'btn primary', type: 'submit' }, '保存');
  const form = h('form', { class: 'stack' },
    h('label', { class: 'field' }, h('span', null, '标题'), title),
    h('label', { class: 'field' }, h('span', null, '内容'), content),
    h('label', { class: 'field' }, h('span', null, '标签'), tags),
    h('div', { class: 'control-row' }, h('span', { class: 'control-label' }, '重要度'), importance, shown),
    h('label', { class: 'control-row' }, h('span', { class: 'control-label grow' }, '启用'), enabled),
    h('label', { class: 'control-row' }, h('span', { class: 'control-label grow' }, '长期保留'), durable),
    h('div', { class: 'row end' }, save));
  const close = sheet(memory ? '编辑记忆' : '添加记忆', form);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const body = {
      title: title.value.trim(), content: content.value.trim(), enabled: enabled.checked, durable: durable.checked,
      importance: Number(importance.value),
      tags: [...new Set(tags.value.split(/[,，、\n]/).map((t) => t.trim()).filter(Boolean))],
    };
    if (!body.title || !body.content) { notify('请填写标题和内容'); return; }
    if (body.tags.length > 8) { notify('标签最多 8 个'); return; }
    const ok = await run(save, async () => {
      if (memory) await put('/api/memories/' + encodeURIComponent(memory.id), body);
      else await post('/api/memories', body);
      await loadState();
    });
    if (ok) close();
  });
}
