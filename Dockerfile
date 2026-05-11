# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Install uv (pinned for reproducibility)
COPY --from=ghcr.io/astral-sh/uv:0.7.2 /uv /uvx /bin/

# Set environment variables for uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KLEINANZEIGEN_SESSION_DIR=/home/appuser/.kleinanzeigen-agent/sessions \
    KLEINANZEIGEN_SKILL_ADDONS_DIR=/home/appuser/.kleinanzeigen-agent/skills

# Run as non-root user (fixed UID so host volume permissions can be set to match)
RUN useradd --create-home --shell /bin/false --uid 1001 appuser
WORKDIR /app

# Download kleinanzeigen-bot binary and install Chromium (used directly by the bot, not via Playwright).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl chromium ca-certificates \
    && curl -fsSL -o /usr/local/bin/kleinanzeigen \
       https://github.com/Second-Hand-Friends/kleinanzeigen-bot/releases/download/latest/kleinanzeigen-bot-linux-amd64 \
    && chmod +x /usr/local/bin/kleinanzeigen \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY --chown=appuser pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Copy application source
COPY --chown=appuser main.py background_worker.py queue_manager.py agent_registry.py tools.py skills.py ./
COPY --chown=appuser skills/ ./skills/

USER appuser

CMD ["/app/.venv/bin/python", "main.py"]
