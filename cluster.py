#!/usr/bin/env python3
"""
Narrative clustering for x-signals.

Clusters all enriched bookmarks into narratives using a two-pass Union-Find
algorithm on subtopic co-occurrence, then writes results to SQLite.

Usage:
    python3 cluster.py               # full re-cluster, with Claude labels
    python3 cluster.py --no-labels   # skip Claude labels (fast, for testing)
    python3 cluster.py --since 60d   # only cluster tweets from last N days
"""

import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic

import db

SCRIPT_DIR = Path(__file__).parent

# Load .env manually so we override any empty env vars already set
_env_path = SCRIPT_DIR / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ[_k.strip()] = _v.strip()

# ── Tuning knobs ─────────────────────────────────────────────────────────────
IDF_THRESHOLD       = 10.0   # combined IDF score to union two tweets (Pass 1)
HIGH_IDF_THRESHOLD  = 6.5    # single subtopic IDF for rare-term pass (Pass 2)
ENTITY_ANCHOR_TYPES = {"agency", "bill", "company", "protocol"}
ENTITY_ANCHOR_MIN   = 2      # min shared anchor entities to trigger cluster merge
ATTACH_THRESHOLD    = 4.0    # IDF overlap needed to attach outlier to a cluster
MIN_CLUSTER         = 3      # minimum tweets to keep a cluster

LABEL_MODEL         = "claude-sonnet-4-5"
LABEL_BATCH         = 10     # clusters per Claude call


# ── Union-Find ────────────────────────────────────────────────────────────────

class UnionFind:
    def __init__(self, keys):
        self.parent = {k: k for k in keys}
        self.rank   = {k: 0  for k in keys}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1

    def clusters(self):
        groups = defaultdict(list)
        for k in self.parent:
            groups[self.find(k)].append(k)
        return list(groups.values())


# ── Data loading ──────────────────────────────────────────────────────────────

def load_tweets(conn, since_days=None):
    """Load all enriched tweets from DB, optionally filtered to last N days."""
    rows = db.get_all_enriched(conn)
    if since_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        rows = [r for r in rows if _parse_dt(r["created_at"]) >= cutoff]

    tweets = []
    for r in rows:
        subtopics = json.loads(r["subtopics"] or "[]")
        entities_raw = json.loads(r["entities"] or "[]")
        entities = []
        for e in entities_raw:
            if isinstance(e, dict):
                entities.append((e.get("name", "").lower().strip(), e.get("type", "").lower()))
            elif isinstance(e, str):
                entities.append((e.lower().strip(), "unknown"))
        topics = json.loads(r["topics"] or "[]")
        tweets.append({
            "id":        r["id"],
            "author":    r["author_handle"],
            "summary":   r["summary"] or "",
            "core_claim":r["core_claim"] or "",
            "subtopics": [s.lower().strip() for s in subtopics],
            "entities":  entities,
            "topics":    topics,
            "position":  r["position"],
            "authority": r["authority"],
            "likes":     r["likes"] or 0,
            "views":     r["views"] or 0,
            "created_at":r["created_at"] or "",
        })
    return tweets


def _parse_dt(s):
    if not s:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


# ── IDF ───────────────────────────────────────────────────────────────────────

def compute_idf(tweets):
    N = len(tweets)
    if N == 0:
        return {}
    df = Counter(s for t in tweets for s in set(t["subtopics"]))
    return {s: math.log(N / count) for s, count in df.items()}


# ── Clustering ────────────────────────────────────────────────────────────────

