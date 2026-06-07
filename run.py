"""
danran 起動スクリプト
  uv run python run.py

通常の `uv run streamlit run app.py` の代わりに使う。
Streamlit の Starlette サーバーが起動する前に
/sw.js と /manifest.json のルートを差し込むことで、
PWA + Web Push を実現する。
（Streamlit 1.57 は Tornado ではなく Starlette ベースのため、
  このパッチが唯一の Python-only 解）
"""
import base64
import os
import socket
import sys
import uuid

# カレントディレクトリをこのファイルと同じ場所に固定
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# ── IPv4 優先（mini 自前ホストの TLS handshake ストール対策）─────────────────
#   macOS の mini では Supabase 等への IPv6/Happy Eyeballs 経路がたまに ~20秒ストールし、
#   起動時 get_session_user() 等の DB クエリが刺さって「暗色スプラッシュのまま真っ暗」になる。
#   プロセス全体の getaddrinfo を IPv4 に絞って回避する（supabase-py/httpx/requests/pywebpush
#   すべて socket.getaddrinfo 経由なので一括で効く）。bridge(tools/ai_bridge.py) と同じ対策。
_USE_IPV4_ONLY = os.environ.get("DANRAN_IPV4_ONLY", "1") != "0"
if _USE_IPV4_ONLY:
    _orig_getaddrinfo = socket.getaddrinfo
    def _getaddrinfo_v4(host, *a, **kw):
        res = _orig_getaddrinfo(host, *a, **kw)
        v4 = [r for r in res if r[0] == socket.AF_INET]
        return v4 or res
    socket.getaddrinfo = _getaddrinfo_v4

# ── ストール自己診断（真っ暗調査・2026-06-07）────────────────────────────────
#   症状: CPU 0%・資源余裕なのに localhost への health が 10 秒以上応答しない瞬間がある
#   （= イベントループが同期処理で固まっている疑い）。固まった瞬間に全スレッドの
#   スタックを /tmp/danran_stall.log へダンプして犯人の行を特定する。
def _start_stall_watch():
    import threading, faulthandler, urllib.request, time as _t, datetime as _dt
    port = os.environ.get("PORT", "8501")
    def _watch():
        _t.sleep(30)   # 起動完了を待つ
        while True:
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/_stcore/health", timeout=6)
                _t.sleep(30)   # 7s→30s: ループバック接続の浪費を抑える（TIME_WAIT蓄積対策）
            except Exception as e:
                try:
                    with open("/tmp/danran_stall.log", "a") as f:
                        f.write(f"\n===== {_dt.datetime.now():%m-%d %H:%M:%S} "
                                f"stall: {type(e).__name__} {e} =====\n")
                        faulthandler.dump_traceback(file=f)
                except Exception:
                    pass
                _t.sleep(30)   # 連発抑制
    threading.Thread(target=_watch, daemon=True, name="danran-stall-watch").start()
_start_stall_watch()

# ── FD 上限の引き上げ（LaunchAgent のデフォルト soft limit=256 対策）──────────
#   アイドルでも ~150 FD 消費しており、家族同時利用＋リロードで 256 に当たると
#   accept() が EMFILE で止まり、cloudflared から「dial tcp 127.0.0.1:8501: i/o timeout」
#   → 利用中にバーストで 502/真っ暗、の原因になる。プロセス内で引き上げる。
try:
    import resource
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    for _target in (10240, 8192, 4096, 2048):
        if _soft >= _target:
            break
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (_target, _hard))
            break
        except (ValueError, OSError):
            continue
except Exception:
    pass

# ────────────────────────────────────────────────
# Starlette の create_streamlit_routes を上書き
# → /sw.js と /manifest.json を最優先ルートとして追加
# ────────────────────────────────────────────────
import streamlit.web.server.starlette.starlette_app as _starlette_mod
from starlette.routing import Route
from starlette.responses import FileResponse

_ICONS_DIR = os.path.join(os.path.dirname(__file__), "icons")
_SW_PATH   = os.path.join(os.path.dirname(__file__), "sw.js")
_MF_PATH   = os.path.join(os.path.dirname(__file__), "manifest.json")

async def _serve_sw(request):
    return FileResponse(
        _SW_PATH,
        media_type="application/javascript; charset=utf-8",
        headers={
            "Cache-Control":        "no-store, no-cache, must-revalidate",
            "Service-Worker-Allowed": "/",   # scope をルートに拡張（必須）
        },
    )

