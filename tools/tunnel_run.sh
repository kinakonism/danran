#!/bin/bash
# danran: Cloudflare クイックトンネルを起動し、払い出された現URLを Supabase app_config(tunnel_host)
# に登録する。URL は再起動で変わるが worker がこの値を読むので自動追従する。
# LaunchAgent (com.danran.tunnel) から KeepAlive で起動される想定。
set -u
SECRETS="$HOME/danran/.streamlit/secrets.toml"
SB_URL=$(grep -E '^[[:space:]]*url[[:space:]]*=' "$SECRETS" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
SB_KEY=$(grep -E '^[[:space:]]*anon_key[[:space:]]*=' "$SECRETS" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')
CF="$HOME/.local/bin/cloudflared"
LOG=/tmp/danran_tunnel.log
: > "$LOG"
"$CF" tunnel --no-autoupdate --url http://localhost:8501 >> "$LOG" 2>&1 &
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
  curl -s -m 15 -X POST "$SB_URL/rest/v1/app_config?on_conflict=key" \
    -H "apikey: $SB_KEY" -H "Authorization: Bearer $SB_KEY" \
    -H "Content-Type: application/json" -H "Prefer: resolution=merge-duplicates" \
    -d "[{\"key\":\"tunnel_host\",\"value\":\"$HOST\",\"updated_at\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}]" >/dev/null \
    && echo "[tunnel_run] registered tunnel_host=$HOST"
fi
wait "$CFPID"
