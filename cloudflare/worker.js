/**
 * danran Cloudflare Worker
 *
 * 役割:
 *   - /sw.js /manifest.json /icons/* を直接配信（PWA 実現）
 *   - Streamlit Cloud の認証フローをサーバー側で自動処理し、セッションをキャッシュ
 *   - それ以外のリクエスト・WebSocket を Streamlit Cloud にリバースプロキシ
 */

const STREAMLIT_HOST   = 'danran-dhawa6nhapcwnq6lrjqzhw.streamlit.app';
const STREAMLIT_ORIGIN = 'https://' + STREAMLIT_HOST;
const GITHUB_RAW_ICONS = 'https://raw.githubusercontent.com/kinakonism/danran/main/static/icons';
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';

// ── セッションキャッシュ（モジュールレベル、同一 Worker isolate 内で共有）───
let _session = null; // { cookies: string, expires: number }

// ── Service Worker スクリプト ─────────────────────────────────────────
const SW_JS = `
var SW_VERSION = '3.0.0';
self.addEventListener('install', function(e) { self.skipWaiting(); });
self.addEventListener('activate', function(e) { e.waitUntil(clients.claim()); });
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
  name: 'danran — 家族チャット',
  short_name: 'danran',
  description: '家族専用プライベートチャット',
  lang: 'ja',
  start_url: '/',
  display: 'standalone',
  orientation: 'portrait',
  background_color: '#1a1a2e',
  theme_color: '#1a1a2e',
  icons: [
    { src: '/icons/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any maskable' },
    { src: '/icons/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any maskable' },
  ],
});

// ── Set-Cookie 1行をパース → { name, value } | null ──────────────────
function parseSetCookie(header) {
  const semi = header.indexOf(';');
  const nameValue = semi >= 0 ? header.slice(0, semi) : header;
  const eq = nameValue.indexOf('=');
  if (eq < 0) return null;
  return { name: nameValue.slice(0, eq).trim(), value: nameValue.slice(eq + 1).trim() };
}

// ── Response の Set-Cookie を既存 cookies オブジェクトに取り込む ─────
function collectCookies(response, cookies) {
  // Cloudflare Workers は Headers を iterable として扱える
  for (const [k, v] of response.headers.entries()) {
    if (k.toLowerCase() !== 'set-cookie') continue;
    const parsed = parseSetCookie(v);
    if (!parsed) continue;
    if (parsed.value === '' || v.includes('Max-Age=0')) {
      delete cookies[parsed.name]; // クリア
    } else {
      cookies[parsed.name] = parsed.value;
    }
  }
}

// ── Streamlit 認証フローをサーバー側で完了し、有効な Cookie 文字列を返す ──
async function ensureSession() {
  if (_session && Date.now() < _session.expires) {
    return _session.cookies;
  }

  const cookies = {};

  // Step 1: GET / → 303 to share.streamlit.io/-/auth/app
  const r1 = await fetch(`${STREAMLIT_ORIGIN}/`, {
    redirect: 'manual',
    headers: { 'User-Agent': UA },
  });
  collectCookies(r1, cookies);

  // 認証リダイレクトが不要なら（既にセッション有効な場合等）そのまま返す
  if (r1.status === 200) {
    const cs = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ');
    _session = { cookies: cs, expires: Date.now() + 3600_000 };
    return cs;
  }

  const authUrl = r1.headers.get('location') || '';
  if (!authUrl.includes('share.streamlit.io')) {
    return Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ');
  }

  // Step 2: share.streamlit.io/-/auth/app → セッショントークン取得 + 303 to /-/login?payload=…
  const r2 = await fetch(authUrl, {
    redirect: 'manual',
    headers: {
      'User-Agent': UA,
      'Cookie': Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; '),
    },
  });
  collectCookies(r2, cookies);
  const loginUrl = r2.headers.get('location') || '';
  if (!loginUrl) return Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ');

  // Step 3: /-/login?payload=… → streamlit_session を danran ドメインに確立
  const loginParsed = new URL(loginUrl.startsWith('http') ? loginUrl : `${STREAMLIT_ORIGIN}${loginUrl}`);
  const r3 = await fetch(`${STREAMLIT_ORIGIN}${loginParsed.pathname}${loginParsed.search}`, {
    redirect: 'manual',
    headers: {
      'User-Agent': UA,
      'Cookie': Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; '),
    },
  });
  collectCookies(r3, cookies);

  // Step 4: GET / → proxy-tracking-id 等を取得
  const r4 = await fetch(`${STREAMLIT_ORIGIN}/`, {
    redirect: 'manual',
    headers: {
      'User-Agent': UA,
      'Cookie': Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; '),
    },
  });
  collectCookies(r4, cookies);

  const cs = Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ');
  // セッションは約4時間有効、3時間でリフレッシュ
  _session = { cookies: cs, expires: Date.now() + 3 * 3600_000 };
  return cs;
}

// ── メインハンドラ ────────────────────────────────────────────────────
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // /sw.js
    if (url.pathname === '/sw.js') {
      return new Response(SW_JS, {
        headers: {
          'Content-Type':           'application/javascript; charset=utf-8',
          'Service-Worker-Allowed': '/',
          'Cache-Control':          'no-store, no-cache, must-revalidate',
        },
      });
    }

    // /manifest.json
    if (url.pathname === '/manifest.json') {
      return new Response(MANIFEST_JSON, {
        headers: {
          'Content-Type':              'application/manifest+json; charset=utf-8',
          'Cache-Control':             'no-store',
          'Access-Control-Allow-Origin': '*',
        },
      });
    }

    // /icons/*
    if (url.pathname.startsWith('/icons/')) {
      const filename = url.pathname.slice('/icons/'.length);
      if (!filename || filename.includes('..')) return new Response('Not Found', { status: 404 });
      const res = await fetch(`${GITHUB_RAW_ICONS}/${filename}`);
      if (!res.ok) return new Response('Not Found', { status: 404 });
      const h = new Headers();
      h.set('Content-Type', res.headers.get('Content-Type') || 'image/png');
      h.set('Cache-Control', 'public, max-age=86400');
      h.set('Access-Control-Allow-Origin', '*');
      return new Response(res.body, { status: 200, headers: h });
    }

    // WebSocket
    const upgrade = request.headers.get('Upgrade');
    if (upgrade && upgrade.toLowerCase() === 'websocket') {
      return handleWebSocket(request, url);
    }

    // HTTP プロキシ
    return proxyHttp(request, url);
  },
};

// ── HTTP プロキシ（認証フロー自動処理つき）────────────────────────────
async function proxyHttp(request, url, retry = true) {
  const sessionCookies = await ensureSession();

  const upstream = new URL(url.pathname + url.search, STREAMLIT_ORIGIN);

  const headers = new Headers();
  for (const [k, v] of request.headers.entries()) {
    const kl = k.toLowerCase();
    if (kl === 'host' || kl === 'cookie') continue;
    headers.set(k, v);
  }
  headers.set('Host', STREAMLIT_HOST);

  // ブラウザの Cookie（Streamlit セッション系は除外）と Worker セッションを合成
  const browserCookies = (request.headers.get('Cookie') || '')
    .split('; ')
    .filter(c => !c.match(/^(streamlit_session|_streamlit_csrf|proxy-tracking-id)=/))
    .join('; ');
  const merged = [browserCookies, sessionCookies].filter(Boolean).join('; ');
  if (merged) headers.set('Cookie', merged);

  const res = await fetch(upstream.toString(), {
    method: request.method,
    headers,
    body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
    redirect: 'manual',
  });

  // 認証リダイレクトが来た場合はセッションをリセットして1回リトライ
  const loc = res.headers.get('location') || '';
  if (retry && res.status >= 300 && res.status < 400 &&
      (loc.includes('share.streamlit.io') || loc.includes('/-/login'))) {
    _session = null;
    return proxyHttp(request, url, false);
  }

  // レスポンスヘッダー整形
  const newHeaders = new Headers();
  for (const [k, v] of res.headers.entries()) {
    const kl = k.toLowerCase();
    // Streamlit セッション Cookie はブラウザに渡さない（Worker が管理）
    if (kl === 'set-cookie') {
      const parsed = parseSetCookie(v);
      if (parsed && /^(streamlit_session|_streamlit_csrf|proxy-tracking-id)$/.test(parsed.name)) continue;
    }
    newHeaders.append(k, v);
  }

  // Location ヘッダーのホストを Workers ドメインに書き換え
  if (loc && res.status >= 300 && res.status < 400) {
    try {
      const locUrl = new URL(loc.startsWith('http') ? loc : `${STREAMLIT_ORIGIN}${loc}`);
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
  const sessionCookies = await ensureSession();

  const wsPath      = url.pathname + url.search;
  const upstreamUrl = `wss://${STREAMLIT_HOST}${wsPath}`;

  const [client, server] = Object.values(new WebSocketPair());

  const upstreamRes = await fetch(upstreamUrl, {
    headers: {
      'Host':       STREAMLIT_HOST,
      'Origin':     STREAMLIT_ORIGIN,
      'Cookie':     sessionCookies,
      'Upgrade':    'websocket',
      'Connection': 'Upgrade',
    },
  });

  const upstream = upstreamRes.webSocket;
  if (!upstream) {
    return new Response('Upstream WebSocket failed', { status: 502 });
  }

  upstream.accept();
  server.accept();

  server.addEventListener('message',   (e) => { try { upstream.send(e.data); } catch (_) {} });
  upstream.addEventListener('message', (e) => { try { server.send(e.data);   } catch (_) {} });

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
