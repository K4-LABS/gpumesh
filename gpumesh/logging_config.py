"""Centralized logging configuration for gpumesh."""

from __future__ import annotations

import json
import logging
import sys
import time

from . import ansi


class JsonFormatter(logging.Formatter):
    """Outputs log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "module": record.name,
            "msg": record.getMessage(),
            "line": record.lineno,
        }
        # ``logger.exception(...)`` and ``exc_info=True`` put the crash here,
        # and this formatter used to ignore it — so in the one mode meant to be
        # parsed by machines, a crash was indistinguishable from an ordinary
        # message: no type, no message, no traceback. Kept as three fields
        # rather than glued onto "msg" so a log pipeline can group on the type
        # alone. json.dumps escapes the traceback's newlines, so the record
        # still occupies exactly one line.
        if record.exc_info:
            exc_type, exc_value = record.exc_info[0], record.exc_info[1]
            payload["exc_type"] = getattr(exc_type, "__name__", str(exc_type))
            payload["exc_msg"] = str(exc_value)
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_COLOR_MAP = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"


class ColorConsoleFormatter(logging.Formatter):
    """Human-readable colored log output for terminals.

    Colour is gated on gpumesh's own capability probe, ``ansi._SUPPORTS_COLOR``
    (``GPUMESH_COLOR=1``/``0`` first, isatty otherwise). This used to emit
    escapes unconditionally, so ``gpumesh serve 2> run.log`` wrote raw escape
    bytes into the file. The project already had exactly one place that decides
    this question and the answer is reused rather than re-derived — a second
    detection scheme would be one more thing to keep in sync.

    One wrinkle worth knowing: that probe asks ``sys.stdout``, while this
    handler writes to stderr. For the uncommon shape where only stderr is
    redirected, ``GPUMESH_COLOR=0`` is the escape hatch.
    """

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        level_color = _COLOR_MAP.get(record.levelname)
        # No colour support, or a custom level the map has no entry for: emit
        # the message bare. The old code appended _RESET regardless, so an
        # unknown level produced a reset sequence with no colour ahead of it —
        # escape bytes that reset nothing.
        if not level_color or not ansi._SUPPORTS_COLOR:
            return msg
        return f"{level_color}{msg}{_RESET}"


# Marks the handler this module installed, so a second setup_logging() can
# replace its own handler without touching one an embedding application
# attached to the same logger.
_MANAGED = "_gpumesh_managed_handler"


def setup_logging(*, verbose: bool = False, json_logs: bool = False) -> None:
    """Configure the gpumesh root logger.

    Safe to call more than once: the handler installed by a previous call is
    replaced, not stacked on top of.

    Args:
        verbose: Enable DEBUG-level output.
        json_logs: Output JSON lines instead of colored text.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logger = logging.getLogger("gpumesh")
    logger.setLevel(level)

    # Idempotency. Calling this twice used to add a second StreamHandler and
    # double every log line, and switching to json_logs afterwards was
    # impossible because the colour handler stayed attached and kept printing
    # alongside the new one. Only handlers this module installed are removed:
    # an application embedding gpumesh may have attached its own to this
    # logger, and silently eating that would be worse than the duplication.
    for existing in [h for h in logger.handlers if getattr(h, _MANAGED, False)]:
        logger.removeHandler(existing)
        existing.close()  # StreamHandler.close does not close sys.stderr

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    setattr(handler, _MANAGED, True)

    if json_logs:
        # No format string: JsonFormatter.format overrides the base entirely
        # and never consults self._style, so an argument here would only look
        # as though it configured something.
        fmt = JsonFormatter()
    else:
        fmt = ColorConsoleFormatter("%(message)s")

    handler.setFormatter(fmt)
    logger.addHandler(handler)
    # gpumesh owns this handler, so a copy reaching the root logger is a
    # duplicate line rather than a second destination. Without this, any host
    # application that calls logging.basicConfig() — or pytest's log_cli —
    # prints every gpumesh line twice.
    logger.propagate = False
