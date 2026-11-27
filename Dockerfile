FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    SCRAPER_CONFIG=/app/config/scraper.yaml

WORKDIR /app

# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev

# /app/data holds the page cache (config/scraper.yaml, cache.path). It must be writable
# by the service user; compose mounts a named volume here, which Docker seeds with this
# directory's ownership on first use, so the cache survives restarts and image updates.
RUN useradd --system --uid 10001 scraper \
    && mkdir -p /app/data \
    && chown scraper:scraper /app/data
USER scraper

EXPOSE 3002
CMD ["uvicorn", "scraper.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "3002"]
