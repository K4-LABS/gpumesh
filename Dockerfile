# =============================================================================
# gpumesh - Distributed GPU Compute Mesh
# Multi-stage build for minimal image size
# =============================================================================

# Stage 1: Build the wheel
FROM python:3.11-slim AS builder
WORKDIR /build
RUN pip install --no-cache-dir build
COPY . .
RUN python -m build --wheel

# Stage 2: Minimal runtime
FROM python:3.11-slim

# Version for image labels. Kept in sync with pyproject.toml by
# scripts/bump_version.py; override with --build-arg VERSION=x.y.z.
ARG VERSION=1.3.0

# Labels for Docker Hub
LABEL maintainer="samurai007ak"
LABEL description="Borrow your friends' GPUs: a distributed compute mesh in pure Python"
LABEL version="1.3.0"
LABEL org.opencontainers.image.source="https://github.com/Samurai007AK/gpumesh"
LABEL org.opencontainers.image.documentation="https://github.com/Samurai007AK/gpumesh#readme"
LABEL org.opencontainers.image.licenses="MIT"

# Install system dependencies for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Install gpumesh from built wheel
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Default port for coordinator
EXPOSE 8732
EXPOSE 48900/udp

ENTRYPOINT ["gpumesh"]
CMD ["--help"]
