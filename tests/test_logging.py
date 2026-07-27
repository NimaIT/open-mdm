"""Observability (AO-1): structured logging, named streams, request context.

These tests are pure-stdlib and do not need a database. They exercise the
JsonFormatter, the stream derivation / filter, request-context injection, the
stream_logger helper and configure_logging() in both text and JSON modes.
"""
import json
import logging
from io import StringIO

import pytest

from app.config import settings
from app.services import logging_config as lc


def _local_json_logger(name):
    """A logger with a StringIO + JsonFormatter + ContextFilter, isolated from
    the global config (propagate off so it does not hit the root handler)."""
    logger = logging.getLogger(name)
    logger.handlers = []
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(lc.JsonFormatter())
    handler.addFilter(lc.ContextFilter())
    logger.addHandler(handler)
    return logger, stream


def _read_json_lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# --------------------------------------------------------------- JsonFormatter
def test_json_formatter_one_line_with_required_keys():
    logger, stream = _local_json_logger("mdm.test.jsonkeys")
    logger.info("hello world")

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1  # exactly one JSON object per record

    obj = json.loads(lines[0])
    for key in ("timestamp", "level", "stream", "logger", "message"):
        assert key in obj
    assert obj["level"] == "INFO"
    assert obj["message"] == "hello world"
    assert obj["logger"] == "mdm.test.jsonkeys"
    # ISO-8601 UTC timestamp.
    assert obj["timestamp"].endswith("+00:00")


def test_json_formatter_includes_extra_fields():
    logger, stream = _local_json_logger("mdm.test.extra")
    logger.info("promotion", extra={"event": "promotion_start", "rows_in": 5})

    obj = _read_json_lines(stream)[0]
    assert obj["event"] == "promotion_start"
    assert obj["rows_in"] == 5


def test_json_formatter_survives_non_serializable_value():
    class Weird:
        def __repr__(self):
            raise RuntimeError("boom repr")

        def __str__(self):
            return "weird-but-stringable"

    logger, stream = _local_json_logger("mdm.test.weird")
    # Non-serialisable extra value must not crash the formatter.
    logger.info("odd value", extra={"thing": Weird(), "obj": object()})

    line = stream.getvalue().strip()
    assert line  # something was emitted
    obj = json.loads(line)  # and it is valid JSON
    assert obj["message"] == "odd value"


def test_json_formatter_bad_format_args_does_not_raise():
    logger, stream = _local_json_logger("mdm.test.badfmt")
    # %s with no arg would raise inside getMessage(); formatter must swallow it.
    logger.info("missing %s arg")
    obj = _read_json_lines(stream)[0]
    assert "message" in obj


def test_json_formatter_renders_exc_info():
    logger, stream = _local_json_logger("mdm.test.exc")
    try:
        raise ValueError("kaboom")
    except ValueError:
        logger.exception("caught it")
    obj = _read_json_lines(stream)[0]
    assert "exc_info" in obj
    assert "ValueError" in obj["exc_info"]


# ------------------------------------------------------------ stream derivation
@pytest.mark.parametrize(
    "name,expected",
    [
        ("mdm", "platform"),
        ("mdm.platform", "platform"),
        ("mdm.something", "platform"),
        ("app.services.pipeline", "platform"),
        ("mdm.integration", "integration"),
        ("mdm.integration.pipeline", "integration"),
        ("mdm.custom", "custom"),
        ("mdm.custom.hooks", "custom"),
    ],
)
def test_stream_for(name, expected):
    assert lc.stream_for(name) == expected


def test_stream_logger_names_and_validation():
    assert lc.stream_logger("integration").name == "mdm.integration"
    assert lc.stream_logger("custom").name == "mdm.custom"
    assert lc.stream_logger("platform").name == "mdm.platform"
    with pytest.raises(ValueError):
        lc.stream_logger("nope")


