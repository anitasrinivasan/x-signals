#!/usr/bin/env bash
# x-signals local setup
set -e

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

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Setup complete! Starting x-signals..."
echo "  Open http://localhost:8501 in your browser"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
streamlit run app.py
