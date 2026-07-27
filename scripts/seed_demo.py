"""Publish the demo models and seed a rich, demo-ready dataset.

Everything here runs through the REAL platform services — real DDL publish, real
four-tier pipeline, real maker-checker workflow with segregation of duties, real
reference resolution, real notifications outbox. Nothing is inserted directly
into a golden table; every golden record is produced by an approved promotion.

Run order (idempotent — it calls scripts.reset_demo first):

    python -m scripts.reset_demo && python -m scripts.seed_demo

What it does:
  1. Import examples/models.yaml and publish all 9 entities (creates the four
     tiers + mdm_pub matviews and reconciles cross-entity reference FKs).
  2. Create the four governance domains.
  3. Create demo users (local, known passwords) showcasing AC-1 / AC-3.
  4. Seed reference/lookup/parent data to golden (owners, timezones,
     capabilities, regions, status codes).
  5. Seed services (UC-1): valid ones approved / left pending / changes-requested
     / rejected, plus a broken-reference row and an invalid-enum row held in
     staging, plus service_capability junction rows (DM-3).
  6. Seed classifications (UC-2) left across the workflow.
  7. Bulk-load demand_forecast (UC-3).
  8. Create a per-domain notification template (NT-2) and a field mapping (EX-3),
     pushing one legacy-shaped record through the mapping.
  9. Refresh the distribution matviews (DD-1/DD-2).
"""
import pathlib
import sys
from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy import text

from app.db import get_ddl_engine, get_engine, session_scope
from app.models import (
    Domain,
    Entity,
    EntityStatus,
    FieldMapping,
    NotificationTemplate,
    User,
    UserSource,
)
from app.services import notifications, workflow
from app.services import scheduler as scheduler_svc
from app.services.auth import hash_password
from app.services.ddl import (
    apply_plan,
    build_alter_plan,
    build_create_plan,
    reconcile_enum_constraints,
    reconcile_matview,
    reconcile_reference_constraints,
)
from app.services.identifiers import qualified
from app.services.model_io import apply_import, parse_document, validate_document
from app.services.pipeline import (
    OP_INSERT,
    apply_staging_to_live,
    promote_landing_to_staging,
    reject_staging,
    write_to_landing,
)

import scripts.reset_demo as reset_demo

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples" / "models.yaml"

# Publish parents before children so reference FKs wire cleanly (reconciliation
# is order-resilient, but this keeps the publish output warning-free).
PUBLISH_ORDER = [
    "owner", "timezone", "capability", "region", "status_code",
    "service", "service_capability", "classification", "demand_forecast",
]

DOMAINS = [
    ("entity_metadata", "Entity Metadata",
     "Automated ingestion + data-quality pipeline + mastering (UC-1).",
     True, 365),
    ("classification", "Classification",
     "Workflow-governed cataloguing metadata edited by business users (UC-2).",
     True, None),
    ("reference_data", "Reference Data",
     "Periodic bulk reference / forecast loads (UC-3).", False, 1825),
    ("lookups", "Lookups",
     "Static category-to-grouping lookup tables (UC-4).", False, None),
]

# username -> (password, email, global roles, domain_roles) — AC-1 / AC-3.
USERS = {
    "editor_meta":  ("Demo!editor1",  "editor_meta@demo.mdm",  [],
                     {"entity_metadata": ["editor"]}),
    "approver_meta": ("Demo!approve1", "approver_meta@demo.mdm", [],
                      {"entity_metadata": ["approver"]}),
    "steward_class": ("Demo!steward1", "steward_class@demo.mdm", [],
                      {"classification": ["editor", "approver"]}),
    "power_user1":  ("Demo!power1",   "power_user1@demo.mdm",  ["power_user"], {}),
    "reader1":      ("Demo!reader1",  "reader1@demo.mdm",      ["reader"], {}),
}

ROLES_OF = {
    "editor_meta": ["editor"], "approver_meta": ["approver"],
    "steward_class": ["approver"], "power_user1": ["power_user"],
    "admin": ["admin"],
}


