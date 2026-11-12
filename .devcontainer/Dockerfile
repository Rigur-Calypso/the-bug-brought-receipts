FROM mcr.microsoft.com/devcontainers/python:1-3.13-bookworm
# This demo has no Yarn dependency. Keep apt on Debian's signed repositories.
RUN rm -f /etc/apt/sources.list.d/yarn.list /etc/apt/sources.list.d/yarn.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends zstd \
    && rm -rf /var/lib/apt/lists/*
