"""
Fetch recent videos for a channel or search query and pull their transcripts.

Requires: YOUTUBE_API_KEY (Google Cloud Console -> YouTube Data API v3)
Transcripts use youtube-transcript-api and need no key, but only work when
captions (auto-generated or manual) exist for the video.

Usage:
  python youtube_fetch.py --channel "UCxxxxxxxx" --max 5
  python youtube_fetch.py --search "Jane Doe CEO interview" --max 5
"""
import argparse
import sys

import requests
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import TranscriptsDisabled, NoTranscriptFound

from common import emit, require_env

API_URL = "https://www.googleapis.com/youtube/v3"


def search_videos(api_key: str, channel_id: str | None, query: str | None, max_results: int):
    params = {
        "key": api_key,
        "part": "snippet",
        "type": "video",
        "order": "date",
        "maxResults": max_results,
    }
    if channel_id:
        params["channelId"] = channel_id
    if query:
        params["q"] = query
    resp = requests.get(f"{API_URL}/search", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("items", [])


def fetch_transcript(video_id: str) -> tuple[str | None, str | None]:
    """Returns (transcript_text, error_reason). Exactly one is None."""
    try:
        transcript = YouTubeTranscriptApi().fetch(video_id)
    except (TranscriptsDisabled, NoTranscriptFound) as e:
        return None, type(e).__name__
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    return " ".join(snippet.text for snippet in transcript), None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", help="YouTube channel ID")
    parser.add_argument("--search", help="Free-text search query")
    parser.add_argument("--max", type=int, default=5, help="Max videos to fetch (default 5)")
    args = parser.parse_args()

    if not args.channel and not args.search:
        sys.exit("Provide --channel or --search")

    require_env("YOUTUBE_API_KEY")
    import os
    api_key = os.environ["YOUTUBE_API_KEY"]

    raw_videos = search_videos(api_key, args.channel, args.search, args.max)

    items = []
    for v in raw_videos:
        video_id = v["id"]["videoId"]
        snippet = v["snippet"]
        transcript, error_reason = fetch_transcript(video_id)
        items.append({
            "video_id": video_id,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "title": snippet.get("title"),
            "channel_title": snippet.get("channelTitle"),
            "published_at": snippet.get("publishedAt"),
            "description": snippet.get("description"),
            "transcript": transcript,
            "transcript_available": transcript is not None,
            "transcript_error": error_reason,
        })

    query_label = args.channel or args.search
    emit("youtube", query_label, items)


if __name__ == "__main__":
    main()