# ------------------------------------------------------------------- publishing
def publish_entity(db, entity: Entity) -> List[str]:
    engine = get_ddl_engine()
    with engine.connect() as conn:
        plan = (
            build_alter_plan(conn, entity)
            if entity.is_deployed
            else build_create_plan(entity)
        )
    with engine.begin() as conn:
        apply_plan(conn, plan, allow_destructive=False)
    entity.status = EntityStatus.PUBLISHED.value
    entity.published_version = entity.version
    entity.published_at = datetime.utcnow()
    db.flush()
    warnings = list(plan.warnings)
    all_entities = db.query(Entity).all()
    with engine.begin() as conn:
        warnings += reconcile_reference_constraints(
            conn, entity, all_entities
        ).get("warnings", [])
    with engine.begin() as conn:
        reconcile_enum_constraints(conn, entity)
    with engine.begin() as conn:
        reconcile_matview(conn, entity)
    db.commit()
    return warnings


# -------------------------------------------------------------- pipeline helpers
def submit(
    db, entity: Entity, payload: Dict, *, submitted_by: str,
    source_system: Optional[str] = None, notify: bool = True,
) -> Optional[Dict]:
    """Capture -> promote one record; returns its staging result dict."""
    engine = get_engine()
    with engine.begin() as conn:
        landing = write_to_landing(
            conn, entity, operation=OP_INSERT, payload=payload,
            source_system=source_system, submitted_by=submitted_by,
        )
    with engine.begin() as conn:
        promotion = promote_landing_to_staging(
            db, conn, entity, landing_ids=[landing["landing_id"]],
            actor=submitted_by,
        )
    db.commit()
    staged = next(
        (r for r in promotion["results"]
         if r.get("landing_id") == landing["landing_id"]),
        None,
    )
    if notify and staged and staged.get("staging_id"):
        notifications.safe_enqueue(
            db, event=notifications.EVENT_SUBMITTED, entity=entity,
            staging_id=staged["staging_id"], actor=submitted_by,
            change_type=staged.get("change_type"),
        )
        db.commit()
    return staged


def approve(db, entity: Entity, staging_id: int, *, actor: str,
            note: str = "Approved for demo") -> Dict:
    with get_engine().begin() as conn:
        result = apply_staging_to_live(
            db, conn, entity, staging_id, actor=actor,
            actor_roles=ROLES_OF.get(actor, []), review_note=note,
        )
    db.commit()
    notifications.safe_enqueue(
        db, event=notifications.EVENT_APPROVED, entity=entity,
        staging_id=staging_id, actor=actor, comment=note,
        change_type=result.get("change_type"), record_id=result.get("mdm_id"),
    )
    db.commit()
    return result


def reject(db, entity: Entity, staging_id: int, *, actor: str, reason: str) -> Dict:
    with get_engine().begin() as conn:
        result = reject_staging(
            db, conn, entity, staging_id, actor=actor, reason=reason,
            actor_roles=ROLES_OF.get(actor, []),
        )
    db.commit()
    notifications.safe_enqueue(
        db, event=notifications.EVENT_REJECTED, entity=entity,
        staging_id=staging_id, actor=actor, comment=reason,
    )
    db.commit()
    return result


def request_changes(db, entity: Entity, staging_id: int, *, actor: str,
                    comment: str) -> Dict:
    with get_engine().begin() as conn:
        result = workflow.request_changes(
            db, conn, entity, staging_id, actor=actor, comment=comment,
            actor_roles=ROLES_OF.get(actor, []),
        )
    db.commit()
    notifications.safe_enqueue(
        db, event=notifications.EVENT_CHANGES_REQUESTED, entity=entity,
        staging_id=staging_id, actor=actor, comment=comment,
    )
    db.commit()
    return result


def seed_and_approve(db, entity: Entity, rows: List[Dict], *,
                     submitter: str, approver: str) -> int:
    """Submit every row and approve it straight to golden (SoD-respecting)."""
    approved = 0
    for row in rows:
        staged = submit(db, entity, row, submitted_by=submitter, notify=False)
        if staged and staged.get("is_valid") and staged.get("staging_id"):
            approve(db, entity, staged["staging_id"], actor=approver)
            approved += 1
        else:
            print(f"    ! {entity.name} row not approved (invalid): {row}")
    return approved


# ------------------------------------------------------------------- setup steps
def create_domains(db) -> None:
    for name, display, desc, req_appr, retention in DOMAINS:
        if db.query(Domain).filter(Domain.name == name).first():
            continue
        db.add(Domain(
            name=name, display_name=display, description=desc,
            requires_approval=req_appr, retention_days=retention,
            default_soft_delete=True, created_by="seed", updated_by="seed",
        ))
    db.commit()


