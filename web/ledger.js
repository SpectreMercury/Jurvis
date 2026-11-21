// 账本: what the user spent. Totals by category, then every payment. Payments are logged in conversation ("午饭 35")
// or imported from an Alipay / WeChat Pay bill. This page reads them straight from the Mac: no model sees the list.
import { store, h, fill, get, del, patch, api, action, notify, ApiError } from './core.js';
import * as ui from './ui.js';
import { icon } from './icons.js';

const CATEGORIES = ['餐饮', '交通', '购物', '日用', '住房', '娱乐', '医疗', '教育', '通讯', '旅行', '订阅', '转账', '其他'];
const PERIODS = [['this_month', '本月'], ['last_month', '上月'], ['this_year', '今年']];
let period = 'this_month';
let sig = null;
let loading = null;
const els = {};

const money = (cents) => '¥' + (Math.abs(cents) / 100).toLocaleString('zh-CN', { minimumFractionDigits: cents % 100 ? 2 : 0, maximumFractionDigits: 2 });
const when = (seconds) => new Date(seconds * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });

export function mount(root) {
  els.periods = h('div', { class: 'row', role: 'group', 'aria-label': '时间范围' });
  els.file = h('input', { type: 'file', accept: '.csv,text/csv', hidden: true });
  els.file.addEventListener('change', upload);
  els.summary = h('div', { class: 'card ledger-summary' });
  els.list = h('div', { class: 'ledger-list' });
  root.append(h('div', { class: 'page' },
    h('header', { class: 'page-head' }, h('h1', null, '账本'),
      h('div', { class: 'row' }, els.periods,
        h('button', { class: 'btn primary small', type: 'button', onclick: () => els.file.click() }, '导入账单'), els.file)),
    h('p', { class: 'muted small' }, '在对话里说“午饭 35”就能记一笔，也可以导入支付宝或微信支付导出的 CSV 账单。每一笔明细只存在这台 Mac 上。'),
    els.summary, els.list));
}

export function activate() { sig = null; refresh(); }

/** Reloads the page; called when the tab opens, the period changes, and the engine reports a ledger change. */
export function refresh() {
  if (store.tab !== 'ledger' || loading) return loading;
  loading = get('/api/ledger?period=' + encodeURIComponent(period)).then(render, () => {}).finally(() => { loading = null; });
  return loading;
}

async function upload() {
  const file = els.file.files[0];
  els.file.value = '';
  if (!file) return;
  const form = new FormData();
  form.append('file', file);
  try {
    const result = await api('POST', '/api/ledger/import', form);
    notify(`已从${result.source}账单记入 ${result.added} 笔` + (result.already_there ? `，${result.already_there} 笔之前已有` : '')
      + (result.uncategorized ? `。${result.uncategorized} 个商户还没分类，本机模型正在后台整理` : ''), 'info');
    sig = null;
    await refresh();
  } catch (err) {
    notify(err instanceof ApiError ? err.message : '导入失败');
  }
}

function render(page) {
  const next = JSON.stringify([period, page]);
  if (next === sig) return;
  sig = next;
  fill(els.periods, PERIODS.map(([id, label]) => h('button', { class: 'chip toggle', type: 'button', 'aria-pressed': String(period === id),
    onclick: () => { period = id; sig = null; refresh(); } }, label)));
  const s = page.summary;
  const facts = [`${s.payments} 笔`, `日均 ¥${s.per_day.toLocaleString('zh-CN')}`];
  if (s.change_percent !== null && s.change_percent !== undefined) facts.push(`较上期 ${s.change_percent > 0 ? '+' : ''}${s.change_percent}%`);
  if (s.income) facts.push(`收入 ¥${s.income.toLocaleString('zh-CN')}`);
  if (s.budget) facts.push(`预算 ¥${s.budget.limit.toLocaleString('zh-CN')} · 已用 ${s.budget.used_percent}%`);
  fill(els.summary,
    h('div', { class: 'muted small' }, `${s.period}支出 · ${s.from} 至 ${s.to}`),
    h('div', { class: 'info-value' }, '¥' + s.spent.toLocaleString('zh-CN', { maximumFractionDigits: 2 })),
    h('div', { class: 'muted small' }, facts.join(' · ')),
    s.by_category.length ? bars(s.by_category.map((g) => ({ label: g.category, value: '¥' + g.spent.toLocaleString('zh-CN'), share: g.share_percent / 100 }))) : null);
  const rows = page.transactions || [];
  if (!rows.length) {
    fill(els.list, h('div', { class: 'empty' }, h('p', { class: 'empty-title' }, '这段时间还没有记录'),
      h('p', { class: 'muted' }, '对 Jurvis 说一句“打车 48”，或者导入一份账单。')));
    return;
  }
  fill(els.list, rows.map((t) => {
    const pick = h('select', { class: 'input small-select', 'aria-label': '分类' }, CATEGORIES.map((c) => h('option', { value: c, selected: c === t.category }, c)));
    pick.addEventListener('change', async () => { await patch('/api/ledger/transactions/' + encodeURIComponent(t.id), { category: pick.value }); sig = null; refresh(); });
    return h('div', { class: 'ledger-row' },
      h('div', { class: 'made-text' }, h('strong', null, t.merchant || t.note || t.category), h('span', { class: 'muted small' }, [when(t.at), t.merchant ? t.note : ''].filter(Boolean).join(' · '))),
      pick,
      h('span', { class: 'ledger-amount' + (t.direction === 'income' ? ' income' : '') }, (t.direction === 'income' ? '+' : '') + money(t.amount_cents)),
      action('', async () => {
        if (!await ui.ask(`删除这笔 ${money(t.amount_cents)} 的记录？`)) return;
        await del('/api/ledger/transactions/' + encodeURIComponent(t.id));
        sig = null;
        await refresh();
      }, { class: 'icon-btn small', 'aria-label': '删除', title: '删除' }));
  }));
  for (const button of els.list.querySelectorAll('.icon-btn')) fill(button, icon('trash', 15));
}

/** Shares of a total as labelled bars; also used by the ledger card in a conversation. */
export function bars(items) {
  return h('div', { class: 'bars' }, items.map((item) => h('div', { class: 'bar-row' },
    h('span', { class: 'bar-label' }, item.label),
    h('span', { class: 'bar-track' }, h('span', { class: 'bar-fill', style: `width:${Math.max(2, Math.min(100, (Number(item.share) || 0) * 100)).toFixed(1)}%` })),
    h('span', { class: 'bar-value' }, item.value))));
}
