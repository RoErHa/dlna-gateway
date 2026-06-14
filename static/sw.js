const APP_CACHE = 'dlna-gw-app-v12';  // v12: video transcode fallback (HEVC/MKV in browser)
const ART_CACHE = 'dlna-gw-art-v2';   // v2: evict stale blanks cached during gateway-down / pre-heal windows
const API_CACHE = 'dlna-gw-api-v1';   // stable browse GETs (stale-while-revalidate)

// GET endpoints that return STABLE browse data (change only on re-index /
// metadata edits). Cached stale-while-revalidate so repeat navigation is
// instant over a slow link. Everything NOT listed — /api/state, /servers,
// /renderers, /index/status, /acoustid/status, /album_favourites (user-
// mutated), /track_meta, /radio/* — stays network-only (always fresh).
const CACHEABLE_API = [
  '/api/browse_letter', '/api/album_tracks', '/api/artist_albums',
  '/api/artist_tracks', '/api/albums', '/api/search', '/api/genres',
  '/api/genre_albums', '/api/genre_tracks', '/api/decades',
  '/api/decade_albums', '/api/decade_tracks',
];

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
  const keep = new Set([APP_CACHE, ART_CACHE, API_CACHE]);
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

  // Stable browse data — stale-while-revalidate: serve the cached copy
  // instantly (snappy over a slow tailnet) AND fetch in the background to
  // refresh it. The background fetch always hits the network, so a stale
  // entry self-corrects on the next view. Only the CACHEABLE_API allowlist
  // qualifies; all other /api/* falls through to network-only below.
  if (event.request.method === 'GET' &&
      CACHEABLE_API.some(p => url.pathname.startsWith(p))) {
    event.respondWith(
      caches.open(API_CACHE).then(cache =>
        cache.match(event.request).then(cached => {
          const network = fetch(event.request).then(resp => {
            if (resp.ok) cache.put(event.request, resp.clone());
            return resp;
          }).catch(() => cached);
          return cached || network;
        })
      )
    );
    return;
  }

  // Never intercept (live) API calls, streams, or POST requests
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