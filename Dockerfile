# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Install uv (pinned for reproducibility)
COPY --from=ghcr.io/astral-sh/uv:0.7.2 /uv /uvx /bin/

# Set environment variables for uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Run as non-root user (fixed UID so host volume permissions can be set to match)
RUN useradd --create-home --shell /bin/false --uid 1001 appuser
WORKDIR /app

# Install system dependencies, download the kleinanzeigen-bot binary,
# and install Playwright's Chromium browser + its OS-level deps in one layer.
# PLAYWRIGHT_BROWSERS_PATH is set to a fixed path so both root (install) and
# appuser (runtime) resolve the same browser location.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl python3-pip \
    && curl -fsSL -o /usr/local/bin/kleinanzeigen \
       https://github.com/Second-Hand-Friends/kleinanzeigen-bot/releases/download/latest/kleinanzeigen-bot-linux-amd64 \
    && chmod +x /usr/local/bin/kleinanzeigen \
    && pip install --no-cache-dir --break-system-packages playwright \
    && playwright install chromium \
    && playwright install-deps chromium \
    && apt-get purge -y --auto-remove python3-pip \
    && rm -rf /var/lib/apt/lists/* /root/.cache

# Install dependencies first for better layer caching
COPY --chown=appuser pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Copy application source
COPY --chown=appuser main.py background_worker.py queue_manager.py ./

USER appuser

CMD ["/app/.venv/bin/python", "main.py"]
