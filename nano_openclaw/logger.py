"""Structured JSON Lines logging for nano-openclaw.

All log output goes to a single rotating file under state_dir/log/.
Each line is a JSON object with mandatory event field and optional
context fields auto-injected via contextvars.
"""

from __future__ import annotations

import gzip
import json
import logging
import logging.handlers
import os
import os.path
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOG_DIRNAME = "log"
LOG_FILENAME = "nano-openclaw.log"
DEFAULT_MAX_BYTES = 50_000_000  # 50 MB
DEFAULT_BACKUP_COUNT = 20

LEVEL_MAP: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

# ---- contextvars -----------------------------------------------------------

_log_ctx: ContextVar[dict[str, str | int | float | bool | None]] = ContextVar(
    "log_ctx", default={}
)


def set_log_context(**fields: str | int | float | bool | None) -> None:
    """Set structured fields that will be auto-injected into every log line.

    Call again to update / overlay; only non-None values are kept.
    """
    ctx = {k: v for k, v in {**_log_ctx.get(), **fields}.items() if v is not None}
    _log_ctx.set(ctx)


def clear_log_context() -> None:
    """Remove all auto-injected context fields."""
    _log_ctx.set({})

# ---- formatter -------------------------------------------------------------


class JsonLinesFormatter(logging.Formatter):
    """Format each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", ""),
            "message": record.getMessage(),
        }

        # Merge contextvar fields (auto-injected)
        ctx = _log_ctx.get()
        for key in ("session_id", "run_id", "turn_id", "tool_call_id", "model"):
            val = ctx.get(key)
            if val is not None:
                obj[key] = val

        # Merge per-call structured fields
        extra_fields: dict[str, Any] = getattr(record, "log_fields", {})
        for key, val in extra_fields.items():
            if val is not None:
                obj[key] = val

        return json.dumps(obj, ensure_ascii=False)

# ---- rotating handler with gzip compression ---------------------------------


class CompressedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that gzip-compresses old backup files."""

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None

        # Rotate existing backup files
        for i in range(self.backupCount - 1, 0, -1):
            sfn_gz = self.rotation_filename(f"{self.baseFilename}.{i}.gz")
            dfn_gz = self.rotation_filename(f"{self.baseFilename}.{i + 1}.gz")
            if os.path.exists(sfn_gz):
                if os.path.exists(dfn_gz):
                    os.remove(dfn_gz)
                os.rename(sfn_gz, dfn_gz)

            sfn = self.rotation_filename(f"{self.baseFilename}.{i}")
            dfn = self.rotation_filename(f"{self.baseFilename}.{i + 1}")
            if os.path.exists(sfn):
                if os.path.exists(dfn):
                    os.remove(dfn)
                os.rename(sfn, dfn)

        # Rotate current log file to .1
        dfn = self.rotation_filename(f"{self.baseFilename}.1")
        if os.path.exists(dfn):
            os.remove(dfn)
        if os.path.exists(self.baseFilename):
            self.rotate(self.baseFilename, dfn)

        # Compress the newly created backup
        if os.path.exists(dfn) and not dfn.endswith(".gz"):
            self._gzip_file(dfn)

        # Remove out-of-range backups
        dfn_gz = self.rotation_filename(f"{self.baseFilename}.{self.backupCount}.gz")
        if os.path.exists(dfn_gz):
            os.remove(dfn_gz)
        dfn = self.rotation_filename(f"{self.baseFilename}.{self.backupCount}")
        if os.path.exists(dfn):
            os.remove(dfn)

        if not self.delay:
            self.stream = self._open()

    def _gzip_file(self, path: str) -> None:
        gz_path = path + ".gz"
        try:
            with open(path, "rb") as src, gzip.open(gz_path, "wb") as dst:
                dst.writelines(src)
            os.remove(path)
        except OSError:
            pass

# ---- StructuredLogger --------------------------------------------------------


class StructuredLogger:
    """Thin wrapper around a standard logger that requires an *event* field."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _log(self, level: int, event: str, message: str = "", **fields: Any) -> None:
        extra = {"event": event, "log_fields": fields}
        self._logger.log(level, message, extra=extra)

    def debug(self, event: str, message: str = "", **fields: Any) -> None:
        self._log(logging.DEBUG, event, message, **fields)

    def info(self, event: str, message: str = "", **fields: Any) -> None:
        self._log(logging.INFO, event, message, **fields)

    def warning(self, event: str, message: str = "", **fields: Any) -> None:
        self._log(logging.WARNING, event, message, **fields)

    def error(self, event: str, message: str = "", **fields: Any) -> None:
        self._log(logging.ERROR, event, message, **fields)

# ---- setup -------------------------------------------------------------------


def get_logger(name: str) -> StructuredLogger:
    """Return a StructuredLogger for *name* (typically ``__name__``)."""
    return StructuredLogger(logging.getLogger(name))


def resolve_log_level(
    config_level: str | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Resolve log level from env var, config, or default WARNING.

    Priority: NANO_LOG_LEVEL env > config logging.level > WARNING
    """
    raw = (env or os.environ).get("NANO_LOG_LEVEL")
    if raw:
        return LEVEL_MAP.get(raw.lower(), logging.WARNING)
    if config_level:
        return LEVEL_MAP.get(config_level.lower(), logging.WARNING)
    return logging.WARNING


def setup_logging(
    state_dir: Path,
    level: int = logging.WARNING,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> None:
    """Configure the root logger to write JSON Lines to state_dir/log/nano-openclaw.log.

    Must be called once at startup before any logger is used.
    """
    log_dir = state_dir / LOG_DIRNAME
    log_dir.mkdir(parents=True, exist_ok=True)

    handler = CompressedRotatingFileHandler(
        filename=str(log_dir / LOG_FILENAME),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(JsonLinesFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    # Remove any handlers that may have been added before setup
    root.handlers.clear()
    root.addHandler(handler)

    logging.captureWarnings(True)
