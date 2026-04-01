#!/usr/bin/env bash
# x-signals local setup — works on macOS and Linux
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  x-signals setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Python check
if ! command -v python3 &>/dev/null; then
  echo "❌ python3 not found. Install from https://python.org and re-run."
  exit 1
fi

# Virtual environment
if [ ! -d venv ]; then
  echo "→ Creating virtual environment..."
  python3 -m venv venv
fi
source venv/bin/activate

# Install dependencies
echo "→ Installing dependencies..."
pip install -r requirements.txt -q

# .env setup
if [ ! -f .env ]; then
  echo ""
  echo "Let's set up your credentials. You'll need:"
  echo "  1. An Anthropic API key (https://console.anthropic.com)"
  echo "  2. Your X/Twitter session cookies (from browser DevTools)"
  echo ""

  read -p "ANTHROPIC_API_KEY: " api_key
  read -p "TWITTER_AUTH_TOKEN: " auth_token
  read -p "TWITTER_CT0: " ct0

  {
    echo "ANTHROPIC_API_KEY=$api_key"
    echo "TWITTER_AUTH_TOKEN=$auth_token"
    echo "TWITTER_CT0=$ct0"
  } > .env

  echo ""
  echo "Telegram notifications are optional (press Enter to skip both)."
  read -p "TELEGRAM_BOT_TOKEN: " tg_token
  read -p "TELEGRAM_CHAT_ID: " tg_chat
  if [ -n "$tg_token" ]; then
    echo "TELEGRAM_BOT_TOKEN=$tg_token" >> .env
    echo "TELEGRAM_CHAT_ID=$tg_chat"   >> .env
  fi

  echo "✅ .env created."
fi

# First-time data pipeline
echo ""
echo "→ Running first-time sync (this fetches your bookmarks — may take a few minutes)..."
python3 sync_bookmarks.py --full

echo "→ Enriching bookmarks with Claude (this may take 2-4 hours for a large corpus)..."
python3 enrich.py

echo "→ Building narrative clusters..."
python3 cluster.py

# ── Scheduler installation ────────────────────────────────────────────────────
install_scheduler() {
  local PYTHON_BIN="$SCRIPT_DIR/venv/bin/python3"

  if [[ "$OSTYPE" == "darwin"* ]]; then
    local PLIST_DIR="$HOME/Library/LaunchAgents"
    mkdir -p "$PLIST_DIR"

    # ── 1. Nightly sync agent ────────────────────────────────────────────────
    local SYNC_PLIST="$PLIST_DIR/com.x-signals.sync.plist"
    cat > "$SYNC_PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.x-signals.sync</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$SCRIPT_DIR/sync_bookmarks.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>23</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR</string>
  <key>StandardOutPath</key>
  <string>$SCRIPT_DIR/sync.log</string>
  <key>StandardErrorPath</key>
  <string>$SCRIPT_DIR/sync.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$SCRIPT_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
PLIST

    # ── 2. Persistent app agent (KeepAlive + RunAtLoad) ──────────────────────
    local APP_PLIST="$PLIST_DIR/com.x-signals.app.plist"
    cat > "$APP_PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.x-signals.app</string>
  <key>ProgramArguments</key>
  <array>
    <string>$SCRIPT_DIR/venv/bin/streamlit</string>
    <string>run</string>
    <string>$SCRIPT_DIR/app.py</string>
    <string>--server.address=0.0.0.0</string>
    <string>--server.port=8501</string>
    <string>--server.headless=true</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR</string>
  <key>StandardOutPath</key>
  <string>$SCRIPT_DIR/app.log</string>
  <key>StandardErrorPath</key>
  <string>$SCRIPT_DIR/app.log</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$SCRIPT_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
PLIST

    # Load both agents (unload first in case of re-run)
    launchctl unload "$SYNC_PLIST" 2>/dev/null || true
    launchctl unload "$APP_PLIST"  2>/dev/null || true
    launchctl load   "$SYNC_PLIST"
    launchctl load   "$APP_PLIST"

    echo ""
    echo "✅ Scheduler installed:"
    echo "   • Nightly sync:    23:00 daily  (com.x-signals.sync)"
    echo "   • Persistent app:  running now, restarts on reboot  (com.x-signals.app)"

  else
    # Linux: add crontab entry (idempotent — remove existing x-signals line first)
    ( crontab -l 2>/dev/null | grep -v "x-signals" ; \
      echo "0 23 * * * $PYTHON_BIN $SCRIPT_DIR/sync_bookmarks.py >> $SCRIPT_DIR/sync.log 2>&1" \
    ) | crontab -

    echo ""
    echo "✅ Cron job installed (nightly sync at 23:00)"
    echo "   To start the app now:  source venv/bin/activate && streamlit run app.py"
    echo "   To keep it running:    use screen, tmux, or a systemd unit"
  fi
}

install_scheduler

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete!"
echo "  App: http://localhost:8501"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
