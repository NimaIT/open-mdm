# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-hostable, metadata-driven Master Data Management platform: FastAPI + SQLAlchemy on PostgreSQL, with a no-build React frontend (vendored React + htm, no JSX, no Node toolchain). Data models are defined as metadata; publishing a model generates and applies the physical DDL at runtime.

## Commands

```bash
# Setup (local, against your own PostgreSQL — copy .env.example to .env first)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m scripts.bootstrap      # idempotent: creates schemas + metadata tables

# Run the dev server (background, port 8099, logs to /tmp/uvicorn.log)
./scripts/devserver.sh restart|stop|status
# or foreground:
.venv/bin/uvicorn app.main:app --port 8000

# Tests — require a reachable PostgreSQL (PGHOST/PGPORT/PGUSER/PGPASSWORD or .env);
# every DB test auto-skips if none is reachable, which silently guts the suite.
.venv/bin/pytest                 # full suite
.venv/bin/pytest -k ddl          # one area
.venv/bin/pytest tests/test_pipeline.py::test_name   # single test

# Docker
docker compose up --build

# Screenshot a UI route (needs devserver running + a JWT)
./scripts/shoot.sh /admin/models out.png "$JWT"
```

Tests run against a **real PostgreSQL database by design** — the product is DDL generation and transactional promotion, so nothing is mocked. `entity_factory` in `tests/conftest.py` creates uniquely-named entities, publishes their DDL, and drops them in teardown. Fixtures must **commit** (not just flush) because HTTP tests drive the app through separate connections.

There is no linter configured. Alembic is scaffolded but has no versions; schema bootstrap is `metadata.create_all()` via `scripts/bootstrap.py`.

## Architecture

### The four-tier pipeline (the core invariant)

Every write flows one direction: `mdm_landing` (raw jsonb, **never rejects**) → `mdm_staging` (typed, nullable — **invalid rows stored deliberately** with errors attached) → human steward approval → `mdm` (golden records, full constraints) → `mdm_history` (every prior version). **The write API never touches the live tier** — only an approved promotion in `app/services/pipeline.py` writes golden records, transactionally, always with a history row. All write endpoints return `202 Accepted`. Don't "fix" code to write live directly or reject bad input at the edge; both violate the design.

### Metadata-driven DDL: Core vs ORM split

Master-data tables don't exist at startup — they're generated from metadata rows (`mdm_meta.entity` / `mdm_meta.attribute`, ORM models in `app/models/meta.py`). Because their shape is unknown at import time, **all generated-table access uses SQLAlchemy Core `text()` with bind parameters**; only the fixed internal `mdm_meta.*` tables use the ORM. Keep that split.

Injection defence for the dynamic SQL is layered in `app/services/identifiers.py`: `validate_ident()` (allow-list regex `^[a-z][a-z0-9_]{0,62}$` + reserved words), `validate_column_name()` (rejects the `mdm_` prefix — reserved for platform bookkeeping columns), `quote_ident()`, and bind parameters for all values. Every dynamic identifier must go through these.

### DDL migration safety

`build_alter_plan()` in `app/services/ddl.py` diffs metadata against `information_schema`. Additive changes and widenings (per `is_safe_widening()` in `identifiers.py`, family + rank based) apply automatically; drops/narrowings are refused and require `confirm_destructive: true` plus `ALLOW_DESTRUCTIVE_DDL`. Landing tables are excluded from diffing (payload is jsonb).

### Two PostgreSQL roles, three engines

`app/db.py`: `get_engine()` — runtime DML-only credentials (`PGUSER`), used everywhere; `get_ddl_engine()` — elevated (`PG_DDL_USER`), used **only** by bootstrap and publish; `get_maintenance_engine()` — AUTOCOMMIT against the `postgres` DB for `CREATE DATABASE`. Never use the DDL engine for runtime queries.

### Authorization invariants (enforced in code, tested in `test_authz.py`)

- Roles: `admin`, `steward`, `reader`, `service` (API key). **Service keys write to landing only and can never approve.**
- Segregation of duties (`ENFORCE_SEGREGATION_OF_DUTIES`, default on): the approver must differ from `mdm_submitted_by` and `mdm_edited_by`.
- Invalid staging rows (`mdm_is_valid = false`) cannot be approved.
- LDAP auth (`app/services/ldap_auth.py`) re-binds as the user; usernames are RFC 4515-escaped. A local break-glass admin remains available.

