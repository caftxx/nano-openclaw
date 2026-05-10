FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY nano_openclaw/ ./nano_openclaw/
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Use /data (not /root) so the container can run as a non-root host user via
# ``user: "${UID:-1000}:${GID:-1000}"`` in docker-compose. ``/root`` is mode
# 0700 owned by root and unreachable by other UIDs; ``/data`` is world-rwx
# so any UID can read/write the bind-mounted state directory.
RUN mkdir -p /data && chmod 777 /data
WORKDIR /data

# Default to the gateway daemon's foreground entry — `gateway run` wires up
# WebUI + WeChat channels + cron + the /rpc WebSocket in a single process.
# Override with `docker compose run --rm tui` to enter the REPL instead.
#
# host/port are read from the mounted config (gateway: { host, port } in
# nano-openclaw.json5).
ENTRYPOINT ["python", "-m", "nano_openclaw"]
CMD ["gateway", "run"]
