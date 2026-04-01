#!/usr/bin/env python3
"""
SQLite schema, import, and query helpers for x-signals.

Usage:
    python3 db.py              # create schema + import all bookmarks from JSON
    python3 db.py --reimport   # drop and recreate (full reimport)
"""

import json
import sqlite3
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "signals.db"
MASTER_JSON = SCRIPT_DIR / "twitter_bookmarks.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmarks (
    id                TEXT PRIMARY KEY,
    author_handle     TEXT,
    author_name       TEXT,
    author_verified   INTEGER DEFAULT 0,
    text              TEXT,
    quoted_tweet_id   TEXT,
    quoted_tweet_text TEXT,
    urls              TEXT,
    media_types       TEXT,
    likes             INTEGER DEFAULT 0,
    views             INTEGER DEFAULT 0,
    created_at        TEXT,
    -- enriched fields (NULL until processed by enrich.py)
    topics            TEXT,
    subtopics         TEXT,
    content_type      TEXT,
    authority         TEXT,
    summary           TEXT,
    core_claim        TEXT,
    position          TEXT,
    entities          TEXT,
    enriched_at       TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS bookmarks_fts USING fts5(
    id UNINDEXED,
    author_handle,
    text,
    summary,
    topics,
    entities,
    content=bookmarks,
    content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS bookmarks_ai AFTER INSERT ON bookmarks BEGIN
    INSERT INTO bookmarks_fts(rowid, id, author_handle, text, summary, topics, entities)
    VALUES (new.rowid, new.id, new.author_handle, new.text, new.summary, new.topics, new.entities);
END;

CREATE TRIGGER IF NOT EXISTS bookmarks_au AFTER UPDATE ON bookmarks BEGIN
    INSERT INTO bookmarks_fts(bookmarks_fts, rowid, id, author_handle, text, summary, topics, entities)
    VALUES ('delete', old.rowid, old.id, old.author_handle, old.text, old.summary, old.topics, old.entities);
    INSERT INTO bookmarks_fts(rowid, id, author_handle, text, summary, topics, entities)
    VALUES (new.rowid, new.id, new.author_handle, new.text, new.summary, new.topics, new.entities);
END;

CREATE TRIGGER IF NOT EXISTS bookmarks_ad AFTER DELETE ON bookmarks BEGIN
    INSERT INTO bookmarks_fts(bookmarks_fts, rowid, id, author_handle, text, summary, topics, entities)
    VALUES ('delete', old.rowid, old.id, old.author_handle, old.text, old.summary, old.topics, old.entities);
END;
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(drop_existing=False):
    conn = get_conn()
    if drop_existing:
        conn.execute("DROP TABLE IF EXISTS bookmarks_fts")
        conn.execute("DROP TRIGGER IF EXISTS bookmarks_ai")
        conn.execute("DROP TRIGGER IF EXISTS bookmarks_au")
        conn.execute("DROP TRIGGER IF EXISTS bookmarks_ad")
        conn.execute("DROP TABLE IF EXISTS bookmarks")
        conn.commit()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def tweet_to_row(t):
    """Convert a tweet dict (from twitter_bookmarks.json) to a db row tuple."""
    author = t.get("author", {})
    quoted = t.get("quotedTweet") or {}
    metrics = t.get("metrics", {})
    media = t.get("media", [])

    return {
        "id": t.get("id"),
        "author_handle": author.get("screenName") or author.get("username"),
        "author_name": author.get("name"),
        "author_verified": 1 if author.get("verified") else 0,
        "text": t.get("text", ""),
        "quoted_tweet_id": quoted.get("id"),
        "quoted_tweet_text": quoted.get("text"),
        "urls": json.dumps(t.get("urls", [])),
        "media_types": json.dumps([m.get("type") for m in media]),
        "likes": metrics.get("likes", 0) or 0,
        "views": metrics.get("views", 0) or 0,
        "created_at": t.get("createdAtISO") or t.get("createdAt"),
    }


def import_from_json(conn=None, json_path=None, verbose=True):
    """Import all tweets from master JSON into SQLite. Skips existing rows."""
    close = conn is None
    if conn is None:
        conn = get_conn()
    if json_path is None:
        json_path = MASTER_JSON

    with open(json_path) as f:
        tweets = json.load(f)

    inserted = 0
    skipped = 0
    for t in tweets:
        row = tweet_to_row(t)
        if not row["id"]:
            continue
        try:
            conn.execute(
                """INSERT OR IGNORE INTO bookmarks
                   (id, author_handle, author_name, author_verified, text,
                    quoted_tweet_id, quoted_tweet_text, urls, media_types,
                    likes, views, created_at)
                   VALUES (:id, :author_handle, :author_name, :author_verified, :text,
                           :quoted_tweet_id, :quoted_tweet_text, :urls, :media_types,
                           :likes, :views, :created_at)""",
                row,
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"[warn] Failed to insert {row['id']}: {e}", file=sys.stderr)

    conn.commit()
    if verbose:
        print(f"Imported {inserted} new rows, skipped {skipped} existing.")
    if close:
        conn.close()
    return inserted


def upsert_enrichment(conn, tweet_id, enrichment):
    """Write enrichment fields back to a bookmark row."""
    conn.execute(
        """UPDATE bookmarks SET
               topics=:topics,
               subtopics=:subtopics,
               content_type=:content_type,
               authority=:authority,
               summary=:summary,
               core_claim=:core_claim,
               position=:position,
               entities=:entities,
               enriched_at=:enriched_at
           WHERE id=:id""",
        {
            "id": tweet_id,
            "topics": json.dumps(enrichment.get("topics", [])),
            "subtopics": json.dumps(enrichment.get("subtopics", [])),
            "content_type": enrichment.get("content_type"),
            "authority": enrichment.get("authority"),
            "summary": enrichment.get("summary"),
            "core_claim": enrichment.get("core_claim"),
            "position": enrichment.get("position"),
            "entities": json.dumps(enrichment.get("entities", [])),
            "enriched_at": enrichment.get("enriched_at"),
        },
    )


def query(sql, params=(), db_path=None):
    """Run a read-only SQL query and return list of dicts."""
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stats():
    rows = query(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN enriched_at IS NOT NULL THEN 1 ELSE 0 END) as enriched "
        "FROM bookmarks"
    )
    return rows[0] if rows else {}


if __name__ == "__main__":
    reimport = "--reimport" in sys.argv
    print(f"Initialising DB at {DB_PATH}...")
    conn = init_db(drop_existing=reimport)
    print("Importing bookmarks from JSON...")
    import_from_json(conn)
    s = stats()
    print(f"DB ready: {s['total']} total, {s['enriched']} enriched.")
    conn.close()
