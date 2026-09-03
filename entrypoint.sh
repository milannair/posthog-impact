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
if [ "${REBUILD:-0}" = "1" ]; then
  echo "[boot] REBUILD=1 — dropping cached PR data and metrics"
  rm -f "$CACHE_DIR/prs.json" "$METRICS_PATH"
fi

cp /app/web/index.html "$SERVE_DIR/index.html"

# Serve immediately so Railway's healthcheck passes while the pipeline warms up.
link_metrics() {
  [ -f "$METRICS_PATH" ] && cp -f "$METRICS_PATH" "$SERVE_DIR/metrics.json"
}
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

  # 2. PR/review data (cached on the volume).
  if [ -f "$CACHE_DIR/prs.json" ]; then
    echo "[boot] PR cache present, skipping GitHub fetch"
  else
    echo "[boot] fetching PRs from GitHub"
    python3 /app/pipeline/fetch_github.py || { echo "[boot] fetch failed"; return 1; }
  fi

  # 3. Analysis.
  if [ -f "$METRICS_PATH" ]; then
    echo "[boot] metrics.json present, skipping analysis"
  else
    echo "[boot] running analysis"
    python3 /app/pipeline/analyze.py || { echo "[boot] analysis failed"; return 1; }
  fi

  link_metrics
  echo "[boot] ready"
}

build || echo "[boot] pipeline incomplete — serving whatever metrics exist"
wait "$SERVER_PID"
