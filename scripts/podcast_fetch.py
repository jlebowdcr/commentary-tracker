"""
Fetch recent episodes from a podcast RSS feed and transcribe them.

No API key needed to read the feed. Transcription needs one of:
  - OPENAI_API_KEY (uses OpenAI's hosted Whisper transcription API), or
  - a local `openai-whisper` install (falls back automatically if no key set)

Usage:
  python podcast_fetch.py --feed "https://feeds.example.com/show.rss" --max 3
"""
import argparse
import os
import tempfile
from pathlib import Path

import feedparser
import requests

from common import emit, load_env

MAX_AUDIO_MB_FOR_TRANSCRIPTION = 200


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


def transcribe_local_whisper(audio_path: Path) -> str | None:
    try:
        import whisper
    except ImportError:
        return None
    model = whisper.load_model("base")
    result = model.transcribe(str(audio_path))
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", required=True, help="Podcast RSS feed URL")
    parser.add_argument("--max", type=int, default=3, help="Max episodes to fetch (default 3)")
    parser.add_argument("--no-transcribe", action="store_true",
                         help="Skip audio transcription, just list episode metadata")
    args = parser.parse_args()

    load_env()
    parsed = feedparser.parse(args.feed)

    items = []
    for entry in parsed.entries[: args.max]:
        audio_url = None
        for link in entry.get("links", []):
            if link.get("type", "").startswith("audio"):
                audio_url = link.get("href")
                break

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

    show_title = parsed.feed.get("title", args.feed)
    emit("podcast", show_title, items)


if __name__ == "__main__":
    main()