def run_union_find(tweets, idf):
    """Two-pass Union-Find + entity anchor merge + attachment pass."""
    ids   = [t["id"] for t in tweets]
    uf    = UnionFind(ids)
    by_id = {t["id"]: t for t in tweets}

    # Subtopic index
    subtopic_index = defaultdict(list)
    for t in tweets:
        for s in t["subtopics"]:
            subtopic_index[s].append(t["id"])

    # ── Pass 1: combined IDF threshold ───────────────────────────────────────
    pair_score = defaultdict(float)
    for subtopic, members in subtopic_index.items():
        w = idf.get(subtopic, 0.0)
        if w < 2.0 or len(members) > 300:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a > b:
                    a, b = b, a
                pair_score[(a, b)] += w

    for (a, b), score in pair_score.items():
        if score >= IDF_THRESHOLD:
            uf.union(a, b)

    # ── Pass 2: rare single-subtopic clustering (for unclustered tweets) ─────
    pass1_ids = {tid for c in uf.clusters() if len(c) >= MIN_CLUSTER for tid in c}
    for subtopic, members in subtopic_index.items():
        w = idf.get(subtopic, 0.0)
        if w < HIGH_IDF_THRESHOLD:
            continue
        remaining = [m for m in members if m not in pass1_ids]
        if len(remaining) < 2:
            continue
        for i in range(1, len(remaining)):
            uf.union(remaining[0], remaining[i])

    # ── Entity anchor merge ───────────────────────────────────────────────────
    raw_clusters = uf.clusters()
    cluster_entities = {}
    for cluster in raw_clusters:
        ents = Counter()
        for tid in cluster:
            for (name, etype) in by_id[tid]["entities"]:
                if etype in ENTITY_ANCHOR_TYPES and name:
                    ents[name] += 1
        cluster_entities[uf.find(cluster[0])] = {n for n, c in ents.items() if c >= 2}

    roots = list(cluster_entities.keys())
    for i in range(len(roots)):
        for j in range(i + 1, len(roots)):
            ri, rj = uf.find(roots[i]), uf.find(roots[j])
            if ri == rj:
                continue
            shared = cluster_entities.get(ri, set()) & cluster_entities.get(rj, set())
            if len(shared) >= ENTITY_ANCHOR_MIN:
                uf.union(ri, rj)
                merged = uf.find(ri)
                cluster_entities[merged] = (
                    cluster_entities.get(ri, set()) | cluster_entities.get(rj, set())
                )

    # ── Build core cluster index for attachment pass ──────────────────────────
    core_clusters   = {uf.find(c[0]): c for c in uf.clusters() if len(c) >= MIN_CLUSTER}
    core_member_ids = {tid for c in core_clusters.values() for tid in c}

    subtopic_to_roots = defaultdict(list)
    for root, cluster in core_clusters.items():
        cluster_subtopics = Counter(s for tid in cluster for s in by_id[tid]["subtopics"])
        for s in cluster_subtopics:
            subtopic_to_roots[s].append(root)

    # ── Attachment pass ───────────────────────────────────────────────────────
    attachments = defaultdict(list)
    for t in tweets:
        if t["id"] in core_member_ids:
            continue
        root_scores = defaultdict(float)
        for s in t["subtopics"]:
            w = idf.get(s, 0.0)
            if w < 2.0:
                continue
            for root in subtopic_to_roots.get(s, []):
                root_scores[root] += w
        if root_scores:
            best_root  = max(root_scores, key=root_scores.get)
            best_score = root_scores[best_root]
            if best_score >= ATTACH_THRESHOLD:
                attachments[best_root].append(t["id"])

    # Merge core + attached
    final_clusters = [
        core_clusters[root] + attachments.get(root, [])
        for root in core_clusters
    ]
    final_clusters.sort(key=len, reverse=True)
    return final_clusters, by_id


# ── Momentum ──────────────────────────────────────────────────────────────────

def _engagement_score(t):
    return math.log1p((t["likes"] or 0) + (t["views"] or 0))


def score_momentum(cluster_ids, by_id, all_engagement_scores):
    now         = datetime.now(timezone.utc)
    cutoff_60   = now - timedelta(days=60)
    cutoff_30   = now - timedelta(days=30)

    recent_ids = []
    older_ids  = []
    for tid in cluster_ids:
        dt = _parse_dt(by_id[tid]["created_at"])
        if dt >= cutoff_30:
            recent_ids.append(tid)
        elif dt >= cutoff_60:
            older_ids.append(tid)

    total = len(cluster_ids) or 1

    def _sub_score(ids):
        if not ids:
            return 0.0
        recency    = len(ids) / total
        eng_vals   = [_engagement_score(by_id[tid]) for tid in ids]
        eng_norm   = (sum(eng_vals) / len(eng_vals)) / (all_engagement_scores["max"] or 1)
        auth_count = sum(1 for tid in ids if by_id[tid]["authority"] in ("expert", "official"))
        authority  = auth_count / len(ids)
        return 0.55 * recency + 0.30 * eng_norm + 0.15 * authority

    score       = _sub_score(recent_ids + older_ids)
    recent_score = _sub_score(recent_ids)
    older_score  = _sub_score(older_ids)
    delta        = recent_score - older_score

    return round(score, 4), round(delta, 4)


