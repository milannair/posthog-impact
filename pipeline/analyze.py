"""Compute engineer impact metrics for the PostHog repo.

Net = Leverage + Delivery - Drag

  Leverage  abstractions other people import and build on, weighted by the number
            of *distinct downstream authors* (one person importing their own module
            is not leverage).
  Delivery  sqrt-damped commits, merged PRs and reviews given, so raw volume has
            sharply diminishing returns.
  Drag      lines of theirs reverted, hot-fixed, rewritten or deleted by someone
            else within 30 days of landing.

Every number written here carries the evidence that produced it, so the dashboard
can always show what went into a score.
"""

import json
import math
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

REPO = os.environ.get("REPO_DIR", "/data/repo")
CACHE_DIR = os.environ.get("CACHE_DIR", "/data/cache")
OUT = os.environ.get("METRICS_PATH", "/data/metrics.json")
SCORE_DAYS = 90
DRAG_DAYS = 30
MIN_COMMITS = 8  # impact floor — below this the sample is too small to rank

BOT_PAT = re.compile(
    r"(\[bot\]|dependabot|renovate|github-actions|posthog-bot|snyk|codecov|"
    r"sentry-io|greenkeeper|imgbot|-bot$|^bot$)",
    re.I,
)
CODE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs")
SCOPES = ("posthog/", "frontend/src/", "plugin-server/src/", "products/", "ee/", "common/", "rust/")
INFRA_PAT = re.compile(
    r"(^\.github/|^bin/|Dockerfile|^ci/|conftest\.py$|jest\.config|playwright|"
    r"^\.pre-commit|pyproject\.toml$|tsconfig|webpack|vite\.config|Makefile$)",
    re.I,
)
FIX_PAT = re.compile(r"\b(fix|hotfix|revert|patch|repair|broke|broken|regression)\b", re.I)

now = datetime.now(timezone.utc)
score_cutoff = now - timedelta(days=SCORE_DAYS)
prev_cutoff = now - timedelta(days=2 * SCORE_DAYS)


def git(*args):
    return subprocess.run(
        ["git", "-C", REPO, *args], capture_output=True, text=True, errors="replace"
    ).stdout


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_commits():
    sep = "\x1e"
    raw = git(
        "log", "--no-merges", f"--since={(now - timedelta(days=185)).date()}",
        f"--pretty=format:{sep}%H%x1f%an%x1f%ae%x1f%aI%x1f%s", "--numstat",
    )
    commits = []
    for block in raw.split(sep):
        block = block.strip("\n")
        if not block:
            continue
        head, *rest = block.split("\n")
        parts = head.split("\x1f")
        if len(parts) < 5:
            continue
        sha, name, email, date, subject = parts[:5]
        files = [
            {"add": int(c[0]), "del": int(c[1]), "path": c[2]}
            for c in (l.split("\t") for l in rest)
            if len(c) == 3 and c[0] != "-"
        ]
        commits.append({"sha": sha, "name": name, "email": email,
                        "date": parse_ts(date), "subject": subject, "files": files})
    return commits


PR_REF = re.compile(r"\(#(\d+)\)\s*$")


