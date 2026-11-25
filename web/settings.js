// 设置: models and providers, memory, skills, MCP servers, smart-home connectors, this device.
import {
  store, h, fill, get, put, patch, del, post, run, action, notify, loadSettings, forgetModels, pickModel, modelLabel, sheet, signOut, shortTime, ApiError,
} from './core.js';
import * as ui from './ui.js';
import * as connect from './connect.js';
import * as voiceKit from './voice.js';
import { connectorRows } from './health.js';
import { appSetup } from './oauth.js';
import { brandMark } from './connect.js';
import { icon } from './icons.js';
import * as reach from './reach.js';

const SKILL_TEMPLATE = `---
name: 我的技能
description: 一句话说明什么时候应该使用这个技能
---

# 做法

1. 第一步……
2. 第二步……
`;

const data = { health: null, skills: null, mcp: null, home: null, devices: null, mail: null, care: null, phone: null };
const fail = {};
const els = {};
let root = null;

function section(title, body, hint) {
  return h('section', { class: 'settings-section' }, h('h2', null, title), hint ? h('p', { class: 'muted small' }, hint) : null, body);
}

export function mount(container) {
  root = container;
  connect.onChange((next) => { if (next) { data.home = next; renderHome(); } });
  for (const name of ['models', 'voice', 'memory', 'health', 'mail', 'care', 'phone', 'skills', 'mcp', 'home', 'devices']) els[name] = h('div', { class: 'stack' }, h('p', { class: 'muted' }, '正在读取…'));
  els.about = h('p', { class: 'muted small' });
  container.append(h('div', { class: 'page' },
    h('header', { class: 'page-head' }, h('h1', null, '设置')),
    section('模型', els.models),
    section('语音', els.voice, '语音识别和朗读都在这台 Mac 上完成，录音不会发到别处。'),
    section('记忆', els.memory),
    section('健康与运动', els.health, '睡眠、运动、心率和经期只存在这台 Mac 上；模型只看到汇总数字。'),
    section('邮件', els.mail, '默认只读：Jurvis 只看，不标已读。Gmail 和 Outlook 用账号登录授权；默认只有本机模型能读。给某个邮箱打开「可以发信」之后，它才能替你发邮件，而且每一封都会先把全文给你看，你点了允许才发出去。'),
    section('主动关心', els.care, '睡眠、压力、恢复或心情一变差，Jurvis 就会用本机模型看看最近的邮件和对话，马上主动问候你，并发通知。同一件事不重复说，安静时段先攒着，到点再发。'),
    section('电话', els.phone, 'Jurvis 可以替你给餐厅等商家打电话订位、询问。用的是你自己的电话服务商账户：对方看到的是你的号码，话费记在你的账上。每一通都会先问你。'),
    ...reach.sections().map(([title, el, hint]) => section(title, el, hint)),
    section('技能', els.skills, '技能是一份写给 Jurvis 的做法说明（SKILL.md），它会在合适的时候照着做。'),
    section('MCP 服务', els.mcp, '通过 MCP 给 Jurvis 接入外部工具。'),
    section('智能家居', els.home),
    section('设备', els.devices),
    els.about));
}

export function reset() {
  for (const key of Object.keys(data)) data[key] = null;
  reach.reset();
}

const quiet = (err) => err instanceof ApiError && err.status === 401;

function loadPart(name, path, pick, render) {
  return get(path).then((result) => {
    data[name] = pick(result);
    delete fail[name];
  }).catch((err) => { if (!quiet(err)) fail[name] = err.message; }).then(render);
}

export function activate() {
  loadSettings().then(() => { delete fail.settings; }).catch((err) => { if (!quiet(err)) fail.settings = err.message; }).then(() => { renderModels(); renderMemory(); renderHome(); });
  voiceKit.status(true).then((result) => { data.voice = result; renderVoice(); });
  loadPart('skills', '/api/skills', (r) => r, renderSkills);
  loadPart('health', '/api/health/connectors', (r) => r.connectors, renderHealth);
  loadPart('mail', '/api/mail', (r) => r, renderMail);
  loadPart('care', '/api/checkin', (r) => r, renderCare);
  loadPart('phone', '/api/phone', (r) => r, renderPhone);
  loadPart('mcp', '/api/mcp', (r) => r.servers || [], renderMcp);
  loadPart('home', '/api/home', (r) => r, renderHome);
  loadPart('devices', '/api/devices', (r) => r, renderDevices);
  reach.activate();
  renderAbout();
}

