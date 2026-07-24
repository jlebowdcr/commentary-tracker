"""
Commentary-tracker dashboard: enter a company + date range, get back staged
raw results (YouTube transcripts + matching podcast episodes) ready to hand
to a synthesis pass afterward. No LLM calls happen here -- this only
gathers and organizes data.

Run with:
  python app.py
Then open http://127.0.0.1:5050
"""
import json
import os
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import podcast_fetch  # noqa: E402
import reddit_rss_fetch  # noqa: E402
import twitter_fetch  # noqa: E402
import youtube_fetch  # noqa: E402

import analytics

podcast_fetch.load_env()  # load .env once at startup, not just inside /search --
                          # auth (below) needs it available for every route

app = Flask(__name__)


@app.before_request
def require_auth():
    """Basic-auth gate for shared/hosted deployments. Only activates when
    DASHBOARD_USERNAME/PASSWORD are actually set (e.g. on Render) -- local
    runs with no .env entry for these stay wide open, same as before."""
    expected_user = os.environ.get("DASHBOARD_USERNAME")
    expected_pass = os.environ.get("DASHBOARD_PASSWORD")
    if not expected_user or not expected_pass:
        return  # auth not configured -- don't lock anyone out of a local dev run

    auth = request.authorization
    valid = (
        auth is not None
        and secrets.compare_digest(auth.username or "", expected_user)
        and secrets.compare_digest(auth.password or "", expected_pass)
    )
    if not valid:
        return Response(
            "Authentication required", 401,
            {"WWW-Authenticate": 'Basic realm="Commentary Tracker"'},
        )


@app.template_filter("format_count")
def format_count(n):
    if n is None:
        return "n/a"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)

MAX_YOUTUBE_RESULTS = 12
MAX_REDDIT_RESULTS = 15
MAX_TWITTER_RESULTS = 20
TOP_N = 5
MIN_YOUTUBE_VIEWS = 1000

# Single-user local tool: stash the most recent search here for /export.md
# rather than wiring up session/cookie storage for a one-person dashboard.
LAST_RESULTS = {}


def _parse_date(s: str | None):
    return datetime.strptime(s, "%Y-%m-%d").date() if s else None


def _sort_desc(items: list, key):
    """None-safe descending sort -- items missing the metric sort last rather
    than crashing or landing arbitrarily."""
    return sorted(items, key=lambda x: key(x) if key(x) is not None else -1, reverse=True)


def _split_top(items: list, n: int = TOP_N):
    return items[:n], items[n:]


def _within_last_7_days(after_date) -> bool:
    """True only if the range has an explicit start date that's within the
    last 7 days. X's recent-search endpoint has no visibility further back
    than that regardless of what's requested, so an open-ended or older
    start date can't be trusted to return a complete picture."""
    if after_date is None:
        return False
    return after_date >= datetime.now().date() - timedelta(days=7)


def _first_nonempty(item: dict, keys: list[str]) -> str:
    for k in keys:
        if item.get(k):
            return item[k]
    return ""


def _is_english(language: str | None) -> bool:
    """True if unknown (fail open -- don't drop unlabeled videos) or if the
    language code starts with 'en' (covers en, en-US, en-GB, en-IN, ...)."""
    return language is None or language.lower().startswith("en")


