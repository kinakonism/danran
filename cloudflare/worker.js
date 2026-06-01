/**
 * danran Cloudflare Worker — v4
 *
 * 発見: Streamlit Community Cloud の実際の Streamlit アプリは
 *       /~/+/ 以下で nginx 認証をバイパスして Uvicorn に直接アクセスできる。
 *       セッション管理不要。
 *
 * v4 の変更:
 *   - WebSocket プロキシを cloudflare:sockets (手動TCP) から
 *     fetch() WebSocket API (ネイティブ) に変更。
 *     → フレームエンコーダー/デコーダー不要、Mac Chrome での安定性向上。
 *
 * 役割:
 *   - /sw.js /manifest.json /icons/* を直接配信（PWA 実現）
 *   - /{path} → /~/+/{path} として Streamlit Uvicorn にリバースプロキシ
 *   - WebSocket: fetch() WebSocket API で /~/+/_stcore/stream にプロキシ
 *   - cron: 12時間ごとに Streamlit を warm-up
 */

const STREAMLIT_HOST   = 'danran-dhawa6nhapcwnq6lrjqzhw.streamlit.app';
const STREAMLIT_ORIGIN = 'https://' + STREAMLIT_HOST;
// 実際の Streamlit アプリは /~/+/ 以下に存在
const APP_BASE_PATH    = '/~/+';
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';

// ── Service Worker スクリプト ─────────────────────────────────────────
const SW_JS = `
var SW_VERSION = '3.1.0';
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

// ── メインハンドラ ────────────────────────────────────────────────────
export default {
  async scheduled(event, env, ctx) {
    // Streamlit warm-up（スリープ防止）
    try {
      const r = await fetch(`${STREAMLIT_ORIGIN}${APP_BASE_PATH}/`, {
        headers: { 'User-Agent': UA },
      });
      console.log('[cron] warm-up:', r.status);
    } catch (e) {
      console.error('[cron] warm-up failed:', e.message);
    }
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // ── 直接配信: PWA ファイル ───────────────────────────────────────
    if (url.pathname === '/sw.js') {
      return new Response(SW_JS, {
        headers: {
          'Content-Type':           'application/javascript; charset=utf-8',
          'Service-Worker-Allowed': '/',
          'Cache-Control':          'no-store, no-cache, must-revalidate',
        },
      });
    }

    if (url.pathname === '/manifest.json') {
      return new Response(MANIFEST_JSON, {
        headers: {
          'Content-Type':              'application/manifest+json; charset=utf-8',
          'Cache-Control':             'no-store',
          'Access-Control-Allow-Origin': '*',
        },
      });
    }

    if (url.pathname.startsWith('/icons/')) {
      const filename = url.pathname.slice('/icons/'.length);
      if (!filename || filename.includes('..')) return new Response('Not Found', { status: 404 });
      const res = await fetch(
        `https://raw.githubusercontent.com/kinakonism/danran/main/static/icons/${filename}`
      );
      if (!res.ok) return new Response('Not Found', { status: 404 });
      const h = new Headers();
      h.set('Content-Type', res.headers.get('Content-Type') || 'image/png');
      h.set('Cache-Control', 'public, max-age=86400');
      h.set('Access-Control-Allow-Origin', '*');
      return new Response(res.body, { status: 200, headers: h });
    }

    // ── WebSocket プロキシ ───────────────────────────────────────────
    if ((request.headers.get('Upgrade') || '').toLowerCase() === 'websocket') {
      console.log('[WS] incoming websocket request:', url.pathname);
      return handleWebSocket(request, url, ctx);
    }

    // ── HTTP プロキシ ────────────────────────────────────────────────
    return proxyHttp(request, url);
  },
};