/** A settings change arrived from another client: refresh, unless the user is typing here. */
export function onSettingsEvent() {
  const active = document.activeElement;
  if (root && active && root.contains(active) && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return;
  loadSettings().then(() => { renderModels(); renderMemory(); renderHome(); reach.onSettings(); }).catch(() => {});
}

export function onState() { renderAbout(); }

function renderAbout() {
  const s = store.state;
  if (!s) return;
  const rt = s.runtime || {};
  els.about.textContent = `Jurvis ${s.version || ''}` + (rt.memory_total_gib ? ` · Mac 内存 ${rt.memory_used_gib} / ${rt.memory_total_gib} GB` : '') + (rt.active ? ` · 正在运行：${rt.active}` : '');
}

function failed(el, key) {
  if (!fail[key]) return false;
  fill(el, h('p', { class: 'error-text' }, fail[key]), h('button', { class: 'btn small', type: 'button', onclick: activate }, '重试'));
  return true;
}

function toggle(label, checked, onChange, hint, disabled) {
  const box = ui.toggle({ checked: !!checked, 'aria-label': label, ...(disabled ? { disabled: '' } : {}) });
  box.addEventListener('change', async () => {
    const want = box.checked;
    const ok = await run(box, () => onChange(want));
    if (!ok) box.checked = !want;
  });
  return h('label', { class: 'control-row' }, h('span', { class: 'control-label grow' }, label, hint ? h('span', { class: 'muted small block' }, hint) : null), box);
}

async function saveSettings(changes) {
  store.settings = await patch('/api/settings', changes);
}

// ---------------------------------------------------------------- 模型
function modelRow(label, key, { allowDefault, defaultLabel, hint }) {
  const current = store.settings.settings[key] || '';
  const text = current ? (current === 'local' ? '本机' : current) : defaultLabel;
  return h('div', { class: 'control-row' },
    h('span', { class: 'control-label grow' }, label, hint ? h('span', { class: 'muted small block' }, hint) : null),
    h('button', { class: 'btn small value-btn', type: 'button', onclick: () => pickModel({
      title: label, current, allowDefault, defaultLabel,
      onPick: async (id) => { await saveSettings({ [key]: id }); renderModels(); },
    }) }, text));
}

function renderModels() {
  const el = els.models;
  if (failed(el, 'settings') || !store.settings) return;
  const s = store.settings.settings;
  const embed = h('input', { class: 'input', placeholder: '例如 miavo:text-embedding-3-small', value: s.embed_model || '', autocomplete: 'off', autocapitalize: 'off', spellcheck: false, 'aria-label': '向量模型' });
  const out = [
    modelRow('默认对话模型', 'chat_model', { allowDefault: false, defaultLabel: '本机', hint: '对话没有单独选择模型时使用' }),
    modelRow('工具模型', 'tool_model', { allowDefault: true, defaultLabel: '跟随对话模型', hint: '用于总结、提取记忆等小任务' }),
    h('div', { class: 'field' }, h('span', null, '向量模型'),
      h('div', { class: 'row' }, embed, action('保存', async () => {
        await saveSettings({ embed_model: embed.value.trim() });
        notify('已保存向量模型。可以到“记忆”里重建索引。', 'info');
      }, { class: 'btn small' })),
      h('span', { class: 'muted small' }, '写成 服务名:模型名；留空则使用内置的关键词匹配。')),
    toggle('联网搜索', s.web_search, (on) => saveSettings({ web_search: on }), '允许 Jurvis 在回答时搜索网页'),
    h('h3', null, '模型服务'),
  ];
  for (const p of store.settings.providers || []) out.push(providerCard(p));
  out.push(h('div', { class: 'row' }, h('button', { class: 'btn small', type: 'button', onclick: addProvider }, '添加 OpenAI 兼容服务')));
  fill(el, ...out);
}

async function saveProvider(name, body) {
  store.settings = await put('/api/providers/' + encodeURIComponent(name), body);
  forgetModels();
}

function providerCard(p) {
  const key = h('input', { class: 'input', type: 'password', autocomplete: 'off', placeholder: p.has_key ? '已配置；填写新的 Key 可替换' : '填写 API Key', 'aria-label': `${p.title} API Key` });
  return h('article', { class: 'card' },
    h('div', { class: 'device-head' },
      h('strong', null, p.title || p.id),
      h('span', { class: 'badge' + (p.has_key ? ' ok' : '') }, p.has_key ? '已配置' : p.builtin ? '未配置' : '未填 Key')),
    h('div', { class: 'muted small break' }, p.id + (p.base_url ? ' · ' + p.base_url : '')),
    h('div', { class: 'row' }, key, action('保存', async () => {
      const value = key.value.trim();
      if (!value) { notify('请先填写 API Key'); return; }
      await saveProvider(p.id, { api_key: value });
      renderModels();
    }, { class: 'btn small' })),
    toggle('这个服务支持联网搜索', p.web_search, async (on) => { await saveProvider(p.id, { web_search: on }); }),
    h('div', { class: 'row end' },
      (p.builtin && !p.has_key) ? null : action(p.builtin ? '清除 Key' : '移除', async () => {
        if (!await ui.ask(p.builtin ? `清除 ${p.title} 的 API Key？` : `移除模型服务“${p.title}”？使用它的默认模型会改回本机。`)) return;
        store.settings = await del('/api/providers/' + encodeURIComponent(p.id));
        forgetModels();
        renderModels();
      }, { class: 'btn small' })));
}

function addProvider() {
  const name = h('input', { class: 'input', placeholder: '例如 openrouter', autocapitalize: 'off', autocomplete: 'off', spellcheck: false, maxLength: 32 });
  const title = h('input', { class: 'input', placeholder: '显示名称（可选）', maxLength: 60 });
  const base = h('input', { class: 'input', placeholder: 'https://example.com/v1', inputMode: 'url', autocapitalize: 'off', autocomplete: 'off', spellcheck: false });
  const key = h('input', { class: 'input', type: 'password', placeholder: 'API Key（没有可留空）', autocomplete: 'off' });
  const save = h('button', { class: 'btn primary', type: 'submit' }, '添加');
  const form = h('form', { class: 'stack' },
    h('label', { class: 'field' }, h('span', null, '服务名'), name, h('span', { class: 'muted small' }, '小写字母、数字、- 和 _；模型会写成 服务名:模型名')),
    h('label', { class: 'field' }, h('span', null, '名称'), title),
    h('label', { class: 'field' }, h('span', null, '接口地址'), base),
    h('label', { class: 'field' }, h('span', null, 'API Key'), key),
    h('div', { class: 'row end' }, save));
  const close = sheet('添加模型服务', form);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = name.value.trim();
    if (!/^[a-z0-9_-]+$/.test(id)) { notify('服务名只能用小写字母、数字、- 和 _'); return; }
    const body = { base_url: base.value.trim() };
    if (title.value.trim()) body.title = title.value.trim();
    if (key.value.trim()) body.api_key = key.value.trim();
    const ok = await run(save, () => saveProvider(id, body));
    if (ok) { close(); renderModels(); }
  });
}