def _precompute_engagement(tweets):
    vals = [_engagement_score(t) for t in tweets]
    return {"max": max(vals) if vals else 1.0}


# ── Slug derivation ───────────────────────────────────────────────────────────

def derive_slug(top_subtopics):
    parts = [re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") for s in top_subtopics[:3]]
    slug  = "-".join(p for p in parts if p)
    return slug[:80] or "cluster"


def _ensure_unique_slug(conn, slug):
    """Append numeric suffix if slug already exists with a different cluster."""
    existing = conn.execute("SELECT id FROM narratives WHERE slug=?", (slug,)).fetchone()
    if not existing:
        return slug
    # Already exists — will be overwritten via upsert (same slug = same cluster)
    return slug


# ── Label generation ──────────────────────────────────────────────────────────

def generate_labels(clusters_data, skip=False):
    """
    clusters_data: list of dicts with keys:
        index, top_subtopics, top_entities, sample_summaries, position_breakdown
    Returns list of dicts: {title, description, key_claim, dominant_position}
    """
    if skip:
        return [{"title": None, "description": None, "key_claim": None, "dominant_position": None}
                for _ in clusters_data]

    client  = anthropic.Anthropic()
    results = []

    for batch_start in range(0, len(clusters_data), LABEL_BATCH):
        batch = clusters_data[batch_start: batch_start + LABEL_BATCH]

        prompt = (
            "You are labelling discourse clusters from a legal/policy analyst's Twitter bookmarks "
            "(crypto regulation, AI governance, digital assets, legal profession transformation).\n\n"
            "For each cluster below, produce a JSON object with:\n"
            '  "title": 5-8 word descriptive headline (not a question)\n'
            '  "description": 2 sentences — what this cluster is about and why it matters\n'
            '  "key_claim": the dominant argument or thesis in 1 sentence\n'
            '  "dominant_position": one of pro | con | neutral | mixed\n\n'
            "Return a JSON array with one object per cluster, in the same order.\n\n"
            "Clusters:\n"
        )
        for c in batch:
            pos_str = "  ".join(f"{k}:{v}" for k, v in c["position_breakdown"].items())
            prompt += (
                f"\n[{c['index']}]\n"
                f"Subtopics: {', '.join(c['top_subtopics'][:5])}\n"
                f"Entities: {', '.join(c['top_entities'][:5])}\n"
                f"Position: {pos_str}\n"
                f"Samples:\n"
            )
            for s in c["sample_summaries"][:3]:
                prompt += f"  - {s[:150]}\n"

        for attempt in range(3):
            try:
                resp = client.messages.create(
                    model=LABEL_MODEL,
                    max_tokens=2048,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = resp.content[0].text.strip()
                # Strip markdown fences if present
                text = re.sub(r"^```(?:json)?\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
                parsed = json.loads(text)
                results.extend(parsed)
                break
            except Exception as e:
                print(f"[warn] Label generation attempt {attempt+1} failed: {e}")
                if attempt < 2:
                    time.sleep(10 * (attempt + 1))
                else:
                    results.extend([
                        {"title": None, "description": None,
                         "key_claim": None, "dominant_position": None}
                        for _ in batch
                    ])

        if batch_start + LABEL_BATCH < len(clusters_data):
            time.sleep(3)  # gentle rate limiting between batches

    return results


# ── DB writes ─────────────────────────────────────────────────────────────────

def _dominant(counter):
    return counter.most_common(1)[0][0] if counter else None


def write_to_db(conn, clusters, labels, by_id, all_engagement_scores):
    now_iso = datetime.now(timezone.utc).isoformat()
    narrative_rows = []  # for edge building

    for i, (cluster_ids, label) in enumerate(zip(clusters, labels)):
        tweets = [by_id[tid] for tid in cluster_ids]

        subtopic_counts = Counter(s for t in tweets for s in t["subtopics"])
        entity_counts   = Counter(name for t in tweets for (name, _) in t["entities"] if name)
        topic_counts    = Counter(topic for t in tweets for topic in t["topics"])
        pos_counts      = Counter(t["position"] for t in tweets if t["position"])

        top_subtopics   = [s for s, _ in subtopic_counts.most_common(5)]
        slug            = derive_slug(top_subtopics)
        momentum, delta = score_momentum(cluster_ids, by_id, all_engagement_scores)

        dates     = [_parse_dt(t["created_at"]) for t in tweets]
        first_seen = min(dates).isoformat() if dates else None
        last_seen  = max(dates).isoformat() if dates else None

        narrative_dict = {
            "slug":              slug,
            "title":             label.get("title"),
            "description":       label.get("description"),
            "key_claim":         label.get("key_claim"),
            "dominant_topic":    _dominant(topic_counts),
            "dominant_position": label.get("dominant_position") or _dominant(pos_counts),
            "status":            "active",
            "tweet_count":       len(cluster_ids),
            "momentum_score":    momentum,
            "momentum_delta":    delta,
            "first_seen":        first_seen,
            "last_seen":         last_seen,
            "clustered_at":      now_iso,
        }

        narrative_id = db.upsert_narrative(conn, narrative_dict)

        # Assign roles: tweets in the first 10% of the cluster's timeline = initiating,
        # con tweets = dissenting, rest = supporting / contextual
        if dates:
            timeline_start = min(dates)
            timeline_span  = (max(dates) - timeline_start).total_seconds() or 1
        for tid in cluster_ids:
            t   = by_id[tid]
            dt  = _parse_dt(t["created_at"])
            if dates and timeline_span:
                frac = (dt - timeline_start).total_seconds() / timeline_span
            else:
                frac = 0.5
            if frac <= 0.1:
                role = "initiating"
            elif t["position"] == "con":
                role = "dissenting"
            elif frac >= 0.85:
                role = "contextual"
            else:
                role = "supporting"
            db.link_tweet_narrative(conn, tid, narrative_id, role)

        narrative_rows.append({
            "id":       narrative_id,
            "subtopics": set(top_subtopics),
        })

    conn.commit()

    # Build edges between narratives sharing ≥2 subtopics
    for i in range(len(narrative_rows)):
        for j in range(i + 1, len(narrative_rows)):
            shared = narrative_rows[i]["subtopics"] & narrative_rows[j]["subtopics"]
            if len(shared) >= 2:
                db.upsert_edge(
                    conn,
                    narrative_rows[i]["id"],
                    narrative_rows[j]["id"],
                    "subtopic_overlap",
                    float(len(shared)),
                )
    conn.commit()
    return len(clusters)


# ── Public API ────────────────────────────────────────────────────────────────

def cluster_all(conn=None, no_labels=False, since_days=None):
    """Full re-cluster of all enriched tweets. Safe to re-run."""
    close = conn is None
    if conn is None:
        conn = db.get_conn()

    print(f"Loading tweets from DB...")
    tweets = load_tweets(conn, since_days=since_days)
    print(f"Loaded {len(tweets)} enriched tweets.")

    idf     = compute_idf(tweets)
    print("Running Union-Find clustering...")
    clusters, by_id = run_union_find(tweets, idf)
    print(f"Found {len(clusters)} clusters covering {sum(len(c) for c in clusters)} tweets.")

    eng_scores = _precompute_engagement(tweets)

    # Prepare cluster summaries for label generation
    clusters_data = []
    for i, cluster_ids in enumerate(clusters):
        tw            = [by_id[tid] for tid in cluster_ids]
        sub_counts    = Counter(s for t in tw for s in t["subtopics"])
        ent_counts    = Counter(n for t in tw for (n, _) in t["entities"] if n)
        pos_counts    = Counter(t["position"] for t in tw if t["position"])
        samples       = [t["summary"] for t in tw if t["summary"]][:3]
        clusters_data.append({
            "index":              i,
            "top_subtopics":      [s for s, _ in sub_counts.most_common(5)],
            "top_entities":       [n for n, _ in ent_counts.most_common(5)],
            "sample_summaries":   samples,
            "position_breakdown": dict(pos_counts),
        })

    print(f"Generating labels ({'skipping' if no_labels else f'batches of {LABEL_BATCH}'})...")
    labels = generate_labels(clusters_data, skip=no_labels)

    print("Writing to DB...")
    # Clear existing narrative data for a clean re-cluster
    conn.execute("DELETE FROM narrative_edges")
    conn.execute("DELETE FROM tweet_narratives")
    conn.execute("DELETE FROM narratives")
    conn.commit()

    n = write_to_db(conn, clusters, labels, by_id, eng_scores)
    print(f"Done. {n} narratives written to DB.")

    if close:
        conn.close()
    return n


def cluster_new(new_ids, conn=None):
    """
    Assign newly synced tweet IDs to existing narratives (attachment pass only).
    Creates new narratives only if ≥3 new tweets form a tight cluster together.
    """
    if not new_ids:
        return
    close = conn is None
    if conn is None:
        conn = db.get_conn()

    # Load new tweets
    placeholders = ",".join("?" * len(new_ids))
    rows = conn.execute(
        f"""SELECT id, subtopics, entities, position, authority, likes, views,
                   created_at, topics, summary, core_claim, author_handle
            FROM bookmarks WHERE id IN ({placeholders}) AND enriched_at IS NOT NULL""",
        new_ids,
    ).fetchall()

    if not rows:
        if close:
            conn.close()
        return

    tweets  = load_tweets(conn)           # full corpus for IDF
    new_set = {r["id"] for r in rows}
    idf     = compute_idf(tweets)
    by_id   = {t["id"]: t for t in tweets}

    # Build subtopic → existing narrative index
    existing = db.get_narrative_summaries(conn, limit=500)
    existing_sub_index = defaultdict(list)
    for n in existing:
        # fetch subtopics for this narrative via tweet_narratives
        sub_rows = conn.execute(
            """SELECT b.subtopics FROM bookmarks b
               JOIN tweet_narratives tn ON b.id = tn.tweet_id
               WHERE tn.narrative_id = ? LIMIT 50""",
            (n["id"],),
        ).fetchall()
        subs = Counter(
            s.lower().strip()
            for r in sub_rows
            for s in json.loads(r["subtopics"] or "[]")
        )
        for s in subs:
            existing_sub_index[s].append(n["id"])

    eng_scores = _precompute_engagement(tweets)
    now_iso    = datetime.now(timezone.utc).isoformat()

    assigned   = 0
    for tid in new_ids:
        if tid not in by_id:
            continue
        t           = by_id[tid]
        root_scores = defaultdict(float)
        for s in t["subtopics"]:
            w = idf.get(s, 0.0)
            if w < 2.0:
                continue
            for nid in existing_sub_index.get(s, []):
                root_scores[nid] += w
        if root_scores:
            best_nid   = max(root_scores, key=root_scores.get)
            best_score = root_scores[best_nid]
            if best_score >= ATTACH_THRESHOLD:
                db.link_tweet_narrative(conn, tid, best_nid, "supporting")
                # Update tweet_count and last_seen
                conn.execute(
                    "UPDATE narratives SET tweet_count = tweet_count + 1, last_seen = ? WHERE id = ?",
                    (now_iso, best_nid),
                )
                assigned += 1

    conn.commit()
    print(f"cluster_new: assigned {assigned} of {len(new_ids)} new tweets to existing narratives.")

    if close:
        conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args       = sys.argv[1:]
    no_labels  = "--no-labels" in args
    since_days = None
    if "--since" in args:
        idx = args.index("--since")
        val = args[idx + 1]          # e.g. "60d" or "90"
        since_days = int(re.sub(r"[^0-9]", "", val))

    cluster_all(no_labels=no_labels, since_days=since_days)
