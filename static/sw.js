/* 智护银伴 Service Worker v23 · SPA cache cleanup */
const CACHE_NAME = 'zhihu-v23-spa-cache-cleanup';
const STATIC_ASSETS = [
  '/',
  '/nurse',
  '/static/nurse.html',
  '/static/manifest.json',
  '/static/design/tokens.css',
  '/static/design/glass.css',
  '/static/design/ui.css',
  '/static/design/mobile.css',
  '/static/design/ambient.svg',
  '/static/design/icons.js',
  '/static/design/dialog.js',
  '/static/design/evidence.js',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
];

const LEGACY_ENTRYPOINTS = new Set([
  '/static/index.html',
  '/legacy',
  '/legacy/',
]);

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache =>
      cache.addAll(STATIC_ASSETS).catch(() => {})
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // 旧版入口不再由 SW 离线缓存兜底，强制交给服务端返回真实 404/重定向。
  if (url.origin === self.location.origin && LEGACY_ENTRYPOINTS.has(url.pathname)) {
    event.respondWith(fetch(event.request));
    return;
  }

  // API 请求：网络优先，离线返回提示
  if (url.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(event.request).catch(() =>
        new Response(
          JSON.stringify({ code: 503, message: '当前处于离线状态，无法请求 AI 服务' }),
          { status: 503, headers: { 'Content-Type': 'application/json' } }
        )
      )
    );
    return;
  }

  // 外部资源（字体等）：网络优先，失败从缓存
  if (url.origin !== self.location.origin) {
    event.respondWith(
      fetch(event.request)
        .then(res => {
          const clone = res.clone();
          caches.open(CACHE_NAME).then(c => c.put(event.request, clone));
          return res;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // 页面 & 静态资源：Stale-While-Revalidate
  event.respondWith(
    caches.open(CACHE_NAME).then(cache =>
      cache.match(event.request).then(cached => {
        const fetchPromise = fetch(event.request).then(res => {
          if (res && res.status === 200 && !LEGACY_ENTRYPOINTS.has(url.pathname)) {
            cache.put(event.request, res.clone());
          }
          return res;
        }).catch(() => null);
        return cached || fetchPromise;
      })
    )
  );
});
