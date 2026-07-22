"""
Fetch recent videos for a channel, search query, or a saved channel watchlist,
and pull their transcripts (any available language, translated to English
when possible).

Requires: YOUTUBE_API_KEY (Google Cloud Console -> YouTube Data API v3)
Transcripts use youtube-transcript-api and need no key, but only work when
captions (auto-generated or manual, in any language) exist for the video.

Usage:
  python youtube_fetch.py --channel "UCxxxxxxxx" --max 5
  python youtube_fetch.py --search "Jane Doe CEO interview" --max 5
  python youtube_fetch.py --search "MELI" --after 2026-01-01 --before 2026-07-01 --max 80
  python youtube_fetch.py --watchlist --max 5
  python youtube_fetch.py --watchlist watchlists/youtube_channels.json --max 5
"""
import argparse
import html
import json
import os
import sys
import time
from pathlib import Path

import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

from common import emit, require_env, ROOT

API_URL = "https://www.googleapis.com/youtube/v3"
DEFAULT_WATCHLIST = ROOT / "watchlists" / "youtube_channels.json"
MAX_TOTAL_RESULTS = 200  # hard cap so a large --max can't blow the daily API quota
MAX_RETRIES = 3


def _to_rfc3339(date_str: str, end_of_day: bool = False) -> str:
    suffix = "T23:59:59Z" if end_of_day else "T00:00:00Z"
    return f"{date_str}{suffix}"


def _get_with_retry(url, params):
    """The YouTube search endpoint intermittently returns a transient 403
    ('accountDelegationForbidden') that has nothing to do with the request
    itself -- retrying the identical call typically succeeds. Only retry on
    that specific transient signature or a 5xx; anything else (quota,
    bad key, bad params) fails immediately."""
    last_exc = None
    for attempt in range(MAX_RETRIES):
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 200:
            return resp
        transient = resp.status_code >= 500 or (
            resp.status_code == 403 and "accountDelegationForbidden" in resp.text
        )
        if not transient or attempt == MAX_RETRIES - 1:
            resp.raise_for_status()
        time.sleep(1.5 * (attempt + 1))
        last_exc = resp
    last_exc.raise_for_status()


