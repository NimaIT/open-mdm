# MDM Platform — API Reference

This document covers every endpoint exposed by the MDM platform's REST API. The intended audience is an integration engineer wiring a source system into the platform or building tooling on top of it.

---

## Table of Contents

1. [Conventions](#1-conventions)
2. [Authentication endpoints](#2-authentication-endpoints)
3. [Data model endpoints](#3-data-model-endpoints)
4. [Master data — write endpoints](#4-master-data--write-endpoints)
5. [Master data — read endpoints](#5-master-data--read-endpoints)
6. [Stewardship endpoints](#6-stewardship-endpoints)
7. [Administration endpoints](#7-administration-endpoints)
8. [Permission matrix](#8-permission-matrix)
9. [End-to-end worked example](#9-end-to-end-worked-example)

---

## 1. Conventions

### Base URL

All endpoints are served under the `/api/v1` prefix. Throughout this document, `$BASE` refers to the scheme and host of your deployment — for example, `https://mdm.corp.example.com`. A full example URL therefore looks like:

```
$BASE/api/v1/data/customer
```

The interactive OpenAPI UI is available at `$BASE/api/docs`.

### Authentication

Three credential types are accepted. They are evaluated in this order:

| Method | How to send | Typical user |
|---|---|---|
| Session cookie | `Cookie: mdm_session=<jwt>` | Browser / human |
| Bearer token | `Authorization: Bearer <jwt>` | Machine that obtained a JWT via `/auth/login` |
| API key | `X-API-Key: mdm_...` | Automated source system |

A JWT (cookie or Bearer) is obtained by calling `POST /auth/login`. An API key is issued by an administrator via `POST /admin/api-keys`; the key string starts with `mdm_` and is shown exactly once.

All three credential types carry role information. A missing or invalid credential returns `401`. Presenting a valid credential that lacks the required permission returns `403`.

### Status codes used

| Code | Meaning |
|---|---|
| `200 OK` | Request completed and response body contains the result. |
| `201 Created` | Resource created synchronously (model definitions, admin entities). |
| `202 Accepted` | Write accepted and deposited into the landing tier. **The change has not been applied to any golden record.** |
| `400 Bad Request` | The request was structurally valid but the operation failed (e.g. DDL execution error, attempting to approve an invalid record). |
| `401 Unauthorized` | Missing, malformed, or expired credential. |
| `403 Forbidden` | Credential is valid but the caller lacks the required permission, or segregation of duties prevents the action. |
| `404 Not Found` | The named entity, record, or resource does not exist. |
| `409 Conflict` | A duplicate exists, or a destructive change requires explicit confirmation. |
| `413 Request Entity Too Large` | Bulk payload exceeds the configured `MAX_BULK_ROWS` limit. |
| `422 Unprocessable Entity` | Request body failed schema validation. |

### The meaning of 202 Accepted

Every master-data write verb (`POST`, `PUT`, `PATCH`, `DELETE`, bulk, CSV import) returns `202 Accepted`. This is deliberate and load-bearing: writes are deposited into the `mdm_landing` schema, validated and coerced into `mdm_staging`, then held there until a human data steward approves the change. Only an approved promotion touches the `mdm` (golden) schema and writes the prior version to `mdm_history`.

A `202` response does not mean the data is visible to readers. It means the data has been captured and is in the pipeline. Validation may have found errors — the response tells you — but the record is never discarded.

### Error response shape

All error responses follow FastAPI's default structure:

```json
{
  "detail": "Human-readable message"
}
```

For structured errors (destructive DDL, bulk import validation), `detail` may be an object:

```json
{
  "detail": {
    "message": "This change would drop columns or narrow types...",
    "destructive_changes": ["live.customer.email: varchar(320) -> varchar(100)"],
    "warnings": []
  }
}
```

---

## 2. Authentication Endpoints

### POST /auth/login

Authenticate a user against LDAP/Active Directory, falling back to the local break-glass administrator account if the directory is unavailable.

**Permission required:** none (public)

**Request body:**

```json
{
  "username": "jsmith",
  "password": "s3cr3t"
}
```

**Response (200):**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in_minutes": 480,
  "username": "jsmith",
  "roles": ["steward"],
  "permissions": ["data:read", "data:write", "model:read", "staging:approve", "staging:edit", "staging:read", "staging:reject", "audit:read", "pipeline:run"]
}
```

The JWT is simultaneously set as an `HttpOnly` cookie named `mdm_session` (marked `Secure` in production environments). You may use either the cookie or the `Authorization: Bearer` header on subsequent requests.

**curl example:**

```bash
curl -X POST "$BASE/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -c cookies.txt \
  -d '{"username":"jsmith","password":"s3cr3t"}'
```

---

### POST /auth/logout

Clear the session cookie on the client.

**Permission required:** none (public)

**Response (200):**

```json
{"detail": "Signed out."}
```

**curl example:**

```bash
curl -X POST "$BASE/api/v1/auth/logout" -b cookies.txt -c cookies.txt
```

---

### GET /auth/me

Return the calling principal's identity, roles, and effective permissions.

**Permission required:** any authenticated user

**Response (200):** caller's username, roles, source (`ldap`, `local`, `service`), and the full permission list.

**curl example:**

```bash
curl "$BASE/api/v1/auth/me" -b cookies.txt
```

---

### GET /auth/ldap/test

Run a connectivity and bind check against the configured LDAP/AD directory. Returns diagnostic information without authenticating any user.

**Permission required:** `admin` role (via `require_admin`)

**curl example:**

```bash
curl "$BASE/api/v1/auth/ldap/test" \
  -H "Authorization: Bearer $JWT"
```

---

## 3. Data Model Endpoints

Data models define the physical schema of each entity (the column names, types, constraints, and behaviour). Model changes are metadata-only until a `publish` call generates and applies the DDL. The four schemas created per entity are `mdm_landing.<name>`, `mdm_staging.<name>`, `mdm.<name>`, and `mdm_history.<name>`.

### Attribute field reference

| Field | Type | Description |
|---|---|---|
| `name` | string | Physical column name. Must be lower snake_case. |
| `display_name` | string | Human label; defaults to title-cased `name`. |
| `description` | string | Free-text documentation. |
| `data_type` | string | One of the supported types listed below. |
| `length` | integer | For `string` and `enum`; ignored otherwise. |
| `numeric_precision` | integer | For `decimal`. |
| `numeric_scale` | integer | For `decimal`. |
| `is_required` | boolean | Generates `NOT NULL` in the live schema. Default `false`. |
| `is_unique` | boolean | Generates a unique index on the live schema. Default `false`. |
| `is_business_key` | boolean | Used by the upsert pipeline to locate an existing record. |
| `is_match_key` | boolean | Drives duplicate detection during staging. |
| `is_indexed` | boolean | Generates a non-unique index. Default `false`. |
| `is_pii` | boolean | Metadata tag; no DDL effect. Default `false`. |
| `default_value` | string | SQL default for the column. |
| `validation` | object | Rules: `min`, `max`, `min_length`, `max_length`, `regex`, `enum`. |
| `normalization` | array of string | Applied before validation: `trim`, `lower`, `upper`, `title`, `collapse_whitespace`, `strip_punctuation`, `digits_only`, `nullify_empty`. |
| `ref_entity` | string | Reference target entity (metadata only, no FK generated). |
| `ref_attribute` | string | Reference target attribute (metadata only). |
| `position` | integer | Column ordering hint. |

**Supported `data_type` values:**

| Logical type | PostgreSQL DDL | Notes |
|---|---|---|
| `string` | `varchar(n)` | `length` is required for non-default sizes. |
| `text` | `text` | Unbounded. |
| `integer` | `integer` | |
| `bigint` | `bigint` | |
| `decimal` | `numeric(p,s)` | Use `numeric_precision` and `numeric_scale`. |
| `float` | `double precision` | |
| `boolean` | `boolean` | |
| `date` | `date` | |
| `timestamp` | `timestamptz` | |
| `uuid` | `uuid` | |
| `json` | `jsonb` | |
| `email` | `varchar(320)` | Validated as an email address. |
| `url` | `text` | Validated as a URL. |
| `enum` | `varchar(n)` | Provide allowed values in `validation.enum`. |

---

### GET /models

List all entity definitions.

**Permission required:** `model:read`

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `domain` | string | Filter to a specific domain. |
| `status` | string | Filter by entity status: `draft`, `published`, `modified`. |

**Response (200):** array of entity summaries.

```json
[
  {
    "id": "a1b2c3d4-...",
    "name": "customer",
    "display_name": "Customer Master",
    "domain": "party",
    "status": "published",
    "version": 3,
    "attribute_count": 7
  }
]
```

**curl example:**

```bash
curl "$BASE/api/v1/models?domain=party" -b cookies.txt
```

---

### POST /models

Create a new entity definition. The entity is created in `draft` status; no tables are created until you call `publish`.

**Permission required:** `model:write`

**Status code:** `201 Created`

**Request body:**

```json
{
  "name": "customer",
  "display_name": "Customer Master",
  "description": "Core customer entity for the party domain.",
  "domain": "party",
  "requires_approval": true,
  "soft_delete": true,
  "auto_approve_threshold": null,
  "retention_days": null,
  "attributes": [
    {
      "name": "customer_code",
      "data_type": "string",
      "length": 40,
      "is_required": true,
      "is_unique": true,
      "is_business_key": true,
      "normalization": ["trim", "upper"]
    },
    {
      "name": "legal_name",
      "data_type": "string",
      "length": 250,
      "is_required": true,
      "is_match_key": true,
      "normalization": ["trim", "collapse_whitespace"]
    },
    {
      "name": "email",
      "data_type": "email",
      "is_pii": true
    },
    {
      "name": "credit_limit",
      "data_type": "decimal",
      "numeric_precision": 14,
      "numeric_scale": 2,
      "validation": {"min": 0}
    },
    {
      "name": "country",
      "data_type": "enum",
      "length": 2,
      "validation": {"enum": ["AU", "US", "GB", "DE", "NZ"]}
    }
  ]
}
```

Returns the full `EntityOut` object. Validation rejects duplicate entity names (`409`) and empty attribute lists (`422`).

**curl example:**

```bash
curl -X POST "$BASE/api/v1/models" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d @customer-model.json
```

---

### GET /models/{entity_name}

Retrieve the full definition of one entity, including all attributes.

**Permission required:** `model:read`

**Response (200):** full `EntityOut` including all attribute detail, status, version history pointers, and timestamps.

**curl example:**

```bash
curl "$BASE/api/v1/models/customer" -b cookies.txt
```

---

### PUT /models/{entity_name}

Replace an entity's definition. Existing attributes not present in the payload are deleted. New attributes in the payload are added. Existing attributes found in both are updated in place.

This operation updates only the metadata stored in the `mdm_meta` schema. **It does not alter any physical tables.** Call `publish` to apply the DDL.

If the entity was in `published` status, it transitions to `modified` to signal that a publish is pending.

**Permission required:** `model:write`

**Request body:** same shape as `POST /models`.

**curl example:**

```bash
curl -X PUT "$BASE/api/v1/models/customer" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d @customer-model-v2.json
```

---

### GET /models/{entity_name}/ddl

Dry-run preview of exactly what DDL statements publishing this entity would execute. No changes are made. Use this before calling `publish` to understand the impact, particularly for changes that touch existing columns.

**Permission required:** `model:read`

**Response (200):**

```json
{
  "entity": "customer",
  "mode": "alter",
  "sql": "ALTER TABLE mdm.customer ADD COLUMN credit_limit numeric(14,2);\nALTER TABLE mdm_staging.customer ADD COLUMN credit_limit numeric(14,2);",
  "destructive": [],
  "warnings": []
}
```

`mode` is `"create"` for entities that have never been published and `"alter"` for those that have.

**curl example:**

```bash
curl "$BASE/api/v1/models/customer/ddl" -b cookies.txt
```

---

### POST /models/{entity_name}/publish

Generate and apply the DDL for this entity across all four tiers. Requires the `model:publish` permission.

**Permission required:** `model:publish`

**Request body:**

| Field | Type | Default | Description |
|---|---|---|---|
| `dry_run` | boolean | `false` | Return the SQL without executing it. |
| `confirm_destructive` | boolean | `false` | Required when the plan contains column drops or type narrowings. |
| `change_note` | string | `"Published"` | Recorded against the model version. |

If the plan is destructive and `confirm_destructive` is not `true`, the endpoint returns `409` with a detailed description of the destructive changes. Re-submit with `confirm_destructive: true` to proceed.

**Response (200 — live run):**

```json
{
  "entity": "customer",
  "status": "published",
  "statements_executed": 12,
  "warnings": [],
  "tiers": {
    "landing": "mdm_landing.customer",
    "staging": "mdm_staging.customer",
    "live": "mdm.customer",
    "history": "mdm_history.customer"
  }
}
```

**Response (200 — dry run):**

```json
{
  "dry_run": true,
  "entity": "customer",
  "sql": "CREATE TABLE mdm.customer (...);",
  "destructive": [],
  "warnings": []
}
```

**curl example:**

```bash
# Preview first
curl -X POST "$BASE/api/v1/models/customer/publish" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'

# Apply
curl -X POST "$BASE/api/v1/models/customer/publish" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"change_note": "Add credit_limit column"}'

# Apply despite destructive changes
curl -X POST "$BASE/api/v1/models/customer/publish" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"confirm_destructive": true, "change_note": "Widen country to varchar(3)"}'
```

---

### DELETE /models/{entity_name}

Remove an entity's metadata definition. Optionally drop its physical tables.

**Permission required:** `model:drop`

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `drop_tables` | boolean | `false` | Also execute `DROP TABLE ... CASCADE` on all four tiers. |
| `confirm` | boolean | `false` | Required when `drop_tables=true`. |

Attempting `drop_tables=true` without `confirm=true` returns `409`.

**curl example:**

```bash
# Delete metadata only
curl -X DELETE "$BASE/api/v1/models/customer" \
  -H "Authorization: Bearer $JWT"

# Delete metadata and physical tables
curl -X DELETE "$BASE/api/v1/models/customer?drop_tables=true&confirm=true" \
  -H "Authorization: Bearer $JWT"
```

---

### GET /models/{entity_name}/versions

List the model version history for an entity.

**Permission required:** `model:read`

**Query parameters:**

| Parameter | Type | Default / max | Description |
|---|---|---|---|
| `limit` | integer | `50` / `200` | Number of versions to return (most recent first). |

**Response (200):** array of version records, each including `version`, `change_note`, `created_at`, `created_by`, and `has_ddl` (whether a DDL snapshot was recorded for that version).

**curl example:**

```bash
curl "$BASE/api/v1/models/customer/versions?limit=10" -b cookies.txt
```

---

### GET /models/export/all

Export all entity definitions as a JSON or YAML document. The file can be committed to version control and imported back on another environment.

**Permission required:** `model:read`

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `fmt` | string | `json` | `json`, `yaml`, or `yml`. |
| `include_runtime` | boolean | `false` | Include runtime statistics in the export. |

**Response (200):** file download (`application/json` or `application/x-yaml`), `Content-Disposition: attachment; filename="mdm-models.json"`.

**curl example:**

```bash
curl "$BASE/api/v1/models/export/all?fmt=yaml" \
  -b cookies.txt -o mdm-models.yaml
```

---

### GET /models/{entity_name}/export

Export a single entity definition.

**Permission required:** `model:read`

**Query parameters:** `fmt` — same as above.

**curl example:**

```bash
curl "$BASE/api/v1/models/customer/export?fmt=yaml" \
  -b cookies.txt -o customer.yaml
```

---

### POST /models/import

Import entity definitions from a JSON or YAML file. Defaults to a dry run that shows you the diff without writing anything. Importing updates metadata only; call `publish` on each entity afterwards to apply the DDL.

**Permission required:** `model:write`

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `dry_run` | boolean | `true` | Preview the diff without writing. |
| `replace` | boolean | `false` | When `false`, attributes absent from the file are preserved. When `true`, they are removed from existing entities. |

**Request:** `multipart/form-data` with a `file` field containing the JSON or YAML document.

**Response (dry run):**

```json
{
  "dry_run": true,
  "filename": "models.yaml",
  "entities": ["customer", "product"],
  "diff": {
    "customer": {"action": "create"},
    "product": {"action": "update", "added_attributes": ["sku"], "removed_attributes": []}
  },
  "warnings": []
}
```

**Response (live run):**

```json
{
  "dry_run": false,
  "filename": "models.yaml",
  "results": {"customer": "created", "product": "updated"},
  "diff": {...},
  "next_step": "Publish each entity to apply the DDL to the database."
}
```

**curl example:**

```bash
# Dry run
curl -X POST "$BASE/api/v1/models/import?dry_run=true" \
  -b cookies.txt \
  -F "file=@models.yaml"

# Apply
curl -X POST "$BASE/api/v1/models/import?dry_run=false" \
  -b cookies.txt \
  -F "file=@models.yaml"
```

---

### GET /models/cluster/privileges

Preflight check: reports which database grants the DDL role has and which are missing, with the SQL to fix each gap.

**Permission required:** `admin` role

**curl example:**

```bash
curl "$BASE/api/v1/models/cluster/privileges" \
  -H "Authorization: Bearer $JWT"
```

---

### POST /models/cluster/bootstrap

Run the bootstrap sequence: create schemas, metadata tables, and set default privileges.

**Permission required:** `admin` role

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `create_database` | boolean | `false` | Also create the MDM database if it does not exist. Requires the DDL role to have `CREATEDB`. |

**curl example:**

```bash
curl -X POST "$BASE/api/v1/models/cluster/bootstrap" \
  -H "Authorization: Bearer $JWT"
```

---

## 4. Master Data — Write Endpoints

All write endpoints require the entity to be in `published` or `modified` status (i.e., its physical tables exist). An attempt to write to an entity in `draft` status returns `404`.

**All write verbs return `202 Accepted`.** They never modify the `mdm` (golden) schema directly. The record enters `mdm_landing`, is optionally promoted to `mdm_staging` in the same request (controlled by the `AUTO_PROMOTE_LANDING` setting), and waits for steward approval.

### Write response body

Every write endpoint returns the same envelope:

| Field | Type | Description |
|---|---|---|
| `accepted` | boolean | Always `true`. |
| `entity` | string | The entity name. |
| `operation` | string | `INSERT`, `UPDATE`, `UPSERT`, or `DELETE`. |
| `landing_id` | integer | The row ID in `mdm_landing.<entity>`. Persist this for correlation. |
| `deduplicated` | boolean | `true` when the same `idempotency_key` was seen before; no new row was created. |
| `staging_id` | integer or null | The row ID in `mdm_staging.<entity>`, present when `AUTO_PROMOTE_LANDING` is enabled. |
| `validation_passed` | boolean or null | Whether the staged row passed all validation rules. `null` when staging did not run. |
| `validation_errors` | integer or null | Count of validation errors on the staged row. |
| `change_type` | string or null | `"insert"`, `"update"`, or `"delete"` as resolved by the pipeline. |
| `status` | string | `"pending_review"` when the entity requires approval, `"auto"` otherwise. |
| `message` | string | Human-readable description. |

A `validation_passed: false` alongside `accepted: true` is normal: the record was captured and a steward can repair it. Nothing was lost.

### Idempotency

All write verbs except `DELETE` accept an `idempotency_key` query parameter. If a request arrives with the same key as a previously accepted write for the same entity, the original `landing_id` is returned with `deduplicated: true` and no new row is written. This makes it safe to retry on network errors in at-least-once delivery systems.

### Source system tagging

Pass `source_system` as a query parameter to tag the landing row with the originating system name (e.g., `"SAP"`, `"Salesforce"`). For API key authentication, the key's configured `source_system` is used automatically.

---

### POST /data/{entity_name}

Submit a new master-data record for insertion.

**Permission required:** `data:write`

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `idempotency_key` | string | Deduplication key. |
| `source_system` | string | Tag the originating system. |

**Request body:** a JSON object whose keys are the entity's attribute names.

**curl example:**

```bash
curl -X POST "$BASE/api/v1/data/customer?idempotency_key=erp-insert-8891&source_system=SAP" \
  -H "X-API-Key: mdm_TGc2..." \
  -H "Content-Type: application/json" \
  -d '{
    "customer_code": "ACME-01",
    "legal_name": "Acme Pty Ltd",
    "email": "accounts@acme.example.com",
    "credit_limit": "50000.00",
    "country": "AU"
  }'
```

**Response (202):**

```json
{
  "accepted": true,
  "entity": "customer",
  "operation": "INSERT",
  "landing_id": 4821,
  "deduplicated": false,
  "staging_id": 3310,
  "validation_passed": true,
  "validation_errors": 0,
  "change_type": "insert",
  "status": "pending_review",
  "message": "Change accepted into the landing tier and staged for steward review."
}
```

---

### PUT /data/{entity_name}/{record_id}

Full replacement of an existing golden record identified by its `mdm_id` (UUID). All supplied fields replace the existing values.

**Permission required:** `data:write`

**Path parameters:** `record_id` — the UUID of the existing golden record (`mdm_id`).

**Query parameters:** `idempotency_key`, `source_system` (same as POST).

**curl example:**

```bash
curl -X PUT "$BASE/api/v1/data/customer/a3f8c2d1-0011-4abc-bf22-000000000001" \
  -H "X-API-Key: mdm_TGc2..." \
  -H "Content-Type: application/json" \
  -d '{
    "customer_code": "ACME-01",
    "legal_name": "Acme Industries Pty Ltd",
    "email": "accounts@acme.example.com",
    "credit_limit": "75000.00",
    "country": "AU"
  }'
```

---

### PATCH /data/{entity_name}/{record_id}

Partial update: only the fields present in the request body are changed. Fields absent from the payload retain their existing values on the golden record after approval.

**Permission required:** `data:write`

**Path parameters:** `record_id` — UUID of the existing golden record.

**Query parameters:** `idempotency_key`, `source_system`.

**curl example:**

```bash
curl -X PATCH "$BASE/api/v1/data/customer/a3f8c2d1-0011-4abc-bf22-000000000001" \
  -H "X-API-Key: mdm_TGc2..." \
  -H "Content-Type: application/json" \
  -d '{"credit_limit": "100000.00"}'
```

Note: both `PUT` and `PATCH` deposit an `UPDATE` operation into the landing table. The pipeline records which fields were supplied (`mdm_supplied_fields`) so the steward's diff view shows only the changed fields.

---

### PUT /data/{entity_name}

Upsert by business key. The pipeline looks up the target record using the entity's `is_business_key` attributes. If a match is found, an `UPDATE` is staged; if not, an `INSERT` is staged.

This is the recommended entry point for integration feeds where the source system does not know the `mdm_id`.

**Permission required:** `data:write`

**Query parameters:** `idempotency_key`, `source_system`.

**curl example:**

```bash
curl -X PUT "$BASE/api/v1/data/customer" \
  -H "X-API-Key: mdm_TGc2..." \
  -H "Content-Type: application/json" \
  -d '{"customer_code": "ACME-01", "legal_name": "Acme Industries Pty Ltd"}'
```

---

### DELETE /data/{entity_name}/{record_id}

Request deletion of a golden record, subject to steward approval. The record is not removed until a steward approves the staged delete. If the entity has `soft_delete: true`, approval tombstones the record (`mdm_is_deleted = true`) rather than physically removing it.

**Permission required:** `data:write`

**Path parameters:** `record_id` — UUID of the golden record to delete.

**Query parameters:** `source_system`.

**curl example:**

```bash
curl -X DELETE "$BASE/api/v1/data/customer/a3f8c2d1-0011-4abc-bf22-000000000001" \
  -H "X-API-Key: mdm_TGc2..."
```

---

### POST /data/{entity_name}/bulk

Submit multiple records in a single request. All rows share a `batch_id` for tracking. Each row is written to landing independently; a failure on one row does not prevent the others from being recorded.

**Permission required:** `data:write`

**Limit:** the number of records must not exceed `MAX_BULK_ROWS` (default `10000`). Exceeding this returns `413`.

**Request body:**

```json
{
  "operation": "UPSERT",
  "source_system": "SAP",
  "records": [
    {"customer_code": "ACME-01", "legal_name": "Acme Pty Ltd", "country": "AU"},
    {"customer_code": "GLOBEX-02", "legal_name": "Globex Corporation", "country": "US"}
  ]
}
```

`operation` must be one of `INSERT`, `UPDATE`, `UPSERT`, or `DELETE`. When `operation` is `UPDATE` or `DELETE`, supply `mdm_id` in each record object to identify the target.

**Response (202):**

```json
{
  "accepted": true,
  "entity": "customer",
  "batch_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "rows_received": 2,
  "landing_ids": [4822, 4823],
  "staged_valid": 1,
  "staged_invalid": 1,
  "message": "Batch landed. Invalid rows are staged with errors for steward review."
}
```

`landing_ids` is capped at 100 entries in the response even if the batch is larger.

**curl example:**

```bash
curl -X POST "$BASE/api/v1/data/customer/bulk" \
  -H "X-API-Key: mdm_TGc2..." \
  -H "Content-Type: application/json" \
  -d '{
    "operation": "UPSERT",
    "source_system": "SAP",
    "records": [
      {"customer_code": "ACME-01", "legal_name": "Acme Pty Ltd", "country": "AU"},
      {"customer_code": "GLOBEX-02", "legal_name": "Globex Corporation", "country": "US"}
    ]
  }'
```

---

### POST /data/{entity_name}/import-csv

Load records from a CSV file. The first row must be a header row with column names matching the entity's attribute names. An `mdm_id` column is recognised for update/delete operations; any unrecognised columns are ignored and reported in `ignored_columns`.

Processing stops at `MAX_BULK_ROWS` rows; the remaining rows in the file are silently skipped.

**Permission required:** `data:write`

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `operation` | string | `UPSERT` | One of `INSERT`, `UPDATE`, `UPSERT`, `DELETE`. |

**Request:** `multipart/form-data` with a `file` field containing the CSV. UTF-8 BOM is stripped automatically.

**Response (202):**

```json
{
  "accepted": true,
  "filename": "customers.csv",
  "rows_loaded": 150,
  "batch_id": "b1c2d3e4-...",
  "ignored_columns": ["internal_ref"],
  "staged": 150,
  "message": "CSV landed and staged for review."
}
```

**curl example:**

```bash
curl -X POST "$BASE/api/v1/data/customer/import-csv?operation=UPSERT" \
  -H "X-API-Key: mdm_TGc2..." \
  -F "file=@customers.csv"
```

---

## 5. Master Data — Read Endpoints

Read endpoints query the `mdm` (golden/live) schema directly. They are not affected by pending changes in staging. Soft-deleted records are excluded by default.

### GET /data/{entity_name}

List golden records with filtering, free-text search, sorting, and pagination.

**Permission required:** `data:read`

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `limit` | integer | `50` (max `1000`) | Page size. |
| `offset` | integer | `0` | Pagination offset. |
| `q` | string | — | Free-text search across all `string`, `text`, `email`, `url`, and `enum` columns (case-insensitive `ILIKE`). |
| `include_deleted` | boolean | `false` | Include soft-deleted records. |
| `sort` | string | — | Column name to sort by. Can be any attribute name or a `mdm_` system column. |
| `order` | string | `asc` | `asc` or `desc`. |
| `<attribute_name>` | string | — | Any attribute name used as an exact-match filter. Multiple filters are ANDed together. |

**Response (200):**

```json
{
  "meta": {
    "total": 842,
    "limit": 50,
    "offset": 0,
    "has_more": true
  },
  "data": [
    {
      "mdm_id": "a3f8c2d1-0011-4abc-bf22-000000000001",
      "mdm_version": 2,
      "mdm_created_at": "2025-03-12T09:14:22.000Z",
      "mdm_updated_at": "2025-06-01T14:30:00.000Z",
      "mdm_created_by": "jsmith",
      "mdm_updated_by": "mwilson",
      "mdm_is_deleted": false,
      "mdm_source_system": "SAP",
      "customer_code": "ACME-01",
      "legal_name": "Acme Industries Pty Ltd",
      "email": "accounts@acme.example.com",
      "credit_limit": "75000.00",
      "country": "AU"
    }
  ]
}
```

**curl examples:**

```bash
# Paginate with search and filter
curl "$BASE/api/v1/data/customer?q=acme&country=AU&limit=50&sort=legal_name&order=asc" \
  -b cookies.txt

# All records including soft-deleted
curl "$BASE/api/v1/data/customer?include_deleted=true" -b cookies.txt
```

---

### GET /data/{entity_name}/{record_id}

Retrieve a single golden record by its `mdm_id` (UUID).

**Permission required:** `data:read`

**Path parameters:** `record_id` — UUID of the golden record.

**Response (200):** a single record object (same field set as the list response `data` array). Returns `404` if the record does not exist, `422` if the `record_id` is not a valid UUID.

**curl example:**

```bash
curl "$BASE/api/v1/data/customer/a3f8c2d1-0011-4abc-bf22-000000000001" \
  -b cookies.txt
```

---

### GET /data/{entity_name}/{record_id}/history

Retrieve the full version lineage of a single golden record. Each row in the response represents one approved change that was applied to the record.

**Permission required:** `data:read`

**Query parameters:**

| Parameter | Type | Default / max | Description |
|---|---|---|---|
| `limit` | integer | `100` / `500` | Versions returned, most recent first. |

**Response (200):**

```json
{
  "record_id": "a3f8c2d1-0011-4abc-bf22-000000000001",
  "versions": [
    {
      "mdm_history_id": 17,
      "mdm_version": 1,
      "mdm_change_type": "insert",
      "mdm_valid_from": "2025-03-12T09:14:22.000Z",
      "mdm_valid_to": "2025-06-01T14:30:00.000Z",
      "mdm_changed_by": "jsmith",
      "mdm_is_deleted": false,
      "customer_code": "ACME-01",
      "legal_name": "Acme Pty Ltd",
      "email": "accounts@acme.example.com",
      "credit_limit": "50000.00",
      "country": "AU"
    }
  ]
}
```

**curl example:**

```bash
curl "$BASE/api/v1/data/customer/a3f8c2d1-0011-4abc-bf22-000000000001/history" \
  -b cookies.txt
```

---

### GET /data/{entity_name}/statistics

Return record counts across all four tiers. Useful for operational monitoring.

**Permission required:** `data:read`

**Response (200):**

```json
{
  "entity": "customer",
  "statistics": {
    "live_active": 831,
    "live_deleted": 11,
    "history_versions": 1204,
    "landing_pending": 3,
    "landing_total": 9847,
    "staging_by_status": {
      "pending_review": 5,
      "applied": 9820,
      "rejected": 14,
      "changes_requested": 2
    },
    "staging_invalid": 3
  }
}
```

**curl example:**

```bash
curl "$BASE/api/v1/data/customer/statistics" -b cookies.txt
```

---

### GET /data/{entity_name}/export-csv

Stream all active golden records as a CSV file.

**Permission required:** `data:read`

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `include_deleted` | boolean | `false` | Include soft-deleted records. |

**Response (200):** `text/csv` file download, `Content-Disposition: attachment; filename="customer.csv"`. Columns include all `mdm_` system columns followed by the entity's defined attributes.

**curl example:**

```bash
curl "$BASE/api/v1/data/customer/export-csv" -b cookies.txt -o customer.csv
```

---

## 6. Stewardship Endpoints

The stewardship API is the review and approval interface. It operates against the `mdm_staging` schema.

### Staging record fields

Every staging record carries these system metadata columns in addition to the entity's own attributes:

| Column | Description |
|---|---|
| `mdm_staging_id` | Primary key in the staging table. |
| `mdm_landing_id` | The originating landing row. |
| `mdm_operation` | `INSERT`, `UPDATE`, or `DELETE` as submitted. |
| `mdm_target_id` | The `mdm_id` of the existing golden record (for updates/deletes). |
| `mdm_match_key` | The business key value used for upsert resolution. |
| `mdm_source_system` | Origin of the submission. |
| `mdm_status` | Current state: `pending_review`, `changes_requested`, `applied`, `rejected`. |
| `mdm_errors` | JSONB object of field-level validation errors. |
| `mdm_is_valid` | Whether all validation rules passed. An invalid record cannot be approved. |
| `mdm_change_type` | Resolved change type: `insert`, `update`, `delete`. |
| `mdm_submitted_by` | Username or API key name that submitted the write. |
| `mdm_submitted_at` | Timestamp of submission. |
| `mdm_edited_by` | Username of the last steward to edit. |
| `mdm_edited_at` | Timestamp of the last edit. |
| `mdm_reviewed_by` | Username of the approving or rejecting steward. |
| `mdm_reviewed_at` | Timestamp of the review decision. |
| `mdm_review_note` | Optional note recorded at approval. |
| `mdm_supplied_fields` | Array of attribute names included in the original write (for partial updates). |

---

### GET /stewardship/queue

Cross-entity work summary. Returns counts of pending and invalid records for every entity that has items needing attention, sorted by pending count descending. This is the steward's home view.

**Permission required:** `staging:read`

**Response (200):**

```json
{
  "queues": [
    {
      "entity": "customer",
      "display_name": "Customer Master",
      "pending_review": 7,
      "invalid": 3,
      "by_status": {
        "pending_review": 5,
        "changes_requested": 2,
        "applied": 9820
      }
    }
  ],
  "total_pending": 7
}
```

**curl example:**

```bash
curl "$BASE/api/v1/stewardship/queue" -b cookies.txt
```

---

### GET /stewardship/{entity_name}/queue

The review queue for a single entity.

**Permission required:** `staging:read`

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `status` | string | `pending_review` | Filter by `mdm_status`. Use `all` to return all statuses. |
| `only_invalid` | boolean | `false` | Restrict to records with `mdm_is_valid = false`. |
| `limit` | integer | `50` (max `500`) | Page size. |
| `offset` | integer | `0` | Pagination offset. |

**Response (200):** paginated envelope with `meta` and `data` array of full staging records (including all `mdm_` columns and attribute columns), ordered oldest-first.

**curl example:**

```bash
# All pending records
curl "$BASE/api/v1/stewardship/customer/queue?status=pending_review" -b cookies.txt

# Triage: only broken records across all statuses
curl "$BASE/api/v1/stewardship/customer/queue?status=all&only_invalid=true" -b cookies.txt
```

---

### GET /stewardship/{entity_name}/staging/{staging_id}

Retrieve a single staged record alongside the current golden record it would replace, with a field-level diff showing only the fields that changed.

**Permission required:** `staging:read`

**Path parameters:** `staging_id` — integer ID from `mdm_staging_id`.

**Response (200):**

```json
{
  "entity": "customer",
  "staging": {
    "mdm_staging_id": 3310,
    "mdm_is_valid": false,
    "mdm_status": "pending_review",
    "mdm_errors": {"country": "Value 'ZZ' is not in the allowed enum list."},
    "customer_code": "ACME-01",
    "legal_name": "Acme Industries Pty Ltd",
    "country": "ZZ"
  },
  "current_golden_record": {
    "mdm_id": "a3f8c2d1-0011-4abc-bf22-000000000001",
    "mdm_version": 1,
    "customer_code": "ACME-01",
    "legal_name": "Acme Pty Ltd",
    "country": "AU"
  },
  "diff": {
    "legal_name": {"current": "Acme Pty Ltd", "incoming": "Acme Industries Pty Ltd"},
    "country": {"current": "AU", "incoming": "ZZ"}
  },
  "can_approve": false,
  "blocked_reason": "Record has unresolved validation errors — edit it to fix them first."
}
```

For new inserts, `current_golden_record` is `null` and `diff` is empty.

**curl example:**

```bash
curl "$BASE/api/v1/stewardship/customer/staging/3310" -b cookies.txt
```

---

### PATCH /stewardship/{entity_name}/staging/{staging_id}

Correct values on a staged record. The record is immediately re-validated after the edit. If the edit resolves all validation errors, `mdm_is_valid` becomes `true` and the record becomes approvable.

**Permission required:** `staging:edit`

**Request body:**

```json
{
  "updates": {
    "country": "AU",
    "credit_limit": "25000.00"
  }
}
```

**Response (200):** the updated staging record with refreshed validation state.

Editing records the steward's username in `mdm_edited_by`. This is tracked for segregation-of-duties purposes: if `ENFORCE_SEGREGATION_OF_DUTIES` is enabled, a steward who edits a record is treated as having touched it and cannot approve it.

**curl example:**

```bash
curl -X PATCH "$BASE/api/v1/stewardship/customer/staging/3310" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"updates": {"country": "AU", "credit_limit": "25000.00"}}'
```

---

### POST /stewardship/{entity_name}/staging/{staging_id}/approve

Approve a staged change and apply it to the golden record. The record transitions from `mdm_staging` to `mdm` (the live table), and the prior version of the golden record is written to `mdm_history`.

**Permission required:** `staging:approve`

**Segregation of duties:** if `ENFORCE_SEGREGATION_OF_DUTIES` is enabled (the default), the approving user must not be the same person who submitted the original write (`mdm_submitted_by`) or last edited the staged record (`mdm_edited_by`). Attempting to approve your own submission returns `403`.

**Request body (optional):**

```json
{"note": "Verified against SAP master data export 2025-07-01"}
```

**Response (200):** details of the applied change including the `mdm_id` of the created or updated golden record.

**curl example:**

```bash
curl -X POST "$BASE/api/v1/stewardship/customer/staging/3310/approve" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"note": "Verified against SAP export"}'
```

---

### POST /stewardship/{entity_name}/staging/{staging_id}/reject

Reject a staged change. The record's `mdm_status` is set to `rejected`. A reason is required.

**Permission required:** `staging:reject`

**Request body:**

```json
{"reason": "Duplicate of existing record C-0042. Merge manually."}
```

**curl example:**

```bash
curl -X POST "$BASE/api/v1/stewardship/customer/staging/3310/reject" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"reason": "Duplicate of C-0042"}'
```

---

### POST /stewardship/{entity_name}/staging/bulk-approve

Approve multiple staged records in one call. Each record is applied in its own transaction, so one failure does not abort the rest.

**Permission required:** `staging:approve`

Segregation-of-duties constraints apply to each record individually. A record the caller submitted will appear in the `errors` list, not abort the batch.

**Request body:**

```json
{
  "staging_ids": [3310, 3311, 3312],
  "note": "Bulk approval — verified against Q2 data extract"
}
```

**Response (200):**

```json
{
  "approved": 2,
  "failed": 1,
  "results": [...],
  "errors": [
    {"staging_id": 3311, "error": "Segregation of duties: you submitted this record."}
  ]
}
```

**curl example:**

```bash
curl -X POST "$BASE/api/v1/stewardship/customer/staging/bulk-approve" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"staging_ids": [3310, 3311, 3312], "note": "Batch verified"}'
```

---

### POST /stewardship/{entity_name}/staging/bulk-reject

Reject multiple staged records. Each rejection is independent; failures do not abort the batch. A reason must be provided via the `note` field (treated as the rejection reason) or a default reason of `"Bulk rejected"` is used.

**Permission required:** `staging:reject`

**Request body:**

```json
{
  "staging_ids": [3313, 3314],
  "note": "Source data quality issue — resubmit after cleansing"
}
```

**curl example:**

```bash
curl -X POST "$BASE/api/v1/stewardship/customer/staging/bulk-reject" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"staging_ids": [3313, 3314], "note": "Source data quality issue"}'
```

---

### POST /stewardship/{entity_name}/promote

Manually run the landing-to-staging promotion pass for an entity. Use this when `AUTO_PROMOTE_LANDING` is disabled (e.g., you are running promotion from a scheduler to avoid inline latency on high-volume feeds).

**Permission required:** `pipeline:run`

**Query parameters:**

| Parameter | Type | Default / max | Description |
|---|---|---|---|
| `limit` | integer | `1000` / `10000` | Maximum landing rows to process in this pass. |

**curl example:**

```bash
curl -X POST "$BASE/api/v1/stewardship/customer/promote?limit=5000" \
  -H "Authorization: Bearer $JWT"
