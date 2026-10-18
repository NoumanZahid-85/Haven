# ── Stage 1: builder ──────────────────────────────────────────────────
FROM python:3.11-slim AS builder
WORKDIR /app

# Pre-generated lockfile export (`uv export --no-dev -o requirements.txt`)
# so the image needs no uv and resolves nothing at build time.
COPY requirements.txt ./
RUN pip install --no-cache-dir --timeout 300 -r requirements.txt

# ── Stage 2: lean runtime image ────────────────────────────────────────
FROM python:3.11-slim AS runtime
WORKDIR /app

# Pure-Python voice stack (WebSocket relays to Gemini Live / Deepgram):
# no native audio libraries needed.
RUN adduser --disabled-password --no-create-home --gecos "" appuser

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/app

# chown runs LAST — COPY always writes as root.
RUN chown -R appuser /app
USER appuser

EXPOSE 8000

# Shell form so ${WEB_CONCURRENCY} expands (Render sets it; 1 on free tier).
# Voice sessions hold in-process state, so one worker per instance.
CMD gunicorn app.main:app \
    --workers ${WEB_CONCURRENCY:-1} \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind 0.0.0.0:8000 \
    --timeout 120 \
    --graceful-timeout 10 \
    --access-logfile - \
    --error-logfile -
