# Rust Discord bot image. Build context is the repository root.
#
# Two stages: a full Rust toolchain builds the binary, and the runtime image
# carries only the binary plus CA roots. That keeps the deploy small and means
# no compiler or cargo registry lands in the shipped layer.

FROM rust:1-slim-bookworm AS builder

WORKDIR /build

# libsql links a native SQLite for its local backend and needs pkg-config to
# find it; ca-certificates is needed by the crate downloads themselves.
RUN apt-get update \
    && apt-get install -y --no-install-recommends pkg-config ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY rust/ ./

# --locked fails the build if Cargo.lock is out of date rather than silently
# resolving new versions during a deploy.
RUN cargo build --release --locked -p gwbot-cli

FROM debian:bookworm-slim

# Turso is reached over TLS, so the runtime needs trust roots. Nothing else is
# installed: the health probe is a subcommand of the bot binary, not curl.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin botuser

COPY --from=builder /build/target/release/giveaway-bot /usr/local/bin/giveaway-bot

ENV PORT=10000
EXPOSE 10000

# Unprivileged runtime user.
USER botuser

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD ["giveaway-bot", "healthcheck"]

# Binds $PORT itself (/health, /ready) alongside the Discord gateway.
CMD ["giveaway-bot", "run"]
