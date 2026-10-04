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

# libsql 0.1.11 publishes wheels for cp38-cp314, including cp314 on manylinux.
# The image stays on 3.13 (Dockerfile FROM python:3.13-slim) because that is the
# interpreter the suite is tested against, not because a wheel is missing - an
# earlier version of this comment claimed there is no cp314 wheel, which was
# wrong. Note the wheel set has no cp314 for Windows, so 3.14 is a Linux-only
# option.
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