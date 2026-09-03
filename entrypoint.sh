#!/usr/bin/env bash
# Railway startup. Everything durable lives on the /data volume, so the clone and
# analysis happen once on first boot and every later restart is instant.
set -uo pipefail

DATA_DIR="${DATA_DIR:-/data}"
REPO_DIR="${REPO_DIR:-$DATA_DIR/repo}"
CACHE_DIR="${CACHE_DIR:-$DATA_DIR/cache}"
METRICS_PATH="${METRICS_PATH:-$DATA_DIR/metrics.json}"
SERVE_DIR=/app/serve
PORT="${PORT:-8080}"

mkdir -p "$DATA_DIR" "$CACHE_DIR" "$SERVE_DIR"

# Set REBUILD=1 in Railway to force a fresh PR pull + re-analysis on next deploy
# (the cloned repo is kept — only derived artifacts are dropped).
# REBUILD=1 drops the derived metrics and forces re-analysis, keeping the cloned
# repo and the weekly PR chunks. REBUILD=all additionally discards the chunks and
# re-downloads every week from GitHub.
case "${REBUILD:-0}" in
  all)
    echo "[boot] REBUILD=all — discarding weekly PR chunks and metrics"
    rm -rf "$CACHE_DIR/prs" "$CACHE_DIR/prs.json" "$METRICS_PATH" ;;
  1)
    echo "[boot] REBUILD=1 — dropping metrics, keeping cached PR weeks"
    rm -f "$METRICS_PATH" ;;
esac

cp /app/web/index.html "$SERVE_DIR/index.html"

# Serve immediately so Railway's healthcheck passes while the pipeline warms up.
link_metrics() {
  [ -f "$METRICS_PATH" ] && cp -f "$METRICS_PATH" "$SERVE_DIR/metrics.json"
}
# A precomputed metrics.json ships in the image, so the dashboard has real data
# the moment it deploys instead of waiting out the first-boot clone.
if [ ! -f "$METRICS_PATH" ] && [ -f /app/web/metrics.json ]; then
  echo "[boot] seeding metrics.json from image"
  cp /app/web/metrics.json "$METRICS_PATH"
  touch "$DATA_DIR/.seeded"
fi
link_metrics
(cd "$SERVE_DIR" && python3 -m http.server "$PORT" --bind 0.0.0.0) &
SERVER_PID=$!
echo "[boot] serving on :$PORT (pid $SERVER_PID)"

build() {
  # 1. Clone once onto the volume; reuse it on every subsequent boot.
  if [ -d "$REPO_DIR/.git" ]; then
    echo "[boot] repo already on volume, reusing $REPO_DIR"
    git -C "$REPO_DIR" fetch --quiet origin 2>/dev/null || true
  else
    echo "[boot] first boot — cloning PostHog/posthog into $REPO_DIR"
    rm -rf "$REPO_DIR"
    # No blob filter: `git log --numstat` needs blob contents, and a blobless
    # clone would lazily fetch them one commit at a time (unusably slow).
    git clone --shallow-since="$(date -u -d '150 days ago' +%Y-%m-%d 2>/dev/null || date -u -v-150d +%Y-%m-%d)" \
      https://github.com/PostHog/posthog.git "$REPO_DIR" || {
        echo "[boot] clone failed"; return 1; }
  fi

  # 2. PR/review data. Always run: weeks already on the volume are skipped, so
  #    this only fetches weeks that are missing plus the current (still-growing)
  #    one. An interrupted run resumes here instead of starting over.
  echo "[boot] syncing PRs from GitHub (cached weeks are skipped)"
  before=$(ls "$CACHE_DIR/prs" 2>/dev/null | wc -l | tr -d ' ')
  python3 /app/pipeline/fetch_github.py || { echo "[boot] fetch failed"; return 1; }
  after=$(ls "$CACHE_DIR/prs" 2>/dev/null | wc -l | tr -d ' ')
  echo "[boot] weekly PR chunks on volume: $before -> $after"

  # 3. Analysis. The metrics.json baked into the image is only a placeholder so
  #    the page has data on first boot; it must not suppress the real run.
  if [ -f "$METRICS_PATH" ] && [ ! -f "$DATA_DIR/.seeded" ] && [ "$after" = "$before" ]; then
    echo "[boot] metrics.json is current, skipping analysis"
  else
    echo "[boot] running analysis"
    python3 /app/pipeline/analyze.py || { echo "[boot] analysis failed"; return 1; }
    rm -f "$DATA_DIR/.seeded"
  fi

  link_metrics
  echo "[boot] ready"
}

build || echo "[boot] pipeline incomplete — serving whatever metrics exist"
wait "$SERVER_PID"
