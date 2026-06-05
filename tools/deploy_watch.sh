#!/bin/bash
# danran 自動デプロイ watcher（mini 常駐・LaunchAgent com.danran.deploy が定期実行）。
#
# 役割: origin/main を取り込み、ランタイムに関わるファイルが変わっていたら streamlit app を再起動。
#   - bridge 自動実装のローカルコミット（push 済み＝HEAD が進む）も、
#   - 別マシンからの push（origin が進む→ff取り込み）も、両方カバーする。
#   - 前回デプロイ〜現在の差分にランタイムファイルが無ければ再起動しない（doc/worker 変更で無駄に落とさない）。
#   - 実装途中（tracked ファイルに未コミット変更）があるときは触らない（bridge 編集中のクロバー防止）。
#
# 反映先: app LaunchAgent (com.danran.app)。tunnel/worker は対象外。
set -u
REPO="$HOME/danran"
GIT=/usr/bin/git
STAMP="$HOME/.danran_last_deployed"     # ★ リポジトリ外に置く（git status を汚さない）
LOG=/tmp/danran_deploy.log
cd "$REPO" 2>/dev/null || exit 0
log(){ echo "$(date '+%F %T') $*" >> "$LOG"; }

# 実装途中（tracked ファイルの未コミット変更）があるなら次回まで待つ
if [ -n "$("$GIT" status --porcelain -uno)" ]; then
  log "skip: tracked files dirty (impl in progress?)"; exit 0
fi

"$GIT" fetch -q origin main 2>>"$LOG" || { log "fetch failed"; exit 0; }
LOCAL=$("$GIT" rev-parse HEAD)
REMOTE=$("$GIT" rev-parse origin/main)

# origin が進んでいれば ff で取り込む（ローカルが ancestor のときのみ＝安全）
if [ "$LOCAL" != "$REMOTE" ]; then
  if "$GIT" merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
    "$GIT" merge --ff-only origin/main -q 2>>"$LOG" && log "pulled ${LOCAL:0:7} -> ${REMOTE:0:7}" \
      || { log "ff merge failed"; exit 0; }
  else
    log "diverged (local not ancestor of origin) - manual review needed"
  fi
fi

NEW=$("$GIT" rev-parse HEAD)
LAST=$(cat "$STAMP" 2>/dev/null || echo "")
[ "$NEW" = "$LAST" ] && exit 0   # 変化なし

# 前回デプロイ〜現在の変更ファイルにランタイムが含まれるか判定
RUNTIME=1
if [ -n "$LAST" ] && "$GIT" cat-file -e "$LAST^{commit}" 2>/dev/null; then
  CHANGED=$("$GIT" diff --name-only "$LAST" "$NEW" 2>/dev/null)
  if echo "$CHANGED" | grep -qE '^(app\.py|run\.py|components/|sw\.js|manifest\.json|icons/|\.streamlit/)'; then
    RUNTIME=1
  else
    RUNTIME=0
  fi
fi

if [ "$RUNTIME" = "1" ]; then
  if launchctl kickstart -k "gui/$(id -u)/com.danran.app" 2>>"$LOG"; then
    log "restarted app for ${NEW:0:7}"
  else
    log "kickstart failed for ${NEW:0:7}"; exit 0   # stamp は更新せず次回再試行
  fi
else
  log "no runtime change (${NEW:0:7}) - skip restart"
fi
echo "$NEW" > "$STAMP"
