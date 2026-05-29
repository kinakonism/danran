/**
 * danran Cloudflare Worker — v3
 *
 * 発見: Streamlit Community Cloud の実際の Streamlit アプリは
 *       /~/+/ 以下で nginx 認証をバイパスして Uvicorn に直接アクセスできる。
 *       セッション管理不要。
 *
 * 役割:
 *   - /sw.js /manifest.json /icons/* を直接配信（PWA 実現）
 *   - /{path} → /~/+/{path} として Streamlit Uvicorn にリバースプロキシ
 *   - WebSocket は cloudflare:sockets で HTTP/1.1 強制プロキシ
 *     (/~/+/_stcore/stream が正しい WS パス)
 *   - cron: 12時間ごとに Streamlit を warm-up
 */

import { connect } from 'cloudflare:sockets';

const STREAMLIT_HOST   = 'danran-dhawa6nhapcwnq6lrjqzhw.streamlit.app';
const STREAMLIT_ORIGIN = 'https://' + STREAMLIT_HOST;
// 実際の Streamlit アプリは /~/+/ 以下に存在
const APP_BASE_PATH    = '/~/+';
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';

const ENC = new TextEncoder();
const DEC = new TextDecoder('latin1');

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

// ── バイト配列結合ヘルパー ─────────────────────────────────────────────
function concat(a, b) {
  const c = new Uint8Array(a.length + b.length);
  c.set(a); c.set(b, a.length);
  return c;
}

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
      return handleWebSocket(request, url);
    }

    // ── HTTP プロキシ ────────────────────────────────────────────────
    return proxyHttp(request, url);
  },
};

// ── HTTP プロキシ: /{path} → /~/+/{path} ──────────────────────────────
async function proxyHttp(request, url) {
  // ブラウザのパスを /~/+/{path} にマップ
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
    const kl = k.toLowerCase();
    if (kl === 'set-cookie') continue; // Streamlit 内部クッキーはブラウザに渡さない
    newHeaders.append(k, v);
  }

  // リダイレクト先を Worker URL に書き換え
  const loc = res.headers.get('location') || '';
  if (loc && res.status >= 300 && res.status < 400) {
    try {
      const locUrl = new URL(loc.startsWith('http') ? loc : `${STREAMLIT_ORIGIN}${loc}`);
      // /~/+/{path} → /{path} に戻す
      let newPath = locUrl.pathname;
      if (newPath.startsWith(APP_BASE_PATH)) newPath = newPath.slice(APP_BASE_PATH.length) || '/';
      locUrl.hostname = url.hostname;
      locUrl.protocol = url.protocol;
      locUrl.port     = '';
      locUrl.pathname = newPath;
      newHeaders.set('Location', locUrl.toString());
    } catch (_) {}
  }

  return new Response(res.body, {
    status:     res.status,
    statusText: res.statusText,
    headers:    newHeaders,
  });
}

