#!/usr/bin/env python3
"""
Import LinkedIn saved posts from a manual browser-console export into the x-signals DB.

This is the no-scrape path: instead of driving a headless browser (which LinkedIn's
bot detection blocks), you run a small JS snippet in your own logged-in LinkedIn tab
that extracts your saved posts to a JSON file. This script imports that file through
the same import → enrich → cluster pipeline the scraper used.

Usage:
    python3 import_li_export.py ~/Downloads/linkedin_saved_posts.json

The snippet produces records shaped like:
    {"id": "<urn>", "text": "...", "authorName": "...", "authorUrl": "...",
     "timestampRel": "3w", "postUrl": "https://www.linkedin.com/feed/update/<urn>/"}
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent


def load_env():
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ[key.strip()] = value.strip()


# Reuse the scraper's relative-timestamp converter so timestamps match exactly.
from sync_linkedin import rel_to_iso


def _urn_from_post_url(post_url: str) -> str:
    """Extract the LinkedIn URN (the DB id) from a /feed/update/<urn>/ URL."""
    post_url = (post_url or "").strip()
    if "/feed/update/" in post_url:
        return post_url.split("/feed/update/", 1)[1].split("?")[0].rstrip("/")
    return post_url.split("?")[0].rstrip("/").split("/")[-1]


def parse_magpie_md(text: str) -> list:
    """
    Parse a Magpie LinkedIn '.md' export (Markdown table) into the same record
    shape the JSON snippet produces: {id, text, authorName, authorUrl,
    timestampRel, postUrl}.

    Table columns (Magpie): Author | Profile | Post URL | Type | Timestamp | Preview
    Column order is resolved from the header row, so it's robust to reordering.
    """
    rows = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
    if len(rows) < 2:
        return []

    def cells(line):
        # Split on unescaped pipes only — Magpie escapes literal '|' in text as '\|'.
        parts = re.split(r"(?<!\\)\|", line)
        # Drop the empty leading/trailing fragments around the outer pipes.
        if parts and parts[0].strip() == "":
            parts = parts[1:]
        if parts and parts[-1].strip() == "":
            parts = parts[:-1]
        return [c.strip().replace("\\|", "|") for c in parts]

    header = [h.lower() for h in cells(rows[0])]
    idx = {name: i for i, name in enumerate(header)}

    def col(c, names):
        for n in names:
            if n in idx and idx[n] < len(c):
                return c[idx[n]]
        return ""

    records = []
    # rows[1] is the |---|---| separator; data starts at rows[2].
    for line in rows[2:]:
        c = cells(line)
        if not c or all(not x for x in c):
            continue
        post_url = col(c, ["post url"])
        urn = _urn_from_post_url(post_url)
        if not urn:
            continue
        records.append({
            "id": urn,
            "text": col(c, ["preview"]),
            "authorName": col(c, ["author"]),
            "authorUrl": col(c, ["profile"]),
            "timestampRel": col(c, ["timestamp"]),
            "postUrl": f"https://www.linkedin.com/feed/update/{urn}/",
        })
    return records


def load_records(path: Path) -> list:
    """Load records from either a Magpie '.md' export or the snippet '.json' export."""
    raw = path.read_text()
    if path.suffix.lower() == ".md" or raw.lstrip().startswith("#"):
        return parse_magpie_md(raw)
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("Export JSON must be a list of records.")
    return data


def to_post(rec: dict) -> dict:
    """Map a console-export record into the post dict that import_linkedin expects."""
    author_url = (rec.get("authorUrl") or "").strip()
    handle = author_url.rstrip("/").split("/")[-1] if author_url else ""
    ts_rel = (rec.get("timestampRel") or "").strip()
    created_iso = (
        rel_to_iso(ts_rel)
        if ts_rel
        else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    )
    return {
        "id": rec["id"],
        "text": rec.get("text", "") or "",
        "author": {"name": rec.get("authorName", "") or "", "handle": handle, "profileUrl": author_url},
        "metrics": {"likes": 0, "comments": 0},
        "createdAt": created_iso[:10],
        "createdAtISO": created_iso,
        "postUrl": rec.get("postUrl", "") or "",
        "source": "linkedin",
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 import_li_export.py <path-to-export.{md,json}>")
        sys.exit(1)

    load_env()

    path = Path(sys.argv[1]).expanduser()
    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    try:
        records = load_records(path)
    except Exception as e:
        print(f"Could not parse export ({path.name}): {e}")
        sys.exit(1)
    print(f"Loaded {len(records)} records from {path.name}")

    from db import get_conn, import_linkedin

    conn = get_conn()
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT id FROM bookmarks WHERE source='linkedin'"
        ).fetchall()
    }
    print(f"DB currently has {len(existing)} LinkedIn posts.")

    posts = [to_post(r) for r in records if r.get("id")]
    new_posts = [p for p in posts if p["id"] not in existing]
    print(f"{len(new_posts)} new, {len(posts) - len(new_posts)} already in DB.")

    if not new_posts:
        print("Nothing new to import.")
        return

    import_linkedin(conn, new_posts, verbose=True)
    conn.close()

    new_ids = [p["id"] for p in new_posts]

    from enrich import enrich_new
    enrich_new(new_ids=new_ids, verbose=True)

    from cluster import cluster_new
    stats = cluster_new(new_ids=new_ids) or {}

    total = len(existing) + len(new_posts)
    print(f"\n✅ Imported {len(new_posts)} new LinkedIn posts (total: {total}).")
    if stats:
        print(
            f"   📚 Clustered: {stats.get('assigned', 0)} into narratives "
            f"({stats.get('unassigned', 0)} unclustered)"
        )


if __name__ == "__main__":
    main()
