# Anticipa API (and `anticipa worker`) image: python:3.12-slim, runtime dependencies only (no dev group), non-root.
# The context is the REPOSITORY ROOT because the app reads config/models.toml next to backend/:
#   docker build -f backend/Dockerfile -t anticipa-api .
#   docker run -p 8000:8000 --env-file <your env> anticipa-api                      # API
#   docker run --env-file <your env> anticipa-api anticipa worker                   # separate worker (optional)
#   Single-service hosting: set ANTICIPA_EMBEDDED_WORKER=1 and the API runs the job loop itself.
#   First deploy: `anticipa migrate` (alembic upgrade head) against MIGRATION_DATABASE_URL.
# Layout in the image mirrors the repo (/app/backend, /app/config) so path resolution is identical to local runs.
FROM python:3.12-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install "uv==0.12.20"
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/anticipa ./anticipa
RUN uv sync --frozen --no-dev
COPY config /app/config

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/backend/.venv/bin:${PATH}"
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /app /app
WORKDIR /app/backend
USER app
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn anticipa.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
