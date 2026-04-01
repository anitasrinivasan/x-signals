#!/usr/bin/env python3
"""
x-signals: Knowledge graph and writing assistant for Twitter bookmarks.

Run with: streamlit run app.py
"""

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

SCRIPT_DIR = Path(__file__).parent
DB_PATH = SCRIPT_DIR / "signals.db"
ENV_PATH = SCRIPT_DIR / ".env"

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

TOPIC_COLORS = {
    "AI Systems & Agents": "#6366f1",
    "Regulatory & Policy": "#ef4444",
    "Crypto/DeFi/Web3": "#f59e0b",
    "Digital Asset Regulation": "#10b981",
    "Legal Profession & AI": "#8b5cf6",
    "Developer Infrastructure": "#3b82f6",
    "Funding & Markets": "#ec4899",
    "Research & Philosophy": "#14b8a6",
}

WRITING_ASSISTANT_SYSTEM = """You are a research and writing assistant for a legal/policy analyst who publishes at high-quality venues including Ivy League law review companions (e.g. CLR, YLJ online), Tech Policy Press, Lawfare, and Substack.

The analyst's expertise spans crypto regulation, digital asset policy, AI governance, and the transformation of the legal profession by AI. She writes long-form, well-sourced arguments — not news recaps.

You have access to two tools:

1. `query_narratives` — queries pre-computed narrative clusters derived from the bookmark corpus.
   Tables: narratives(id, title, description, key_claim, dominant_topic, dominant_position,
   momentum_score, momentum_delta, tweet_count, first_seen, last_seen, status),
   tweet_narratives(tweet_id, narrative_id, role),
   narrative_edges(source_id, target_id, edge_type, weight).
   momentum_score (0–1): overall activity. momentum_delta: positive = heating up, negative = cooling.

2. `query_db` — queries the raw bookmark corpus: 5,000+ Twitter bookmarks spanning Dec 2022–Mar 2026.
   Schema: bookmarks(id, author_handle, author_name, text, quoted_tweet_text, created_at,
   topics [JSON], subtopics [JSON], content_type, authority, summary, core_claim, position,
   entities [JSON], likes, views, urls [JSON]).
   Use json_each(topics) to filter by topic. created_at is ISO 8601.

When answering:
- For trend analysis and pitch generation: start with query_narratives, then use query_db only to fetch specific supporting tweets
- Cite specific tweets by author_handle and summary/core_claim
- Distinguish between expert opinion, primary sources, and community views
- Be direct about what the corpus does and doesn't contain
- For writing pitches, be specific: a real thesis, not just a topic area"""

OPUS_PITCH_SYSTEM = """You are writing notes for yourself. You are a legal/policy analyst — you publish rigorous, opinionated analysis at law review companions, Tech Policy Press, Lawfare, and Substack. Your writing is direct, argues a specific position, and trusts the reader. You use "I" naturally.

You've just finished scanning your bookmark corpus and you want to capture what you'd actually pitch to an editor this week. Write as if you're jotting urgent notes — specific, grounded, impatient with vagueness.

Not: "Scholars have debated whether X..."
But: "The thing I want to say about X is Y, and this week is the moment because Z."

Venue shorthand: LR = law review companion | TPP = Tech Policy Press | LF = Lawfare | Sub = Substack | Thread = X thread

Format each pitch as:
**[Headline]** → [Venue]
[2-3 sentences of why this, why now, what the argument actually is]
Sources: [2-3 @handles with their specific claims]"""

