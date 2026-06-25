#!/bin/bash
# danran: Cloudflare クイックトンネルを起動し、払い出された現URLを CF KV と Supabase の両方に登録する。
# URL は再起動で変わるが worker がこの値を読むので自動追従する。
# LaunchAgent (com.danran.tunnel) から KeepAlive で起動される想定。
set -u
SECRETS="$HOME/danran/.streamlit/secrets.toml"
SB_URL=$(grep -E '^[[:space:]]*url[[:space:]]*=' "$SECRETS" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
SB_KEY=$(grep -E '^[[:space:]]*anon_key[[:space:]]*=' "$SECRETS" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
WORKER_URL="https://danran-chat.kinakonism.workers.dev"
CF="$HOME/.local/bin/cloudflared"
LOG=/tmp/danran_tunnel.log
: > "$LOG"
# ★ 127.0.0.1 を明示（localhost だと ::1=IPv6 が先に解決され、app は IPv4 のみ listen のため
#   cloudflared が origin 到達失敗→断続的 502→「真っ暗」になる。IPv4 直指定で根絶）。
# ★ --edge-ip-version 4：cloudflared→Cloudflareエッジを IPv4 に固定。mini の IPv6 経路が
#   不安定で、QUIC/IPv6 だと HTTP 遅延・WS中継(client→server)不調・URL不安定を招くため。
# ★ --protocol http2：QUIC(UDP) より TCP の方が不安定網で安定しやすい。
# ★ ポートは 8601（2026-06-07 に 8501 から移転）。8501 は過去の接続嵐で TIME_WAIT ゾンビが
#   ~2800 個恒久滞留し、新規接続が約1割の確率で衝突→SYN握りつぶし→「真っ暗」の原因だった。
#   PORT は app の LaunchAgent 環境変数と一致させること。
"$CF" tunnel --no-autoupdate --edge-ip-version 4 --protocol http2 --url http://127.0.0.1:${DANRAN_PORT:-8601} >> "$LOG" 2>&1 &
CFPID=$!
URL=""
for i in $(seq 1 40); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1)
  [ -n "$URL" ] && break
  kill -0 "$CFPID" 2>/dev/null || break
  sleep 2
done
if [ -n "$URL" ]; then
  HOST=${URL#https://}
  # ★ 1st: Worker の /tunnel-admin/update 経由で CF KV を更新
  #   Supabase REST API が停止中でも KV は確実に動く。
  curl -s -m 15 -X POST "$WORKER_URL/tunnel-admin/update" \
    -H "x-danran-auth: $SB_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"host\":\"$HOST\"}" >> "$LOG" 2>&1 \
    && echo "[tunnel_run] KV registered tunnel_host=$HOST" \
    || echo "[tunnel_run] KV registration failed (continuing)"
  # ★ 2nd: Supabase にも登録（Supabase が生きているときの追従）
  curl -s -m 15 -X POST "$SB_URL/rest/v1/app_config?on_conflict=key" \
    -H "apikey: $SB_KEY" -H "Authorization: Bearer $SB_KEY" \
    -H "Content-Type: application/json" -H "Prefer: resolution=merge-duplicates" \
    -d "[{\"key\":\"tunnel_host\",\"value\":\"$HOST\",\"updated_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}]" \
    >> "$LOG" 2>&1 \
    && echo "[tunnel_run] Supabase registered tunnel_host=$HOST" \
    || echo "[tunnel_run] Supabase registration failed (OK if restricted)"
fi
wait "$CFPID"
