/**
 * danran Cloudflare Worker — v5
 *
 * 履歴: v3/v4 は Streamlit Community Cloud の /~/+/ 認証バイパスで Uvicorn に直結していたが、
 *       2026-06 に Streamlit がこのバイパスを封鎖（/~/+/ が 400）。全アプリへ cookie 認証
 *       バウンス（/ →share.streamlit.io/-/auth→ /-/login→ /）が導入された。
 *
 * v5 の変更:
 *   - 認証 cookie ハンドシェイク（ensureSession / doHandshake）を実装し、取得した
 *     streamlit_session 等を全上流リクエスト（HTTP + WebSocket）に注入。
 *   - プロキシ先を /~/+/{path} から正規の /{path} に戻した。
 *   - session 失効時は張り直して1回リトライ（isAuthBounce）。
 *
 * 役割:
 *   - /sw.js /manifest.json /icons/* を直接配信（PWA 実現）
 *   - /{path} → 上流 /{path} に cookie 認証付きでリバースプロキシ
 *   - WebSocket: fetch() WebSocket API で /_stcore/stream にプロキシ（cookie 注入）
 *   - cron: 15分ごとに Streamlit を warm-up＆セッション張り直し
 */

// ★ v6: 上流を Streamlit Community Cloud から「Mac mini 自前ホスト（Cloudflare Tunnel）」へ変更。
//   Streamlit Cloud が /~/+/ バイパスを封鎖したため、mini で run.py を直接動かしトンネル公開する。
//   トンネルURLが変わったらここを更新（将来は named tunnel / 独自ドメインで固定化）。
const STREAMLIT_HOST   = 'underlying-contributors-guests-dining.trycloudflare.com';  // フォールバック
const STREAMLIT_ORIGIN = 'https://' + STREAMLIT_HOST;
const SELF_HOSTED      = true;   // 自前ホストは認証ハンドシェイク不要

// ★ トンネルURL自己追従: mini が現在のトンネルホストを Supabase app_config(tunnel_host) に書き、
//   worker がそれを読む。クイックトンネルのURLが再起動で変わっても worker が自動追従する。
const SB_URL  = 'https://fyadpbzlvyzihynpcckw.supabase.co';
const SB_ANON = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImZ5YWRwYnpsdnl6aWh5bnBjY2t3Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3Nzk3OTE3NTYsImV4cCI6MjA5NTM2Nzc1Nn0.Kf9kV1OBO5qxitqhmx32DgijXrxhRRbr8pHaM7q5Jy8';
let HOST_CACHE = { host: STREAMLIT_HOST, at: 0 };
const HOST_TTL_MS = 60 * 1000;   // 60秒キャッシュ

async function getUpstreamHost(force) {
  // force=true でキャッシュを無視して Supabase から取り直す
  // （トンネル再起動直後の stale ホスト固着 → 最大60秒つながらない、を接続失敗時に即解消）
  const now = Date.now();
  if (!force && HOST_CACHE.host && (now - HOST_CACHE.at) < HOST_TTL_MS) return HOST_CACHE.host;
  try {
    const r = await fetch(`${SB_URL}/rest/v1/app_config?key=eq.tunnel_host&select=value`, {
      headers: { apikey: SB_ANON, Authorization: 'Bearer ' + SB_ANON },
    });
    const rows = await r.json();
    const v = rows && rows[0] && rows[0].value;
    if (v) { HOST_CACHE = { host: v, at: now }; return v; }
  } catch (e) { console.error('[host] supabase lookup failed:', e.message); }
  HOST_CACHE.at = now;   // 失敗時も叩きすぎない
  return HOST_CACHE.host || STREAMLIT_HOST;
}
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1';

