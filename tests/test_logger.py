from __future__ import annotations

import logging

from nano_openclaw.config.types import LoggingConfig
from nano_openclaw.logger import resolve_log_level


def test_default_log_level_is_info():
    assert LoggingConfig().level == "info"
    assert resolve_log_level(env={}) == logging.INFO


def test_explicit_log_level_still_takes_precedence():
    assert resolve_log_level("error", env={}) == logging.ERROR
    assert resolve_log_level("error", env={"NANO_LOG_LEVEL": "debug"}) == logging.DEBUG
