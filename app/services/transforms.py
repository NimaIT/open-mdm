"""Named, parameterised transformation functions for the ingestion pipeline (EX-2).

These go *beyond* the fixed ``normalization`` rules in ``validation.py`` (which
stay exactly as they are). An attribute's ``transforms`` list is applied AFTER
normalisation and coercion, in order, by ``validation.validate_record``. Field
mappings (EX-3) reuse the same registry for their optional per-mapping transform.

Contract and safety:
  * A transform is ``fn(value, **params) -> value``. It operates on a single,
    already-coerced field value.
  * A spec is either a bare string (the transform name) or a mapping
    ``{"fn": name, ...params}`` (``"name"`` is accepted as an alias for ``"fn"``).
  * A transform that RAISES becomes a structured validation error — the row is
    held invalid in staging, never a crash, never a poisoned transaction.
  * An UNKNOWN transform name becomes a clear config error on that field.

Operators register custom transforms with ``register_transform`` / ``@transform``
from a module listed in ``HOOK_MODULES`` (loaded at startup); the module only has
to import to take effect.
"""
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.services.logging_config import stream_logger

# Operator-supplied transforms are "custom logic" -> "custom" stream (AO-1).
log = stream_logger("custom")

TransformFn = Callable[..., Any]

_REGISTRY: Dict[str, TransformFn] = {}
# Registration/reset is startup-only, but guard registry mutation against
# concurrent iteration on the hot ingestion path. Keep it minimal.
_LOCK = threading.Lock()


class TransformError(Exception):
    """A transform could not be applied (bad params, or an unknown name)."""


# ------------------------------------------------------------------ registry
def register_transform(name: str, func: TransformFn, *, overwrite: bool = True) -> None:
    """Register a named transform. Operators call this from a HOOK_MODULES module."""
    key = (name or "").strip().lower()
    if not key:
        raise TransformError("A transform must have a non-empty name.")
    with _LOCK:
        if key in _REGISTRY and not overwrite:
            raise TransformError(f"Transform '{key}' is already registered.")
        _REGISTRY[key] = func


def transform(name: str) -> Callable[[TransformFn], TransformFn]:
    """Decorator form of :func:`register_transform`."""

    def _deco(func: TransformFn) -> TransformFn:
        register_transform(name, func)
        return func

    return _deco


def registered_transforms() -> List[str]:
    with _LOCK:
        return sorted(_REGISTRY)


# ------------------------------------------------------------------ helpers
def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def normalize_spec(spec: Any) -> Tuple[Optional[str], Dict[str, Any]]:
    """Split a transform spec into ``(name, params)``.

    Accepts a bare string, or a mapping carrying ``fn``/``name`` plus params.
    """
    if isinstance(spec, str):
        return spec.strip().lower(), {}
    if isinstance(spec, dict):
        name = spec.get("fn") or spec.get("name")
        params = {k: v for k, v in spec.items() if k not in ("fn", "name")}
        return (str(name).strip().lower() if name else None), params
    raise TransformError(f"Invalid transform spec: {spec!r}")


def as_spec_list(specs: Any) -> List[Any]:
    """Coerce a transforms field (None / str / dict / list) into a list of specs."""
    if specs is None:
        return []
    if isinstance(specs, (str, dict)):
        return [specs]
    if isinstance(specs, list):
        return specs
    raise TransformError(f"Invalid transforms value: {specs!r}")


