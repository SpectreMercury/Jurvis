// Jurvis service worker: caches the app shell only, network-first so updates arrive; the cache is the offline fallback.
// API calls (/api/, /pair, …) are never handled here.
// It also shows what the engine pushes while no page is open (engine/push.py) and opens the conversation when a notification is tapped.
const CACHE = 'jurvis-shell-v20';
const SHELL = [
  './',
  './index.html',
  './app.css',
  './app.js',
  './core.js',
  './md.js',
  './chat.js',
  './icons.js',
  './ui.js',
  './connect.js',
  './brands.js',
  './preview.js',
  './voice.js',
  './library.js',
  './home.js',
  './memory.js',
  './goals.js',
  './ledger.js',
  './journal.js',
  './health.js',
  './oauth.js',
  './settings.js',
  './reach.js',
  './manifest.webmanifest',
  './icon.svg',
  './icon-192.png',
  './icon-512.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    await Promise.all(SHELL.map((path) => cache.add(new Request(path, { cache: 'reload' })).catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name !== CACHE && name.startsWith('jurvis-shell-')) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  const scope = new URL(self.registration.scope);
  if (url.origin !== scope.origin || !url.pathname.startsWith(scope.pathname) || url.pathname.includes('/api/')) return;
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const response = await fetch(request, { cache: 'no-cache' });
      if (response.ok && response.type === 'basic') cache.put(request, response.clone()).catch(() => {});
      return response;
    } catch (err) {
      const cached = (await cache.match(request, { ignoreSearch: true })) || (request.mode === 'navigate' ? await cache.match('./index.html') : null);
      if (cached) return cached;
      throw err;
    }
  })());
});

self.addEventListener('push', (event) => {
  let told = {};
  try { told = event.data ? event.data.json() : {}; } catch { told = {}; }
  // The tag is the notice's id: the page announcing the same notice later replaces this one instead of adding a second.
  event.waitUntil(self.registration.showNotification(told.title || 'Jurvis', {
    body: told.body || '', tag: told.id || undefined, icon: './icon-192.png', badge: './icon-192.png', data: told,
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  event.waitUntil((async () => {
    const pages = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    const open = pages.find((page) => page.url.startsWith(self.registration.scope));
    if (open) {
      await open.focus();
      open.postMessage({ type: 'jurvis-open-chat' });
      return;
    }
    await self.clients.openWindow('./');
  })());
});
