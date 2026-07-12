const APP_CACHE = 'dlna-gw-app-v16';  // v16: Videos location browse groups country → location
const ART_CACHE = 'dlna-gw-art-v2';   // v2: evict stale blanks cached during gateway-down / pre-heal windows
const API_CACHE = 'dlna-gw-api-v1';   // stable browse GETs (stale-while-revalidate)

// GET endpoints that return STABLE browse data (change only on re-index /
// metadata edits). Cached stale-while-revalidate so repeat navigation is
// instant over a slow link. Everything NOT listed — /api/state, /servers,
// /renderers, /index/status, /album_favourites (user-mutated),
// /track_meta, /radio/* — stays network-only (always fresh).
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
  '/static/vendor/hls.min.js',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png'
];

// ── Install: pre-cache app shell ────────────────────────────────
self.addEventListener('install', event => {
  // skipWaiting() UNCONDITIONALLY and first. Previously it was chained AFTER
  // cache.addAll(SHELL) — and addAll is atomic, so a single failing shell
  // entry rejected the chain, skipWaiting() never ran, and the new worker sat
  // in 'waiting' forever (a waiting SW only activates once ALL old tabs close).
  // A plain refresh then never updated; only "clear site data" fixed it
  // (2026-06-27). Precache is now best-effort and can never block activation.
  self.skipWaiting();
  event.waitUntil(
    caches.open(APP_CACHE)
      .then(cache => cache.addAll(SHELL))
      .catch(() => {})   // best-effort — a missing shell entry must not wedge the SW
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

  // HTML navigation (the document) — NETWORK-FIRST. The shell used to be
  // served stale-while-revalidate (cached-first), so a once-broken/empty
  // cached '/' pinned the app blank on every load ("full UI, no content",
  // 2026-06-27). Network-first means an online load always gets the fresh
  // document; the cache is only a fallback when the network is unreachable
  // (offline). app.js/app.css stay versioned via APP_CACHE below.
  if (event.request.mode === 'navigate' || url.pathname === '/') {
    event.respondWith(
      fetch(event.request)
        .then(resp => {
          if (resp.ok) {
            const copy = resp.clone();
            caches.open(APP_CACHE).then(c => c.put(event.request, copy));
          }
          return resp;
        })
        .catch(() => caches.open(APP_CACHE).then(c => c.match(event.request))
                       .then(cached => cached || caches.match('/')))
    );
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

  // App shell & static assets (app.js / app.css / icons / manifest) —
  // NETWORK-FIRST. Same rationale as the navigation document above: a
  // poisoned/broken cached app.js used to pin the whole app blank with no way
  // to recover by refresh (2026-06-27). An online load must always get the
  // fresh asset; the cache is the OFFLINE fallback only. (Data SWR for the
  // CACHEABLE_API allowlist and cache-first /art above are unchanged — those
  // can't blank the app, and that's where the real speed win is.)
  event.respondWith(
    fetch(event.request)
      .then(resp => {
        if (resp.ok) {
          const copy = resp.clone();
          caches.open(APP_CACHE).then(c => c.put(event.request, copy));
        }
        return resp;
      })
      .catch(() => caches.open(APP_CACHE).then(c => c.match(event.request)))
  );
});