"""Compute engineer impact metrics for the PostHog repo.

Impact = w_leverage*Leverage + w_delivery*Delivery - w_drag*Drag

Every sub-metric is emitted with its raw count, its normalized 0-100 score, and
concrete evidence, so the dashboard can always explain a number.
"""

import json
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

BOT_PAT = re.compile(
    r"(\[bot\]|dependabot|renovate|github-actions|posthog-bot|snyk|codecov|"
    r"sentry-io|greenkeeper|imgbot|-bot$)",
    re.I,
)

CODE_EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".sql")
INFRA_PAT = re.compile(
    r"(^\.github/|^bin/|^docker|Dockerfile|^ci/|conftest\.py$|"
    r"^\.circleci/|jest\.config|playwright|^\.pre-commit|pyproject\.toml$|"
    r"package\.json$|tsconfig|webpack|vite\.config|Makefile$)",
    re.I,
)
FIX_PAT = re.compile(r"\b(fix|hotfix|revert|patch|repair|broke|broken|regression)\b", re.I)

now = datetime.now(timezone.utc)
score_cutoff = now - timedelta(days=SCORE_DAYS)


def git(*args, cwd=REPO):
    return subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, errors="replace"
    ).stdout


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- git history
def load_commits():
    """Commits in the last 150 days with author, date, subject, and file stats."""
    sep = "\x1e"
    raw = git(
        "log",
        "--no-merges",
        f"--since={(now - timedelta(days=150)).date()}",
        f"--pretty=format:{sep}%H%x1f%an%x1f%ae%x1f%aI%x1f%s",
        "--numstat",
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
        files = []
        for line in rest:
            cols = line.split("\t")
            if len(cols) == 3 and cols[0] != "-":
                files.append(
                    {"add": int(cols[0]), "del": int(cols[1]), "path": cols[2]}
                )
        commits.append(
            {
                "sha": sha,
                "name": name,
                "email": email,
                "date": parse_ts(date),
                "subject": subject,
                "files": files,
            }
        )
    return commits


# ------------------------------------------------------------------- identity
def build_identity(prs, commits):
    """Map commit email/name -> canonical GitHub login where possible."""
    email_to_login = {}
    name_to_login = {}
    for p in prs:
        a = (p.get("author") or {}).get("login")
        if not a:
            continue
        name_to_login.setdefault(a.lower(), a)
    # git noreply emails encode the login directly
    for c in commits:
        m = re.match(r"^(?:\d+\+)?([\w.-]+)@users\.noreply\.github\.com$", c["email"])
        if m:
            email_to_login[c["email"]] = m.group(1)

    def resolve(c):
        if c["email"] in email_to_login:
            return email_to_login[c["email"]]
        key = c["name"].lower().replace(" ", "")
        if key in name_to_login:
            return name_to_login[key]
        return c["name"]

    return resolve


# --------------------------------------------------------------- import graph
IMPORT_RE = {
    "py": re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.M),
    "ts": re.compile(r"""(?:from|import)\s+['"]([^'"]+)['"]""", re.M),
}


def build_import_graph(scoped_files):
    """Map module-ish key -> set of files importing it (regex, not AST)."""
    importers = defaultdict(set)
    for path in scoped_files:
        full = os.path.join(REPO, path)
        try:
            with open(full, "r", errors="replace") as f:
                src = f.read(200_000)
        except OSError:
            continue
        if path.endswith(".py"):
            for a, b in IMPORT_RE["py"].findall(src):
                mod = a or b
                importers[mod.replace(".", "/")].add(path)
        elif path.endswith((".ts", ".tsx", ".js", ".jsx")):
            for spec in IMPORT_RE["ts"].findall(src):
                if spec.startswith("."):
                    base = os.path.normpath(os.path.join(os.path.dirname(path), spec))
                else:
                    base = spec.lstrip("~@/")
                importers[base].add(path)
    return importers


def file_keys(path):
    """Candidate import keys a file could be referenced by."""
    stem = re.sub(r"\.(py|tsx?|jsx?)$", "", path)
    keys = {stem}
    if stem.endswith("/index"):
        keys.add(stem[: -len("/index")])
    if stem.endswith("/__init__"):
        keys.add(stem[: -len("/__init__")])
    # also allow suffix matching on the last 2-3 segments
    segs = stem.split("/")
    for n in (2, 3):
        if len(segs) >= n:
            keys.add("/".join(segs[-n:]))
    return keys