### PATCH semantics: `mdm_supplied_fields`

Staging rows record which business fields the caller actually sent. Apply-to-live updates **only those fields**, so a PATCH mentioning one field can't null out steward-curated values. Steward edits merge into this set. Any change to the apply path must preserve this.

### Record resolution (`_resolve_target()` in `pipeline.py`)

Order: explicit `mdm_id` → business key (`is_business_key` attrs, exact match) → match key (`is_match_key` attrs, normalized case-insensitive). Business-key hits on INSERT flag `duplicate`; match-key hits flag `probable_duplicate` — both surface to steward review rather than erroring. This is the extension point for fuzzy matching (deliberately not implemented).

### Layout

- `app/api/v1/` — routers: `auth`, `models_api` (model CRUD/import/publish), `data` (write + read API), `stewardship` (queue/review/approve), `admin`
- `app/services/` — all business logic: `pipeline` (tier promotion), `ddl`, `identifiers`, `validation` (type coercion, normalization rules), `model_io` (YAML/JSON import-export), `auth`, `ldap_auth`, `bootstrap`
- `app/ui.py` + `app/static/` — SPA served without a build step; UI code is `app/static/app.js` using `htm` tagged templates. The UI adapts to role (stewards never see DDL controls).
- `docs/ARCHITECTURE.md` — full design rationale and tier column reference. NOTE: its "deliberately left out" list is now largely superseded — see below.

## Capabilities added since the original build (see `DEVLOG.md` for the full record)

A 9-workstream effort (W1–W9) closed the gap against `mdm_platform_functional_requirements.md`. Several former omissions are now implemented — do not treat ARCHITECTURE.md §10 as current:

- **Cross-entity references / FK / M2M** (`app/services/references.py`, `ddl.py`): `reference` attribute type stores the parent `mdm_id`, resolved from a human value at promotion; FK constraints on the LIVE tier only; `broken_reference` holds a child in staging until the parent exists (re-resolved automatically); `kind='association'` entities are M2M junctions with a composite unique index. Enum `validation.enum` → live-tier `CHECK`.
- **First-class domains + expanded RBAC** (`mdm_meta.domain`, `auth.py`, `deps.py`): roles `editor` / `approver` / `power_user` added alongside admin/steward/reader/service. `User.domain_roles` CONFERS a role's permissions within one domain (AC-3). Entity-scoped routes use `require_entity_permission(perm, action)`. Power-user `?direct=true` auto-approves through the pipeline (never a hand-rolled live write). `/auth/me` (`principal_context`) aggregates global ∪ domain_roles for UI hints; routes remain authoritative per-entity.
- **Maker-checker workflow** (`app/services/workflow.py`, `mdm_meta.workflow_task` / `workflow_event`): change requests, mandatory review comments, `request_changes`, per-user inbox + claim, reassign/terminate, immutable decision chain. `audit_event` + `workflow_event` are DB append-only (BEFORE UPDATE/DELETE trigger). Governance state commits on the SAME `conn` transaction as the tier write; staging rows are `FOR UPDATE`-locked in decisions.
- **Notifications** (`app/services/notifications.py`): pluggable outbox (default, no network) / SMTP transport, per-domain templates, enqueued post-commit via a swallow-all guard (never breaks a transition).
- **Distribution + scheduler** (`app/services/scheduler.py`): `mdm_pub.<entity>` materialized views; a stdlib in-process scheduler (opt-in `SCHEDULER_ENABLED`) refreshes views + runs retention (prunes landing/history only, never golden/append-only).
- **Extensibility** (`hooks.py` / `transforms.py` / `mappings.py`): pre_stage / pre_commit / post_commit hooks (savepoint-isolated on conn+db; post_commit runs after commit), a transform registry (`Attribute.transforms`), and source→target field mappings applied at promotion (landing keeps the raw payload).
- **Observability** (`logging_config.py`): structured JSON logging with `platform` / `integration` / `custom` streams + request-id correlation (`LOG_JSON`).

Demo: `scripts/reset_demo.py` + `scripts/seed_demo.py` build a 4-domain / 9-entity dataset (`examples/models.yaml`) aligned to the four use cases. Run on http://127.0.0.1:8000 (admin / `LOCAL_ADMIN_PASSWORD`).