// ── HTTP プロキシ: /{path} → /~/+/{path} ──────────────────────────────
async function proxyHttp(request, url) {
  const upstreamPath = APP_BASE_PATH + url.pathname + url.search;
  const upstream = `${STREAMLIT_ORIGIN}${upstreamPath}`;

  const headers = new Headers();
  for (const [k, v] of request.headers.entries()) {
    const kl = k.toLowerCase();
    if (kl === 'host') continue;
    headers.set(k, v);
  }
  headers.set('Host', STREAMLIT_HOST);
  headers.set('User-Agent', UA);

  const res = await fetch(upstream, {
    method: request.method,
    headers,
    body: ['GET', 'HEAD'].includes(request.method) ? undefined : request.body,
    redirect: 'manual',
  });

  const newHeaders = new Headers();
  for (const [k, v] of res.headers.entries()) {
    newHeaders.append(k, v);
  }

  // リダイレクト先を Worker URL に書き換え
  const loc = res.headers.get('location') || '';
  if (loc && res.status >= 300 && res.status < 400) {
    try {
      const locUrl = new URL(loc.startsWith('http') ? loc : `${STREAMLIT_ORIGIN}${loc}`);
      let newPath = locUrl.pathname;
      if (newPath.startsWith(APP_BASE_PATH)) newPath = newPath.slice(APP_BASE_PATH.length) || '/';
      locUrl.hostname = url.hostname;
      locUrl.protocol = url.protocol;
      locUrl.port     = '';
      locUrl.pathname = newPath;
      newHeaders.set('Location', locUrl.toString());
    } catch (_) {}
  }

  // ── HTML 応答は <head> に iOS「ホーム画面に追加」用メタを注入する ──
  //   ★ Safari はホーム画面追加時に「最上位HTMLの<head>」を読む。Worker が配信する
  //     この HTML に静的に埋め込むことで、アイコン=danran / 名前="danran" を確実に反映。
  //     （アプリ内 JS で後挿入しても iOS では無視されがちなのでここで行う。）
  const ctype = (newHeaders.get('content-type') || '').toLowerCase();
  if (ctype.includes('text/html')) {
    // HTMLRewriter は本文を解凍して再出力するため、圧縮/長さ系ヘッダを除去
    newHeaders.delete('content-encoding');
    newHeaders.delete('content-length');
    const baseResp = new Response(res.body, {
      status: res.status, statusText: res.statusText, headers: newHeaders,
    });
    return new HTMLRewriter()
      // Streamlit 既定の apple-touch-icon は除去（ロゴが赤い船になるのを防ぐ）
      .on('link[rel="apple-touch-icon"]', { element(el) { el.remove(); } })
      .on('title', { element(el) { el.setInnerContent('danran'); } })
      .on('head', {
        element(el) {
          el.append('<link rel="apple-touch-icon" href="/icons/icon-192.png">', { html: true });
          el.append('<link rel="apple-touch-icon" sizes="192x192" href="/icons/icon-192.png">', { html: true });
          el.append('<meta name="apple-mobile-web-app-capable" content="yes">', { html: true });
          el.append('<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">', { html: true });
          el.append('<meta name="apple-mobile-web-app-title" content="danran">', { html: true });
          el.append('<link rel="manifest" href="/manifest.json">', { html: true });
        },
      })
      .transform(baseResp);
  }

  return new Response(res.body, {
    status:     res.status,
    statusText: res.statusText,
    headers:    newHeaders,
  });
}

