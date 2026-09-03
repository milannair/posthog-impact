"""Fetch merged PRs + reviews from the PostHog repo via GitHub GraphQL.

Each week of the window is cached as its own file under CACHE_DIR/prs/, written
as soon as that week finishes. A run that is interrupted — a redeploy, a crash,
a rate-limit give-up — only loses the week it was in the middle of; the next run
resumes from there instead of re-downloading everything.

Window is 120 days so the 30-day drag window is fully computable for the 90-day
scoring window.
"""

import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
CACHE_DIR = os.environ.get("CACHE_DIR", "/data/cache")
CHUNK_DIR = os.path.join(CACHE_DIR, "prs")
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "120"))
OWNER, NAME = "PostHog", "posthog"

QUERY = """
query($q:String!, $cursor:String) {
  search(query:$q, type:ISSUE, first:50, after:$cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { ... on PullRequest {
        number title mergedAt createdAt url additions deletions changedFiles
        author { login }
        labels(first:10) { nodes { name } }
        reviews(first:20) {
          nodes { state submittedAt author { login } }
        }
        comments(first:1) { totalCount }
        files(first:100) { nodes { path additions deletions } }
        closingIssuesReferences(first:5) { totalCount }
      } }
  }
}
"""


def gql(q, cursor):
    body = json.dumps({"query": QUERY, "variables": {"q": q, "cursor": cursor}}).encode()
    for attempt in range(6):
        req = urllib.request.Request(
            "https://api.github.com/graphql", data=body,
            headers={"Authorization": f"bearer {TOKEN}",
                     "Content-Type": "application/json",
                     "User-Agent": "posthog-impact"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                payload = json.loads(r.read())
            if payload.get("errors"):
                raise RuntimeError(payload["errors"])
            return payload["data"]["search"]
        except Exception as e:  # noqa: BLE001
            if attempt == 5:
                raise
            # Secondary rate limits want a long, jittered wait — the old 2**n
            # topped out at 8s, which is not enough to clear one.
            delay = min(60, 5 * (2 ** attempt)) + random.uniform(0, 5)
            print(f"  retry {attempt + 1} in {delay:.0f}s after {e}", file=sys.stderr)
            time.sleep(delay)


def slices(start, end):
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=7), end)
        yield cur, nxt
        cur = nxt


def chunk_path(a, b):
    return os.path.join(CHUNK_DIR, f"{a.isoformat()}_{b.isoformat()}.json")


SEARCH_CAP = 1000  # GitHub returns at most 1000 results for one search query


def fetch_range(a, b, into, seen):
    """Fetch one merged:a..b range into `into`. Returns True if it hit the cap."""
    q = (f"repo:{OWNER}/{NAME} is:pr is:merged "
         f"merged:{a.isoformat()}..{b.isoformat()}")
    cursor, n_seen = None, 0
    while True:
        conn = gql(q, cursor)
        for n in conn["nodes"]:
            n_seen += 1
            if n and n.get("number") not in seen:
                seen.add(n["number"])
                into.append(n)
        if not conn["pageInfo"]["hasNextPage"]:
            return n_seen >= SEARCH_CAP
        cursor = conn["pageInfo"]["endCursor"]


def fetch_slice(a, b):
    """One week of merged PRs.

    PostHog merges close to 1,000 PRs a week, which is exactly GitHub's search
    result cap — a week that hits it is silently truncated. When that happens,
    re-walk the week a day at a time so nothing is dropped.
    """
    out, seen = [], set()
    if not fetch_range(a, b, out, seen):
        return out
    print(f"  {a}..{b} hit the {SEARCH_CAP}-result cap — splitting by day",
          file=sys.stderr)
    out.clear()
    seen.clear()
    day = a
    while day < b:
        nxt = day + timedelta(days=1)
        fetch_range(day, nxt, out, seen)
        day = nxt
    return out


def write_atomic(path, obj):
    """Write via a temp file + rename so a kill mid-write can't leave a
    half-written chunk that later parses as valid-but-truncated JSON."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def main():
    if not TOKEN:
        sys.exit("GITHUB_TOKEN not set")
    os.makedirs(CHUNK_DIR, exist_ok=True)

    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=1)
    start = end - timedelta(days=WINDOW_DAYS)

    fetched = failed = reused = 0
    for a, b in slices(start, end):
        path = chunk_path(a, b)
        # A slice that ends today or later is still accumulating merges, so it is
        # always refetched. Completed past weeks are immutable and reused.
        stale = b > today
        if os.path.exists(path) and not stale:
            reused += 1
            continue
        try:
            rows = fetch_slice(a, b)
        except Exception as e:  # noqa: BLE001
            # Keep going: one bad week shouldn't cost us the other sixteen, and
            # the chunk is simply absent so the next run retries just this one.
            print(f"{a}..{b}: FAILED ({e})", file=sys.stderr)
            failed += 1
            continue
        write_atomic(path, rows)
        fetched += 1
        print(f"{a}..{b}: {len(rows)} PRs {'(refresh)' if stale else ''}",
              file=sys.stderr)

    # No merged prs.json: the weekly chunks on the volume *are* the dataset, and
    # analyze.py streams them one file at a time. Holding 15k PRs (with their
    # file lists) in memory here bought nothing and cost a container restart.
    print(f"weeks: {reused} cached, {fetched} fetched"
          + (f", {failed} failed" if failed else ""), file=sys.stderr)
    return 1 if failed and not fetched and not reused else 0


if __name__ == "__main__":
    sys.exit(main())
