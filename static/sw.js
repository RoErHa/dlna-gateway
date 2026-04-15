const APP_CACHE = 'dlna-gw-app-v3';
const ART_CACHE = 'dlna-gw-art-v1';

const SHELL = [
  '/',
  '/static/app.css',
  '/static/app.js',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png'
];

// ── Install: pre-cache app shell ────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(APP_CACHE)
      .then(cache => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

// ── Activate: clean old caches ──────────────────────────────────
self.addEventListener('activate', event => {
  const keep = new Set([APP_CACHE, ART_CACHE]);
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => !keep.has(k)).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ── Fetch: route by request type ────────────────────────────────
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Never intercept API calls, streams, or POST requests
  if (url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/stream') ||
      url.pathname.startsWith('/cd/') ||
      event.request.method !== 'GET') {
    return;
  }

  // Album art images — cache-first (images rarely change)
  if (url.pathname === '/art' ||
      (event.request.destination === 'image' && url.origin !== self.location.origin)) {
    event.respondWith(
      caches.open(ART_CACHE).then(cache =>
        cache.match(event.request).then(cached => {
          if (cached) return cached;
          return fetch(event.request).then(resp => {
            if (resp.ok) cache.put(event.request, resp.clone());
            return resp;
          }).catch(() => cached || new Response('', { status: 404 }));
        })
      )
    );
    return;
  }

  // App shell & static assets — stale-while-revalidate
  event.respondWith(
    caches.open(APP_CACHE).then(cache =>
      cache.match(event.request).then(cached => {
        const network = fetch(event.request).then(resp => {
          if (resp.ok) cache.put(event.request, resp.clone());
          return resp;
        }).catch(() => cached);
        return cached || network;
      })
    )
  );
});