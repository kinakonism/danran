// danran service worker — push notifications
// バージョンを上げると古いキャッシュが破棄される
var SW_VERSION = '1.0.0';

self.addEventListener('install', function (event) {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(clients.claim());
});

// ── プッシュ通知受信 ──────────────────────────────────
self.addEventListener('push', function (event) {
  if (!event.data) return;

  var data = {};
  try { data = event.data.json(); } catch (e) { data = { body: event.data.text() }; }

  var title   = data.title  || 'danran 🏠';
  var body    = data.body   || '新しいメッセージがあります';
  var icon    = data.icon   || '/icons/icon-192.png';
  var badge   = '/icons/badge.png';
  var tag     = data.room   || 'danran';
  var destUrl = data.url    || '/';

  event.waitUntil(
    self.registration.showNotification(title, {
      body:       body,
      icon:       icon,
      badge:      badge,
      tag:        tag,
      renotify:   true,
      silent:     false,
      data:       { url: destUrl },
    })
  );
});

// ── 通知タップ → アプリを前面に ──────────────────────
self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if ('focus' in c) return c.focus();
      }
      if (clients.openWindow) return clients.openWindow('/');
    })
  );
});