// ---------------------------------------------------------------- 记忆
const VOICE_NAMES = { vivian: 'Vivian · 女声', serena: 'Serena · 女声', ono_anna: 'Anna · 女声（日语）', sohee: 'Sohee · 女声（韩语）', uncle_fu: '傅叔 · 男声', dylan: 'Dylan · 男声', eric: 'Eric · 男声', ryan: 'Ryan · 男声（英语）', aiden: 'Aiden · 男声（英语）' };

function renderVoice() {
  const el = els.voice;
  const v = data.voice;
  if (!v) return;
  const line = (label, ok, name) => h('div', { class: 'control-row' }, h('span', { class: 'control-label grow' }, label),
    h('span', { class: 'badge' + (ok ? ' ok' : '') }, ok ? name : '未安装'));
  const out = [line('语音识别', v.stt, v.stt_model), line('语音合成', v.tts, v.tts_model)];
  if (!v.stt || !v.tts) out.push(h('p', { class: 'muted small' }, '模型放在 Mac 上 Jurvis 目录的 models/speech/ 里，安装方法见 docs/MODELS.md。'));
  if (v.tts && v.speakers.length) {
    const current = (store.settings && store.settings.settings.voice.speaker) || (v.speakers.includes('vivian') ? 'vivian' : v.speakers[0]);   // the engine's default
    const pick = ui.select({ 'aria-label': '朗读的声音', value: current, onchange: async () => {
      await saveSettings({ voice: { speaker: pick.value } });
    } }, v.speakers.map((id) => ({ value: id, label: VOICE_NAMES[id] || id })));
    const sample = action('试听', async () => { voiceKit.unlock(); await voiceKit.speak('你好，我是 Jurvis。Hello, this is Jurvis.', 'sample'); }, { class: 'btn small' });
    out.push(h('div', { class: 'field' }, h('span', null, '朗读的声音'), h('div', { class: 'row' }, pick, sample)));
  }
  fill(el, ...out);
}

function renderMemory() {
  const el = els.memory;
  if (failed(el, 'settings') || !store.settings) return;
  const m = store.settings.settings.memory || {};
  const set = (key) => (on) => saveSettings({ memory: { [key]: on } });
  const max = ui.number({ min: '1', max: '50', step: '1', value: String(m.max_retrieved === undefined ? 8 : m.max_retrieved), 'aria-label': '每次最多取回' });
  const threshold = ui.number({ min: '0', max: '1', step: '0.05', value: String(m.threshold === undefined ? 0.3 : m.threshold), 'aria-label': '相关度阈值' });
  // How context is kept short. The two switches rewrite the record with an extra model call, so the engine only takes them with a cloud model.
  const c = store.settings.settings.context || {};
  const local = (store.settings.settings.chat_model || 'local') === 'local';
  const keep = (key) => (on) => saveSettings({ context: { [key]: on } });
  const history = ui.select({ value: String(c.cloud_history || 4), 'aria-label': '在线模型带上的历史' },
    [['4', '4 倍（默认）'], ['3', '3 倍'], ['2', '2 倍'], ['1', '和本机模型一样']].map(([value, label]) => ({ value, label })));
  history.addEventListener('change', () => run(history, () => saveSettings({ context: { cloud_history: Number(history.value) } })));
  const cloudOnly = local ? '默认对话模型是在线模型时才能开启' : null;
  fill(el, 
    toggle('启用记忆', m.enabled, set('enabled')),
    toggle('自动提取', m.auto_extract, set('auto_extract'), '从对话里记下值得长期保留的信息'),
    toggle('自动取回', m.auto_retrieve, set('auto_retrieve'), '回答前先找出相关的记忆'),
    toggle('私密的记忆只给本机模型', m.private_local !== false, set('private_local'),
      '关于健康、收入负债、证件、住址和家庭矛盾的记忆会标为「私密」，连同陪伴对话的内容，都不发给云端模型。可以在「记忆」页逐条改标记'),
    h('div', { class: 'control-row' }, h('span', { class: 'control-label grow' }, '每次最多取回（条）'), max),
    h('div', { class: 'control-row' }, h('span', { class: 'control-label grow' }, '相关度阈值（0–1）'), threshold),
    h('div', { class: 'control-row' }, h('span', { class: 'control-label grow' }, '在线模型带上的历史',
      h('span', { class: 'muted small block' }, '每次回复带上多少之前的原话，按本机模型的量算。带得少更省更快，更早的内容靠摘要和翻对话记录找回')), history),
    toggle('每步压缩', c.step_compaction && !local, keep('step_compaction'),
      cloudOnly || '目标和后台任务里，每做一步就把经过整理成简报再继续。上下文很短，但每步多一次模型调用', local),
    toggle('摘要写成简报', c.brief_summary && !local, keep('brief_summary'),
      cloudOnly || '对话太长时，较早的内容不写成完整摘要，只留还没办完的事和去哪找原文', local),
    h('div', { class: 'row end' }, action('保存', async () => {
      const count = Math.round(Number(max.value));
      const level = Number(threshold.value);
      if (!Number.isFinite(count) || count < 1) throw new ApiError('取回条数至少为 1', 400);
      if (!Number.isFinite(level) || level < 0 || level > 1) throw new ApiError('阈值需在 0 到 1 之间', 400);
      await saveSettings({ memory: { max_retrieved: count, threshold: level } });
      notify('已保存', 'info');
    }, { class: 'btn small' })));
}

