# syntax=docker/dockerfile:1
# Tinker — Autonomous Architecture Thinking Engine
#
# Build:  docker build -t tinker:latest .
# Run:    docker run --env-file .env tinker:latest
#
# Note: Tinker requires an external Ollama server for AI inference.
# The container itself does NOT include Ollama — point TINKER_SERVER_URL
# at your Ollama host (e.g. host-gateway, a VM, or another container).

FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency manifests first for better layer caching.
# Changes to source code won't invalidate the pip install layer.
COPY pyproject.toml ./
COPY requirements/base.txt ./requirements/base.txt
COPY requirements/dev.txt ./requirements/dev.txt
COPY requirements/metrics.txt ./requirements/metrics.txt

# Install Python dependencies from the pinned lock file.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements/base.txt

# Copy the rest of the application source.
COPY . .

# Create a non-root user to run the application.
# uid/gid 1000 is the conventional first non-root user on Linux.
RUN groupadd --gid 1000 tinker \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /bin/false tinker \
    && chown -R tinker:tinker /app

USER tinker

# Health endpoint (lightweight asyncio HTTP server)
EXPOSE 8081
# Web UI
EXPOSE 8082
# Prometheus metrics
EXPOSE 9090

# Liveness probe: curl the /health endpoint every 30 seconds.
# --fail causes curl to exit non-zero on HTTP 4xx/5xx responses.
# --silent suppresses progress output.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl --fail --silent http://localhost:8081/health || exit 1

CMD ["python", "main.py"]
