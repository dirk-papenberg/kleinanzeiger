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
RUN useradd --no-create-home --shell /bin/false --uid 1001 appuser
WORKDIR /app

# Install dependencies first for better layer caching
COPY --chown=appuser pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Download the kleinanzeigen-bot Linux binary from GitHub releases
# (curl avoids Docker ADD silently decompressing the PyInstaller binary)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && curl -fsSL -o /usr/local/bin/kleinanzeigen \
       https://github.com/Second-Hand-Friends/kleinanzeigen-bot/releases/download/latest/kleinanzeigen-bot-linux-amd64 \
    && chmod +x /usr/local/bin/kleinanzeigen \
    && apt-get purge -y --auto-remove curl \
    && rm -rf /var/lib/apt/lists/*

# Copy application source
COPY --chown=appuser main.py background_worker.py queue_manager.py ./

# Install the project itself
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

USER appuser

CMD ["uv", "run", "python", "main.py"]