// ---------------------------------------------------------------- 技能
function renderSkills() {
  const el = els.skills;
  if (failed(el, 'skills') || !data.skills) return;
  const list = data.skills.skills || [];
  fill(el, 
    ...list.map((s) => h('article', { class: 'card' },
      h('div', { class: 'device-head' }, h('strong', null, s.title || s.name),
        s.source === 'shared' ? h('span', { class: 'badge' }, 'Claude Code 共用') : s.source === 'project' ? h('span', { class: 'badge' }, '项目内') : null,
        s.enabled ? null : h('span', { class: 'badge' }, '已停用')),
      h('div', { class: 'muted small' }, s.name),
      s.description ? h('p', { class: 'memory-content' }, s.description) : null,
      // Skills Jurvis does not own are switched on and off here; their files are left alone.
      s.source && s.source !== 'personal' ? h('div', { class: 'row end' },
        action(s.enabled ? '停用' : '启用', async () => {
          await post('/api/skills/' + encodeURIComponent(s.name) + '/enabled', { enabled: !s.enabled });
          await loadPart('skills', '/api/skills', (r) => r, renderSkills);
        }, { class: 'btn small' })) : h('div', { class: 'row end' },
        action('编辑', async () => {
          const found = await get('/api/skills/' + encodeURIComponent(s.name));
          editSkill(s.name, found.content || '');
        }, { class: 'btn small' }),
        action('删除', async () => {
          if (!await ui.ask(`删除技能“${s.title || s.name}”？`)) return;
          await del('/api/skills/' + encodeURIComponent(s.name));
          await loadPart('skills', '/api/skills', (r) => r, renderSkills);
        }, { class: 'btn small' })))),
    list.length ? null : h('p', { class: 'muted' }, '还没有技能。'),
    h('div', { class: 'row' }, h('button', { class: 'btn small', type: 'button', onclick: () => editSkill(null, SKILL_TEMPLATE) }, '添加技能')),
    data.skills.folder ? h('p', { class: 'muted small break' }, '保存在 Mac 上：' + data.skills.folder) : null);
}

function editSkill(existing, text) {
  const name = h('input', { class: 'input', placeholder: '例如 weekly-report', autocapitalize: 'off', autocomplete: 'off', spellcheck: false, maxLength: 64, value: existing || '', disabled: !!existing });
  const content = h('textarea', { class: 'input mono tall', rows: 14, spellcheck: false, autocapitalize: 'off' });
  content.value = text;
  const save = h('button', { class: 'btn primary', type: 'submit' }, '保存');
  const form = h('form', { class: 'stack' },
    h('label', { class: 'field' }, h('span', null, '文件夹名'), name, existing ? null : h('span', { class: 'muted small' }, '字母、数字、- 和 _')),
    h('label', { class: 'field' }, h('span', null, 'SKILL.md'), content, h('span', { class: 'muted small' }, '开头的 name 和 description 必须保留；description 用一句话说明什么时候用它。')),
    h('div', { class: 'row end' }, save));
  const close = sheet(existing ? '编辑技能' : '添加技能', form, { wide: true });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = (existing || name.value).trim();
    if (!id) { notify('请填写文件夹名'); return; }
    if (!content.value.trim()) { notify('技能内容是空的'); return; }
    const ok = await run(save, async () => {
      await put('/api/skills/' + encodeURIComponent(id), { content: content.value });
      await loadPart('skills', '/api/skills', (r) => r, renderSkills);
    });
    if (ok) close();
  });
}

