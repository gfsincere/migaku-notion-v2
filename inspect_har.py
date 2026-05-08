"""Pull request bodies for the most interesting Migaku endpoints from a HAR.

The Migaku browser extension (and v2 itself, eventually) talks to four hosts:

  - core-server.migaku.com         (read = /pull-sync, write = /push/enqueue)
  - file-sync-worker-api.migaku.com  (PUT /data/SRSMEDIA/<filename>)
  - ai-worker.migaku.com            (translation / nuance / definitions)
  - identitytoolkit/securetoken.googleapis.com   (Firebase auth)

Capture a HAR in your browser's DevTools while the extension does
something interesting (creating a card, syncing words, uploading audio),
then run this script with the .har path and you'll get pretty-printed
request and response bodies for every relevant entry. This is the living
API spec for v2 — re-run after any Migaku-side change.

Usage:
    python inspect_har.py path/to/capture.har
"""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.stdout.reconfigure(encoding="utf-8")

har_path = Path(sys.argv[1] if len(sys.argv) > 1 else
                "lesson_samples/migaku-card-creator.har")
har = json.loads(har_path.read_text(encoding="utf-8"))
entries = har["log"]["entries"]

WANTED = (
    "core-server.migaku.com",
    "file-sync-worker-api.migaku.com",
    "ai-worker.migaku.com",
)

def short(s, n=4000):
    if not s:
        return "(empty)"
    if len(s) <= n:
        return s
    return s[:n] + f"\n... [truncated {len(s) - n} more chars]"

for i, e in enumerate(entries, 1):
    req = e["request"]
    res = e["response"]
    p = urlparse(req["url"])
    if (p.hostname or "") not in WANTED:
        continue
    print("=" * 100)
    print(f"#{i:02d}  {req['method']}  {p.hostname}{p.path}")
    if p.query:
        print(f"     query: {p.query}")
    print(f"     -> {res.get('status', '?')}")
    body = req.get("postData", {}).get("text", "")
    if body:
        try:
            parsed = json.loads(body)
            body = json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            pass
        print("\n--- request body ---")
        print(short(body))
    rb = res.get("content", {}).get("text", "")
    if rb:
        try:
            parsed = json.loads(rb)
            rb = json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            pass
        print("\n--- response body ---")
        print(short(rb, n=2000))
    print()
