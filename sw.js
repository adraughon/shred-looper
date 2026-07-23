/* Shred Looper service worker — stale-while-revalidate keyed on build. */
const CACHE = 'sl-0f8fbad134';
const PRECACHE = ['./', './index.html', './manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(PRECACHE)));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  e.respondWith(
    caches.open(CACHE).then(async (c) => {
      const cached = await c.match(e.request);
      const network = fetch(e.request)
        .then((resp) => {
          if (resp && resp.ok) c.put(e.request, resp.clone());
          return resp;
        })
        .catch(() => cached);
      return cached || network; // serve cache instantly, refresh behind
    })
  );
});