```

---

### GET /stewardship/{entity_name}/landing

Inspect the raw landing tier. This is the immutable audit trail of everything that was sent to the write API, regardless of whether it later validated or was approved.

**Permission required:** `staging:read`

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `status` | string | Filter by `mdm_status` (e.g., `pending`, `promoted`, `deduplicated`). |
| `limit` | integer | `50` (max `500`). |
| `offset` | integer | Pagination offset. |

**Response (200):** paginated envelope with raw landing rows, ordered by `mdm_landing_id` descending.

**curl example:**

```bash
curl "$BASE/api/v1/stewardship/customer/landing?status=pending" -b cookies.txt
```

---

### GET /stewardship/batches

List promotion batches (the records of each landing-to-staging pass). Useful for monitoring bulk loads.

**Permission required:** `staging:read`

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `entity_name` | string | Filter to a specific entity. |
| `limit` | integer | `50` (max `200`). |

**Response (200):** array of batch records with `id`, `entity`, `stage`, `status`, `rows_in`, `rows_ok`, `rows_failed`, `started_at`, `finished_at`, `triggered_by`, and `error`.

**curl example:**

```bash
curl "$BASE/api/v1/stewardship/batches?entity_name=customer" -b cookies.txt
```

---

## 7. Administration Endpoints

### Users

#### GET /admin/users

List all user accounts.

**Permission required:** `user:manage`

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `source` | string | Filter by `ldap`, `local`, or `service`. |

**Response (200):** array of user objects including `id`, `username`, `email`, `display_name`, `source`, `roles`, `is_active`, `last_login_at`, `dn` (for LDAP users), and `entity_permissions`.

**curl example:**

```bash
curl "$BASE/api/v1/admin/users?source=ldap" \
  -H "Authorization: Bearer $JWT"
