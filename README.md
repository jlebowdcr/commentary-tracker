# commentary-tracker

Pulls expert / management commentary from YouTube, podcasts, Reddit, and X,
and hands it to the `commentary-tracker` agent (`~/.claude/agents/commentary-tracker.md`)
to synthesize into a sourced research note.

Each connector is a standalone script under `scripts/`. They only use each
platform's official API (or public RSS, for podcasts) — no login-wall
scraping, so nothing here violates platform ToS. Every script writes a JSON
result to `output/` and prints it to stdout.


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
| Reddit | As of June 2026, Reddit's **Responsible Builder Policy** ended self-serve app creation — `reddit.com/prefs/apps` no longer issues credentials on demand. You must request and receive explicit approval first; see [the policy page](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy) (requires being logged into Reddit) for the current request path. Note the policy also restricts using Reddit data for ML/AI purposes without separate written approval — worth confirming this use case is covered before relying on it. | Free once approved, rate-limited | **denied** — application rejected, `reddit_fetch.py` unusable until reapplied/approved |
| X / Twitter | [developer.x.com](https://developer.x.com/) → create a project & app → generate a **Bearer Token** | No free tier for new developers since Feb 2026 — pay-per-use: $0.005/post read (capped 2M reads/mo), $0.015–$0.20/post created, card on file required. `twitter_fetch.py` enforces its own hard monthly read budget on top of that (`X_MONTHLY_READ_BUDGET`, default 500 reads =~ $2.50/mo) so a bad query can't run away — see below. | **available (spend-capped)** |
| Podcast transcription | Local Whisper — `pip install -r requirements-whisper.txt` + ffmpeg (installed via `winget install Gyan.FFmpeg`) | Free, runs on this machine; slow on CPU (a ~40min episode can take a while) | **configured** |

You don't need all four wired up before this is useful — the agent will
just skip sources whose keys are missing and say so in the note.

**ffmpeg PATH note:** winget installs ffmpeg but new shell sessions in this
environment don't always pick up the updated PATH automatically. If
`ffmpeg` isn't found, prepend its bin folder for that command, e.g.:
```powershell
$env:PATH += ";C:\Users\Julia\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.2-full_build\bin"
```

**Whisper model size:** defaults to `base`. Override with the `WHISPER_MODEL`
env var (e.g. `tiny` for faster/rougher, `small`/`medium` for slower/better)
if the default speed/accuracy tradeoff doesn't fit.

## Manual usage (without the agent)

```powershell
# YouTube: single channel, free-text search, date-filtered search, or a saved watchlist
python scripts\youtube_fetch.py --channel "UCxxxxxxxx" --max 5
python scripts\youtube_fetch.py --search "Jane Doe CEO interview" --max 5
python scripts\youtube_fetch.py --search "MELI" --after 2026-01-01 --before 2026-07-01 --max 80
python scripts\youtube_fetch.py --watchlist --max 5   # uses watchlists/youtube_channels.json
python scripts\youtube_fetch.py --watchlist watchlists/my_other_list.json --max 5

# Podcasts: raw feed URL, or a name from the curated list
python scripts\podcast_fetch.py --feed "https://feeds.example.com/show.rss" --max 3 --no-transcribe
python scripts\podcast_fetch.py --feed-name "Money Stuff" --max 3
python scripts\podcast_fetch.py --list-feeds   # see all curated shows

python scripts\reddit_fetch.py --query "Acme Corp guidance" --subreddit investing --max 10
python scripts\twitter_fetch.py --query "from:someexec" --max 20
python scripts\twitter_fetch.py --usage   # this month's X read spend vs. budget
```

### X / Twitter spend cap

`twitter_fetch.py` checks a local usage ledger (`.x_usage.json`, gitignored,
not shared with the dashboard's other sources) before every call and
refuses the request — no API call, no charge — once the current calendar
month has used up `X_MONTHLY_READ_BUDGET` reads (default 500 =~ $2.50 at
$0.005/read). Raise or lower the cap in `.env`. Run
`python scripts\twitter_fetch.py --usage` anytime to see reads used, spend,
and remaining budget for the month.

Requests are always made with `sort_order=relevancy` (X's own relevance
ranking) instead of the default recency, so the page of results is more
likely to contain substantive/high-engagement posts rather than just
whatever was posted in the last few minutes — at no extra cost, since it
only changes which posts fill the same page-size quota.

### YouTube channel watchlist (`watchlists/youtube_channels.json`)

A starter list of finance-relevant channels (verified working, real channel
IDs resolved via the Data API): Bloomberg Podcasts, Bloomberg Television,
Yahoo Finance, CNBC Television, Business Breakdowns, Invest Like The Best.
Add more entries as `{"name": "...", "channel_id": "UC..."}`.

Note: the YouTube search endpoint intermittently returns a transient
`403 accountDelegationForbidden` that has nothing to do with the request —
`youtube_fetch.py` retries automatically on that specific error.

### Curated podcast feeds (`watchlists/podcast_feeds.json`)

Verified RSS feeds for: Money Stuff, Odd Lots, Masters in Business (all
Bloomberg, hosted on Omny), Business Breakdowns and Invest Like the Best
(Colossus), Animal Spirits (Ritholtz Wealth Management / The Compound), and
We Study Billionaires (The Investor's Podcast Network). Add more with
`{"name": "...", "feed": "https://...", "description": "..."}` — Apple
Podcasts/Spotify links are not feed URLs; find the real RSS via the show's
hosting platform page (Omny, Libsyn, Megaphone, etc).

## Dashboard

A local Flask web UI at `dashboard/app.py`: enter a company/ticker and a date
range, get back a sentiment summary, a stock-price-vs-sentiment chart, and
staged raw results across all four sources shown side by side (YouTube,
Podcasts, Reddit, X).

```powershell
cd dashboard
pip install -r requirements.txt
python app.py
# open http://127.0.0.1:5050
```

### Sentiment summary + chart (`dashboard/analytics.py`)

- **Summary box**: calls `claude-opus-4-8` (needs `ANTHROPIC_API_KEY` in
  `.env` — real per-search cost, proportional to how much excerpt text gets
  sent) with the top 5 items per source and asks for 4-6 grounded sentiment
  bullets. Verified it stays grounded in the supplied content, but **also
  observed a small factual drift** in testing (misstated a dollar figure by
  ~$2 that was correct in the source text) — the UI carries a disclaimer to
  verify figures against the source cards below rather than citing the box
  directly.
- **Stock Price vs. Social Sentiment chart**: price comes from Yahoo's
  unauthenticated chart endpoint (`query1.finance.yahoo.com/v8/finance/chart`),
  with the ticker resolved from a bare company name via Yahoo's free search
  endpoint (prefers US-exchange matches, e.g. `MELI` over `MELI.BA`). The
  sentiment line is a **local, free VADER score** (no LLM call) averaged per
  day across all fetched items with a date — deliberately not LLM-generated,
  since a chart needs exact numeric points and an LLM guessing a time series
  isn't reliable for that.
  **Note:** Stooq was the original plan for price data but now sits behind a
  JavaScript proof-of-work bot challenge — not something to work around, so
  the dashboard uses Yahoo's endpoint instead.

- **Layout**: four columns side by side (YouTube / Podcasts / Reddit / X),
  each independently scrollable so one long column doesn't push the others
  around. Collapses to 2 columns then 1 on narrower viewports. Each column
  shows its top 5 (by that source's sort metric) with a "See more (N)"
  toggle for the rest — the LLM summary above only ever sees this same top-5
  set, so it's consistent with what's visible without expanding anything.
- **Sorting**: each column sorts by the most-popular-first metric that's
  actually available from that platform — YouTube by view count (fetched via
  a separate `videos.list` call, since search results don't include it),
  Reddit by score, X by impressions (falling back to summed
  likes+retweets+replies+quotes if impressions aren't in the response).
  **Podcasts stay sorted by date** — podcast RSS feeds never publish
  play/download counts (that data is private to the podcast host), so there's
  no real popularity signal to sort by; each column header states what it's
  sorted by so this isn't silently inconsistent.
- **YouTube**: searched as `"<company> stock"`, not the bare input — a bare
  ticker/name is often ambiguous (`MELI` is a common word/name in Indonesian
  and Malay; a bare search returned ~0/12 relevant videos, `"MELI stock"`
  returned 7/8 relevant). Transcripts are fetched automatically since that's
  fast (captions API, not audio transcription); when none is available the
  card just says "No transcript" inline next to the channel/date, same as
  any other missing-data case.
- **Podcasts**: matched by whole-word keyword in title/summary across
  `watchlists/podcast_feeds.json`, filtered to the date range. Metadata only
  by default — click **Transcribe** on a specific episode to run local
  Whisper on demand (slow; the button shows a loading state while it runs).
- **Reddit**: only runs if `REDDIT_CLIENT_ID`/`SECRET`/`USER_AGENT` are set in
  `.env`; otherwise the column shows the current access status instead of
  attempting a call. Results are filtered to the date range after the fact
  (PRAW's search has no native date-range parameter).
- **X**: only runs if the selected date range's start date is within the
  last 7 days (there's a "Last 7 days" button next to the date pickers that
  sets exactly that range) — X's recent-search endpoint can't see further
  back than that regardless of what's requested, so any other range skips
  the call entirely rather than returning an incomplete or empty result.
  Within that window, it then runs if `X_BEARER_TOKEN` is set; otherwise
  shows the on-hold status. Once the monthly read budget (see "X / Twitter
  spend cap" above) is used up, the column shows the budget-exhausted error
  instead of attempting a call.
- **Export**: "Download as Markdown" saves the current search's raw staged
  data (not a synthesized digest) for handing off elsewhere, across all four
  sources.
- If `ffmpeg` errors surface only when transcribing *through the dashboard*
  (not via `podcast_fetch.py` directly), start `app.py` with
  `use_reloader=False` (already set) — Flask's reloader re-execs a child
  process that doesn't reliably inherit a PATH modified after the parent
  shell started, which matters here since Whisper shells out to ffmpeg.

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