def create_users(db) -> None:
    for username, (password, email, roles, domain_roles) in USERS.items():
        if db.query(User).filter(User.username == username).first():
            continue
        db.add(User(
            username=username,
            display_name=username.replace("_", " ").title(),
            email=email, source=UserSource.LOCAL.value,
            password_hash=hash_password(password),
            roles=roles, domain_roles=domain_roles, is_active=True,
            created_by="seed", updated_by="seed",
        ))
    db.commit()


def create_notification_template(db) -> None:
    """Per-domain 'submitted' template for entity_metadata (NT-2)."""
    exists = (
        db.query(NotificationTemplate)
        .filter(NotificationTemplate.domain == "entity_metadata",
                NotificationTemplate.event == notifications.EVENT_SUBMITTED)
        .first()
    )
    if exists:
        return
    db.add(NotificationTemplate(
        domain="entity_metadata",
        event=notifications.EVENT_SUBMITTED,
        subject="[Entity Metadata] Review {entity} #{staging_id} ({change_type})",
        body=(
            "A {change_type} change to '{entity}' was submitted by {actor} and "
            "needs an entity-metadata approver.\n\nReview it: {deep_link}"
        ),
        recipients=[], from_address="entity-metadata@demo.mdm", enabled=True,
        created_by="seed", updated_by="seed",
    ))
    db.commit()


def create_field_mapping(db) -> None:
    """legacy_cmdb source-schema mapping for service (EX-3): svc->service_code,
    svc_name->name."""
    for source_field, target_field in (("svc", "service_code"),
                                        ("svc_name", "name")):
        exists = (
            db.query(FieldMapping)
            .filter(FieldMapping.entity_name == "service",
                    FieldMapping.source_system == "legacy_cmdb",
                    FieldMapping.source_field == source_field)
            .first()
        )
        if exists:
            continue
        db.add(FieldMapping(
            source_system="legacy_cmdb", entity_name="service",
            source_field=source_field, target_field=target_field,
            enabled=True, created_by="seed", updated_by="seed",
        ))
    db.commit()


# ----------------------------------------------------------------- demo dataset
OWNERS = [
    {"owner_code": "OWN-PLAT", "name": "Platform Engineering",
     "email": "platform@demo.mdm", "owner_type": "team"},
    {"owner_code": "OWN-DATA", "name": "Data Services",
     "email": "data@demo.mdm", "owner_type": "team"},
    {"owner_code": "OWN-JSMITH", "name": "Jordan Smith",
     "email": "jordan.smith@demo.mdm", "owner_type": "individual"},
    {"owner_code": "OWN-ACME", "name": "Acme Cloud Vendor",
     "email": "accounts@acme.example", "owner_type": "vendor"},
]

TIMEZONES = [
    {"tz_code": "Australia/Sydney", "display_name": "Sydney", "utc_offset": "+10:00"},
    {"tz_code": "Australia/Perth", "display_name": "Perth", "utc_offset": "+08:00"},
    {"tz_code": "Europe/London", "display_name": "London", "utc_offset": "+00:00"},
    {"tz_code": "America/New_York", "display_name": "New York", "utc_offset": "-05:00"},
    {"tz_code": "Asia/Singapore", "display_name": "Singapore", "utc_offset": "+08:00"},
]

CAPABILITIES = [
    {"capability_code": "CAP-INGEST", "name": "Data Ingestion"},
    {"capability_code": "CAP-REPORT", "name": "Reporting"},
    {"capability_code": "CAP-AUTH", "name": "Authentication"},
    {"capability_code": "CAP-STORAGE", "name": "Storage"},
]

REGIONS = [
    {"region_code": "AU", "name": "Australia", "super_region": "APAC"},
    {"region_code": "SG", "name": "Singapore", "super_region": "APAC"},
    {"region_code": "GB", "name": "United Kingdom", "super_region": "EMEA"},
    {"region_code": "DE", "name": "Germany", "super_region": "EMEA"},
    {"region_code": "US", "name": "United States", "super_region": "AMER"},
]

STATUS_CODES = [
    {"code": "NEW", "label": "New", "grouping": "open"},
    {"code": "IN_PROGRESS", "label": "In Progress", "grouping": "open"},
    {"code": "ON_HOLD", "label": "On Hold", "grouping": "pending"},
    {"code": "DONE", "label": "Completed", "grouping": "closed"},
    {"code": "CANCELLED", "label": "Cancelled", "grouping": "closed"},
]

