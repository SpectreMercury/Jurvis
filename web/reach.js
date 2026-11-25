// 设置 的后半部分: how Jurvis reaches you when the app is closed, what it may read of your day, what it need not ask about,
// and the nightly copy of your data. Each part reads and writes its own endpoint.
import { store, h, fill, get, put, patch, del, post, run, action, notify, shortTime, ApiError } from './core.js';
import * as ui from './ui.js';
import { icon } from './icons.js';

const data = { push: null, personal: null, permits: null, backup: null };
const fail = {};
const els = {};
const KIND_ICON = { calendar: 'clock', reminders: 'tasks', contacts: 'bookmark' };
const TOOL_NAME = { run_command: '命令', browser: '浏览器', home: '智能家居', mcp: 'MCP 工具' };

/** [title, element, hint] for each part, in the order they appear in 设置. */
export function sections() {
  for (const name of ['push', 'personal', 'permits', 'backup']) els[name] = h('div', { class: 'stack' }, h('p', { class: 'muted' }, '正在读取…'));
  return [
    ['通知', els.push, '有事找你时（主动问候、后台任务做完了、有个操作等你确认），打开着的 App 会自己提醒；App 关着时靠推送。'],
    ['日历与通讯录', els.personal, '日历、提醒事项和通讯录由 iPhone 或 Mac 上的 Jurvis App 读取后送到这台 Mac，在 App 的设置里逐项打开。默认只有本机模型能读；Jurvis 只会新增日程和提醒，不改不删。'],
    ['不用再问的操作', els.permits, '确认卡片上选了「以后都允许」的那一类操作会列在这里，随时可以收回。电话、邮件、门锁和摄像头每次都问，不会出现在这里。'],
    ['备份与电源', els.backup, '每晚把对话、记忆、账本、健康数据、设置和密钥复制一份。备份里有和数据目录一样的密钥，放进会同步到云端的文件夹前请想清楚。'],
  ];
}

export function reset() { for (const key of Object.keys(data)) data[key] = null; }

function load(name, path, render) {
  return get(path).then((result) => { data[name] = result; delete fail[name]; })
    .catch((err) => { if (!(err instanceof ApiError && err.status === 401)) fail[name] = err.message; }).then(render);
}

export function activate() {
  load('push', '/api/push', renderPush);
  load('personal', '/api/personal', renderPersonal);
  load('permits', '/api/permits', renderPermits);
  load('backup', '/api/backup', renderBackup);
}

export function onSettings() { renderBackup(); }

function failed(el, key) {
  if (!fail[key]) return false;
  fill(el, h('p', { class: 'error-text' }, fail[key]), h('button', { class: 'btn small', type: 'button', onclick: activate }, '重试'));
  return true;
}

function toggle(label, checked, onChange, hint) {
  const box = ui.toggle({ checked: !!checked, 'aria-label': label });
  box.addEventListener('change', async () => {
    const want = box.checked;
    const ok = await run(box, () => onChange(want));
    if (!ok) box.checked = !want;
  });
  return h('label', { class: 'control-row' }, h('span', { class: 'control-label grow' }, label, hint ? h('span', { class: 'muted small block' }, hint) : null), box);
}

const when = (seconds) => new Date(seconds * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });

// ---------------------------------------------------------------- 通知
function keyBytes(text) {
  const raw = atob(text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - text.length % 4) % 4));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

const pushable = () => 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;

async function subscription() {
  if (!pushable()) return null;
  const registration = await navigator.serviceWorker.getRegistration();
  return registration ? registration.pushManager.getSubscription() : null;
}

async function enablePush() {
  if (!pushable()) throw new ApiError('这个浏览器不支持推送。iPhone 和 iPad 上要先用 Safari 的「添加到主屏幕」，再从主屏幕打开 Jurvis。', 400);
  const registration = await Promise.race([navigator.serviceWorker.ready, new Promise((resolve) => setTimeout(resolve, 4000))]);
  if (!registration) throw new ApiError('推送需要浏览器信任这个地址的证书：请用 Tailscale Funnel 的地址或 localhost 打开 Jurvis，自签名证书的局域网地址不行。', 400);
  if (await Notification.requestPermission() !== 'granted') throw new ApiError('浏览器没有给通知权限，请在浏览器的网站设置里允许通知。', 400);
  const key = keyBytes(data.push.vapid_key);
  let sub = await registration.pushManager.getSubscription();
  if (sub && sub.options && sub.options.applicationServerKey && String(new Uint8Array(sub.options.applicationServerKey)) !== String(key)) {
    await sub.unsubscribe();  // made for another engine's key: this one could never use it
    sub = null;
  }
  sub = sub || await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  const json = sub.toJSON();
  await post('/api/push/subscribe', { kind: 'web', endpoint: json.endpoint, keys: json.keys });
  data.push = await get('/api/push');
}

