# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set environment variables for uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for better layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source
COPY main.py background_worker.py queue_manager.py ./

# Install the project itself
RUN uv sync --frozen --no-dev

# Run as non-root user
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser /app
USER appuser

CMD ["uv", "run", "python", "main.py"]
