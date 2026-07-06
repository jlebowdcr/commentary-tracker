# commentary-tracker

Pulls expert / management commentary from YouTube, podcasts, Reddit, and X,
and hands it to the `commentary-tracker` agent (`~/.claude/agents/commentary-tracker.md`)
to synthesize into a sourced research note.

Each connector is a standalone script under `scripts/`. They only use each
platform's official API (or public RSS, for podcasts) — no login-wall
scraping, so nothing here violates platform ToS. Every script writes a JSON
result to `output/` and prints it to stdout.

**Note for Reddit API reviewers:** `scripts/reddit_fetch.py` is the Reddit
piece of this project. It's one of four independent, read-only connectors
(YouTube, podcasts, Reddit, X) that make up a broader research tool, not a
standalone "Reddit bot" — the other three scripts don't touch Reddit's API
and are included here only because they share the same output format and
downstream agent. The Reddit connector only searches public posts/comments
via PRAW (official API) for a small set of named subreddits and never
posts, votes, follows, or messages — see `reddit_fetch.py` for the full
implementation.

## Setup

```powershell
cd C:\Users\Julia\commentary-tracker
pip install -r requirements.txt
copy .env.example .env
# then edit .env and fill in the keys below
```

## Getting API access

| Platform | Where to get it | Cost / limits | Status |
|---|---|---|---|
| YouTube | [Google Cloud Console](https://console.cloud.google.com/) → enable "YouTube Data API v3" → Credentials → API key | Free, ~10k quota units/day | **configured** |
| Podcasts | No signup — reads public RSS feeds directly | n/a | **configured** (works with no key) |
| Reddit | As of June 2026, Reddit's **Responsible Builder Policy** ended self-serve app creation — `reddit.com/prefs/apps` no longer issues credentials on demand. You must request and receive explicit approval first; see [the policy page](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy) (requires being logged into Reddit) for the current request path. Note the policy also restricts using Reddit data for ML/AI purposes without separate written approval — worth confirming this use case is covered before relying on it. | Free once approved, rate-limited | **pending approval** |
| X / Twitter | [developer.x.com](https://developer.x.com/) → create a project & app → generate a **Bearer Token** | No free tier for new developers since Feb 2026 — pay-per-use: $0.005/post read (capped 2M reads/mo), $0.015–$0.20/post created, card on file required | **on hold** |
| Podcast transcription (optional) | `OPENAI_API_KEY` from [platform.openai.com](https://platform.openai.com/) for hosted Whisper, **or** `pip install openai-whisper` (+ ffmpeg) to transcribe locally for free | OpenAI charges per audio minute; local is free but slow on CPU | not set up |

You don't need all four wired up before this is useful — the agent will
just skip sources whose keys are missing and say so in the note.

## Manual usage (without the agent)

```powershell
python scripts\youtube_fetch.py --channel "UCxxxxxxxx" --max 5
python scripts\podcast_fetch.py --feed "https://feeds.example.com/show.rss" --max 3 --no-transcribe
python scripts\reddit_fetch.py --query "Acme Corp guidance" --subreddit investing --max 10
python scripts\twitter_fetch.py --query "from:someexec" --max 20
```

## Output shape

Every script emits the same envelope so the agent can merge results:

```json
{
  "source": "youtube",
  "query": "...",
  "fetched_at": "2026-07-06T00:00:00+00:00",
  "item_count": 3,
  "items": [ ... ]
}
```

Raw JSON also lands in `output/<source>_<query>_<timestamp>.json` for audit trail.
