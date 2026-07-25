"""Import and export of entity definitions as JSON or YAML.

The exported document is the portable, version-controllable representation of
a data model — the intended workflow is to design in one environment, export,
commit to Git, then import into the next environment.
"""
import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import yaml
from sqlalchemy.orm import Session

from app.models import Attribute, Entity, EntityStatus, ModelVersion
from app.services.identifiers import (
    IdentifierError,
    SUPPORTED_TYPES,
    validate_column_name,
    validate_ident,
)

SCHEMA_VERSION = "1.0"

ATTR_FIELDS = (
    "name", "display_name", "description", "data_type", "length",
    "numeric_precision", "numeric_scale", "is_required", "is_unique",
    "is_business_key", "is_match_key", "is_indexed", "is_pii",
    "default_value", "validation", "normalization", "ref_entity", "ref_attribute",
)
ENTITY_FIELDS = (
    "name", "display_name", "description", "domain", "requires_approval",
    "soft_delete", "auto_approve_threshold", "retention_days",
)


# ------------------------------------------------------------------- export
def entity_to_dict(entity: Entity, *, include_runtime: bool = False) -> Dict:
    doc: Dict = {f: getattr(entity, f) for f in ENTITY_FIELDS}
    doc["attributes"] = [
        {f: getattr(a, f) for f in ATTR_FIELDS} for a in
        sorted(entity.attributes, key=lambda a: a.position)
    ]
    if include_runtime:
        doc["_runtime"] = {
            "status": entity.status,
            "version": entity.version,
            "published_version": entity.published_version,
            "published_at": entity.published_at.isoformat()
            if entity.published_at else None,
        }
    return doc


def export_models(
    entities: List[Entity], *, fmt: str = "json", include_runtime: bool = False
) -> str:
    doc = {
        "mdm_schema_version": SCHEMA_VERSION,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "entities": [entity_to_dict(e, include_runtime=include_runtime) for e in entities],
    }
    fmt = (fmt or "json").lower()
    if fmt in {"yaml", "yml"}:
        return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100)
    if fmt == "json":
        return json.dumps(doc, indent=2, default=str)
    raise ValueError(f"Unsupported export format '{fmt}' (use json or yaml)")


# ------------------------------------------------------------------- import
def parse_document(content: str, fmt: Optional[str] = None) -> Dict:
    """Parse an import file, sniffing the format when not specified."""
    text = content.strip()
    if not text:
        raise ValueError("Import file is empty")
    if fmt is None:
        fmt = "json" if text[0] in "{[" else "yaml"
    try:
        doc = json.loads(text) if fmt == "json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not parse {fmt} document: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError("Import document must be a mapping at the top level")
    return doc


def validate_document(doc: Dict) -> Tuple[List[Dict], List[str]]:
    """Structurally validate an import document.

    Returns (entities, errors). Never raises on content problems so the UI can
    show every issue at once rather than one at a time.
    """
    errors: List[str] = []
    version = str(doc.get("mdm_schema_version", SCHEMA_VERSION))
    if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        errors.append(
            f"Document schema version {version} is incompatible with "
            f"{SCHEMA_VERSION} supported by this server."
        )

    raw_entities = doc.get("entities")
    if raw_entities is None and "name" in doc:
        raw_entities = [doc]  # allow a single bare entity document
    if not isinstance(raw_entities, list) or not raw_entities:
        errors.append("Document contains no entities.")
        return [], errors

    seen_names = set()
    cleaned: List[Dict] = []
    for idx, raw in enumerate(raw_entities):
        label = f"entities[{idx}]"
        if not isinstance(raw, dict):
            errors.append(f"{label}: must be a mapping.")
            continue
        try:
            name = validate_ident(str(raw.get("name", "")), kind="entity name")
        except IdentifierError as exc:
            errors.append(f"{label}: {exc}")
            continue
        label = f"entity '{name}'"
        if name in seen_names:
            errors.append(f"{label}: duplicated in the document.")
            continue
        seen_names.add(name)

        attrs = raw.get("attributes")
        if not isinstance(attrs, list) or not attrs:
            errors.append(f"{label}: must define at least one attribute.")
            continue

        clean_attrs: List[Dict] = []
        seen_attr = set()
        for a_idx, attr in enumerate(attrs):
            alabel = f"{label} attributes[{a_idx}]"
            if not isinstance(attr, dict):
                errors.append(f"{alabel}: must be a mapping.")
                continue
            try:
                aname = validate_column_name(str(attr.get("name", "")))
            except IdentifierError as exc:
                errors.append(f"{alabel}: {exc}")
                continue
            if aname in seen_attr:
                errors.append(f"{label}: attribute '{aname}' is duplicated.")
                continue
            seen_attr.add(aname)

            dtype = str(attr.get("data_type", "")).lower()
            if dtype not in SUPPORTED_TYPES:
                errors.append(
                    f"{label}.{aname}: unsupported data_type '{dtype}'. "
                    f"Supported: {', '.join(SUPPORTED_TYPES)}"
                )
                continue

            validation = attr.get("validation") or {}
            if not isinstance(validation, dict):
                errors.append(f"{label}.{aname}: 'validation' must be a mapping.")
                validation = {}
            normalization = attr.get("normalization") or []
            if isinstance(normalization, str):
                normalization = [normalization]
            if not isinstance(normalization, list):
                errors.append(f"{label}.{aname}: 'normalization' must be a list.")
                normalization = []

            clean = {
                "name": aname,
                "display_name": attr.get("display_name") or aname.replace("_", " ").title(),
                "description": attr.get("description"),
                "data_type": dtype,
                "length": _int_or_none(attr.get("length")),
                "numeric_precision": _int_or_none(attr.get("numeric_precision")),
                "numeric_scale": _int_or_none(attr.get("numeric_scale")),
                "is_required": bool(attr.get("is_required", False)),
                "is_unique": bool(attr.get("is_unique", False)),
                "is_business_key": bool(attr.get("is_business_key", False)),
                "is_match_key": bool(attr.get("is_match_key", False)),
                "is_indexed": bool(attr.get("is_indexed", False)),
                "is_pii": bool(attr.get("is_pii", False)),
                "default_value": attr.get("default_value"),
                "validation": validation,
                "normalization": normalization,
                "ref_entity": attr.get("ref_entity"),
                "ref_attribute": attr.get("ref_attribute"),
                "position": a_idx,
            }
            clean_attrs.append(clean)

        if not clean_attrs:
            errors.append(f"{label}: no valid attributes remain after validation.")
            continue

        if not any(a["is_business_key"] for a in clean_attrs):
            errors.append(
                f"{label}: warning — no business key defined; updates will "
                "require an explicit mdm_id."
            )

        cleaned.append(
            {
                "name": name,
                "display_name": raw.get("display_name") or name.replace("_", " ").title(),
                "description": raw.get("description"),
                "domain": raw.get("domain"),
                "requires_approval": bool(raw.get("requires_approval", True)),
                "soft_delete": bool(raw.get("soft_delete", True)),
                "auto_approve_threshold": _int_or_none(raw.get("auto_approve_threshold")),
                "retention_days": _int_or_none(raw.get("retention_days")),
                "attributes": clean_attrs,
            }
        )
    return cleaned, errors


