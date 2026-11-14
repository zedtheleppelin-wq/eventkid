/* Service Worker: App läuft auch offline. Termine (events.json) werden immer zuerst frisch geladen. */
const CACHE = "kinderkalender-v1";
const SCHALE = ["./", "index.html", "manifest.json", "icon.svg", "icon-192.png", "icon-512.png", "events.json"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SCHALE)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  const eigene = url.origin === location.origin;
  const daten = eigene && url.pathname.endsWith("events.json");
  if (daten || req.mode === "navigate") {                 // Netz zuerst, sonst Zwischenspeicher
    e.respondWith(fetch(req).then(r => {
      const kopie = r.clone(); caches.open(CACHE).then(c => c.put(req, kopie)); return r;
    }).catch(() => caches.match(req).then(r => r || caches.match("index.html"))));
    return;
  }
  e.respondWith(caches.match(req).then(r => r || fetch(req).then(n => {   // Rest: Zwischenspeicher zuerst (auch Schriften)
    if (eigene || url.hostname.includes("fonts.g")) { const kopie = n.clone(); caches.open(CACHE).then(c => c.put(req, kopie)); }
    return n;
  }).catch(() => r)));
});
