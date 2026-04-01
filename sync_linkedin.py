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
MASTER_JSON = SCRIPT_DIR / "linkedin_bookmarks.json"
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


def load_master():
    """Load linkedin_bookmarks.json, return (list_of_dicts, set_of_ids)."""
    if not MASTER_JSON.exists():
        return [], set()
    with open(MASTER_JSON) as f:
        data = json.load(f)
    return data, {p["id"] for p in data}


def save_master(posts_list):
    sorted_posts = sorted(
        posts_list,
        key=lambda p: p.get("createdAtISO", p.get("createdAt", "")),
        reverse=True,
    )
    with open(MASTER_JSON, "w") as f:
        json.dump(sorted_posts, f, indent=2)


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

async def extract_post(container, urn: str) -> dict | None:
    """Extract structured data from a post container element."""
    try:
        # Text content — try selectors in priority order
        text = ""
        for sel in [
            ".feed-shared-text",
            ".update-components-text",
            ".feed-shared-update-v2__description",
            ".feed-shared-article__description",
            ".update-components-entity__title-text",
        ]:
            el = await container.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    break

        # Author name
        author_name = ""
        for sel in [
            ".update-components-actor__name span[aria-hidden='true']",
            ".update-components-actor__name",
            ".feed-shared-actor__name",
        ]:
            el = await container.query_selector(sel)
            if el:
                author_name = (await el.inner_text()).strip()
                if author_name:
                    break

        # Author profile URL
        author_url = ""
        for sel in [
            ".update-components-actor__meta-link",
            ".feed-shared-actor__container-link",
            "a.app-aware-link[href*='/in/']",
            "a.app-aware-link[href*='/company/']",
        ]:
            el = await container.query_selector(sel)
            if el:
                href = await el.get_attribute("href") or ""
                if "/in/" in href or "/company/" in href:
                    author_url = href.split("?")[0]
                    break

        # Reactions count
        likes = 0
        for sel in [
            ".social-details-social-counts__reactions-count",
            "button[aria-label*='reaction'] span",
            ".social-counts-reactions span",
        ]:
            el = await container.query_selector(sel)
            if el:
                raw = (await el.inner_text()).strip().replace(",", "")
                try:
                    likes = int(raw)
                except ValueError:
                    pass
                break

        # Comments count
        comments = 0
        for sel in [
            ".social-details-social-counts__comments-count",
            "button[aria-label*='comment'] span",
            ".social-details-social-counts__comments a",
        ]:
            el = await container.query_selector(sel)
            if el:
                raw = (await el.inner_text()).strip().replace(",", "").split()[0]
                try:
                    comments = int(raw)
                except ValueError:
                    pass
                break

        # Timestamp — LinkedIn shows relative times; prefer datetime attribute
        created_at_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        for sel in [
            ".update-components-actor__sub-description time",
            ".feed-shared-actor__sub-description time",
            "time[datetime]",
        ]:
            el = await container.query_selector(sel)
            if el:
                dt_attr = await el.get_attribute("datetime")
                if dt_attr:
                    created_at_iso = dt_attr
                break

        # Skip containers with no useful content (profile cards, ads)
        if not text and not author_name:
            return None

        post_url = f"https://www.linkedin.com/feed/update/{urn}/"
        handle = author_url.rstrip("/").split("/")[-1] if author_url else ""

        return {
            "id": urn,
            "text": text,
            "author": {
                "name": author_name,
                "handle": handle,
                "profileUrl": author_url,
            },
            "metrics": {
                "likes": likes,
                "comments": comments,
            },
            "createdAt": created_at_iso[:10],
            "createdAtISO": created_at_iso,
            "postUrl": post_url,
            "source": "linkedin",
        }

    except Exception as e:
        print(f"[warn] Failed to extract {urn}: {e}", file=sys.stderr)
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

        # Inject session cookie — equivalent to being logged into linkedin.com
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

        # Wait for feed content — bail if page didn't authenticate
        try:
            await page.wait_for_selector(
                "[data-urn], .scaffold-finite-scroll__content, .feed-shared-update-v2",
                timeout=20_000,
            )
        except Exception:
            title = await page.title()
            print(
                f"[error] Saved posts page didn't load (page title: '{title}'). "
                "Check your LINKEDIN_LI_AT cookie — it may have expired.",
                file=sys.stderr,
            )
            await browser.close()
            return []

        stale_scrolls = 0
        scroll_round = 0

        while not stop_early and stale_scrolls < 3 and len(posts) < max_posts:
            scroll_round += 1

            # Extract all visible post containers that have a URN
            containers = await page.query_selector_all("[data-urn]")
            prev_count = len(posts)

            for container in containers:
                urn = await container.get_attribute("data-urn") or ""

                # Only process posts (activity/share URNs), skip people/companies
                if not (urn.startswith("urn:li:activity:") or urn.startswith("urn:li:share:")):
                    continue
                if urn in seen_urns:
                    continue
                seen_urns.add(urn)

                # Incremental stop: hit a post we already have → done
                if stop_ids and urn in stop_ids:
                    print(f"  Hit known post {urn} — stopping scroll.")
                    stop_early = True
                    break

                post = await extract_post(container, urn)
                if post:
                    posts.append(post)

            # Scroll down and wait for new content
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

    existing_list, existing_ids = load_master()
    print(f"Loaded {len(existing_list)} existing LinkedIn saved posts.")

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

    merged = existing_list + new_posts
    save_master(merged)
    print(f"Saved {len(new_posts)} new posts → {MASTER_JSON}")

    # Import → enrich → cluster (same pipeline as Twitter)
    cluster_stats = {}
    try:
        from db import get_conn, import_linkedin
        db_conn = get_conn()
        import_linkedin(db_conn, new_posts, verbose=True)
        db_conn.close()

        from enrich import enrich_new
        enrich_new(new_ids=[p["id"] for p in new_posts], verbose=True)

        from cluster import cluster_new
        cluster_stats = cluster_new(new_ids=[p["id"] for p in new_posts]) or {}

    except Exception as e:
        print(f"[warn] DB/enrichment step failed: {e}", file=sys.stderr)

    # Telegram notification
    lines = [
        f"✅ x-signals LinkedIn: +{len(new_posts)} new saved posts "
        f"(total: {len(merged)}) — {today}"
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
