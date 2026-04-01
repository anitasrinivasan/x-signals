#!/usr/bin/env python3
"""
Narrative clustering validation probe.

Runs Union-Find clustering on the last N days of enriched bookmarks
and prints cluster summaries to stdout. No DB writes, no API calls.

Usage:
    python3 cluster_probe.py              # last 60 days
    python3 cluster_probe.py --days 90   # last 90 days
    python3 cluster_probe.py --min 5     # require at least 5 tweets per cluster
"""

import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "signals.db"

# Tuning knobs
DEFAULT_DAYS = 60
DEFAULT_MIN_CLUSTER = 3
IDF_THRESHOLD = 10.0         # min combined IDF score to union two tweets (Pass 1)
HIGH_IDF_THRESHOLD = 6.5     # single subtopic IDF floor for Pass 2 (very rare terms)
ENTITY_ANCHOR_TYPES = {"agency", "bill", "company", "protocol"}
ENTITY_ANCHOR_MIN = 2        # min shared anchor entities to trigger merge


# ---------------------------------------------------------------------------
# Union-Find
# ---------------------------------------------------------------------------

class UnionFind:
    def __init__(self, keys):
        self.parent = {k: k for k in keys}
        self.rank = {k: 0 for k in keys}

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


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------

def load_tweets(days):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT id, author_handle, text, summary, subtopics, entities,
               position, authority, likes, views, created_at
        FROM bookmarks
        WHERE enriched_at IS NOT NULL
          AND created_at >= ?
        ORDER BY created_at DESC
        """,
        (cutoff,),
    ).fetchall()
    conn.close()

    tweets = []
    for r in rows:
        subtopics = json.loads(r["subtopics"] or "[]")
        entities_raw = json.loads(r["entities"] or "[]")
        # Normalise entity list: [{name, type}] or plain strings
        entities = []
        for e in entities_raw:
            if isinstance(e, dict):
                entities.append((e.get("name", "").lower().strip(), e.get("type", "").lower()))
            elif isinstance(e, str):
                entities.append((e.lower().strip(), "unknown"))
        tweets.append({
            "id": r["id"],
            "author": r["author_handle"],
            "summary": r["summary"],
            "subtopics": [s.lower().strip() for s in subtopics],
            "entities": entities,
            "position": r["position"],
            "authority": r["authority"],
            "likes": r["likes"] or 0,
            "views": r["views"] or 0,
            "created_at": r["created_at"],
        })
    return tweets


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def compute_idf(tweets):
    """IDF per subtopic: log(N / df). Rare subtopics score high; common ones score low."""
    N = len(tweets)
    df = Counter(s for t in tweets for s in set(t["subtopics"]))
    return {s: math.log(N / count) for s, count in df.items()}


def cluster_tweets(tweets, min_cluster):
    ids = [t["id"] for t in tweets]
    uf = UnionFind(ids)
    by_id = {t["id"]: t for t in tweets}

    idf = compute_idf(tweets)

    # Index: subtopic → list of tweet ids
    subtopic_index = defaultdict(list)
    for t in tweets:
        for s in t["subtopics"]:
            subtopic_index[s].append(t["id"])

    # Accumulate IDF-weighted score per pair of tweets
    pair_score = defaultdict(float)
    for subtopic, members in subtopic_index.items():
        w = idf.get(subtopic, 0.0)
        # Skip subtopics so common they carry almost no signal (IDF < 2 ≈ top 13%)
        if w < 2.0:
            continue
        if len(members) > 300:  # safety cap for degenerate cases
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a > b:
                    a, b = b, a
                pair_score[(a, b)] += w

    # Union pairs whose combined IDF score clears the threshold
    for (a, b), score in pair_score.items():
        if score >= IDF_THRESHOLD:
            uf.union(a, b)

    # Entity anchor merging: merge clusters sharing >= ENTITY_ANCHOR_MIN anchor entities
    raw_clusters = uf.clusters()
    cluster_entities = {}
    for cluster in raw_clusters:
        ents = Counter()
        for tid in cluster:
            for (name, etype) in by_id[tid]["entities"]:
                if etype in ENTITY_ANCHOR_TYPES and name:
                    ents[name] += 1
        # Only include entities that appear in multiple tweets within the cluster
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
                merged_root = uf.find(ri)
                cluster_entities[merged_root] = (
                    cluster_entities.get(ri, set()) | cluster_entities.get(rj, set())
                )

    # ── Pass 2: high-IDF single-subtopic clustering ──────────────────────────
    # Catches debates coalescing around one very specific term (e.g. "SB 1047",
    # "third-party audits") that didn't clear the combined threshold in Pass 1.
    # Only runs on tweets still unclustered after Pass 1.
    pass1_member_ids = {tid for c in uf.clusters() if len(c) >= min_cluster for tid in c}
    for subtopic, members in subtopic_index.items():
        w = idf.get(subtopic, 0.0)
        if w < HIGH_IDF_THRESHOLD:
            continue                        # too common to be an anchor on its own
        remaining = [m for m in members if m not in pass1_member_ids]
        if len(remaining) < 2:
            continue
        # Union all tweets sharing this rare subtopic together
        for i in range(1, len(remaining)):
            uf.union(remaining[0], remaining[i])

    # Identify core clusters (cleared either Pass 1 or Pass 2)
    core_clusters = {uf.find(c[0]): c for c in uf.clusters() if len(c) >= min_cluster}
    core_member_ids = {tid for cluster in core_clusters.values() for tid in cluster}

    # Build a subtopic → core cluster index for fast lookup
    # Maps subtopic → list of (root, cluster_size) — used to score unclustered tweets
    subtopic_to_roots = defaultdict(list)
    for root, cluster in core_clusters.items():
        cluster_subtopics = Counter(s for tid in cluster for s in by_id[tid]["subtopics"])
        for s, count in cluster_subtopics.items():
            subtopic_to_roots[s].append((root, count))

    # Attachment pass: assign unclustered tweets to their best-matching core cluster
    # A tweet needs IDF-weighted overlap >= ATTACH_THRESHOLD to be attached
    ATTACH_THRESHOLD = 4.0
    attachments = defaultdict(list)  # root → list of attached tweet ids

    for t in tweets:
        if t["id"] in core_member_ids:
            continue
        best_root = None
        best_score = ATTACH_THRESHOLD  # must beat this to attach
        root_scores = defaultdict(float)
        for s in t["subtopics"]:
            w = idf.get(s, 0.0)
            if w < 2.0:
                continue
            for (root, _) in subtopic_to_roots.get(s, []):
                root_scores[root] += w
        for root, score in root_scores.items():
            if score > best_score:
                best_score = score
                best_root = root
        if best_root is not None:
            attachments[best_root].append(t["id"])

    # Merge core clusters with their attached tweets
    final_clusters = []
    for root, cluster in core_clusters.items():
        merged = cluster + attachments.get(root, [])
        final_clusters.append(merged)

    final_clusters.sort(key=len, reverse=True)
    return final_clusters, by_id, idf


# ---------------------------------------------------------------------------
# Momentum
# ---------------------------------------------------------------------------

def momentum(cluster_ids, by_id):
    now = datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(days=30)
    older_cutoff = now - timedelta(days=60)

    recent_count = 0
    older_count = 0
    for tid in cluster_ids:
        try:
            dt_str = by_id[tid]["created_at"]
            # Handle timezone-aware ISO strings
            if dt_str.endswith("Z"):
                dt_str = dt_str[:-1] + "+00:00"
            dt = datetime.fromisoformat(dt_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= recent_cutoff:
                recent_count += 1
            elif dt >= older_cutoff:
                older_count += 1
        except Exception:
            pass

    total = recent_count + older_count or 1
    # Fraction of activity in recent half vs older half
    ratio = recent_count / total
    direction = "↑ heating up" if ratio > 0.6 else ("↓ cooling" if ratio < 0.4 else "→ steady")
    return recent_count, older_count, direction


# ---------------------------------------------------------------------------
# Print report
# ---------------------------------------------------------------------------

def print_report(clusters, by_id, total_tweets):
    print(f"\n{'='*70}")
    print(f"NARRATIVE CLUSTER PROBE — {len(clusters)} clusters from {total_tweets} tweets")
    print(f"{'='*70}\n")

    for i, cluster_ids in enumerate(clusters, 1):
        tweets = [by_id[tid] for tid in cluster_ids]

        # Date range
        dates = []
        for t in tweets:
            try:
                ds = t["created_at"]
                if ds.endswith("Z"):
                    ds = ds[:-1] + "+00:00"
                dates.append(datetime.fromisoformat(ds))
            except Exception:
                pass
        if dates:
            first = min(dates).strftime("%b %d")
            last = max(dates).strftime("%b %d")
            date_range = f"{first} → {last}"
        else:
            date_range = "unknown"

        # Top subtopics
        subtopic_counts = Counter(s for t in tweets for s in t["subtopics"])
        top_subtopics = [f"{s} ({n})" for s, n in subtopic_counts.most_common(4)]

        # Top entities
        entity_counts = Counter(name for t in tweets for (name, _) in t["entities"] if name)
        top_entities = [f"{e} ({n})" for e, n in entity_counts.most_common(4)]

        # Position breakdown
        pos_counts = Counter(t["position"] for t in tweets if t["position"])
        pos_str = "  ".join(f"{p}: {n}" for p, n in pos_counts.most_common())

        # Authority breakdown
        auth_counts = Counter(t["authority"] for t in tweets if t["authority"])

        # Momentum
        recent, older, direction = momentum(cluster_ids, by_id)

        # Sample summaries
        samples = [t["summary"] for t in tweets if t["summary"]][:2]

        print(f"CLUSTER {i:03d}  [{len(cluster_ids)} tweets]  {date_range}  {direction}")
        print(f"  Subtopics : {', '.join(top_subtopics) or '—'}")
        print(f"  Entities  : {', '.join(top_entities) or '—'}")
        print(f"  Position  : {pos_str or '—'}")
        print(f"  Authority : {', '.join(f'{k}:{v}' for k,v in auth_counts.most_common())}")
        print(f"  Momentum  : {recent} tweets (last 30d) vs {older} (prior 30d)")
        for j, s in enumerate(samples, 1):
            # Truncate long summaries
            display = s if len(s) <= 120 else s[:117] + "..."
            print(f"  Sample {j}  : {display}")
        print()

    # Summary stats
    sizes = [len(c) for c in clusters]
    noise_tweets = total_tweets - sum(sizes)
    print(f"{'─'*70}")
    print(f"Cluster sizes: min={min(sizes)}, max={max(sizes)}, median={sorted(sizes)[len(sizes)//2]}")
    print(f"Tweets in clusters: {sum(sizes)} / {total_tweets}  ({noise_tweets} unclustered/noise)")
    print()


def print_unclustered(clusters, by_id, tweets, n_sample=30):
    """Show what's in the unclustered tweets."""
    clustered_ids = {tid for c in clusters for tid in c}
    unclustered = [t for t in tweets if t["id"] not in clustered_ids]

    print(f"\n{'='*70}")
    print(f"UNCLUSTERED TWEETS — {len(unclustered)} tweets not assigned to any cluster")
    print(f"{'='*70}\n")

    # 1. Subtopic frequency among unclustered — shows what's isolated
    subtopic_counts = Counter(s for t in unclustered for s in t["subtopics"])
    print("Top 30 subtopics in unclustered pool (potential missed clusters):")
    for s, n in subtopic_counts.most_common(30):
        print(f"  {n:4d}  {s}")
    print()

    # 2. How many unclustered tweets share a subtopic with >=1 other unclustered tweet
    subtopic_to_unclustered = defaultdict(list)
    for t in unclustered:
        for s in t["subtopics"]:
            subtopic_to_unclustered[s].append(t["id"])

    groupable = {tid for s, members in subtopic_to_unclustered.items()
                 if len(members) >= 3 for tid in members}
    print(f"Unclustered tweets sharing a subtopic with 2+ others: {len(groupable)} "
          f"({100*len(groupable)//len(unclustered) if unclustered else 0}%)")
    print()

    # 3. Show the top 5 subtopics that appear 3+ times in unclustered — missed clusters
    print("Subtopics with 3+ unclustered tweets (potential new clusters at lower threshold):")
    shown = 0
    for s, members in sorted(subtopic_to_unclustered.items(), key=lambda x: -len(x[1])):
        if len(members) < 3:
            break
        print(f"\n  '{s}'  ({len(members)} tweets)")
        for tid in members[:3]:
            t = by_id[tid]
            summary = (t["summary"] or "")[:100]
            print(f"    [{t['position'] or '?'}] @{t['author']}: {summary}")
        shown += 1
        if shown >= 10:
            break

    print()

    # 4. Sample of true singletons (subtopics unique in the corpus)
    singletons = [t for t in unclustered
                  if all(subtopic_counts[s] == 1 for s in t["subtopics"])]
    print(f"True singletons (all subtopics unique in unclustered pool): {len(singletons)}")
    if singletons:
        print("Sample:")
        for t in singletons[:5]:
            summary = (t["summary"] or "")[:110]
            print(f"  @{t['author']}: {summary}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    days = DEFAULT_DAYS
    min_cluster = DEFAULT_MIN_CLUSTER

    args = sys.argv[1:]
    if "--days" in args:
        idx = args.index("--days")
        days = int(args[idx + 1])
    if "--min" in args:
        idx = args.index("--min")
        min_cluster = int(args[idx + 1])

    print(f"Loading enriched tweets from last {days} days...")
    tweets = load_tweets(days)
    print(f"Loaded {len(tweets)} tweets. Running Union-Find clustering...")

    clusters, by_id, idf = cluster_tweets(tweets, min_cluster)
    print_report(clusters, by_id, len(tweets))
    print_unclustered(clusters, by_id, tweets)