def build_identity(prs, commits):
    """Resolve a commit to a GitHub login.

    PostHog squash-merges, so a commit subject ends in "(#12345)" — that maps a
    commit to its PR and therefore to the PR author's login exactly. Everything
    else (noreply emails, display-name matching) is a fallback.
    """
    logins = {}
    for p in prs:
        a = (p.get("author") or {}).get("login")
        if a:
            logins[a.lower()] = a
    pr_author = {
        p["number"]: p["author"]["login"] for p in prs if p.get("author")
    }

    # A commit whose subject carries a PR number teaches us that this git
    # identity (email) belongs to that PR's author.
    email_to_login = {}
    name_to_login = {}
    for c in commits:
        m = PR_REF.search(c["subject"])
        if m:
            login = pr_author.get(int(m.group(1)))
            if login:
                email_to_login.setdefault(c["email"], login)
                name_to_login.setdefault(c["name"], login)
    for c in commits:
        m = re.match(r"^(?:\d+\+)?([\w.-]+)@users\.noreply\.github\.com$", c["email"])
        if m:
            email_to_login.setdefault(c["email"], logins.get(m.group(1).lower(), m.group(1)))

    def resolve(c):
        m = PR_REF.search(c["subject"])
        if m and pr_author.get(int(m.group(1))):
            return pr_author[int(m.group(1))]
        if c["email"] in email_to_login:
            return email_to_login[c["email"]]
        if c["name"] in name_to_login:
            return name_to_login[c["name"]]
        return logins.get(c["name"].lower().replace(" ", ""), c["name"])

    return resolve


IMPORT_PY = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)
IMPORT_TS = re.compile(r"""(?:from|import\()\s*['"]([^'"]+)['"]""", re.M)


def build_import_graph(files):
    importers = defaultdict(set)
    for path in files:
        try:
            with open(os.path.join(REPO, path), "r", errors="replace") as f:
                src = f.read(160_000)
        except OSError:
            continue
        if path.endswith(".py"):
            for a, b in IMPORT_PY.findall(src):
                importers[(a or b).replace(".", "/")].add(path)
        else:
            for spec in IMPORT_TS.findall(src):
                base = (
                    os.path.normpath(os.path.join(os.path.dirname(path), spec))
                    if spec.startswith(".")
                    else spec.lstrip("~@/")
                )
                importers[base].add(path)
    return importers


def file_keys(path):
    stem = re.sub(r"\.(py|tsx?|jsx?)$", "", path)
    keys = {stem}
    for suffix in ("/index", "/__init__"):
        if stem.endswith(suffix):
            keys.add(stem[: -len(suffix)])
    segs = stem.split("/")
    for n in (2, 3):
        if len(segs) >= n:
            keys.add("/".join(segs[-n:]))
    return keys


