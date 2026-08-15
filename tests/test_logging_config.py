"""Tests for ``gpumesh.logging_config``.

``setup_logging`` mutates a process-global logger, so an autouse fixture
snapshots and restores it. Without that, a test here would leave a stderr
handler attached to the ``gpumesh`` logger for the rest of the session and
every later test in the suite would start emitting log lines into its own
captured output.
"""

import contextlib
import json
import logging
import os
import re
import subprocess
import sys

import pytest

from gpumesh import ansi
from gpumesh.logging_config import (_MANAGED, ColorConsoleFormatter,
                                    JsonFormatter, setup_logging)


_RESET = "\033[0m"


def _managed_handlers():
    """The handlers ``setup_logging`` installed, ignoring anyone else's.

    Counting *every* handler on the logger looks equivalent and is not.
    ``setup_logging`` sets ``propagate = False`` so an embedding application
    does not receive a duplicate of every record — and pytest's ``caplog``
    responds to a non-propagating logger by attaching its own
    ``LogCaptureHandler`` directly to it. A bare ``len(logger.handlers)``
    therefore counts pytest's bookkeeping as if this module had installed it,
    and passes alone while failing once any earlier test file has used
    ``caplog``. The contract being tested was never "one handler exists"; it
    is "one handler *we* installed exists", which is what the module's own
    ``_MANAGED`` tag records — and what ``test_a_foreign_handler_is_left_alone``
    depends on.
    """
    logger = logging.getLogger("gpumesh")
    return [h for h in logger.handlers if getattr(h, _MANAGED, False)]


@pytest.fixture
def color_enabled(monkeypatch):
    """Force gpumesh's colour probe on.

    ``ansi._SUPPORTS_COLOR`` is decided once at import from GPUMESH_COLOR and
    ``sys.stdout.isatty()``. Under pytest stdout is captured, so the honest
    answer is "no colour" — which is exactly what the negative tests below
    depend on, and why the positive ones have to say so explicitly.
    """
    monkeypatch.setattr(ansi, "_SUPPORTS_COLOR", True)


@pytest.fixture
def color_disabled(monkeypatch):
    monkeypatch.setattr(ansi, "_SUPPORTS_COLOR", False)


@contextlib.contextmanager
def _restored_level_names():
    """Yield ``logging.addLevelName``, then undo whatever it registered.

    ``addLevelName`` writes straight into ``logging._levelToName`` and
    ``logging._nameToLevel``, which are module globals shared by the whole
    process, and there is no public call that removes a name again. A test
    that registers 25 as "NOTICE" and walks away therefore changes how every
    later test in the session — in every other file — renders that level, and
    the damage is invisible until something depends on ``getLevelName``.

    Snapshot-and-restore under the logging module's own lock is the only way
    back. Written as a context manager rather than only a fixture so that
    ``test_custom_level_names_do_not_leak`` can drive it directly and prove
    the restore actually happens.
    """
    with logging._lock:
        saved_level_to_name = logging._levelToName.copy()
        saved_name_to_level = logging._nameToLevel.copy()
    try:
        yield logging.addLevelName
    finally:
        with logging._lock:
            logging._levelToName.clear()
            logging._levelToName.update(saved_level_to_name)
            logging._nameToLevel.clear()
            logging._nameToLevel.update(saved_name_to_level)


@pytest.fixture
def custom_level_name():
    """Register custom level names for one test only. See above."""
    with _restored_level_names() as add_level_name:
        yield add_level_name


@pytest.fixture(autouse=True)
def _restore_gpumesh_logger():
    logger = logging.getLogger("gpumesh")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    saved_propagate = logger.propagate
    # Start from a clean logger: another module in the suite may already have
    # called setup_logging, and these tests count handlers and output lines.
    logger.handlers[:] = []
    yield
    logger.handlers[:] = saved_handlers
    logger.setLevel(saved_level)
    logger.propagate = saved_propagate


def _record(msg="hello", level=logging.INFO, name="gpumesh.worker",
            lineno=42, args=None, exc_info=None):
    record = logging.LogRecord(
        name=name, level=level, pathname="/src/gpumesh/worker.py",
        lineno=lineno, msg=msg, args=args, exc_info=exc_info,
    )
    return record


def _boom():
    """Produce a real exc_info triple."""
    try:
        raise ValueError("kaput")
    except ValueError:
        import sys
        return sys.exc_info()


# ── JsonFormatter ──────────────────────────────────────────────────────────

