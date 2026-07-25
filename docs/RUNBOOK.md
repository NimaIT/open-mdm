# Runbook

Operational procedures for running the MDM Platform in production.

---

## 1. Health monitoring

### Probe endpoints

| Endpoint  | Purpose     | Returns unhealthy when                          | Use as              |
|-----------|-------------|-------------------------------------------------|---------------------|
| `/health` | Liveness    | Never (the process is up or it is not)          | Liveness probe      |
| `/ready`  | Readiness   | The database is unreachable                     | Readiness probe, load-balancer health check |

The readiness probe calls `db.check_connection()`, which issues
`SELECT current_database(), current_user, current_setting('server_version')`.
If the runtime connection pool cannot reach the cluster, `/ready` returns a
non-2xx status. Configure your load balancer or orchestrator to stop sending
traffic to an instance that fails `/ready`.

### What to alert on

| Condition | How to detect | Severity |
|-----------|---------------|----------|
| Process down | `/health` failing | Critical |
| Database unreachable | `/ready` failing | Critical |
| Landing `pending` count growing and not draining | SQL query — see section 7 | Warning |
| Staging `pending_review` count growing beyond threshold | SQL query — see section 7 | Warning |
| High `error` rate on landing rows | SQL query — see section 7 | Warning |
| Many staging rows with `mdm_is_valid = false` from one source | SQL query — see section 7 | Warning |
| Audit log gaps (no rows in `mdm_meta.audit_event` for >N minutes during business hours) | SQL query on `occurred_at` | Warning |

---

## 2. Provisioning a new environment

### Step 1 — Create the two PostgreSQL roles

Connect to the cluster as a superuser and run:

```sql
CREATE ROLE mdm_ddl LOGIN PASSWORD '<strong-password>';
CREATE ROLE mdm_app  LOGIN PASSWORD '<strong-password>';
CREATE DATABASE mdm OWNER mdm_ddl;
GRANT CONNECT ON DATABASE mdm TO mdm_app;
```

`mdm_ddl` owns the database and will create schemas and tables.
`mdm_app` is the runtime role — DML only. It never issues DDL.

If you want the platform to be able to provision new databases itself (required
for multi-tenancy scenarios), grant CREATEDB:

```sql
ALTER ROLE mdm_ddl CREATEDB;
```

### Step 2 — Configure the application

Copy `.env.example` to `.env` and set at minimum:

```
SECRET_KEY=<cryptographically random string>
PGHOST=<your-cluster-host>
PGDATABASE=mdm
PGUSER=mdm_app
PGPASSWORD=<mdm_app password>
PG_DDL_USER=mdm_ddl
PG_DDL_PASSWORD=<mdm_ddl password>
PGSSLMODE=require
ENVIRONMENT=production
```

### Step 3 — Run bootstrap

Bootstrap is idempotent — running it multiple times is safe.

```bash
python -m scripts.bootstrap
```

Or via the API (requires the DDL role to be reachable):

```bash
curl -X POST http://localhost:8000/api/v1/admin/bootstrap -b cookies.txt
```

Bootstrap creates the five MDM schemas (`mdm_meta`, `mdm_landing`, `mdm_staging`,
`mdm`, `mdm_history`), grants DML to the runtime role on each, sets
`ALTER DEFAULT PRIVILEGES` so future generated tables are reachable without
re-granting, and creates the fixed internal tables via SQLAlchemy metadata.

### Step 4 — Check privileges

```bash
curl http://localhost:8000/api/v1/models/cluster/privileges -b cookies.txt
```

The response lists every privilege check with `ok: true/false` and, for any
failing check, the exact SQL `remedy`. Example failing check:

```json
{
  "name": "create_schema",
  "ok": false,
  "detail": "Required to create MDM schemas and tables.",
  "remedy": "GRANT CREATE ON DATABASE mdm TO \"mdm_ddl\";"
}
```

Apply the remedy SQL as superuser and re-run bootstrap. Do not proceed past this
step until all checks pass.

---

## 3. Publishing a model change safely

### Preview before applying

```bash
curl http://localhost:8000/api/v1/models/customer/ddl -b cookies.txt
```

Returns the exact SQL that would execute, along with `warnings` (e.g., a required
column added without a default to a populated table) and `destructive` (drops and
narrowings). No writes occur.

### Read the destructive-change report

