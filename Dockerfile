FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pipeline/ /app/pipeline/
COPY web/ /app/web/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV DATA_DIR=/data \
    REPO_DIR=/data/repo \
    CACHE_DIR=/data/cache \
    METRICS_PATH=/data/metrics.json \
    PORT=8080

EXPOSE 8080
CMD ["/app/entrypoint.sh"]