// ── 上流完全死亡時の自動リトライページ（🏠スプラッシュ＋health回復で自動リロード）──
//   素の 502 を返すと PWA 起動が「真っ暗な失敗画面」で止まり手動再起動が必要になる。
//   このページは 4 秒ごとに health を払い、回復したら勝手に本物のアプリへ戻る。
const RETRY_HTML = `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes"><title>danran</title>
<style>body{margin:0;background:#1a1614;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;gap:12px;font-family:-apple-system,sans-serif}
@keyframes p{0%,100%{opacity:.5;transform:scale(.96)}50%{opacity:1;transform:scale(1)}}</style></head><body>
<div style="font-size:3.2rem;animation:p 1.2s ease-in-out infinite">\u{1F3E0}</div>
<div style="color:rgba(240,232,224,.5);font-size:.85rem;font-weight:700;letter-spacing:.12em">danran</div>
<div style="color:rgba(240,232,224,.35);font-size:.75rem">つなぎ直しています…</div>
<script>(function p(){fetch("/_stcore/health",{cache:"no-store"}).then(function(r){
if(r.ok){location.reload();}else{setTimeout(p,4000);}}).catch(function(){setTimeout(p,4000);});})();</script>
</body></html>`;

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

// ── Streamlit Community Cloud 認証 cookie ハンドシェイク ───────────────────
//
// 2026-06 に Streamlit Community Cloud は全アプリへ cookie 認証バウンスを導入し、
// 旧来の /~/+/ 認証バイパス（v3/v4）は 400 で封鎖された。
// 正規ブラウザは次の連鎖で streamlit_session cookie を取得してからアプリを開く:
//   GET /  →303→ share.streamlit.io/-/auth/app?redirect_uri=…
//          →303→ /-/login?payload=…（_streamlit_csrf / streamlit_session 発行）
//          →303→ /（streamlit_session 更新）  →200 アプリ本体（proxy-tracking-id 発行）
// Worker がこの連鎖をサーバ側で実行して取得した cookie を全上流リクエストに注入する。
// danran のユーザ認証は別途アプリ内（Supabase sessions）にあるため、
// プラットフォーム session を全員で共有しても各ブラウザの WS=独立 Streamlit session で問題ない。
let SESSION = { cookie: '', at: 0 };
let SESSION_PROMISE = null;
const SESSION_TTL_MS = 25 * 60 * 1000;   // 25分でリフレッシュ（streamlit_session は数時間有効）

function parseSetCookies(resp, jar) {
  let lines = [];
  try { if (typeof resp.headers.getSetCookie === 'function') lines = resp.headers.getSetCookie(); } catch (e) {}
  if (!lines.length) { const one = resp.headers.get('set-cookie'); if (one) lines = [one]; }
  for (const line of lines) {
    const m = /^\s*([^=;\s]+)=([^;]*)/.exec(line);
    if (!m) continue;
    const name = m[1], val = m[2];
    if (val === '' || /(?:^|;)\s*max-age=0\b/i.test(line)) delete jar[name];
    else jar[name] = val;
  }
}
function jarToHeader(jar) {
  return Object.keys(jar).map((k) => `${k}=${jar[k]}`).join('; ');
}

// 認証連鎖をたどって cookie を集める
async function doHandshake() {
  const jar = {};
  let url = `${STREAMLIT_ORIGIN}/`;
  for (let i = 0; i < 8; i++) {
    const r = await fetch(url, {
      headers: { 'User-Agent': UA, 'Accept': 'text/html', 'Cookie': jarToHeader(jar) },
      redirect: 'manual',
    });
    parseSetCookies(r, jar);
    try { if (r.body) await r.body.cancel(); } catch (e) {}
    if (r.status >= 300 && r.status < 400) {
      const loc = r.headers.get('location');
      if (!loc) break;
      url = new URL(loc, url).toString();
      continue;
    }
    break;   // 200 到達
  }
  const cookie = jarToHeader(jar);
  console.log('[auth] handshake done, cookies=' + Object.keys(jar).join(','));
  return cookie;
}

// 有効な session cookie を返す（キャッシュ＋同時実行は1本に集約）
async function ensureSession(force) {
  if (SELF_HOSTED) return '';   // 自前ホストは cookie 認証不要
  const now = Date.now();
  if (!force && SESSION.cookie && (now - SESSION.at) < SESSION_TTL_MS) return SESSION.cookie;
  if (!SESSION_PROMISE) {
    SESSION_PROMISE = doHandshake()
      .then((c) => { if (c) SESSION = { cookie: c, at: Date.now() }; SESSION_PROMISE = null; return SESSION.cookie; })
      .catch((e) => { SESSION_PROMISE = null; console.error('[auth] handshake failed:', e.message); return SESSION.cookie; });
  }
  return SESSION_PROMISE;
}

