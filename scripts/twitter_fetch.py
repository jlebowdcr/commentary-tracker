"""
Search recent X/Twitter posts via the official X API v2 recent-search endpoint.

Requires X_BEARER_TOKEN (developer.x.com -> create a project/app -> generate
a bearer token). There's no free tier as of Feb 2026 -- pay-per-use at
$0.005/post read. To keep spend predictable, reads are capped against a
monthly budget (X_MONTHLY_READ_BUDGET in .env, default 500 reads =~ $2.50)
tracked in .x_usage.json at the repo root. Once the budget is used up for
the current calendar month, calls are refused (not throttled) until the
ledger rolls over.

Requests always use sort_order=relevancy (X's own relevance ranking) rather
than the default recency, so the page filled by max_results is more likely
to include substantive/high-engagement posts instead of just whatever was
posted in the last few minutes. This doesn't cost anything extra -- same
per-read price, same page-size cap -- it only changes which posts fill the
quota. The dashboard's client-side sort by impressions/engagement still runs
on top of this, but can only reorder what's in that page, not expand it.

Usage:
  python twitter_fetch.py --query "from:someexec" --max 20
  python twitter_fetch.py --query "Acme Corp guidance" --max 20
  python twitter_fetch.py --usage        # show this month's spend/budget
"""
import argparse
import json
import os
from datetime import datetime, timezone

import requests

from common import ROOT, emit, load_env, require_env

API_URL = "https://api.twitter.com/2/tweets/search/recent"
USAGE_LEDGER = ROOT / ".x_usage.json"
DEFAULT_MONTHLY_BUDGET = 500  # reads/month =~ $2.50 at $0.005/read
COST_PER_READ = 0.005


def has_credentials() -> bool:
    load_env()
    return bool(os.environ.get("X_BEARER_TOKEN"))


def _monthly_budget() -> int:
    load_env()
    return int(os.environ.get("X_MONTHLY_READ_BUDGET", DEFAULT_MONTHLY_BUDGET))


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _load_ledger() -> dict:
    if not USAGE_LEDGER.exists():
        return {"month": _current_month(), "reads": 0}
    ledger = json.loads(USAGE_LEDGER.read_text(encoding="utf-8"))
    if ledger.get("month") != _current_month():
        return {"month": _current_month(), "reads": 0}
    return ledger


def _save_ledger(ledger: dict):
    USAGE_LEDGER.write_text(json.dumps(ledger, indent=2), encoding="utf-8")


def _record_reads(count: int):
    ledger = _load_ledger()
    ledger["reads"] += count
    _save_ledger(ledger)


def usage_summary() -> dict:
    """Read-only snapshot of this month's spend against budget."""
    ledger = _load_ledger()
    budget = _monthly_budget()
    return {
        "month": ledger["month"],
        "reads_used": ledger["reads"],
        "budget_reads": budget,
        "reads_remaining": max(0, budget - ledger["reads"]),
        "spend_usd": round(ledger["reads"] * COST_PER_READ, 2),
        "budget_usd": round(budget * COST_PER_READ, 2),
    }


def search_tweets(query: str, max_results: int = 20) -> list[dict]:
    """Caller is responsible for checking has_credentials() first.

    Raises RuntimeError (without calling the API, so it never costs anything)
    if this month's read budget is already used up, or too low to satisfy
    the X API's own 10-post minimum page size.
    """
    ledger = _load_ledger()
    budget = _monthly_budget()
    remaining = budget - ledger["reads"]
    if remaining < 10:
        raise RuntimeError(
            f"X read budget exhausted for {ledger['month']} "
            f"({ledger['reads']}/{budget} reads, ~${ledger['reads'] * COST_PER_READ:.2f} spent). "
            f"Resets next calendar month, or raise X_MONTHLY_READ_BUDGET in .env."
        )

    request_max = max(10, min(max_results, 100, remaining))
    params = {
        "query": query,
        "max_results": request_max,
        "sort_order": "relevancy",
        "tweet.fields": "created_at,author_id,public_metrics,lang",
        "expansions": "author_id",
        "user.fields": "username,name",
    }
    resp = requests.get(
        API_URL,
        headers={"Authorization": f"Bearer {os.environ['X_BEARER_TOKEN']}"},
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()

    users_by_id = {u["id"]: u for u in payload.get("includes", {}).get("users", [])}
    items = []
    for tweet in payload.get("data", []):
        author = users_by_id.get(tweet.get("author_id"), {})
        items.append({
            "id": tweet["id"],
            "url": f"https://x.com/{author.get('username', 'i')}/status/{tweet['id']}",
            "author_username": author.get("username"),
            "author_name": author.get("name"),
            "created_at": tweet.get("created_at"),
            "text": tweet.get("text"),
            "metrics": tweet.get("public_metrics"),
        })

    # Bill against posts actually returned (X's own result_count), not the
    # requested page size -- a short result still shouldn't be recorded as
    # a full page of reads.
    _record_reads(payload.get("meta", {}).get("result_count", len(items)))
    return items


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query",
                         help="X search query, e.g. 'from:handle' or free text (supports X search operators)")
    parser.add_argument("--max", type=int, default=20, help="Max posts to fetch, 10-100 (default 20)")
    parser.add_argument("--usage", action="store_true", help="Show this month's read spend/budget and exit")
    args = parser.parse_args()

    if args.usage:
        load_env()
        u = usage_summary()
        print(f"{u['month']}: {u['reads_used']}/{u['budget_reads']} reads "
              f"(${u['spend_usd']:.2f}/${u['budget_usd']:.2f}), "
              f"{u['reads_remaining']} reads remaining")
        return

    if not args.query:
        parser.error("--query is required (or use --usage)")

    require_env("X_BEARER_TOKEN")
    items = search_tweets(args.query, args.max)
    emit("twitter", args.query, items)


if __name__ == "__main__":
    main()
