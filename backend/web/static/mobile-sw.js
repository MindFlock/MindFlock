// The phone page's notifications (mobile.js, ASK section): Android Chrome shows
// a page's notification only through a service worker, and the worker is also
// what hears the tap. No fetch handler — the page stays a plain page; this
// file only exists so a notification can be shown and its tap can open the
// asking session's sheet.
self.addEventListener("install", function () {
  self.skipWaiting();
});
self.addEventListener("activate", function (e) {
  e.waitUntil(self.clients.claim());
});
self.addEventListener("notificationclick", function (e) {
  e.notification.close();
  var title = (e.notification.data && e.notification.data.title) || "";
  e.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        var u = new URL(c.url);
        if (/\/m(\?|$)|\/mobile\.html/.test(u.pathname + u.search)) {
          c.postMessage({ type: "mf-ask-open", title: title });
          return c.focus();
        }
      }
      return self.clients.openWindow("/m" + (title ? "?s=" + encodeURIComponent(title) : ""));
    })
  );
});
