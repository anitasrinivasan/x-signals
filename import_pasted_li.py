#!/usr/bin/env python3
"""
Import the manually-pasted LinkedIn saved posts (fetched via public URLs) into
the x-signals DB, then enrich + cluster through the standard pipeline.

Reads pasted_li_records.json: [{id, authorName, text}, ...]
- created_at is derived from the LinkedIn activity URN (snowflake-style: ms = id >> 22)
- new posts are inserted; existing rows that currently have empty text are updated
  and their enriched_at reset so they re-enrich with the real content.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
RECORDS = SCRIPT_DIR / "pasted_li_records.json"


def load_env():
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ[k.strip()] = v.strip()


def urn_to_iso(urn: str) -> str:
    """Derive an ISO timestamp from a LinkedIn activity/share URN."""
    try:
        num = int(urn.rstrip("/").split(":")[-1])
        ms = num >> 22
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S+00:00"
        )
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def main():
    load_env()
    records = json.loads(RECORDS.read_text())
    print(f"Loaded {len(records)} pasted records.")

    from db import get_conn, import_linkedin

    conn = get_conn()
    existing = {
        r["id"]: (r["text"] or "")
        for r in conn.execute(
            "SELECT id, text FROM bookmarks WHERE source='linkedin'"
        ).fetchall()
    }

    new_posts = []
    updated_ids = []
    for rec in records:
        rid = rec["id"]
        iso = urn_to_iso(rid)
        post_url = f"https://www.linkedin.com/feed/update/{rid}/"
        if rid in existing:
            # Refresh rows that were imported earlier with empty text.
            if len(existing[rid]) < len(rec["text"]):
                conn.execute(
                    "UPDATE bookmarks SET text=?, author_name=?, created_at=?, "
                    "urls=?, enriched_at=NULL WHERE id=?",
                    (rec["text"], rec["authorName"], iso,
                     json.dumps([post_url]), rid),
                )
                updated_ids.append(rid)
            continue
        new_posts.append({
            "id": rid,
            "text": rec["text"],
            "author": {"name": rec["authorName"], "handle": "", "profileUrl": ""},
            "metrics": {"likes": 0, "comments": 0},
            "createdAt": iso[:10],
            "createdAtISO": iso,
            "postUrl": post_url,
            "source": "linkedin",
        })

    conn.commit()
    print(f"{len(new_posts)} new to insert, {len(updated_ids)} existing refreshed.")

    if new_posts:
        import_linkedin(conn, new_posts, verbose=True)
    conn.close()

    all_ids = [p["id"] for p in new_posts] + updated_ids
    if not all_ids:
        print("Nothing to enrich.")
        return

    print(f"\nEnriching {len(all_ids)} posts...")
    from enrich import enrich_new
    enrich_new(new_ids=all_ids, verbose=True)

    print("\nClustering...")
    from cluster import cluster_new
    stats = cluster_new(new_ids=all_ids) or {}
    print(f"Clustered: {stats.get('assigned', 0)} assigned, "
          f"{stats.get('unassigned', 0)} unclustered.")


if __name__ == "__main__":
    main()
