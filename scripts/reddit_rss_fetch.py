"""
Fetch subreddit RSS feeds listed in watchlists/subreddit_feeds.json and
store new posts in data/commentary.db, deduplicated by post link.

Usage:
  python reddit_rss_fetch.py
"""
import html
import json
import logging
import re
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

import feedparser
import requests

USER_AGENT = "commentary-tracker-reddit-rss/0.1 (personal research script, not affiliated with Reddit)"
DELAY_SECONDS = 4  # pause between feeds so we don't hammer Reddit's servers
MAX_RETRIES = 3  # extra attempts specifically for 429 (rate-limited) responses
DEFAULT_RETRY_WAIT = 5  # base seconds to wait if Reddit doesn't tell us how long via Retry-After

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "commentary.db"
LOG_PATH = DATA_DIR / "reddit_rss.log"
WATCHLIST_PATH = ROOT / "watchlists" / "subreddit_feeds.json"

logger = logging.getLogger("reddit_rss_fetch")

TAG_RE = re.compile(r"<[^>]+>")  # matches anything that looks like <...>


def clean_body_html(raw_html: str) -> str:
    """Turn Reddit's RSS 'summary' field into plain post text."""
    # Reddit wraps the real post content between SC_OFF/SC_ON comments, then
    # appends a "submitted by ... [link] [comments]" footer after SC_ON.
    # We only want what's before that footer.
    body = raw_html.split("<!-- SC_ON -->")[0]
    body = TAG_RE.sub(" ", body)      # strip HTML tags like <p>, <div>, <a href=...>
    body = html.unescape(body)        # turn &amp; -> &, &#39; -> ', etc. back into normal text
    return " ".join(body.split())     # collapse repeated whitespace/newlines into single spaces


def clean_author(raw_author: str) -> str:
    """Reddit's RSS gives '/u/username' -- strip the prefix to match reddit_fetch.py's format."""
    return raw_author.removeprefix("/u/")


def init_db() -> sqlite3.Connection:
    """Create data/commentary.db and its table if they don't exist yet, and return a connection."""
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS posts (
            link       TEXT PRIMARY KEY,
            subreddit  TEXT,
            title      TEXT,
            author     TEXT,
            published  TEXT,
            body       TEXT,
            fetched_at TEXT
        )
    """)
    return conn


def save_post(conn: sqlite3.Connection, post: dict) -> bool:
    """Insert one post. Returns True if it was new, False if this link was already stored."""
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO posts (link, subreddit, title, author, published, body, fetched_at)
        VALUES (:link, :subreddit, :title, :author, :published, :body, :fetched_at)
        """,
        post,
    )
    conn.commit()
    return cursor.rowcount == 1


def search_local_posts(keyword: str, after: date | None = None, before: date | None = None,
                        max_results: int | None = None) -> list[dict]:
    """Search already-saved posts in data/commentary.db for a keyword in the
    title or body, optionally restricted to a date range. Returns dicts
    shaped for the dashboard's Reddit section (used in place of the
    PRAW-based live search, which is blocked pending Reddit's approval)."""
    DATA_DIR.mkdir(exist_ok=True)
    pattern = re.compile(r"\b" + re.escape(keyword) + r"\b", re.IGNORECASE)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets us read columns by name, e.g. row["title"]
    rows = conn.execute(
        "SELECT * FROM posts WHERE title LIKE ? OR body LIKE ?",
        (f"%{keyword}%", f"%{keyword}%"),
    ).fetchall()
    conn.close()

    matches = []
    for row in rows:
        # the SQL LIKE above is a fast rough filter; re-check with a
        # word-boundary regex so "AI" doesn't match inside "said" or "paid"
        if not pattern.search(row["title"] or "") and not pattern.search(row["body"] or ""):
            continue

        published_date = datetime.fromisoformat(row["published"]).date() if row["published"] else None
        if after and published_date and published_date < after:
            continue
        if before and published_date and published_date > before:
            continue

        matches.append({
            "title": row["title"],
            "subreddit": row["subreddit"],
            "author": row["author"],
            "published_date": published_date.isoformat() if published_date else None,
            "url": row["link"],
            "selftext": row["body"],
            "score": None,        # not available via RSS, only the official API
            "top_comments": [],   # ditto -- RSS gives posts, not comment trees
        })

    matches.sort(key=lambda x: x["published_date"] or "", reverse=True)
    return matches[:max_results] if max_results else matches


def setup_logging():
    """Send log messages to both the screen and data/reddit_rss.log."""
    DATA_DIR.mkdir(exist_ok=True)
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


def load_watchlist() -> list[dict]:
    with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_feed(feed_url: str) -> feedparser.FeedParserDict:
    headers = {"User-Agent": USER_AGENT}

    for attempt in range(1, MAX_RETRIES + 2):  # e.g. MAX_RETRIES=3 -> attempts 1,2,3,4
        response = requests.get(feed_url, headers=headers, timeout=10)

        if response.status_code == 429 and attempt <= MAX_RETRIES:
            # exponential backoff when Reddit doesn't tell us a wait time itself:
            # attempt 1 -> 5s, attempt 2 -> 10s, attempt 3 -> 20s
            backoff = DEFAULT_RETRY_WAIT * (2 ** (attempt - 1))
            wait = float(response.headers.get("Retry-After", backoff))
            logger.warning(
                f"429 from {feed_url} -- waiting {wait:.0f}s before retry "
                f"({attempt}/{MAX_RETRIES})"
            )
            time.sleep(wait)
            continue  # go around the loop and try again

        break  # got something other than a retryable 429 -- stop looping

    response.raise_for_status()  # raise an exception if we still have a 4xx/5xx status
    parsed = feedparser.parse(response.content)
    if parsed.bozo and not parsed.entries:
        # bozo means feedparser had trouble parsing this as a feed at all;
        # if it also found zero entries, treat it as a real failure rather
        # than silently reporting "0 posts" for a feed that's actually broken
        raise ValueError(f"unparseable feed ({parsed.bozo_exception})")
    return parsed


def process_feed(conn: sqlite3.Connection, subreddit: str, feed_url: str) -> tuple[int, int]:
    """Fetch one subreddit's feed and store its posts. Returns (posts_seen, posts_new)."""
    parsed = fetch_feed(feed_url)
    new_count = 0
    for entry in parsed.entries:
        post = {
            "link": entry.get("link"),
            "subreddit": subreddit,
            "title": entry.get("title"),
            "author": clean_author(entry.get("author", "")),
            "published": entry.get("published"),
            "body": clean_body_html(entry.get("summary", "")),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        if save_post(conn, post):
            new_count += 1
    return len(parsed.entries), new_count


def main():
    setup_logging()
    watchlist = load_watchlist()
    conn = init_db()

    total_seen = 0
    total_new = 0
    error_count = 0

    logger.info(f"Starting run: {len(watchlist)} feed(s) in watchlist")

    for i, entry in enumerate(watchlist):
        subreddit = entry["subreddit"]
        feed_url = entry["feed"]
        try:
            seen, new = process_feed(conn, subreddit, feed_url)
            total_seen += seen
            total_new += new
            logger.info(f"r/{subreddit}: {seen} fetched, {new} new")
        except Exception as e:
            error_count += 1
            logger.error(f"r/{subreddit}: FAILED -- {e}")

        if i < len(watchlist) - 1:  # no need to sleep after the very last feed
            time.sleep(DELAY_SECONDS)

    conn.close()
    logger.info(
        f"Run complete: {len(watchlist)} feeds checked, {total_seen} posts fetched, "
        f"{total_new} new posts saved, {error_count} error(s)"
    )


if __name__ == "__main__":
    main()
