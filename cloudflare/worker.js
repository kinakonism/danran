/**
 * danran Cloudflare Worker
 *
 * 役割:
 *   - /sw.js /manifest.json /icons/* を直接配信（PWA 実現）
 *   - それ以外のリクエストを Streamlit Cloud にリバースプロキシ
 *   - WebSocket（Streamlit のリアルタイム通信）も透過的にプロキシ
 *
 * デプロイ先: Cloudflare Workers（無料プラン）
 * アクセス URL: https://danran.<あなたのCFアカウント>.workers.dev
 */

const STREAMLIT_HOST   = 'danran-dhawa6nhapcwnq6lrjqzhw.streamlit.app';
const STREAMLIT_ORIGIN = 'https://' + STREAMLIT_HOST;
const GITHUB_RAW_ICONS = 'https://raw.githubusercontent.com/kinakonism/danran/main/static/icons';

// ── Service Worker スクリプト（プッシュ通知・バッジ専用）────────────────
const SW_JS = `
var SW_VERSION = '3.0.0';
self.addEventListener('install', function(e) { self.skipWaiting(); });
self.addEventListener('activate', function(e) { e.waitUntil(clients.claim()); });

// コンポーネントからバッジ更新依頼を受け取る
self.addEventListener('message', function(event) {
  if (!event.data) return;
  var type = event.data.type;
  if (type !== 'danran-set-badge' && type !== 'danran-clear-badge') return;
  var count = (type === 'danran-clear-badge') ? 0 : (event.data.count || 0);
  try {
    var nav = self.navigator || navigator;
    if (!('setAppBadge' in nav)) return;
    (count > 0 ? nav.setAppBadge(count) : nav.clearAppBadge()).catch(function(){});
  } catch(e) {}
});

// プッシュ通知受信
self.addEventListener('push', function(event) {
  if (!event.data) return;
  var data = {};
  try { data = event.data.json(); } catch(e) { data = { body: event.data.text() }; }
  var title       = data.title       || 'danran 🏠';
  var body        = data.body        || '新しいメッセージがあります';
  var icon        = data.icon        || '/icons/icon-192.png';
  var tag         = data.room        || 'danran';
  var destUrl     = data.url         || '/';
  var unreadCount = data.unread_count;
  var notifPromise = self.registration.showNotification(title, {
    body: body, icon: icon, badge: '/icons/badge.png',
    tag: tag, renotify: true, silent: false, data: { url: destUrl },
  });
  var badgePromise = Promise.resolve();
  try {
    var nav = self.navigator || navigator;
    if ('setAppBadge' in nav) {
      badgePromise = (unreadCount !== undefined)
        ? nav.setAppBadge(unreadCount).catch(function(){})
        : nav.setAppBadge().catch(function(){});
    }
  } catch(e) {}
  event.waitUntil(Promise.all([notifPromise, badgePromise]));
});

// 通知タップ → アプリを前面に
self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(list) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i]; if ('focus' in c) return c.focus();
      }
      if (clients.openWindow) return clients.openWindow('/');
    })
  );
});
`.trim();

