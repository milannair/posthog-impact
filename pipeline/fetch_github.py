"""Fetch merged PRs + reviews from the PostHog repo via GitHub GraphQL.

Caches raw pages to CACHE_DIR so re-runs are cheap. Window is 120 days so the
30-day drag window is fully computable for the 90-day scoring window.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import urllib.request

TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
CACHE_DIR = os.environ.get("CACHE_DIR", "/data/cache")
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
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={
            "Authorization": f"bearer {TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "posthog-impact",
        },
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                payload = json.loads(r.read())
            if payload.get("errors"):
                raise RuntimeError(payload["errors"])
            return payload["data"]["search"]
        except Exception as e:  # noqa: BLE001
            if attempt == 4:
                raise
            print(f"  retry {attempt + 1} after {e}", file=sys.stderr)
            time.sleep(2 ** attempt)


def main():
    if not TOKEN:
        sys.exit("GITHUB_TOKEN not set")
    os.makedirs(CACHE_DIR, exist_ok=True)
    out_path = os.path.join(CACHE_DIR, "prs.json")

    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    start = end - timedelta(days=WINDOW_DAYS)

    # Search caps at 1000 results per query, so walk the window in weekly slices.
    prs, seen = [], set()
    slice_start = start
    while slice_start < end:
        slice_end = min(slice_start + timedelta(days=7), end)
        q = (
            f"repo:{OWNER}/{NAME} is:pr is:merged "
            f"merged:{slice_start.isoformat()}..{slice_end.isoformat()}"
        )
        cursor, got = None, 0
        while True:
            conn = gql(q, cursor)
            for n in conn["nodes"]:
                if n and n.get("number") not in seen:
                    seen.add(n["number"])
                    prs.append(n)
                    got += 1
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]
        print(
            f"{slice_start}..{slice_end}: +{got} (total {len(prs)})", file=sys.stderr
        )
        slice_start = slice_end
    with open(out_path, "w") as f:
        json.dump(prs, f)
    print(f"wrote {len(prs)} PRs to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
