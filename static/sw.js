// danran service worker — /app/static/sw.js
// Streamlit Cloud の静的ファイル配信 (enableStaticServing) 経由で配信される。
// scope: '/app/static/' で登録されるため fetch インターセプトは行わない。
// push 受信・バッジ更新・通知タップに特化した SW。
var SW_VERSION = '2.0.0';

self.addEventListener('install', function (event) {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(clients.claim());
});

// ── クライアント（コンポーネント）からのバッジ設定メッセージ ─────────────
self.addEventListener('message', function (event) {
  if (!event.data) return;
  var type = event.data.type;
  if (type !== 'danran-set-badge' && type !== 'danran-clear-badge') return;

  var count = (type === 'danran-clear-badge') ? 0 : (event.data.count || 0);
  try {
    var nav = self.navigator || navigator;
    if (!('setAppBadge' in nav)) return;
    (count > 0 ? nav.setAppBadge(count) : nav.clearAppBadge())
      .catch(function () {});
  } catch (e) {}
});

// ── プッシュ通知受信 ────────────────────────────────────────────────────────
self.addEventListener('push', function (event) {
  if (!event.data) return;

  var data = {};
  try { data = event.data.json(); } catch (e) { data = { body: event.data.text() }; }

  var title       = data.title       || 'danran 🏠';
  var body        = data.body        || '新しいメッセージがあります';
  var icon        = data.icon        || '/app/static/icons/icon-192.png';
  var tag         = data.room        || 'danran';
  var destUrl     = data.url         || '/';
  var unreadCount = data.unread_count;

  var notifPromise = self.registration.showNotification(title, {
    body:     body,
    icon:     icon,
    badge:    '/app/static/icons/badge.png',
    tag:      tag,
    renotify: true,
    silent:   false,
    data:     { url: destUrl },
  });

  var badgePromise = Promise.resolve();
  try {
    var nav = self.navigator || navigator;
    if ('setAppBadge' in nav) {
      badgePromise = (unreadCount !== undefined)
        ? nav.setAppBadge(unreadCount).catch(function () {})
        : nav.setAppBadge().catch(function () {});
    }
  } catch (e) {}

  event.waitUntil(Promise.all([notifPromise, badgePromise]));
});

// ── 通知タップ → アプリを前面に ────────────────────────────────────────────
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
