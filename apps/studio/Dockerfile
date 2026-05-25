# Lion Studio — backend (FastAPI) + frontend (Next.js) in one image
#
# Build from repo root:
#   docker build -f apps/studio/Dockerfile -t lion-studio .
#
# Run:
#   docker run -p 8765:8765 -p 3000:3000 -v ~/.lionagi:/root/.lionagi lion-studio

# ── Stage 1: Build frontend ──────────────────────────────────────────────────
FROM node:22-slim AS frontend-build

WORKDIR /build
COPY apps/studio/frontend/package.json apps/studio/frontend/package-lock.json* ./
RUN npm ci --legacy-peer-deps 2>/dev/null || npm install --legacy-peer-deps
COPY apps/studio/frontend/ ./
# Bake the backend URL into the client bundle. NEXT_PUBLIC_* vars are
# resolved at build time, so the empty string used previously became the
# literal URL prefix in production builds — fetch('/api/runs') then hit
# the Next.js server on :3000 (which doesn't proxy /api) and returned 404.
# Pointing at localhost:8765 lets the browser reach the FastAPI backend
# directly via the host port mapping.
ENV NEXT_PUBLIC_STUDIO_API_BASE="http://localhost:8765"
RUN npm run build

# ── Stage 2: Final image ─────────────────────────────────────────────────────
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    nodejs npm git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install lionagi
COPY pyproject.toml README.md LICENSE ./
COPY lionagi/ ./lionagi/
RUN pip install --no-cache-dir ".[studio]"

# Copy backend
COPY apps/studio/server/ ./apps/studio/server/
COPY apps/__init__.py ./apps/__init__.py
COPY apps/studio/__init__.py ./apps/studio/__init__.py

# Copy built frontend
COPY --from=frontend-build /build/.next ./apps/studio/frontend/.next
COPY --from=frontend-build /build/node_modules ./apps/studio/frontend/node_modules
COPY --from=frontend-build /build/package.json ./apps/studio/frontend/package.json
COPY --from=frontend-build /build/next.config.mjs ./apps/studio/frontend/next.config.mjs

VOLUME /root/.lionagi

EXPOSE 8765 3000

CMD ["sh", "-c", "uvicorn apps.studio.server.app:app --host 0.0.0.0 --port 8765 & cd apps/studio/frontend && npx next start --port 3000 --hostname 0.0.0.0"]