If `is_destructive: true` appears in the DDL preview response, the `destructive`
array lists each problematic change with the SQL and an explanation. Read every
item. Data loss is possible.

### Publish (additive changes)

```bash
curl -X POST http://localhost:8000/api/v1/models/customer/publish \
     -b cookies.txt \
     -H 'Content-Type: application/json' \
     -d '{"change_note": "add credit_tier column"}'
```

### Publish with destructive changes (deliberate, read the report first)

```bash
curl -X POST http://localhost:8000/api/v1/models/customer/publish \
     -b cookies.txt \
     -H 'Content-Type: application/json' \
     -d '{"change_note": "narrow email field", "confirm_destructive": true}'
```

This also requires `ALLOW_DESTRUCTIVE_DDL=false` to be left at its default, or
set to `true` if you want to permit destructive changes globally. The per-request
`confirm_destructive` flag is still required regardless.

### If a publish fails midway

Publishing runs inside a transaction on the DDL connection. If it fails, the
transaction rolls back and no partial schema change is left. The entity status
will remain `modified` rather than advancing to `published`. Re-run the DDL
preview to confirm the current state, fix the root cause (missing privilege,
conflicting constraint, etc.), and republish.

---

## 4. Common incidents

### 4.1 Writes rejected with 409 "defined but not published"

**Symptom.** API writes to an entity return `409 Conflict` with a message
referencing the entity being defined but not published.

**Diagnosis.** The entity was created or modified in the metadata but DDL has not
been applied. Check entity status:

```sql
SELECT name, status, version, published_version
FROM mdm_meta.entity
WHERE name = 'customer';
```

`status` will be `draft` or `modified`.

**Resolution.** Publish the entity:

```bash
curl -X POST http://localhost:8000/api/v1/models/customer/publish \
     -b cookies.txt \
     -H 'Content-Type: application/json' \
     -d '{"change_note": "initial publish"}'
```

If the entity should not yet be published, the write is premature and the caller
should wait.

---

### 4.2 A user authenticates but receives no access

**Symptom.** User logs in successfully (password accepted) but receives an empty
session or an access-denied response.

**Diagnosis.** LDAP authentication succeeded but no group mapping resolved to an
application role. This is the deliberate behaviour described in the README: a user
who matches no group mapping is refused with a clear message rather than silently
granted an empty session.

Check whether the user's group DNs appear in the mapping table:

```sql
SELECT group_dn, role, is_active
FROM mdm_meta.group_role_mapping
WHERE is_active = true;
```

Compare against the user's actual group memberships in the directory.

**Resolution.** Add a `group_role_mapping` row for the user's group DN, or add
the user to a group that already has a mapping. Via the UI: **Administration
-> LDAP / AD -> Group mappings -> Add**. Via SQL:

```sql
INSERT INTO mdm_meta.group_role_mapping (id, group_dn, role, is_active)
VALUES (gen_random_uuid(),
        'CN=MDM-Stewards,OU=Groups,DC=corp,DC=example,DC=com',
        'steward',
        true);
```

---

### 4.3 Directory unreachable — break-glass local admin

**Symptom.** LDAP/AD is down. All directory-backed users cannot log in.

**Resolution.** Sign in as the local break-glass admin. The username and generated
password are printed in the application startup log if `LOCAL_ADMIN_PASSWORD` was
not set in `.env`. Search the startup output for `LOCAL ADMIN`.

The local admin account works regardless of LDAP availability because
`LOCAL_ADMIN_ENABLED=true` by default and the account is sourced from local
storage in `mdm_meta.app_user`, not the directory.

Set a strong, pre-configured `LOCAL_ADMIN_PASSWORD` in `.env` before going to
production so you are not relying on a startup-log secret.

---

### 4.4 Landing rows stuck in `pending`

**Symptom.** Writes return `202 Accepted` but rows in `mdm_landing.<entity>` with
`mdm_status = 'pending'` accumulate and do not advance.

**Diagnosis 1 — auto-promotion is disabled.**

```bash
grep AUTO_PROMOTE_LANDING .env
```

If `AUTO_PROMOTE_LANDING=false`, promotion does not run inline on writes. Trigger
it explicitly:

```bash
curl -X POST http://localhost:8000/api/v1/stewardship/customer/promote \
     -b cookies.txt
```

Or from a cron/scheduler on your defined cadence.

**Diagnosis 2 — promotion is running but failing.**

Check for `mdm_status = 'error'` rows:

```sql
SELECT mdm_landing_id, mdm_errors, mdm_received_at
FROM mdm_landing.customer
WHERE mdm_status = 'error'
ORDER BY mdm_received_at DESC
LIMIT 20;
```

Inspect `mdm_errors` for the structured error. Common causes: the entity tables do
not exist (not published), or a per-row exception during staging insert. Check the
application log for the corresponding `landing row N failed` log line.

**Resolution.** Fix the root cause (publish the entity, resolve the DB constraint
issue), then re-trigger promotion. Rows with `mdm_status = 'error'` can be reset
to `'pending'` to retry:

```sql
UPDATE mdm_landing.customer
SET mdm_status = 'pending', mdm_errors = NULL, mdm_processed_at = NULL
WHERE mdm_status = 'error'
  AND mdm_landing_id IN (<ids>);
```

---

### 4.5 Staging queue growing — no stewards reviewing

**Symptom.** `mdm_staging.<entity>` rows with `mdm_status = 'pending_review'`
accumulate.

**Diagnosis.** Either no stewards are assigned, stewards are not reviewing, or the
queue is large enough that manual review is impractical.

**Short-term.** Alert the stewards responsible for this entity. Point them at the
review queue:

```bash
curl http://localhost:8000/api/v1/stewardship/customer/queue?status=pending_review \
     -b cookies.txt
```

**Bulk approval (use with care).** If the queue contains known-good data (e.g.,
an initial bulk load from a trusted source with `mdm_is_valid = true`):

```bash
curl -X POST http://localhost:8000/api/v1/stewardship/customer/staging/bulk-approve \
     -b cookies.txt \
     -H 'Content-Type: application/json' \
     -d '{"staging_ids": [101, 102, 103]}'
```

Note: `mdm_is_valid` must be `true` for each record or the approval will be
refused per-row.

---

### 4.6 Many invalid staged rows from one source system

**Symptom.** A spike of staging rows with `mdm_is_valid = false` from a single
`mdm_source_system` value.

**Diagnosis.** Likely an upstream contract change — a field was renamed, removed,
or a type changed in the source. Query the error distribution:

```sql
SELECT e->>'code' AS error_code,
       e->>'field' AS field,
       count(*) AS occurrences
FROM mdm_staging.customer,
     jsonb_array_elements(mdm_errors) AS e
WHERE mdm_source_system = 'SAP'
  AND mdm_is_valid = false
  AND mdm_status = 'pending_review'
GROUP BY error_code, field
ORDER BY occurrences DESC;
```

Common codes: `type` (coercion failure), `required` (field missing), `enum`
(disallowed value), `max_length` (truncation needed), `unknown_field` (field not
in model).

**Resolution.**
- For `unknown_field`: the source is sending a field name that does not exist on
  the entity. Either add the attribute to the model and republish, or instruct
  the source to stop sending it.
- For `type` or `max_length`: the entity's attribute definition may need
  widening (safe, no confirmation required). Alternatively, the source team needs
  to fix their output format.
- Bulk-reject the bad batch if the data cannot be repaired:
  `POST /stewardship/customer/staging/bulk-approve` with `reject` semantics, or
  update the staging status directly.

---

### 4.7 Duplicate golden records appearing

**Symptom.** Two or more `mdm.<entity>` rows represent what appears to be the same
real-world entity.

**Diagnosis.** Match keys are not configured, or were configured after some records
were already approved, or the match key fields differ enough between the two records
that the deterministic comparison did not fire (e.g., one says "Acme Pty Ltd" and
another "ACME PTY LTD" before normalisation was applied).

Check whether match keys are defined:

```sql
SELECT name FROM mdm_meta.attribute
WHERE entity_id = (SELECT id FROM mdm_meta.entity WHERE name = 'customer')
  AND is_match_key = true;
```

If empty, no match keys are configured and the DDL plan for that entity will have
included a warning:

```
No match key defined — duplicate detection against golden records will be
skipped for this entity.
```

**Resolution.**
- Add `is_match_key = true` to the appropriate attribute(s) via the UI or YAML and
  republish. New writes will use the match key for duplicate detection from that
  point forward. Existing duplicates are not merged automatically.
- For existing duplicates: the platform has no automated merge. A steward must
  identify the golden record to keep (`mdm_id`), copy any curated values from the
  duplicate into it via a PATCH, and then approve a DELETE on the duplicate.

