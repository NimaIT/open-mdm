"""Reference-data resolution: human-readable value -> parent golden-record id.

This implements DQ-2 (reference-data lookup) and DQ-5 (record-level dependency
ordering) for ``reference`` attributes.

A ``reference`` attribute physically stores the parent entity's ``mdm_id`` (a
uuid). Inbound writes, however, usually carry a human-readable value (a business
key such as a country code or a supplier name). During promotion each reference
value is resolved against the parent's live tier:

  * a value that is already a uuid of an existing live parent is kept as-is;
  * otherwise the parent live table is searched, case-insensitively and trimmed,
    on the configured ``ref_attribute`` (defaulting to the parent's first
    business key);
  * an unresolvable value attaches a soft ``broken_reference`` error and the
    column is nulled — the row is stored in staging (invalid), never rejected,
    so a steward can see it and so it can be unblocked automatically once the
    parent arrives (DQ-5).

All identifiers are validated/quoted and all values are bound; the module never
interpolates raw SQL.
"""
import json
import uuid as _uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.config import settings
from app.services.identifiers import (
    IdentifierError,
    qualified,
    quote_ident,
    validate_column_name,
    validate_ident,
)
from app.services.logging_config import stream_logger
from app.services.validation import build_match_key

# Reference resolution is part of ingestion -> "integration" stream (AO-1).
log = stream_logger("integration")


def _dumps(obj) -> str:
    def _default(value):
        if isinstance(value, _uuid.UUID):
            return str(value)
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    return json.dumps(obj, default=_default)


def reference_attributes(entity) -> List:
    """The entity's ``reference`` attributes."""
    return [a for a in entity.attributes if (a.data_type or "").lower() == "reference"]


def _is_uuid(value: Any) -> bool:
    if isinstance(value, _uuid.UUID):
        return True
    try:
        _uuid.UUID(str(value).strip())
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _resolve_ref_column(
    db: Session, attr, cache: Optional[Dict[str, Optional[str]]] = None
) -> Optional[str]:
    """The parent attribute used to resolve inbound values.

    Explicit ``ref_attribute`` wins; otherwise fall back to the parent's first
    business key. Returns None when neither is available (resolution then only
    succeeds for values that are already a valid parent uuid).

    ``cache`` (optional, keyed by parent entity name) memoises the fallback
    business-key lookup across a resolve pass, avoiding an N+1 metadata query
    per inbound row.
    """
    if attr.ref_attribute:
        try:
            return validate_column_name(attr.ref_attribute)
        except IdentifierError:
            return None
    key = attr.ref_entity
    if not key:
        return None
    if cache is not None and key in cache:
        return cache[key]
    from app.models import Entity

    parent = db.query(Entity).filter(Entity.name == key).one_or_none()
    result = None
    if parent is not None:
        bkeys = parent.business_key
        if bkeys:
            result = bkeys[0].name
    if cache is not None:
        cache[key] = result
    return result


def resolve_references(
    db: Session,
    conn: Connection,
    entity,
    values: Dict[str, Any],
    errors: List,
    *,
    ref_column_cache: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Any]:
    """Resolve every present reference value in ``values`` to a parent mdm_id.

    Mutates ``values`` in place (resolved uuid, or None when unresolvable) and
    appends ``broken_reference`` entries to ``errors``. Returns ``values``.

    ``ref_column_cache`` may be shared across many rows of the same entity to
    memoise the parent business-key lookup (avoids a per-row N+1).
    """
    for attr in reference_attributes(entity):
        raw = values.get(attr.name)
        if raw is None:
            continue

        # The raw inbound value is preserved IN FULL on the error so DQ-5
        # re-resolution can retry it once the parent arrives — the uuid column
        # itself is nulled (it cannot hold a human-readable value). Truncating
        # here would make a longer legitimate lookup value unresolvable forever.
        raw_value = None if raw is None else str(raw)

        parent_name = attr.ref_entity
        if not parent_name:
            errors.append(
                {
                    "field": attr.name,
                    "code": "broken_reference",
                    "value": raw_value,
                    "message": (
                        f"'{attr.name}' is a reference attribute with no ref_entity "
                        "configured; cannot resolve."
                    ),
                }
            )
            values[attr.name] = None
            continue

        try:
            ref_name = validate_ident(parent_name, kind="ref_entity")
        except IdentifierError:
            errors.append(
                {
                    "field": attr.name,
                    "code": "broken_reference",
                    "value": raw_value,
                    "message": f"Invalid ref_entity '{parent_name}' for '{attr.name}'.",
                }
            )
            values[attr.name] = None
            continue

        parent_live = qualified(settings.SCHEMA_LIVE, ref_name)

        # 1. Already a uuid pointing at a live (non-deleted) parent -> keep it.
        if _is_uuid(raw):
            found = conn.execute(
                text(
                    f"select mdm_id from {parent_live} "
                    "where mdm_id = cast(:v as uuid) and mdm_is_deleted = false"
                ),
                {"v": str(raw).strip()},
            ).scalar()
            if found:
                # Keep the DB-returned uuid object so it binds back to a uuid
                # column cleanly (a str would need an explicit text->uuid cast).
                values[attr.name] = found
                continue

        # 2. Resolve the human-readable value against the parent attribute.
        ref_col = _resolve_ref_column(db, attr, ref_column_cache)
        if ref_col:
            matches = conn.execute(
                text(
                    f"select mdm_id from {parent_live} "
                    f"where lower(trim({quote_ident(ref_col)}::text)) "
                    "= lower(trim(:v)) and mdm_is_deleted = false limit 2"
                ),
                {"v": str(raw)},
            ).scalars().all()
            if len(matches) == 1:
                values[attr.name] = matches[0]
                continue

        # 3. Unresolvable -> soft error, hold in staging.
        errors.append(
            {
                "field": attr.name,
                "code": "broken_reference",
                "value": raw_value,
                "message": (
                    f"Could not resolve '{raw}' to a '{ref_name}' record"
                    + (f" by {ref_col}." if ref_col else " (no lookup key available).")
                ),
            }
        )
        values[attr.name] = None
        # Structured integration event — no raw value at INFO (may be sensitive).
        log.info(
            "reference resolution failed entity=%s field=%s ref_entity=%s",
            getattr(entity, "name", None), attr.name, ref_name,
            extra={"event": "reference_unresolved",
                   "entity": getattr(entity, "name", None),
                   "field": attr.name, "ref_entity": ref_name,
                   "ref_column": ref_col},
        )

    return values


