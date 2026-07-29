#!/bin/bash
# danran: mini 健康監視（LaunchAgent com.danran.health・1時間ごと）
# 2026-06-07 の障害（82日稼働でカーネルのTCP残骸掃除が停止→ポート枯渇→真っ暗）の再発防止。
# 兆候を検知したら まさと に Web Push で「再起動どき」を知らせる。アラートは12時間に1回まで。
PORT="${DANRAN_PORT:-8701}"

# ★ 残骸カウントは「現行アプリポート」に限定する（2026-07-29）。
#   全体カウントだと、ポート移転で退避した後も旧ポートの死骸（再起動まで消えない）を
#   数え続けて警報が鳴り止まない。実害を生むのは現行ポートの4タプル汚染だけ。
#   閾値 2500 は 2026-06 に実害（SYN握りつぶし）が出た ~2800 の手前。
TW=$(netstat -an | grep "\.${PORT} " | grep -c TIME_WAIT)
FAIL=0
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -m 3 "http://127.0.0.1:${PORT}/_stcore/health" || FAIL=$((FAIL+1))
done

MSG=""
[ "$TW" -gt 2500 ] && MSG="ポート${PORT}のTCP残骸が ${TW} 個に蓄積（2,500超）。次の空きポートへ移転か再起動を。"
[ "$FAIL" -ge 2 ] && MSG="${MSG}ローカル接続が ${FAIL}/5 失敗。"
[ -z "$MSG" ] && exit 0

# 12時間に1回だけ通知
STAMP=/tmp/danran_health_alert
if [ -f "$STAMP" ]; then
  AGE=$(( $(date +%s) - $(stat -f %m "$STAMP") ))
  [ "$AGE" -lt 43200 ] && exit 0
fi
touch "$STAMP"

cd "$HOME/danran" || exit 0
.bridge-venv/bin/python - << PYEOF 2>/dev/null
import sys
sys.path.insert(0, "tools")
from ai_bridge import push_to_owner
push_to_owner("🏥 mini 健康アラート",
              "${MSG}そろそろ再起動どきかも。手順: ターミナルで ssh -t mini 'sudo fdesetup authrestart' → 起動後に画面共有でログイン")
PYEOF
echo "$(date '+%m-%d %H:%M:%S') alerted: ${MSG}" >> /tmp/danran_health_watch.log
