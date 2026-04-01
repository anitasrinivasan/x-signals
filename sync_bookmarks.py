#!/usr/bin/env python3
"""
Daily Twitter/X bookmark sync for x-signals.

Fetches the latest 500 bookmarks, deduplicates against the master JSON by
tweet ID, and appends anything new. Sends a Telegram notification on every run.

Usage:
    python3 sync_bookmarks.py           # normal daily sync (500 bookmarks)
    python3 sync_bookmarks.py --full    # pull 5000 (for catching up after a gap)
"""

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
MASTER_JSON = SCRIPT_DIR / "twitter_bookmarks.json"
DAILY_BATCH = 500
FULL_BATCH = 5000


def load_env():
    """Load .env file from the same directory as this script, if present."""
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ[key.strip()] = value.strip()


def get_required_env(key):
    val = os.environ.get(key)
    if not val:
        raise EnvironmentError(
            f"Missing required env var: {key}\n"
            f"Set it in {SCRIPT_DIR / '.env'} or as an environment variable."
        )
    return val


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(bot_token, chat_id, message):
    """Send a Telegram message. Silently skips if credentials are missing."""
    if not bot_token or not chat_id:
        return
    try:
        payload = json.dumps({"chat_id": chat_id, "text": message}).encode()
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[warn] Telegram notification failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Bookmark sync
# ---------------------------------------------------------------------------

def load_master():
    """Load master JSON, return (list_of_dicts, set_of_ids)."""
    if not MASTER_JSON.exists():
        return [], set()
    with open(MASTER_JSON) as f:
        data = json.load(f)
    return data, {t["id"] for t in data}


def save_master(tweets_list):
    """Sort by createdAtISO descending and write back to master JSON."""
    sorted_tweets = sorted(
        tweets_list,
        key=lambda t: t.get("createdAtISO", ""),
        reverse=True,
    )
    with open(MASTER_JSON, "w") as f:
        json.dump(sorted_tweets, f, indent=2)


def sync(batch_size):
    load_env()

    auth_token = get_required_env("TWITTER_AUTH_TOKEN")
    ct0 = get_required_env("TWITTER_CT0")
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    today = date.today().isoformat()

    # Load existing bookmarks
    existing_list, existing_ids = load_master()
    print(f"Loaded {len(existing_list)} existing bookmarks from master JSON.")

    # Fetch from Twitter
    print(f"Fetching up to {batch_size} bookmarks from Twitter...")
    try:
        from twitter_cli.client import TwitterClient
        from twitter_cli.serialization import tweet_to_dict

        # Patch the absolute max count cap so large batches work
        import twitter_cli.client as _tc_client
        _tc_client._ABSOLUTE_MAX_COUNT = max(_tc_client._ABSOLUTE_MAX_COUNT, batch_size)

        client = TwitterClient(
            auth_token=auth_token,
            ct0=ct0,
            rate_limit_config={"maxCount": batch_size, "requestDelay": 2.5},
        )
        fetched_tweets = client.fetch_bookmarks(count=batch_size)

    except Exception as e:
        err = str(e)
        if "auth" in err.lower() or "401" in err or "cookie" in err.lower():
            msg = f"⚠️ x-signals: Twitter cookies expired — update TWITTER_AUTH_TOKEN and TWITTER_CT0 in .env ({today})"
        else:
            msg = f"⚠️ x-signals: sync failed — {err} ({today})"
        print(msg, file=sys.stderr)
        send_telegram(telegram_token, telegram_chat, msg)
        sys.exit(1)

    print(f"Fetched {len(fetched_tweets)} bookmarks from Twitter.")

    # Dedup: keep only tweet IDs not already in master
    new_dicts = []
    for tweet in fetched_tweets:
        d = tweet_to_dict(tweet)
        if d["id"] not in existing_ids:
            new_dicts.append(d)

    # Warn if entire batch was new (may have missed some)
    if len(fetched_tweets) == batch_size and len(new_dicts) == batch_size:
        print(
            f"[warn] All {batch_size} fetched bookmarks were new. "
            f"You may have bookmarked more than {batch_size} items since the last sync. "
            f"Run with --full to catch up."
        )

    if not new_dicts:
        msg = f"☑️ x-signals: no new bookmarks — {today}"
        print(msg)
        send_telegram(telegram_token, telegram_chat, msg)
        return

    # Merge and save
    merged = existing_list + new_dicts
    save_master(merged)

    # Upsert new rows into SQLite, enrich, and cluster
    cluster_stats = {}
    try:
        from db import get_conn, init_db, import_from_json
        from enrich import enrich_new
        db_conn = get_conn()
        if db_conn is None:
            init_db()
            db_conn = get_conn()
        import_from_json(db_conn, verbose=False)
        db_conn.close()
        enrich_new(new_ids=[d["id"] for d in new_dicts], verbose=True)
        from cluster import cluster_new
        cluster_stats = cluster_new(new_ids=[d["id"] for d in new_dicts]) or {}
    except Exception as e:
        print(f"[warn] DB/enrichment step failed: {e}", file=sys.stderr)

    # Build Telegram message
    lines = [f"✅ x-signals: +{len(new_dicts)} new bookmarks (total: {len(merged)}) — {today}"]
    if cluster_stats:
        assigned   = cluster_stats.get("assigned", 0)
        unassigned = cluster_stats.get("unassigned", 0)
        lines.append(f"   📚 Enriched: {len(new_dicts)} · Clustered: {assigned} into narratives"
                     + (f" ({unassigned} unclustered)" if unassigned else ""))
        for title, delta in cluster_stats.get("top_heating", []):
            short = title[:50] + "…" if len(title) > 50 else title
            lines.append(f"   ↑ {short} (+{delta})")

    msg = "\n".join(lines)
    print(msg)
    send_telegram(telegram_token, telegram_chat, msg)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    full = "--full" in sys.argv
    batch = FULL_BATCH if full else DAILY_BATCH
    sync(batch)
