# Python Discord bot image. Build context is the repository root.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/bot

WORKDIR /app

COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY bot/ ./bot/
COPY app.py ./app.py

# Unprivileged runtime user.
RUN useradd --create-home --shell /usr/sbin/nologin botuser \
    && chown -R botuser:botuser /app
USER botuser

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','10000')+'/health')"]

# Binds $PORT itself (/health) alongside the Discord gateway.
CMD ["python", "-m", "giveaway_bot", "run"]