// ---------------------------------------------------------------- MCP
function renderMcp() {
  const el = els.mcp;
  if (failed(el, 'mcp') || !data.mcp) return;
  const apply = (result) => { data.mcp = result.servers || []; renderMcp(); };
  fill(el, 
    ...data.mcp.map((s) => h('article', { class: 'card' },
      h('div', { class: 'device-head' },
        h('strong', null, s.name),
        h('span', { class: 'badge' + (s.connected ? ' ok' : s.enabled ? ' warn' : '') }, !s.enabled ? '已停用' : s.connected ? '已连接' : '未连接')),
      h('div', { class: 'muted small break' }, (s.transport === 'stdio' ? '命令 · ' : '地址 · ') + (s.target || '')),
      s.error ? h('div', { class: 'error-text small break' }, String(s.error)) : null,
      (s.tools || []).length ? ui.disclosure('small', `${s.tools.length} 个工具`,
        h('ul', { class: 'plain-list' }, s.tools.map((t) => h('li', null, h('code', null, t.name), t.description ? ' — ' + t.description : '')))) : null,
      h('div', { class: 'row spread' },
        h('span', { class: 'muted small' }, s.trusted ? '已信任：调用工具不再询问' : '未信任：调用工具前先问你'),
        h('div', { class: 'row' },
          s.transport === 'http' ? h('button', { class: 'btn small', type: 'button', onclick: () => editMcp(s, apply) }, '编辑') : h('span', { class: 'muted small' }, '在 Mac 上修改'),
          action('移除', async () => {
            if (!await ui.ask(`移除 MCP 服务“${s.name}”？`)) return;
            apply(await del('/api/mcp/' + encodeURIComponent(s.name)));
          }, { class: 'btn small' }))))),
    data.mcp.length ? null : h('p', { class: 'muted' }, '还没有 MCP 服务。'),
    h('div', { class: 'row' },
      h('button', { class: 'btn small', type: 'button', onclick: () => editMcp(null, apply) }, '添加网址服务'),
      action('重新连接', async () => apply(await post('/api/mcp/reconnect')), { class: 'btn small' })),
    h('p', { class: 'muted small' }, '这里只能添加通过网址（HTTP）连接的服务；需要在 Mac 上运行命令的服务，请在 Mac 的 Jurvis 里添加。'));
}

function editMcp(server, apply) {
  const name = h('input', { class: 'input', placeholder: '例如 notes', autocapitalize: 'off', autocomplete: 'off', spellcheck: false, maxLength: 32, value: server ? server.name : '', disabled: !!server });
  const url = h('input', { class: 'input', placeholder: 'https://example.com/mcp', inputMode: 'url', autocapitalize: 'off', autocomplete: 'off', spellcheck: false, value: server ? server.target || '' : '' });
  const headers = h('textarea', { class: 'input mono', rows: 3, spellcheck: false, autocapitalize: 'off', placeholder: 'Authorization: Bearer …' });
  const enabled = ui.toggle({ checked: server ? !!server.enabled : true, 'aria-label': '启用' });
  const trusted = ui.toggle({ checked: server ? !!server.trusted : false, 'aria-label': '信任' });
  const save = h('button', { class: 'btn primary', type: 'submit' }, '保存');
  const form = h('form', { class: 'stack' },
    h('label', { class: 'field' }, h('span', null, '服务名'), name, server ? null : h('span', { class: 'muted small' }, '字母、数字、- 和 _')),
    h('label', { class: 'field' }, h('span', null, '网址'), url),
    h('label', { class: 'field' }, h('span', null, '请求头（可选，每行一个 名称: 值）'), headers,
      server ? h('span', { class: 'muted small' }, '已保存的请求头不会显示在这里。保存会替换它们：如果这个服务需要请求头，请重新填写。') : null),
    h('label', { class: 'control-row' }, h('span', { class: 'control-label grow' }, '启用'), enabled),
    h('label', { class: 'control-row' }, h('span', { class: 'control-label grow' }, '信任', h('span', { class: 'muted small block' }, '信任后，调用它的工具不再逐次询问')), trusted),
    h('div', { class: 'row end' }, save));
  const close = sheet(server ? '编辑 MCP 服务' : '添加 MCP 服务', form);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = name.value.trim();
    if (!/^[A-Za-z0-9_-]+$/.test(id)) { notify('服务名只能用字母、数字、- 和 _'); return; }
    if (!/^https?:\/\//i.test(url.value.trim())) { notify('网址需以 http:// 或 https:// 开头'); return; }
    const map = {};
    for (const line of headers.value.split('\n')) {
      if (!line.trim()) continue;
      const at = line.indexOf(':');
      if (at < 1) { notify('请求头每行写成 名称: 值'); return; }
      map[line.slice(0, at).trim()] = line.slice(at + 1).trim();
    }
    const ok = await run(save, async () => {
      apply(await put('/api/mcp/' + encodeURIComponent(id), { url: url.value.trim(), headers: map, enabled: enabled.checked, trusted: trusted.checked }));
    });
    if (ok) close();
  });
}

// ---------------------------------------------------------------- 邮件
function renderHealth() {
  const el = els.health;
  if (failed(el, 'health') || !data.health) return;
  fill(el, h('div', { class: 'health-accounts' }, connectorRows(data.health, () => loadPart('health', '/api/health/connectors', (r) => r.connectors, renderHealth))));
}

