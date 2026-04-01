#!/usr/bin/env python3
"""
x-signals: Knowledge graph and writing assistant for Twitter bookmarks.

Run with: streamlit run app.py
"""

import json
import os
import sqlite3
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

You have access to a `query_db` tool that queries her personal research corpus: 5,000+ Twitter bookmarks spanning Dec 2022–Mar 2026, enriched with topic tags, content types, summaries, and core claims.

Schema:
- bookmarks(id, author_handle, author_name, text, quoted_tweet_text, created_at, topics [JSON], subtopics [JSON], content_type, authority, summary, core_claim, position, entities [JSON], likes, views, urls [JSON])
- topics/subtopics/entities are stored as JSON strings — use json_each() to filter by them
- created_at is ISO 8601

When answering:
- Always query the corpus for evidence before drawing conclusions
- Cite specific tweets by author_handle and summary/core_claim
- Distinguish between expert opinion, primary sources, and community views
- Be direct about what the corpus does and doesn't contain
- For writing pitches, be specific: a real thesis, not just a topic area"""

WHAT_TO_WRITE_PROMPT = """Analyze my recent bookmarks (last 60 days) to surface what I should write next.

For each pitch, tell me:
1. **The thesis** — a specific, arguable claim, not just a topic
2. **Why now** — what in recent bookmarks signals this is the right moment (a debate reaching inflection, threads converging, regulatory event on horizon, gap in coverage)
3. **Venue** — recommend one: law review companion (novel legal argument, doctrinal depth needed), Tech Policy Press/Lawfare (timely policy analysis, 1500-3000 words, practitioner audience), Substack (nuanced take for engaged followers), or Thread (fastest-moving narrative, insert into active debate)
4. **Key sources** — 3-4 specific bookmarks from the corpus that would anchor the piece

Surface 4-5 pitches. Focus on arguments that are timely, that I'm positioned to make given what I've been tracking, and that haven't already been written to death."""


def load_env():
    if ENV_PATH.exists():
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ[key.strip()] = value.strip()


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

    # Agentic loop with query_db tool
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
            "Limit results to avoid overwhelming the context."
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

    messages = [{"role": "user", "content": user_input}]

    with st.spinner("Researching your corpus…"):
        response_placeholder = st.empty()
        query_log = []
        final_text = ""

        for iteration in range(10):  # max agentic iterations
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=4096,
                system=WRITING_ASSISTANT_SYSTEM,
                tools=[query_db_tool],
                messages=messages,
            )

            # Process response content
            tool_calls = []
            text_blocks = []
            for block in response.content:
                if block.type == "text":
                    text_blocks.append(block.text)
                elif block.type == "tool_use":
                    tool_calls.append(block)

            if text_blocks:
                final_text = "\n\n".join(text_blocks)
                response_placeholder.markdown(final_text)

            if response.stop_reason == "end_turn" or not tool_calls:
                break

            # Execute tool calls
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for tool_call in tool_calls:
                sql = tool_call.input.get("sql", "")
                query_log.append(sql)

                if not sql.strip().upper().startswith("SELECT"):
                    result_content = json.dumps({"error": "Only SELECT queries are allowed."})
                else:
                    try:
                        rows = run_query(sql)
                        # Limit payload size
                        result_content = json.dumps(rows[:50], default=str)
                    except Exception as e:
                        result_content = json.dumps({"error": str(e)})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": result_content,
                })

            messages.append({"role": "user", "content": tool_results})

    # Show final response
    if final_text:
        st.markdown("---")
        st.markdown(final_text)

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
        st.error(
            "Database not found. Run: `python3 db.py` to initialise, then `python3 enrich.py` to enrich."
        )
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
    tab1, tab2 = st.tabs(["✍️ Writing Assistant", "🔍 Browse"])

    with tab1:
        writing_assistant_tab()
    with tab2:
        browse_tab()


if __name__ == "__main__":
    main()
