"""
Fetch recent episodes from a podcast RSS feed and transcribe them.

No API key needed to read the feed. Transcription needs one of:
  - OPENAI_API_KEY (uses OpenAI's hosted Whisper transcription API), or
  - a local `openai-whisper` install (falls back automatically if no key set)

Usage:
  python podcast_fetch.py --feed "https://feeds.example.com/show.rss" --max 3
  python podcast_fetch.py --feed-name "Money Stuff" --max 3
  python podcast_fetch.py --list-feeds
"""
import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import feedparser
import requests

from common import emit, load_env, ROOT

MAX_AUDIO_MB_FOR_TRANSCRIPTION = 200
DEFAULT_FEED_LIST = ROOT / "watchlists" / "podcast_feeds.json"


def load_feed_list(path: Path = DEFAULT_FEED_LIST):
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_feed_name(name: str) -> str:
    feeds = load_feed_list()
    for entry in feeds:
        if entry["name"].lower() == name.lower():
            return entry["feed"]
    available = ", ".join(e["name"] for e in feeds)
    sys.exit(f"No feed named {name!r} in {DEFAULT_FEED_LIST}. Available: {available}")


def _extract_audio_url(entry) -> str | None:
    for link in entry.get("links", []):
        if link.get("type", "").startswith("audio"):
            return link.get("href")
    return None


def search_feeds_by_keyword(keyword: str | list[str], after: date | None = None,
                             before: date | None = None, feeds: list | None = None) -> list[dict]:
    """Scan every feed in `feeds` (defaults to the curated watchlist) for
    episodes whose title or summary mention `keyword` (or any of `keyword`
    if given a list -- e.g. a company name alongside its ticker, since
    commentary may use either), within an optional date range. Metadata
    only -- never transcribes."""
    feeds = feeds if feeds is not None else load_feed_list()
    keywords = [keyword] if isinstance(keyword, str) else keyword
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(k) for k in keywords) + r")\b", re.IGNORECASE
    )
    matches = []
    for feed_entry in feeds:
        parsed = feedparser.parse(feed_entry["feed"])
        show_title = parsed.feed.get("title", feed_entry["name"])
        for entry in parsed.entries:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            if not pattern.search(title) and not pattern.search(summary):
                continue

            published_date = None
            if entry.get("published_parsed"):
                published_date = datetime(*entry["published_parsed"][:6]).date()
            if after and published_date and published_date < after:
                continue
            if before and published_date and published_date > before:
                continue

            matches.append({
                "show": show_title,
                "title": title,
                "published_at": entry.get("published"),
                "published_date": published_date.isoformat() if published_date else None,
                "audio_url": _extract_audio_url(entry),
                "episode_link": entry.get("link"),
                "summary": summary,
            })
    return matches


def transcribe_openai(audio_path: Path) -> str | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    with open(audio_path, "rb") as f:
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": f},
            data={"model": "whisper-1"},
            timeout=600,
        )
    if resp.status_code != 200:
        return None
    return resp.json().get("text")


_whisper_model = None


def transcribe_local_whisper(audio_path: Path) -> str | None:
    global _whisper_model
    try:
        import whisper
    except ImportError:
        return None
    if _whisper_model is None:
        _whisper_model = whisper.load_model(os.environ.get("WHISPER_MODEL", "base"))
    result = _whisper_model.transcribe(str(audio_path))
    return result.get("text")


def transcribe(audio_url: str) -> str | None:
    with tempfile.TemporaryDirectory() as tmp:
        audio_path = Path(tmp) / "episode.mp3"
        resp = requests.get(audio_url, stream=True, timeout=120)
        resp.raise_for_status()
        size = 0
        with open(audio_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                size += len(chunk)
                if size > MAX_AUDIO_MB_FOR_TRANSCRIPTION * 1_000_000:
                    return None
                f.write(chunk)
        text = transcribe_openai(audio_path)
        if text is None:
            text = transcribe_local_whisper(audio_path)
        return text


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", help="Podcast RSS feed URL")
    parser.add_argument("--feed-name", help="Look up a feed by name in watchlists/podcast_feeds.json")
    parser.add_argument("--search", help="Keyword to search across all curated feeds (metadata only, no transcription)")
    parser.add_argument("--after", help="With --search: only episodes published after this date (YYYY-MM-DD)")
    parser.add_argument("--before", help="With --search: only episodes published before this date (YYYY-MM-DD)")
    parser.add_argument("--list-feeds", action="store_true",
                         help="Print known feed names from watchlists/podcast_feeds.json and exit")
    parser.add_argument("--max", type=int, default=3, help="Max episodes to fetch (default 3)")
    parser.add_argument("--no-transcribe", action="store_true",
                         help="Skip audio transcription, just list episode metadata")
    args = parser.parse_args()

    if args.list_feeds:
        for entry in load_feed_list():
            print(f"{entry['name']}: {entry['description']}")
        return

    if args.search:
        after = _parse_date(args.after) if args.after else None
        before = _parse_date(args.before) if args.before else None
        matches = search_feeds_by_keyword(args.search, after=after, before=before)
        emit("podcast_search", args.search, matches)
        return

    if not args.feed and not args.feed_name:
        sys.exit("Provide --feed, --feed-name, --search, or --list-feeds")

    feed_url = args.feed or resolve_feed_name(args.feed_name)

    load_env()
    parsed = feedparser.parse(feed_url)

    items = []
    for entry in parsed.entries[: args.max]:
        audio_url = _extract_audio_url(entry)

        transcript = None
        if audio_url and not args.no_transcribe:
            transcript = transcribe(audio_url)

        items.append({
            "title": entry.get("title"),
            "published_at": entry.get("published"),
            "audio_url": audio_url,
            "episode_link": entry.get("link"),
            "summary": entry.get("summary"),
            "transcript": transcript,
            "transcript_available": transcript is not None,
        })

    show_title = parsed.feed.get("title", feed_url)
    emit("podcast", show_title, items)


if __name__ == "__main__":
    main()
