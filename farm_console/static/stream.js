// The browser allows six HTTP/1.1 connections per host, and a page kept in the
// back/forward cache holds its /stream open, so a few quick clicks between
// console pages stall the next one until the cache evicts a page. Close the
// stream when a page is left, and reload one the cache restores, whose data is
// stale and whose stream is closed.
(() => {
  const sources = new Set();
  htmx.createEventSource = (url) => {
    const source = new EventSource(url);
    sources.add(source);
    return source;
  };
  addEventListener("pagehide", () => {
    sources.forEach((s) => s.close());
    sources.clear();
  });
  addEventListener("pageshow", (e) => { if (e.persisted) location.reload(); });
})();
