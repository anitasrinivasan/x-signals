# x-signals

A personal knowledge graph and writing assistant built on 5,000+ Twitter/X bookmarks. Designed for a legal/policy analyst who publishes at law review companions, Tech Policy Press, Lawfare, and Substack.

The core idea: rather than treating bookmarks as a flat archive to search, x-signals enriches them with structured metadata (topics, subtopics, positions, entities, claims), clusters them into discourse narratives, and lets Claude query the resulting knowledge graph to surface what's worth writing and why now.

---

## What it does

**Nightly sync** — pulls new Twitter bookmarks via cookie auth, deduplicates, enriches with Claude Sonnet (topics, subtopics, content type, authority, summary, core claim, position, named entities), clusters into narratives, and sends a Telegram summary.

**Narrative clustering** — two-pass Union-Find on IDF-weighted subtopic co-occurrence groups related tweets into coherent discourse threads without ML or embeddings. Each narrative gets a momentum score (recency × engagement × authority) and a delta (heating up vs cooling down). Claude Sonnet labels each cluster with a title, description, and key claim.

**Streamlit app** with three tabs:
- **Writing Assistant** — Claude with live DB access. "What to Write Next" queries narrative clusters first (compact, rate-limit-safe), then drills into raw tweets for citations. Also supports Topic Briefing and Argument Builder modes.
- **Narrative Graph** — force-directed graph of narrative clusters, colored by topic, sized by tweet count, with a momentum leaderboard.
- **Browse** — filter, chart, and paginate the full bookmark corpus.

---

## Setup

### Prerequisites
- Python 3.11+
- [twitter-cli](https://pypi.org/project/twitter-cli/) — requires active X/Twitter session cookies
- Anthropic API key
- Telegram bot token (optional, for nightly notifications)

### Install

```bash
git clone https://github.com/anitasrinivasan/x-signals.git
cd x-signals
pip install -r requirements.txt
```

### Configure `.env`

```
TWITTER_AUTH_TOKEN=your_auth_token
TWITTER_CT0=your_ct0_cookie
ANTHROPIC_API_KEY=your_key
TELEGRAM_BOT_TOKEN=your_bot_token   # optional
TELEGRAM_CHAT_ID=your_chat_id       # optional
```

Get `auth_token` and `ct0` from your browser's cookies while logged into X.com.

### First run

```bash
# 1. Initialise DB and import bookmarks from JSON
python3 db.py

# 2. Fetch bookmarks (requires twitter-cli config — see below)
python3 sync_bookmarks.py --full

# 3. Enrich all bookmarks with Claude Sonnet
python3 enrich.py

# 4. Cluster into narratives
python3 cluster.py

# 5. Launch the app
streamlit run app.py
```

### twitter-cli config

Create `~/.twitter-cli/config.yaml`:
```yaml
maxCount: 5000
```

And patch the absolute cap (twitter-cli defaults to 500 max):
```python
# In your venv: find the installed package and set _ABSOLUTE_MAX_COUNT = 5000
```

---

## Daily sync (launchd)

A launchd agent runs the sync at 23:00 nightly. Install:

```bash
# Copy the plist to LaunchAgents (update paths as needed)
cp com.anitasrinivasan.x-signals.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.anitasrinivasan.x-signals.plist
```

The sync pipeline: fetch bookmarks → deduplicate → import to SQLite → enrich new tweets → cluster new tweets into narratives → Telegram notification.

---

## Architecture

### Data pipeline

```
Twitter API (cookie auth)
    ↓  twitter-cli
twitter_bookmarks.json  (master bookmark store, gitignored)
    ↓  db.py
signals.db  (SQLite, gitignored)
    ↓  enrich.py
bookmarks table  (enriched with Claude Sonnet)
    ↓  cluster.py
narratives / tweet_narratives / narrative_edges tables
    ↓  app.py
Streamlit app
```

### SQLite schema

**`bookmarks`** — tweet content + enrichment fields (topics, subtopics, content_type, authority, summary, core_claim, position, entities)

**`bookmarks_fts`** — FTS5 virtual table for full-text search

**`narratives`** — clustered discourse threads with momentum scoring and Claude-generated labels

**`tweet_narratives`** — join table with roles (initiating / supporting / dissenting / contextual)

**`narrative_edges`** — connections between narratives (subtopic overlap, entity anchor)

### Clustering algorithm

1. **Pass 1** — IDF-weighted pair scoring: tweets sharing subtopics with combined IDF ≥ 10.0 are unioned. Rare subtopics (specific legislation, named protocols) score high; generic ones ("ai agents", "regulation") score low.
2. **Pass 2** — rare single-subtopic clustering: tweets sharing one very specific subtopic (IDF ≥ 6.5) are grouped, catching discourse threads that didn't clear the combined threshold.
3. **Entity anchor merge** — clusters sharing 2+ named entities of type agency/bill/company/protocol are merged.
4. **Attachment pass** — unclustered tweets attach to the nearest cluster if IDF overlap ≥ 4.0.
5. **Momentum scoring** — `0.55 × recency + 0.30 × engagement + 0.15 × authority`, with delta vs prior 30-day window.
6. **Label generation** — Claude Sonnet generates title, description, key claim, and dominant position per cluster (batches of 10).

### Files

| File | Purpose |
|------|---------|
| `sync_bookmarks.py` | Nightly sync: fetch → dedupe → import → enrich → cluster → notify |
| `db.py` | SQLite schema, import helpers, narrative schema |
| `enrich.py` | Claude Sonnet enrichment pipeline (resumable, parallel workers) |
| `cluster.py` | Narrative clustering (full re-cluster + incremental `cluster_new()`) |
| `cluster_probe.py` | Diagnostic/validation script — prints clusters without writing to DB |
| `app.py` | Streamlit app (Writing Assistant, Narrative Graph, Browse) |
| `requirements.txt` | Python dependencies |

---

## Usage notes

- `signals.db` and `twitter_bookmarks.json` are gitignored — they stay local.
- Re-running `cluster.py` does a full re-cluster (clears and rewrites narrative tables). Safe to run anytime.
- `cluster.py --no-labels` skips Claude label generation (fast, for testing).
- `cluster.py --since 60d` clusters only the last 60 days.
- Enrichment is resumable — `enrich.py` skips already-enriched rows.
- The app works without narratives (falls back to `query_db` only) if `cluster.py` hasn't been run.

---

## Rate limits

Enrichment uses Claude Sonnet at batch size 25 with 10s inter-batch delay and exponential backoff on 429s. At typical org limits (~8k output tokens/min), enrichment of 5,000 tweets takes ~2-3 hours. The `--worker N --num-workers M` flags in `enrich.py` support parallel workers once the initial corpus is enriched.

Narrative label generation uses Claude Sonnet in batches of 10 clusters with 3s inter-batch delay. 232 clusters ≈ 5-10 minutes.
