FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Build context is agents/ (one level up from hyperion/).
# This lets us copy both the hyperion package and the shared utilities.
COPY hyperion/pyproject.toml hyperion/uv.lock* hyperion/README.md ./
COPY hyperion/src/ ./src/

# Shared utilities (loaded at runtime by importlib in tools/second_brain.py)
COPY _shared/ ./_shared/

# Agent records (config/agents/*.json). Bind-mounted at runtime so UI edits
# persist; baked into the image so the container still has seeds without a mount.
COPY hyperion/config/ ./config/

# Install dependencies (no dev deps)
RUN uv sync --frozen --no-dev

# The tasks/ directory is mounted at runtime; pre-create it
RUN mkdir -p /app/tasks /app/config

# Expose both service ports
EXPOSE 4100 4101

# Default: run the FastAPI service
# Override CMD to run hyperion-mcp for the MCP transport
CMD ["uv", "run", "hyperion-api"]