def apply_transforms(
    value: Any, specs: Any, *, field: str = "value"
) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """Apply an ordered list of transform specs to a value.

    Returns ``(value, error)`` where ``error`` is ``None`` on success or a
    structured error dict (``field``/``code``/``message``) on the first failure.
    Application stops at the first error, matching the fail-fast behaviour of
    type coercion. Never raises for config/runtime problems.
    """
    try:
        spec_list = as_spec_list(specs)
    except TransformError as exc:
        return value, {"field": field, "code": "bad_transform_config",
                       "message": str(exc)}
    for spec in spec_list:
        try:
            name, params = normalize_spec(spec)
        except TransformError as exc:
            return value, {"field": field, "code": "bad_transform_config",
                           "message": str(exc)}
        with _LOCK:
            fn = _REGISTRY.get(name) if name else None
        if fn is None:
            return value, {
                "field": field, "code": "unknown_transform",
                "message": f"Unknown transform '{name}' on '{field}'. "
                           f"Available: {', '.join(registered_transforms())}",
            }
        try:
            value = fn(value, **params)
        except TypeError as exc:
            # Almost always a bad/missing parameter for the transform.
            log.warning(
                "transform '%s' misconfigured on field '%s': %s", name, field, exc,
                extra={"event": "transform_failed", "transform": name,
                       "field": field, "code": "bad_transform_config"},
            )
            return value, {"field": field, "code": "bad_transform_config",
                           "message": f"Transform '{name}' on '{field}': {exc}"}
        except Exception as exc:  # runtime failure — hold the row invalid
            log.warning(
                "transform '%s' failed on field '%s': %s", name, field, exc,
                extra={"event": "transform_failed", "transform": name,
                       "field": field, "code": "transform_error"},
            )
            return value, {"field": field, "code": "transform_error",
                           "message": f"Transform '{name}' failed on '{field}': {exc}"}
    return value, None


# --------------------------------------------------------------- built-ins
def _upper(v, **_):
    return v.upper() if isinstance(v, str) else (None if v is None else str(v).upper())


def _lower(v, **_):
    return v.lower() if isinstance(v, str) else (None if v is None else str(v).lower())


def _trim(v, **_):
    return v.strip() if isinstance(v, str) else v


def _title(v, **_):
    return v.title() if isinstance(v, str) else (None if v is None else str(v).title())


def _strip_punctuation(v, **_):
    if v is None:
        return None
    return re.sub(r"[^\w\s]", "", str(v))


def _digits_only(v, **_):
    if v is None:
        return None
    return re.sub(r"\D", "", str(v))


def _default(v, *, value=None, **_):
    """Substitute ``value`` when the field is empty/None; otherwise keep it."""
    return value if _is_empty(v) else v


def _coalesce(v, *, values=None, **_):
    """Return the field value if present, else the first non-empty candidate."""
    if not _is_empty(v):
        return v
    for cand in values or []:
        if not _is_empty(cand):
            return cand
    return v


def _map(v, *, mapping=None, default=None, **_):
    """Value substitution, e.g. ``{"US": "United States"}``.

    A value not present in the mapping is passed through unchanged (or replaced
    by ``default`` when one is supplied).
    """
    if v is None:
        return None
    table = mapping or {}
    key = str(v)
    if key in table:
        return table[key]
    if v in table:
        return table[v]
    return default if default is not None else v


def _left(v, *, n=None, **_):
    if v is None:
        return None
    if n is None:
        raise TransformError("left requires an 'n' parameter (number of chars).")
    return str(v)[: int(n)]


def _regex_replace(v, *, pattern=None, repl="", **_):
    if v is None:
        return None
    if pattern is None:
        raise TransformError("regex_replace requires a 'pattern' parameter.")
    return re.sub(pattern, repl, str(v))


_BUILTINS: Dict[str, TransformFn] = {
    "upper": _upper,
    "lower": _lower,
    "trim": _trim,
    "title": _title,
    "strip_punctuation": _strip_punctuation,
    "digits_only": _digits_only,
    "default": _default,
    "coalesce": _coalesce,
    "map": _map,
    "left": _left,
    "regex_replace": _regex_replace,
}


def reset_transforms() -> None:
    """Restore the registry to exactly the built-ins.

    Drops any operator-registered custom transforms. Used by tests to keep the
    process-wide registry from leaking registrations between cases.
    """
    with _LOCK:
        _REGISTRY.clear()
        _REGISTRY.update(_BUILTINS)


reset_transforms()