# service_code -> (payload, disposition)
SERVICES = [
    ({"service_code": "SVC-BILLING", "name": "Billing API", "owner_id": "OWN-PLAT",
      "timezone_code": "Australia/Sydney", "lifecycle": "active",
      "criticality": "high", "monthly_cost": "12500.00",
      "description": "   Handles customer invoicing   ", "is_public": "true"},
     "approve"),
    ({"service_code": "SVC-CATALOG", "name": "Product Catalog", "owner_id": "OWN-DATA",
      "timezone_code": "Asia/Singapore", "lifecycle": "active",
      "criticality": "medium", "monthly_cost": "4200.50",
      "description": "Master product catalog"},
     "approve"),
    ({"service_code": "SVC-AUTH", "name": "Auth Gateway", "owner_id": "OWN-PLAT",
      "timezone_code": "Europe/London", "lifecycle": "planned",
      "criticality": "critical", "monthly_cost": "0",
      "description": "SSO / auth gateway"},
     "approve"),
    ({"service_code": "SVC-BACKUP", "name": "Backup Service", "owner_id": "OWN-ACME",
      "timezone_code": "Australia/Perth", "lifecycle": "active",
      "criticality": "medium", "monthly_cost": "1500",
      "description": "Nightly backups"},
     "pending"),
    ({"service_code": "SVC-REPORTS", "name": "Reporting Engine",
      "owner_id": "OWN-JSMITH", "timezone_code": "America/New_York",
      "lifecycle": "deprecated", "criticality": "low", "monthly_cost": "800",
      "description": "Legacy reporting engine"},
     "request_changes"),
    ({"service_code": "SVC-OLDCRM", "name": "Old CRM Sync", "owner_id": "OWN-DATA",
      "timezone_code": "Australia/Sydney", "lifecycle": "retired",
      "criticality": "low", "monthly_cost": "50", "description": "Deprecated sync"},
     "reject"),
    # Deliberately invalid rows — held in staging (never rejected at the edge):
    ({"service_code": "SVC-ORPHAN", "name": "Orphaned Service",
      "owner_id": "OWN-DOESNOTEXIST", "timezone_code": "Australia/Sydney",
      "lifecycle": "active", "criticality": "low", "monthly_cost": "10",
      "description": "Owner does not exist yet -> broken_reference (DQ-5)"},
     "broken_reference"),
    ({"service_code": "SVC-BADENUM", "name": "Bad Enum Service",
      "owner_id": "OWN-PLAT", "timezone_code": "Australia/Sydney",
      "lifecycle": "zombie", "criticality": "high", "monthly_cost": "99",
      "description": "lifecycle 'zombie' is not a valid enum -> soft validation"},
     "invalid_enum"),
]

# (service_code, capability_code) junction edges — all linked to approved services.
SERVICE_CAPABILITIES = [
    ("SVC-BILLING", "CAP-INGEST"),
    ("SVC-BILLING", "CAP-STORAGE"),
    ("SVC-CATALOG", "CAP-REPORT"),
    ("SVC-AUTH", "CAP-AUTH"),
]

# class_code -> (payload, disposition)
CLASSIFICATIONS = [
    ({"class_code": "CLS-PCI", "name": "PCI Cardholder Scope",
      "sensitivity": "restricted", "service_id": "SVC-BILLING",
      "description": "Handles cardholder data"}, "approve"),
    ({"class_code": "CLS-PUBLIC", "name": "Public Catalog Data",
      "sensitivity": "public", "service_id": "SVC-CATALOG",
      "description": "Publicly listable catalog"}, "pending"),
    ({"class_code": "CLS-INTERNAL", "name": "Internal Auth Data",
      "sensitivity": "internal", "service_id": "SVC-AUTH",
      "description": "Internal auth metadata"}, "request_changes"),
]

# demand_forecast bulk batch: (region_code, period, forecast_value, scenario).
# The business key is region_code + period (per the model), so each row is a
# distinct region/period; the scenario varies to exercise its enum.
FORECASTS = [
    ("AU", 2025, "1000.00", "base"),
    ("AU", 2026, "1100.00", "base"),
    ("AU", 2027, "1250.00", "high"),
    ("SG", 2026, "700.00", "base"),
    ("US", 2025, "5000.00", "base"),
    ("US", 2026, "5200.00", "base"),
    ("US", 2027, "6100.00", "low"),
]