def _int_or_none(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def diff_against_existing(db: Session, defs: List[Dict]) -> List[Dict]:
    """Describe what an import would change, without changing anything."""
    report: List[Dict] = []
    for d in defs:
        existing = db.query(Entity).filter(Entity.name == d["name"]).one_or_none()
        if existing is None:
            report.append(
                {
                    "entity": d["name"],
                    "action": "create",
                    "attributes_added": [a["name"] for a in d["attributes"]],
                    "attributes_removed": [],
                    "attributes_changed": [],
                    "deployed": False,
                }
            )
            continue

        current = {a.name: a for a in existing.attributes}
        incoming = {a["name"]: a for a in d["attributes"]}
        added = sorted(set(incoming) - set(current))
        removed = sorted(set(current) - set(incoming))
        changed = []
        for name in sorted(set(current) & set(incoming)):
            cur, inc = current[name], incoming[name]
            deltas = {}
            for field in ("data_type", "length", "is_required", "is_unique",
                          "numeric_precision", "numeric_scale"):
                old, new = getattr(cur, field), inc.get(field)
                if old != new:
                    deltas[field] = {"from": old, "to": new}
            if deltas:
                changed.append({"attribute": name, "changes": deltas})
        report.append(
            {
                "entity": d["name"],
                "action": "update" if (added or removed or changed) else "no_change",
                "attributes_added": added,
                "attributes_removed": removed,
                "attributes_changed": changed,
                "deployed": existing.is_deployed,
                "warning": (
                    f"{len(removed)} attribute(s) would be removed from a deployed "
                    "entity — this is a destructive change requiring confirmation."
                )
                if removed and existing.is_deployed
                else None,
            }
        )
    return report


def apply_import(
    db: Session,
    defs: List[Dict],
    *,
    actor: str,
    replace: bool = False,
) -> List[Dict]:
    """Create or update entity definitions from parsed definitions.

    This only writes *metadata*. Publishing the DDL is a separate, deliberate
    step — importing a model never silently mutates the physical database.
    """
    results: List[Dict] = []
    for d in defs:
        entity = db.query(Entity).filter(Entity.name == d["name"]).one_or_none()
        attrs = d.pop("attributes")

        if entity is None:
            entity = Entity(**d, status=EntityStatus.DRAFT.value, version=1,
                            created_by=actor, updated_by=actor)
            db.add(entity)
            db.flush()
            for a in attrs:
                db.add(Attribute(entity_id=entity.id, created_by=actor,
                                 updated_by=actor, **a))
            action = "created"
        else:
            for k, v in d.items():
                setattr(entity, k, v)
            entity.updated_by = actor
            entity.version += 1
            if entity.status == EntityStatus.PUBLISHED.value:
                entity.status = EntityStatus.MODIFIED.value

            current = {a.name: a for a in entity.attributes}
            incoming = {a["name"]: a for a in attrs}
            for name, a in incoming.items():
                if name in current:
                    for k, v in a.items():
                        setattr(current[name], k, v)
                    current[name].updated_by = actor
                else:
                    db.add(Attribute(entity_id=entity.id, created_by=actor,
                                     updated_by=actor, **a))
            if replace:
                for name, obj in current.items():
                    if name not in incoming:
                        db.delete(obj)
            action = "updated"

        db.flush()
        db.refresh(entity)
        db.add(
            ModelVersion(
                entity_id=entity.id,
                version=entity.version,
                snapshot=entity_to_dict(entity),
                change_note=f"Imported ({action}) by {actor}",
                created_by=actor,
            )
        )
        results.append(
            {"entity": entity.name, "action": action, "version": entity.version,
             "attributes": len(attrs)}
        )
    return results
