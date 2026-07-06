"""
Search Reddit for threads matching a query (optionally within one subreddit)
and pull top-level comments via the official Reddit API (PRAW).

Requires a Reddit "script" app: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET,
REDDIT_USER_AGENT (free at https://www.reddit.com/prefs/apps).

Usage:
  python reddit_fetch.py --query "Acme Corp earnings" --max 10
  python reddit_fetch.py --query "management credibility" --subreddit investing --max 10
"""
import argparse

import praw

from common import emit, require_env
import os


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True, help="Search query")
    parser.add_argument("--subreddit", default="all", help="Subreddit to search (default: all)")
    parser.add_argument("--max", type=int, default=10, help="Max threads to fetch (default 10)")
    parser.add_argument("--comments-per-thread", type=int, default=5,
                         help="Top-level comments to include per thread (default 5)")
    args = parser.parse_args()

    require_env("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT")
    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
    )
    reddit.read_only = True

    items = []
    for submission in reddit.subreddit(args.subreddit).search(args.query, limit=args.max, sort="new"):
        submission.comments.replace_more(limit=0)
        top_comments = [
            {
                "author": str(c.author) if c.author else "[deleted]",
                "body": c.body,
                "score": c.score,
                "created_utc": c.created_utc,
            }
            for c in submission.comments[: args.comments_per_thread]
        ]
        items.append({
            "title": submission.title,
            "url": f"https://www.reddit.com{submission.permalink}",
            "subreddit": str(submission.subreddit),
            "author": str(submission.author) if submission.author else "[deleted]",
            "created_utc": submission.created_utc,
            "score": submission.score,
            "selftext": submission.selftext,
            "top_comments": top_comments,
        })

    emit("reddit", args.query, items)


if __name__ == "__main__":
    main()