---

### 4.8 A bad approval that needs undoing

**Symptom.** A staging record was approved and applied to the live table
incorrectly — wrong values, wrong record targeted, or premature approval.

**Important constraint.** There is no undo endpoint. The approved staging record
has `mdm_status = 'applied'` and cannot be reopened.

**Recovery using `mdm_history`.**

The history table holds the pre-change state of the golden record. Find the
version before the bad approval:

```sql
-- Find the history row that was written immediately before the bad change
SELECT mdm_history_id,
       mdm_version,
       mdm_change_type,
       mdm_valid_from,
       mdm_valid_to,
       mdm_changed_by,
       -- add the business columns you need to inspect:
       legal_name,
       credit_limit,
       country
FROM mdm_history.customer
WHERE mdm_id = '<the mdm_id of the affected record>'
ORDER BY mdm_version DESC;
```

The row with the highest `mdm_version` less than the current live version holds
the values that were in effect before the bad approval.

To restore those values, submit a PATCH with the correct values via the write API
and get it approved by the appropriate steward. This creates a new history entry
and increments the version, leaving a complete audit trail including the bad
approval and the subsequent correction.

Do not modify the live table directly — that would bypass the staging tier and
break the lineage chain.

---

## 5. Routine maintenance

### Archiving the landing tier

Landing tables grow monotonically by design. The landing tier is the inbound audit
trail and is not auto-pruned. Archive or partition it on a schedule that suits your
retention policy.

The `retention_days` column on `mdm_meta.entity` sets the target age for landing
rows; the platform exposes a retention endpoint for your scheduler to call:

```bash
curl -X POST http://localhost:8000/api/v1/admin/retention/customer \
     -b cookies.txt
```

To archive promoted rows older than N days before deleting, export first:

```sql
COPY (
  SELECT * FROM mdm_landing.customer
  WHERE mdm_status IN ('promoted', 'rejected')
    AND mdm_received_at < now() - INTERVAL '90 days'
) TO '/archive/customer_landing_pre_20260101.csv' CSV HEADER;
```

Then delete:

```sql
DELETE FROM mdm_landing.customer
WHERE mdm_status IN ('promoted', 'rejected')
  AND mdm_received_at < now() - INTERVAL '90 days';
```

Rows with `mdm_status = 'pending'` should not be archived until they are promoted
or explicitly rejected — they may still be waiting for the promotion job.

### Reviewing the audit log

```sql
-- Recent consequential actions
SELECT occurred_at, actor, action, entity_name, record_id, success
FROM mdm_meta.audit_event
ORDER BY occurred_at DESC
LIMIT 100;

-- All actions by a specific user
SELECT occurred_at, action, entity_name, record_id, detail
FROM mdm_meta.audit_event
WHERE actor = 'jsmith'
ORDER BY occurred_at DESC;

-- Failed actions
SELECT occurred_at, actor, action, entity_name, detail
FROM mdm_meta.audit_event
WHERE success = false
ORDER BY occurred_at DESC;
```

### Rotating API keys

API keys are stored hashed in `mdm_meta.api_key`. The raw key is shown once at
creation and is not retrievable.

To rotate a key:
1. Issue a new key under **Administration -> API keys -> New**.
2. Update the consuming service to use the new key.
3. Verify the new key is working by checking `last_used_at` on the new row.
4. Deactivate the old key: **Administration -> API keys -> Deactivate**, or:

```sql
UPDATE mdm_meta.api_key SET is_active = false
WHERE key_prefix = '<old-prefix>';
```

### Rotating `SECRET_KEY`

`SECRET_KEY` signs session cookies and JWTs. Rotating it invalidates all active
sessions, forcing users to re-authenticate.

1. Generate a new key: `python -c "import secrets; print(secrets.token_hex(32))"`.
2. Update `SECRET_KEY` in `.env` (or your secrets manager).
3. Restart the application. All existing sessions will be invalidated immediately.

Schedule this rotation according to your security policy. There is no incremental
key rollover — all sessions terminate at once.

---

## 6. Backup and recovery

### What holds state

All state is in PostgreSQL. There are no file-system blobs, no external object
stores, and no in-process caches that need to survive a restart. A full database
backup is a full application backup.

Five schemas to include in every backup:

| Schema          | Content                                            |
|-----------------|----------------------------------------------------|
| `mdm_meta`      | Entity/attribute definitions, users, audit events, promotion batches |
| `mdm_landing`   | Inbound audit trail — all writes ever received     |
| `mdm_staging`   | Review queue, validation results                   |
| `mdm`           | Golden records (current state)                     |
| `mdm_history`   | Every prior version of every golden record         |

### Point-in-time recovery

`mdm_history` is what makes point-in-time reconstruction possible. For any golden
record, the full sequence of approved values can be read from history using
`mdm_valid_from` and `mdm_valid_to`. You can reconstruct "what did this record
look like on date X" with:

```sql
SELECT *
FROM mdm_history.customer
WHERE mdm_id = '<mdm_id>'
  AND mdm_valid_from <= '2026-06-01 00:00:00+00'
  AND mdm_valid_to   >  '2026-06-01 00:00:00+00';
```

### Recommendations

- Enable WAL archiving (continuous archiving + point-in-time recovery) on the
  PostgreSQL cluster so you can recover to any second, not just nightly backup
  snapshots.
- Take nightly logical backups (`pg_dump`) as a secondary safety net and to
  support cross-cluster restore.
- Test recovery regularly against a non-production cluster. A backup you have
  never restored is untested.
- The landing tier is high-write. If it is partitioned by time (a common approach
  for archival), ensure your backup covers all active partitions.

---

## 7. Useful SQL queries

All column names are taken directly from the DDL definitions in
`app/services/ddl.py` and `app/models/meta.py`. Replace `customer` with the
relevant entity name.

### Pending counts per tier

```sql
-- Landing: rows awaiting promotion
SELECT count(*) AS pending_landing
FROM mdm_landing.customer
WHERE mdm_status = 'pending';

-- Staging: rows awaiting steward review
SELECT count(*) AS pending_review
FROM mdm_staging.customer
WHERE mdm_status = 'pending_review';

-- Staging: all rows by status
SELECT mdm_status, count(*) AS cnt
FROM mdm_staging.customer
GROUP BY mdm_status
ORDER BY cnt DESC;
```

### Invalid staging rows grouped by error code

```sql
SELECT e->>'code'  AS error_code,
       e->>'field' AS field,
       count(*)    AS occurrences
FROM mdm_staging.customer,
     jsonb_array_elements(mdm_errors) AS e
WHERE mdm_is_valid = false
  AND mdm_status = 'pending_review'
GROUP BY error_code, field
ORDER BY occurrences DESC;
```

### Records changed in the last 24 hours (live tier)

```sql
SELECT mdm_id,
       mdm_version,
       mdm_updated_at,
       mdm_updated_by,
       mdm_source_system
FROM mdm.customer
WHERE mdm_updated_at >= now() - INTERVAL '24 hours'
ORDER BY mdm_updated_at DESC;
```

### Full audit trail for one golden record

```sql
-- Audit events
SELECT occurred_at,
       actor,
       action,
       tier,
       detail,
       before_value,
       after_value
FROM mdm_meta.audit_event
WHERE entity_name = 'customer'
  AND record_id = '<mdm_id>'
ORDER BY occurred_at;

-- History versions
SELECT mdm_history_id,
       mdm_version,
       mdm_change_type,
       mdm_valid_from,
       mdm_valid_to,
       mdm_changed_by,
       mdm_source_system
FROM mdm_history.customer
WHERE mdm_id = '<mdm_id>'
ORDER BY mdm_version DESC;
```

### Landing rows by source system and status

```sql
SELECT mdm_source_system,
       mdm_status,
       count(*)                   AS row_count,
       min(mdm_received_at)       AS earliest,
       max(mdm_received_at)       AS latest
FROM mdm_landing.customer
GROUP BY mdm_source_system, mdm_status
ORDER BY mdm_source_system, mdm_status;
```

### Promotion batch history

```sql
SELECT id,
       stage,
       status,
       rows_in,
       rows_ok,
       rows_failed,
       started_at,
       finished_at,
       triggered_by
FROM mdm_meta.promotion_batch
WHERE entity_name = 'customer'
ORDER BY started_at DESC
LIMIT 20;
```

### Staging rows from one batch

```sql
SELECT mdm_staging_id,
       mdm_status,
       mdm_is_valid,
       mdm_change_type,
       mdm_submitted_at,
       mdm_errors
FROM mdm_staging.customer
WHERE mdm_batch_id = '<batch-uuid>'
ORDER BY mdm_staging_id;
```