class TestJsonFormatter:
    def test_emits_one_parseable_json_object(self):
        payload = json.loads(JsonFormatter().format(_record()))
        assert payload["level"] == "INFO"
        assert payload["module"] == "gpumesh.worker"
        assert payload["msg"] == "hello"
        assert payload["line"] == 42

    def test_output_is_a_single_line(self):
        """JSON *lines*: a newline inside the record must not split the record."""
        line = JsonFormatter().format(_record(msg="two\nlines"))
        assert "\n" not in line
        assert json.loads(line)["msg"] == "two\nlines"

    def test_timestamp_is_iso_utc(self):
        payload = json.loads(JsonFormatter().format(_record()))
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", payload["ts"])

    def test_timestamp_uses_the_record_creation_time(self):
        record = _record()
        record.created = 0  # 1970-01-01T00:00:00Z
        payload = json.loads(JsonFormatter().format(record))
        assert payload["ts"] == "1970-01-01T00:00:00Z"

    def test_percent_args_are_interpolated(self):
        payload = json.loads(
            JsonFormatter().format(_record(msg="took %ss on %s",
                                           args=(1.5, "box")))
        )
        assert payload["msg"] == "took 1.5s on box"

    def test_non_string_message_is_stringified(self):
        payload = json.loads(JsonFormatter().format(_record(msg={"a": 1})))
        assert payload["msg"] == "{'a': 1}"

    def test_braces_and_quotes_are_escaped(self):
        raw = 'he said "{\\"x\\": 1}"'
        payload = json.loads(JsonFormatter().format(_record(msg=raw)))
        assert payload["msg"] == raw

    @pytest.mark.parametrize("level", [
        logging.DEBUG, logging.INFO, logging.WARNING,
        logging.ERROR, logging.CRITICAL,
    ])
    def test_every_level_name_round_trips(self, level):
        payload = json.loads(JsonFormatter().format(_record(level=level)))
        assert payload["level"] == logging.getLevelName(level)

    def test_record_with_exception_still_produces_valid_json(self):
        """Whatever else it does, it must never emit a broken line."""
        line = JsonFormatter().format(_record(msg="failed", exc_info=_boom()))
        payload = json.loads(line)
        assert payload["msg"] == "failed"

    def test_exception_info_is_included(self):
        """logger.exception() must not lose the exception in JSON mode.

        format() never looked at record.exc_info, so the type, message and
        traceback were dropped entirely — in the one mode that exists to be
        parsed, a crash was indistinguishable from an ordinary message.
        """
        payload = json.loads(
            JsonFormatter().format(_record(msg="failed", exc_info=_boom()))
        )
        blob = json.dumps(payload)
        assert "ValueError" in blob
        assert "kaput" in blob

    def test_exception_fields_are_separate_from_the_message(self):
        """Grouping a log stream by exception type needs a field, not a blob."""
        payload = json.loads(
            JsonFormatter().format(_record(msg="failed", exc_info=_boom()))
        )
        assert payload["msg"] == "failed"
        assert payload["exc_type"] == "ValueError"
        assert payload["exc_msg"] == "kaput"
        assert "Traceback (most recent call last)" in payload["traceback"]

    def test_a_traceback_does_not_break_the_one_line_rule(self):
        """A traceback is multi-line; the record still is not."""
        line = JsonFormatter().format(_record(msg="failed", exc_info=_boom()))
        assert "\n" not in line
        assert "kaput" in json.loads(line)["traceback"]

    def test_records_without_an_exception_carry_no_exception_keys(self):
        payload = json.loads(JsonFormatter().format(_record()))
        assert "exc_type" not in payload
        assert "traceback" not in payload


# ── ColorConsoleFormatter ──────────────────────────────────────────────────

