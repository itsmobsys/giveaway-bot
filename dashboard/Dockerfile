# syntax=docker/dockerfile:1
# Dashboard image for Render (and any Docker host).
# Multi-stage so the runtime layer carries no build tooling.

# --- deps -------------------------------------------------------------------
FROM node:22-alpine AS deps
WORKDIR /app
# libc6-compat keeps native musl builds (sharp, etc.) working on Alpine.
RUN apk add --no-cache libc6-compat
COPY package.json package-lock.json* ./
RUN npm ci

# --- build ------------------------------------------------------------------
FROM node:22-alpine AS builder
WORKDIR /app
RUN apk add --no-cache libc6-compat
COPY --from=deps /app/node_modules ./node_modules
COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
ENV NODE_ENV=production
RUN npm run build

# --- runtime ----------------------------------------------------------------
FROM node:22-alpine AS runner
WORKDIR /app
RUN apk add --no-cache libc6-compat && addgroup -g 1001 -S nodejs && adduser -S nextjs -u 1001
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000

COPY --from=builder /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json
# Migrations live outside the image normally; the compose file mounts them.
COPY --from=builder /app/scripts ./scripts
COPY --from=builder /app/src/lib ./src/lib

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/api/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "node_modules/next/dist/bin/next", "start", "--port", "3000"]