// ── WebSocket プロキシ（cloudflare:sockets で HTTP/1.1 強制）─────────
//
// 重要: 正しい WS パスは /~/+/_stcore/stream
//       /_stcore/stream は nginx が HTML を返す（拒否）
//       /~/+/_stcore/stream は Uvicorn に直接到達する（Cookie 不要）
async function handleWebSocket(request, url) {
  // ブラウザの WS パスを /~/+/_stcore/stream にマップ
  // (ブラウザは /_stcore/stream に接続するが、上流は /~/+/ 以下)
  const wsPath = APP_BASE_PATH + url.pathname + url.search;

  // ブラウザ向け WebSocket ペア
  const [client, server] = Object.values(new WebSocketPair());
  server.accept();

  // cloudflare:sockets で HTTP/1.1 TLS 接続
  let tcpSocket;
  try {
    tcpSocket = connect(
      { hostname: STREAMLIT_HOST, port: 443 },
      { secureTransport: 'on' }
    );
  } catch (err) {
    console.error('[WS] connect failed:', err.message);
    server.close(1011, 'upstream connect failed');
    return new Response(null, { status: 101, webSocket: client });
  }

  // HTTP/1.1 WebSocket ハンドシェイク送信
  const wsKey = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(16))));
  const handshake =
    `GET ${wsPath} HTTP/1.1\r\n` +
    `Host: ${STREAMLIT_HOST}\r\n` +
    `Upgrade: websocket\r\n` +
    `Connection: Upgrade\r\n` +
    `Sec-WebSocket-Key: ${wsKey}\r\n` +
    `Sec-WebSocket-Version: 13\r\n` +
    `Origin: ${STREAMLIT_ORIGIN}\r\n` +
    `User-Agent: ${UA}\r\n` +
    `\r\n`;

  try {
    const w = tcpSocket.writable.getWriter();
    await w.write(ENC.encode(handshake));
    w.releaseLock();
  } catch (err) {
    console.error('[WS] handshake write failed:', err.message);
    server.close(1011, 'handshake write failed');
    return new Response(null, { status: 101, webSocket: client });
  }

  // 101 レスポンスを読む
  let frameBuffer = new Uint8Array(0);
  try {
    const r = tcpSocket.readable.getReader();
    let done = false;
    while (!done) {
      const chunk = await r.read();
      if (chunk.done) throw new Error('TCP closed before 101');
      frameBuffer = concat(frameBuffer, chunk.value);

      const text = DEC.decode(frameBuffer);
      const headerEnd = text.indexOf('\r\n\r\n');
      if (headerEnd >= 0) {
        if (!text.startsWith('HTTP/1.1 101')) {
          const firstLine = text.split('\r\n')[0];
          throw new Error(`Upstream returned: ${firstLine}`);
        }
        frameBuffer = frameBuffer.slice(headerEnd + 4);
        done = true;
      }
    }
    r.releaseLock();
  } catch (err) {
    console.error('[WS] 101 read failed:', err.message);
    server.close(1011, err.message);
    return new Response(null, { status: 101, webSocket: client });
  }

  const dbgFirst = Array.from(frameBuffer.slice(0, 8)).map(b => b.toString(16).padStart(2,'0')).join(' ');
  console.log(`[WS] connected to ${wsPath}, post-101 buf=${frameBuffer.length}B first8: ${dbgFirst}`);

  // ── フレームブリッジ ──────────────────────────────────────────────

  const writeQueue = [];
  let writing = false;
  async function flushWrites() {
    if (writing) return;
    writing = true;
    while (writeQueue.length > 0) {
      const data = writeQueue.shift();
      try {
        const w = tcpSocket.writable.getWriter();
        await w.write(data);
        w.releaseLock();
      } catch (_) { break; }
    }
    writing = false;
  }

  // ブラウザ → Streamlit
  server.addEventListener('message', (e) => {
    try {
      const isBinary = e.data instanceof ArrayBuffer || ArrayBuffer.isView(e.data);
      const payload = isBinary
        ? new Uint8Array(e.data instanceof ArrayBuffer ? e.data : e.data.buffer)
        : ENC.encode(e.data);
      writeQueue.push(encodeWSFrame(payload, isBinary, true));
      flushWrites();
    } catch (_) {}
  });

  server.addEventListener('close', () => {
    try {
      writeQueue.push(new Uint8Array([0x88, 0x82, 0, 0, 0, 0, 0x03, 0xE8]));
      flushWrites();
    } catch (_) {}
    setTimeout(() => { try { tcpSocket.close(); } catch (_) {} }, 500);
  });

  // Streamlit → ブラウザ
  (async () => {
    const r = tcpSocket.readable.getReader();
    try {
      while (true) {
        const { value, done } = await r.read();
        if (done) break;

        frameBuffer = concat(frameBuffer, value);
        const { frames, remaining } = decodeWSFrames(frameBuffer);
        frameBuffer = remaining;

        for (const { opcode, payload } of frames) {
          if (opcode === 0x8) {
            try { server.close(); } catch (_) {}
            return;
          } else if (opcode === 0x9) { // ping → pong
            const pong = encodeWSFrame(payload, false, true);
            pong[0] = 0x8A;
            writeQueue.push(pong);
            flushWrites();
          } else if (opcode === 0x1) {
            server.send(new TextDecoder().decode(payload));
          } else if (opcode === 0x2) {
            server.send(payload.buffer.slice(payload.byteOffset, payload.byteOffset + payload.byteLength));
          }
        }
      }
    } catch (_) {}
    r.releaseLock();
    try { server.close(); } catch (_) {}
  })();

  return new Response(null, { status: 101, webSocket: client });
}

// ── WebSocket フレームエンコーダー ────────────────────────────────────
function encodeWSFrame(payload, isBinary, masked) {
  const opcode = isBinary ? 0x2 : 0x1;
  const len = payload.length;
  let offset = 2;
  if (len >= 65536) offset += 8;
  else if (len >= 126) offset += 2;
  if (masked) offset += 4;

  const frame = new Uint8Array(offset + len);
  frame[0] = 0x80 | opcode;

  if (len >= 65536) {
    frame[1] = (masked ? 0x80 : 0) | 127;
    new DataView(frame.buffer).setBigUint64(2, BigInt(len));
    offset = masked ? 14 : 10;
  } else if (len >= 126) {
    frame[1] = (masked ? 0x80 : 0) | 126;
    frame[2] = (len >> 8) & 0xFF;
    frame[3] = len & 0xFF;
    offset = masked ? 8 : 4;
  } else {
    frame[1] = (masked ? 0x80 : 0) | len;
    offset = masked ? 6 : 2;
  }

  if (masked) {
    const mask = crypto.getRandomValues(new Uint8Array(4));
    frame.set(mask, offset - 4);
    for (let i = 0; i < len; i++) frame[offset + i] = payload[i] ^ mask[i % 4];
  } else {
    frame.set(payload, offset);
  }
  return frame;
}

// ── WebSocket フレームデコーダー ─────────────────────────────────────
function decodeWSFrames(buf) {
  const frames = [];
  let i = 0;
  while (i + 2 <= buf.length) {
    const opcode = buf[i] & 0x0F;
    const masked  = (buf[i + 1] & 0x80) !== 0;
    let plen = buf[i + 1] & 0x7F;
    let hlen = 2;

    if (plen === 126) {
      if (i + 4 > buf.length) break;
      plen = (buf[i + 2] << 8) | buf[i + 3];
      hlen = 4;
    } else if (plen === 127) {
      if (i + 10 > buf.length) break;
      plen = Number(new DataView(buf.buffer, buf.byteOffset + i + 2).getBigUint64(0));
      hlen = 10;
    }
    if (masked) hlen += 4;
    if (i + hlen + plen > buf.length) break;

    let payload = buf.slice(i + hlen, i + hlen + plen);
    if (masked) {
      const mk = buf.slice(i + hlen - 4, i + hlen);
      const out = new Uint8Array(plen);
      for (let j = 0; j < plen; j++) out[j] = payload[j] ^ mk[j % 4];
      payload = out;
    }
    frames.push({ opcode, payload });
    i += hlen + plen;
  }
  return { frames, remaining: buf.slice(i) };
}