class TestColorConsoleFormatter:
    @pytest.mark.parametrize("level,code", [
        (logging.DEBUG, "\033[36m"),
        (logging.INFO, "\033[32m"),
        (logging.WARNING, "\033[33m"),
        (logging.ERROR, "\033[31m"),
        (logging.CRITICAL, "\033[1;31m"),
    ])
    def test_level_colors(self, level, code, color_enabled):
        out = ColorConsoleFormatter("%(message)s").format(_record(level=level))
        assert out == f"{code}hello{_RESET}"

    def test_message_survives_intact(self, color_enabled):
        out = ColorConsoleFormatter("%(message)s").format(_record(msg="plain"))
        stripped = re.sub(r"\033\[[0-9;]*m", "", out)
        assert stripped == "plain"

    def test_format_string_is_honoured(self, color_enabled):
        out = ColorConsoleFormatter("%(levelname)s|%(name)s|%(message)s").format(
            _record(level=logging.WARNING)
        )
        assert "WARNING|gpumesh.worker|hello" in out

    def test_unknown_level_gets_no_escape_codes_at_all(self, color_enabled,
                                                       custom_level_name):
        """A custom level has no entry in _COLOR_MAP.

        The prefix was empty but the reset was appended anyway — a dangling
        reset with nothing ahead of it to reset. Harmless on a terminal,
        literal escape bytes in a redirected log.

        The registration goes through ``custom_level_name`` because
        ``logging.addLevelName`` is not undoable by any public call and this
        used to leave level 25 named "NOTICE" for the rest of the session.
        """
        custom_level_name(25, "NOTICE")
        assert logging.getLevelName(25) == "NOTICE"
        out = ColorConsoleFormatter("%(message)s").format(_record(level=25))
        assert out == "hello"

    def test_custom_level_names_do_not_leak(self):
        """The guard on ``custom_level_name`` itself, order-independently.

        Asserting the restore from a *later* test would only work under a
        fixed test order, and this suite normally runs under pytest-randomly —
        a check that can be reordered into passing vacuously is not a check.
        So the context manager is driven directly here instead, and its exit
        is what the last assertion is about.
        """
        assert logging.getLevelName(25) == "Level 25", (
            "level 25 was already named before this test ran — something in "
            "this session is registering level names without restoring them"
        )
        with _restored_level_names() as add_level_name:
            add_level_name(25, "NOTICE")
            assert logging.getLevelName(25) == "NOTICE"
            assert logging.getLevelName("NOTICE") == 25
        assert logging.getLevelName(25) == "Level 25"
        assert "NOTICE" not in logging._nameToLevel

    def test_traceback_is_rendered_before_the_reset(self, color_enabled):
        """super().format() appends the traceback, so the reset lands last."""
        out = ColorConsoleFormatter("%(message)s").format(
            _record(msg="failed", exc_info=_boom())
        )
        assert "ValueError: kaput" in out
        assert out.endswith(_RESET)

    def test_no_color_when_the_output_is_not_a_terminal(self, color_disabled):
        """Redirecting the log to a file used to write escape bytes into it.

        The formatter emitted ANSI unconditionally, ignoring the one
        colour-capability probe the project already has.
        """
        out = ColorConsoleFormatter("%(message)s").format(_record())
        assert out == "hello"
        assert "\033[" not in out

    def test_the_shared_ansi_probe_is_what_decides(self, monkeypatch):
        """Specifically ansi._SUPPORTS_COLOR — not a second scheme of its own.

        Anything else would be one more place for GPUMESH_COLOR handling to
        drift out of step with the rest of the CLI.
        """
        monkeypatch.setattr(ansi, "_SUPPORTS_COLOR", False)
        assert "\033[" not in ColorConsoleFormatter("%(message)s").format(_record())
        monkeypatch.setattr(ansi, "_SUPPORTS_COLOR", True)
        assert "\033[" in ColorConsoleFormatter("%(message)s").format(_record())


_COLOR_PROBE = (
    "import logging;"
    "from gpumesh.logging_config import setup_logging;"
    "setup_logging();"
    "logging.getLogger('gpumesh').info('probe')"
)


