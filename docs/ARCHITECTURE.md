# Architecture

This document describes the design of the MDM Platform: why each layer exists,
what trade-offs were made, and how the components fit together. It is a complement
to the README, not a replacement for it.

---

## 1. The problem and why four tiers exist

Most integration patterns pick one of two bad options: validate at the edge and
reject bad data, or accept everything into one table and hope someone cleans it up.
The first loses inbound messages you cannot get back. The second corrupts the
golden record.

This platform takes a third position: **capture first, validate in the middle,
gate at the golden record**. The constraint that makes this work is that every
tier in the pipeline has explicitly different rules:

- Landing must never reject. Losing an inbound message is worse than storing a
  bad one. The payload is stored as raw `jsonb` regardless of shape.
- Staging deliberately stores invalid rows. A steward cannot fix data they cannot
  see. Invalid rows are written with their structured errors attached so they are
  visible and repairable.
- Live carries the real constraints — `NOT NULL`, unique indexes, typed columns.
  It is written only by an approved promotion, never by the write API directly.
- History records every prior version of a golden record, effective-dated, so
  the full lineage of any value can be reconstructed.

These rules are encoded as design invariants in `app/services/pipeline.py`:

```
# Design rules that hold throughout:
# * The write API never touches the live tier. Ever.
# * Landing never rejects a payload — capture first, validate later.
# * Staging holds invalid rows deliberately, so a human can repair them.
# * Applying to live is transactional and always writes a history row.
```

---

## 2. The four tiers

| Tier    | Schema (default)  | Constraints                                      | Purpose                                                                                  |
|---------|-------------------|--------------------------------------------------|------------------------------------------------------------------------------------------|
| Landing | `mdm_landing`     | None — payload stored as `jsonb`                 | Append-only inbound audit trail. Captures exactly what was sent, with source system, batch id, and receipt time. Never rejects. |
| Staging | `mdm_staging`     | Typed columns, but NOT enforced as NOT NULL      | Validated and coerced from landing. Invalid rows retained with errors so stewards can see and fix them. Awaits human review. |
| Live    | `mdm`             | Full constraints: NOT NULL, unique indexes, PK   | The golden records. Written only by an approved steward decision. Versioned. |
| History | `mdm_history`     | None — mirrors live structure without constraints | Every prior version of every golden record, effective-dated via `mdm_valid_from` / `mdm_valid_to`. |

Data flows in one direction only:

```
  API write (POST / PUT / PATCH / DELETE)
                   |
                   v
         +-------------------+
         |   mdm_landing     |   raw jsonb, never rejected
         +-------------------+
                   |
      validate, coerce, normalise, match
                   |
                   v
         +-------------------+
         |   mdm_staging     |   typed; invalid rows stored with errors
         +-------------------+
                   |
     human data steward: review, edit, approve
                   |
                   v
         +-------------------+        +-------------------+
         |      mdm          |------->|   mdm_history     |
         |  golden records   | prior  |  every version    |
         +-------------------+ version+-------------------+
```

---

## 3. The metadata-driven model

### Entity and attribute tables

The physical master-data tables do not exist at application startup. Instead, the
platform stores model definitions in two fixed internal tables:

- `mdm_meta.entity` — one row per logical entity (Customer, Product, ...).
  Key columns include `name`, `display_name`, `domain`, `status`, `version`,
  `requires_approval`, `soft_delete`, and `retention_days`.
- `mdm_meta.attribute` — one row per field of an entity. Stores the logical
  `data_type`, `length`, `numeric_precision`, `numeric_scale`, `is_required`,
  `is_unique`, `is_business_key`, `is_match_key`, `is_indexed`, `is_pii`,
  `default_value`, `validation` (jsonb), and `normalization` (jsonb list of rules).

When an entity is published, `app/services/ddl.py` reads those rows and generates
four `CREATE TABLE` statements — one per tier — and executes them against the
database. The DDL is deterministic: running `build_create_plan()` twice with the
same metadata produces the same SQL.

### Why SQLAlchemy Core for master-data tables, ORM for internal tables

The SQLAlchemy ORM requires model classes to be declared at import time, because
the mapper maps Python class attributes to fixed column names. Master-data tables
are created at runtime from user configuration: column names, types, and
constraints are not known when the application starts. Declaring ORM classes for
them at import time is not possible.

SQLAlchemy Core, by contrast, works with raw SQL strings and `text()` constructs
at runtime. All DDL generation, promotion queries, and golden-record writes
therefore use Core. The query in `write_to_landing`, for example, is:

```python
conn.execute(
    text(f"INSERT INTO {t} (mdm_operation, mdm_payload, ...) VALUES ..."),
    {...bind params...},
)
```

The fixed internal tables (`mdm_meta.entity`, `mdm_meta.attribute`,
`mdm_meta.app_user`, `mdm_meta.audit_event`, and others in `app/models/meta.py`)
are the exception. Their structure is known at startup, so they use the ORM via
`declarative_base()`. SQLAlchemy's `metadata.create_all()` bootstraps them.

---

## 4. SQL injection defence

Every physical object name in this platform originates from user-supplied
metadata: entity names become table names, attribute names become column names.
This is the largest injection risk in the system. The defence is layered:

1. **Allow-list regex.** `validate_ident()` in `app/services/identifiers.py`
   checks every identifier against `IDENT_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")`.
   Anything that does not match — upper-case letters, hyphens, Unicode, SQL
   metacharacters — is rejected with `IdentifierError` before it reaches the
   database.

2. **Reserved-word check.** The same function checks the lowercased name against
   a curated set of PostgreSQL reserved words (`RESERVED` in `identifiers.py`).
   An entity named `table` or `select` is rejected.

3. **Reserved prefix check.** `validate_column_name()` additionally rejects any
   attribute name that starts with `mdm_` or that appears in `SYSTEM_COLUMNS`.
   This prevents user-defined attributes from shadowing the platform's own
   bookkeeping columns.

4. **Quoting.** Validated identifiers are passed through `quote_ident()`, which
   wraps them in double quotes and escapes any embedded double quotes by doubling
   them (`"` -> `""`). Even a valid identifier like `user` is emitted as `"user"`.

5. **Bind parameters for values.** No data value is ever interpolated into SQL.
   Every write uses `text()` with named bind parameters. The staging insert in
   `promote_landing_to_staging` is representative:

```python
conn.execute(
    text(f"INSERT INTO {staging_t} (...) VALUES (:lid, :op, :tid, ...)"),
    {"lid": landing_id, "op": operation, "tid": resolved_id, ...},
)
```

The pipeline between validation and the database thus has three layers:
identifier allow-list + reserved-word block + quoting on the left; bind
parameters on the right.

---

## 5. Migration safety and `is_safe_widening`

When a published entity is modified and re-published, `build_alter_plan()` in
`ddl.py` diffs the metadata model against the deployed tables by introspecting
`information_schema.columns`. It classifies every change as either safe or
destructive.

**Safe (applied automatically):**
- Adding a new column.
- A type widening: `integer` -> `bigint`, `varchar(100)` -> `varchar(255)`,
  `varchar(n)` -> `text`.

**Destructive (refused, reported with SQL, require `confirm_destructive: true`):**
- Dropping a column (no longer in the model).
- Narrowing a type: `varchar(320)` -> `varchar(100)`, `bigint` -> `integer`.
- Any cross-family change: `integer` -> `text`.

The safety decision is made by `is_safe_widening()` in `identifiers.py`. It
classifies types into families (`text`, `num`, `bool`, `time`, `uuid`, `json`)
and within each family assigns a rank order from narrower to wider. A change is
safe when it stays in the same family and moves to an equal or higher rank, with
special handling for the `text` family where `varchar(n) -> text` (rank rises)
is always safe, but `varchar(50) -> varchar(20)` (same rank, shorter length) is
not.

Landing tables are excluded from migration diffing entirely — the payload column
is `jsonb` and the business columns do not exist there; there is nothing to
migrate when you add or widen a business attribute.

An added `NOT NULL` column with no default value on a live table that already has
rows is promoted as nullable and a warning is emitted instructing the operator to
backfill before tightening the constraint.

---

## 6. System columns on each tier

Every generated table carries platform-owned bookkeeping columns prefixed `mdm_`.
User-defined attributes cannot use this prefix — `validate_column_name()` enforces
it.

### Landing (`mdm_landing.<entity>`)