// 認証バウンス（session 失効）への 30x かどうか
function isAuthBounce(res) {
  if (res.status < 300 || res.status >= 400) return false;
  const loc = (res.headers.get('location') || '').toLowerCase();
  return loc.includes('/-/login') || loc.includes('/-/auth') || loc.includes('share.streamlit.io');
}

// ── メインハンドラ ────────────────────────────────────────────────────
export default {
  async scheduled(event, env, ctx) {
    // Streamlit warm-up（スリープ防止）＋ セッション張り直し
    try {
      const cookie = await ensureSession(true);
      const r = await fetch(`${STREAMLIT_ORIGIN}/`, {
        headers: { 'User-Agent': UA, 'Cookie': cookie },
        redirect: 'manual',
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

    // favicon は danran アイコンを返す（Streamlit 既定の赤い船 favicon を上書き）
    if (url.pathname === '/favicon.png' || url.pathname === '/favicon.ico') {
      const res = await fetch(
        'https://raw.githubusercontent.com/kinakonism/danran/main/static/icons/icon-192.png'
      );
      const h = new Headers();
      h.set('Content-Type', 'image/png');
      h.set('Cache-Control', 'public, max-age=86400');
      return new Response(res.ok ? res.body : null, { status: res.ok ? 200 : 404, headers: h });
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

// 1 回分の上流リクエスト（cookie 注入）
async function fetchUpstream(request, url, cookie, bodyBuf, host) {
  const upstream = `https://${host}${url.pathname}${url.search}`;
  const headers = new Headers();
  for (const [k, v] of request.headers.entries()) {
    const kl = k.toLowerCase();
    if (kl === 'host' || kl === 'cookie') continue;   // host と cookie は worker が決める
    headers.set(k, v);
  }
  headers.set('Host', host);
  headers.set('User-Agent', UA);
  if (cookie) headers.set('Cookie', cookie);

  const isBodyless = ['GET', 'HEAD'].includes(request.method);
  return fetch(upstream, {
    method: request.method,
    headers,
    body: isBodyless ? undefined : bodyBuf,
    redirect: 'manual',
  });
}

// ── HTTP プロキシ: /{path} → 上流 /{path}（cookie 認証注入）──────────────
async function proxyHttp(request, url) {
  // リトライで body を再送できるよう、非 GET はバッファ化
  const isBodyless = ['GET', 'HEAD'].includes(request.method);
  const bodyBuf = isBodyless ? undefined : await request.arrayBuffer();

  let host = await getUpstreamHost();
  let cookie = await ensureSession(false);
  // ★ 接続失敗(throw)や 502/530（トンネル死亡）はホストをキャッシュ無視で取り直して
  //   1回だけリトライ。トンネル再起動直後の stale ホスト固着（最大60秒 502）を自己修復する。
  let res = null;
  try {
    res = await fetchUpstream(request, url, cookie, bodyBuf, host);
  } catch (e) {
    res = null;
  }
  if (!res || res.status === 502 || res.status === 530) {
    try { if (res && res.body) await res.body.cancel(); } catch (e) {}
    const host2 = await getUpstreamHost(true);
    if (host2 !== host || !res) {
      host = host2;
      try {
        res = await fetchUpstream(request, url, cookie, bodyBuf, host);
      } catch (e) {
        res = null;
      }
    }
  }
  // それでもダメな場合、ページ要求には「🏠つなぎ直し」自動リトライページを返す
  // （素の 502 だと PWA 起動が真っ暗な失敗画面で止まる。health 回復で自動リロード）
  if (!res || res.status === 502 || res.status === 530) {
    const accept = request.headers.get('accept') || '';
    if (isBodyless && accept.includes('text/html')) {
      try { if (res && res.body) await res.body.cancel(); } catch (e) {}
      return new Response(RETRY_HTML, {
        status: 503,
        headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' },
      });
    }
    if (!res) res = new Response('upstream unreachable', { status: 502 });
  }

  // session 失効で認証バウンスに飛ばされたら、張り直して1回だけリトライ
  if (isAuthBounce(res)) {
    try { if (res.body) await res.body.cancel(); } catch (e) {}
    cookie = await ensureSession(true);
    res = await fetchUpstream(request, url, cookie, bodyBuf, host);
  }

  const newHeaders = new Headers();
  for (const [k, v] of res.headers.entries()) {
    newHeaders.append(k, v);
  }

  // リダイレクト先が streamlit.app を指していたら Worker URL に書き換え
  const loc = res.headers.get('location') || '';
  if (loc && res.status >= 300 && res.status < 400) {
    try {
      const locUrl = new URL(loc.startsWith('http') ? loc : `https://${host}${loc}`);
      if (locUrl.hostname === host) {
        locUrl.hostname = url.hostname;
        locUrl.protocol = url.protocol;
        locUrl.port     = '';
        newHeaders.set('Location', locUrl.toString());
      }
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
      // Streamlit 既定のアイコン類は除去（ホーム画面が赤い船ロゴになるのを防ぐ）
      .on('link[rel="apple-touch-icon"]', { element(el) { el.remove(); } })
      .on('link[rel="apple-touch-icon-precomposed"]', { element(el) { el.remove(); } })
      .on('link[rel="shortcut icon"]', { element(el) { el.remove(); } })
      .on('link[rel="icon"]', { element(el) { el.remove(); } })
      .on('title', { element(el) { el.setInnerContent('danran'); } })
      .on('head', {
        element(el) {
          // ★ 起動詰まり自動復旧ウォッチドッグ。しばらく放置後の初回起動でコールドな WS 接続が
          //   失敗し「スプラッシュのまま＝真っ暗」になることがある（手動で再起動すると直る＝
          //   再接続が warm で通る）。これを自動化：7秒経っても通常画面が出ない（スプラッシュ常駐 or
          //   _danran_cfg 無し）なら自動リロードして再接続させる。手動再起動と同じ効果。
          //   sessionStorage で最大3回まで（無限ループ防止）、正常表示でカウンタ解除。
          //   3回の即時リロードで直らない場合も諦めず「持久戦モード」へ: スプラッシュを
          //   保持したまま health を払い続け、通った時だけリロード（健全時のみ＝無限高速
          //   ループにならない）。リロード間隔は _dws で漸増（8s→16s→…→60s 上限）。
          //   正常表示で _dwd/_dws とも解除。これで「3回失敗→永久に真っ暗」を根絶する。
          el.append(
            '<script>(function(){try{setTimeout(function(){try{' +
            'var stuck=(!document.getElementById("_danran_cfg"))||document.getElementById("_danran_splash_wait");' +
            'var ok=document.querySelector("[data-testid=\\"stChatInput\\"]")||document.querySelector("input[type=password]");' +
            'if(!stuck||ok){sessionStorage.removeItem("_dwd");sessionStorage.removeItem("_dws");return;}' +
            'var n=parseInt(sessionStorage.getItem("_dwd")||"0",10);' +
            'if(n<3){sessionStorage.setItem("_dwd",String(n+1));location.reload();return;}' +
            'var s=parseInt(sessionStorage.getItem("_dws")||"0",10);' +
            'var wait=Math.min(8000*(s+1),60000);' +
            'if(window._danranBootSplash)window._danranBootSplash(true,"\\u3064\\u306a\\u304e\\u76f4\\u3057\\u3066\\u3044\\u307e\\u3059\\u2026");' +
            'setTimeout(function poll(){' +
            'var ok2=document.querySelector("[data-testid=\\"stChatInput\\"]")||document.getElementById("_danran_room_list");' +
            'if(ok2){sessionStorage.removeItem("_dwd");sessionStorage.removeItem("_dws");' +
            'if(window._danranBootSplash)window._danranBootSplash(false,"");return;}' +
            'fetch("/_stcore/health",{cache:"no-store"}).then(function(r){' +
            'if(r.ok){sessionStorage.setItem("_dws",String(s+1));location.reload();}' +
            'else{setTimeout(poll,wait);}}).catch(function(){setTimeout(poll,wait);});' +
            '},wait);' +
            '}catch(e){}},7000);}catch(e){}})();</script>',
            { html: true },
          );
          // ★ 復帰ウォッチドッグ。app 再起動(自動デプロイ)や iOS バックグラウンド復帰で WS が切れて
          //   「真っ暗のまま」になるのを防ぐ。フォアグラウンド復帰/再オンライン時に health を確認し、
          //   サーバが落ちていれば戻り次第リロード、長時間離脱(>=20s)後は健康でもリロードして再接続。
          el.append(
            '<script>(function(){var h=0;' +
            'function rl(){var t=0;(function p(){fetch("/_stcore/health",{cache:"no-store"}).then(function(r){' +
            'if(r.ok){location.reload();}else{if(++t<20)setTimeout(p,1500);}}).catch(function(){if(++t<20)setTimeout(p,1500);});})();}' +
            'function res(){if(document.visibilityState!=="visible")return;var a=h?(Date.now()-h):0;h=0;' +
            'fetch("/_stcore/health",{cache:"no-store"}).then(function(r){if(!r.ok){rl();}else if(a>=20000){location.reload();}}).catch(function(){rl();});}' +
            'document.addEventListener("visibilitychange",function(){if(document.visibilityState==="hidden"){h=Date.now();}else{res();}});' +
            'window.addEventListener("pageshow",function(e){if(e.persisted)res();});' +
            'window.addEventListener("online",res);})();</script>',
            { html: true },
          );
          // ★ 起動ブートスプラッシュ。Streamlit はロード直後に「スケルトン（灰色の角丸
          //   プレースホルダ）」をチラ見せするため、最初から地色＋🏠パルスで覆い隠す。
          //   本物の画面（_danran_cfg / ログインフォーム / Python 側スプラッシュ）が出たら
          //   フェードアウト。12秒で強制除去（7秒ウォッチドッグのリロードとも共存し、
          //   リロード後も同じスプラッシュが出るので連続演出になる）。
          el.append(
            '<style>@keyframes _drBootPulse{0%,100%{opacity:.5;transform:scale(.96)}50%{opacity:1;transform:scale(1)}}' +
            '[data-testid="stAppSkeleton"],[data-testid="stSkeleton"]{display:none!important}</style>' +
            // mk(sticky): sticky=true（持久戦リカバリ中）は時間切れで剥がさず、実画面が出るまで保持。
            // window._danranBootSplash として公開し、7秒ウォッチドッグの持久戦モードからも呼べる。
            '<script>(function(){function mk(sticky,label){try{' +
            'var d=document.getElementById("_danran_boot_splash");' +
            'if(!d){' +
            'd=document.createElement("div");d.id="_danran_boot_splash";' +
            'd.style.cssText="position:fixed;inset:0;z-index:2147483647;background:#1a1614;pointer-events:none;' +
            'display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;transition:opacity .25s ease;";' +
            'd.innerHTML="<div style=\'font-size:3.2rem;animation:_drBootPulse 1.2s ease-in-out infinite\'>\\ud83c\\udfe0</div>' +
            '<div style=\'color:rgba(240,232,224,.5);font-size:.85rem;font-weight:700;letter-spacing:.12em\'>danran</div>' +
            '<div id=_danran_boot_sub style=\'color:rgba(240,232,224,.35);font-size:.75rem;min-height:1em\'></div>";' +
            'document.body.appendChild(d);var t0=Date.now();var cfgAt=0;' +
            'var fade=function(){d.style.opacity="0";setTimeout(function(){if(d.parentNode)d.remove();},260);};' +
            // 剥がすのは「最終画面が実際に描画されたとき」: ルーム一覧 or チャット入力欄 or ログインフォーム。
            // cfg(設定タグ)だけで剥がすと組み立て途中がチラ見えする。出現後 250ms 待って塗り完了させてから。
            // フォールバック: cfg 出現から 3s 経過(0ルーム招待待ち等のレア画面) / 12s 強制（sticky 中は無効）。
            '(function chk(){' +
            'var hard=document.getElementById("_danran_room_list")||' +
            'document.querySelector("[data-testid=\\u0022stChatInput\\u0022]")||' +
            'document.querySelector("input[type=password]");' +
            'if(!cfgAt&&document.getElementById("_danran_cfg"))cfgAt=Date.now();' +
            'if(hard){setTimeout(fade,250);return;}' +
            'if(d.getAttribute("data-sticky")!=="1"&&((cfgAt&&Date.now()-cfgAt>3000)||Date.now()-t0>12000)){fade();return;}' +
            'setTimeout(chk,150);})();' +
            '}' +
            'if(sticky)d.setAttribute("data-sticky","1");' +
            'var sub=document.getElementById("_danran_boot_sub");' +
            'if(sub&&label!==undefined)sub.textContent=label||"";' +
            '}catch(e){}}' +
            'window._danranBootSplash=mk;' +
            'if(document.body){mk();}else{document.addEventListener("DOMContentLoaded",function(){mk();});}' +
            '})();</script>',
            { html: true },
          );
          el.append('<link rel="icon" type="image/png" href="/icons/icon-192.png">', { html: true });
          el.append('<link rel="apple-touch-icon" href="/icons/icon-192.png">', { html: true });
          el.append('<link rel="apple-touch-icon" sizes="180x180" href="/icons/icon-192.png">', { html: true });
          el.append('<link rel="apple-touch-icon" sizes="192x192" href="/icons/icon-192.png">', { html: true });
          el.append('<meta name="apple-mobile-web-app-capable" content="yes">', { html: true });
          el.append('<meta name="mobile-web-app-capable" content="yes">', { html: true });
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
  const wsPath = url.pathname + url.search;

  // ブラウザ向け WebSocket ペア
  const [client, server] = Object.values(new WebSocketPair());
  server.accept();

  // ★ 重要: Sec-WebSocket-Protocol を転送（streamlit + auth token）
  // Streamlit JS は new WebSocket(url, ['streamlit', 'PLACEHOLDER_AUTH_TOKEN']) で接続する
  // このサブプロトコルをそのまま upstream に転送しないと Streamlit が認証できない
  const wsProtocol = request.headers.get('Sec-WebSocket-Protocol');

  // ★ 認証 cookie を注入（旧 /~/+/ バイパス廃止により必須）
  const cookie = await ensureSession(false);

  // ★ 上流 WS 接続はコールド時（しばらく放置後の初回）に失敗しやすいので最大4回リトライ。
  //   2回目以降は上流ホストを「キャッシュ無視」で取り直す。トンネル再起動直後は worker の
  //   ホストキャッシュ(60s)が死んだ旧ホストを指し続け、その間ずっと繋がらない＝
  //   「真っ暗/スプラッシュのまま」の一因だった。接続失敗をトリガーに即時自己修復する。
  let upstreamWS = null;
  let agreedProtocol = null;
  let lastErr = null;
  for (let attempt = 0; attempt < 4 && !upstreamWS; attempt++) {
    const host = await getUpstreamHost(attempt > 0);
    const upstreamUrl = `https://${host}${wsPath}`;
    const upstreamHeaders = new Headers();
    upstreamHeaders.set('Host', host);
    upstreamHeaders.set('User-Agent', UA);
    upstreamHeaders.set('Origin', `https://${host}`);
    upstreamHeaders.set('Upgrade', 'websocket');
    upstreamHeaders.set('Connection', 'Upgrade');
    upstreamHeaders.set('Sec-WebSocket-Version', '13');
    if (wsProtocol) upstreamHeaders.set('Sec-WebSocket-Protocol', wsProtocol);
    if (cookie) upstreamHeaders.set('Cookie', cookie);
    try {
      const upstreamResp = await fetch(upstreamUrl, { headers: upstreamHeaders });
      if (!upstreamResp.webSocket) {
        const body = await upstreamResp.text().catch(() => '');
        throw new Error(`upstream returned ${upstreamResp.status}: ${body.slice(0, 80)}`);
      }
      upstreamWS = upstreamResp.webSocket;
      agreedProtocol = upstreamResp.headers.get('Sec-WebSocket-Protocol');
      upstreamWS.accept();
      console.log(`[WS] connected to upstream (try ${attempt + 1}): ${wsPath}, protocol=${agreedProtocol}`);
    } catch (err) {
      lastErr = err;
      if (attempt < 3) await new Promise((r) => setTimeout(r, 350 * (attempt + 1)));
    }
  }
  if (!upstreamWS) {
    console.error('[WS] upstream connect failed after retries:', lastErr && lastErr.message);
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