@pytest.mark.parametrize("value,expect_color", [("1", True), ("0", False)])
def test_gpumesh_color_env_var_is_honoured(value, expect_color):
    """End-to-end, in a child process, because the probe runs at import time.

    That is the only place the environment variable is ever read for real, and
    the setting a user actually makes — ``GPUMESH_COLOR=0 gpumesh serve
    2> run.log`` — is set before the process starts. A monkeypatched env var in
    this process would prove nothing.
    """
    env = dict(os.environ, GPUMESH_COLOR=value)
    proc = subprocess.run(
        [sys.executable, "-c", _COLOR_PROBE],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "probe" in proc.stderr
    assert ("\033[" in proc.stderr) is expect_color


# ── setup_logging ──────────────────────────────────────────────────────────

class TestSetupLogging:
    def test_configures_the_gpumesh_logger(self):
        setup_logging()
        logger = logging.getLogger("gpumesh")
        assert logger.level == logging.INFO
        assert len(_managed_handlers()) == 1

    def test_verbose_selects_debug(self):
        setup_logging(verbose=True)
        logger = logging.getLogger("gpumesh")
        assert logger.level == logging.DEBUG
        assert _managed_handlers()[-1].level == logging.DEBUG

    def test_handler_level_matches_logger_level(self):
        setup_logging(verbose=False)
        logger = logging.getLogger("gpumesh")
        assert _managed_handlers()[-1].level == logging.INFO

    def test_default_formatter_is_the_color_one(self):
        setup_logging()
        formatter = _managed_handlers()[-1].formatter
        assert isinstance(formatter, ColorConsoleFormatter)

    def test_json_logs_selects_the_json_formatter(self):
        setup_logging(json_logs=True)
        formatter = _managed_handlers()[-1].formatter
        assert isinstance(formatter, JsonFormatter)

    def test_writes_to_stderr_not_stdout(self, capsys):
        setup_logging()
        logging.getLogger("gpumesh").info("to stderr please")
        captured = capsys.readouterr()
        assert "to stderr please" in captured.err
        assert "to stderr please" not in captured.out

    def test_json_mode_emits_parseable_lines_end_to_end(self, capsys):
        setup_logging(json_logs=True)
        logging.getLogger("gpumesh.worker").warning("lease expired")
        line = capsys.readouterr().err.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["level"] == "WARNING"
        assert payload["module"] == "gpumesh.worker"
        assert payload["msg"] == "lease expired"

    def test_debug_is_suppressed_without_verbose(self, capsys):
        setup_logging(verbose=False)
        logging.getLogger("gpumesh").debug("noisy detail")
        assert "noisy detail" not in capsys.readouterr().err

    def test_debug_appears_with_verbose(self, capsys):
        setup_logging(verbose=True)
        logging.getLogger("gpumesh").debug("noisy detail")
        assert "noisy detail" in capsys.readouterr().err

    def test_child_loggers_inherit_the_configuration(self, capsys):
        setup_logging()
        logging.getLogger("gpumesh.server.api").info("child speaks")
        assert "child speaks" in capsys.readouterr().err

    def test_propagation_to_the_root_logger_is_off(self):
        """The root logger must not get a second copy of every line.

        gpumesh owns its handler, so anything that also configures the root
        logger — a host application, ``logging.basicConfig``, pytest's
        ``log_cli`` — was printing every gpumesh line twice.
        """
        setup_logging()
        assert logging.getLogger("gpumesh").propagate is False

    def test_a_root_handler_receives_nothing(self):
        """The same finding, observed rather than asserted about a flag."""
        root = logging.getLogger()
        seen = []

        class _Collector(logging.Handler):
            def emit(self, record):
                seen.append(record.getMessage())

        collector = _Collector()
        root.addHandler(collector)
        saved_root_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            setup_logging()
            logging.getLogger("gpumesh.worker").info("mine alone")
        finally:
            root.removeHandler(collector)
            root.setLevel(saved_root_level)
        assert seen == []

    def test_calling_twice_says_it_once(self, capsys):
        """The user-visible symptom of the non-idempotency."""
        setup_logging()
        setup_logging()
        logging.getLogger("gpumesh").info("said once")
        assert capsys.readouterr().err.count("said once") == 1

    def test_setup_logging_is_idempotent(self):
        """A second call replaces the handler instead of stacking one on top.

        Reached by a library embedding gpumesh, by tests, and by
        ``gpumesh serve`` re-invoking cli.main in-process — each of which
        doubled every log line.
        """
        setup_logging()
        setup_logging()
        assert len(_managed_handlers()) == 1

    def test_many_calls_still_leave_one_handler(self):
        for _ in range(5):
            setup_logging()
        assert len(_managed_handlers()) == 1

    def test_switching_to_json_replaces_the_colour_handler(self, capsys):
        """You must be able to change format after the fact.

        The first call's colour handler used to stay attached, so the output
        was one coloured line *and* one JSON line instead of JSON only.
        """
        setup_logging()
        setup_logging(json_logs=True)
        logging.getLogger("gpumesh").info("mixed")
        lines = [ln for ln in capsys.readouterr().err.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert json.loads(lines[0])["msg"] == "mixed"

    def test_switching_back_from_json_works_too(self):
        setup_logging(json_logs=True)
        setup_logging()
        handlers = _managed_handlers()
        assert len(handlers) == 1
        assert isinstance(handlers[0].formatter, ColorConsoleFormatter)

    def test_a_foreign_handler_is_left_alone(self):
        """Only handlers this module installed are replaced.

        An application embedding gpumesh may route these records into its own
        logging setup. Silently removing that would be a worse bug than the
        duplication being fixed here.
        """
        logger = logging.getLogger("gpumesh")
        theirs = logging.NullHandler()
        logger.addHandler(theirs)
        setup_logging()
        setup_logging()
        assert theirs in logger.handlers, "an embedder's handler was removed"
        assert len(_managed_handlers()) == 1, "our handler was duplicated"
