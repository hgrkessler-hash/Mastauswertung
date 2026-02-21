
self.addEventListener("install", e => {
  e.waitUntil(
    caches.open("mast-cache").then(cache => cache.addAll(["/"]))
  );
});