async function disablePush() {
  const sub = await subscription();
  if (sub) {
    data.push = await post('/api/push/unsubscribe', { address: sub.endpoint });
    await sub.unsubscribe().catch(() => {});
  }
}

async function renderPush() {
  const el = els.push;
  if (failed(el, 'push') || !data.push) return;
  const p = data.push;
  const sub = await subscription().catch(() => null);
  const here = !!sub && p.targets.some((t) => t.mine && t.kind === 'web');
  const apply = (next) => { data.push = next; renderPush(); };

  const keyText = h('textarea', { class: 'input mono', rows: 4, placeholder: '-----BEGIN PRIVATE KEY-----\n…\n-----END PRIVATE KEY-----', autocomplete: 'off', spellcheck: 'false' });
  const keyId = h('input', { class: 'input', placeholder: 'Key ID，10 位', maxLength: 20, autocomplete: 'off', autocapitalize: 'characters', value: p.apns ? p.apns.key_id : '' });
  const teamId = h('input', { class: 'input', placeholder: 'Team ID，10 位', maxLength: 20, autocomplete: 'off', autocapitalize: 'characters', value: p.apns ? p.apns.team_id : '' });
  const saveKey = h('button', { class: 'btn small primary', type: 'submit' }, p.apns ? '更新' : '保存');
  const form = h('form', { class: 'stack' },
    h('p', { class: 'muted small' }, 'iPhone 和 iPad 上的 App 走 Apple 的推送服务，需要你自己 Apple 开发者账号里的一把推送密钥：在 developer.apple.com → Certificates, IDs & Profiles → Keys 新建一把，勾选 Apple Push Notifications service (APNs)，下载 .p8 文件，把它的内容贴在这里。密钥只保存在这台 Mac 上。'),
    h('label', { class: 'field' }, h('span', null, p.apns ? '.p8 密钥（留空表示不换）' : '.p8 密钥的内容'), keyText),
    h('div', { class: 'row' }, keyId, teamId),
    h('div', { class: 'row end' },
      p.apns ? action('移除密钥', async () => { if (await ui.ask('移除 Apple 推送密钥？iPhone 和 iPad 之后收不到推送。')) apply(await del('/api/push/apns')); }, { class: 'btn small' }) : null,
      saveKey));
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    await run(saveKey, async () => { apply(await put('/api/push/apns', { key: keyText.value, key_id: keyId.value, team_id: teamId.value })); notify('已保存', 'info'); });
  });

  fill(el,
    toggle('这台设备的浏览器接收推送', here, async (want) => { if (want) await enablePush(); else await disablePush(); renderPush(); },
      pushable() ? '关掉页面之后也能收到。内容经浏览器厂商的推送服务转发，全程加密，只有这个浏览器能解开。' : '这个浏览器不支持。iPhone 和 iPad 上要先「添加到主屏幕」。'),
    here ? h('div', { class: 'row' }, action('发一条试试', async () => {
      const result = await post('/api/push/test');
      notify(result.sent ? '已发出，几秒内应该出现在通知里' : '没有发出去：' + (result.errors[0] || '未知原因'), result.sent ? 'info' : 'error');
    }, { class: 'btn small' })) : null,
    p.targets.length ? h('div', { class: 'health-accounts' }, p.targets.map((t) => h('div', { class: 'health-account' },
      h('span', { class: 'tile round' }, icon(t.kind === 'apns' ? 'send' : 'globe', 15)),
      h('div', { class: 'made-text grow' },
        h('strong', null, t.name + (t.mine ? '（这台设备）' : '')),
        h('span', { class: (t.error ? 'error-text' : 'muted') + ' small' },
          t.error || [t.kind === 'apns' ? 'App 推送' : '浏览器推送', t.sent_at ? '上次推送 ' + shortTime(t.sent_at) : '还没有推送过'].join(' · '))),
      action(icon('trash', 15), async () => apply(await del('/api/push/targets/' + encodeURIComponent(t.id))), { class: 'icon-btn small', 'aria-label': '移除', title: '移除' })))) : null,
    ui.disclosure('push-apns', h('span', null, 'iPhone 和 iPad App 的推送' + (p.apns ? ` · 已填写（Key ${p.apns.key_id}）` : ' · 还没有密钥')), form, false));
}

