# MDM Platform

A self-hostable, metadata-driven **Master Data Management** application for PostgreSQL.

Data models are configured in the UI (or imported from version-controlled files). Publishing a
model generates and applies the physical DDL. Every inbound write lands in a staging area,
gets validated, and waits for a human data steward to approve it before it touches a golden
record.

```
   POST / PUT / PATCH / DELETE
              │
              ▼
      ┌───────────────┐   raw jsonb, never rejected
      │  mdm_landing  │
      └───────┬───────┘
              │  validate · coerce · normalise · match
              ▼
      ┌───────────────┐   typed, invalid rows retained for repair
      │  mdm_staging  │
      └───────┬───────┘
              │  ← human data steward: review, edit, approve
              ▼
      ┌───────────────┐        ┌────────────────┐
      │      mdm      │───────▶│  mdm_history   │
      │ golden records│  prior │ every version  │
      └───────────────┘ version└────────────────┘
```

**The write API never modifies a golden record directly.** That is the guarantee the whole
system is built around.

---

## Contents

- [Quickstart](#quickstart)
- [Why four tiers](#why-four-tiers)
- [Roles and perspectives](#roles-and-perspectives)
- [Defining a data model](#defining-a-data-model)
- [The write API](#the-write-api)
- [Stewardship workflow](#stewardship-workflow)
- [LDAP / Active Directory](#ldap--active-directory)
- [Configuration reference](#configuration-reference)
- [Deployment](#deployment)
- [Testing](#testing)
- [Known limitations](#known-limitations)

Further reading: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) ·
[`docs/API.md`](docs/API.md) · [`docs/LDAP.md`](docs/LDAP.md) ·
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) · [`docs/RUNBOOK.md`](docs/RUNBOOK.md)

---

## Quickstart

### With Docker Compose

```bash
cp .env.example .env          # then edit SECRET_KEY at minimum
docker compose up --build
```

Open <http://localhost:8000>. Sign in with the break-glass admin printed in the startup logs
(or whatever you set as `LOCAL_ADMIN_PASSWORD`).

### Locally against your own PostgreSQL

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # point PG* at your cluster
.venv/bin/python -m scripts.bootstrap        # create schemas + metadata tables
.venv/bin/uvicorn app.main:app --port 8000
```

`scripts/bootstrap.py` is idempotent and reports precisely which grant is missing if the
DDL role lacks a privilege, rather than surfacing a raw driver error.

Interactive API docs: <http://localhost:8000/api/docs>

---

## Why four tiers

Most "MDM" tools either validate at the edge and reject bad data, or accept everything into one
table and hope someone cleans it up. Both fail in practice: the first loses inbound messages
you needed, the second corrupts the golden record.

| Tier | Schema | Constraints | Purpose |
|---|---|---|---|
| Landing | `mdm_landing` | none — `jsonb` payload | **Never rejects.** Captures exactly what was sent, with source system, batch id and receipt time. Losing an inbound message is worse than storing a bad one. |
| Staging | `mdm_staging` | typed, but nullable | Validated and coerced. **Invalid rows are stored deliberately**, with their errors attached, because a steward cannot fix data they cannot see. |
| Live | `mdm` | `NOT NULL`, unique indexes, versioned | The golden records. Only ever written by an approved promotion. |
| History | `mdm_history` | none | Every prior version of every golden record, effective-dated. |

A record's journey is fully auditable: `mdm_landing_id` → `mdm_staging_id` → `mdm_id` → history
rows, plus an append-only audit event for every consequential action.

---

## Roles and perspectives

| Role | Model design | DDL publish | Write data | Approve | Users / keys |
|---|---|---|---|---|---|
| **admin** | ✅ | ✅ | ✅ | ✅ | ✅ |
| **steward** | read-only | ❌ | ✅ | ✅ | ❌ |
| **reader** | read-only | ❌ | ❌ | ❌ | ❌ |
| **service** (API key) | read-only | ❌ | ✅ landing only | ❌ | ❌ |

Two guarantees worth calling out:

- **A service account can never approve.** Machine credentials write into landing and stop
  there. Otherwise an integration could rubber-stamp its own data and the review step would be
  theatre.
- **Segregation of duties.** By default a steward cannot approve a record they submitted *or*
  last edited — a second human must sign off. Set `ENFORCE_SEGREGATION_OF_DUTIES=false` if your
  team is too small for that, but understand what you're giving up.

The UI adapts to the signed-in role: stewards land on the review queue and never see the DDL
publish controls; readers get golden records and history only.

---

## Defining a data model

Design in the UI under **Data models → New model**, or write a file and import it:

```yaml
mdm_schema_version: "1.0"
entities:
  - name: customer                 # physical table name, lower snake_case
    display_name: Customer Master
    domain: party
    requires_approval: true
    soft_delete: true
    attributes:
      - name: customer_code
        data_type: string
        length: 40
        is_required: true
        is_unique: true
        is_business_key: true      # resolves updates to existing records
        normalization: [trim, upper]
      - name: legal_name
        data_type: string
        length: 250
        is_required: true
        is_match_key: true         # drives duplicate detection
        normalization: [trim, collapse_whitespace]
      - name: email
        data_type: email           # validated, stored as varchar(320)
        is_pii: true
      - name: credit_limit
        data_type: decimal
        numeric_precision: 14
        numeric_scale: 2
        validation: {min: 0}
      - name: country
        data_type: enum
        length: 2
        validation: {enum: [AU, US, GB, DE, NZ]}
```

```bash
# Preview what would change — writes nothing
curl -X POST "$BASE/api/v1/models/import?dry_run=true" \
     -b cookies.txt -F "file=@customer.yaml"

# Apply the definition (metadata only), then publish the DDL
curl -X POST "$BASE/api/v1/models/import?dry_run=false" -b cookies.txt -F "file=@customer.yaml"
curl -X POST "$BASE/api/v1/models/customer/publish" -b cookies.txt \
     -H 'Content-Type: application/json' -d '{"change_note":"initial"}'
```

**Types:** `string` `text` `integer` `bigint` `decimal` `float` `boolean` `date` `timestamp`
`uuid` `json` `email` `url` `enum`

**Normalisation rules** (applied before validation and matching): `trim` `lower` `upper`
`title` `collapse_whitespace` `strip_punctuation` `digits_only` `nullify_empty`

**Validation rules:** `min` `max` `min_length` `max_length` `regex` `enum`

### Publishing is safe by default

`GET /models/{entity}/ddl` returns the exact SQL that publishing would run. Adding columns and
widening types apply automatically. Anything that could destroy data — dropping a column,
narrowing `varchar(320)` to `varchar(100)` — is classified **destructive**, refused, and
reported with the SQL and a required confirmation flag:

```json
{
  "detail": {
    "message": "This change would drop columns or narrow types...",
    "destructive_changes": [
      "live.customer.email: varchar(320) -> varchar(100) may truncate or fail. SQL: ..."
    ]
  }
}
```

Re-submit with `{"confirm_destructive": true}` once you've read it.

---

## The write API

Every verb is asynchronous by design and returns **`202 Accepted`** — the change has been
captured and staged, not applied.

```bash
KEY="mdm_..."   # issued under Administration → API keys

# Create
curl -X POST "$BASE/api/v1/data/customer?idempotency_key=erp-8891" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"customer_code":"acme-01","legal_name":"Acme Pty Ltd","country":"AU"}'

# Partial update — only the supplied fields change
curl -X PATCH "$BASE/api/v1/data/customer/$MDM_ID" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"credit_limit":"50000"}'

# Upsert by business key — the usual integration entry point
curl -X PUT "$BASE/api/v1/data/customer" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"customer_code":"acme-01","legal_name":"Acme Industries Pty Ltd"}'

# Request deletion (subject to approval)
curl -X DELETE "$BASE/api/v1/data/customer/$MDM_ID" -H "X-API-Key: $KEY"

# Bulk and CSV
curl -X POST "$BASE/api/v1/data/customer/bulk" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"operation":"UPSERT","records":[{...},{...}],"source_system":"SAP"}'
curl -X POST "$BASE/api/v1/data/customer/import-csv" -b cookies.txt -F "file=@customers.csv"
```

Response:

```json
{
  "accepted": true,
  "landing_id": 4821,
  "staging_id": 3310,
  "validation_passed": false,
  "validation_errors": 2,
  "change_type": "insert",
  "status": "pending_review",
  "message": "Change accepted into the landing tier and staged for steward review."
}
```

Note `validation_passed: false` with `accepted: true` — the record was captured and is waiting
for a steward to repair it. Nothing was lost.

**Idempotency:** pass `?idempotency_key=...`. A replay returns the original `landing_id` with
`"deduplicated": true` and creates nothing new. Essential for at-least-once message delivery.

Reads are immediate and unversioned by review:

```bash
curl "$BASE/api/v1/data/customer?q=acme&country=AU&limit=50&sort=legal_name" -b cookies.txt
curl "$BASE/api/v1/data/customer/$MDM_ID/history" -b cookies.txt
curl "$BASE/api/v1/data/customer/statistics" -b cookies.txt
curl "$BASE/api/v1/data/customer/export-csv" -b cookies.txt -o customers.csv
```

Any attribute name works as an exact-match filter; `q=` is a free-text search across text
columns.

---

## Stewardship workflow

```bash
# What needs attention, across all entities
curl "$BASE/api/v1/stewardship/queue" -b cookies.txt

# One entity's queue; add &only_invalid=true to triage the broken ones
curl "$BASE/api/v1/stewardship/customer/queue?status=pending_review" -b cookies.txt

# Incoming record beside the current golden values, with a field-level diff
curl "$BASE/api/v1/stewardship/customer/staging/3310" -b cookies.txt

# Repair it — re-validates immediately
curl -X PATCH "$BASE/api/v1/stewardship/customer/staging/3310" -b cookies.txt \
  -H 'Content-Type: application/json' \
  -d '{"updates":{"country":"AU","credit_limit":"25000"}}'

# Approve (applies to live + writes history) or reject
curl -X POST "$BASE/api/v1/stewardship/customer/staging/3310/approve" -b cookies.txt \
  -H 'Content-Type: application/json' -d '{"note":"Verified against SAP"}'
curl -X POST "$BASE/api/v1/stewardship/customer/staging/3310/reject" -b cookies.txt \
  -H 'Content-Type: application/json' -d '{"reason":"Duplicate of C-0042"}'

# Bulk decisions — each record applies independently, so one failure
# doesn't abort the rest
curl -X POST "$BASE/api/v1/stewardship/customer/staging/bulk-approve" -b cookies.txt \
  -H 'Content-Type: application/json' -d '{"staging_ids":[3310,3311,3312]}'
```

An invalid record cannot be approved — the API returns `400` explaining that it must be
corrected first. Attempting to approve your own submission returns `403` naming segregation of
duties.

---

## LDAP / Active Directory

Set `LDAP_ENABLED=true` and configure the directory (see [`docs/LDAP.md`](docs/LDAP.md) for the
full walkthrough, including LDAPS and nested groups):

```bash
LDAP_ENABLED=true
LDAP_SERVER=ldaps://dc01.corp.example.com:636
LDAP_USE_SSL=true
LDAP_BIND_DN=CN=svc_mdm,OU=Service Accounts,DC=corp,DC=example,DC=com
LDAP_BIND_PASSWORD=...
LDAP_BASE_DN=DC=corp,DC=example,DC=com
LDAP_USER_FILTER=(&(objectClass=user)(sAMAccountName={username}))
LDAP_NESTED_GROUPS=true
```

Then map directory groups to roles under **Administration → LDAP / AD** (or via
`LDAP_ADMIN_GROUP_DN` etc. for bootstrap).

How it works: the service account locates the user, then the application **re-binds as that
user** to verify the password — credentials are never retrieved or compared locally. Nested AD
groups are resolved transitively via `LDAP_MATCHING_RULE_IN_CHAIN`. Usernames are escaped per
RFC 4515, so a username like `*)(objectClass=*` cannot alter the search filter.

A user who authenticates but matches no group mapping is refused with a clear message rather
than silently granted an empty session.

**Break-glass access:** if the directory is unreachable, the local admin account still works, so
you can't be locked out of your own MDM system during an AD outage. Set a strong
`LOCAL_ADMIN_PASSWORD`, or let the app generate one and read it from the startup log.

---

## Configuration reference

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | dev placeholder | **Change this.** Signs sessions and JWTs. |
| `PGHOST` / `PGPORT` / `PGDATABASE` | `localhost` / `5432` / `mdm` | Target cluster. |
| `PGUSER` / `PGPASSWORD` | `mdm_app` | Runtime role — DML only. |
| `PG_DDL_USER` / `PG_DDL_PASSWORD` | falls back to `PGUSER` | Elevated role, used only when publishing. |
| `PGSSLMODE` | `prefer` | Use `require` or stricter in production. |
| `SCHEMA_*` | `mdm_meta` … | Schema names, if you need to avoid collisions. |
| `LDAP_*` | disabled | See [`docs/LDAP.md`](docs/LDAP.md). |
| `LOCAL_ADMIN_ENABLED` | `true` | Break-glass admin. |
| `ENFORCE_SEGREGATION_OF_DUTIES` | `true` | Blocks self-approval. |
| `ALLOW_DESTRUCTIVE_DDL` | `false` | Global guard; per-request confirmation still required. |
| `AUTO_PROMOTE_LANDING` | `true` | Validate into staging on write. Disable to batch it. |
| `SOFT_DELETE` | `true` | Tombstone rather than remove. |
| `MAX_BULK_ROWS` | `10000` | Per-request cap. |
| `ACCESS_TOKEN_TTL_MINUTES` | `480` | Session lifetime. |
| `CORS_ORIGINS` | localhost | Comma-separated. |

### Two-role privilege model

Publishing needs elevated rights; serving traffic does not. Keep them separate:

```sql
CREATE ROLE mdm_ddl LOGIN PASSWORD '...' ;
CREATE ROLE mdm_app LOGIN PASSWORD '...' ;
CREATE DATABASE mdm OWNER mdm_ddl;
GRANT CONNECT ON DATABASE mdm TO mdm_app;
-- Optional: allow the app to provision new databases itself
ALTER ROLE mdm_ddl CREATEDB;
```

The bootstrap grants `mdm_app` DML on all MDM schemas and sets default privileges so
future generated tables are reachable too. `GET /api/v1/models/cluster/privileges` reports
exactly which grants are missing and the SQL to fix each one.

---

## Deployment

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). In short:

- Run behind TLS. Session cookies are marked `Secure` automatically when `ENVIRONMENT=production`.
- Probes: `/health` (liveness) and `/ready` (readiness — fails when the database is unreachable).
- The frontend has **no build step**: React is vendored under `app/static/vendor/`, so the
  image needs no Node toolchain and works in air-gapped networks.
- Scale horizontally; all state is in PostgreSQL.

---

## Testing

```bash
.venv/bin/pytest              # 271 tests
.venv/bin/pytest -k ddl       # one area
```

The suite runs against a **real PostgreSQL database**, deliberately — the product *is* DDL
generation and transactional promotion, so mocking the database away would test almost nothing.
Set `PG*` to a scratch database; each test provisions uniquely-named entities and drops them
afterwards. LDAP is covered using `ldap3`'s mock AD directory, so no live server is needed.

`scripts/capture_ui.py` drives the UI in headless Firefox and writes screenshots, if you want
visual regression evidence.

---

## Known limitations

Stated plainly, so nothing surprises you in production:

- **Matching is deterministic only.** Duplicate detection uses exact comparison on configured
  match keys after normalisation. There is no fuzzy/probabilistic matching or survivorship
  ruleset; `_resolve_target` in `app/services/pipeline.py` is the extension point.
- **Single-approver workflow.** One steward approval applies a change. Multi-stage approval
  chains would need a workflow table.
- **`AUTO_PROMOTE_LANDING` promotes inline.** Landing→staging validation runs in the request.
  For very high write volumes, disable it and drive `POST /stewardship/{entity}/promote` from a
  scheduler instead.
- **No built-in scheduler.** Retention (`retention_days`) and periodic promotion are exposed as
  endpoints for your own cron/Airflow to call.
- **Reference attributes are metadata only.** `ref_entity`/`ref_attribute` are recorded and
  shown in the UI, but no cross-entity foreign key is generated — relationships between master
  entities are usually resolved after the golden record exists.
- **Landing is not auto-pruned.** It grows monotonically by design (it is your inbound audit
  trail). Archive it on a schedule that suits your retention policy.
