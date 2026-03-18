FROM python:3.13-slim

WORKDIR /app

# Install uv + uvx for fast dependency resolution and stdio MCP servers.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=ghcr.io/astral-sh/uv:latest /uvx /usr/local/bin/uvx

# Install Node.js + npx for stdio MCP servers.
COPY --from=node:22-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# Stage 1: Install dependencies only (cached layer).
COPY pyproject.toml README.md ./
COPY intaris/__init__.py intaris/__init__.py
RUN uv pip install --system --no-cache ".[postgresql]" \
    && uv pip uninstall --system intaris

# Stage 2: Copy full source and install the package (no deps needed).
COPY intaris/ intaris/
RUN uv pip install --system --no-cache --no-deps .

# Data directory for database and logs.
ENV DATA_DIR=/data
RUN mkdir -p /data

EXPOSE 8060

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8060/health')" || exit 1

CMD ["intaris"]
