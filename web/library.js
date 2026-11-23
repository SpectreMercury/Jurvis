// 作品库: everything Jurvis wrote or generated — documents in the workspace folder, plus images and videos.
import { store, h, fill, api, get, notify, assetURL, sheet, shortTime, ApiError } from './core.js';
import { icon, fileIcon } from './icons.js';
import { renderMarkdown } from './md.js';
import { artifactKind, openPreview } from './preview.js';

const FILTERS = [['all', '全部', null], ['doc', '文档', 'doc'], ['image', '图片', 'image'], ['video', '视频', 'video']];
let files = null;
let failure = '';
let filter = 'all';
let sig = '';
const els = {};

export function mount(root) {
  els.search = h('input', { type: 'search', class: 'input', placeholder: '搜索文件名', 'aria-label': '搜索作品', autocomplete: 'off' });
  els.search.addEventListener('input', render);
  els.file = h('input', { type: 'file', hidden: true, accept: '.txt,.md,.markdown,.csv,.json,text/*' });
  els.file.addEventListener('change', async () => {
    const file = els.file.files && els.file.files[0];
    els.file.value = '';
    if (!file) return;
    const form = new FormData();
    form.append('file', file, file.name);
    try {
      const saved = await api('POST', '/api/files', form);
      notify(`已导入 ${saved.name || file.name}`, 'info');
      await load();
    } catch (err) {
      if (!(err instanceof ApiError && err.status === 401)) notify(err.message);
    }
  });
  els.filters = h('div', { class: 'seg', role: 'tablist', 'aria-label': '类型' }, FILTERS.map(([id, label, glyph]) =>
    h('button', { type: 'button', role: 'tab', dataset: { filter: id }, onclick: () => { filter = id; render(); } }, glyph ? icon(glyph, 14) : null, label)));
  els.sub = h('span', { class: 'page-sub' }, 'Jurvis 写下和生成的所有东西');
  els.body = h('div', { class: 'stack' }, h('p', { class: 'muted' }, '正在读取…'));
  root.append(h('div', { class: 'page' },
    h('header', { class: 'page-head' },
      h('div', { class: 'page-title' }, h('h1', null, '作品库'), els.sub),
      h('div', { class: 'lib-tools' }, els.search,
        h('button', { class: 'btn small', type: 'button', onclick: () => els.file.click() }, icon('plus', 15), '导入文件'), els.file)),
    h('div', { class: 'row' }, els.filters),
    els.body));
}

async function load() {
  try {
    const result = await get('/api/files');
    files = Array.isArray(result.files) ? result.files : [];
    failure = '';
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return;
    failure = err.message;
  }
  render();
}

export function activate() { sig = ''; load(); }
export function onState() { render(); }
export function reset() { files = null; failure = ''; sig = ''; }

function size(bytes) {
  return bytes < 1000 ? bytes + ' B' : bytes < 1e6 ? (bytes / 1000).toFixed(1) + ' KB' : (bytes / 1e6).toFixed(1) + ' MB';
}

/** Opens a workspace document in a sheet. */
export function openDocument(name) {
  if (artifactKind(name)) {
    const pane = openPreview(name);
    get('/api/files/' + encodeURIComponent(name)).then((result) => pane.show(name, String(result.content || '')))
      .catch((err) => pane.message(name, err.message, true));
    return;
  }
  const body = h('div', { class: 'doc-view' }, h('p', { class: 'muted small' }, '正在读取…'));
  sheet(name, body, { wide: true });
  get('/api/files/' + encodeURIComponent(name)).then((result) => {
    const text = String(result.content || '');
    fill(body, /\.(md|markdown|txt)$/i.test(name) || !/\./.test(name) ? h('div', { class: 'md' }, renderMarkdown(text)) : h('pre', { class: 'plain' }, text));
  }).catch((err) => { fill(body, h('p', { class: 'error-text' }, err.message)); });
}