function renderMail() {
  const el = els.mail;
  if (failed(el, 'mail') || !data.mail) return;
  const apply = (result) => { data.mail.accounts = result.accounts; renderMail(); };
  const accounts = data.mail.accounts || [];
  fill(el,
    accounts.length ? h('div', { class: 'health-accounts' }, accounts.map((a) => h('div', { class: 'health-account' },
      brandMark(a.name, '', a.kind),
      h('div', { class: 'made-text grow' },
        h('strong', null, a.address),
        h('span', { class: (a.error ? 'error-text' : 'muted') + ' small' }, a.error || `${a.name} · ${a.auth === 'oauth' ? '已登录授权' : '专用密码'} · ${a.send ? '可读可发，每封先问你' : '只读'}`)),
      h('div', { class: 'row' },
        action(a.send ? '可以发信' : '不发信', () => sendSwitch(a, apply),
          { class: 'btn small', title: a.send ? '点一下改回只读' : '点一下允许 Jurvis 从这个邮箱发信（每封都先问你）' }),
        action(a.cloud ? '本机和云端模型' : '仅本机模型', async () => apply(await patch('/api/mail/' + encodeURIComponent(a.id), { cloud: !a.cloud })),
          { class: 'btn small', title: a.cloud ? '点一下改成只给本机模型读' : '点一下也允许云端模型读' }),
        action(icon('trash', 15), async () => { if (!await ui.ask(`移除邮箱 ${a.address}？`)) return; apply(await del('/api/mail/' + encodeURIComponent(a.id))); },
          { class: 'icon-btn small', 'aria-label': `移除 ${a.address}`, title: '移除' }))))) : null,
    h('div', { class: 'mail-grid' }, (data.mail.presets || []).map((p) => h('button', { class: 'mail-tile', type: 'button', onclick: () => connectMail(p) },
      brandMark(p.name, 'large', p.kind),
      h('span', { class: 'mail-name' }, p.name),
      h('span', { class: 'mail-how' }, p.auth === 'oauth' ? (p.kind === 'gmail' ? '用 Google 登录' : '用 Microsoft 登录') : p.secret)))));
}

/** Lets Jurvis send from a mailbox, or takes that back. Gmail and Outlook need one more permission, given by signing in again. */
async function sendSwitch(a, apply) {
  if (a.send) { apply(await patch('/api/mail/' + encodeURIComponent(a.id), { send: false })); return; }
  if (!await ui.ask(`允许 Jurvis 从 ${a.address} 发邮件？它写好的每一封都会先把全文给你看，你点「允许」才会发出去。`, { ok: '允许发信' })) return;
  try {
    apply(await patch('/api/mail/' + encodeURIComponent(a.id), { send: true }));
  } catch (err) {
    if (a.auth !== 'oauth') throw err;
    const { url } = await post('/api/mail/connect/' + a.kind, { cloud: a.cloud, send: true });
    window.open(url, '_blank', 'noopener');
    notify(`发信需要多一项授权：在新打开的页面里再登录一次 ${a.name}，完成后这里会自动更新。`, 'info');
  }
}

export function refreshMail() { return loadPart('mail', '/api/mail', (r) => r, renderMail); }

async function connectMail(p) {
  if (p.auth !== 'oauth') { addMail(p); return; }
  if (!p.app.configured) { appSetup(p.app, refreshMail, p.kind === 'gmail' ? 'Google 开发者应用（Gmail 和 Google Health 共用）' : 'Microsoft 开发者应用'); return; }
  try {
    const { url } = await post('/api/mail/connect/' + p.kind, {});
    window.open(url, '_blank', 'noopener');
    notify(`在新打开的页面里登录 ${p.name}，完成后这里会自动出现。` + (p.app.on_this_mac ? '（回调地址是 localhost，需要在 Mac 上操作）' : ''), 'info');
  } catch (err) { notify(err.message); }
}

function addMail(p) {
  const address = h('input', { class: 'input', type: 'email', placeholder: 'you@example.com', autocomplete: 'off', autocapitalize: 'off' });
  const secret = h('input', { class: 'input', type: 'password', placeholder: p.secret, autocomplete: 'off' });
  const host = h('input', { class: 'input', placeholder: 'imap.example.com', autocapitalize: 'off', autocomplete: 'off' });
  const save = h('button', { class: 'btn primary', type: 'submit' }, '连接');
  const form = h('form', { class: 'stack' },
    h('div', { class: 'mail-sheet-head' }, brandMark(p.name, 'large', p.kind), h('p', { class: 'muted small' }, p.hint)),
    p.link ? h('a', { href: p.link, target: '_blank', rel: 'noopener', class: 'small' }, `打开 ${p.name} 的设置页 ↗`) : null,
    h('label', { class: 'field' }, h('span', null, '邮箱地址'), address),
    h('label', { class: 'field' }, h('span', null, p.secret), secret),
    p.kind === 'imap' ? h('label', { class: 'field' }, h('span', null, 'IMAP 服务器'), host) : null,
    h('p', { class: 'muted small' }, `${p.secret}只保存在这台 Mac 上，只用来读取收件箱；不会发信，也不会把邮件标为已读。`),
    h('div', { class: 'row end' }, save));
  const close = sheet(`连接 ${p.name}`, form);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const id = (address.value.split('@')[0] || 'mail').toLowerCase().replace(/[^a-z0-9_-]/g, '').slice(0, 24) || 'mail';
    const ok = await run(save, async () => {
      const result = await put('/api/mail/' + encodeURIComponent(id), { kind: p.kind, address: address.value.trim(), password: secret.value, host: host.value.trim() });
      data.mail.accounts = result.accounts; renderMail();
    });
    if (ok) close();
  });
}

// ---------------------------------------------------------------- 电话
const CALL_STATUS = { dialing: '正在拨号', ringing: '对方响铃中', talking: '通话中' };

export function refreshPhone() { return loadPart('phone', '/api/phone', (r) => r, renderPhone); }