```

---

#### PUT /admin/users/{username}/roles

Override a user's roles. Valid role values are `admin`, `steward`, `reader`, `service`.

**Permission required:** `user:manage`

**Important for LDAP users:** roles are recomputed from directory group memberships at each login. A role override applied here will be overwritten on the user's next login unless the underlying group mapping is also changed.

**Request body:** a JSON array of role strings.

```json
["steward"]
```

**Response (200):**

```json
{
  "username": "jsmith",
  "roles": ["steward"],
  "note": "LDAP users have roles refreshed from directory groups on next login."
}
```

**curl example:**

```bash
curl -X PUT "$BASE/api/v1/admin/users/jsmith/roles" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '["steward"]'
```

---

#### PUT /admin/users/{username}/status

Activate or deactivate a user account. An administrator cannot deactivate their own account.

**Permission required:** `user:manage`

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `is_active` | boolean | `true` to activate, `false` to deactivate. |

**curl example:**

```bash
curl -X PUT "$BASE/api/v1/admin/users/jsmith/status?is_active=false" \
  -H "Authorization: Bearer $JWT"
```

---

#### GET /admin/roles

List all roles with their associated permissions and descriptions.

**Permission required:** `user:manage`

**Response (200):**

```json
{
  "roles": [
    {
      "name": "admin",
      "permissions": ["apikey:manage", "audit:read", "data:read", "data:write", "model:drop", "model:publish", "model:read", "model:write", "pipeline:run", "settings:manage", "staging:approve", "staging:edit", "staging:read", "staging:reject", "user:manage"],
      "description": "Full control: model design, DDL publishing, users, settings."
    },
    {
      "name": "steward",
      "permissions": ["audit:read", "data:read", "data:write", "model:read", "pipeline:run", "staging:approve", "staging:edit", "staging:read", "staging:reject"],
      "description": "Reviews, edits and approves staged data. Cannot change models."
    },
    {
      "name": "reader",
      "permissions": ["data:read", "model:read", "staging:read"],
      "description": "Read-only access to golden records."
    },
    {
      "name": "service",
      "permissions": ["data:write", "model:read"],
      "description": "Machine account. Writes into landing only; cannot approve."
    }
  ]
}
```

---

### LDAP Group Mappings

Group mappings define how LDAP/AD directory groups are translated into MDM roles. A user who authenticates via LDAP but matches no mapping is denied access.

#### GET /admin/ldap/group-mappings

List all configured group-to-role mappings.

**Permission required:** `admin` role

**curl example:**

```bash
curl "$BASE/api/v1/admin/ldap/group-mappings" \
  -H "Authorization: Bearer $JWT"
