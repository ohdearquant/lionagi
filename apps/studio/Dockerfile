# Lion Studio — single-origin image: Vite SPA served by FastAPI/uvicorn
#
# Build from repo root:
#   docker build -f apps/studio/Dockerfile -t lion-studio .
#
# Run:
#   docker run -p 8765:8765 -v ~/.lionagi:/root/.lionagi lion-studio

# ── Stage 1: Build frontend ──────────────────────────────────────────────────
FROM node:22-alpine AS frontend-build

WORKDIR /build
COPY apps/studio/frontend/package.json apps/studio/frontend/package-lock.json* ./
RUN npm ci --legacy-peer-deps 2>/dev/null || npm install --legacy-peer-deps
COPY apps/studio/frontend/ ./
RUN npm run build

# ── Stage 2: Final image ─────────────────────────────────────────────────────
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install lionagi
COPY pyproject.toml README.md LICENSE ./
COPY lionagi/ ./lionagi/
RUN pip install --no-cache-dir ".[studio]"

# Copy marketplace + plugin manifest so Studio's Library/Skills/Agents
# views resolve resources. plugins.py tries _THIS.parents[3] first then
# falls back to LIONAGI_HOME paths; in the container, /app/lionagi/ on
# sys.path makes parents[3] = /app.
COPY marketplace/ ./marketplace/
COPY .claude-plugin/ ./.claude-plugin/

# Copy built frontend dist — uvicorn serves this via the SPA mount in app.py.
COPY --from=frontend-build /build/dist ./apps/studio/frontend/dist

VOLUME /root/.lionagi

EXPOSE 8765

ENV LIONAGI_STUDIO_FRONTEND_DIST=/app/apps/studio/frontend/dist

CMD ["uvicorn", "lionagi.studio.app:app", "--host", "0.0.0.0", "--port", "8765"]
