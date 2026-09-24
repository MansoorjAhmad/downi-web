/* DOWNI Web service worker — offline app shell.
 * Navigations: network-first (fresh deploys win), cached shell as fallback.
 * Static assets: cache-first. /api/* always goes to the network. */
const CACHE = 'downi-web-v2.1';
const SHELL = [
  '/',
  '/index.html',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png',
  '/icon-maskable-512.png',
  '/apple-touch-icon.png',
  '/logo.png',
  '/og.png',
  '/assets/vendor/tailwind.js',
  '/assets/vendor/PlusJakartaSans.woff2',
  '/assets/vendor/JetBrainsMono.woff2',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;

  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request)
        .then((resp) => {
          if (resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put('/index.html', copy));
          }
          return resp;
        })
        .catch(() => caches.match('/index.html'))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then(
      (hit) =>
        hit ||
        fetch(event.request)
          .then((resp) => {
            if (resp.ok && url.origin === self.location.origin) {
              const copy = resp.clone();
              caches.open(CACHE).then((c) => c.put(event.request, copy));
            }
            return resp;
          })
          .catch(() => caches.match('/index.html'))
    )
  );
});
