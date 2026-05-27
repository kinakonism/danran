// danran service worker — push notifications + badge suppression
// バージョンを上げると古いキャッシュが破棄される
var SW_VERSION = '1.3.1';

self.addEventListener('install', function (event) {
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(clients.claim());
});

// ── クライアント（コンポーネント iframe）からのバッジ設定メッセージ受信 ───────
// iframe 内から navigator.setAppBadge() を呼ぶと Permissions Policy で拒否される
// ことがあるため、クライアントが SW に postMessage してバッジ更新を依頼する。
// SW は TopLevel コンテキストのワーカーとして扱われ API 呼び出しが確実に通る。
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

// ── ページ初期ロード時に Streamlit Cloud バッジ非表示 CSS を注入 ──────────
// Streamlit Cloud の外側シェル HTML に CSS を挿入することで、
// Python コードが動く前（最も早い段階）にバッジが非表示になる。
// SW がインストール済みのセッション（= PWA として使っている全ケース）で有効。
self.addEventListener('fetch', function (event) {
  // ナビゲーション（ページ初期ロード）のみ処理
  // Streamlit 内部の /~/+/ サブフレームは触らない
  if (event.request.mode !== 'navigate') return;
  var url = event.request.url;
  if (url.indexOf('/~/+/') >= 0) return;

  event.respondWith(
    fetch(event.request)
      .then(function (res) {
        var ct = res.headers.get('content-type') || '';
        // HTML 以外はそのまま返す
        if (!ct.includes('text/html')) return res;

        return res.text().then(function (html) {
          // Streamlit Cloud が React で注入するバッジのクラスを狙い撃ち
          // クラス名はハッシュ付きなので部分一致（[class*=]）で消す
          var css =
            '<style id="_danran_nobadge">' +
            '[class*="viewerBadge"]{display:none!important}' +
            '[class*="hostedWith"]{display:none!important}' +
            '[class*="HostedWith"]{display:none!important}' +
            '[class*="createdBy"]{display:none!important}' +
            '[class*="CreatedBy"]{display:none!important}' +
            'a[href*="streamlit.io"]{display:none!important}' +
            '</style>';

          // ── アプリバッジ用ヘルパーをトップレベルHTMLに注入 ──────────
          // setAppBadge は「トップレベル閲覧コンテキスト」からしか呼べない仕様。
          // iframe (Streamlit コンポーネント) からは NotAllowedError になるため、
          // SW のフェッチインターセプターでページ初回ロード時に注入する。
          // コンポーネントは window.top._danranSetBadge(n) 経由でこの関数を呼ぶ。
          var badgeScript =
            '<script id="_danran_badge_fn">window._danranSetBadge=function(n){' +
            'try{if(!("setAppBadge"in navigator))return;' +
            '(n>0?navigator.setAppBadge(n):navigator.clearAppBadge()).catch(function(){});}' +
            'catch(e){}' +
            '};<\/script>';

          // </head> の直前に挿入（なければ先頭に付ける）
          var inject = css + badgeScript;
          var modified = html.indexOf('</head>') >= 0
            ? html.replace('</head>', inject + '</head>')
            : inject + html;

          // Content-Length はバイト数が変わるので除去必須
          var headers = new Headers(res.headers);
          headers.delete('content-length');
          return new Response(modified, {
            status: res.status,
            statusText: res.statusText,
            headers: headers,
          });
        });
      })
      .catch(function () {
        // ネットワークエラーはそのままフォールバック
        return fetch(event.request);
      })
  );
});

// ── プッシュ通知受信 ──────────────────────────────────
self.addEventListener('push', function (event) {
  if (!event.data) return;

  var data = {};
  try { data = event.data.json(); } catch (e) { data = { body: event.data.text() }; }

  var title        = data.title        || 'danran 🏠';
  var body         = data.body         || '新しいメッセージがあります';
  var icon         = data.icon         || '/icons/icon-192.png';
  var tag          = data.room         || 'danran';
  var destUrl      = data.url          || '/';
  var unreadCount  = data.unread_count;   // Python が受信者別の未読数を送る

  // ── 通知表示 + バッジ即時更新 ───────────────────────
  var notifPromise = self.registration.showNotification(title, {
    body:     body,
    icon:     icon,
    badge:    '/icons/badge.png',
    tag:      tag,
    renotify: true,
    silent:   false,
    data:     { url: destUrl },
  });

  // navigator.setAppBadge は ServiceWorker でも使用可能（iOS 16.4+ / Chrome PWA）
  var badgePromise = Promise.resolve();
  try {
    var nav = self.navigator || navigator;
    if ('setAppBadge' in nav) {
      badgePromise = (unreadCount !== undefined)
        ? nav.setAppBadge(unreadCount).catch(function () {})
        : nav.setAppBadge().catch(function () {});        // dot badge（数字なし）
    }
  } catch (e) {}

  event.waitUntil(Promise.all([notifPromise, badgePromise]));
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
