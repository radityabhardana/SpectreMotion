# SpectreMotion — production image (multi-stage, non-root).
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg + fonts are the only heavy system deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- deps layer (cached) ----
FROM base AS deps
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---- runtime ----
FROM base AS runtime
COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin
COPY . .

# Non-root user; owns the data dir so SQLite + uploads are writable.
RUN useradd --create-home --uid 10001 spectre \
    && mkdir -p /app/data \
    && chown -R spectre:spectre /app
USER spectre

ENV HOST=0.0.0.0 \
    PORT=8080 \
    USE_IONICE=0 \
    SUBTITLE_FONT_PATH=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf

EXPOSE 8080

# Default command runs the web app; the worker runs as a second service.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.web:app", "--host", "0.0.0.0", "--port", "8080"]
