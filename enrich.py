#!/usr/bin/env python3
"""
Enrichment pipeline for x-signals.

Sends batches of unenriched bookmarks to Claude Haiku, extracts structured
metadata (topics, subtopics, content_type, authority, summary, core_claim,
position, entities), and writes results back to signals.db.

Usage:
    python3 enrich.py                       # enrich all unenriched rows
    python3 enrich.py --ids id1 id2 ...     # re-enrich specific tweet IDs
    python3 enrich.py --limit 100           # enrich only first N unenriched
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
BATCH_SIZE = 25
MODEL = "claude-sonnet-4-5"

TOPICS = [
    "AI Systems & Agents",
    "Regulatory & Policy",
    "Crypto/DeFi/Web3",
    "Digital Asset Regulation",
    "Legal Profession & AI",
    "Developer Infrastructure",
    "Funding & Markets",
    "Research & Philosophy",
]

SYSTEM_PROMPT = f"""You are a research classifier for a legal/policy analyst's Twitter bookmark corpus.
The corpus covers crypto regulation, AI policy, digital assets, and legal profession transformation.

For each tweet provided, return a JSON object with these fields:
- "id": the tweet id (copy from input)
- "topics": array of 1-3 top-level topics from this list: {json.dumps(TOPICS)}
- "subtopics": array of 1-4 specific subtopics (free-form, e.g. "stablecoin yield", "SEC enforcement", "agentic AI")
- "content_type": one of: primary_source | expert_opinion | debate | data_claim | announcement | thread | other
  - primary_source: links to or quotes official documents (SEC/CFTC releases, legislation text, court orders)
  - expert_opinion: practitioner or expert making a substantive argument
  - debate: response to another tweet, disagreement, or quote-tweet adding counterpoint
  - data_claim: empirical assertion with numbers or research
  - announcement: product launch, regulatory event, funding news
  - thread: numbered or multi-part explanation
  - other: everything else
- "authority": one of: official | expert | community
  - official: government, regulatory body, official institutional account
  - expert: known practitioner, academic, policy professional in the relevant domain
  - community: general participant
