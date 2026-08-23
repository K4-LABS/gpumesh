# =============================================================================
# gpumesh - Distributed GPU Compute Mesh
# Multi-stage build for minimal image size
# =============================================================================

# The base image is pinned by digest, not by tag. `python:3.11-slim` is a
# moving target: it is rebuilt whenever Debian ships a security update, so two
# builds a week apart produce different images from identical source and there
# is no record of which one shipped. The digest below is the multi-arch
# manifest list, so amd64 and arm64 both resolve from it.
#
# The tradeoff of a digest pin is that it stops picking up those Debian
# security rebuilds automatically — which is why .github/dependabot.yml has a
# `docker` ecosystem entry on a 3-day cooldown whose entire job is to move
# this line. A digest pin without a bumper attached is how an image quietly
# rots; the two go together.
#
# Resolved from Docker Hub for tag 3.11-slim.
ARG PYTHON_IMAGE=python:3.11-slim@sha256:a630a63cdb314e2d138a2fca3e375e319e8568346ffafac5b980f888630ac4f1

# -----------------------------------------------------------------------------
# Stage 1: Build the wheel
# -----------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder
WORKDIR /build
RUN pip install --no-cache-dir build
# NOTE: this `COPY . .` copies whatever .dockerignore does not exclude. That
# file is load-bearing: the repo contains gitignored operator notes carrying a
# live coordinator token and SSH credentials, and they are excluded there.
# Before adding anything to this stage, or before collapsing this build into a
# single stage, re-read .dockerignore.
COPY . .
RUN python -m build --wheel

# -----------------------------------------------------------------------------
# Stage 2: Minimal runtime
# -----------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

# Version for image labels. Kept in sync with pyproject.toml by
# scripts/bump_version.py; override with --build-arg VERSION=x.y.z.
ARG VERSION=3.1.0

# Labels for Docker Hub
LABEL maintainer="samurai007ak"
LABEL description="Borrow your friends' GPUs: a distributed compute mesh in pure Python"
# Both version labels read the same ARG. Hardcoding the number here meant
# `--build-arg VERSION=x.y.z` moved image.version but left this one saying
# 2.0.0, so an image could carry two different answers to "what version is
# this".
LABEL version="${VERSION}"
# image.source is what links this image to the repository on GitHub — it is
# what GitHub Packages, Docker Hub, and provenance tooling read to associate
# the artifact with its source. Do not remove it.
LABEL org.opencontainers.image.source="https://github.com/K4-LABS/gpumesh"
LABEL org.opencontainers.image.documentation="https://github.com/K4-LABS/gpumesh#readme"
# Must match the `license` field in pyproject.toml and the notice at the top
# of LICENSE. This label is what SBOM and container-scanning tools report as
# the image's license, so a stale value here is how a downstream consumer
# concludes gpumesh has a different license than it actually does.
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.title="gpumesh"
LABEL org.opencontainers.image.version="${VERSION}"

# Install system dependencies for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

# Install gpumesh from built wheel
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# -----------------------------------------------------------------------------
# Run as a non-root user
# -----------------------------------------------------------------------------
# This container's entire purpose is executing Python that arrived over a
# network from another machine. Running that as UID 0 means any sandbox escape,
# any deserialization bug, any path-traversal in a task's own code starts with
# root in the container — and with a shared kernel that is a much shorter
# distance to the host than most people assume.
#
# The UID is fixed at 10001 and referenced NUMERICALLY in the USER directive
# below. That matters for Kubernetes: `securityContext.runAsNonRoot` is
# validated by the kubelet before the container starts, and the kubelet cannot
# read /etc/passwd inside an image it has not run yet. A `USER gpumesh`
# directive is unverifiable at that point and the pod is rejected with
# "container has runAsNonRoot and image has non-numeric user". `USER 10001`
# is checkable without resolving anything.
ARG UID=10001
ARG GID=10001

# /data is both the user's HOME and the working directory, and that is
# deliberate — gpumesh writes state to two different kinds of path and one
# mount point needs to cover both:
#
#   * `gpumesh serve --db` defaults to ~/.gpumesh/gpumesh.db — next to
#     config.json, not in the working directory — so the coordinator's job
#     queue survives restarts from anywhere. Previously the default was the
#     RELATIVE path "gpumesh.db", which landed wherever the process started;
#     with no WORKDIR at all it was created at /gpumesh.db, in the
#     container's writable layer, outside every volume, and destroyed on
#     `docker compose down`. The old compose file mounted /root/.gpumesh,
#     which never held the database.
#
#   * connection_manager.py writes ~/.gpumesh/config.json, and security.py
#     stores tokens there at mode 0600. That resolves through HOME.
#
# Pointing both at /data means a single `-v gpumesh_data:/data` persists the
# database and the config together.
RUN groupadd --system --gid ${GID} gpumesh \
    && useradd --system --uid ${UID} --gid ${GID} \
       --home-dir /data --shell /usr/sbin/nologin gpumesh \
    && mkdir -p /data \
    && chown -R ${UID}:${GID} /data \
    && chmod 700 /data

ENV HOME=/data
WORKDIR /data

USER 10001:10001

# Default port for coordinator
EXPOSE 8732
EXPOSE 48900/udp

# The database lives here. Declaring it means `docker run` without an explicit
# -v still gets a persistent anonymous volume rather than silently losing the
# task history when the container is replaced.
VOLUME ["/data"]

ENTRYPOINT ["gpumesh"]
CMD ["--help"]