def reresolve_broken_references(
    db: Session, conn: Connection, entity, *, limit: int = 500
) -> Dict[str, int]:
    """Re-resolve staging rows blocked on a missing parent (DQ-5).

    Finds invalid staging rows whose errors include ``broken_reference``, re-runs
    resolution against the current live tier, and, when the parent now exists,
    updates the stored reference column, drops the resolved errors and flips the
    row valid (if no other errors remain). Bounded by ``limit``.
    """
    ref_attrs = reference_attributes(entity)
    if not ref_attrs:
        return {"checked": 0, "unblocked": 0}

    staging_t = qualified(settings.SCHEMA_STAGING, entity.name)
    ref_names = [a.name for a in ref_attrs]

    # A reference attribute may itself be a match/business key. When such a
    # reference is unblocked (null -> resolved uuid), the row's stored
    # mdm_match_key — built while the reference was still null — is stale and
    # must be recomputed. That needs every key column, so load them too.
    key_attrs = entity.match_keys or entity.business_key
    key_names = [a.name for a in key_attrs]
    ref_in_key = any(n in key_names for n in ref_names)
    select_names = list(dict.fromkeys(ref_names + key_names))
    col_sql = ", ".join(quote_ident(c) for c in select_names)

    rows = conn.execute(
        text(
            f"select mdm_staging_id, mdm_errors, mdm_match_key, {col_sql} "
            f"from {staging_t} "
            "where mdm_is_valid = false "
            "and mdm_errors @> '[{\"code\": \"broken_reference\"}]'::jsonb "
            "and mdm_status in ('pending_review', 'changes_requested') "
            "order by mdm_staging_id limit :lim"
        ),
        {"lim": limit},
    ).mappings().all()

    ref_column_cache: Dict[str, Any] = {}
    unblocked = 0
    for row in rows:
        existing = row["mdm_errors"] or []
        if isinstance(existing, str):
            existing = json.loads(existing)
        # Keep any errors unrelated to reference resolution.
        kept = [e for e in existing if e.get("code") != "broken_reference"]
        # The original inbound value was preserved on each broken_reference
        # error (the uuid column was nulled). Retry from that; fall back to the
        # stored column for already-resolved references.
        broken_values = {
            e.get("field"): e.get("value")
            for e in existing
            if e.get("code") == "broken_reference"
        }

        values = {}
        for a in ref_attrs:
            if a.name in broken_values and broken_values[a.name] is not None:
                values[a.name] = broken_values[a.name]
            else:
                values[a.name] = row[a.name]
        new_errors: List = []
        resolve_references(
            db, conn, entity, values, new_errors,
            ref_column_cache=ref_column_cache,
        )

        merged = kept + new_errors
        is_valid = not merged

        set_sql = ", ".join(f"{quote_ident(a.name)} = :v_{a.name}" for a in ref_attrs)
        bind: Dict[str, Any] = {f"v_{a.name}": values.get(a.name) for a in ref_attrs}
        bind.update(
            id=row["mdm_staging_id"], errs=_dumps(merged), valid=is_valid
        )

        # Recompute the match key if a reference that participates in it just
        # resolved. Only overwrite with a fully-formed key (all key parts
        # present) so a still-broken row never has its key nulled out.
        if ref_in_key:
            key_values = {
                a.name: values[a.name] if a.name in values else row[a.name]
                for a in key_attrs
            }
            new_match_key = build_match_key(key_values, entity)
            if new_match_key is not None and new_match_key != row["mdm_match_key"]:
                set_sql += ", mdm_match_key = :mk"
                bind["mk"] = new_match_key

        conn.execute(
            text(
                f"update {staging_t} set {set_sql}, "
                "mdm_errors = cast(:errs as jsonb), mdm_is_valid = :valid "
                "where mdm_staging_id = :id"
            ),
            bind,
        )
        if is_valid:
            unblocked += 1

    return {"checked": len(rows), "unblocked": unblocked}