def seed_services(db, entities: Dict[str, Entity]) -> None:
    svc = entities["service"]
    for payload, disposition in SERVICES:
        staged = submit(db, svc, payload, submitted_by="editor_meta")
        if not (staged and staged.get("staging_id")):
            print(f"    ! service {payload['service_code']} failed to stage")
            continue
        sid = staged["staging_id"]
        if disposition == "approve":
            approve(db, svc, sid, actor="approver_meta",
                    note="Reviewed and approved for the golden catalog.")
        elif disposition == "reject":
            reject(db, svc, sid, actor="approver_meta",
                   reason="Retired system — not mastered.")
        elif disposition == "request_changes":
            request_changes(db, svc, sid, actor="approver_meta",
                            comment="Confirm the owner and target lifecycle "
                                    "before this can be approved.")
        # 'pending', 'broken_reference', 'invalid_enum' -> left in staging as-is.


def seed_service_capabilities(db, entities: Dict[str, Entity]) -> None:
    sc = entities["service_capability"]
    for service_code, capability_code in SERVICE_CAPABILITIES:
        staged = submit(db, sc, {"service_id": service_code,
                                 "capability_id": capability_code},
                        submitted_by="editor_meta", notify=False)
        if staged and staged.get("is_valid") and staged.get("staging_id"):
            approve(db, sc, staged["staging_id"], actor="approver_meta")
        else:
            print(f"    ! junction {service_code}->{capability_code} not approved: "
                  f"{staged}")


def seed_classifications(db, entities: Dict[str, Entity]) -> None:
    cls = entities["classification"]
    for payload, disposition in CLASSIFICATIONS:
        staged = submit(db, cls, payload, submitted_by="editor_meta")
        if not (staged and staged.get("staging_id")):
            continue
        sid = staged["staging_id"]
        if disposition == "approve":
            approve(db, cls, sid, actor="steward_class",
                    note="Classification confirmed.")
        elif disposition == "request_changes":
            request_changes(db, cls, sid, actor="steward_class",
                            comment="Please cite the governing policy reference.")
        # 'pending' -> left for the classification inbox.


def seed_forecasts(db, entities: Dict[str, Entity]) -> None:
    fc = entities["demand_forecast"]
    for region_code, period, value, scenario in FORECASTS:
        staged = submit(db, fc, {"region_code": region_code, "period": period,
                                 "forecast_value": value, "scenario": scenario},
                        submitted_by="power_user1", source_system="forecast_batch",
                        notify=False)
        if staged and staged.get("is_valid") and staged.get("staging_id"):
            approve(db, fc, staged["staging_id"], actor="admin",
                    note="Bulk forecast load approved.")
        else:
            print(f"    ! forecast {region_code}/{period} not approved: {staged}")


def seed_legacy_mapped_record(db, entities: Dict[str, Entity]) -> None:
    """Push one legacy_cmdb-shaped record through the field mapping (EX-3)."""
    svc = entities["service"]
    payload = {
        "svc": "SVC-LEGACY", "svc_name": "Legacy Billing Bridge",
        "owner_id": "OWN-PLAT", "timezone_code": "Australia/Sydney",
        "lifecycle": "active", "criticality": "low", "monthly_cost": "300",
        "description": "Ingested from the legacy CMDB via field mapping",
    }
    staged = submit(db, svc, payload, submitted_by="power_user1",
                    source_system="legacy_cmdb", notify=False)
    if staged and staged.get("is_valid") and staged.get("staging_id"):
        approve(db, svc, staged["staging_id"], actor="approver_meta",
                note="Legacy-mapped service approved.")
    else:
        print(f"    ! legacy-mapped record not approved: {staged}")