```

---

#### POST /admin/ldap/group-mappings

Create a new group-to-role mapping.

**Permission required:** `admin` role

**Status code:** `201 Created`

**Request body:**

```json
{
  "group_dn": "CN=MDM-Stewards,OU=Groups,DC=corp,DC=example,DC=com",
  "role": "steward",
  "description": "Active Directory group for MDM data stewards"
}
```

Returns `409` if the same `group_dn` + `role` combination already exists.

**curl example:**

```bash
curl -X POST "$BASE/api/v1/admin/ldap/group-mappings" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "group_dn": "CN=MDM-Stewards,OU=Groups,DC=corp,DC=example,DC=com",
    "role": "steward",
    "description": "MDM data steward group"
  }'
```

---

#### DELETE /admin/ldap/group-mappings/{mapping_id}

Remove a group-to-role mapping. Users currently in the mapped group retain their session roles until their next login.

**Permission required:** `admin` role

**curl example:**

```bash
curl -X DELETE "$BASE/api/v1/admin/ldap/group-mappings/f47ac10b-58cc-4372-a567-0e02b2c3d479" \
  -H "Authorization: Bearer $JWT"
```

---

### API Keys

Service API keys allow automated systems to authenticate and write master data without a user session. A key holds the `service` role: it can write into landing and read model definitions, but it cannot approve staged changes.

#### GET /admin/api-keys

List all API keys. The actual key value is never returned after creation; only the prefix and metadata are shown.

**Permission required:** `apikey:manage`

**curl example:**

```bash
curl "$BASE/api/v1/admin/api-keys" \
  -H "Authorization: Bearer $JWT"
