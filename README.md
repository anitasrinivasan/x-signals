# x-signals

**Self-hosted, open source.** Bring your own Anthropic API key and Twitter/LinkedIn session cookies. No subscription, no cloud service, no data leaves your machine.

A personal knowledge graph and writing assistant built on your Twitter/X bookmarks and LinkedIn saved posts. Designed for analysts and writers who want to convert their saved content into publications — law review pieces, policy commentary, Substack posts.

The core idea: rather than treating bookmarks as a flat archive to search, x-signals enriches them with structured metadata (topics, subtopics, positions, entities, claims), clusters them into discourse narratives, and lets Claude query the resulting knowledge graph to surface what's worth writing and why now.

---

## What it does

**Nightly sync** — pulls new Twitter bookmarks and LinkedIn saved posts via cookie auth, deduplicates, enriches with Claude Sonnet (topics, subtopics, content type, authority, summary, core claim, position, named entities), clusters into narratives, and sends a Telegram summary.

**Narrative clustering** — two-pass Union-Find on IDF-weighted subtopic co-occurrence groups related posts into coherent discourse threads without ML or embeddings. Each narrative gets a momentum score (recency × engagement × authority) and a delta (heating up vs cooling down). Claude Sonnet labels each cluster with a title, description, and key claim. A full re-cluster runs monthly to surface new topic spaces.

**Streamlit app** with three tabs:
- **Writing Assistant** — Claude with live DB access. "What to Write Next" queries narrative clusters first (compact, rate-limit-safe), then drills into raw posts for citations. Also supports Topic Briefing and Argument Builder modes.
- **Narrative Graph** — force-directed graph of narrative clusters, colored by topic, sized by post count, with a momentum leaderboard.
- **Browse** — filter by source (Twitter / LinkedIn), topic, date range, and full-text search across the full corpus.

---

## Setup

