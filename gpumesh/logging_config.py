"""Centralized logging configuration for gpumesh."""

from __future__ import annotations

import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    """Outputs log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "module": record.name,
            "msg": record.getMessage(),
            "line": record.lineno,
        }, default=str)


_COLOR_MAP = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"


class ColorConsoleFormatter(logging.Formatter):
    """Human-readable colored log output for terminals."""

    def format(self, record: logging.LogRecord) -> str:
        level_color = _COLOR_MAP.get(record.levelname, "")
        msg = super().format(record)
        return f"{level_color}{msg}{_RESET}"


def setup_logging(*, verbose: bool = False, json_logs: bool = False) -> None:
    """Configure the gpumesh root logger.

    Args:
        verbose: Enable DEBUG-level output.
        json_logs: Output JSON lines instead of colored text.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logger = logging.getLogger("gpumesh")
    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)

    if json_logs:
        fmt = JsonFormatter("%(message)s")
    else:
        fmt = ColorConsoleFormatter("%(message)s")

    handler.setFormatter(fmt)
    logger.addHandler(handler)
