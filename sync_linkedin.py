#!/usr/bin/env python3
"""
LinkedIn saved posts sync for x-signals.

Uses Playwright (headless Chromium) to navigate linkedin.com/my-items/saved-posts/
with your li_at session cookie, scrolls through all saved posts, and imports new
ones into the same SQLite database as Twitter bookmarks (source='linkedin').

The enrichment and clustering pipelines are identical — LinkedIn posts and tweets
flow into the same knowledge graph.

Usage:
    python3 sync_linkedin.py           # daily sync (stops when it hits known posts)
    python3 sync_linkedin.py --full    # get all saved posts (first run / catch-up)

Credential:
    LINKEDIN_LI_AT in .env — the li_at cookie from linkedin.com
    (DevTools → Application → Cookies → www.linkedin.com → li_at)
"""

import asyncio
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SAVED_POSTS_URL = "https://www.linkedin.com/my-items/saved-posts/"

# How many posts to fetch on each run
DAILY_MAX = 100    # stop early once we hit known posts anyway
FULL_MAX  = 1000   # LinkedIn caps saved posts around 1000


# ---------------------------------------------------------------------------
# Env / persistence helpers
# ---------------------------------------------------------------------------

def load_env():
    env_path = SCRIPT_DIR / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ[key.strip()] = value.strip()


def send_telegram(bot_token, chat_id, message):
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
        print(f"[warn] Telegram failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Playwright scraper
# ---------------------------------------------------------------------------

def rel_to_iso(rel: str) -> str:
    """Convert LinkedIn relative timestamp ('4h', '1d', '2w', '1mo', '1yr') to ISO 8601."""
    import re
    now = datetime.now(timezone.utc)
    rel = rel.strip().lower()
    m = re.match(r"(\d+)\s*(s|m|h|d|w|mo|yr)", rel)
    if not m:
        return now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    n, unit = int(m.group(1)), m.group(2)
    from datetime import timedelta
    deltas = {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
              "h": timedelta(hours=n),   "d": timedelta(days=n),
              "w": timedelta(weeks=n),   "mo": timedelta(days=n * 30),
              "yr": timedelta(days=n * 365)}
    return (now - deltas.get(unit, timedelta(0))).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse_post_innertext(raw: str) -> tuple[str, str, str]:
    """
    Parse LinkedIn saved-posts inner_text into (author_name, post_text, timestamp_rel).

    The page renders each saved post as:
        {Author Name}
        View {Author Name}'s profile
        • {Degree}
        {Author Job Title}
        {relative_time} •
        {relative_time} Visible to everyone
        {POST TEXT...}
    """
    lines = [l.strip() for l in raw.split("\n") if l.strip()]

    author_name = lines[0] if lines else ""

    post_text = ""
    timestamp_rel = ""
    for i, line in enumerate(lines):
        if "Visible to everyone" in line:
            # Timestamp is embedded: "4h Visible to everyone"
            ts = line.replace("Visible to everyone", "").strip().rstrip("•").strip()
            if ts:
                timestamp_rel = ts
            post_text = "\n".join(lines[i + 1:]).strip()
            break

    return author_name, post_text, timestamp_rel


async def extract_post(container) -> dict | None:
    """Extract a post dict from a [data-chameleon-result-urn] container element."""
    try:
        urn = await container.get_attribute("data-chameleon-result-urn") or ""
        if not urn:
            return None

        # All visible text for parsing
        raw_text = (await container.inner_text()).strip()
        author_name, post_text, ts_rel = parse_post_innertext(raw_text)

        # Author profile URL (strip LinkedIn tracking params)
        author_url = ""
        el = await container.query_selector("a[href*='/in/'], a[href*='/company/']")
        if el:
            href = await el.get_attribute("href") or ""
            author_url = href.split("?")[0]

        if not author_name and not post_text:
            return None

        created_at_iso = rel_to_iso(ts_rel) if ts_rel else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        handle = author_url.rstrip("/").split("/")[-1] if author_url else ""
        post_url = f"https://www.linkedin.com/feed/update/{urn}/"

        return {
            "id": urn,
            "text": post_text,
            "author": {"name": author_name, "handle": handle, "profileUrl": author_url},
            "metrics": {"likes": 0, "comments": 0},
            "createdAt": created_at_iso[:10],
            "createdAtISO": created_at_iso,
            "postUrl": post_url,
            "source": "linkedin",
        }

    except Exception as e:
        print(f"[warn] extract failed: {e}", file=sys.stderr)
        return None


async def scrape_saved_posts(li_at: str, stop_ids: set = None, max_posts: int = FULL_MAX) -> list:
    """
    Headless Playwright session: inject li_at cookie → navigate → scroll-and-extract.

    stop_ids: set of already-known post IDs. When we see one, stop scrolling
              (used for incremental daily sync). Pass None for --full.
    """
    from playwright.async_api import async_playwright

    posts = []
    seen_urns = set()
    stop_early = False

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )

        await ctx.add_cookies([{
            "name": "li_at",
            "value": li_at,
            "domain": ".linkedin.com",
            "path": "/",
            "httpOnly": True,
            "secure": True,
        }])

        page = await ctx.new_page()
        print("→ Opening LinkedIn saved posts (headless)...")
        await page.goto(SAVED_POSTS_URL, wait_until="domcontentloaded", timeout=30_000)

        # Wait for post cards — bail if not authenticated
        try:
            await page.wait_for_selector("[data-chameleon-result-urn]", timeout=20_000)
        except Exception:
            title = await page.title()
            url = page.url
            if "login" in url or "authwall" in url or "checkpoint" in url:
                print("[error] Redirected to login — LINKEDIN_LI_AT cookie is expired.", file=sys.stderr)
            else:
                print(f"[error] No post containers found (title: '{title}'). Check cookie.", file=sys.stderr)
            await browser.close()
            return []

        stale_scrolls = 0
        scroll_round = 0

        while not stop_early and stale_scrolls < 3 and len(posts) < max_posts:
            scroll_round += 1
            prev_count = len(posts)

            containers = await page.query_selector_all("[data-chameleon-result-urn]")

            for container in containers:
                urn = await container.get_attribute("data-chameleon-result-urn") or ""
                if not urn or urn in seen_urns:
                    continue
                seen_urns.add(urn)

                if stop_ids and urn in stop_ids:
                    print(f"  Hit known post — stopping scroll.")
                    stop_early = True
                    break

                post = await extract_post(container)
                if post:
                    posts.append(post)

            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2500)

            if len(posts) == prev_count:
                stale_scrolls += 1
                print(f"  Scroll {scroll_round}: no new posts ({stale_scrolls}/3 stale)")
            else:
                stale_scrolls = 0
                print(f"  Scroll {scroll_round}: {len(posts)} posts so far")

        await browser.close()

    print(f"→ Extracted {len(posts)} saved posts.")
    return posts


# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------

def sync(full: bool = False):
    load_env()

    li_at = os.environ.get("LINKEDIN_LI_AT", "").strip()
    if not li_at:
        print("LINKEDIN_LI_AT not set in .env — skipping LinkedIn sync.")
        print("To enable: add your li_at cookie to .env (DevTools → Application → Cookies → linkedin.com)")
        return

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat  = os.environ.get("TELEGRAM_CHAT_ID", "")
    today = date.today().isoformat()

    from db import get_conn, import_linkedin
    db_conn = get_conn()
    existing_ids = {row[0] for row in db_conn.execute(
        "SELECT id FROM bookmarks WHERE source='linkedin'"
    ).fetchall()}
    print(f"Loaded {len(existing_ids)} existing LinkedIn saved posts from DB.")

    max_posts = FULL_MAX if full else DAILY_MAX
    stop_ids  = None if full else existing_ids   # don't stop early on --full

    try:
        scraped = asyncio.run(
            scrape_saved_posts(li_at, stop_ids=stop_ids, max_posts=max_posts)
        )
    except Exception as e:
        msg = f"⚠️ x-signals LinkedIn: scrape failed — {e} ({today})"
        print(msg, file=sys.stderr)
        send_telegram(telegram_token, telegram_chat, msg)
        sys.exit(1)

    new_posts = [p for p in scraped if p["id"] not in existing_ids]

    if not new_posts:
        msg = f"☑️ x-signals LinkedIn: no new saved posts — {today}"
        print(msg)
        send_telegram(telegram_token, telegram_chat, msg)
        return

    import_linkedin(db_conn, new_posts, verbose=True)
    db_conn.close()

    # Enrich → cluster (same pipeline as Twitter)
    cluster_stats = {}
    try:
        from enrich import enrich_new
        enrich_new(new_ids=[p["id"] for p in new_posts], verbose=True)

        from cluster import cluster_new
        cluster_stats = cluster_new(new_ids=[p["id"] for p in new_posts]) or {}

    except Exception as e:
        print(f"[warn] DB/enrichment step failed: {e}", file=sys.stderr)

    # Telegram notification
    lines = [
        f"✅ x-signals LinkedIn: +{len(new_posts)} new saved posts "
        f"(total: {len(existing_ids) + len(new_posts)}) — {today}"
    ]
    if cluster_stats:
        assigned   = cluster_stats.get("assigned", 0)
        unassigned = cluster_stats.get("unassigned", 0)
        lines.append(
            f"   📚 Clustered: {assigned} into narratives"
            + (f" ({unassigned} unclustered)" if unassigned else "")
        )
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
    sync(full=full)
