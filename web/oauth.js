// The user's own Google, WHOOP or Microsoft app: where to make it, the callback address to give it, and its ID and secret.
// One sheet for every connector that signs in (Google Health and Gmail share the Google app).
import { h, put, run, notify, sheet } from './core.js';

/** Opens the sheet for an app as /api/... describes it ({app, app_name, setup, console, redirect_uri, client_id, client_from, configured}). */
export function appSetup(app, changed, title = '') {
  const id = h('input', { class: 'input', autocomplete: 'off', autocapitalize: 'off', spellcheck: false, value: app.client_id || '', placeholder: 'Client ID' });
  const secret = h('input', { class: 'input', type: 'password', autocomplete: 'off', placeholder: app.configured ? '留空则保留已保存的密钥' : 'Client Secret' });
  const save = h('button', { class: 'btn primary', type: 'submit' }, '保存');
  const copy = h('button', { class: 'btn small', type: 'button', onclick: () => navigator.clipboard.writeText(app.redirect_uri).then(() => notify('已复制回调地址', 'info'), () => {}) }, '复制');
  const form = h('form', { class: 'stack' },
    h('p', { class: 'muted small' }, app.setup),
    h('a', { href: app.console, target: '_blank', rel: 'noopener', class: 'small' }, `打开 ${app.app_name} 开发者后台 ↗`),
    h('label', { class: 'field' }, h('span', null, '回调地址'), h('div', { class: 'row' }, h('code', { class: 'break grow' }, app.redirect_uri), copy)),
    app.client_from === 'env' ? h('p', { class: 'muted small' }, '现在用的是 .env 里的应用；在这里保存的只在 .env 没填时生效。') : null,
    h('label', { class: 'field' }, h('span', null, 'Client ID'), id),
    h('label', { class: 'field' }, h('span', null, 'Client Secret'), secret),
    h('p', { class: 'muted small' }, '只保存在这台 Mac 上（data/security/oauth_apps.json），不会再发回任何设备。'),
    h('div', { class: 'row end' }, save));
  const close = sheet(title || `${app.app_name} 开发者应用`, form);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const ok = await run(save, async () => { await put('/api/oauth/' + app.app, { client_id: id.value, client_secret: secret.value }); });
    if (ok) { close(); changed(); notify('已保存，现在可以登录连接了', 'info'); }
  });
}
