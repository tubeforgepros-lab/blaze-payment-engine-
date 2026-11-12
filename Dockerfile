FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BLAZE_DB_PATH=/data/blaze_ledgers.db

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 blaze \
 && mkdir -p /data \
 && chown -R blaze:blaze /app /data

USER blaze

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/health" || exit 1

# Schema is created/migrated at import time by server.py -> init_ledgers.setup_database()
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