- "summary": 1-2 sentences capturing what is being said (not just what it's about)
- "core_claim": the specific argument, thesis, or claim being made, if any. Null if purely informational.
- "position": one of: pro | con | neutral | mixed | null
  - relative to any regulation, policy, or technology being discussed
  - null if no clear position is taken
- "entities": array of objects with "name" and "type" fields
  - types: person | bill | agency | company | protocol | concept

Return a JSON array — one object per tweet — in the same order as the input.
Keep summaries concise. For very short or content-free tweets (e.g. just a URL, a GIF), set summary to null and core_claim to null.
Do not include any text outside the JSON array."""


def load_env():
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ[key.strip()] = value.strip()


def get_unenriched(conn, limit=None, ids=None, worker=None, num_workers=None):
    if ids:
        placeholders = ",".join("?" * len(ids))
        return conn.execute(
            f"SELECT id, author_handle, text, quoted_tweet_text FROM bookmarks "
            f"WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    conditions = ["enriched_at IS NULL"]
    if worker is not None and num_workers:
        conditions.append(f"(rowid % {num_workers}) = {worker}")
    where = " AND ".join(conditions)
    q = (f"SELECT id, author_handle, text, quoted_tweet_text FROM bookmarks "
         f"WHERE {where} ORDER BY created_at DESC")
    if limit:
        q += f" LIMIT {limit}"
    return conn.execute(q).fetchall()


def build_user_message(batch):
    items = []
    for row in batch:
        item = {
            "id": row["id"],
            "author": row["author_handle"],
            "text": (row["text"] or "")[:500],
        }
        if row["quoted_tweet_text"]:
            item["quoted_text"] = row["quoted_tweet_text"][:200]
        items.append(item)
    return json.dumps(items, ensure_ascii=False)


def call_claude(client, batch):
    """Send a batch to Claude and return parsed enrichment list."""
    message = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_message(batch)}],
    )
    raw = message.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]
    return json.loads(raw)


def enrich_new(new_ids, verbose=True):
    """Enrich a specific list of tweet IDs. Called from sync_bookmarks.py."""
    if not new_ids:
        return
    load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[enrich] ANTHROPIC_API_KEY not set, skipping enrichment.", file=sys.stderr)
        return
    try:
        import anthropic
        import sqlite3
        from db import get_conn, upsert_enrichment
    except ImportError as e:
        print(f"[enrich] Missing dependency: {e}", file=sys.stderr)
        return

    client = anthropic.Anthropic(api_key=api_key)
    conn = get_conn()
    rows = get_unenriched(conn, ids=new_ids)
    _run_enrichment(client, conn, rows, verbose=verbose)
    conn.close()


def _run_enrichment(client, conn, rows, verbose=True, label=""):
    from db import upsert_enrichment

    total = len(rows)
    enriched = 0
    errors = 0

    for i in range(0, total, BATCH_SIZE):
        batch = rows[i: i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

        if verbose:
            print(f"{label}Batch {batch_num}/{total_batches} ({len(batch)} tweets)...", end=" ", flush=True)

        try:
            results = call_claude(client, batch)
        except Exception as e:
            err_str = str(e)
            # Retry on rate limit with exponential backoff
            if "429" in err_str or "rate_limit" in err_str:
                for wait in [30, 60, 120]:
                    print(f"rate limit, retrying in {wait}s...", end=" ", flush=True)
                    time.sleep(wait)
                    try:
                        results = call_claude(client, batch)
                        break
                    except Exception as e2:
                        err_str = str(e2)
                        if "429" not in err_str and "rate_limit" not in err_str:
                            break
                else:
                    print(f"ERROR (gave up): {e}")
                    errors += len(batch)
                    continue
            else:
                print(f"ERROR: {e}")
                errors += len(batch)
                time.sleep(5)
                continue

        now = datetime.now(timezone.utc).isoformat()
        batch_enriched = 0
        for result in results:
            tweet_id = result.get("id")
            if not tweet_id:
                continue
            result["enriched_at"] = now
            try:
                upsert_enrichment(conn, tweet_id, result)
                batch_enriched += 1
            except Exception as e:
                print(f"\n[warn] Failed to write enrichment for {tweet_id}: {e}", file=sys.stderr)

        conn.commit()
        enriched += batch_enriched

        if verbose:
            print(f"ok ({batch_enriched}/{len(batch)} written, {enriched}/{total} total)")

        # Polite rate-limit pause between batches (8k tokens/min limit → ~10s safe gap)
        if i + BATCH_SIZE < total:
            time.sleep(10)

    if verbose:
        print(f"\n{label}Done. Enriched {enriched}/{total} tweets. Errors: {errors}.")
    return enriched


if __name__ == "__main__":
    load_env()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set in .env or environment.", file=sys.stderr)
        sys.exit(1)

    try:
        import anthropic
    except ImportError:
        print("Error: anthropic package not installed. Run: pip install anthropic", file=sys.stderr)
        sys.exit(1)

    from db import get_conn, init_db, stats

    # Parse args
    ids = None
    limit = None
    worker = None
    num_workers = None
    args = sys.argv[1:]
    if "--ids" in args:
        idx = args.index("--ids")
        ids = args[idx + 1:]
    if "--limit" in args:
        idx = args.index("--limit")
        limit = int(args[idx + 1])
    if "--worker" in args:
        idx = args.index("--worker")
        worker = int(args[idx + 1])
    if "--num-workers" in args:
        idx = args.index("--num-workers")
        num_workers = int(args[idx + 1])

    worker_label = f"[worker {worker}/{num_workers}] " if worker is not None else ""

    # Ensure DB exists
    conn = get_conn()
    try:
        conn.execute("SELECT 1 FROM bookmarks LIMIT 1")
    except Exception:
        print("DB not initialised. Run: python3 db.py")
        sys.exit(1)

    rows = get_unenriched(conn, limit=limit, ids=ids, worker=worker, num_workers=num_workers)

    if not rows:
        s = stats()
        print(f"{worker_label}Nothing to enrich. ({s['enriched']}/{s['total']} already enriched)")
        conn.close()
        sys.exit(0)

    print(f"{worker_label}Enriching {len(rows)} tweets with {MODEL}...")
    client = anthropic.Anthropic(api_key=api_key)
    _run_enrichment(client, conn, rows, verbose=True, label=worker_label)
    conn.close()

    s = stats()
    print(f"{worker_label}Done. DB status: {s['enriched']}/{s['total']} enriched.")
