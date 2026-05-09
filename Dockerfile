# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Install uv (pinned for reproducibility)
COPY --from=ghcr.io/astral-sh/uv:0.7.2 /uv /uvx /bin/

# Set environment variables for uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for better layer caching
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Copy application source
COPY main.py background_worker.py queue_manager.py ./

# Install the project itself
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Download the kleinanzeigen-bot Linux binary from GitHub releases
ADD https://github.com/Second-Hand-Friends/kleinanzeigen-bot/releases/download/latest/kleinanzeigen-bot-linux-amd64 /usr/local/bin/kleinanzeigen
RUN chmod +x /usr/local/bin/kleinanzeigen

# Run as non-root user
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser /app
USER appuser

CMD ["uv", "run", "python", "main.py"]
