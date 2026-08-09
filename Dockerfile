# syntax=docker/dockerfile:1

######################
# kleinanzeigen-bot build stage
# Building from source (instead of downloading the prebuilt release binary)
# so we can layer our own nodriver connect-timeout patch on top of
# kleinanzeigen-bot's own post_install nodriver patches (scripts/fix_nodriver.py).
# See scripts/patch_nodriver_connect_timeout.py for why.
######################
FROM python:3.12-slim AS kleinanzeigen-bot-build

ARG KLEINANZEIGEN_BOT_REF=main

RUN apt-get update \
    && apt-get install -y --no-install-recommends git binutils build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir pdm \
    && git clone --depth 1 --branch "${KLEINANZEIGEN_BOT_REF}" \
       https://github.com/Second-Hand-Friends/kleinanzeigen-bot.git /opt/kleinanzeigen-bot-src

COPY scripts/patch_nodriver_connect_timeout.py /opt/patch_nodriver_connect_timeout.py

WORKDIR /opt/kleinanzeigen-bot-src
# `pdm install` runs kleinanzeigen-bot's own post_install hook
# (scripts/fix_nodriver.py) first; our extra patch is applied on top of that.
RUN pdm install -v \
    && pdm run python /opt/patch_nodriver_connect_timeout.py \
    && pdm run compile \
    && ./dist/kleinanzeigen-bot --help

######################
# runtime image
######################
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

# Install Chromium (used directly by the bot, not via Playwright).
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=kleinanzeigen-bot-build /opt/kleinanzeigen-bot-src/dist/kleinanzeigen-bot /usr/local/bin/kleinanzeigen
RUN chmod +x /usr/local/bin/kleinanzeigen

# Install Python dependencies
COPY --chown=appuser pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Copy application source
COPY --chown=appuser main.py background_worker.py queue_manager.py agent_registry.py tools.py skills.py ./
COPY --chown=appuser skills/ ./skills/

USER appuser

CMD ["/app/.venv/bin/python", "main.py"]