```

---

#### POST /admin/api-keys

Issue a new API key. **The key value is returned exactly once.** Store it immediately; it cannot be retrieved again.

**Permission required:** `apikey:manage`

**Status code:** `201 Created`

**Request body:**

```json
{
  "name": "sap-erp-integration",
  "source_system": "SAP ERP",
  "allowed_entities": ["customer", "product"],
  "expires_at": "2026-12-31T23:59:59Z"
}
```

`allowed_entities`: if non-empty, the key can only write to the listed entity names. Attempts to access other entities return `403`. If the list is empty, the key is unrestricted.

`expires_at`: optional ISO 8601 datetime. Omit for a non-expiring key.

**Response (201):**

```json
{
  "id": "b1c2d3e4-...",
  "name": "sap-erp-integration",
  "api_key": "mdm_TGc2Kv9mXpQrN8sLzJwY4hFbDuAeO3iM7cVtgR6nk",
  "key_prefix": "mdm_TGc2Kv9m",
  "warning": "Store this key now — it cannot be retrieved again.",
  "usage": "Send it as the X-API-Key header on write requests."
}
```

**curl example:**

```bash
curl -X POST "$BASE/api/v1/admin/api-keys" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "sap-erp-integration",
    "source_system": "SAP ERP",
    "allowed_entities": ["customer"],
    "expires_at": "2026-12-31T23:59:59Z"
  }'
