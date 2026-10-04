# Python Discord bot image.
#
# Build context is the repository root (see render.yaml dockerContext).
FROM python:3.13-slim

# PYTHONPATH runs the package from source instead of installing it. That matters:
# the default migrations directory is derived from the source tree
# (bot/giveaway_bot/../.. -> shared/migrations), so an installed copy inside
# site-packages would look for migrations that are not there. With this layout
# the path resolves on its own and MIGRATIONS_DIR does not need setting.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/bot

WORKDIR /app

# Dependencies first, so editing the source does not bust the layer cache.
# 3.13 rather than 3.14 because the Turso driver (libsql) is a Rust extension with
# wheels for cp311-cp313 only, and has to be compiled from source on 3.14.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# The previous image used `COPY giveaway_bot ./giveaway_bot`, which has no source
# at the repository root - the package lives at bot/giveaway_bot - so the build
# failed outright.
COPY bot/ ./bot/
COPY shared/ ./shared/
COPY app.py ./app.py

# Unprivileged runtime user.
RUN useradd --create-home --shell /usr/sbin/nologin botuser \
    && mkdir -p /app/data && chown -R botuser:botuser /app
USER botuser

VOLUME ["/app/data"]

HEALTHCHECK --interval=60s --timeout=30s --start-period=30s --retries=3 \
  CMD ["python", "-m", "giveaway_bot", "doctor"]

# The healthcheck needs to finish, so the bot itself is not the foreground command.
CMD ["python", "-m", "giveaway_bot", "run"]