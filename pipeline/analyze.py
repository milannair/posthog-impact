"""Engineer impact for PostHog/posthog.

    net = leverage^0.75 + delivery - drag^0.75

The 0.75 exponent on both sides gives diminishing returns, so an engineer with
thousands of files cannot dominate on count alone.

Computed over the last 90 days of the default branch, excluding bots, generated
files, tests, and bulk commits touching more than 60 files. Every number written
here carries the evidence that produced it.
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
HOTFIX_DAYS = 14
BULK_FILES = 60      # commits above this are migrations/renames, not authorship
MIN_COMMITS = 8      # impact floor
DAMP = 0.75

BOT_PAT = re.compile(
    r"(\[bot\]|dependabot|renovate|github-actions|posthog-bot|snyk|codecov|"
    r"sentry-io|greenkeeper|imgbot|-bot$|^bot$|^posthog$)", re.I)
CODE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs")
SCOPES = ("posthog/", "frontend/src/", "plugin-server/src/", "products/", "ee/",
          "common/", "rust/")
GENERATED_PAT = re.compile(
    r"(\.min\.|/migrations/|/__snapshots__/|\.snap$|/generated/|_pb2\.py$|"
    r"\.generated\.|/vendor/|\.lock$|schema\.py$|/dist/|/node_modules/)", re.I)
TEST_PAT = re.compile(r"(^|/)(tests?|__tests__|e2e|cypress|spec)(/|$)|"
                      r"(test_[^/]+|[^/]+_test|[^/]+\.(test|spec))\.[a-z]+$", re.I)
SHARED_LAYER = re.compile(
    r"(^|/)(lib|common|hooks|models|core|utils|api|schema|providers)(/|$)")
FIX_PAT = re.compile(r"\b(fix|bug|regression)\b", re.I)
REFACTOR_PAT = re.compile(r"\b(refactor|cleanup|clean-up|simplify)\b", re.I)
PR_REF = re.compile(r"\(#(\d+)\)\s*$")

now = datetime.now(timezone.utc)
score_cutoff = now - timedelta(days=SCORE_DAYS)
prev_cutoff = now - timedelta(days=2 * SCORE_DAYS)


def git(*a):
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True,
                          text=True, errors="replace").stdout


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def interesting(path):
    return (path.endswith(CODE_EXT) and path.startswith(SCOPES)
            and not GENERATED_PAT.search(path) and not TEST_PAT.search(path))


def load_commits():
    sep = "\x1e"
    raw = git("log", "--no-merges", f"--since={(now - timedelta(days=185)).date()}",
              f"--pretty=format:{sep}%H%x1f%an%x1f%ae%x1f%aI%x1f%s", "--numstat")
    out = []
    for block in raw.split(sep):
        block = block.strip("\n")
        if not block:
            continue
        head, *rest = block.split("\n")
        p = head.split("\x1f")
        if len(p) < 5:
            continue
        sha, name, email, date, subject = p[:5]
        all_files = [c for c in (l.split("\t") for l in rest) if len(c) == 3 and c[0] != "-"]
        bulk = len(all_files) > BULK_FILES
        files = [{"add": int(c[0]), "del": int(c[1]), "path": c[2]}
                 for c in all_files if interesting(c[2])]
        out.append({"sha": sha, "name": name, "email": email, "date": parse_ts(date),
                    "subject": subject, "files": files, "bulk": bulk,
                    "nfiles": len(all_files)})
    return out


def build_identity(prs, commits):
    """GitHub PR author via the `(#1234)` squash-merge ref, falling back to email."""
    pr_author = {p["number"]: p["author"]["login"] for p in prs if p.get("author")}
    logins = {l.lower(): l for l in pr_author.values()}
    by_email, by_name = {}, {}
    for c in commits:
        m = PR_REF.search(c["subject"])
        if m and pr_author.get(int(m.group(1))):
            by_email.setdefault(c["email"], pr_author[int(m.group(1))])
            by_name.setdefault(c["name"], pr_author[int(m.group(1))])
    for c in commits:
        m = re.match(r"^(?:\d+\+)?([\w.-]+)@users\.noreply\.github\.com$", c["email"])
        if m:
            by_email.setdefault(c["email"], logins.get(m.group(1).lower(), m.group(1)))

    def resolve(c):
        m = PR_REF.search(c["subject"])
        if m and pr_author.get(int(m.group(1))):
            return pr_author[int(m.group(1))]
        return (by_email.get(c["email"]) or by_name.get(c["name"])
                or logins.get(c["name"].lower().replace(" ", ""), c["name"]))

    return resolve


IMPORT_PY = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M)
IMPORT_TS = re.compile(r"""(?:from|import\()\s*['"]([^'"]+)['"]""", re.M)


def build_fan_in(files):
    """Count import statements at HEAD referencing each module.

    Specifiers are matched on *trailing path segments*, so `foo/types` never
    matches `bar/types`.
    """
    refs = defaultdict(int)
    for path in files:
        try:
            with open(os.path.join(REPO, path), "r", errors="replace") as f:
                src = f.read(160_000)
        except OSError:
            continue
        if path.endswith(".py"):
            specs = [(a or b).replace(".", "/") for a, b in IMPORT_PY.findall(src)]
        else:
            specs = [
                os.path.normpath(os.path.join(os.path.dirname(path), s))
                if s.startswith(".") else s.lstrip("~@/")
                for s in IMPORT_TS.findall(src)
            ]
        for s in specs:
            refs[s.strip("/")] += 1
    return refs


def fan_in_for(path, refs):
    stem = re.sub(r"\.(py|tsx?|jsx?)$", "", path)
    cands = {stem}
    for suf in ("/index", "/__init__"):
        if stem.endswith(suf):
            cands.add(stem[: -len(suf)])
    segs = stem.split("/")
    for n in (2, 3, 4):
        if len(segs) >= n:
            cands.add("/".join(segs[-n:]))
    return sum(refs.get(c, 0) for c in cands)


def main():
    with open(os.path.join(CACHE_DIR, "prs.json")) as f:
        prs = json.load(f)
    prs = [p for p in prs if p.get("author") and not BOT_PAT.search(p["author"]["login"])]

    commits = load_commits()
    resolve = build_identity(prs, commits)
    for c in commits:
        c["login"] = resolve(c)
        c["is_fix"] = bool(FIX_PAT.search(c["subject"]))
        c["is_refactor"] = bool(REFACTOR_PAT.search(c["subject"]))
    commits = [c for c in commits if not BOT_PAT.search(c["login"])]
    commits.sort(key=lambda c: c["date"])
    print(f"loaded {len(commits)} commits, {len(prs)} PRs", file=sys.stderr)

    window = [c for c in commits if c["date"] >= score_cutoff]
    prev = [c for c in commits if prev_cutoff <= c["date"] < score_cutoff]
    win_prs = [p for p in prs if parse_ts(p["mergedAt"]) >= score_cutoff]
    prev_prs = [p for p in prs if prev_cutoff <= parse_ts(p["mergedAt"]) < score_cutoff]
    contributors = {c["login"] for c in window} | {p["author"]["login"] for p in win_prs}

    S = defaultdict(lambda: defaultdict(float))
    abs_ev, drag_ev = defaultdict(list), defaultdict(list)
    area_ct = defaultdict(lambda: defaultdict(int))

    for c in window:
        S[c["login"]]["commits"] += 1
        for t in {"/".join(f["path"].split("/")[:2]) for f in c["files"] if "/" in f["path"]}:
            area_ct[c["login"]][t] += 1
    for p in win_prs:
        S[p["author"]["login"]]["prs"] += 1
    for p in win_prs:
        seen = set()
        for r in (p.get("reviews") or {}).get("nodes", []):
            rl = (r.get("author") or {}).get("login")
            if rl and rl != p["author"]["login"] and rl not in seen and not BOT_PAT.search(rl):
                seen.add(rl)
                S[rl]["reviews"] += 1

    # ---------- file creation / modification history ----------
    created_by, created_at = {}, {}
    modifiers = defaultdict(set)
    deleted_by = {}
    for c in commits:
        if c["bulk"]:
            continue
        for f in c["files"]:
            p = f["path"]
            modifiers[p].add(c["login"])
            if p not in created_by and f["del"] == 0 and f["add"] > 0:
                created_by[p], created_at[p] = c["login"], c["date"]

    tracked = [p for p in git("ls-files").splitlines() if interesting(p)]
    tracked_set = set(tracked)
    print(f"fan-in over {len(tracked)} files", file=sys.stderr)
    refs = build_fan_in(tracked)

    # ---------- LEVERAGE ----------
    adopted_total = 0
    for path, owner in created_by.items():
        if created_at[path] < score_cutoff or owner not in contributors:
            continue
        if path not in tracked_set:
            continue
        fi = fan_in_for(path, refs)
        adopters = len(modifiers[path] - {owner})
        score = 0.0
        if fi >= 2:
            score += min(fi, 40) * 1.0
        score += adopters * 4.0
        if SHARED_LAYER.search(path) and (fi >= 2 or adopters > 0):
            score += 5.0
        score = min(score, 45.0)
        if score <= 0:
            continue
        S[owner]["leverage_raw"] += score
        S[owner]["files_adopted"] += 1
        S[owner]["fan_in"] += fi
        S[owner]["adopters"] += adopters
        if adopters >= 2:
            adopted_total += 1
        if score >= 8:
            abs_ev[owner].append({
                "path": path, "delta": f"+{score:.0f}", "sort": score,
                "note": f"fan-in {fi} · "
                        + (f"extended by {adopters} other engineer"
                           + ("s" if adopters != 1 else "")
                           if adopters else "no other authors yet")
                        + (" · shared layer" if SHARED_LAYER.search(path) else ""),
            })

    # ---------- DRAG ----------
    per_file_type = defaultdict(int)  # (login, path, kind) -> count, capped at 2

    def add_drag(login, path, kind, points, note, delta):
        key = (login, path, kind)
        if per_file_type[key] >= 2:
            return
        per_file_type[key] += 1
        S[login]["drag_raw"] += points
        S[login]["drag_events"] += 1
        S[login][kind] += 1
        drag_ev[login].append({"path": path, "delta": delta, "sort": points, "note": note})

    # Reverts: a commit starting with "Revert" naming the engineer's PR.
    pr_author = {p["number"]: p["author"]["login"] for p in prs if p.get("author")}
    for c in commits:
        if c["date"] < score_cutoff or not c["subject"].lower().startswith("revert"):
            continue
        for num in re.findall(r"#(\d+)", c["subject"]):
            victim = pr_author.get(int(num))
            if victim and victim != c["login"]:
                add_drag(victim, f"PR #{num}", "reverts", 15,
                         f"{c['login']} · {c['date'].date()} · {c['subject'][:78]}",
                         "revert")
                break

    # Deletions of a file its creator made, by someone else, within 30 days.
    for c in commits:
        if c["bulk"] or c["date"] < score_cutoff:
            continue
        for f in c["files"]:
            p = f["path"]
            if f["add"] == 0 and f["del"] > 0 and p not in tracked_set:
                owner = created_by.get(p)
                if (owner and owner != c["login"] and created_at.get(p)
                        and c["date"] - created_at[p] <= timedelta(days=DRAG_DAYS)):
                    add_drag(owner, p, "deleted", 8,
                             f"{c['login']} · {c['date'].date()} · deleted · {c['subject'][:60]}",
                             "deleted")

    touches = defaultdict(list)
    for c in commits:
        if not c["bulk"]:
            for f in c["files"]:
                touches[f["path"]].append((c, f))

    for path, seq in touches.items():
        for i, (c, f) in enumerate(seq):
            if c["date"] < score_cutoff or f["add"] < 20:
                continue
            login = c["login"]
            S[login]["lines_landed"] += f["add"]
            for c2, f2 in seq[i + 1:]:
                age = c2["date"] - c["date"]
                if age > timedelta(days=DRAG_DAYS):
                    break
                if f2["del"] == 0:
                    continue
                other = c2["login"] != login
                if (other and c2["is_fix"] and not c2["is_refactor"]
                        and age <= timedelta(days=HOTFIX_DAYS)):
                    add_drag(login, path, "hotfixed", 4,
                             f"{c2['login']} · {c2['date'].date()} · {c2['subject'][:70]}",
                             "hot-fix")
                if other and f2["del"] >= 0.6 * f["add"]:
                    pts = min(min(f2["del"], 400) / 30.0, 13.3)
                    S[login]["lines_reworked"] += min(f2["del"], f["add"])
                    add_drag(login, path, "rewritten", pts,
                             f"{c2['login']} · {c2['date'].date()} · {c2['subject'][:70]}",
                             f"−{int(f2['del'])}")
                if not other and f2["del"] >= 0.5 * f["add"]:
                    pts = min(min(f2["del"], 400) / 40.0, 10.0)
                    add_drag(login, path, "self_rewrite", pts,
                             f"self · {c2['date'].date()} · {c2['subject'][:70]}",
                             f"−{int(f2['del'])}")

    # ---------- hotspots ----------
    fix_by_file, top_author = defaultdict(int), defaultdict(lambda: defaultdict(int))
    for c in window:
        if c["bulk"]:
            continue
        for f in c["files"]:
            top_author[f["path"]][c["login"]] += 1
            if c["is_fix"]:
                fix_by_file[f["path"]] += 1
    hotspots = [{"path": p, "fixes": n,
                 "author": max(top_author[p], key=top_author[p].get)}
                for p, n in sorted(fix_by_file.items(), key=lambda kv: -kv[1])[:6]]

    # ---------- scores ----------
    engineers = []
    for login in contributors:
        s = S[login]
        if s["commits"] < MIN_COMMITS:
            continue
        leverage = round(s["leverage_raw"] ** DAMP)
        delivery = round(6 * math.sqrt(s["commits"]) + 3 * math.sqrt(s["prs"])
                         + 2.5 * math.sqrt(s["reviews"]))
        drag = round(s["drag_raw"] ** DAMP)
        areas = sorted(area_ct[login].items(), key=lambda kv: -kv[1])[:5]
        landed = s["lines_landed"]
        engineers.append({
            "login": login,
            "subtitle": " · ".join(a for a, _ in areas[:3]) or "—",
            "leverage": leverage, "delivery": delivery, "drag": drag,
            "net": leverage + delivery - drag,
            "leverage_raw": round(s["leverage_raw"]), "drag_raw": round(s["drag_raw"]),
            "commits": int(s["commits"]), "prs": int(s["prs"]), "reviews": int(s["reviews"]),
            "adopted": int(s["files_adopted"]), "fan_in": int(s["fan_in"]),
            "adopters": int(s["adopters"]), "drag_events": int(s["drag_events"]),
            "reverts": int(s["reverts"]), "deleted": int(s["deleted"]),
            "hotfixed": int(s["hotfixed"]), "rewritten": int(s["rewritten"]),
            "self_rewrite": int(s["self_rewrite"]),
            "lines_landed": int(landed),
            "rework_rate": round(100 * min(s["lines_reworked"], landed) / landed, 1) if landed else 0.0,
            "drag100": round(100 * drag / s["commits"], 1),
            "abs": [{k: v for k, v in e.items() if k != "sort"}
                    for e in sorted(abs_ev[login], key=lambda e: -e["sort"])[:6]],
            "rework": [{k: v for k, v in e.items() if k != "sort"}
                       for e in sorted(drag_ev[login], key=lambda e: -e["sort"])[:6]],
            "areas": [{"path": a, "delta": str(n), "note": "commits touching this tree"}
                      for a, n in areas],
            "top_area": areas[0][0] if areas else "—",
        })

    engineers.sort(key=lambda e: -e["net"])

    for i, e in enumerate(engineers):
        parts = [
            f"Raw leverage {e['leverage_raw']:,} across {e['adopted']:,} files they created "
            f"({e['fan_in']:,} import sites, {e['adopters']} later modifications by others) "
            f"→ {e['leverage']} after damping",
            f"delivery {e['delivery']} from {e['commits']:,} commits, {e['prs']:,} PRs and "
            f"{e['reviews']} reviews",
        ]
        if e["drag_raw"]:
            kinds = [f"{e[k]} {n}" for k, n in
                     (("reverts", "reverts"), ("deleted", "deletions"),
                      ("hotfixed", "hot-fixes"), ("rewritten", "rewrites by others"),
                      ("self_rewrite", "self-rewrites")) if e[k]]
            parts.append(f"raw drag {e['drag_raw']:,} across {e['drag_events']} events "
                         f"({', '.join(kinds)}) → {e['drag']} after damping")
        else:
            parts.append("no drag events in this window")
        e["verdict"] = (f"Rank {i + 1} by net impact, mostly in {e['top_area']}. "
                        + "; ".join(parts)
                        + f". Net = {e['leverage']} + {e['delivery']} − {e['drag']} = {e['net']}.")

    def pct(a, b):
        return "—" if not b else f"{'+' if a >= b else ''}{round(100 * (a - b) / b)}%"

    payload = {
        "generated_at": now.isoformat(), "repo": "PostHog/posthog",
        "window_days": SCORE_DAYS, "drag_window_days": DRAG_DAYS,
        "hotfix_window_days": HOTFIX_DAYS, "min_commits": MIN_COMMITS, "damp": DAMP,
        "head": git("log", "-1", "--format=%h %cI").strip(),
        "kpis": [
            {"label": "Commits", "value": len(window), "delta": pct(len(window), len(prev)),
             "good": len(window) >= len(prev), "note": "vs. prior 90 days"},
            {"label": "Pull requests merged", "value": len(win_prs),
             "delta": pct(len(win_prs), len(prev_prs)),
             "good": len(win_prs) >= len(prev_prs), "note": "authored by humans"},
            {"label": "Contributors", "value": len(contributors),
             "delta": f"{len(engineers)} ranked", "good": True,
             "note": f"≥{MIN_COMMITS} commits to be ranked"},
            {"label": "Abstractions adopted", "value": adopted_total, "delta": "2+ authors",
             "good": True, "note": "new files later modified by 2+ others"},
            {"label": "Drag events", "value": sum(e["drag_events"] for e in engineers),
             "delta": f"{DRAG_DAYS}d window", "good": False,
             "note": "reverts, deletions, hot-fixes, rewrites"},
        ],
        "hotspots": hotspots, "engineers": engineers,
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
