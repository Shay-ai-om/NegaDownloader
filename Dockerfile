FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.source="https://github.com/Shay-ai-om/NegaDownloader" \
      org.opencontainers.image.description="Self-hosted web interface for yt-dlp"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NEGADOWNLOADER_PASSWORD="" \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DISPLAY=:99

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl ffmpeg xvfb openbox x11vnc novnc websockify \
      fonts-liberation fonts-noto-color-emoji gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium

COPY app ./app
COPY templates ./templates
COPY static ./static
COPY entrypoint.sh ./entrypoint.sh
RUN chmod 755 /app/entrypoint.sh \
    && mkdir -p /app/data /app/download \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin negadownloader \
    && chown -R negadownloader:negadownloader /app /ms-playwright

EXPOSE 8080
VOLUME ["/app/data", "/app/download"]
ENTRYPOINT ["/app/entrypoint.sh"]