```

---

#### DELETE /admin/api-keys/{key_id}

Revoke an API key. Revocation is immediate: subsequent requests using the key return `401`. The key record is retained in the database for audit purposes; it is deactivated, not deleted.

**Permission required:** `apikey:manage`

**curl example:**

```bash
curl -X DELETE "$BASE/api/v1/admin/api-keys/b1c2d3e4-..." \
  -H "Authorization: Bearer $JWT"
```

---

### Audit Log

#### GET /admin/audit

Query the audit log. All consequential actions — logins, model changes, writes, approvals, rejections, user management, key issuance — are recorded here.

**Permission required:** `audit:read`

**Query parameters:**

| Parameter | Type | Description |
|---|---|---|
| `entity_name` | string | Filter to events for a specific entity. |
| `actor` | string | Filter to events by a specific username. |
| `action` | string | Filter by action type (e.g., `api_insert`, `staging_approve`, `model_publish`). |
| `record_id` | string | Filter to events referencing a specific record ID. |
| `limit` | integer | `100` (max `1000`). |
| `offset` | integer | Pagination offset. |

**Response (200):** paginated envelope. Each event includes `id`, `occurred_at`, `actor`, `actor_roles`, `action`, `entity_name`, `record_id`, `tier`, `detail`, `success`, `before_value`, `after_value`, and `ip_address`.

**curl example:**

```bash
curl "$BASE/api/v1/admin/audit?entity_name=customer&actor=jsmith&limit=50" \
  -H "Authorization: Bearer $JWT"