def search_videos(api_key, channel_id=None, query=None, max_results=5, after=None, before=None):
    """Paginates through the YouTube search endpoint until max_results is reached."""
    max_results = min(max_results, MAX_TOTAL_RESULTS)
    items = []
    page_token = None
    while len(items) < max_results:
        params = {
            "key": api_key,
            "part": "snippet",
            "type": "video",
            "order": "date",
            "maxResults": min(50, max_results - len(items)),
        }
        if channel_id:
            params["channelId"] = channel_id
        if query:
            params["q"] = query
        if after:
            params["publishedAfter"] = _to_rfc3339(after)
        if before:
            params["publishedBefore"] = _to_rfc3339(before, end_of_day=True)
        if page_token:
            params["pageToken"] = page_token

        resp = _get_with_retry(f"{API_URL}/search", params)
        payload = resp.json()
        items.extend(payload.get("items", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return items[:max_results]


def fetch_transcript(video_id: str):
    """Returns (text, language_code, translated_to_en_text, error_reason).

    Prefers an English transcript if one exists; otherwise fetches whatever
    language is available and attempts an English machine translation on
    top of it when the transcript source supports translation.
    """
    try:
        transcript_list = YouTubeTranscriptApi().list(video_id)
    except (TranscriptsDisabled, NoTranscriptFound) as e:
        return None, None, None, type(e).__name__
    except Exception as e:
        return None, None, None, f"{type(e).__name__}: {e}"

    try:
        transcript = transcript_list.find_transcript(["en"])
    except NoTranscriptFound:
        transcript = next(iter(transcript_list), None)
        if transcript is None:
            return None, None, None, "NoTranscriptFound"

    language_code = transcript.language_code
    try:
        fetched = transcript.fetch()
        text = " ".join(s.text for s in fetched)
    except Exception as e:
        return None, None, None, f"{type(e).__name__}: {e}"

    translated_text = None
    if language_code != "en" and transcript.is_translatable:
        try:
            translated = transcript.translate("en").fetch()
            translated_text = " ".join(s.text for s in translated)
        except Exception:
            pass  # translation is best-effort; original-language transcript still stands

    return text, language_code, translated_text, None


def fetch_video_stats(api_key, video_ids: list[str]) -> dict:
    """search.list doesn't return view/like counts -- that needs a separate
    videos.list call (cheap: 1 quota unit regardless of batch size, vs 100
    for a search call). Batches in groups of 50 (API max per request)."""
    stats = {}
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        resp = _get_with_retry(f"{API_URL}/videos", {
            "key": api_key,
            "part": "statistics",
            "id": ",".join(batch),
        })
        for item in resp.json().get("items", []):
            s = item.get("statistics", {})
            stats[item["id"]] = {
                "view_count": int(s["viewCount"]) if "viewCount" in s else None,
                "like_count": int(s["likeCount"]) if "likeCount" in s else None,
                "comment_count": int(s["commentCount"]) if "commentCount" in s else None,
            }
    return stats


def build_video_item(video: dict, stats: dict | None = None, watchlist_name: str | None = None) -> dict:
    """Turns one raw search-result item into our standard item dict, fetching
    its transcript. The YouTube API returns title/description as HTML-entity
    -encoded text (e.g. "America&#39;s") -- html.unescape() cleans that up
    so it displays correctly everywhere (dashboard, markdown export, JSON)."""
    video_id = video["id"]["videoId"]
    snippet = video["snippet"]
    text, lang, translated, error = fetch_transcript(video_id)
    video_stats = (stats or {}).get(video_id, {})
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "title": html.unescape(snippet.get("title", "")),
        "channel_title": html.unescape(snippet.get("channelTitle", "")),
        "watchlist_name": watchlist_name,
        "published_at": snippet.get("publishedAt"),
        "description": html.unescape(snippet.get("description", "")),
        "view_count": video_stats.get("view_count"),
        "like_count": video_stats.get("like_count"),
        "comment_count": video_stats.get("comment_count"),
        "transcript": text,
        "transcript_language": lang,
        "transcript_translated_en": translated,
        "transcript_available": text is not None,
        "transcript_error": error,
    }


def fetch_channel(api_key, channel_id, channel_name, max_results, after, before):
    raw_videos = search_videos(api_key, channel_id=channel_id, max_results=max_results,
                                after=after, before=before)
    stats = fetch_video_stats(api_key, [v["id"]["videoId"] for v in raw_videos])
    return [build_video_item(v, stats, channel_name) for v in raw_videos]


def load_watchlist(path: Path):
    if not path.exists():
        sys.exit(f"Watchlist file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", help="YouTube channel ID")
    parser.add_argument("--search", help="Free-text search query")
    parser.add_argument("--watchlist", nargs="?", const=str(DEFAULT_WATCHLIST),
                         help=f"Pull latest videos for every channel in a watchlist JSON file "
                              f"(default: {DEFAULT_WATCHLIST})")
    parser.add_argument("--max", type=int, default=5,
                         help="Max videos per channel/query (default 5, hard cap "
                              f"{MAX_TOTAL_RESULTS})")
    parser.add_argument("--after", help="Only videos published after this date (YYYY-MM-DD)")
    parser.add_argument("--before", help="Only videos published before this date (YYYY-MM-DD)")
    args = parser.parse_args()

    if not args.channel and not args.search and not args.watchlist:
        sys.exit("Provide --channel, --search, or --watchlist")

    require_env("YOUTUBE_API_KEY")
    api_key = os.environ["YOUTUBE_API_KEY"]

    if args.watchlist:
        channels = load_watchlist(Path(args.watchlist))
        items = []
        for entry in channels:
            items.extend(fetch_channel(api_key, entry["channel_id"], entry["name"],
                                        args.max, args.after, args.before))
        query_label = f"watchlist_{len(channels)}_channels"
        emit("youtube", query_label, items)
        return

    if args.channel:
        items = fetch_channel(api_key, args.channel, None, args.max, args.after, args.before)
        query_label = args.channel
    else:
        raw_videos = search_videos(api_key, query=args.search, max_results=args.max,
                                    after=args.after, before=args.before)
        stats = fetch_video_stats(api_key, [v["id"]["videoId"] for v in raw_videos])
        items = [build_video_item(v, stats) for v in raw_videos]
        query_label = args.search

    emit("youtube", query_label, items)


if __name__ == "__main__":
    main()
