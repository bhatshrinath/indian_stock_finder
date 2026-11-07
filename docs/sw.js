/* Offline-friendly service worker: app shell cached, data always fetched fresh
   (falls back to the last cached copy when offline). */
const CACHE = "sf-v9";
const SHELL = ["./", "index.html", "app.css", "app.js", "icon.svg", "manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  // network first for everything (fresh data and code), cache as fallback
  e.respondWith(
    fetch(e.request).then((r) => {
      const copy = r.clone();
      const key = url.pathname.includes("/data/") ? new Request(url.origin + url.pathname) : e.request;
      caches.open(CACHE).then((c) => c.put(key, copy));
      return r;
    }).catch(() => caches.match(url.pathname.includes("/data/") ? new Request(url.origin + url.pathname) : e.request))
  );
});
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window" }).then((cs) => (cs[0] ? cs[0].focus() : self.clients.openWindow("./"))));
});