```

---

### System

#### GET /admin/system

Return operational configuration and database connectivity status.

**Permission required:** `admin` role

**Response (200):**

```json
{
  "database": {"connected": true, "latency_ms": 1.4},
  "schemas": ["mdm_meta", "mdm_landing", "mdm_staging", "mdm", "mdm_history"],
  "ldap_enabled": true,
  "ldap_server": "ldaps://dc01.corp.example.com:636",
  "segregation_of_duties": true,
  "auto_promote_landing": true,
  "soft_delete": true,
  "environment": "production"
}
```

**curl example:**

```bash
curl "$BASE/api/v1/admin/system" \
  -H "Authorization: Bearer $JWT"
```

---

## 8. Permission Matrix

The table below maps each endpoint group to the permissions required. Permissions are granted to roles as shown in the second table.

### Endpoints by permission

| Permission | Endpoints |
|---|---|
| `model:read` | `GET /models`, `GET /models/{name}`, `GET /models/{name}/ddl`, `GET /models/{name}/versions`, `GET /models/export/all`, `GET /models/{name}/export` |
| `model:write` | `POST /models`, `PUT /models/{name}`, `POST /models/import` |
| `model:publish` | `POST /models/{name}/publish` |
| `model:drop` | `DELETE /models/{name}` |
| `data:read` | `GET /data/{name}`, `GET /data/{name}/{id}`, `GET /data/{name}/{id}/history`, `GET /data/{name}/statistics`, `GET /data/{name}/export-csv` |
| `data:write` | `POST /data/{name}`, `PUT /data/{name}/{id}`, `PATCH /data/{name}/{id}`, `PUT /data/{name}` (upsert), `DELETE /data/{name}/{id}`, `POST /data/{name}/bulk`, `POST /data/{name}/import-csv` |
| `staging:read` | `GET /stewardship/queue`, `GET /stewardship/{name}/queue`, `GET /stewardship/{name}/staging/{id}`, `GET /stewardship/{name}/landing`, `GET /stewardship/batches` |
| `staging:edit` | `PATCH /stewardship/{name}/staging/{id}` |
| `staging:approve` | `POST /stewardship/{name}/staging/{id}/approve`, `POST /stewardship/{name}/staging/bulk-approve` |
| `staging:reject` | `POST /stewardship/{name}/staging/{id}/reject`, `POST /stewardship/{name}/staging/bulk-reject` |
| `pipeline:run` | `POST /stewardship/{name}/promote` |
| `user:manage` | `GET /admin/users`, `PUT /admin/users/{u}/roles`, `PUT /admin/users/{u}/status`, `GET /admin/roles` |
| `apikey:manage` | `GET /admin/api-keys`, `POST /admin/api-keys`, `DELETE /admin/api-keys/{id}` |
| `audit:read` | `GET /admin/audit` |
| `admin` role only | `GET /auth/ldap/test`, `GET /admin/ldap/group-mappings`, `POST /admin/ldap/group-mappings`, `DELETE /admin/ldap/group-mappings/{id}`, `GET /admin/system`, `GET /models/cluster/privileges`, `POST /models/cluster/bootstrap` |

### Permissions by role

| Permission | admin | steward | reader | service |
|---|---|---|---|---|
| `model:read` | yes | yes | yes | yes |
| `model:write` | yes | — | — | — |
| `model:publish` | yes | — | — | — |
| `model:drop` | yes | — | — | — |
| `data:read` | yes | yes | yes | — |
| `data:write` | yes | yes | — | yes |
| `staging:read` | yes | yes | yes | — |
| `staging:edit` | yes | yes | — | — |
| `staging:approve` | yes | yes | — | **never** |
| `staging:reject` | yes | yes | — | — |
| `pipeline:run` | yes | yes | — | — |
| `audit:read` | yes | yes | — | — |
| `user:manage` | yes | — | — | — |
| `apikey:manage` | yes | — | — | — |
| `settings:manage` | yes | — | — | — |

The `service` role cannot hold `staging:approve` under any circumstances. This is enforced in the PERMISSIONS constant and is the basis for the segregation guarantee: a machine credential can write data in but cannot approve it.

---

## 9. End-to-End Worked Example

This walkthrough traces a record from first contact with the API through golden-record creation and history retrieval.

### Step 1: Issue an API key (admin, once per integration)

An administrator issues a key scoped to the `customer` entity for the source system.

```bash
curl -X POST "$BASE/api/v1/admin/api-keys" \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "sap-erp-integration",
    "source_system": "SAP ERP",
    "allowed_entities": ["customer"]
  }'
