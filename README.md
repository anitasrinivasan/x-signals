# x-signals

A personal knowledge graph and writing assistant built on the user's own Twitter/X bookmarks. Designed for a legal/policy analyst who intends to convert these insights into publications at venues like law review companions, tech policy media, and even Substack.

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

### Quickstart

```bash
git clone https://github.com/anitasrinivasan/x-signals.git
cd x-signals
./setup.sh
```

`setup.sh` handles everything: virtualenv, dependencies, credential prompts, first-time pipeline, and scheduler installation. See the [Deployment](#deployment) section for details on Mac Mini, Docker, and VPS options.

### Manual install (if you prefer step-by-step)

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in your credentials

python3 sync_bookmarks.py --full   # fetch bookmarks
python3 enrich.py                  # enrich with Claude (~2-4h for large corpus)
python3 cluster.py                 # build narrative clusters
streamlit run app.py               # launch app
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

## Deployment

### Option A — Local machine (macOS / Linux)

```bash
git clone https://github.com/anitasrinivasan/x-signals.git
cd x-signals
./setup.sh
```

`setup.sh` creates a virtualenv, installs deps, walks through credential setup, runs the first-time pipeline, and installs a scheduler:

| OS | Scheduler installed |
|----|---------------------|
| macOS | Two launchd agents: nightly sync at 23:00 + persistent Streamlit (auto-restarts on crash/reboot) |
| Linux | crontab entry for nightly sync; start Streamlit manually or via systemd/screen |

Open **http://localhost:8501** in your browser.

**Accessing from other devices on the same network**

Find the machine's local IP (`ip route get 1 | awk '{print $7}'` on Linux; System Settings → Network on Mac) and open `http://<ip>:8501` from any device on the same Wi-Fi.

**Remote access from anywhere:** [Tailscale](https://tailscale.com) (free personal plan) creates a private mesh — install on both machines, then use the Tailscale IP instead of the LAN IP. No port-forwarding or firewall changes needed.

---

### Option B — Docker (Mac, Linux, VPS)

```bash
git clone https://github.com/anitasrinivasan/x-signals.git
cd x-signals
cp .env.example .env   # fill in your credentials
docker compose up -d
```

The container runs the Streamlit UI **and** a nightly cron job for sync in one process. Data persists in `./data/` and `./pitches/` via volume mounts; the `.env` file is passed in automatically.

To run the first-time pipeline inside the container:

```bash
docker compose exec x-signals python sync_bookmarks.py --full
docker compose exec x-signals python enrich.py
docker compose exec x-signals python cluster.py
```

Verify the cron job loaded: `docker compose exec x-signals crontab -l`

**VPS (recommended for reliable nightly sync):** any small instance works — Hetzner CX11 (~€4/mo), DigitalOcean Basic (~$6/mo), Oracle Cloud Free Tier. Clone the repo, fill in `.env`, run `docker compose up -d`. Reverse-proxy with Caddy or nginx if you want a domain + HTTPS; otherwise access via IP:8501.

---

### Getting your Twitter/X cookies

The sync uses your X.com session cookies directly (no API key required):

1. Log into [x.com](https://x.com) in Chrome or Firefox
2. Open DevTools (`Cmd+Option+I` / `F12`) → **Application** tab → **Cookies** → `https://x.com`
3. Copy the value of **`auth_token`** → paste as `TWITTER_AUTH_TOKEN` in `.env`
4. Copy the value of **`ct0`** → paste as `TWITTER_CT0` in `.env`

> **Note:** cookies expire when you log out of X. If sync starts failing with 401 errors, re-copy the cookie values.

---

### API cost estimates

All AI work uses your own Anthropic API key.

| Phase | When | Typical cost |
|-------|------|--------------|
| Initial enrichment (~1,000 tweets) | One-time, first run | ~$3–8 |
| Daily sync (10–20 new tweets) | Every night | ~$0.05–0.15 |
| Pitch generation | Per session (on demand) | ~$0.10–0.30 |

Costs scale with bookmark volume. The enrichment phase is the expensive part; after that, daily costs are minimal.

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