// ── WebSocket プロキシ（fetch() WebSocket API 使用）────────────────────
//
// 重要: 正しい WS パスは /~/+/_stcore/stream
//       /_stcore/stream は nginx が HTML を返す（拒否）
//       /~/+/_stcore/stream は Uvicorn に直接到達する（Cookie 不要）
//
// v4: cloudflare:sockets (手動TCP) を廃止し fetch() WebSocket API に変更。
//     フレームエンコーダー/デコーダー不要でシンプルかつ安定。
async function handleWebSocket(request, url, ctx) {
  const wsPath = APP_BASE_PATH + url.pathname + url.search;

  // ブラウザ向け WebSocket ペア
  const [client, server] = Object.values(new WebSocketPair());
  server.accept();

  // upstream へ WebSocket 接続（fetch() API）
  const upstreamUrl = `https://${STREAMLIT_HOST}${wsPath}`;

  // upstream への接続ヘッダー
  const upstreamHeaders = new Headers();
  upstreamHeaders.set('Host', STREAMLIT_HOST);
  upstreamHeaders.set('User-Agent', UA);
  upstreamHeaders.set('Origin', STREAMLIT_ORIGIN);
  upstreamHeaders.set('Upgrade', 'websocket');
  upstreamHeaders.set('Connection', 'Upgrade');
  upstreamHeaders.set('Sec-WebSocket-Version', '13');

  // ★ 重要: Sec-WebSocket-Protocol を転送（streamlit + auth token）
  // Streamlit JS は new WebSocket(url, ['streamlit', 'PLACEHOLDER_AUTH_TOKEN']) で接続する
  // このサブプロトコルをそのまま upstream に転送しないと Streamlit が認証できない
  const wsProtocol = request.headers.get('Sec-WebSocket-Protocol');
  if (wsProtocol) upstreamHeaders.set('Sec-WebSocket-Protocol', wsProtocol);

  // Cookie があれば転送
  const cookie = request.headers.get('Cookie');
  if (cookie) upstreamHeaders.set('Cookie', cookie);

  let upstreamWS;
  let agreedProtocol = null;
  try {
    const upstreamResp = await fetch(upstreamUrl, { headers: upstreamHeaders });
    if (!upstreamResp.webSocket) {
      const body = await upstreamResp.text().catch(() => '');
      throw new Error(`upstream returned ${upstreamResp.status}: ${body.slice(0, 120)}`);
    }
    upstreamWS = upstreamResp.webSocket;
    // upstream が選択したサブプロトコルを取得（ブラウザへの 101 にも echo する）
    agreedProtocol = upstreamResp.headers.get('Sec-WebSocket-Protocol');
    upstreamWS.accept();
    console.log(`[WS] connected to upstream: ${wsPath}, protocol=${agreedProtocol}`);
  } catch (err) {
    console.error('[WS] upstream connect failed:', err.message);
    server.close(1011, 'upstream connect failed');
    return new Response(null, { status: 101, webSocket: client });
  }

  // ── ブリッジ: client ↔ upstreamWS ────────────────────────────────
  // ctx.waitUntil() でブリッジが Response 返却後も GC されないよう保護する

  const bridgePromise = new Promise((resolve) => {
    let closedCount = 0;
    const tryResolve = () => { if (++closedCount >= 2) resolve(); };

    let msgCount = 0;
    // ブラウザ → Streamlit
    server.addEventListener('message', (e) => {
      msgCount++;
      const isBinary = e.data instanceof ArrayBuffer || ArrayBuffer.isView(e.data);
      const len = isBinary ? (e.data.byteLength || e.data.length) : e.data.length;
      if (msgCount <= 3) console.log(`[WS] client→upstream #${msgCount} binary=${isBinary} len=${len}`);
      try {
        upstreamWS.send(e.data);
      } catch (err) {
        console.error('[WS] client→upstream send error:', err.message);
      }
    });

    server.addEventListener('close', (e) => {
      console.log(`[WS] client closed: ${e.code} ${e.reason}`);
      try { upstreamWS.close(e.code || 1000, e.reason || ''); } catch (_) {}
      tryResolve();
    });

    server.addEventListener('error', (e) => {
      console.error('[WS] client error:', e.message || e);
    });

    let upMsgCount = 0;
    // Streamlit → ブラウザ
    upstreamWS.addEventListener('message', (e) => {
      upMsgCount++;
      const isBinary = e.data instanceof ArrayBuffer || ArrayBuffer.isView(e.data);
      const len = isBinary ? (e.data.byteLength || e.data.length) : e.data.length;
      if (upMsgCount <= 5) console.log(`[WS] upstream→client #${upMsgCount} binary=${isBinary} len=${len}`);
      try {
        server.send(e.data);
      } catch (err) {
        console.error('[WS] upstream→client send error:', err.message);
      }
    });

    upstreamWS.addEventListener('close', (e) => {
      console.log(`[WS] upstream closed: ${e.code} ${e.reason}`);
      try { server.close(e.code || 1000, e.reason || ''); } catch (_) {}
      tryResolve();
    });

    upstreamWS.addEventListener('error', (e) => {
      console.error('[WS] upstream error:', e.message || e);
      try { server.close(1011, 'upstream error'); } catch (_) {}
      tryResolve();
    });
  });

  // Worker が Response 返却後もブリッジが継続動作するよう保護
  ctx.waitUntil(bridgePromise);

  // ★ upstream が選択したサブプロトコルをブラウザへの 101 に echo する
  // これがないと Chrome は "Server sent no subprotocol" で接続を拒否する
  const responseHeaders = {};
  if (agreedProtocol) {
    responseHeaders['Sec-WebSocket-Protocol'] = agreedProtocol;
  } else if (wsProtocol) {
    // upstream が echo しなかった場合は browser が要求した最初のプロトコルを使う
    responseHeaders['Sec-WebSocket-Protocol'] = wsProtocol.split(',')[0].trim();
  }

  return new Response(null, { status: 101, webSocket: client, headers: responseHeaders });
}
