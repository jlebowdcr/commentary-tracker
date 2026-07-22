"""
Dashboard analytics: ticker resolution + stock price (Yahoo), and
LLM-based sentiment-summary synthesis (Claude API).
"""
import os
from datetime import date, datetime

import requests

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def resolve_ticker(company: str) -> str | None:
    """Best-effort company-name-or-ticker -> US equity ticker symbol via
    Yahoo's free (unauthenticated) search endpoint. Prefers US exchanges so
    we don't land on a foreign cross-listing (e.g. MELI.BA over MELI)."""
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": company}, headers=_UA, timeout=10,
        )
        resp.raise_for_status()
        quotes = resp.json().get("quotes", [])
    except Exception:
        return None

    equities = [q for q in quotes if q.get("quoteType") == "EQUITY"]
    if not equities:
        return None
    us_exchanges = {"NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BATS"}
    us_matches = [q for q in equities if q.get("exchange") in us_exchanges]
    return (us_matches or equities)[0]["symbol"]


def fetch_stock_prices(ticker: str, after: date | None, before: date | None) -> list[dict]:
    """Daily closing prices via Yahoo's chart endpoint (no key required)."""
    period2 = before or date.today()
    period1 = after or date(period2.year - 1, period2.month, period2.day)
    try:
        resp = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={
                "period1": int(datetime.combine(period1, datetime.min.time()).timestamp()),
                "period2": int(datetime.combine(period2, datetime.min.time()).timestamp()) + 86400,
                "interval": "1d",
            },
            headers=_UA, timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()["chart"]["result"][0]
    except Exception:
        return []

    timestamps = result.get("timestamp", [])
    closes = result["indicators"]["quote"][0].get("close", [])
    prices = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        d = datetime.utcfromtimestamp(ts).date().isoformat()
        prices.append({"date": d, "close": round(close, 2)})
    return prices


def synthesize_summary(company: str, after: str | None, before: str | None,
                        sources: dict[str, list[dict]]) -> list[str]:
    """Calls Claude to turn the top-5-per-source items into a handful of
    public-sentiment bullet points. `sources` maps source name -> list of
    {title, date, excerpt, link} dicts (already capped to top 5 by caller)."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ["ANTHROPIC_API_KEY not configured in .env"]

    lines = [f"Company/ticker: {company}", f"Date window: {after or 'any'} to {before or 'any'}", ""]
    for source_name, items in sources.items():
        if not items:
            continue
        lines.append(f"## {source_name}")
        for item in items:
            lines.append(f"- [{item.get('date', '?')}] {item.get('title', '')}")
            excerpt = (item.get("excerpt") or "").strip().replace("\n", " ")
            if excerpt:
                lines.append(f"  {excerpt[:600]}")
        lines.append("")
    content = "\n".join(lines)

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=500,
            system=(
                "You are a research analyst summarizing public sentiment from social/media "
                "content for an investment research audience. Given titles and excerpts from "
                "YouTube, podcasts, Reddit, and X about a company, write 4-6 concise bullet "
                "points on the most relevant sentiment and themes for the given date window. "
                "Each bullet must be grounded in the provided content -- do not invent facts, "
                "numbers, or quotes not present in the material. If sources are thin or absent, "
                "say so plainly rather than padding. Output only the bullets, one per line, each "
                "starting with '- '. No preamble, no headers."
            ),
            messages=[{"role": "user", "content": content}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        bullets = [line.lstrip("- ").strip() for line in text.splitlines() if line.strip()]
        return bullets or ["No summary generated."]
    except Exception as e:
        return [f"Summary generation failed: {e}"]