function mediaCard(asset) {
  const video = String(asset.mime || '').startsWith('video');
  const thumb = h('span', { class: 'lib-thumb' }, icon(video ? 'video' : 'image', 20));
  const card = h('button', { class: 'lib-card', type: 'button', 'aria-label': (video ? '视频 ' : '图片 ') + (asset.name || '') }, thumb,
    h('span', { class: 'lib-meta' }, icon(video ? 'video' : 'image', 15), h('span', { class: 'clip' }, asset.name || (video ? '视频' : '图片')),
      asset.created_at ? h('span', { class: 'small' }, shortTime(asset.created_at)) : null));
  let url = null;
  if (!video) assetURL(asset.id).then((u) => { url = u; fill(thumb, h('img', { src: u, alt: '', loading: 'lazy' })); }).catch(() => {});
  card.addEventListener('click', async () => {
    try {
      url = url || await assetURL(asset.id);
      sheet(asset.name || (video ? '视频' : '图片'), video ? h('video', { class: 'lightbox', src: url, controls: true, playsInline: true })
        : h('img', { class: 'lightbox', src: url, alt: asset.name || '图片' }), { wide: true });
    } catch (err) { notify('素材读取失败：' + ((err && err.message) || '')); }
  });
  return card;
}

function render() {
  if (!els.body) return;
  for (const button of els.filters.children) button.setAttribute('aria-selected', String(button.dataset.filter === filter));
  const q = els.search.value.trim().toLowerCase();
  const match = (name) => !q || String(name || '').toLowerCase().includes(q);
  const docs = (files || []).filter((f) => match(f.name)).sort((a, b) => (b.modified || 0) - (a.modified || 0));
  const assets = ((store.state && store.state.assets) || []).filter((a) => /^(image|video)/.test(String(a.mime || '')) && a.kind !== 'card' && !String(a.kind || '').startsWith('avatar') && match(a.name))
    .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  const images = assets.filter((a) => String(a.mime).startsWith('image'));
  const videos = assets.filter((a) => String(a.mime).startsWith('video'));
  const next = JSON.stringify([filter, q, docs, assets.map((a) => a.id), failure, files === null]);
  if (next === sig) return;
  sig = next;
  els.sub.textContent = files === null ? 'Jurvis 写下和生成的所有东西' : `${(files || []).length} 个文档 · ${images.length} 张图片 · ${videos.length} 个视频`;

  const out = [];
  if (failure) out.push(h('div', { class: 'note error' }, failure));
  const showDocs = filter === 'all' || filter === 'doc';
  const media = filter === 'image' ? images : filter === 'video' ? videos : filter === 'all' ? assets : [];
  if (showDocs && docs.length) {
    out.push(h('section', { class: 'stack' }, h('div', { class: 'side-title' }, '文档'),
      h('div', { class: 'lib-table' },
        h('div', { class: 'lib-row head' }, h('span', { class: 'grow' }, '名称'), h('span', null, '大小'), h('span', null, '修改时间')),
        docs.map((f) => h('button', { class: 'lib-row', type: 'button', onclick: () => openDocument(f.name) },
          icon(fileIcon(f.name)), h('span', { class: 'file-name' }, f.name), h('span', { class: 'small' }, size(Number(f.bytes) || 0)),
          h('span', { class: 'small' }, f.modified ? shortTime(f.modified) : ''))))));
  }
  if (media.length) {
    out.push(h('section', { class: 'stack' }, h('div', { class: 'side-title' }, filter === 'video' ? '视频' : filter === 'image' ? '图片' : '图片与视频'),
      h('div', { class: 'lib-grid' }, media.slice(0, 120).map(mediaCard))));
  }
  if (!out.length && files !== null) {
    out.push(h('div', { class: 'empty' }, h('span', { class: 'tile round' }, icon('folder')),
      h('p', { class: 'empty-title' }, q ? '没有匹配的作品' : '这里还是空的'),
      q ? null : h('p', { class: 'muted' }, '让 Jurvis 写一份文档、生成一张图片，结果就会出现在这里。')));
  }
  if (!out.length) out.push(h('p', { class: 'muted' }, '正在读取…'));
  fill(els.body, ...out);
}