// ── Web App Manifest ──────────────────────────────────────────────────
const MANIFEST_JSON = JSON.stringify({
  name:             'danran — 家族チャット',
  short_name:       'danran',
  description:      '家族専用プライベートチャット',
  lang:             'ja',
  start_url:        '/',
  display:          'standalone',
  orientation:      'portrait',
  background_color: '#1a1a2e',
  theme_color:      '#1a1a2e',
  icons: [
    { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any maskable' },
    { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any maskable' },
  ],
});

// ── メインハンドラ ────────────────────────────────────────────────────
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // /sw.js — Service Worker 本体を直接配信
    if (url.pathname === '/sw.js') {
      return new Response(SW_JS, {
        headers: {
          'Content-Type':           'application/javascript; charset=utf-8',
          'Service-Worker-Allowed': '/',
          'Cache-Control':          'no-store, no-cache, must-revalidate',
        },
      });
    }

    // /manifest.json — PWA マニフェストを直接配信
    if (url.pathname === '/manifest.json') {
      return new Response(MANIFEST_JSON, {
        headers: {
          'Content-Type':              'application/manifest+json; charset=utf-8',
          'Cache-Control':             'no-store',
          'Access-Control-Allow-Origin': '*',
        },
      });
    }

    // /icons/* — GitHub リポジトリの static/icons/ から配信
    if (url.pathname.startsWith('/icons/')) {
      const filename = url.pathname.slice('/icons/'.length);
      if (!filename || filename.includes('..')) {
        return new Response('Not Found', { status: 404 });
      }
      const iconUrl = `${GITHUB_RAW_ICONS}/${filename}`;
      const res = await fetch(iconUrl);
      if (!res.ok) return new Response('Not Found', { status: 404 });
      const h = new Headers();
      h.set('Content-Type',               res.headers.get('Content-Type') || 'image/png');
      h.set('Cache-Control',              'public, max-age=86400');
      h.set('Access-Control-Allow-Origin', '*');
      return new Response(res.body, { status: 200, headers: h });
    }

    // WebSocket — Streamlit リアルタイム通信をプロキシ
    const upgrade = request.headers.get('Upgrade');
    if (upgrade && upgrade.toLowerCase() === 'websocket') {
      return handleWebSocket(request, url);
    }

    // その他 HTTP — Streamlit Cloud にプロキシ
    return proxyHttp(request, url);
  },
};

// ── HTTP プロキシ ─────────────────────────────────────────────────────
async function proxyHttp(request, url) {
  const upstream = new URL(url.pathname + url.search, STREAMLIT_ORIGIN);

  const headers = new Headers(request.headers);
  headers.set('Host', STREAMLIT_HOST);
  // Cloudflare が付与する CF 固有ヘッダーは除去（Streamlit が混乱しないように）
  headers.delete('cf-connecting-ip');
  headers.delete('cf-ipcountry');
  headers.delete('cf-ray');
  headers.delete('cf-visitor');

  const res = await fetch(upstream.toString(), {
    method:   request.method,
    headers,
    body:     ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
    redirect: 'manual',
  });

  const newHeaders = new Headers(res.headers);

  // リダイレクト先 URL をワーカードメインに書き換え
  const location = res.headers.get('Location');
  if (location) {
    try {
      const locUrl    = new URL(location, STREAMLIT_ORIGIN);
      locUrl.hostname = url.hostname;
      locUrl.protocol = url.protocol;
      locUrl.port     = '';
      newHeaders.set('Location', locUrl.toString());
    } catch (_) {}
  }

  return new Response(res.body, {
    status:     res.status,
    statusText: res.statusText,
    headers:    newHeaders,
  });
}

// ── WebSocket プロキシ ────────────────────────────────────────────────
async function handleWebSocket(request, url) {
  const wsPath      = url.pathname + url.search;
  const upstreamUrl = `wss://${STREAMLIT_HOST}${wsPath}`;

  // Cloudflare の WebSocket ペア（client = ブラウザ側, server = ワーカー内部）
  const [client, server] = Object.values(new WebSocketPair());

  // Streamlit へのアウトバウンド WebSocket 接続
  const upstreamRes = await fetch(upstreamUrl, {
    headers: {
      'Host':       STREAMLIT_HOST,
      'Origin':     STREAMLIT_ORIGIN,
      'Upgrade':    'websocket',
      'Connection': 'Upgrade',
    },
  });

  const upstream = upstreamRes.webSocket;
  if (!upstream) {
    return new Response('Upstream WebSocket connection failed', { status: 502 });
  }

  upstream.accept();
  server.accept();

  // ブラウザ → Streamlit
  server.addEventListener('message', (e) => {
    try { upstream.send(e.data); } catch (_) {}
  });
  // Streamlit → ブラウザ
  upstream.addEventListener('message', (e) => {
    try { server.send(e.data); } catch (_) {}
  });

  // どちらかが切断したら両方閉じる
  const closeAll = () => {
    try { server.close();   } catch (_) {}
    try { upstream.close(); } catch (_) {}
  };
  server.addEventListener('close',   closeAll);
  upstream.addEventListener('close', closeAll);
  server.addEventListener('error',   closeAll);
  upstream.addEventListener('error', closeAll);

  return new Response(null, { status: 101, webSocket: client });
}