```

Response (201) — store the `api_key` value immediately:

```json
{
  "id": "b1c2d3e4-0000-4000-a000-000000000001",
  "name": "sap-erp-integration",
  "api_key": "mdm_TGc2Kv9mXpQrN8sLzJwY4hFbDuAeO3iM7cVtgR6nk",
  "key_prefix": "mdm_TGc2Kv9m",
  "warning": "Store this key now — it cannot be retrieved again.",
  "usage": "Send it as the X-API-Key header on write requests."
}
```

```bash
# Retain for all subsequent source-system calls
KEY="mdm_TGc2Kv9mXpQrN8sLzJwY4hFbDuAeO3iM7cVtgR6nk"
```

---

### Step 2: Submit a new record (source system)

The SAP integration pushes a customer record. It passes an idempotency key so retries are safe.

```bash
curl -X POST "$BASE/api/v1/data/customer?idempotency_key=sap-cust-88412&source_system=SAP+ERP" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "customer_code": "ACME-01",
    "legal_name": "Acme Pty Ltd",
    "email": "accounts@acme.example.com",
    "credit_limit": "50000.00",
    "country": "ZZ"
  }'
```

Response (202) — note `validation_passed: false` because `"ZZ"` is not in the allowed enum. The record was still captured:

```json
{
  "accepted": true,
  "entity": "customer",
  "operation": "INSERT",
  "landing_id": 4821,
  "deduplicated": false,
  "staging_id": 3310,
  "validation_passed": false,
  "validation_errors": 1,
  "change_type": "insert",
  "status": "pending_review",
  "message": "Change accepted into the landing tier and staged for steward review."
}
```

---

### Step 3: Steward opens the queue

A data steward signs in and sees the pending work.

```bash
# Log in and get session cookie
curl -X POST "$BASE/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -c steward-cookies.txt \
  -d '{"username": "mwilson", "password": "..."}'

# Check the global queue
curl "$BASE/api/v1/stewardship/queue" -b steward-cookies.txt
```

Response shows `customer` has 1 pending, 1 invalid.

---

### Step 4: Steward reviews the staging detail and corrects the error

```bash
curl "$BASE/api/v1/stewardship/customer/staging/3310" -b steward-cookies.txt
```

The response shows `diff` with `country: {current: null, incoming: "ZZ"}` and `can_approve: false`. The steward corrects the value:

```bash
curl -X PATCH "$BASE/api/v1/stewardship/customer/staging/3310" \
  -b steward-cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"updates": {"country": "AU"}}'
```

The response shows `mdm_is_valid: true` and an empty `mdm_errors` object. The record is now approvable — but `mwilson` cannot approve it because they have edited it (segregation of duties).

---

### Step 5: A second steward approves

A second steward, `plee`, reviews and approves.

```bash
# plee signs in
curl -X POST "$BASE/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -c plee-cookies.txt \
  -d '{"username": "plee", "password": "..."}'

# Approve
curl -X POST "$BASE/api/v1/stewardship/customer/staging/3310/approve" \
  -b plee-cookies.txt \
  -H "Content-Type: application/json" \
  -d '{"note": "Country corrected to AU — verified against SAP master"}'
```

Response confirms the record was applied:

```json
{
  "mdm_id": "a3f8c2d1-0011-4abc-bf22-000000000001",
  "mdm_version": 1,
  "change_type": "insert",
  "reviewed_by": "plee",
  "review_note": "Country corrected to AU — verified against SAP master"
}
```

---

### Step 6: Read the golden record

The record is now live in the `mdm.customer` table and visible to all readers.

```bash
curl "$BASE/api/v1/data/customer/a3f8c2d1-0011-4abc-bf22-000000000001" \
  -b steward-cookies.txt
```

```json
{
  "mdm_id": "a3f8c2d1-0011-4abc-bf22-000000000001",
  "mdm_version": 1,
  "mdm_created_at": "2025-07-25T10:14:22.000Z",
  "mdm_updated_at": "2025-07-25T10:14:22.000Z",
  "mdm_created_by": "plee",
  "mdm_updated_by": "plee",
  "mdm_is_deleted": false,
  "mdm_source_system": "SAP ERP",
  "customer_code": "ACME-01",
  "legal_name": "Acme Pty Ltd",
  "email": "accounts@acme.example.com",
  "credit_limit": "50000.00",
  "country": "AU"
}
```

---

### Step 7: Read the record's history

Because this is the first version, history has one entry — the initial insert as it existed before any future updates.

```bash
curl "$BASE/api/v1/data/customer/a3f8c2d1-0011-4abc-bf22-000000000001/history" \
  -b steward-cookies.txt
```

```json
{
  "record_id": "a3f8c2d1-0011-4abc-bf22-000000000001",
  "versions": [
    {
      "mdm_history_id": 1,
      "mdm_version": 1,
      "mdm_change_type": "insert",
      "mdm_valid_from": "2025-07-25T10:14:22.000Z",
      "mdm_valid_to": null,
      "mdm_changed_by": "plee",
      "mdm_is_deleted": false,
      "customer_code": "ACME-01",
      "legal_name": "Acme Pty Ltd",
      "email": "accounts@acme.example.com",
      "credit_limit": "50000.00",
      "country": "AU"
    }
  ]
}
```

Future approved updates to this record will add further rows to history, each effective-dated by `mdm_valid_from` and `mdm_valid_to`, providing full lineage from first insert through every change.
