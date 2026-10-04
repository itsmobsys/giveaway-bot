# Python Discord bot image.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first so edits to the source do not bust the layer cache.
COPY bot/pyproject.toml ./bot/pyproject.toml
COPY bot/README.md ./bot/README.md
COPY giveaway_bot ./giveaway_bot
COPY shared/migrations ./shared/migrations

RUN pip install --upgrade pip && pip install ./bot

# Unprivileged runtime user.
RUN useradd --create-home --shell /usr/sbin/nologin botuser \
    && mkdir -p /app/data && chown -R botuser:botuser /app
USER botuser

VOLUME ["/app/data"]

HEALTHCHECK --interval=60s --timeout=30s --start-period=30s --retries=3 \
  CMD ["python", "-m", "giveaway_bot", "doctor"]

# Run in the background so the container healthcheck can execute.
CMD ["python", "-m", "giveaway_bot", "run"]