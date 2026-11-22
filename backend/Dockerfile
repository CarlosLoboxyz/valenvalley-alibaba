# Build context is the REPOSITORY ROOT (the backend imports ../scripts):
#   docker build -f backend/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first for layer caching
COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

COPY backend/__init__.py backend/__init__.py
COPY backend/app backend/app
COPY scripts scripts
COPY main.py ./

# Non-root runtime user; writable runtime dirs live on named volumes in compose
RUN useradd --system --uid 10001 --home-dir /app app \
 && mkdir -p /app/output /app/debug_raw \
 && chown -R app:app /app/output /app/debug_raw
USER app

ENV OUTPUT_DIR=/app/output \
    DEBUG_DIR=/app/debug_raw

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"

# EXACTLY ONE worker: job manager, job guard and cache locks are in-process state.
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
