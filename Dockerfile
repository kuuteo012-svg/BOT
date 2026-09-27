FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1

# FFmpeg is required for video OCR/frame extraction. Build tools cover packages
# that may not have a prebuilt wheel for the selected Python version.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        gcc \
        g++ \
        make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r requirements.txt

COPY . .

# The bot connects to an already-running Edge over CDP; it does not launch a
# browser inside this image. For Docker on Linux, run with --network host and
# keep EDGE_CDP_HOST=127.0.0.1, or use host.docker.internal plus:
#   --add-host=host.docker.internal:host-gateway
RUN useradd --create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /app/data /app/logs /app/backups \
    && chown -R bot:bot /app
USER bot

# Mount .env, Telegram session, data, logs, and browser profile at runtime.
VOLUME ["/app/data", "/app/logs", "/app/backups", "/app/edge_bot_profile"]

STOPSIGNAL SIGINT
ENTRYPOINT ["python", "-u", "main_script.py"]