function renderPhone() {
  const el = els.phone;
  if (failed(el, 'phone') || !data.phone) return;
  const { binding, catalog = [], calls = [], public_url: reach, problem, speech = {} } = data.phone;
  const spec = binding ? catalog.find((c) => c.kind === binding.kind) : null;
  const blockers = [
    !reach ? (problem || '还没有公网地址，电话服务商连不到这台 Mac。') : null,
    speech.stt && speech.tts ? null : '打电话要用本机的语音识别和语音合成模型，请先在上面的“语音”里装好。',
  ].filter(Boolean);
  fill(el,
    binding ? h('div', { class: 'health-accounts' }, h('div', { class: 'health-account' },
      brandMark(binding.title, '', binding.kind),
      h('div', { class: 'made-text grow' },
        h('strong', null, binding.caller),
        h('span', { class: 'muted small' }, `${binding.title} · 主叫号码` + (binding.country ? ` · 默认区号 ${binding.country}` : ''))),
      h('div', { class: 'row' },
        h('button', { class: 'btn small', type: 'button', onclick: () => bindPhone(spec, binding) }, '修改'),
        action(icon('trash', 15), async () => {
          if (!await ui.ask(`解绑 ${binding.title}？之后 Jurvis 就不能打电话了，通话记录会保留。`)) return;
          data.phone.binding = (await del('/api/phone')).binding; renderPhone();
        }, { class: 'icon-btn small', 'aria-label': `解绑 ${binding.title}`, title: '解绑' }))))
      : h('div', { class: 'mail-grid phone-grid' }, catalog.map((c) => h('button', { class: 'mail-tile', type: 'button', onclick: () => bindPhone(c) },
        brandMark(c.title, 'large', c.kind), h('span', { class: 'mail-name' }, c.title), h('span', { class: 'mail-how' }, c.description)))),
    binding ? blockers.map((text) => h('p', { class: 'error-text small' }, text)) : null,
    binding && !blockers.length ? h('p', { class: 'muted small' }, '已经可以用了。在对话里说“帮我给某某餐厅打电话订周六晚上 7 点 4 位”试试。') : null,
    calls.length ? h('div', { class: 'health-accounts' }, calls.slice(0, 8).map((c) => h('button', { class: 'health-account call-row', type: 'button', onclick: () => showCall(c.id) },
      h('div', { class: 'made-text grow' },
        h('strong', null, c.place || c.number),
        h('span', { class: 'muted small' }, c.status === 'ended' ? (c.summary || c.ended || '') : c.purpose)),
      h('span', { class: 'call-state' + (c.status !== 'ended' ? ' live' : c.outcome === 'done' ? ' done' : '') },
        c.status === 'ended' ? (c.outcome_label || '已结束') : CALL_STATUS[c.status] || c.status),
      h('span', { class: 'muted small' }, shortTime(c.created_at))))) : null);
}

function bindPhone(spec, binding = null) {
  if (!spec) return;
  const inputs = {};
  const country = h('input', { class: 'input', placeholder: '+86', inputMode: 'tel', autocomplete: 'off', value: binding ? binding.country : '' });
  const save = h('button', { class: 'btn primary', type: 'submit' }, binding ? '保存' : '绑定');
  const form = h('form', { class: 'stack' },
    h('div', { class: 'mail-sheet-head' }, brandMark(spec.title, 'large', spec.kind), h('p', { class: 'muted small' }, spec.setup)),
    h('a', { href: spec.console, target: '_blank', rel: 'noopener', class: 'small' }, `打开 ${spec.title} 后台 ↗`),
    spec.fields.map((f) => {
      const kept = binding && f.secret;
      inputs[f.name] = h('input', { class: 'input', type: f.secret ? 'password' : 'text', autocomplete: 'off', autocapitalize: 'off', spellcheck: false,
        placeholder: kept ? '已保存，留空则不改' : f.placeholder || '', value: binding && !f.secret ? binding.settings[f.name] || '' : '' });
      return h('label', { class: 'field' }, h('span', null, f.label + (f.optional ? '（可选）' : '')), inputs[f.name]);
    }),
    h('label', { class: 'field' }, h('span', null, '默认国家区号（可选）'), country),
    h('p', { class: 'muted small' }, '没写区号的号码会按这个区号拨打。凭据只保存在这台 Mac 上，绑定时会向服务商核对一次。'),
    h('div', { class: 'row end' }, save));
  const close = sheet(`${binding ? '修改' : '绑定'} ${spec.title}`, form);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const settings = Object.fromEntries(Object.entries(inputs).map(([name, input]) => [name, input.value.trim()]));
    const ok = await run(save, async () => {
      data.phone.binding = (await put('/api/phone', { kind: spec.kind, settings, country: country.value.trim() })).binding;
      renderPhone();
    });
    if (ok) close();
  });
}

async function showCall(id) {
  let c;
  try { c = await get('/api/phone/calls/' + encodeURIComponent(id)); } catch (err) { notify(err.message); return; }
  const live = c.status !== 'ended';
  let close = () => {};
  const body = h('div', { class: 'stack' },
    h('p', { class: 'muted small' }, `${c.number} · ${new Date(c.created_at * 1000).toLocaleString('zh-CN')}` + (live ? ` · ${CALL_STATUS[c.status] || ''}` : c.ended ? ` · ${c.ended}` : '')),
    h('p', { class: 'small' }, h('strong', null, '要办的事：'), c.purpose),
    c.summary ? h('p', null, h('strong', null, (c.outcome_label || '结果') + '：'), c.summary) : null,
    c.booking ? h('p', { class: 'muted small' }, `日程：${c.booking.start.replace('T', ' ')}${c.booking.timezone ? `（${c.booking.timezone}）` : ''} · `
      + (c.booking.status === 'added' ? '已加入日历' : '等 Mac 或 iPhone 上的 Jurvis 打开后加入日历')) : null,
    c.transcript.length ? h('div', { class: 'call-lines' }, c.transcript.map((t) => h('p', { class: 'call-line ' + t.who },
      h('span', { class: 'call-who' }, t.who === 'them' ? '对方' : 'Jurvis'), t.text))) : h('p', { class: 'muted small' }, live ? '还没有人说话。' : '没有通话内容。'),
    live ? h('div', { class: 'row end' }, action('挂断', async () => { await post(`/api/phone/calls/${encodeURIComponent(id)}/hangup`); close(); refreshPhone(); }, { class: 'btn danger' })) : null);
  close = sheet(c.place || c.number, body);
}

