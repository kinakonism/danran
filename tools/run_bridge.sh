#!/bin/zsh
# danran AIサポート bridge ランチャー（launchd から起動される想定）
# nvm を読み込んで node/claude を PATH に乗せ、Homebrew python3 で bridge を実行する。
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" >/dev/null 2>&1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$HOME/danran" || exit 1
exec /opt/homebrew/bin/python3 -u tools/ai_bridge.py   # -u: ログを即時フラッシュ
