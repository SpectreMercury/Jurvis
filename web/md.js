// A small, safe Markdown subset rendered straight to DOM nodes (never innerHTML):
// paragraphs, headings, code fences, inline code, bold, bullet/numbered lists, and http(s) links.

const LINK = /^\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/;
const BARE = /^https?:\/\/[^\s<>"'）】》，。；]+/;

function link(text, url) {
  const a = document.createElement('a');
  let safe = null;
  try {
    const parsed = new URL(url);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') safe = parsed.href;
  } catch { /* not a URL */ }
  if (!safe) return document.createTextNode(text);
  a.href = safe;
  a.target = '_blank';
  a.rel = 'noopener noreferrer';
  a.textContent = text;
  return a;
}

/** Inline: `code`, **bold**, [text](http…), bare http(s) URLs. Everything else is text. */
export function inline(text, parent) {
  let plain = '';
  const flush = () => {
    if (plain) { parent.append(document.createTextNode(plain)); plain = ''; }
  };
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === '`') {
      const end = text.indexOf('`', i + 1);
      if (end > i + 1) {
        flush();
        const code = document.createElement('code');
        code.textContent = text.slice(i + 1, end);
        parent.append(code);
        i = end + 1;
        continue;
      }
    } else if (ch === '*' && text[i + 1] === '*') {
      const end = text.indexOf('**', i + 2);
      if (end > i + 2) {
        flush();
        const strong = document.createElement('strong');
        inline(text.slice(i + 2, end), strong);
        parent.append(strong);
        i = end + 2;
        continue;
      }
    } else if (ch === '[') {
      const match = LINK.exec(text.slice(i));
      if (match) {
        flush();
        parent.append(link(match[1], match[2]));
        i += match[0].length;
        continue;
      }
    } else if (ch === 'h' && (i === 0 || !/[\w/]/.test(text[i - 1]))) {
      const match = BARE.exec(text.slice(i));
      if (match) {
        const url = match[0].replace(/[.,;:!?)\]]+$/, '');
        flush();
        parent.append(link(url, url));
        i += url.length;
        continue;
      }
    }
    plain += ch;
    i += 1;
  }
  flush();
}

const BULLET = /^\s{0,6}[-*+•]\s+(.*)$/;
const NUMBER = /^\s{0,6}\d{1,3}[.)]\s+(.*)$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const FENCE = /^\s{0,3}(```|~~~)\s*([\w+#.-]*)\s*$/;

export function renderMarkdown(source) {
  const out = document.createDocumentFragment();
  const lines = String(source || '').replace(/\r\n?/g, '\n').split('\n');
  let paragraph = [];
  const flush = () => {
    if (!paragraph.length) return;
    const p = document.createElement('p');
    paragraph.forEach((line, index) => {
      if (index) p.append(document.createElement('br'));
      inline(line, p);
    });
    out.append(p);
    paragraph = [];
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = FENCE.exec(line);
    if (fence) {
      flush();
      const body = [];
      i += 1;
      // An unclosed fence (still streaming) runs to the end of the text.
      while (i < lines.length && !(lines[i].trim() === fence[1])) { body.push(lines[i]); i += 1; }
      i += 1;
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      code.textContent = body.join('\n');
      if (fence[2]) pre.dataset.lang = fence[2];
      pre.append(code);
      out.append(pre);
      continue;
    }
    if (!line.trim()) { flush(); i += 1; continue; }
    const heading = HEADING.exec(line);
    if (heading) {
      flush();
      const el = document.createElement('p');
      el.className = 'md-heading';
      const strong = document.createElement('strong');
      inline(heading[2], strong);
      el.append(strong);
      out.append(el);
      i += 1;
      continue;
    }
    const kind = BULLET.test(line) ? BULLET : NUMBER.test(line) ? NUMBER : null;
    if (kind) {
      flush();
      const list = document.createElement(kind === BULLET ? 'ul' : 'ol');
      let item = null;
      while (i < lines.length) {
        const match = kind.exec(lines[i]);
        if (match) {
          item = document.createElement('li');
          inline(match[1], item);
          list.append(item);
        } else if (item && lines[i].trim() && /^\s{2,}/.test(lines[i]) && !FENCE.test(lines[i])) {
          item.append(document.createElement('br'));
          inline(lines[i].trim(), item);
        } else break;
        i += 1;
      }
      out.append(list);
      continue;
    }
    paragraph.push(line);
    i += 1;
  }
  flush();
  return out;
}
