"""Structured, stream-aware application logging (AO-1).

Delivers three things ops needs to run this platform against centralised log
aggregation (ELK / Splunk):

  1. **JSON structured logging** — one JSON object per line via :class:`JsonFormatter`,
     with an ISO-8601 UTC ``timestamp``, ``level``, ``stream``, ``logger``,
     ``message`` and any contextual / ``extra`` fields. The formatter never raises
     on non-serialisable values (it falls back to ``str``), so logging can never
     break a request path.

  2. **Named log streams** — every ``mdm`` record is stamped with a ``stream``
     derived from its logger name: ``mdm.integration.*`` -> ``integration``,
     ``mdm.custom.*`` -> ``custom``, everything else -> ``platform``. Use
     :func:`stream_logger` to obtain the canonical logger for a stream.

  3. **Request context** — a per-request ``request_id`` (plus ``actor`` / ``method``
     / ``path``) carried in :mod:`contextvars` and injected into every record, so a
     whole request's log lines share one id for correlation.

``configure_logging()`` wires the handlers from config and is idempotent. When
``LOG_STREAM_FILES`` + ``LOG_DIR`` are set it additionally writes each stream to
its own rotating JSON file for file-based ELK ingestion.
"""
import contextvars
import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config import settings

# --------------------------------------------------------------------- streams
STREAMS = ("platform", "integration", "custom")
_DEFAULT_STREAM = "platform"


def stream_for(logger_name: str) -> str:
    """Derive the stream name from a logger name.

    ``mdm.integration`` / ``mdm.integration.*`` -> ``integration``;
    ``mdm.custom`` / ``mdm.custom.*`` -> ``custom``; anything else -> ``platform``.
    """
    name = logger_name or ""
    if name == "mdm.integration" or name.startswith("mdm.integration."):
        return "integration"
    if name == "mdm.custom" or name.startswith("mdm.custom."):
        return "custom"
    return _DEFAULT_STREAM


def stream_logger(stream: str) -> logging.Logger:
    """Return the canonical ``mdm.<stream>`` logger for one of the named streams."""
    key = (stream or "").strip().lower()
    if key not in STREAMS:
        raise ValueError(f"Unknown log stream '{stream}'. Expected one of {STREAMS}.")
    return logging.getLogger(f"mdm.{key}")


# ------------------------------------------------------------- request context
_request_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "mdm_request_id", default=None
)
_actor: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "mdm_actor", default=None
)
_method: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "mdm_method", default=None
)
_path: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "mdm_path", default=None
)

_CONTEXT_VARS = {
    "request_id": _request_id,
    "actor": _actor,
    "method": _method,
    "path": _path,
}
_CONTEXT_KEYS = tuple(_CONTEXT_VARS)


def set_request_context(**fields: Optional[str]) -> Dict[str, contextvars.Token]:
    """Set request-context fields, returning reset tokens for :func:`reset_request_context`.

    Only the fields passed are set; unknown keys are ignored. Best-effort — never
    raises into a request path.
    """
    tokens: Dict[str, contextvars.Token] = {}
    for key, value in fields.items():
        var = _CONTEXT_VARS.get(key)
        if var is not None:
            tokens[key] = var.set(value)
    return tokens


def bind_actor(actor: Optional[str]) -> None:
    """Stamp the resolved actor onto the current request context (best-effort)."""
    try:
        _actor.set(actor)
    except Exception:  # pragma: no cover - contextvars.set never raises here
        pass


def reset_request_context(tokens: Dict[str, contextvars.Token]) -> None:
    """Reset context vars using tokens from :func:`set_request_context`."""
    for key, token in (tokens or {}).items():
        var = _CONTEXT_VARS.get(key)
        if var is not None:
            try:
                var.reset(token)
            except Exception:  # pragma: no cover - defensive
                pass


def current_request_context() -> Dict[str, str]:
    """The non-empty request-context fields for the current context."""
    out: Dict[str, str] = {}
    for key, var in _CONTEXT_VARS.items():
        value = var.get()
        if value is not None:
            out[key] = value
    return out