WHAT_TO_WRITE_PROMPT = """Analyze my bookmark corpus to surface what I should write next.

Start by calling query_narratives to get the narrative landscape:
  SELECT * FROM narratives ORDER BY momentum_score DESC LIMIT 30

Then identify:
1. Narratives with high positive momentum_delta (heating up fast — write now)
2. Narratives with strong position split (dominant_position = 'mixed' or near-even pro/con — contested debate with no winner yet)
3. Narrative pairs connected by edges (cross-topic stories no one has connected in print)
4. High momentum_score but zero edges (isolated discourse — potential first-mover piece)

For each pitch, use query_db to fetch 3-4 specific supporting tweets from tweet_narratives JOIN bookmarks.

Surface 4-5 pitches. For each:
1. **The thesis** — a specific, arguable claim, not just a topic
2. **Why now** — what the momentum data signals (delta direction, position split, convergence)
3. **Venue** — law review companion (doctrinal depth), Tech Policy Press/Lawfare (timely policy, 1500-3000w), Substack (nuanced take), or Thread (fast-moving)
4. **Key sources** — 3-4 specific tweets from the corpus (author + claim)"""


def load_env():
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ[key.strip()] = value.strip()


def send_telegram(text):
    """Send a Telegram message if credentials are configured."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        import urllib.request, urllib.parse
        payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # Telegram is best-effort; don't surface errors in the UI


def save_pitches(text):
    """Save pitch text to ~/x-signals/pitches/YYYY-MM-DD.md. Returns file path."""
    pitches_dir = SCRIPT_DIR / "pitches"
    pitches_dir.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    path = pitches_dir / f"{date_str}.md"
    path.write_text(text, encoding="utf-8")
    return path


def format_pitch_telegram(text, date_str):
    """Extract first 2 pitch blocks for Telegram preview."""
    lines = text.strip().splitlines()
    previews = []
    current = []
    for line in lines:
        if line.startswith("**") and current:
            previews.append("\n".join(current).strip())
            current = [line]
            if len(previews) >= 2:
                break
        else:
            current.append(line)
    if current and len(previews) < 2:
        previews.append("\n".join(current).strip())

    body = "\n\n".join(p[:300] for p in previews[:2])
    total = text.count("\n**")  # rough pitch count
    more = f"\n\n[+{max(0, total - 2)} more pitches saved locally]" if total > 2 else ""
    return f"📝 *x-signals pitches — {date_str}*\n\n{body}{more}"


def get_conn():
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def run_query(sql, params=()):
    conn = get_conn()
    if not conn:
        return []
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        st.error(f"Query error: {e}\n\nSQL: {sql}")
        return []
    finally:
        conn.close()


def parse_json_field(val):
    if not val:
        return []
    try:
        return json.loads(val)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Tweet card component
# ---------------------------------------------------------------------------

def tweet_card(t, show_summary=True):
    topics = parse_json_field(t.get("topics"))
    entities = parse_json_field(t.get("entities"))
    urls = parse_json_field(t.get("urls"))

    author = t.get("author_handle", "unknown")
    created = t.get("created_at", "")[:10]
    tweet_url = f"https://x.com/{author}/status/{t['id']}"

    with st.container():
        cols = st.columns([3, 1])
        with cols[0]:
            st.markdown(f"**[@{author}](https://x.com/{author})** · {created}")
        with cols[1]:
            likes = t.get("likes", 0) or 0
            views = t.get("views", 0) or 0
            st.caption(f"♥ {likes:,}  👁 {views:,}")

        if show_summary and t.get("summary"):
            st.markdown(f"*{t['summary']}*")
            if t.get("core_claim"):
                st.caption(f"**Claim:** {t['core_claim']}")

        with st.expander("Full text"):
            st.write(t.get("text", ""))
            if t.get("quoted_tweet_text"):
                st.caption(f"↩ Quoted: {t['quoted_tweet_text'][:300]}")

        # Topic tags
        tag_cols = st.columns(min(len(topics) + 1, 5))
        for i, topic in enumerate(topics[:4]):
            color = TOPIC_COLORS.get(topic, "#6b7280")
            tag_cols[i].markdown(
                f'<span style="background:{color}22;color:{color};padding:2px 8px;border-radius:12px;font-size:0.75rem">{topic}</span>',
                unsafe_allow_html=True,
            )

        link_col = tag_cols[-1] if len(topics) < 4 else st.columns(1)[0]
        link_col.markdown(f"[→ Tweet]({tweet_url})")

        st.divider()


# ---------------------------------------------------------------------------
# Narrative Graph tab
# ---------------------------------------------------------------------------

def narrative_graph_tab():
    st.header("Narrative Graph")

    # Check if narratives exist
    narratives = run_query(
        "SELECT id, title, description, key_claim, dominant_topic, dominant_position, "
        "momentum_score, momentum_delta, tweet_count, first_seen, last_seen, status "
        "FROM narratives ORDER BY momentum_score DESC"
    )
    if not narratives:
        st.info(
            "No narratives yet. Run `python3 cluster.py` to generate narrative clusters."
        )
        return

    edges = run_query(
        "SELECT source_id, target_id, edge_type, weight FROM narrative_edges"
    )

    # Sidebar controls
    with st.sidebar:
        st.subheader("Narrative filters")
        min_tweets = st.slider("Min tweet count", 1, 30, 3)
        topic_filter = st.multiselect("Topic filter", TOPICS)
        status_filter = st.selectbox("Status", ["All", "active", "dormant"])

    # Filter narratives
    filtered = [
        n for n in narratives
        if n["tweet_count"] >= min_tweets
        and (not topic_filter or n["dominant_topic"] in topic_filter)
        and (status_filter == "All" or n["status"] == status_filter)
    ]

    if not filtered:
        st.warning("No narratives match current filters.")
        return

    filtered_ids = {n["id"] for n in filtered}
    filtered_edges = [
        e for e in edges
        if e["source_id"] in filtered_ids and e["target_id"] in filtered_ids
    ]

    # Metrics row
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Narratives", len(filtered))
    m2.metric("Edges", len(filtered_edges))
    heating = sum(1 for n in filtered if n["momentum_delta"] > 0.05)
    m3.metric("↑ Heating up", heating)
    cooling = sum(1 for n in filtered if n["momentum_delta"] < -0.05)
    m4.metric("↓ Cooling", cooling)

    # Build pyvis graph
    try:
        import networkx as nx
        from pyvis.network import Network

        G = nx.Graph()
        for n in filtered:
            color = TOPIC_COLORS.get(n["dominant_topic"] or "", "#6b7280")
            size  = max(10, min(60, (n["tweet_count"] or 1) * 3))
            delta = n["momentum_delta"] or 0
            arrow = "↑" if delta > 0.05 else ("↓" if delta < -0.05 else "→")
            label = (n["title"] or n["id"])[:40]
            hover = (
                f"<b>{n['title'] or 'Unlabelled'}</b><br>"
                f"Momentum: {n['momentum_score']:.3f} {arrow}{delta:+.3f}<br>"
                f"Tweets: {n['tweet_count']} · {n['dominant_position'] or '?'}<br>"
                f"<i>{(n['description'] or '')[:120]}</i>"
            )
            G.add_node(n["id"], label=label, size=size, color=color, title=hover)

        edge_colors = {
            "subtopic_overlap": "#6b7280",
            "entity_anchor":    "#f59e0b",
            "position_opposition": "#ef4444",
        }
        for e in filtered_edges:
            ecolor = edge_colors.get(e["edge_type"], "#6b7280")
            G.add_edge(
                e["source_id"], e["target_id"],
                weight=e["weight"], color=ecolor,
                width=max(1, int(e["weight"])),
            )

        net = Network(
            height="600px", width="100%",
            bgcolor="#0e1117", font_color="white",
            notebook=False,
        )
        net.from_nx(G)
        net.set_options(json.dumps({
            "physics": {
                "stabilization": {"iterations": 150},
                "barnesHut": {"gravitationalConstant": -8000, "springLength": 120},
            },
            "edges": {"smooth": {"type": "continuous"}},
            "interaction": {
                "hover":        True,
                "tooltipDelay": 100,
                "zoomView":     True,
                "dragView":     True,
                "minZoom":      0.15,
                "maxZoom":      3.0,
                "navigationButtons": True,
            },
        }))

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w") as f:
            net.save_graph(f.name)
            html = open(f.name).read()

        # Inject graph control buttons directly into the pyvis iframe HTML
        btn_style = (
            "background:#1e293b;color:#e2e8f0;border:1px solid #334155;"
            "border-radius:6px;padding:5px 12px;margin:2px;cursor:pointer;"
            "font-size:12px;font-family:sans-serif;"
        )
        controls_html = f"""