| Column                | Type           | Purpose                                                          |
|-----------------------|----------------|------------------------------------------------------------------|
| `mdm_landing_id`      | bigserial PK   | Monotonic row identity, used throughout for lineage.             |
| `mdm_operation`       | varchar(10)    | `INSERT`, `UPDATE`, `DELETE`, or `UPSERT`.                       |
| `mdm_payload`         | jsonb NOT NULL | Exactly what the caller sent, unmodified.                        |
| `mdm_target_id`       | varchar(64)    | Explicit `mdm_id` if the caller supplied one.                    |
| `mdm_source_system`   | varchar(100)   | Caller-declared provenance tag.                                  |
| `mdm_batch_id`        | uuid           | Groups rows from one bulk submission.                            |
| `mdm_idempotency_key` | varchar(255)   | Unique index (partial, where not null). Replay deduplication.    |
| `mdm_submitted_by`    | varchar(255)   | Authenticated user or API key name.                              |
| `mdm_received_at`     | timestamptz    | Server-assigned receipt time.                                    |
| `mdm_status`          | varchar(20)    | `pending`, `promoted`, `rejected`, `error`.                      |
| `mdm_errors`          | jsonb          | Populated if promotion itself fails (row-level exception).       |
| `mdm_processed_at`    | timestamptz    | When the row was promoted to staging or errored.                 |

### Staging (`mdm_staging.<entity>`)

| Column                | Type           | Purpose                                                          |
|-----------------------|----------------|------------------------------------------------------------------|
| `mdm_staging_id`      | bigserial PK   | Row identity. Referenced by live and audit.                      |
| `mdm_landing_id`      | bigint         | Back-reference to the originating landing row.                   |
| `mdm_operation`       | varchar(10)    | The requested operation.                                         |
| `mdm_target_id`       | varchar(64)    | Resolved `mdm_id` of the golden record to update, if any.       |
| `mdm_match_key`       | text           | Composite normalised match key (see section 7).                  |
| `mdm_source_system`   | varchar(100)   | Propagated from landing.                                         |
| `mdm_batch_id`        | uuid           | Propagated from landing.                                         |
| `mdm_status`          | varchar(20)    | `pending_review`, `changes_requested`, `approved`, `rejected`, `applied`, `error`. |
| `mdm_errors`          | jsonb          | Structured validation errors, empty array when valid.            |
| `mdm_is_valid`        | boolean        | `true` only when `mdm_errors` is empty. Approval check gates on this. |
| `mdm_change_type`     | varchar(20)    | `insert`, `update`, `delete`.                                    |
| `mdm_submitted_by`    | varchar(255)   | Original submitter (used for segregation-of-duties check).       |
| `mdm_submitted_at`    | timestamptz    | When the staging row was created.                                |
| `mdm_edited_by`       | varchar(255)   | Last steward to edit the row (also checked for SoD).             |
| `mdm_edited_at`       | timestamptz    | When a steward last edited the row.                              |
| `mdm_reviewed_by`     | varchar(255)   | Who approved or rejected.                                        |
| `mdm_reviewed_at`     | timestamptz    | When.                                                            |
| `mdm_review_note`     | text           | Free-text note attached on approval or rejection.                |
| `mdm_supplied_fields` | jsonb          | **See note below.**                                              |
| *(business columns)*  | *(per entity)* | Typed but not NOT NULL — invalid rows must be storable.          |

**`mdm_supplied_fields` specifically.** This column records the list of business
field names that the original caller actually sent in the payload. It solves a
subtle but important problem for PATCH semantics.

Consider: a steward curates `credit_limit` to 75000 on a golden record. Later, a
source system sends a PATCH that only mentions `legal_name`. The staging row for
that PATCH will have `credit_limit = NULL` — because the caller did not supply
it and the column defaults to `NULL`. Without `mdm_supplied_fields`, when that
staging row is applied to the live table, the code would have no way to
distinguish "caller deliberately set credit_limit to NULL" from "caller never
mentioned credit_limit". It would either silently overwrite the curated 75000
with NULL, or would have to apply every non-null staging column — which would
still let DDL defaults leak into the golden record.

`mdm_supplied_fields` resolves this: when applying a staging row to live, the
apply step reads this column and updates only those fields:

```python
supplied = row["mdm_supplied_fields"] or []
if supplied_fields:
    supplied = {k: values[k] for k in supplied_fields if k in values}
```

When a steward edits a staging row, their edits are merged into
`mdm_supplied_fields` so that the repair is not lost at apply time:

```python
supplied = sorted(set(previously) | set(updates))
```

### Live (`mdm.<entity>`)

