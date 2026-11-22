// Jurvis service worker: caches the app shell only, network-first so updates arrive; the cache is the offline fallback.
// API calls (/api/, /pair, …) are never handled here.
const CACHE = 'jurvis-shell-v15';
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
  './health.js',
  './oauth.js',
  './settings.js',
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
