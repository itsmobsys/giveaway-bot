# syntax=docker/dockerfile:1
# Dashboard image for Render (and any Docker host).
# Multi-stage so the runtime layer carries no build tooling.
#
# Build context is the REPOSITORY ROOT (see render.yaml dockerContext and
# docker-compose.yml): the dashboard needs shared/migrations baked in, and a
# ./dashboard-only context cannot reach ../shared.

# --- deps -------------------------------------------------------------------
FROM node:22-alpine AS deps
WORKDIR /app/dashboard
# libc6-compat keeps native musl builds (sharp, etc.) working on Alpine.
RUN apk add --no-cache libc6-compat
COPY dashboard/package.json dashboard/package-lock.json* ./
RUN npm ci

# --- build ------------------------------------------------------------------
FROM node:22-alpine AS builder
WORKDIR /app/dashboard
RUN apk add --no-cache libc6-compat
COPY --from=deps /app/dashboard/node_modules ./node_modules
COPY dashboard/. ./
# Bake the shared migrations in so `npm run db:migrate` works without a mount.
COPY shared/migrations /app/migrations
ENV NEXT_TELEMETRY_DISABLED=1
ENV NODE_ENV=production
RUN npm run build

# --- runtime ----------------------------------------------------------------
FROM node:22-alpine AS runner
WORKDIR /app/dashboard
RUN apk add --no-cache libc6-compat && addgroup -g 1001 -S nodejs && adduser -S nextjs -u 1001
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV MIGRATIONS_DIR=/app/migrations

# dashboard/public is required for the build (tracked via .gitkeep).
COPY --from=builder /app/dashboard/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/dashboard/.next ./.next
COPY --from=builder /app/dashboard/node_modules ./node_modules
COPY --from=builder /app/dashboard/package.json ./package.json
# Migrations live in the image now; a compose mount may still override them.
COPY --from=builder /app/migrations /app/migrations
COPY --from=builder /app/dashboard/scripts ./scripts
COPY --from=builder /app/dashboard/src/lib ./src/lib

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "node_modules/next/dist/bin/next", "start", "--port", "3000"]
