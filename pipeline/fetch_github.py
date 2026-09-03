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
  rateLimit { limit cost remaining resetAt }
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


MAX_SLEEP = 3900  # a GraphQL budget resets hourly; never wait more than that + slack


def api_get(path):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": f"bearer {TOKEN}", "User-Agent": "posthog-impact"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def graphql_reset_wait():
    """Seconds until the GraphQL budget resets, from /rate_limit.

    That endpoint is free — it does not consume points — so it is safe to call
    precisely when we are already exhausted and the GraphQL query itself can no
    longer tell us anything.
    """
    try:
        gl = api_get("/rate_limit")["resources"]["graphql"]
        if gl.get("remaining", 0) > 0:
            return 0
        return max(0, int(gl["reset"]) - int(time.time())) + 5
    except Exception as e:  # noqa: BLE001
        print(f"  could not read /rate_limit ({e})", file=sys.stderr)
        return 60


def is_rate_limited(err):
    text = str(err).lower()
    return "rate_limit" in text or "rate limit" in text or "was submitted too quickly" in text


def sleep_loudly(secs, why):
    secs = min(secs, MAX_SLEEP)
    until = datetime.now(timezone.utc) + timedelta(seconds=secs)
    print(f"  {why}: sleeping {secs}s until {until.strftime('%H:%M:%S')}Z",
          file=sys.stderr, flush=True)
    time.sleep(secs)


def gql(q, cursor):
    body = json.dumps({"query": QUERY, "variables": {"q": q, "cursor": cursor}}).encode()
    attempt = 0
    rate_waits = 0
    while True:
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
            data = payload["data"]
            # Spend down to a reserve rather than into the wall: hitting zero
            # costs a full reset, so pause once the remaining budget is thin.
            rl = data.get("rateLimit") or {}
            if rl.get("remaining") is not None and rl["remaining"] <= max(rl.get("cost", 1) * 3, 15):
                reset = rl.get("resetAt")
                wait = 60
                if reset:
                    wait = max(0, int((datetime.fromisoformat(
                        reset.replace("Z", "+00:00")) - datetime.now(timezone.utc)
                    ).total_seconds())) + 5
                sleep_loudly(wait, f"GraphQL budget down to {rl['remaining']}")
            return data["search"]
        except urllib.error.HTTPError as e:
            # Secondary limits answer with Retry-After; honour it exactly.
            retry_after = e.headers.get("Retry-After") if e.headers else None
            if retry_after and rate_waits < 4:
                rate_waits += 1
                sleep_loudly(int(retry_after) + 2, f"HTTP {e.code}, Retry-After")
                continue
            if e.code in (403, 429) and rate_waits < 4:
                rate_waits += 1
                sleep_loudly(graphql_reset_wait(), f"HTTP {e.code} rate limited")
                continue
            if attempt >= 4:
                raise
            attempt += 1
            sleep_loudly(min(60, 5 * 2 ** attempt) + random.uniform(0, 5),
                         f"HTTP {e.code}")
        except Exception as e:  # noqa: BLE001
            # A depleted GraphQL budget arrives as a 200 with an errors block, so
            # waiting out the reset has to happen here too — the old code just
            # backed off 63s and then abandoned the whole week.
            if is_rate_limited(e):
                if rate_waits >= 4:
                    raise
                rate_waits += 1
                sleep_loudly(graphql_reset_wait(), "GraphQL rate limit exceeded")
                continue
            if attempt >= 4:
                raise
            attempt += 1
            sleep_loudly(min(60, 5 * 2 ** attempt) + random.uniform(0, 5), f"error {e}")


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

    try:
        gl = api_get("/rate_limit")["resources"]["graphql"]
        print(f"graphql budget: {gl['remaining']}/{gl['limit']} remaining",
              file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass

    fetched = reused = 0
    todo = []
    for a, b in slices(start, end):
        # A slice that ends today or later is still accumulating merges, so it is
        # always refetched. Completed past weeks are immutable and reused.
        if os.path.exists(chunk_path(a, b)) and b <= today:
            reused += 1
        else:
            todo.append((a, b))

    # Two passes: a week that fails on the first is retried at the end, by which
    # time a rate-limit reset has usually landed. Anything still missing is left
    # absent so the next boot picks up exactly those weeks.
    failed = []
    for attempt_round in (1, 2):
        queue, failed = failed if attempt_round == 2 else todo, []
        if attempt_round == 2 and queue:
            print(f"retrying {len(queue)} failed week(s)", file=sys.stderr)
        for a, b in queue:
            try:
                rows = fetch_slice(a, b)
            except Exception as e:  # noqa: BLE001
                print(f"{a}..{b}: FAILED ({e})", file=sys.stderr)
                failed.append((a, b))
                continue
            write_atomic(chunk_path(a, b), rows)
            fetched += 1
            print(f"{a}..{b}: {len(rows)} PRs {'(refresh)' if b > today else ''}",
                  file=sys.stderr, flush=True)
        if not failed:
            break

    # No merged prs.json: the weekly chunks on the volume *are* the dataset, and
    # analyze.py streams them one file at a time. Holding 15k PRs (with their
    # file lists) in memory here bought nothing and cost a container restart.
    print(f"weeks: {reused} cached, {fetched} fetched"
          + (f", {len(failed)} still missing (next boot retries them)" if failed else ""),
          file=sys.stderr)
    return 1 if failed and not fetched and not reused else 0


if __name__ == "__main__":
    sys.exit(main())
