FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    SCRAPER_CONFIG=/app/config/scraper.yaml

WORKDIR /app

# The Chrome Fetcher's Engine: Google's own Chrome (the arm64 build on the Pi; Patchright
# drives it as channel "chrome", at /opt/google/chrome), a virtual display for its
# visible window, and tini as PID 1 to reap the processes Chrome leaves behind. Google
# serves only the current stable build, so each image gets the Chrome of its build day;
# `google-chrome --version` in the container says which.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini xvfb fonts-liberation \
    && curl -fsSLo /tmp/chrome.deb \
       "https://dl.google.com/linux/direct/google-chrome-stable_current_$(dpkg --print-architecture).deb" \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -f /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix

# Dependencies first so code changes don't reinstall them.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev

# /app/data holds the page cache (config/scraper.yaml, cache.path) and Chrome's profile
# (chrome.profile_dir). It must be writable by the service user; compose mounts a named
# volume here, which Docker seeds with this directory's ownership on first use, so the
# cache and the profile survive restarts and image updates. Chrome also wants a home
# directory of its own.
RUN useradd --system --create-home --uid 10001 scraper \
    && mkdir -p /app/data \
    && chown scraper:scraper /app/data
USER scraper

EXPOSE 3002
# Chrome's window goes on display :99, a virtual screen (Xvfb) started with the service.
# A container that was killed leaves Xvfb's lock behind, and Xvfb then refuses to start
# again, so it is cleared first.
ENV DISPLAY=:99
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "rm -f /tmp/.X99-lock /tmp/.X11-unix/X99; Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp & exec uvicorn scraper.api:create_app --factory --host 0.0.0.0 --port 3002"]