| Column              | Type         | Purpose                                                            |
|---------------------|--------------|--------------------------------------------------------------------|
| `mdm_id`            | uuid PK      | Stable golden-record identifier. `DEFAULT gen_random_uuid()`.      |
| `mdm_version`       | integer      | Monotonically incremented on every approved change.                |
| `mdm_source_system` | varchar(100) | Source system of the most recent approved change.                  |
| `mdm_is_deleted`    | boolean      | Soft-delete tombstone; partial unique indexes exclude deleted rows. |
| `mdm_deleted_at`    | timestamptz  | When the soft-delete was applied.                                  |
| `mdm_created_at`    | timestamptz  | Row creation time.                                                 |
| `mdm_created_by`    | varchar(255) | Actor who approved the insert.                                     |
| `mdm_updated_at`    | timestamptz  | Time of most recent approved change.                               |
| `mdm_updated_by`    | varchar(255) | Actor who approved the most recent change.                         |
| `mdm_approved_by`   | varchar(255) | Explicit approval actor (may differ from `mdm_updated_by`).        |
| `mdm_staging_id`    | bigint       | Back-reference to the staging row that produced this version.      |
| *(business columns)*| *(per entity)*| Full NOT NULL and unique constraints enforced.                    |

Unique indexes on the live tier are partial: `WHERE mdm_is_deleted = false`. This
allows a soft-deleted record's unique business-key value to be reused by a new
record.

### History (`mdm_history.<entity>`)

| Column              | Type         | Purpose                                                            |
|---------------------|--------------|--------------------------------------------------------------------|
| `mdm_history_id`    | bigserial PK | Append-only row identity.                                          |
| `mdm_id`            | uuid NOT NULL| The golden record this is a snapshot of.                           |
| `mdm_version`       | integer      | The version of the golden record before this change.               |
| `mdm_change_type`   | varchar(20)  | `update`, `delete`.                                                |
| `mdm_valid_from`    | timestamptz  | When this version became active (`mdm_updated_at` / `mdm_created_at` of the live row at snapshot time). |
| `mdm_valid_to`      | timestamptz  | When this version was superseded (`DEFAULT now()`).                |
| `mdm_changed_by`    | varchar(255) | Actor who approved the change.                                     |
| `mdm_source_system` | varchar(100) | Source system at the time of the change.                           |
| `mdm_is_deleted`    | boolean      | Whether the record was soft-deleted at this version.               |
| `mdm_staging_id`    | bigint       | Staging row that triggered this history entry.                     |
| *(business columns)*| *(per entity)*| No NOT NULL constraints — stores what was true, including NULLs. |

---

## 7. Record resolution order

When a write arrives at staging, the pipeline must decide whether it is an insert
or an update and — if an update — which golden record it targets. `_resolve_target()`
in `pipeline.py` implements this with three levels of specificity:

1. **Explicit `mdm_id`.** If the caller supplied `mdm_target_id` (from a PATCH or
   DELETE by UUID), a direct lookup is performed. If the record does not exist, an
   error is recorded and the change is treated as an insert.

2. **Business key.** If the entity defines one or more `is_business_key` attributes
   and all of them are present and non-null in the payload, those columns are used
   for an exact lookup against the live table (excluding soft-deleted rows). On a
   match, the change type is `update`. An INSERT operation that matches an existing
   business key is flagged as a `duplicate` error in staging to ensure a steward
   reviews it.

3. **Deterministic match key.** If no business key resolves, but the entity has
   `is_match_key` attributes and all of them are present, a case-insensitive trimmed
   comparison is performed: `lower(trim(column::text)) = :mk_column`. This is the
   duplicate-detection step. A match on INSERT is flagged `probable_duplicate` so a
   steward reviews before approval.

If none of these resolves, the change type is `insert` and no target is set.

---

## 8. Security model

### Two PostgreSQL roles

The database layer enforces least privilege via two separate connection credentials:

| Credential               | Config vars                          | Privilege level                                        |
|--------------------------|--------------------------------------|--------------------------------------------------------|
| Runtime (DML) connection | `PGUSER` / `PGPASSWORD`             | `SELECT, INSERT, UPDATE, DELETE` on MDM schemas only. No DDL. |
| DDL connection           | `PG_DDL_USER` / `PG_DDL_PASSWORD`   | `CREATE` on database + schemas. Used only when publishing. |

The bootstrap grants the runtime role DML on all MDM schemas and sets
`ALTER DEFAULT PRIVILEGES` so future generated tables are reachable without
re-granting. `get_engine()` in `db.py` always uses the runtime credentials.
`get_ddl_engine()` uses the elevated credentials, and is called only from
`bootstrap.py` and the publish endpoint.

A third `get_maintenance_engine()` connects to the `postgres` maintenance database
(configurable via `PG_MAINTENANCE_DATABASE`) with `AUTOCOMMIT` isolation, which is
required for `CREATE DATABASE`.

### Four application roles