// ---------------------------------------------------------------- 主动关心
function askToNotify() {
  if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission().catch(() => {});
}
function renderCare() {
  const el = els.care;
  if (failed(el, 'care') || !data.care) return;
  const c = data.care;
  const time = (key) => {
    const input = h('input', { class: 'input small-select', type: 'time', value: c[key] });
    input.addEventListener('change', async () => { data.care = await put('/api/checkin', { [key]: input.value }); });
    return input;
  };
  fill(el,
    toggle('身体数据一变差就主动问候我', c.enabled, async (want) => { if (want) askToNotify(); data.care = await put('/api/checkin', { enabled: want }); }),
    h('div', { class: 'control-row' }, h('span', { class: 'control-label grow' }, '安静时段（不打扰）'), time('quiet_start'), h('span', { class: 'muted' }, '到'), time('quiet_end')),
    c.last_at ? h('p', { class: 'muted small' }, '上一次问候：' + new Date(c.last_at * 1000).toLocaleString('zh-CN')
      + (c.spacing > 3600 ? `。前几次你没有回，现在两次问候至少隔 ${Math.round(c.spacing / 3600)} 小时` : '')
      + (c.muted_until && c.muted_until * 1000 > Date.now() ? `。你说过上次不合适，同样的事在 ${new Date(c.muted_until * 1000).toLocaleDateString('zh-CN')} 前不再提` : '')) : null,
    'Notification' in window && Notification.permission !== 'granted'
      ? h('p', { class: 'muted small' }, Notification.permission === 'denied' ? '浏览器关掉了通知，问候只会出现在页面里。' : '打开上面的开关时会请求通知权限。') : null,
    h('div', { class: 'row' }, action('现在看看', async () => {
      askToNotify();
      try { await post('/api/checkin/now'); notify('Jurvis 正在看看你最近怎么样，写好了会出现在对话里', 'info'); } catch (err) { notify(err.message, 'info'); }
    }, { class: 'btn small' })));
}

// ---------------------------------------------------------------- 智能家居
function renderHome() {
  const el = els.home;
  if (failed(el, 'home') || failed(el, 'settings') || !data.home || !store.settings) return;
  const catalog = data.home.catalog || [];
  const connectors = data.home.connectors || [];
  const confirmMode = (store.settings.settings.home || {}).confirm || 'sensitive';
  const select = ui.select({ value: confirmMode, 'aria-label': 'Jurvis 操作设备前的确认方式' },
    [{ value: 'sensitive', label: '只有门锁、门窗等敏感操作先问我' }, { value: 'always', label: '每次操作都先问我' }]);
  select.addEventListener('change', async () => {
    const want = select.value;
    const ok = await run(select, () => saveSettings({ home: { confirm: want } }));
    if (!ok) select.value = confirmMode;
  });
  connect.use(data.home);
  fill(el, 
    h('div', { class: 'field' }, h('span', null, 'Jurvis 在对话中操作设备时'), select),
    connectors.length ? h('div', { class: 'connect-list' }, connectors.map(connect.connectedRow)) : h('p', { class: 'muted' }, '还没有连接任何品牌。'),
    h('div', { class: 'row' }, h('button', { class: 'btn small', type: 'button', onclick: () => connect.open() }, '连接品牌…'),
      h('span', { class: 'muted small' }, `支持 ${catalog.length} 个品牌和平台`)));
}

// ---------------------------------------------------------------- 设备
function renderDevices() {
  const el = els.devices;
  let mine = '';
  try { mine = localStorage.getItem('jurvis.device') || ''; } catch { /* unavailable */ }
  const list = Array.isArray(data.devices) ? data.devices.filter((d) => !d.revoked) : [];
  fill(el, 
    fail.devices ? h('p', { class: 'error-text small' }, fail.devices) : null,
    list.length ? h('ul', { class: 'plain-list' }, list.map((d) => h('li', { class: 'row spread' },
      h('span', null, d.name || '未命名设备', d.id === mine ? h('span', { class: 'badge ok gap' }, '这台设备') : null),
      h('span', { class: 'muted small' }, d.last_seen ? '最近使用 ' + shortTime(d.last_seen) : '')))) : null,
    h('p', { class: 'muted small' }, '撤销其他设备的访问需要在 Mac 的 Jurvis 里操作。'),
    h('div', { class: 'row' }, h('button', { class: 'btn danger small', type: 'button', onclick: async () => {
      if (!await ui.ask('断开这台设备与 Jurvis 的连接？之后需要在 Mac 上重新生成邀请才能再次使用。', { ok: '断开', danger: true })) return;
      try { localStorage.removeItem('jurvis.device'); } catch { /* unavailable */ }
      signOut();
    } }, '断开连接')));
}
