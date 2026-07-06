"""
Search recent X/Twitter posts via the official X API v2 recent-search endpoint.

Requires X_BEARER_TOKEN (developer.x.com -> create a project/app -> generate
a bearer token). The free tier only covers the last 7 days of posts and has
a monthly read cap, so scope queries narrowly.

Usage:
  python twitter_fetch.py --query "from:someexec" --max 20
  python twitter_fetch.py --query "Acme Corp guidance" --max 20
"""
import argparse
import os

import requests

from common import emit, require_env

API_URL = "https://api.twitter.com/2/tweets/search/recent"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True,
                         help="X search query, e.g. 'from:handle' or free text (supports X search operators)")
    parser.add_argument("--max", type=int, default=20, help="Max posts to fetch, 10-100 (default 20)")
    args = parser.parse_args()

    require_env("X_BEARER_TOKEN")

    params = {
        "query": args.query,
        "max_results": max(10, min(args.max, 100)),
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

    emit("twitter", args.query, items)


if __name__ == "__main__":
    main()