def main():
    with open(os.path.join(CACHE_DIR, "prs.json")) as f:
        prs = json.load(f)
    prs = [p for p in prs if p.get("author") and not BOT_PAT.search(p["author"]["login"])]

    commits = load_commits()
    resolve = build_identity(prs, commits)
    for c in commits:
        c["login"] = resolve(c)
        c["is_fix"] = bool(FIX_PAT.search(c["subject"]))
    commits = [c for c in commits if not BOT_PAT.search(c["login"])]
    commits.sort(key=lambda c: c["date"])
    print(f"loaded {len(commits)} commits, {len(prs)} PRs", file=sys.stderr)

    window = [c for c in commits if c["date"] >= score_cutoff]
    prev = [c for c in commits if prev_cutoff <= c["date"] < score_cutoff]
    win_prs = [p for p in prs if parse_ts(p["mergedAt"]) >= score_cutoff]
    prev_prs = [p for p in prs if prev_cutoff <= parse_ts(p["mergedAt"]) < score_cutoff]

    contributors = {c["login"] for c in window} | {p["author"]["login"] for p in win_prs}

    S = defaultdict(lambda: defaultdict(float))
    abs_ev = defaultdict(list)      # adopted abstractions
    rework_ev = defaultdict(list)   # rework/drag events
    area_ct = defaultdict(lambda: defaultdict(int))

    for c in window:
        S[c["login"]]["commits"] += 1
        trees = {"/".join(f["path"].split("/")[:2]) for f in c["files"] if "/" in f["path"]}
        for t in trees:
            area_ct[c["login"]][t] += 1
        if any(INFRA_PAT.search(f["path"]) for f in c["files"]):
            S[c["login"]]["unblocking"] += 1

    for p in win_prs:
        S[p["author"]["login"]]["prs"] += 1
    for p in win_prs:
        author = p["author"]["login"]
        seen = set()
        for r in (p.get("reviews") or {}).get("nodes", []):
            rl = ((r.get("author") or {}).get("login"))
            if not rl or rl == author or rl in seen or BOT_PAT.search(rl):
                continue
            seen.add(rl)
            S[rl]["reviews"] += 1

    # ---------------- LEVERAGE: who builds on whose code ----------------
    tracked = [
        p for p in git("ls-files").splitlines()
        if p.endswith(CODE_EXT) and p.startswith(SCOPES)
    ]
    print(f"import graph over {len(tracked)} files", file=sys.stderr)
    importers = build_import_graph(tracked)

    file_lines = defaultdict(lambda: defaultdict(int))
    for c in commits:
        for f in c["files"]:
            file_lines[f["path"]][c["login"]] += f["add"]

    adopted_total = 0
    downstream = defaultdict(set)    # login -> distinct other engineers building on them
    owned_adopted = defaultdict(set)  # login -> their modules that anyone imports
    for path in tracked:
        authors = file_lines.get(path)
        if not authors:
            continue
        owner = max(authors, key=authors.get)
        if owner not in contributors:
            continue
        deps = set()
        for k in file_keys(path):
            deps |= importers.get(k, set())
        deps.discard(path)
        if not deps:
            continue
        dep_authors = set()
        for d in deps:
            da = file_lines.get(d)
            if da:
                dep_authors.add(max(da, key=da.get))
        dep_authors.discard(owner)

        # Leverage credits fan-in, but pays far more for *other people* adopting it.
        # Downstream authors are unioned across all of an owner's files — the same
        # colleague importing ten of their modules is one adopter, not ten.
        S[owner]["fan_in"] += len(deps)
        downstream[owner] |= dep_authors
        owned_adopted[owner].add(path)
        if len(dep_authors) >= 2:
            adopted_total += 1
        share = authors[owner] / max(sum(authors.values()), 1)
        if share > 0.6 and len(deps) >= 5:
            S[owner]["load_bearing"] += 1
        pts = round(math.sqrt(len(deps)) * 1.6 + len(dep_authors) * 3.0)
        if pts >= 4:
            others = sorted(dep_authors)[:3]
            abs_ev[owner].append({
                "path": path, "delta": f"+{pts}", "sort": pts,
                "note": f"fan-in {len(deps)} · "
                        + (f"extended by {', '.join(others)}"
                           + (f" +{len(dep_authors) - 3}" if len(dep_authors) > 3 else "")
                           if others else "no other authors yet"),
            })

    # ---------------- DRAG ----------------
    by_subject = defaultdict(list)
    for c in commits:
        by_subject[c["subject"]].append(c)
    revert_re = re.compile(r'Revert\s+"(.+?)"')
    for c in commits:
        m = revert_re.search(c["subject"])
        if not m:
            continue
        for orig in by_subject.get(m.group(1), []):
            if orig["date"] < c["date"] and orig["date"] >= score_cutoff:
                S[orig["login"]]["reverts"] += 1
                rework_ev[orig["login"]].append({
                    "path": orig["files"][0]["path"] if orig["files"] else orig["subject"],
                    "delta": "revert", "sort": 40,
                    "note": f"{c['login']} · {c['date'].date()} · {c['subject'][:78]}",
                })

    touches = defaultdict(list)
    for c in commits:
        for f in c["files"]:
            touches[f["path"]].append((c, f))

    drag_events = 0
    for path, seq in touches.items():
        for i, (c, f) in enumerate(seq):
            if c["date"] < score_cutoff or f["add"] == 0:
                continue
            login = c["login"]
            S[login]["lines_landed"] += f["add"]
            end = c["date"] + timedelta(days=DRAG_DAYS)
            for c2, f2 in seq[i + 1:]:
                if c2["date"] > end:
                    break
                if c2["login"] == login or f2["del"] == 0:
                    continue
                killed = min(f2["del"], f["add"])
                S[login]["lines_reworked"] += killed
                if c2["is_fix"] and killed >= 5:
                    S[login]["hotfixes"] += 1
                    drag_events += 1
                    rework_ev[login].append({
                        "path": path, "delta": f"−{killed}", "sort": killed,
                        "note": f"{c2['login']} · {c2['date'].date()} · {c2['subject'][:78]}",
                    })
                    break

    # ---------------- hotspots ----------------
    fix_by_file = defaultdict(int)
    top_author = defaultdict(lambda: defaultdict(int))
    for c in window:
        for f in c["files"]:
            top_author[f["path"]][c["login"]] += 1
            if c["is_fix"]:
                fix_by_file[f["path"]] += 1
    hotspots = [
        {"path": p, "fixes": n,
         "author": max(top_author[p], key=top_author[p].get) if top_author.get(p) else "—"}
        for p, n in sorted(fix_by_file.items(), key=lambda kv: -kv[1])[:6]
    ]

    # ---------------- scores ----------------
    engineers = []
    for login in contributors:
        s = S[login]
        if s["commits"] < MIN_COMMITS:
            continue
        dep_authors = len(downstream[login])
        leverage = round(math.sqrt(s["fan_in"]) * 2.2 + dep_authors * 6
                         + math.sqrt(len(owned_adopted[login])) * 4
                         + s["load_bearing"] * 4 + math.sqrt(s["unblocking"]) * 6)
        delivery = round(math.sqrt(s["commits"]) * 8 + math.sqrt(s["prs"]) * 7
                         + math.sqrt(s["reviews"]) * 9)
        landed = s["lines_landed"]
        rework_rate = 100 * min(s["lines_reworked"], landed) / landed if landed else 0.0
        drag = round(rework_rate * 1.4 + s["hotfixes"] * 2.5 + s["reverts"] * 12)

        areas = sorted(area_ct[login].items(), key=lambda kv: -kv[1])[:5]
        abs_list = sorted(abs_ev[login], key=lambda e: -e["sort"])[:6]
        rw_list = sorted(rework_ev[login], key=lambda e: -e["sort"])[:6]

        top_area = areas[0][0] if areas else "—"
        engineers.append({
            "login": login,
            "subtitle": " · ".join(a for a, _ in areas[:3]) or "—",
            "leverage": leverage, "delivery": delivery, "drag": drag,
            "net": leverage + delivery - drag,
            "commits": int(s["commits"]), "prs": int(s["prs"]),
            "reviews": int(s["reviews"]),
            "adopted": len(owned_adopted[login]),
            "dep_authors": dep_authors,
            "fan_in": int(s["fan_in"]),
            "load_bearing": int(s["load_bearing"]),
            "unblocking": int(s["unblocking"]),
            "reverts": int(s["reverts"]),
            "hotfixes": int(s["hotfixes"]),
            "lines_landed": int(landed),
            "rework_rate": round(rework_rate, 1),
            "drag100": round(100 * drag / s["commits"], 1),
            "abs": [{k: v for k, v in e.items() if k != "sort"} for e in abs_list],
            "rework": [{k: v for k, v in e.items() if k != "sort"} for e in rw_list],
            "areas": [{"path": a, "delta": str(n), "note": "commits touching this tree"}
                      for a, n in areas],
            "top_area": top_area,
        })

    # The three pillars come from different unit systems (import sites vs. commits
    # vs. reworked lines), so raw sums would let Leverage swamp the other two and
    # Net would just be Leverage renamed. Put each pillar on a common footing:
    # the 90th-percentile engineer scores 300 in every pillar. Ordering within a
    # pillar is untouched — only the shared scale changes.
    def scale_to(key, target=300.0):
        vals = sorted(e[key] for e in engineers)
        if not vals:
            return 1.0
        p90 = vals[min(int(0.9 * len(vals)), len(vals) - 1)]
        return target / p90 if p90 > 0 else 1.0

    factors = {k: scale_to(k) for k in ("leverage", "delivery", "drag")}
    for e in engineers:
        for k in ("leverage", "delivery", "drag"):
            e[k + "_raw_points"] = e[k]
            e[k] = round(e[k] * factors[k])
        e["net"] = e["leverage"] + e["delivery"] - e["drag"]
        e["drag100"] = round(100 * e["drag"] / e["commits"], 1) if e["commits"] else 0.0

    engineers.sort(key=lambda e: -e["net"])

    # Verdicts are generated from the same numbers shown on the card — never prose
    # that isn't backed by a metric on screen.
    for i, e in enumerate(engineers):
        bits = []
        if e["dep_authors"]:
            bits.append(
                f"{e['dep_authors']} other engineers build on code they own "
                f"({e['fan_in']} import sites across {e['adopted']} modules)"
            )
        else:
            bits.append("no cross-author adoption of their modules in this window")
        bits.append(
            f"delivery is {e['commits']} commits, {e['prs']} merged PRs and "
            f"{e['reviews']} reviews given"
        )
        if e["drag"]:
            d = [f"{e['rework_rate']}% of their landed lines were reworked by others "
                 f"within {DRAG_DAYS} days"]
            if e["hotfixes"]:
                d.append(f"{e['hotfixes']} follow-up fixes by colleagues")
            if e["reverts"]:
                d.append(f"{e['reverts']} reverts")
            bits.append("drag: " + ", ".join(d))
        else:
            bits.append("no measurable drag in this window")
        if e["load_bearing"]:
            bits.append(f"{e['load_bearing']} load-bearing files they dominantly authored")
        if e["unblocking"]:
            bits.append(f"{e['unblocking']} commits to CI/build/test tooling")
        e["verdict"] = f"Rank {i + 1} by net impact, mostly in {e['top_area']}. " \
                       + "; ".join(bits).capitalize() + "."

    def pct(a, b):
        if not b:
            return "—"
        return f"{'+' if a >= b else ''}{round(100 * (a - b) / b)}%"

    payload = {
        "generated_at": now.isoformat(),
        "repo": "PostHog/posthog",
        "window_days": SCORE_DAYS,
        "drag_window_days": DRAG_DAYS,
        "min_commits": MIN_COMMITS,
        "head": git("log", "-1", "--format=%h %cI").strip(),
        "kpis": [
            {"label": "Commits", "value": len(window),
             "delta": pct(len(window), len(prev)), "good": len(window) >= len(prev),
             "note": "vs. prior 90 days"},
            {"label": "Pull requests merged", "value": len(win_prs),
             "delta": pct(len(win_prs), len(prev_prs)), "good": len(win_prs) >= len(prev_prs),
             "note": "authored by humans"},
            {"label": "Contributors", "value": len(contributors),
             "delta": f"{len(engineers)} ranked", "good": True,
             "note": f"≥{MIN_COMMITS} commits to be ranked"},
            {"label": "Abstractions adopted", "value": adopted_total,
             "delta": "2+ authors", "good": True,
             "note": "modules imported by 2+ other authors"},
            {"label": "Drag events", "value": drag_events,
             "delta": f"{DRAG_DAYS}d window", "good": False,
             "note": "reverts, hot-fixes, rewrites by others"},
        ],
        "scale_factors": {k: round(v, 4) for k, v in factors.items()},
        "hotspots": hotspots,
        "engineers": engineers,
    }
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f)
    print(f"wrote {OUT}: {len(engineers)} ranked of {len(contributors)}", file=sys.stderr)
    for e in engineers[:8]:
        print(f"  net {e['net']:5d}  lev {e['leverage']:4d}  del {e['delivery']:4d}"
              f"  drag {e['drag']:4d}  {e['login']}", file=sys.stderr)


if __name__ == "__main__":
    main()
