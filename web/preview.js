// Live preview of what Jurvis writes: an HTML page runs in a sandboxed frame, an SVG shows as an image,
// each with a 预览 / 源码 switch. Used by the workspace panel and the 作品库.
//
// Agent-written HTML is untrusted. It never touches the app's DOM: it goes into <iframe sandbox="allow-scripts">
// through srcdoc, so it runs in an opaque origin (no access to this app's localStorage, cookies, or DOM, and any
// request it could make would carry no credentials), and a CSP put in front of the page turns the network off.
import { h, fill, sheet } from './core.js';
import { icon } from './icons.js';

/** 'html' | 'svg' for files that have a rendered preview, otherwise null. */
export function artifactKind(name) {
  const n = String(name || '');
  return /\.html?$/i.test(n) ? 'html' : /\.svg$/i.test(n) ? 'svg' : null;
}

// Inline scripts and styles run; images, media and fonts only from data:/blob:. Everything else — fetch, XHR,
// WebSocket, external scripts/styles/images, nested frames, form posts, <base> — is refused.
const CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; font-src data:; form-action 'none'; base-uri 'none'";
const HEAD = '<meta http-equiv="Content-Security-Policy" content="' + CSP + '"><meta name="referrer" content="no-referrer">';
const DOCTYPE = /^(?:\s|<!--[\s\S]*?-->)*<!doctype[^>]*>/i;

/** The page as it is handed to the frame: the policy comes before anything the page itself says. */
export function sandboxedDocument(html) {
  const text = String(html || '');
  const found = DOCTYPE.exec(text);
  return found ? found[0] + HEAD + text.slice(found[0].length) : '<!doctype html>' + HEAD + text;
}

function frame(html, onLeave) {
  const el = h('iframe', { class: 'pv-page', title: '页面预览', sandbox: 'allow-scripts', referrerpolicy: 'no-referrer', allow: '', loading: 'eager' });
  // The sandbox cannot stop a page from navigating its own frame. The first load is the page; a second one means
  // it went somewhere else, so the frame is taken down rather than showing a site that is not the file.
  let loads = 0;
  el.addEventListener('load', () => { loads += 1; if (loads > 1) onLeave(); });
  el.srcdoc = sandboxedDocument(html);
  return el;
}

const svgURL = (text) => 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(text);

const MODES = [['preview', '预览', 'play'], ['source', '源码', 'code']];

/**
 * A preview pane. `el` goes into the page once and stays there (moving an iframe reloads it);
 * `show(name, text, {live})` updates it and does nothing when nothing changed.
 */
export function viewer({ expand = true } = {}) {
  let name = null, text = null, live = false, chosen = null, shown = '';
  const seg = h('div', { class: 'seg', role: 'tablist', 'aria-label': '查看方式' }, MODES.map(([id, label, glyph]) =>
    h('button', { type: 'button', role: 'tab', dataset: { mode: id }, onclick: () => { chosen = id; draw(true); } }, icon(glyph, 14), label)));
  const reload = h('button', { class: 'icon-btn small', type: 'button', 'aria-label': '重新载入预览', title: '重新载入预览', onclick: () => draw(true) }, icon('refresh', 16));
  const grow = expand ? h('button', { class: 'icon-btn small', type: 'button', 'aria-label': '放大查看', title: '放大查看',
    onclick: () => { if (text !== null) openPreview(name).show(name, text); } }, icon('panel', 16)) : null;
  const stage = h('div', { class: 'pv-stage' });
  const el = h('div', { class: 'pv' }, h('div', { class: 'pv-bar' }, seg, h('span', { class: 'grow' }), reload, grow), stage);

  // While the file is still being written the source is the useful view; once done, the rendered page.
  const mode = () => chosen || (live ? 'source' : 'preview');

  function note(message, error) {
    shown = '';
    fill(stage, h('p', { class: 'pv-note small ' + (error ? 'error-text' : 'muted') }, message));
  }

  function draw(force) {
    const now = mode();
    for (const button of seg.children) button.setAttribute('aria-selected', String(button.dataset.mode === now));
    reload.disabled = now !== 'preview' || text === null;
    if (grow) grow.disabled = text === null;
    if (text === null) return;
    const sig = now + '|' + name + '|' + text.length + '|' + text;
    if (sig === shown && !force) return;
    if (now === 'source') {
      const old = shown.startsWith('source|' + name + '|') ? stage.firstChild : null;
      const top = old ? old.scrollTop : 0;
      const box = h('div', { class: 'pv-source' }, h('pre', { class: 'plain' }, text));
      fill(stage, box);
      if (live) requestAnimationFrame(() => { box.scrollTop = box.scrollHeight; });
      else if (top) box.scrollTop = top;
    } else if (artifactKind(name) === 'svg') {
      const image = h('img', { class: 'pv-image', alt: name, src: svgURL(text) });
      image.addEventListener('error', () => note('这个 SVG 无法显示，可以切到“源码”查看内容。', true));
      fill(stage, h('div', { class: 'pv-frame' }, image));
    } else {
      fill(stage, h('div', { class: 'pv-frame' }, frame(text, () => note('页面跳转到了别的地址，预览已停止。点“重新载入预览”回到这个文件。'))));
      reload.disabled = false;
    }
    shown = sig;
  }

  return {
    el,
    show(nextName, nextText, options = {}) {
      if (nextName !== name) { chosen = null; shown = ''; }
      name = nextName;
      text = String(nextText === null || nextText === undefined ? '' : nextText);
      live = !!options.live;
      draw(false);
    },
    /** A line in place of the content: loading, or a failure. */
    message(nextName, messageText, error = false) {
      if (nextName !== name) chosen = null;
      name = nextName;
      text = null;
      draw(false);
      note(messageText, error);
    },
  };
}

/** Opens a full-size preview in a sheet and returns its viewer; call show() on it once the text is there. */
export function openPreview(name) {
  const pane = viewer({ expand: false });
  pane.message(name, '正在读取…');
  sheet(name, pane.el, { wide: true });
  const panel = pane.el.closest('.sheet');
  if (panel) panel.classList.add('pv-sheet');
  return pane;
}