async def _serve_manifest(request):
    return FileResponse(
        _MF_PATH,
        media_type="application/manifest+json; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )

async def _serve_icons(request):
    """/icons/ 以下のファイルを返す（アイコン等）
    注: /static/ は Streamlit 自身が使うパスなので衝突を避けるため /icons/ を使う。"""
    filename = request.path_params["filename"]
    path = os.path.join(_ICONS_DIR, filename)
    if not os.path.isfile(path):
        from starlette.responses import Response
        return Response(status_code=404)
    return FileResponse(path)

async def _serve_mobileconfig(request):
    """/install.mobileconfig — iOS Web Clip プロファイルを動的生成して返す。
    ユーザーが Safari でこの URL を開くと「プロファイルをインストール」が自動表示され、
    Settings でインストールするとホーム画面に danran アイコンが自動追加される。
    FullScreen=true により既存 PWA と同じ全画面表示で起動する。
    """
    from starlette.responses import Response

    # リクエストヘッダーからアプリの URL を動的に生成
    host = request.headers.get("host", "localhost:8501")
    # Streamlit Cloud / リバースプロキシ越しは https
    proto = request.headers.get("x-forwarded-proto", "http")
    app_url = f"{proto}://{host}/"

    # アイコンを Base64 エンコード（プロファイルに埋め込む）
    icon_path = os.path.join(_ICONS_DIR, "icon-192.png")
    try:
        with open(icon_path, "rb") as f:
            icon_b64 = base64.b64encode(f.read()).decode()
    except OSError:
        icon_b64 = ""

    profile_uuid = str(uuid.uuid4()).upper()
    webclip_uuid = str(uuid.uuid4()).upper()

    plist = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        '<dict>\n'
        '  <key>PayloadContent</key>\n'
        '  <array>\n'
        '    <dict>\n'
        '      <key>FullScreen</key><true/>\n'
        f'      <key>Icon</key><data>{icon_b64}</data>\n'
        '      <key>IsRemovable</key><true/>\n'
        '      <key>Label</key><string>danran</string>\n'
        '      <key>PayloadDescription</key><string>danran ホーム画面に追加</string>\n'
        '      <key>PayloadDisplayName</key><string>danran</string>\n'
        '      <key>PayloadIdentifier</key><string>com.danran.webclip</string>\n'
        '      <key>PayloadType</key><string>com.apple.webClip.managed</string>\n'
        f'      <key>PayloadUUID</key><string>{webclip_uuid}</string>\n'
        '      <key>PayloadVersion</key><integer>1</integer>\n'
        f'      <key>URL</key><string>{app_url}</string>\n'
        '    </dict>\n'
        '  </array>\n'
        '  <key>PayloadDescription</key>\n'
        '  <string>danran をホーム画面に追加します</string>\n'
        '  <key>PayloadDisplayName</key><string>danran 🏠</string>\n'
        f'  <key>PayloadIdentifier</key><string>com.danran.profile.{profile_uuid.lower()}</string>\n'
        '  <key>PayloadRemovalDisallowed</key><false/>\n'
        '  <key>PayloadType</key><string>Configuration</string>\n'
        f'  <key>PayloadUUID</key><string>{profile_uuid}</string>\n'
        '  <key>PayloadVersion</key><integer>1</integer>\n'
        '</dict>\n'
        '</plist>'
    )

    return Response(
        content=plist,
        media_type="application/x-apple-aspen-config",
        headers={
            "Content-Disposition": 'attachment; filename="danran.mobileconfig"',
            "Cache-Control": "no-store, no-cache",
        },
    )

_orig_create_routes = _starlette_mod.create_streamlit_routes

def _patched_create_routes(runtime):
    original = _orig_create_routes(runtime)
    custom = [
        Route("/sw.js",                    endpoint=_serve_sw),
        Route("/manifest.json",            endpoint=_serve_manifest),
        Route("/icons/{filename:path}",    endpoint=_serve_icons),
        Route("/install.mobileconfig",     endpoint=_serve_mobileconfig),
    ]
    return custom + original   # カスタムルートを最優先

_starlette_mod.create_streamlit_routes = _patched_create_routes

# ────────────────────────────────────────────────
# Streamlit CLI を直接呼び出す
# ────────────────────────────────────────────────
from streamlit.web import cli as _st_cli

sys.argv = [
    "streamlit", "run", "app.py",
    "--server.port",      os.environ.get("PORT", "8501"),
    "--server.headless",  "true",
]
sys.exit(_st_cli.main())