def _load_youtube_watchlist() -> list[dict]:
    """Unlike youtube_fetch.load_watchlist() (which sys.exit()s if the file
    is missing -- fine for a CLI, fatal for a running web server), this
    fails open to an empty list so a missing/malformed watchlist file just
    means no curated channels get checked, not a crashed dashboard."""
    try:
        return json.loads(youtube_fetch.DEFAULT_WATCHLIST.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/autocomplete")
def autocomplete():
    query = (request.args.get("q") or "").strip()
    if len(query) < 2:  # skip Yahoo call for 0-1 char queries -- too noisy, too fast to matter
        return jsonify([])
    return jsonify(analytics.search_companies(query, limit=8))


@app.route("/search", methods=["POST"])
def search():
    company = request.form["company"].strip()
    after = request.form.get("after") or None
    before = request.form.get("before") or None

    # Resolve once, up front: commentary might refer to a company by its
    # ticker ("SPCX") or its actual name ("SpaceX"), and text search across
    # every source below only catches whichever one is literally in the
    # text. Searching every resolvable variant closes that gap -- e.g. a
    # video titled "SpaceX Stock Falls..." never had the string "SPCX" in
    # it anywhere, so a search for just "SPCX" could never have found it.
    resolved = analytics.resolve_company(company)
    ticker = resolved["ticker"] if resolved else None
    search_terms = list(dict.fromkeys(  # dedupe, keep first-seen order
        [company] + ([resolved["name"]] if resolved else []) + ([ticker] if ticker else [])
    ))

    youtube_items = []
    youtube_error = None
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        youtube_error = "YOUTUBE_API_KEY not configured in .env"
    else:
        try:
            # A bare company name/ticker is often ambiguous (e.g. "MELI" is a
            # common word/name in Indonesian and Malay) and returns unrelated
            # results. Biasing the query toward finance content fixes this --
            # verified: "MELI" alone returned ~0/12 relevant videos, "MELI
            # stock" returned 7/8 relevant.
            # Run one search per resolved term (ticker, name, and whatever was
            # typed) and merge by video ID, since a video may use only one of
            # those strings in its title/description.
            videos_by_id = {}
            for term in search_terms:
                raw_videos = youtube_fetch.search_videos(
                    api_key, query=f"{term} stock", max_results=MAX_YOUTUBE_RESULTS,
                    after=after, before=before
                )
                for v in raw_videos:
                    videos_by_id[v["id"]["videoId"]] = v

            # Also check each curated channel (watchlists/youtube_channels.json)
            # directly: a video from a trusted source like Bloomberg or CNBC
            # might not rank highly enough in the general web-wide search to
            # place in the top MAX_YOUTUBE_RESULTS, even though it's exactly
            # the kind of source this dashboard is built to surface. Scoped to
            # the primary term only (not every resolved variant) to keep
            # quota cost bounded -- each channel search is its own 100-unit
            # API call, and this runs once per channel on every dashboard search.
            primary_term = search_terms[0]
            for channel in _load_youtube_watchlist():
                channel_videos = youtube_fetch.search_videos(
                    api_key, channel_id=channel["channel_id"], query=f"{primary_term} stock",
                    max_results=5, after=after, before=before
                )
                for v in channel_videos:
                    videos_by_id[v["id"]["videoId"]] = v

            raw_videos = list(videos_by_id.values())
            stats = youtube_fetch.fetch_video_stats(api_key, list(videos_by_id.keys()))
            # Drop low-view and non-English videos before fetching transcripts
            # (not after) so we don't pay for a transcript call on something
            # we're about to discard. Language is only checked when YouTube
            # actually tells us one (many uploads don't set it) -- we'd rather
            # keep an unlabeled video than wrongly drop real English content.
            raw_videos = [
                v for v in raw_videos
                if (stats.get(v["id"]["videoId"], {}).get("view_count") or 0) > MIN_YOUTUBE_VIEWS
                and _is_english(stats.get(v["id"]["videoId"], {}).get("language"))
            ]
            youtube_items = [youtube_fetch.build_video_item(v, stats) for v in raw_videos]
            youtube_items = _sort_desc(youtube_items, lambda x: x["view_count"])
        except Exception as e:
            youtube_error = str(e)

    podcast_matches = podcast_fetch.search_feeds_by_keyword(
        search_terms, after=_parse_date(after), before=_parse_date(before)
    )
    # search_feeds_by_keyword returns results grouped by feed (its own iteration
    # order), not merged by date across feeds -- sort explicitly so "sorted by
    # date" actually holds once results from multiple shows are combined.
    podcast_matches = sorted(
        podcast_matches,
        key=lambda x: x["published_date"] or "",
        reverse=True,
    )

    after_date = _parse_date(after)
    before_date = _parse_date(before)

    reddit_items = []
    reddit_status = None
    try:
        # Sourced from data/commentary.db (scripts/reddit_rss_fetch.py), not
        # a live API call -- results are only as fresh as your last run of
        # that script. The PRAW-based live search is blocked pending
        # Reddit's Responsible Builder Policy approval, so this local
        # dataset stands in for it here.
        reddit_items = reddit_rss_fetch.search_local_posts(
            search_terms, after=after_date, before=before_date, max_results=MAX_REDDIT_RESULTS
        )
    except Exception as e:
        reddit_status = f"Error: {e}"

    twitter_items = []
    twitter_status = None
    if not _within_last_7_days(after_date):
        # X's recent-search endpoint silently drops anything older than 7 days,
        # so a range that isn't entirely inside that window would return
        # incomplete or empty results -- skip the call rather than spend
        # budget on something that can't be relevant. Use the "Last 7 days"
        # button to pick a range that qualifies.
        twitter_status = ('X/Twitter needs a date range entirely within the last 7 days '
                           '-- use the "Last 7 days" button above to include it.')
    elif twitter_fetch.has_credentials():
        try:
            # Deliberately NOT looping over search_terms here like the other
            # sources -- each read counts against a real, small paid budget
            # (X_MONTHLY_READ_BUDGET), so we search only what was typed
            # rather than multiplying paid reads per dashboard search.
            raw = twitter_fetch.search_tweets(company, max_results=MAX_TWITTER_RESULTS)
            for t in raw:
                if t.get("created_at"):
                    d = datetime.strptime(t["created_at"][:10], "%Y-%m-%d").date()
                    if after_date and d < after_date:
                        continue
                    if before_date and d > before_date:
                        continue
                twitter_items.append(t)

            def _twitter_popularity(tweet):
                m = tweet.get("metrics") or {}
                if m.get("impression_count") is not None:
                    return m["impression_count"]
                engagement = [m.get(k) for k in
                              ("like_count", "retweet_count", "reply_count", "quote_count")]
                engagement = [v for v in engagement if v is not None]
                return sum(engagement) if engagement else None

            twitter_items = _sort_desc(twitter_items, _twitter_popularity)
        except Exception as e:
            twitter_status = f"Error: {e}"
    else:
        twitter_status = "X API is on hold -- no free tier since Feb 2026 (pay-per-use only)."

    youtube_top, youtube_rest = _split_top(youtube_items)
    podcast_top, podcast_rest = _split_top(podcast_matches)
    reddit_top, reddit_rest = _split_top(reddit_items)
    twitter_top, twitter_rest = _split_top(twitter_items)

    # --- Stock price chart data ---
    # ticker was already resolved up front, alongside search_terms
    stock_prices = analytics.fetch_stock_prices(ticker, after_date, before_date) if ticker else []

    # --- LLM sentiment-summary bullets (top 5 per source only, to bound cost) ---
    summary_sources = {
        "YouTube": [
            {"title": i["title"], "date": (i.get("published_at") or "")[:10],
             "excerpt": _first_nonempty(i, ["transcript", "description"])}
            for i in youtube_top
        ],
        "Podcasts": [
            {"title": i["title"], "date": i.get("published_date"), "excerpt": i.get("summary", "")}
            for i in podcast_top
        ],
        "Reddit": [
            {"title": i["title"], "date": i.get("published_date"),
             "excerpt": _first_nonempty(i, ["selftext"])}
            for i in reddit_top
        ],
        "X/Twitter": [
            {"title": (i.get("text") or "")[:80], "date": (i.get("created_at") or "")[:10],
             "excerpt": i.get("text", "")}
            for i in twitter_top
        ],
    }
    if any(summary_sources.values()):
        summary_bullets = analytics.synthesize_summary(company, after, before, summary_sources)
    else:
        summary_bullets = ["No content found across any source for this company and date range."]

    results = {
        "company": company,
        "after": after,
        "before": before,
        "youtube_top": youtube_top,
        "youtube_rest": youtube_rest,
        "youtube_error": youtube_error,
        "podcast_top": podcast_top,
        "podcast_rest": podcast_rest,
        "reddit_top": reddit_top,
        "reddit_rest": reddit_rest,
        "reddit_status": reddit_status,
        "twitter_top": twitter_top,
        "twitter_rest": twitter_rest,
        "twitter_status": twitter_status,
        "summary_bullets": summary_bullets,
        "ticker": ticker,
        "chart_data": {"prices": stock_prices},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    LAST_RESULTS["data"] = results
    return render_template("results.html", **results)


@app.route("/transcribe", methods=["POST"])
def transcribe_episode():
    audio_url = (request.json or {}).get("audio_url")
    if not audio_url:
        return jsonify({"error": "missing audio_url"}), 400
    try:
        text = podcast_fetch.transcribe(audio_url)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if text is None:
        return jsonify({"error": "Transcription unavailable (no Whisper model installed, or download failed)"}), 502
    return jsonify({"transcript": text})


@app.route("/export.md")
def export_markdown():
    data = LAST_RESULTS.get("data")
    if not data:
        return "No search yet -- run a search first.", 404

    youtube_items = data["youtube_top"] + data["youtube_rest"]
    podcast_items = data["podcast_top"] + data["podcast_rest"]
    reddit_items = data["reddit_top"] + data["reddit_rest"]
    twitter_items = data["twitter_top"] + data["twitter_rest"]

    lines = [f"# {data['company']} — Commentary Staging", ""]
    lines.append(f"- Window: {data['after'] or 'any'} to {data['before'] or 'any'}")
    lines.append(f"- Generated: {data['generated_at']}")
    lines.append("")
    lines.append("## Sentiment Summary")
    lines.append("")
    for bullet in data["summary_bullets"]:
        lines.append(f"- {bullet}")
    lines.append("")
    lines.append("## YouTube")
    lines.append("")
    if data["youtube_error"]:
        lines.append(f"_Error: {data['youtube_error']}_\n")
    if not youtube_items and not data["youtube_error"]:
        lines.append("_No matches._\n")
    for item in youtube_items:
        lines.append(f"### {item['title']}")
        lines.append(f"- Channel: {item['channel_title']}")
        lines.append(f"- Views: {item['view_count'] if item['view_count'] is not None else 'n/a'}")
        lines.append(f"- Published: {item['published_at']}")
        lines.append(f"- Link: {item['url']}")
        if item["transcript"]:
            lines.append(f"\n**Transcript ({item['transcript_language']}):**\n\n{item['transcript']}\n")
            if item["transcript_translated_en"]:
                lines.append(f"**English translation:**\n\n{item['transcript_translated_en']}\n")
        else:
            lines.append(f"\n_No transcript available ({item['transcript_error']})_\n")

    lines.append("## Podcasts")
    lines.append("")
    if not podcast_items:
        lines.append("_No matches._\n")
    for item in podcast_items:
        lines.append(f"### {item['title']} ({item['show']})")
        lines.append(f"- Published: {item['published_at']}")
        if item["episode_link"]:
            lines.append(f"- Link: {item['episode_link']}")
        lines.append(f"\n{item['summary']}\n")
        lines.append("_Transcript not fetched automatically — use the dashboard's Transcribe "
                      "button, or run podcast_fetch.py directly._\n")

    lines.append("## Reddit")
    lines.append("")
    if data["reddit_status"]:
        lines.append(f"_{data['reddit_status']}_\n")
    if not reddit_items and not data["reddit_status"]:
        lines.append("_No matches._\n")
    for item in reddit_items:
        lines.append(f"### {item['title']} (r/{item['subreddit']})")
        lines.append(f"- Author: {item['author']} · Score: {item['score']}")
        lines.append(f"- Published: {item['published_date']}")
        lines.append(f"- Link: {item['url']}")
        if item["selftext"]:
            lines.append(f"\n{item['selftext']}\n")
        for c in item["top_comments"]:
            lines.append(f"> {c['author']} ({c['score']} pts): {c['body']}")
        lines.append("")

    lines.append("## X / Twitter")
    lines.append("")
    if data["twitter_status"]:
        lines.append(f"_{data['twitter_status']}_\n")
    if not twitter_items and not data["twitter_status"]:
        lines.append("_No matches._\n")
    for item in twitter_items:
        lines.append(f"### @{item['author_username']} ({item['author_name']})")
        lines.append(f"- Published: {item['created_at']}")
        lines.append(f"- Metrics: {item['metrics']}")
        lines.append(f"- Link: {item['url']}")
        lines.append(f"\n{item['text']}\n")

    md = "\n".join(lines)
    safe_name = "".join(c if c.isalnum() else "_" for c in data["company"])[:40]
    return Response(
        md,
        mimetype="text/markdown",
        headers={"Content-Disposition": f"attachment; filename={safe_name}_staging.md"},
    )


if __name__ == "__main__":
    # use_reloader=False: the reloader re-execs a child process, and in this
    # environment that child doesn't reliably inherit a PATH modified after
    # the parent shell started -- which matters here since Whisper shells out
    # to ffmpeg. Restart manually after editing app.py instead.
    app.run(debug=True, port=5050, use_reloader=False)