def test_filter_stamps_stream_from_logger_name():
    logger, stream = _local_json_logger("mdm.integration.pipeline")
    logger.info("landing->staging")
    obj = _read_json_lines(stream)[0]
    assert obj["stream"] == "integration"


def test_integration_stream_records_carry_stream(caplog):
    # Records emitted through stream_logger('integration') derive stream=integration.
    filt = lc.ContextFilter()
    with caplog.at_level(logging.INFO, logger="mdm.integration"):
        rec = logging.LogRecord(
            name="mdm.integration.pipeline", level=logging.INFO, pathname="",
            lineno=0, msg="x", args=(), exc_info=None,
        )
        filt.filter(rec)
        assert rec.stream == "integration"


# --------------------------------------------------------------- request context
def test_request_context_fields_appear_when_set():
    logger, stream = _local_json_logger("mdm.test.ctx")
    tokens = lc.set_request_context(
        request_id="req-123", actor=None, method="POST", path="/api/v1/data"
    )
    lc.bind_actor("alice")  # actor is filled in later by auth, after set_request_context
    try:
        logger.info("inside request")
    finally:
        lc.reset_request_context(tokens)

    obj = _read_json_lines(stream)[0]
    assert obj["request_id"] == "req-123"
    assert obj["method"] == "POST"
    assert obj["path"] == "/api/v1/data"
    assert obj["actor"] == "alice"


def test_request_context_absent_outside_request():
    # After reset, no context keys leak into later records.
    logger, stream = _local_json_logger("mdm.test.noctx")
    logger.info("no request here")
    obj = _read_json_lines(stream)[0]
    for key in ("request_id", "actor", "method", "path"):
        assert key not in obj


# --------------------------------------------------------------- configure_logging
def test_configure_logging_text_mode_and_json_mode():
    orig_json = settings.LOG_JSON
    try:
        # Text mode: root handler uses the plain text formatter.
        settings.LOG_JSON = False
        lc.configure_logging()
        root = logging.getLogger()
        assert root.handlers, "configure_logging must attach a handler"
        assert not isinstance(root.handlers[0].formatter, lc.JsonFormatter)

        # JSON mode: root handler uses the JsonFormatter.
        settings.LOG_JSON = True
        lc.configure_logging()
        root = logging.getLogger()
        assert isinstance(root.handlers[0].formatter, lc.JsonFormatter)

        # Idempotent: a second call does not stack handlers.
        n = len(root.handlers)
        lc.configure_logging()
        assert len(logging.getLogger().handlers) == n
    finally:
        settings.LOG_JSON = orig_json
        lc.configure_logging()  # restore process-wide default config


def test_logging_config_info_reports_active_config():
    orig_json = settings.LOG_JSON
    try:
        settings.LOG_JSON = True
        info = lc.logging_config_info()
        assert info["json"] is True
        assert set(info["streams"]) == {"platform", "integration", "custom"}
        assert "level" in info
        assert "files" in info
    finally:
        settings.LOG_JSON = orig_json


def test_stream_files_written(tmp_path):
    orig = (settings.LOG_STREAM_FILES, settings.LOG_DIR, settings.LOG_JSON)
    try:
        settings.LOG_STREAM_FILES = True
        settings.LOG_DIR = str(tmp_path)
        settings.LOG_JSON = True
        lc.configure_logging()

        lc.stream_logger("integration").info("integration event")
        lc.stream_logger("custom").warning("custom event")
        for h in logging.getLogger("mdm").handlers:
            h.flush()

        integ = (tmp_path / "integration.log").read_text(encoding="utf-8")
        cust = (tmp_path / "custom.log").read_text(encoding="utf-8")
        assert "integration event" in integ
        # Each per-stream file keeps only its own stream.
        assert "integration event" not in cust
        assert "custom event" in cust
    finally:
        (settings.LOG_STREAM_FILES, settings.LOG_DIR, settings.LOG_JSON) = orig
        lc.configure_logging()  # restore default config
