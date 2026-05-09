FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY nano_openclaw/ ./nano_openclaw/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /root

ENTRYPOINT ["python", "-m", "nano_openclaw"]