| Role      | Capabilities                                              |
|-----------|-----------------------------------------------------------|
| `admin`   | Full access: model design, DDL publish, data write, approve, user management. |
| `steward` | Model read-only, write to landing, review and approve staging records. |
| `reader`  | Read golden records and history only.                     |
| `service` | Write to landing only (via API key). Cannot approve.      |

Two invariants are enforced in code:

- **Service keys cannot approve.** The `service` role does not have access to the
  approval endpoint. A machine credential can write into landing and stop there.
  This ensures no integration can rubber-stamp its own data and bypass review.

- **Segregation of duties.** When `ENFORCE_SEGREGATION_OF_DUTIES=true` (default),
  `apply_staging_to_live()` compares the approving actor against
  `mdm_edited_by OR mdm_submitted_by`. If they match, a `SegregationOfDutiesError`
  is raised. A second human must sign off. The setting can be disabled for small
  teams, but the README is explicit about what that gives up.

### LDAP / Active Directory

When `LDAP_ENABLED=true`, authentication works by:
1. Service account (`LDAP_BIND_DN`) locates the user in the directory.
2. The application re-binds **as that user** to verify the password — credentials
   are not retrieved or compared locally.
3. Group membership is resolved (optionally transitively via `LDAP_NESTED_GROUPS`).
4. Each group DN is matched against `mdm_meta.group_role_mapping` rows to
   determine the application role.

A user who authenticates successfully but matches no group mapping is refused with
a clear message. A local break-glass admin (`LOCAL_ADMIN_ENABLED=true`, default)
remains available if the directory is unreachable.

---

## 9. Auditability

Every consequential action writes a row to `mdm_meta.audit_event`, which is
append-only (no update or delete is ever issued against it). The table captures:
`actor`, `actor_roles`, `action`, `entity_name`, `record_id`, `tier`,
`before_value` (jsonb), `after_value` (jsonb), `ip_address`, and `success`.

The full lineage of a golden record is traceable forward and backward:

```
mdm_landing.<entity>.mdm_landing_id
      |
      v (mdm_landing_id FK)
mdm_staging.<entity>.mdm_staging_id
      |
      v (mdm_staging_id FK on live; mdm_staging_id on history)
mdm.<entity>.mdm_id  <------->  mdm_history.<entity>.mdm_id
                                (one history row per approved change)
      |
      v
mdm_meta.audit_event.record_id = mdm_id
```

Promotion runs are recorded in `mdm_meta.promotion_batch`, which stores row
counts (`rows_in`, `rows_ok`, `rows_failed`), timing, and the triggering actor.

---

## 10. Design decisions and tradeoffs

### What was deliberately chosen

- **Metadata-driven DDL at runtime.** The operator defines entities through the UI
  or YAML; the platform generates and applies the physical schema. This makes the
  system self-service without requiring DDL access for data stewards.

- **SQLAlchemy Core for generated tables.** The ORM is not applicable to tables
  whose structure is unknown at import time. Core's `text()` with bind parameters
  provides the same injection safety without requiring static class declarations.

- **Additive-safe, destructive-blocked migrations by default.** Operators can
  iterate on the model without fear of accidentally dropping data. Drops and
  narrowings must be deliberate.

- **`mdm_supplied_fields` for PATCH correctness.** Tracking exactly which fields
  were sent solves the "silent overwrite" problem for partial updates without
  requiring callers to send explicit null markers.

### What was deliberately left out

- **Fuzzy or probabilistic matching.** Duplicate detection uses exact comparison
  on normalised match keys. There is no edit-distance, phonetic, or probabilistic
  matching. `_resolve_target()` in `pipeline.py` is the extension point if this
  is needed.

- **Multi-stage approval chains.** One steward approval applies a change. A
  multi-stage workflow (e.g., approval + senior countersign) would require a
  separate workflow state table. The current design is a single-approver model.

- **Built-in scheduler.** Retention via `retention_days` and periodic batch
  promotion are exposed as API endpoints. Scheduling them is left to the operator's
  own cron, Airflow, or equivalent.

- **Probabilistic survivorship.** There is no merge or survivorship ruleset. When
  two staging records resolve to the same golden record, each is applied
  independently by whichever steward reviews it first. Field-level source priority
  rules are not implemented.

- **Cross-entity foreign keys.** `ref_entity` and `ref_attribute` on `Attribute`
  are recorded in the metadata and shown in the UI, but no database-level foreign
  key constraint is generated between master-data entities. Relationships between
  golden records are resolved at the application level after records exist.
