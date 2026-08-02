// __APP_VERSION__ is substituted by the /sw.js route at request time, so the
// cache name moves with each release and `activate` clears the previous one.
const VERSION = '__APP_VERSION__';
const CACHE = 'qobuz-librarian-' + VERSION;
const PRECACHE = [
  '/static/dist/app.css?v=' + VERSION,
  '/static/app.js?v=' + VERSION,
  '/static/vendor/htmx-2.0.4.min.js',
  '/static/vendor/inter/inter-latin.woff2',
  '/static/vendor/inter/inter-latin-ext.woff2',
  // The offline page's heading face and theme script — everything it names
  // must be in this list or it renders half-styled at the one moment it runs.
  '/static/vendor/fraunces/fraunces-latin-600.woff2',
  '/static/offline.js',
  '/static/icon.png',
  '/static/icon-192.png',
  '/static/manifest.json',
  '/static/offline.html',
];

self.addEventListener('install', event => {
  // skipWaiting is chained inside waitUntil so a failed precache aborts the
  // install rather than activating a worker with a half-populated cache.
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(PRECACHE))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // SSE streams and API calls: always network — never cache dynamic data.
  if (url.pathname.startsWith('/api/')) return;

  // Static assets: cache-first; populate cache on first miss. Versioned URLs
  // (?v=) mean a release is a fresh cache key, so this never serves stale CSS.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(event.request).then(hit => {
        if (hit) return hit;
        return fetch(event.request).then(response => {
          // Don't store a 404/5xx — it would be served from cache until the
          // next version bump. Hand the response back without caching it.
          if (!response || !response.ok) return response;
          const clone = response.clone();
          caches.open(CACHE).then(cache => cache.put(event.request, clone));
          return response;
        });
      })
    );
    return;
  }

  // Page navigations: network-first; fall back to offline page when the
  // server is unreachable (container stopped, network down).
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(() => caches.match('/static/offline.html'))
    );
  }
});
