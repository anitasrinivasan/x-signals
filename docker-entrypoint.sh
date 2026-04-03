#!/bin/bash
set -e

# Install nightly sync + monthly re-cluster cron jobs
(
  echo "0 23 * * * python /app/sync_bookmarks.py >> /app/data/sync.log 2>&1"
  echo "15 23 * * * python /app/sync_linkedin.py >> /app/data/sync_linkedin.log 2>&1"
  echo "30 23 1 * * python /app/cluster.py >> /app/data/recluster.log 2>&1"
) | crontab -

# Start cron daemon in background
service cron start

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  x-signals"
echo "  Streamlit:  http://127.0.0.1:8501"
echo "  Twitter sync:    23:00 daily"
echo "  LinkedIn sync:   23:15 daily"
echo "  Full re-cluster: 23:30 on 1st/month"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Hand off to Streamlit (PID 1)
exec streamlit run app.py \
  --server.address=127.0.0.1 \
  --server.port=8501 \
  --server.headless=true
