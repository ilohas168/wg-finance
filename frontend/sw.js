// Service worker: lets the app open offline from the home screen.
// App files are loaded from the network first, so a new version shows up on
// the very next launch; the cached copy is only used when offline.
// API calls are never cached.
const CACHE = "wg-finance-v11";
const SHELL = ["/", "/index.html", "/app.js", "/styles.css", "/manifest.webmanifest", "/icons/icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  const key = event.request.mode === "navigate" ? "/index.html" : event.request;
  event.respondWith(
    fetch(event.request, { cache: "no-cache" })
      .then((res) => {
        if (res.ok) {
          const copy = res.clone(); // clone now, before the page reads the body
          caches.open(CACHE).then((cache) => cache.put(key, copy));
        }
        return res;
      })
      .catch(() => caches.match(key)),
  );
});