// ---------------------------------------------------------------- 日历与通讯录
function renderPersonal() {
  const el = els.personal;
  if (failed(el, 'personal') || !data.personal) return;
  const apply = (next) => { data.personal = next; renderPersonal(); };
  fill(el, h('div', { class: 'health-accounts' }, data.personal.kinds.map((k) => h('div', { class: 'health-account' },
    h('span', { class: 'tile round' }, icon(KIND_ICON[k.kind] || 'doc', 15)),
    h('div', { class: 'made-text grow' },
      h('strong', null, k.name),
      h('span', { class: 'muted small' }, k.synced_at
        ? `${k.count} 条 · ${when(k.synced_at)} 来自 ${k.device || '一台设备'}`
        : '还没有送来。在 iPhone 或 Mac 上的 Jurvis App → 设置 里打开。')),
    k.synced_at ? h('div', { class: 'row' },
      action(k.cloud ? '本机和云端模型' : '仅本机模型', async () => apply(await patch('/api/personal/' + k.kind, { cloud: !k.cloud })),
        { class: 'btn small', title: k.cloud ? '点一下改成只给本机模型读' : '点一下也允许云端模型读' }),
      action(icon('trash', 15), async () => {
        if (!await ui.ask(`从这台 Mac 上清掉${k.name}的副本？设备上的原件不受影响；要不再送来，请在 App 的设置里关掉。`)) return;
        apply(await del('/api/personal/' + k.kind));
      }, { class: 'icon-btn small', 'aria-label': `清掉${k.name}`, title: '清掉这份副本' })) : null))));
}

// ---------------------------------------------------------------- 不用再问的操作
function renderPermits() {
  const el = els.permits;
  if (failed(el, 'permits') || !data.permits) return;
  const list = data.permits.permits || [];
  fill(el, list.length ? h('div', { class: 'health-accounts' }, list.map((p) => h('div', { class: 'health-account' },
    h('span', { class: 'tile round' }, icon('check', 15)),
    h('div', { class: 'made-text grow' },
      h('strong', null, p.label.replace(/^以后/, '').replace(/都不用问$/, '')),
      h('span', { class: 'muted small' }, [TOOL_NAME[p.tool] || p.tool, '允许于 ' + when(p.created_at), p.uses ? `用过 ${p.uses} 次` : '还没用过'].join(' · '))),
    action('收回', async () => { data.permits = await del('/api/permits/' + encodeURIComponent(p.id)); renderPermits(); }, { class: 'btn small' }))))
    : h('p', { class: 'muted small' }, '现在每一类操作都会先问你。'));
}

// ---------------------------------------------------------------- 备份与电源
function renderBackup() {
  const el = els.backup;
  if (failed(el, 'backup') || !data.backup) return;
  const b = data.backup;
  const apply = (next) => { data.backup = next; renderBackup(); };
  const keep = ui.select({ value: String(b.keep), 'aria-label': '保留几份' }, [3, 7, 14, 30].concat([3, 7, 14, 30].includes(b.keep) ? [] : [b.keep]).sort((x, y) => x - y)
    .map((n) => ({ value: String(n), label: `最近 ${n} 份` })));
  keep.addEventListener('change', () => run(keep, async () => apply(await put('/api/backup', { keep: Number(keep.value) }))));
  const folder = h('input', { class: 'input', placeholder: b.location, value: b.folder, autocapitalize: 'off', autocomplete: 'off', spellcheck: 'false' });
  const power = (store.settings && store.settings.settings.power) || {};
  const latest = b.copies[0];
  fill(el,
    toggle('每晚自动备份', b.enabled, async (want) => apply(await put('/api/backup', { enabled: want })), `凌晨 3 点之后引擎空下来时做。位置：${b.location}`),
    toggle('连同生成的图片和视频', b.assets, async (want) => apply(await put('/api/backup', { assets: want })), '素材很大，默认不带。'),
    h('div', { class: 'control-row' }, h('span', { class: 'control-label grow' }, '保留'), keep),
    h('div', { class: 'control-row' }, h('span', { class: 'control-label grow' }, '备份位置', h('span', { class: 'muted small block' }, '留空用数据目录里的 backups；换到另一块硬盘才防得住这块盘坏掉。只能在 Mac 上改。')), folder,
      action('保存', async () => apply(await put('/api/backup', { folder: folder.value.trim() })), { class: 'btn small' })),
    b.last_error ? h('p', { class: 'error-text small' }, '上一次没有完成：' + b.last_error) : null,
    h('p', { class: 'muted small' }, latest ? `最近一份：${latest.name}（${(latest.bytes / 1048576).toFixed(1)} MB），共 ${b.copies.length} 份` : '还没有备份。'),
    h('div', { class: 'row' }, action('现在备份一次', async () => { apply(await post('/api/backup/run')); notify('备份完成', 'info'); }, { class: 'btn small' })),
    toggle('接着电源时不让 Mac 自己睡着', power.keep_awake, async (want) => { store.settings = await patch('/api/settings', { power: { keep_awake: want } }); },
      'Mac 睡着后定时任务、事件触发、主动问候和电话都会停。打开后，插着电源时屏幕可以关，但系统不休眠；合上盖子仍然会睡。'));
}
