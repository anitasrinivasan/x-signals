#!/bin/bash
set -e

# Install nightly sync cron jobs
# Twitter at 23:00, LinkedIn at 23:15 (staggered to avoid overlap)
(
  echo "0 23 * * * python /app/sync_bookmarks.py >> /app/data/sync.log 2>&1"
  echo "15 23 * * * python /app/sync_linkedin.py >> /app/data/sync_linkedin.log 2>&1"
) | crontab -

# Start cron daemon in background
service cron start

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  x-signals"
echo "  Streamlit:  http://0.0.0.0:8501"
echo "  Nightly sync scheduled at 23:00"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Hand off to Streamlit (PID 1)
exec streamlit run app.py \
  --server.address=0.0.0.0 \
  --server.port=8501 \
  --server.headless=true
