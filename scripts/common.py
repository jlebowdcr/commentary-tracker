"""Shared helpers for the commentary-tracker connector scripts."""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"


def load_env():
    """Load key=value pairs from commentary-tracker/.env into os.environ."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def require_env(*names):
    load_env()
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        sys.exit(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            f"Set them in {ROOT / '.env'} (see .env.example)."
        )


def emit(source: str, query: str, items: list):
    """Write a standard-shaped result to output/<source>_<timestamp>.json and print it."""
    result = {
        "source": source,
        "query": query,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(items),
        "items": items,
    }
    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_query = "".join(c if c.isalnum() else "_" for c in query)[:40]
    out_path = OUTPUT_DIR / f"{source}_{safe_query}_{stamp}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\n[saved to {out_path}]", file=sys.stderr)
    return result
