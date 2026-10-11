# The sandbox a console's bash and python run in: Debian, Python 3.12, uv, git, the usual small tools.
# Built once: `container build -t lion-sandbox hub/tools/sandbox`. The project directory is mounted at
# its host path; uv keeps its environment and cache under /opt so the host's .venv is never touched.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates ripgrep jq \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_CACHE_DIR=/opt/uv-cache UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /work
CMD ["sleep", "infinity"]