<div id="graph-controls" style="position:absolute;top:8px;right:8px;z-index:1000;display:flex;gap:4px;">
  <button style="{btn_style}" onclick="network.fit()">⊙ Reset view</button>
  <button style="{btn_style}" id="btn-physics"
    onclick="if(window._physicsOn){{network.stopSimulation();this.textContent='▶ Physics';window._physicsOn=false;}}else{{network.startSimulation();this.textContent='⏸ Freeze';window._physicsOn=true;}}">⏸ Freeze</button>
</div>
<script>window._physicsOn = true;</script>
"""
        # Inject before </body>
        html = html.replace("</body>", controls_html + "\n</body>")
        st.components.v1.html(html, height=640, scrolling=False)

    except ImportError:
        st.error("pyvis/networkx not installed. Run: `pip install pyvis networkx`")
        st.info("Showing table view instead.")

    # Momentum leaderboard
    st.subheader("Momentum leaderboard")
    leaderboard = sorted(filtered, key=lambda n: n["momentum_score"], reverse=True)[:15]
    rows = []
    for n in leaderboard:
        delta = n["momentum_delta"] or 0
        arrow = "↑" if delta > 0.05 else ("↓" if delta < -0.05 else "→")
        rows.append({
            "Narrative": n["title"] or n["id"],
            "Topic": n["dominant_topic"] or "—",
            "Tweets": n["tweet_count"],
            "Score": f"{n['momentum_score']:.3f}",
            "Trend": f"{arrow} {delta:+.3f}",
            "Position": n["dominant_position"] or "—",
            "Last seen": (n["last_seen"] or "")[:10],
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    # Narrative detail expander
    st.subheader("Narrative details")
    selected_title = st.selectbox(
        "Select narrative to explore",
        options=[n["title"] or f"Narrative {n['id']}" for n in filtered],
    )
    selected = next((n for n in filtered if (n["title"] or f"Narrative {n['id']}") == selected_title), None)
    if selected:
        st.markdown(f"**{selected['title']}**")
        if selected.get("description"):
            st.write(selected["description"])
        if selected.get("key_claim"):
            st.caption(f"**Key claim:** {selected['key_claim']}")

        # Fetch tweets for this narrative
        narrative_tweets = run_query(
            """SELECT b.*, tn.role FROM bookmarks b
               JOIN tweet_narratives tn ON b.id = tn.tweet_id
               WHERE tn.narrative_id = ?
               ORDER BY b.created_at DESC LIMIT 25""",
            (selected["id"],),
        )
        st.caption(f"{len(narrative_tweets)} tweets in this narrative")
        for t in narrative_tweets:
            tweet_card(t)


# ---------------------------------------------------------------------------
# Browse tab
# ---------------------------------------------------------------------------

def browse_tab():
    st.header("Browse")

    # Sidebar filters
    with st.sidebar:
        st.subheader("Filters")

        selected_topics = st.multiselect("Topics", TOPICS)
        author_filter = st.text_input("Author handle (partial ok)").strip().lower()

        date_min = datetime(2022, 12, 1)
        date_max = datetime.now()
        date_range = st.date_input(
            "Date range",
            value=(date_min.date(), date_max.date()),
            min_value=date_min.date(),
            max_value=date_max.date(),
        )

        content_types = st.multiselect(
            "Content type",
            ["primary_source", "expert_opinion", "debate", "data_claim", "announcement", "thread", "other"],
        )
        authority_filter = st.multiselect("Authority", ["official", "expert", "community"])

        only_enriched = st.checkbox("Only enriched rows", value=True)

    # Build query
    conditions = []
    params = []

    if only_enriched:
        conditions.append("enriched_at IS NOT NULL")

    if selected_topics:
        topic_conds = []
        for topic in selected_topics:
            topic_conds.append(
                "EXISTS (SELECT 1 FROM json_each(topics) WHERE value = ?)"
            )
            params.append(topic)
        conditions.append("(" + " OR ".join(topic_conds) + ")")

    if author_filter:
        conditions.append("LOWER(author_handle) LIKE ?")
        params.append(f"%{author_filter}%")

    if len(date_range) == 2:
        conditions.append("created_at >= ? AND created_at <= ?")
        params.append(date_range[0].isoformat())
        params.append(date_range[1].isoformat() + "T23:59:59")

    if content_types:
        placeholders = ",".join("?" * len(content_types))
        conditions.append(f"content_type IN ({placeholders})")
        params.extend(content_types)

    if authority_filter:
        placeholders = ",".join("?" * len(authority_filter))
        conditions.append(f"authority IN ({placeholders})")
        params.extend(authority_filter)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    count_sql = f"SELECT COUNT(*) as n FROM bookmarks {where}"
    data_sql = f"SELECT * FROM bookmarks {where} ORDER BY created_at DESC"

    count_rows = run_query(count_sql, params)
    total = count_rows[0]["n"] if count_rows else 0

    # Header metrics
    m1, m2, m3 = st.columns(3)
    m1.metric("Matching bookmarks", f"{total:,}")
    enriched = run_query("SELECT COUNT(*) as n FROM bookmarks WHERE enriched_at IS NOT NULL")
    m2.metric("Total enriched", f"{enriched[0]['n']:,}" if enriched else "—")
    m3.metric("Total in corpus", run_query("SELECT COUNT(*) as n FROM bookmarks")[0]["n"])

    if total == 0:
        st.info("No results. Try adjusting your filters.")
        return

    # Topic distribution chart
    topic_sql = f"""
        SELECT jt.value as topic, COUNT(*) as cnt
        FROM bookmarks b, json_each(b.topics) jt
        {where.replace("WHERE", "WHERE b.id IN (SELECT id FROM bookmarks " + where + ") AND")}
        GROUP BY topic ORDER BY cnt DESC LIMIT 12
    """
    # Simpler approach: fetch all and compute in Python
    all_rows = run_query(data_sql, params)
    topic_counts = {}
    for row in all_rows:
        for t in parse_json_field(row.get("topics")):
            topic_counts[t] = topic_counts.get(t, 0) + 1

    if topic_counts:
        sorted_topics = sorted(topic_counts.items(), key=lambda x: x[1], reverse=True)
        fig = px.bar(
            x=[c for _, c in sorted_topics],
            y=[t for t, _ in sorted_topics],
            orientation="h",
            color=[t for t, _ in sorted_topics],
            color_discrete_map=TOPIC_COLORS,
            labels={"x": "Count", "y": ""},
            height=300,
        )
        fig.update_layout(showlegend=False, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    # Timeline scatter
    timeline_data = [
        {
            "date": r["created_at"][:10],
            "topic": parse_json_field(r.get("topics"))[0] if parse_json_field(r.get("topics")) else "Other",
            "author": r["author_handle"],
            "summary": r.get("summary") or r.get("text", "")[:80],
        }
        for r in all_rows
        if r.get("created_at")
    ]
    if timeline_data:
        fig2 = px.scatter(
            timeline_data,
            x="date",
            y="topic",
            color="topic",
            color_discrete_map=TOPIC_COLORS,
            hover_data=["author", "summary"],
            height=280,
            title="Activity timeline",
        )
        fig2.update_layout(showlegend=False, margin=dict(l=0, r=0, t=30, b=0))
        st.plotly_chart(fig2, use_container_width=True)

    # Paginated tweet cards
    page_size = 25
    total_pages = (total + page_size - 1) // page_size
    page = st.number_input("Page", min_value=1, max_value=max(total_pages, 1), value=1) - 1
    page_rows = all_rows[page * page_size: (page + 1) * page_size]

    st.caption(f"Showing {page * page_size + 1}–{min((page + 1) * page_size, total)} of {total:,}")
    for row in page_rows:
        tweet_card(row)


# ---------------------------------------------------------------------------
# Writing Assistant tab
# ---------------------------------------------------------------------------

def writing_assistant_tab():
    st.header("Writing Assistant")
    st.caption("Powered by your 5,000+ bookmark corpus · Claude with live DB access")

    load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        st.error("ANTHROPIC_API_KEY not set in .env")
        return

    try:
        import anthropic
    except ImportError:
        st.error("anthropic package not installed. Run: pip install anthropic")
        return

    client = anthropic.Anthropic(api_key=api_key)

    # Mode selector
    mode = st.radio(
        "Mode",
        ["💡 What to Write Next", "🔍 Topic Briefing", "🔨 Argument Builder"],
        horizontal=True,
    )

    # Input area
    user_input = None
    if mode == "💡 What to Write Next":
        st.info("No input needed — I'll scan your recent bookmarks and surface the strongest pitches.")
        run_btn = st.button("Generate pitches", type="primary")
        if run_btn:
            user_input = WHAT_TO_WRITE_PROMPT
    elif mode == "🔍 Topic Briefing":
        topic_input = st.text_area(
            "Topic or angle",
            placeholder="e.g. 'stablecoin yield regulation' or 'AI agents and fiduciary duty'",
            height=80,
        )
        run_btn = st.button("Brief me", type="primary")
        if run_btn and topic_input.strip():
            user_input = (
                f"Give me a comprehensive briefing on: {topic_input.strip()}\n\n"
                "Include: the landscape of positions and who holds them, the strongest voices on each side, "
                "key primary sources saved in my corpus, how the debate has evolved over time, "
                "and a gap analysis — what's notably absent that could be a thesis."
            )
    else:  # Argument Builder
        thesis_input = st.text_area(
            "Your draft thesis",
            placeholder="e.g. 'DeFi protocols that auto-compound staking rewards are offering unregistered securities under the Howey test'",
            height=100,
        )
        run_btn = st.button("Build the argument", type="primary")
        if run_btn and thesis_input.strip():
            user_input = (
                f"Help me build this argument using my bookmark corpus:\n\n{thesis_input.strip()}\n\n"
                "Return: supporting evidence from the corpus (cited), the strongest counterarguments with their sources, "
                "adjacent arguments that strengthen or complicate this thesis, "
                "and a venue recommendation (law review companion / Tech Policy Press / Lawfare / Substack / Thread) with rationale."
            )

    if not user_input:
        return

    # Telegram notify option (only for What to Write Next)
    notify_mode = False
    if mode == "💡 What to Write Next":
        tg_ready = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
        notify_mode = st.checkbox(
            "🔔 Send to Telegram when done",
            value=tg_ready,
            disabled=not tg_ready,
            help="Requires TELEGRAM_BOT_TOKEN in .env" if not tg_ready else "",
        )

    # Tools for the agentic loop
    query_db_tool = {
        "name": "query_db",
        "description": (
            "Execute a read-only SQL SELECT query against the bookmarks SQLite database. "
            "Returns a JSON array of matching rows. "
            "Schema: bookmarks(id TEXT, author_handle TEXT, author_name TEXT, text TEXT, "
            "quoted_tweet_text TEXT, created_at TEXT, topics TEXT [JSON array], subtopics TEXT [JSON array], "
            "content_type TEXT, authority TEXT, summary TEXT, core_claim TEXT, position TEXT, "
            "entities TEXT [JSON array], likes INT, views INT, urls TEXT [JSON array]). "
            "Use json_each(topics) to filter by topic. created_at is ISO 8601. "
            "Limit results to avoid overwhelming the context. "
            "Also has access to tweet_narratives(tweet_id, narrative_id, role) for joining."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A valid SQLite SELECT statement. Must start with SELECT.",
                }
            },
            "required": ["sql"],
        },
    }

    query_narratives_tool = {
        "name": "query_narratives",
        "description": (
            "Query pre-computed narrative clusters. USE THIS FIRST for 'What to Write Next' and Topic Briefing. "
            "Returns compact narrative summaries (~75 tokens each). "
            "Tables: "
            "narratives(id INT, title TEXT, description TEXT, key_claim TEXT, dominant_topic TEXT, "
            "dominant_position TEXT, momentum_score REAL 0-1, momentum_delta REAL, "
            "tweet_count INT, first_seen TEXT, last_seen TEXT, status TEXT), "
            "tweet_narratives(tweet_id TEXT, narrative_id INT, role TEXT), "
            "narrative_edges(source_id INT, target_id INT, edge_type TEXT, weight REAL). "
            "momentum_delta > 0 means heating up, < 0 means cooling. "
            "dominant_position: pro | con | neutral | mixed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "A valid SQLite SELECT statement against narrative tables.",
                }
            },
            "required": ["sql"],
        },
    }

    # Check whether narratives table has data; only include the tool if populated
    has_narratives = bool(run_query("SELECT 1 FROM narratives LIMIT 1"))
    tools = [query_narratives_tool, query_db_tool] if has_narratives else [query_db_tool]

    messages = [{"role": "user", "content": user_input}]
    is_pitch_mode = (mode == "💡 What to Write Next")

    # ── Phase 1: Sonnet + tools (research) ───────────────────────────────────
    with st.spinner("Researching your corpus…"):
        research_placeholder = st.empty()
        query_log  = []
        research_brief = ""  # accumulated Sonnet text (used as context for Opus)

        for iteration in range(10):
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=4096,
                system=WRITING_ASSISTANT_SYSTEM,
                tools=tools,
                messages=messages,
            )

            tool_calls  = []
            text_blocks = []
            for block in response.content:
                if block.type == "text":
                    text_blocks.append(block.text)
                elif block.type == "tool_use":
                    tool_calls.append(block)

            if text_blocks:
                research_brief = "\n\n".join(text_blocks)
                if not is_pitch_mode:
                    # For non-pitch modes, render Sonnet output directly
                    research_placeholder.markdown(research_brief)

            if response.stop_reason == "end_turn" or not tool_calls:
                break

            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for tool_call in tool_calls:
                sql       = tool_call.input.get("sql", "")
                tool_name = tool_call.name
                query_log.append(f"[{tool_name}] {sql}")

                if not sql.strip().upper().startswith("SELECT"):
                    result_content = json.dumps({"error": "Only SELECT queries are allowed."})
                else:
                    try:
                        rows = run_query(sql)
                        if tool_name == "query_narratives":
                            slimmed = [dict(r) for r in rows[:30]]
                        else:
                            def slim(r):
                                return {
                                    "id":           r.get("id"),
                                    "author":       r.get("author_handle"),
                                    "created_at":   (r.get("created_at") or "")[:10],
                                    "summary":      (r.get("summary") or "")[:200],
                                    "core_claim":   (r.get("core_claim") or "")[:150],
                                    "topics":       r.get("topics"),
                                    "subtopics":    r.get("subtopics"),
                                    "content_type": r.get("content_type"),
                                    "authority":    r.get("authority"),
                                    "position":     r.get("position"),
                                    "text":         (r.get("text") or "")[:200],
                                    "likes":        r.get("likes"),
                                    "views":        r.get("views"),
                                }
                            slimmed = [slim(r) for r in rows[:20]]
                        result_content = json.dumps(
                            {"total_matched": len(rows), "returned": len(slimmed), "rows": slimmed},
                            default=str,
                        )
                    except Exception as e:
                        result_content = json.dumps({"error": str(e)})

                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": tool_call.id,
                    "content":     result_content,
                })

            messages.append({"role": "user", "content": tool_results})

    # ── Phase 2 (pitch mode only): Opus writes the final pitches ─────────────
    final_text = research_brief  # default: use Sonnet output as-is

    if is_pitch_mode and research_brief:
        research_placeholder.empty()  # clear the interim Sonnet text
        with st.spinner("Writing pitches…"):
            opus_prompt = (
                "Here is research gathered from my bookmark corpus:\n\n"
                f"{research_brief}\n\n"
                "Now write the pitches."
            )
            try:
                opus_response = client.messages.create(
                    model="claude-opus-4-5",
                    max_tokens=2048,
                    system=OPUS_PITCH_SYSTEM,
                    messages=[{"role": "user", "content": opus_prompt}],
                )
                final_text = opus_response.content[0].text
            except Exception as e:
                # Fall back to Sonnet output if Opus fails
                final_text = research_brief
                st.warning(f"Opus unavailable ({e}), showing Sonnet draft instead.")

    # ── Render output ─────────────────────────────────────────────────────────
    if final_text:
        st.markdown("---")
        st.markdown(final_text)

        # Export button
        date_str = datetime.now().strftime("%Y-%m-%d")
        st.download_button(
            "⬇ Download as Markdown",
            data=final_text,
            file_name=f"pitches-{date_str}.md" if is_pitch_mode else f"x-signals-{date_str}.md",
            mime="text/markdown",
        )

        # Save + Telegram notification (pitch mode only)
        if is_pitch_mode:
            save_pitches(final_text)
            if notify_mode:
                tg_msg = format_pitch_telegram(final_text, date_str)
                send_telegram(tg_msg)
                st.success("✅ Pitches saved and sent to Telegram.")
            else:
                st.caption(f"💾 Saved to pitches/{date_str}.md")

    # Show SQL queries in collapsible
    if query_log:
        with st.expander(f"📊 {len(query_log)} database queries run"):
            for i, sql in enumerate(query_log, 1):
                st.code(sql, language="sql")


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="x-signals",
        page_icon="🐦",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    load_env()

    if not DB_PATH.exists():
        # First-run onboarding
        st.title("👋 Welcome to x-signals")
        st.markdown(
            "Your database hasn't been set up yet. "
            "Run the setup script to get started:"
        )
        st.code("./setup.sh", language="bash")
        st.markdown("**Or manually:**")
        st.code(
            "cp .env.example .env   # fill in your keys\n"
            "python3 sync_bookmarks.py --full\n"
            "python3 enrich.py\n"
            "python3 cluster.py\n"
            "streamlit run app.py",
            language="bash",
        )
        missing = [k for k in ("ANTHROPIC_API_KEY", "TWITTER_AUTH_TOKEN", "TWITTER_CT0")
                   if not os.environ.get(k)]
        if missing:
            st.warning(f"Missing in .env: {', '.join(missing)}")
        return

    # Check enrichment status
    enriched_count = run_query("SELECT COUNT(*) as n FROM bookmarks WHERE enriched_at IS NOT NULL")
    total_count = run_query("SELECT COUNT(*) as n FROM bookmarks")
    enriched_n = enriched_count[0]["n"] if enriched_count else 0
    total_n = total_count[0]["n"] if total_count else 0

    if enriched_n < total_n * 0.5:
        st.warning(
            f"Only {enriched_n}/{total_n} bookmarks are enriched. "
            f"Run `python3 enrich.py` for best results. "
            f"Writing Assistant will work but with limited data."
        )

    # Navigation
    tab1, tab2, tab3 = st.tabs(["✍️ Writing Assistant", "🕸️ Narrative Graph", "🔍 Browse"])

    with tab1:
        writing_assistant_tab()
    with tab2:
        narrative_graph_tab()
    with tab3:
        browse_tab()


if __name__ == "__main__":
    main()