# ------------------------------------------------------------------- analysis
def main():
    with open(os.path.join(CACHE_DIR, "prs.json")) as f:
        prs = json.load(f)
    prs = [p for p in prs if p.get("author") and not BOT_PAT.search(p["author"]["login"])]

    commits = load_commits()
    resolve = build_identity(prs, commits)
    for c in commits:
        c["login"] = resolve(c)
    commits = [c for c in commits if not BOT_PAT.search(c["login"])]
    commits.sort(key=lambda c: c["date"])

    scored_commits = [c for c in commits if c["date"] >= score_cutoff]
    active = {c["login"] for c in scored_commits}
    active |= {
        p["author"]["login"]
        for p in prs
        if parse_ts(p["mergedAt"]) >= score_cutoff
    }

    E = {
        login: {
            "login": login,
            "raw": defaultdict(float),
            "evidence": defaultdict(list),
        }
        for login in active
    }

    def ev(login, key, text, limit=4):
        if login in E and len(E[login]["evidence"][key]) < limit:
            E[login]["evidence"][key].append(text)

    # ---------------- area churn (risk weighting) -----------------
    dir_churn = defaultdict(int)
    dir_fixes = defaultdict(int)
    for c in commits:
        is_fix = bool(FIX_PAT.search(c["subject"]))
        for f in c["files"]:
            d = "/".join(f["path"].split("/")[:2])
            dir_churn[d] += 1
            if is_fix:
                dir_fixes[d] += 1
    max_churn = max(dir_churn.values() or [1])

    def difficulty(path):
        d = "/".join(path.split("/")[:2])
        churn = dir_churn.get(d, 0) / max_churn
        fixrate = dir_fixes.get(d, 0) / max(dir_churn.get(d, 1), 1)
        return 1.0 + 1.5 * churn + 1.5 * fixrate

    # ---------------- DELIVERY ------------------------------------
    for p in prs:
        login = p["author"]["login"]
        if login not in E or parse_ts(p["mergedAt"]) < score_cutoff:
            continue
        e = E[login]
        e["raw"]["prs_merged"] += 1
        paths = [f["path"] for f in (p.get("files") or {}).get("nodes", [])]
        diff = max((difficulty(x) for x in paths), default=1.0)
        e["raw"]["difficulty_sum"] += diff
        size = p["additions"] + p["deletions"]
        bucket = "S" if size < 50 else ("M" if size < 400 else "L")
        e["raw"][f"pr_size_{bucket}"] += 1
        e["raw"]["risk_weighted_prs"] += diff
        if p.get("closingIssuesReferences", {}).get("totalCount", 0):
            e["raw"]["issues_closed"] += 1
        if diff > 2.0:
            top = "/".join(paths[0].split("/")[:2]) if paths else "?"
            ev(login, "risk_weighted_prs",
               f"#{p['number']} “{p['title'][:70]}” in {top} "
               f"(difficulty ×{diff:.2f}, {len(paths)} files)")

    # reviews given
    for p in prs:
        author = p["author"]["login"]
        seen = set()
        for r in (p.get("reviews") or {}).get("nodes", []):
            ra = (r.get("author") or {})
            rl = ra.get("login")
            if not rl or rl == author or rl in seen or BOT_PAT.search(rl):
                continue
            if not r.get("submittedAt") or parse_ts(r["submittedAt"]) < score_cutoff:
                continue
            seen.add(rl)
            if rl not in E:
                continue
            E[rl]["raw"]["reviews_given"] += 1
            E[rl]["raw"].setdefault("review_authors", 0)
            E[rl]["evidence"]["_review_authors_set"] = E[rl]["evidence"].get(
                "_review_authors_set", []
            )
            if author not in E[rl]["evidence"]["_review_authors_set"]:
                E[rl]["evidence"]["_review_authors_set"].append(author)

    for login, e in E.items():
        e["raw"]["review_authors"] = len(e["evidence"].pop("_review_authors_set", []))

    for c in scored_commits:
        if c["login"] in E:
            E[c["login"]]["raw"]["commits"] += 1

    # ---------------- LEVERAGE ------------------------------------
    tracked = [
        p
        for p in git("ls-files").splitlines()
        if p.endswith(CODE_EXT)
        and p.startswith(("posthog/", "frontend/src/", "plugin-server/src/", "products/",
                          "rust/", "ee/", "common/"))
    ]
    tracked_set = set(tracked)
    importers = build_import_graph(tracked)

    # who authored / last-touched each file, from history
    file_authors = defaultdict(lambda: defaultdict(int))
    file_created_by = {}
    for c in commits:
        for f in c["files"]:
            file_authors[f["path"]][c["login"]] += f["add"]
    # creation attribution: earliest commit in our window that added the file
    for c in commits:
        for f in c["files"]:
            if f["path"] not in file_created_by and f["del"] == 0 and f["add"] > 0:
                file_created_by[f["path"]] = c["login"]

    for path in tracked_set:
        authors = file_authors.get(path)
        if not authors:
            continue
        owner = max(authors, key=authors.get)
        if owner not in E:
            continue
        total_lines = sum(authors.values())
        ownership = authors[owner] / max(total_lines, 1)

        dependents = set()
        for k in file_keys(path):
            dependents |= importers.get(k, set())
        dependents.discard(path)
        if not dependents:
            continue

        dep_authors = set()
        for d in dependents:
            da = file_authors.get(d)
            if da:
                dep_authors.add(max(da, key=da.get))
        dep_authors.discard(owner)
        modules = {d.split("/")[0] + "/" + (d.split("/")[1] if "/" in d[d.find("/") + 1:] else "")
                   for d in dependents}

        e = E[owner]
        if len(dep_authors) >= 1:
            e["raw"]["foundation_dependents"] += len(dependents)
            e["raw"]["foundation_dep_authors"] += len(dep_authors)
            if len(dependents) >= 3:
                ev(owner, "foundation_dependents",
                   f"{path} — imported by {len(dependents)} files "
                   f"from {len(dep_authors)} other engineers")
        e["raw"]["module_reach"] = max(e["raw"]["module_reach"], len(modules))
        if ownership > 0.6 and len(dependents) >= 5:
            e["raw"]["bus_factor_files"] += 1
            ev(owner, "bus_factor_files",
               f"{path} — {ownership:.0%} of added lines are theirs, "
               f"{len(dependents)} files depend on it")

    # unblocking: infra/CI/test-tooling work
    for c in scored_commits:
        if c["login"] not in E:
            continue
        infra = [f for f in c["files"] if INFRA_PAT.search(f["path"])]
        if infra:
            E[c["login"]]["raw"]["unblocking_commits"] += 1
            ev(c["login"], "unblocking_commits",
               f"{c['subject'][:78]} ({len(infra)} infra files)")

    # ---------------- DRAG ----------------------------------------
    by_sha = {c["sha"]: c for c in commits}
    revert_re = re.compile(r'Revert\s+"(.+?)"')
    subj_to_commit = defaultdict(list)
    for c in commits:
        subj_to_commit[c["subject"]].append(c)

    for c in commits:
        m = revert_re.search(c["subject"])
        if not m:
            continue
        for orig in subj_to_commit.get(m.group(1), []):
            if orig["date"] < c["date"] and orig["login"] in E:
                E[orig["login"]]["raw"]["reverts"] += 1
                ev(orig["login"], "reverts",
                   f"“{orig['subject'][:60]}” reverted by {c['login']} "
                   f"after {(c['date'] - orig['date']).days}d")

    # hotfix chains + 30-day churn-by-others
    file_touch = defaultdict(list)  # path -> [(date, login, sha, subject)]
    for c in commits:
        for f in c["files"]:
            file_touch[f["path"]].append((c["date"], c["login"], c["sha"], c["subject"], f))

    for path, touches in file_touch.items():
        touches.sort(key=lambda t: t[0])
        for i, (date, login, sha, subj, f) in enumerate(touches):
            if login not in E or date < score_cutoff or f["add"] == 0:
                continue
            E[login]["raw"]["lines_landed"] += f["add"]
            window_end = date + timedelta(days=DRAG_DAYS)
            for date2, login2, sha2, subj2, f2 in touches[i + 1:]:
                if date2 > window_end:
                    break
                if login2 == login:
                    continue
                # lines of theirs removed by someone else soon after
                E[login]["raw"]["lines_reworked_by_others"] += min(f2["del"], f["add"])
                if FIX_PAT.search(subj2):
                    E[login]["raw"]["hotfixes_by_others"] += 1
                    ev(login, "hotfixes_by_others",
                       f"{path.split('/')[-1]}: {login2} shipped "
                       f"“{subj2[:52]}” {(date2 - date).days}d later")
                    break

    for login, e in E.items():
        landed = e["raw"]["lines_landed"]
        reworked = min(e["raw"]["lines_reworked_by_others"], landed)
        e["raw"]["rework_rate_pct"] = 100 * reworked / landed if landed else 0.0
        e["raw"]["survival_rate_pct"] = 100 - e["raw"]["rework_rate_pct"]

    # ---------------- normalize & score ---------------------------
    def pct_rank(values):
        """value -> 0..100 percentile within cohort."""
        srt = sorted(values)
        n = len(srt)

        def rank(v):
            if n <= 1:
                return 50.0
            below = sum(1 for x in srt if x < v)
            equal = sum(1 for x in srt if x == v)
            return 100.0 * (below + 0.5 * equal) / n

        return rank

    METRICS = {
        "leverage": [
            ("foundation_dep_authors", "Foundation reach",
             "Distinct other engineers whose files import code this person owns."),
            ("foundation_dependents", "Files built on their code",
             "Total files importing modules where this person is the dominant author."),
            ("module_reach", "Cross-module reach",
             "How many distinct top-level modules depend on their code."),
            ("bus_factor_files", "Load-bearing ownership",
             "Files they authored >60% of that 5+ other files depend on."),
            ("unblocking_commits", "Unblocking work",
             "Commits touching CI, build tooling, or test infrastructure — small diffs, team-wide effect."),
        ],
        "delivery": [
            ("risk_weighted_prs", "Risk-weighted PRs",
             "Merged PRs, each multiplied by how churn-prone and fix-prone its area is."),
            ("prs_merged", "PRs merged", "Raw count of merged pull requests in the window."),
            ("reviews_given", "Reviews given", "Reviews submitted on other people's PRs."),
            ("review_authors", "Engineers reviewed for",
             "Distinct colleagues whose PRs they reviewed."),
            ("issues_closed", "PRs closing issues",
             "Merged PRs that closed a tracked issue."),
        ],
        "drag": [
            ("rework_rate_pct", "Rework rate",
             "% of lines they landed that someone else deleted or rewrote within 30 days."),
            ("hotfixes_by_others", "Hotfixes by others",
             "Times a colleague shipped a fix to a file within 30 days of their change."),
            ("reverts", "Reverts", "Their commits later reverted by someone."),
        ],
    }

    logins = list(E)
    rankers = {}
    for pillar, metrics in METRICS.items():
        for key, _, _ in metrics:
            rankers[key] = pct_rank([E[l]["raw"][key] for l in logins])

    out_engineers = []
    for login in logins:
        e = E[login]
        rec = {"login": login, "pillars": {}, "raw": {}}
        for pillar, metrics in METRICS.items():
            items = []
            for key, label, desc in metrics:
                raw = round(e["raw"][key], 2)
                items.append(
                    {
                        "key": key,
                        "label": label,
                        "description": desc,
                        "raw": raw,
                        "score": round(rankers[key](e["raw"][key]), 1),
                        "evidence": e["evidence"].get(key, []),
                    }
                )
            rec["pillars"][pillar] = {
                "score": round(sum(i["score"] for i in items) / len(items), 1),
                "metrics": items,
            }
        rec["raw"] = {k: round(v, 2) for k, v in e["raw"].items()}
        out_engineers.append(rec)

    W = {"leverage": 0.45, "delivery": 0.35, "drag": 0.20}
    for r in out_engineers:
        r["impact"] = round(
            W["leverage"] * r["pillars"]["leverage"]["score"]
            + W["delivery"] * r["pillars"]["delivery"]["score"]
            - W["drag"] * r["pillars"]["drag"]["score"],
            1,
        )
    out_engineers.sort(key=lambda r: -r["impact"])

    payload = {
        "generated_at": now.isoformat(),
        "window_days": SCORE_DAYS,
        "drag_window_days": DRAG_DAYS,
        "repo": "PostHog/posthog",
        "totals": {
            "engineers": len(out_engineers),
            "commits": len(scored_commits),
            "prs": len([p for p in prs if parse_ts(p["mergedAt"]) >= score_cutoff]),
        },
        "default_weights": W,
        "engineers": out_engineers,
    }
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(payload, f)
    print(f"wrote {OUT}: {len(out_engineers)} engineers", file=sys.stderr)
    for r in out_engineers[:8]:
        print(f"  {r['impact']:6.1f}  {r['login']}", file=sys.stderr)


if __name__ == "__main__":
    main()
