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
import sys
import uuid

# カレントディレクトリをこのファイルと同じ場所に固定
os.chdir(os.path.dirname(os.path.abspath(__file__)))

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
