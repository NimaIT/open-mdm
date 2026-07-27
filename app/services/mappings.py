"""Configurable field-to-field mapping: source schema -> target schema (EX-3).

Mappings are applied during PROMOTION (landing -> staging), keyed by the landing
row's ``mdm_source_system``, BEFORE ``validate_record``. This preserves the
capture-first invariant: the landing row still stores the RAW payload exactly as
sent; renaming/defaulting/transforming happens only on the way to staging.

For each matching mapping the source field is renamed to the target field, with
an optional per-mapping default (applied when the source value is empty) and an
optional transform spec (reusing the EX-2 transform registry). Unmapped keys pass
through unchanged. A mapping whose ``target_field`` is not an attribute of the
entity surfaces as a row error rather than crashing the promotion.
"""
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models import FieldMapping
from app.services.transforms import _is_empty, apply_transforms


def load_field_mappings(db: Session, entity_name: str) -> List[FieldMapping]:
    """All enabled mappings for an entity (both source-specific and global)."""
    return (
        db.query(FieldMapping)
        .filter(FieldMapping.entity_name == entity_name, FieldMapping.enabled.is_(True))
        .order_by(FieldMapping.source_system.nullsfirst(), FieldMapping.source_field)
        .all()
    )


def apply_field_mappings(
    entity,
    payload: Dict[str, Any],
    *,
    source_system: Optional[str],
    mappings: List[FieldMapping],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Build the staging-bound payload from the raw landing payload.

    Returns ``(mapped_payload, errors)``. ``payload`` is never mutated. A mapping
    applies when it is enabled and its ``source_system`` is NULL (all sources) or
    equals the landing row's source system.
    """
    relevant = [
        m for m in mappings
        if m.enabled and (m.source_system is None or m.source_system == source_system)
    ]
    if not relevant:
        return dict(payload), []

    attr_names = {a.name for a in entity.attributes}
    targets = {m.target_field for m in relevant}
    result = dict(payload)
    errors: List[Dict[str, Any]] = []

    for m in relevant:
        if m.target_field not in attr_names:
            errors.append({
                "field": m.target_field, "code": "unknown_mapping_target",
                "message": (
                    f"Field mapping (source '{m.source_field}') targets unknown "
                    f"attribute '{m.target_field}' on entity '{entity.name}'."
                ),
            })
            continue
        raw = payload.get(m.source_field)
        if _is_empty(raw) and m.default_value is not None:
            raw = m.default_value
        if m.transform:
            raw, terr = apply_transforms(raw, m.transform, field=m.target_field)
            if terr:
                errors.append(terr)
        result[m.target_field] = raw

    # Drop renamed source keys so they don't trip validate_record's unknown_field
    # check. Only drop keys that are neither real attributes nor a mapping target.
    for m in relevant:
        s = m.source_field
        if (s != m.target_field and s in result
                and s not in attr_names and s not in targets):
            del result[s]

    return result, errors