# --------------------------------------------------------------------- filter
class ContextFilter(logging.Filter):
    """Stamp every record with its derived ``stream`` and request-context fields.

    Attached to handlers (not loggers) so it runs for propagated records too.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            record.stream = stream_for(record.name)
            ctx = current_request_context()
            for key in _CONTEXT_KEYS:
                # Only set when present so records outside a request stay clean and
                # so an explicit `extra=` value is never clobbered by a None.
                if key in ctx and getattr(record, key, None) is None:
                    setattr(record, key, ctx[key])
        except Exception:  # pragma: no cover - a filter must never break logging
            pass
        return True


class _StreamFileFilter(logging.Filter):
    """Pass only records whose derived stream matches, for per-stream files."""

    def __init__(self, stream: str):
        super().__init__()
        self.stream = stream

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return stream_for(record.name) == self.stream


# ------------------------------------------------------------------ formatter
# Standard LogRecord attributes we must NOT re-emit as "extra" fields.
_RESERVED_KEYS = set(
    vars(
        logging.LogRecord(
            name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
        )
    )
) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
    "stream",
    *_CONTEXT_KEYS,
}


class JsonFormatter(logging.Formatter):
    """Render a LogRecord as a single line of JSON.

    Guaranteed crash-proof: the whole body is wrapped so a non-serialisable value
    (handled by ``default=str``), a bad ``%`` format string, or any other oddity
    yields a minimal fallback line instead of raising back into the caller.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        try:
            return self._format(record)
        except Exception:  # never raise into the logging machinery / request path
            return self._fallback(record)

    def _format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:
            message = str(getattr(record, "msg", ""))

        payload: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "stream": getattr(record, "stream", None) or _DEFAULT_STREAM,
            "logger": record.name,
            "message": message,
        }

        # Request-context correlation fields (only when set).
        for key in _CONTEXT_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value

        # Any structured `extra={...}` fields the call site attached.
        for key, value in record.__dict__.items():
            if key in _RESERVED_KEYS or key in payload or key.startswith("_"):
                continue
            payload[key] = value

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, ensure_ascii=False)

    @staticmethod
    def _fallback(record: logging.LogRecord) -> str:
        try:
            return json.dumps(
                {
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    "level": getattr(record, "levelname", "ERROR"),
                    "stream": _DEFAULT_STREAM,
                    "logger": getattr(record, "name", "mdm"),
                    "message": "log-format-error",
                    "raw": str(getattr(record, "msg", "")),
                },
                default=str,
            )
        except Exception:  # pragma: no cover - last resort, still no raise
            return '{"level":"ERROR","stream":"platform","message":"log-format-error"}'


TEXT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

# Per-stream rotating-file sizing. Kept modest; ELK/Splunk ships them onward.
_FILE_MAX_BYTES = 10 * 1024 * 1024
_FILE_BACKUP_COUNT = 5


# ------------------------------------------------------------------- configure
def _resolve_level() -> int:
    if settings.DEBUG:
        return logging.DEBUG
    return getattr(logging, (settings.LOG_LEVEL or "INFO").upper(), logging.INFO)


def configure_logging() -> None:
    """(Re)configure the root and ``mdm`` logger handlers from settings.

    Idempotent: existing handlers on the root and ``mdm`` loggers are cleared and
    rebuilt, so calling this more than once never duplicates output.
    """
    level = _resolve_level()
    root = logging.getLogger()
    mdm = logging.getLogger("mdm")

    # Clear prior handlers so re-invocation is clean (idempotent).
    for logger in (root, mdm):
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - defensive
                pass

    ctx_filter = ContextFilter()
    formatter: logging.Formatter = (
        JsonFormatter() if settings.LOG_JSON else logging.Formatter(TEXT_FORMAT)
    )

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(ctx_filter)
    root.addHandler(stream_handler)

    root.setLevel(level)
    mdm.setLevel(level)
    # mdm records still propagate to the root stdout handler above.
    mdm.propagate = True

    # Optional per-stream rotating files (always JSON — meant for ELK ingestion).
    if settings.LOG_STREAM_FILES and settings.LOG_DIR:
        try:
            os.makedirs(settings.LOG_DIR, exist_ok=True)
            for stream in STREAMS:
                fh = logging.handlers.RotatingFileHandler(
                    os.path.join(settings.LOG_DIR, f"{stream}.log"),
                    maxBytes=_FILE_MAX_BYTES,
                    backupCount=_FILE_BACKUP_COUNT,
                    encoding="utf-8",
                )
                fh.setFormatter(JsonFormatter())
                fh.addFilter(ctx_filter)
                fh.addFilter(_StreamFileFilter(stream))
                # Attach to the mdm logger: every mdm.* record propagates here and
                # each file keeps only its own stream.
                mdm.addHandler(fh)
        except Exception:  # pragma: no cover - never let file setup break startup
            logging.getLogger("mdm").warning(
                "Could not set up per-stream log files in %s", settings.LOG_DIR
            )


def logging_config_info() -> Dict[str, Any]:
    """The active logging configuration, for the ops verification endpoint."""
    files: List[str] = []
    if settings.LOG_STREAM_FILES and settings.LOG_DIR:
        files = [os.path.join(settings.LOG_DIR, f"{s}.log") for s in STREAMS]
    return {
        "json": bool(settings.LOG_JSON),
        "level": logging.getLevelName(_resolve_level()),
        "streams": list(STREAMS),
        "stream_files_enabled": bool(settings.LOG_STREAM_FILES),
        "log_dir": settings.LOG_DIR,
        "files": files,
    }