### Prerequisites
- Python 3.11+
- [twitter-cli](https://pypi.org/project/twitter-cli/) — requires active X/Twitter session cookies
- Playwright + headless Chromium (~300MB, installed automatically by `setup.sh`) — for LinkedIn sync
- Anthropic API key
- Telegram bot token (optional, for nightly notifications)

### Quickstart

```bash
git clone https://github.com/anitasrinivasan/x-signals.git
cd x-signals
./setup.sh
```

`setup.sh` handles everything: virtualenv, dependencies, credential prompts, first-time pipeline, and scheduler installation.

### Manual install

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # fill in your credentials

python3 sync_bookmarks.py --full   # fetch Twitter bookmarks
python3 sync_linkedin.py --full    # fetch LinkedIn saved posts (if configured)
python3 enrich.py                  # enrich with Claude (~2-4h for large corpus)
python3 cluster.py                 # build narrative clusters
streamlit run app.py               # launch app
```

### twitter-cli config

Create `~/.twitter-cli/config.yaml`:
```yaml
maxCount: 5000
```

> The 500-bookmark cap in twitter-cli is patched automatically at runtime — no manual code changes needed.

### Linux system dependencies

On Ubuntu/Debian, install these before running `setup.sh`:
```bash
sudo apt-get install libcurl4-openssl-dev
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
| macOS | Four launchd agents (see below) |
| Linux | Three crontab entries + manual Streamlit start |

**macOS launchd schedule:**

| Job | When | Label |
|-----|------|-------|
| Twitter sync | 23:00 daily | `com.x-signals.sync` |
| LinkedIn sync | 23:15 daily | `com.x-signals.linkedin` |
| Full re-cluster | 23:30 on 1st of month | `com.x-signals.recluster` |
| Streamlit app | Always-on, restarts on crash/reboot | `com.x-signals.app` |

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

The container runs the Streamlit UI and nightly cron jobs in one process. Data persists in `./data/` and `./pitches/` via volume mounts.

To run the first-time pipeline inside the container:

```bash
docker compose exec x-signals python sync_bookmarks.py --full
docker compose exec x-signals python sync_linkedin.py --full
docker compose exec x-signals python enrich.py
docker compose exec x-signals python cluster.py
```

**VPS (recommended for reliable nightly sync):** any small instance works — Hetzner CX11 (~€4/mo), DigitalOcean Basic (~$6/mo), Oracle Cloud Free Tier. Clone the repo, fill in `.env`, run `docker compose up -d`. Reverse-proxy with Caddy or nginx if you want a domain + HTTPS; otherwise access via IP:8501.

---

### Getting your credentials

**Twitter/X cookies**

1. Log into [x.com](https://x.com) in Chrome or Firefox
2. Open DevTools (`Cmd+Option+I` / `F12`) → **Application** tab → **Cookies** → `https://x.com`
3. Copy **`auth_token`** → paste as `TWITTER_AUTH_TOKEN` in `.env`
4. Copy **`ct0`** → paste as `TWITTER_CT0` in `.env`

> Cookies expire when you log out of X. If sync fails with 401 errors, re-copy the values.

**LinkedIn cookie**

1. Log into [linkedin.com](https://linkedin.com) in Chrome or Firefox
2. Open DevTools → **Application** tab → **Cookies** → `https://www.linkedin.com`
3. Copy **`li_at`** → paste as `LINKEDIN_LI_AT` in `.env`

LinkedIn sync is optional — if `LINKEDIN_LI_AT` is not set, `sync_linkedin.py` exits silently.

> `li_at` rotates periodically. If LinkedIn sync stops returning posts, re-copy the value.

---

### API cost estimates

All AI work uses your own Anthropic API key.

| Phase | When | Typical cost |
|-------|------|--------------|
| Initial enrichment (~2,000 posts) | One-time, first run | ~$5–15 |
| Daily sync (10–30 new posts) | Every night | ~$0.05–0.20 |
| Monthly re-cluster (label generation) | 1st of month | ~$0.50–1.00 |
| Writing Assistant session | On demand | ~$0.10–0.30 |

Costs scale with corpus size. Enrichment is the expensive one-time cost; daily and monthly costs are minimal.

---

## Architecture

### Data pipeline

```
Twitter (cookie auth)          LinkedIn (cookie auth)
    ↓  sync_bookmarks.py           ↓  sync_linkedin.py
twitter_bookmarks.json         (direct to DB)
    ↓  db.py                       ↓
                signals.db  (SQLite, gitignored)
                    ↓  enrich.py
            bookmarks table  (source='twitter'|'linkedin')
                enriched with Claude Sonnet
                    ↓  cluster.py
        narratives / tweet_narratives / narrative_edges
                    ↓  app.py
                Streamlit app
```

### SQLite schema

**`bookmarks`** — post content + enrichment fields (topics, subtopics, content_type, authority, summary, core_claim, position, entities, source)

**`bookmarks_fts`** — FTS5 virtual table for full-text search

**`narratives`** — clustered discourse threads with momentum scoring and Claude-generated labels

**`tweet_narratives`** — join table with roles (initiating / supporting / dissenting / contextual)

**`narrative_edges`** — connections between narratives (subtopic overlap, entity anchor)

### Clustering algorithm

1. **Pass 1** — IDF-weighted pair scoring: posts sharing subtopics with combined IDF ≥ 10.0 are unioned. Rare subtopics (specific legislation, named protocols) score high; generic ones score low.
2. **Pass 2** — rare single-subtopic clustering: posts sharing one very specific subtopic (IDF ≥ 6.5) are grouped, catching threads that didn't clear the combined threshold.
3. **Entity anchor merge** — clusters sharing 2+ named entities of type agency/bill/company/protocol are merged.
4. **Attachment pass** — unclustered posts attach to the nearest cluster if IDF overlap ≥ 4.0.
5. **Momentum scoring** — `0.55 × recency + 0.30 × engagement + 0.15 × authority`, with delta vs prior 30-day window.
6. **Label generation** — Claude Sonnet generates title, description, key claim, and dominant position per cluster (batches of 10).

**Incremental vs full re-cluster:** `cluster_new()` runs after each nightly sync — it attaches new posts to existing narratives (fast, no API calls). `cluster_all()` runs monthly — it rebuilds all narratives from scratch, surfacing new topic clusters that have accumulated over the month.

### Files

| File | Purpose |
|------|---------|
| `sync_bookmarks.py` | Twitter nightly sync: fetch → dedupe → import → enrich → cluster → notify |
| `sync_linkedin.py` | LinkedIn nightly sync: Playwright scrape → import → enrich → cluster → notify |
| `db.py` | SQLite schema, import helpers, narrative schema |
| `enrich.py` | Claude Sonnet enrichment pipeline (resumable, parallel workers) |
| `cluster.py` | Narrative clustering (full re-cluster + incremental `cluster_new()`) |
| `cluster_probe.py` | Diagnostic/validation script — prints clusters without writing to DB |
| `app.py` | Streamlit app (Writing Assistant, Narrative Graph, Browse) |
| `setup.sh` | One-command setup: venv, deps, credentials, first-time pipeline, scheduler |
| `requirements.txt` | Python dependencies |
| `Dockerfile` / `docker-compose.yml` | Container deployment |

---

## Usage notes

- `signals.db` and `twitter_bookmarks.json` are gitignored — they stay local.
- LinkedIn posts and Twitter bookmarks share the same `bookmarks` table (`source` column distinguishes them). The Writing Assistant and narrative clustering work across both automatically.
- Re-running `cluster.py` does a full re-cluster (clears and rewrites narrative tables). Safe to run anytime.
- `cluster.py --no-labels` skips Claude label generation (fast, for testing).
- `cluster.py --since 60d` clusters only the last 60 days.
- `sync_linkedin.py --full` fetches all saved posts (first run). Without `--full`, it stops when it hits already-imported posts.
- Enrichment is resumable — `enrich.py` skips already-enriched rows.
- The app works without narratives (falls back to `query_db` only) if `cluster.py` hasn't been run.

---

## Rate limits

Enrichment uses Claude Sonnet at batch size 25 with 10s inter-batch delay and exponential backoff on 429s. At typical org limits (~8k output tokens/min), enrichment of 5,000 posts takes ~2-3 hours. The `--worker N --num-workers M` flags in `enrich.py` support parallel workers once the initial corpus is enriched.

Narrative label generation uses Claude Sonnet in batches of 10 clusters with 3s inter-batch delay. ~200 clusters ≈ 5-10 minutes.