# ----------------------------------------------------------------- summary
def summarize(db, entity_names: List[str]) -> None:
    engine = get_engine()
    print("\n================= SEED SUMMARY =================")
    print("Golden records per entity (live tier, active):")
    total_golden = 0
    with engine.connect() as conn:
        for name in entity_names:
            live = qualified("mdm", name)
            n = conn.execute(
                text(f"select count(*) from {live} where not mdm_is_deleted")
            ).scalar()
            total_golden += n
            print(f"  {name:22} {n}")
        print(f"  {'TOTAL':22} {total_golden}")

        print("\nStaging rows held invalid (pending review) — DQ soft validation:")
        for name in entity_names:
            st = qualified("mdm_staging", name)
            n = conn.execute(
                text(f"select count(*) from {st} where not mdm_is_valid "
                     "and mdm_status in ('pending_review','changes_requested')")
            ).scalar()
            if n:
                broken = conn.execute(
                    text(f"select count(*) from {st} where not mdm_is_valid "
                         "and mdm_errors @> '[{\"code\": \"broken_reference\"}]'::jsonb")
                ).scalar()
                print(f"  {name:22} {n} invalid ({broken} broken_reference)")

    # Workflow tasks (inbox) by status.
    from app.models import WorkflowTask
    counts: Dict[str, int] = {}
    for t in db.query(WorkflowTask).all():
        counts[t.status] = counts.get(t.status, 0) + 1
    pending = counts.get("pending_review", 0) + counts.get("changes_requested", 0)
    print("\nWorkflow tasks by status:")
    for status, n in sorted(counts.items()):
        print(f"  {status:22} {n}")
    print(f"  {'PENDING (inbox)':22} {pending}")

    # Notifications outbox.
    from app.models import Notification
    notif_counts: Dict[str, int] = {}
    for n in db.query(Notification).all():
        notif_counts[n.status] = notif_counts.get(n.status, 0) + 1
    print("\nNotifications outbox by status:")
    for status, n in sorted(notif_counts.items()):
        print(f"  {status:22} {n}")

    print("\nDemo users (local, known passwords):")
    for username, (password, email, roles, domain_roles) in USERS.items():
        scope = roles or domain_roles or "-"
        print(f"  {username:14} / {password:14} roles/domain_roles={scope}")
    print("  admin          / (from .env LOCAL_ADMIN_PASSWORD)  roles=['admin']")
    print("===============================================")


# ----------------------------------------------------------------- orchestration
def main() -> int:
    print(">>> Resetting to a clean slate...")
    reset_demo.main()

    print("\n>>> Importing model definitions from examples/models.yaml ...")
    defs, errors = validate_document(parse_document(EXAMPLES.read_text(encoding="utf-8")))
    blocking = [e for e in errors if "warning" not in e.lower()]
    if blocking:
        raise SystemExit("Model import failed:\n  " + "\n  ".join(blocking))

    with session_scope() as db:
        apply_import(
            db, [dict(d, attributes=list(d["attributes"])) for d in defs],
            actor="seed",
        )
        db.commit()

        entities = {e.name: e for e in db.query(Entity).all()}
        print(">>> Publishing entities (DDL across four tiers + matviews)...")
        for name in PUBLISH_ORDER:
            entity = entities[name]
            warnings = publish_entity(db, entity)
            note = f" ({len(warnings)} warning(s))" if warnings else ""
            print(f"    published {name}{note}")
        # Refresh handles from the session after publish commits.
        entities = {e.name: e for e in db.query(Entity).all()}

        print(">>> Creating domains, users, notification template, field mapping...")
        create_domains(db)
        create_users(db)
        create_notification_template(db)
        create_field_mapping(db)

        print(">>> Seeding reference / lookup / parent data to golden...")
        n = seed_and_approve(db, entities["owner"], OWNERS,
                             submitter="editor_meta", approver="approver_meta")
        print(f"    owners approved:       {n}")
        n = seed_and_approve(db, entities["timezone"], TIMEZONES,
                             submitter="editor_meta", approver="approver_meta")
        print(f"    timezones approved:    {n}")
        n = seed_and_approve(db, entities["capability"], CAPABILITIES,
                             submitter="editor_meta", approver="approver_meta")
        print(f"    capabilities approved: {n}")
        n = seed_and_approve(db, entities["region"], REGIONS,
                             submitter="power_user1", approver="admin")
        print(f"    regions approved:      {n}")
        n = seed_and_approve(db, entities["status_code"], STATUS_CODES,
                             submitter="power_user1", approver="admin")
        print(f"    status codes approved: {n}")

        print(">>> Seeding services (UC-1) with mixed dispositions...")
        seed_services(db, entities)
        print(">>> Seeding service_capability junctions (DM-3)...")
        seed_service_capabilities(db, entities)
        print(">>> Seeding classifications (UC-2) into the workflow...")
        seed_classifications(db, entities)
        print(">>> Bulk-loading demand_forecast (UC-3)...")
        seed_forecasts(db, entities)
        print(">>> Pushing one legacy_cmdb record through the field mapping (EX-3)...")
        seed_legacy_mapped_record(db, entities)

        entity_names = [e.name for e in db.query(Entity).order_by(Entity.name).all()]
        summarize(db, entity_names)

    print("\n>>> Refreshing distribution matviews (DD-1/DD-2)...")
    refreshed = scheduler_svc.refresh_views()
    print(f"    refreshed {refreshed.get('count')} matview(s).")

    print("\nDone. Demo dataset is ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
