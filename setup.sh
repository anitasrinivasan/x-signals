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
echo "→ Installing Python dependencies..."
pip install -r requirements.txt -q

# Install Playwright's headless Chromium (needed for LinkedIn sync)
echo "→ Installing Playwright Chromium (~300MB, one-time)..."
python3 -m playwright install chromium --with-deps || {
  echo "⚠️  Playwright Chromium install failed. LinkedIn sync will not work."
  echo "    To fix later: source venv/bin/activate && playwright install chromium"
}

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
  echo ""
  echo "LinkedIn saved posts sync is optional (press Enter to skip)."
  echo "  To get your li_at cookie: log into linkedin.com → DevTools → Application → Cookies → li_at"
  read -p "LINKEDIN_LI_AT (optional): " li_at
  if [ -n "$li_at" ]; then
    echo "LINKEDIN_LI_AT=$li_at" >> .env
  fi

  echo ""
  echo "Telegram notifications are optional (press Enter to skip both)."
  read -p "TELEGRAM_BOT_TOKEN: " tg_token
  read -p "TELEGRAM_CHAT_ID: " tg_chat
  if [ -n "$tg_token" ]; then
    echo "TELEGRAM_BOT_TOKEN=$tg_token" >> .env
    echo "TELEGRAM_CHAT_ID=$tg_chat"   >> .env
  fi

  chmod 600 .env
  echo "✅ .env created (permissions: 600)."
fi

# Ensure .env is always owner-only readable, even if it predates this script
chmod 600 .env 2>/dev/null || true

# Validate credentials before running the pipeline
echo ""
echo "→ Validating credentials..."
python3 - << 'PYEOF'
import os, sys
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
for line in open(env_path):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ[k.strip()] = v.strip()
missing = [k for k in ["ANTHROPIC_API_KEY", "TWITTER_AUTH_TOKEN", "TWITTER_CT0"] if not os.environ.get(k)]
if missing:
    print("❌ Missing required credentials: " + ", ".join(missing))
    print("   Re-run setup.sh or fill in .env manually.")
    sys.exit(1)
try:
    import anthropic
    anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"]).models.list()
    print("✅ Anthropic API key valid")
except Exception as e:
    print(f"❌ Anthropic API key invalid: {e}")
    sys.exit(1)
PYEOF

# First-time data pipeline
echo ""
echo "→ Running first-time sync (this fetches your bookmarks — may take a few minutes)..."
python3 sync_bookmarks.py --full || {
  echo "❌ Twitter sync failed. Check your TWITTER_AUTH_TOKEN and TWITTER_CT0 cookies in .env"
  exit 1
}

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
    <string>--server.address=127.0.0.1</string>
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

    # ── 3. LinkedIn sync agent (23:15, staggered after Twitter) ─────────────
    local LINKEDIN_PLIST="$PLIST_DIR/com.x-signals.linkedin.plist"
    cat > "$LINKEDIN_PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.x-signals.linkedin</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$SCRIPT_DIR/sync_linkedin.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>23</integer>
    <key>Minute</key><integer>15</integer>
  </dict>
  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR</string>
  <key>StandardOutPath</key>
  <string>$SCRIPT_DIR/sync_linkedin.log</string>
  <key>StandardErrorPath</key>
  <string>$SCRIPT_DIR/sync_linkedin.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$SCRIPT_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
PLIST

    # ── 4. Monthly full re-cluster (1st of month, 23:30) ────────────────────
    local RECLUSTER_PLIST="$PLIST_DIR/com.x-signals.recluster.plist"
    cat > "$RECLUSTER_PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.x-signals.recluster</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$SCRIPT_DIR/cluster.py</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Day</key><integer>1</integer>
    <key>Hour</key><integer>23</integer>
    <key>Minute</key><integer>30</integer>
  </dict>
  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR</string>
  <key>StandardOutPath</key>
  <string>$SCRIPT_DIR/recluster.log</string>
  <key>StandardErrorPath</key>
  <string>$SCRIPT_DIR/recluster.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$SCRIPT_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
PLIST

    # Load all agents (unload first in case of re-run)
    launchctl unload "$SYNC_PLIST"      2>/dev/null || true
    launchctl unload "$APP_PLIST"       2>/dev/null || true
    launchctl unload "$LINKEDIN_PLIST"  2>/dev/null || true
    launchctl unload "$RECLUSTER_PLIST" 2>/dev/null || true
    launchctl load   "$SYNC_PLIST"
    launchctl load   "$APP_PLIST"
    launchctl load   "$LINKEDIN_PLIST"
    launchctl load   "$RECLUSTER_PLIST"

    echo ""
    echo "✅ Scheduler installed:"
    echo "   • Twitter sync:    23:00 daily        (com.x-signals.sync)"
    echo "   • LinkedIn sync:   23:15 daily        (com.x-signals.linkedin)"
    echo "   • Full re-cluster: 23:30 on 1st/month (com.x-signals.recluster)"
    echo "   • Persistent app:  running now, restarts on reboot  (com.x-signals.app)"

  else
    # Linux: add crontab entries (idempotent — remove existing x-signals lines first)
    ( crontab -l 2>/dev/null | grep -v "x-signals" ; \
      echo "0 23 * * * $PYTHON_BIN $SCRIPT_DIR/sync_bookmarks.py >> $SCRIPT_DIR/sync.log 2>&1" ; \
      echo "15 23 * * * $PYTHON_BIN $SCRIPT_DIR/sync_linkedin.py >> $SCRIPT_DIR/sync_linkedin.log 2>&1" ; \
      echo "30 23 1 * * $PYTHON_BIN $SCRIPT_DIR/cluster.py >> $SCRIPT_DIR/recluster.log 2>&1" \
    ) | crontab -

    echo ""
    echo "✅ Cron jobs installed:"
    echo "   • Twitter sync:    23:00 daily"
    echo "   • LinkedIn sync:   23:15 daily"
    echo "   • Full re-cluster: 23:30 on 1st of month"
    echo "   To start the app now:  source venv/bin/activate && streamlit run app.py"
    echo "   To keep it running:    use screen, tmux, or a systemd unit"
  fi
}

install_scheduler

# Harden log file permissions (create if absent, then lock to owner-only)
touch "$SCRIPT_DIR/sync.log" "$SCRIPT_DIR/app.log" "$SCRIPT_DIR/recluster.log"
chmod 600 "$SCRIPT_DIR/sync.log" "$SCRIPT_DIR/app.log" "$SCRIPT_DIR/recluster.log"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete!"
echo "  App: http://localhost:8501"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
