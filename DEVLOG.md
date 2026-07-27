# DEVLOG

Running record of every change made while closing the gap between the repo and
`mdm_platform_functional_requirements.md` (39 requirements, 10 categories, UC-1…4).

**Purpose:** this file is the source of truth for in-flight work. If the session is
`/clear`-ed, resume from the last unchecked item below. Each entry records what
changed, why, and its verification status.

---

## Approved plan & decisions (2026-07-27)

Gap analysis produced against the requirements doc. Categories 1 (ingestion),
plus AC-2, UI-4, DD-3, AO-3, DQ-1/4/7 already satisfied. Everything else is
partial or absent — built out in 9 workstreams below.

**Decisions locked with the user:**
1. **Execution:** phased with checkpoints — build → review → test one workstream
   at a time, log each here.
2. **Relationships (DM-2/DQ-2/DQ-5):** soft-resolve display→internal id in the
   pipeline; hold child in staging as `broken_reference` until parent exists;
   generate real FK constraints **only on the live/golden tier**. Landing &
   staging stay constraint-free to preserve "landing never rejects".
3. **Notifications (NT):** pluggable notification service — SMTP transport for
   real envs + console/in-app **outbox** transport for local testing, selected
   by config. Per-domain templates + deep-links.
4. **Scheduler (DD-2):** lightweight in-process (APScheduler-style) job runner,
   opt-in via config, for materialized-view refresh + retention.

**Baseline (2026-07-27):** portable Postgres 16.11 at `~/pgsql16`, venv at
`.venv/Scripts`. `pytest` = **267 passed**. App boots, DB connects as `mdm_ddl`.

---

## Workstreams

- [x] **W1 — Relationships & Reference Data** (DM-2, DM-3, DM-4, DM-5, DQ-2, DQ-5) ✅ 2026-07-27
- [x] **W2 — Roles, Domains & Access** (AC-1, AC-3, AC-4, DM-1, UI-2) ✅ 2026-07-27
- [x] **W3 — Governed Workflow** (GC-1…7, AO-2) ✅ 2026-07-27
- [x] **W4 — Notifications** (NT-1/2/3) ✅ 2026-07-27
- [x] **W5 — Downstream Distribution + Scheduler** (DD-1, DD-2) ✅ 2026-07-27
- [x] **W6 — Extensibility hooks** (EX-1, EX-2, EX-3) ✅ 2026-07-27
- [x] **W7 — Observability** (AO-1) ✅ 2026-07-27
- [x] **W8 — UI wiring** (UI-1, UI-3, UI-5 + UI for W1–W5) ✅ 2026-07-27
- [x] **W9 — Cleanup, example models & data, run** (final) ✅ 2026-07-27

---

## Change log

### 2026-07-27 — Session start
- Read `mdm_platform_functional_requirements.md`; produced verified gap analysis
  across all 39 requirements (4 parallel Explore agents mapped workflow, auth,
  DDL, UI surfaces).
- Established baseline: started Postgres, confirmed connectivity, ran full suite
  (**271 passed** — corrected from an initial miscount of 267).
- Created this DEVLOG and the 9-workstream plan above.

### 2026-07-27 — W1 Relationships & Reference Data ✅ (271 → 299 passed)
New capability: cross-entity references, junction/M2M, DB-enforced enums, and
pipeline reference resolution — all consistent with capture-first.

**New data type `reference`** — physically `uuid`, stores the resolved parent
`mdm_id`; requires `ref_entity` (+ optional `ref_attribute`, defaults to the
parent's first business key).

**Files changed:**
- `app/services/identifiers.py` — `reference`→uuid type; `quote_literal()` for
  safe enum-CHECK literals; `reference`↔`uuid` widening family.
- `app/services/ddl.py` — **FK on live tier only** via `ALTER … ADD CONSTRAINT`
  (not inline, since a child may publish before its parent); **enum→CHECK on
  live only**; `reconcile_reference_constraints()` (adds outbound + inbound FKs
  when tables appear, savepoint-isolated, degrades to warnings — never hard-fails
  publish); `reconcile_enum_constraints()` (drops+re-adds a changed/removed enum
  CHECK on re-publish); FK re-point detection (drop+re-add when `ref_entity`
  changes); collision-safe constraint names (`hashlib.sha1` suffix); composite
  partial UNIQUE index for `kind='association'` junctions.
- `app/services/references.py` *(new)* — `resolve_references()` (DQ-2: human
  value → parent `mdm_id`, exact-one-match, else soft `broken_reference`) and
  `reresolve_broken_references()` (DQ-5: unblocks held children when the parent
  arrives; recomputes match key; batch-cached to avoid N+1).
- `app/services/pipeline.py` — resolution wired into `promote_landing_to_staging`
  (after validate, before staging insert) and `edit_staging`; bounded
  re-resolution runs at the end of each promotion so new parents auto-unblock.
- `app/api/v1/models_api.py` — publish path runs reference + enum reconciliation
  (DDL engine, best-effort).
- `app/api/v1/stewardship.py` — `POST /{entity}/reresolve` (`pipeline:run`).
- `app/models/meta.py` + `app/services/bootstrap.py` — `Entity.kind`
  (master|reference|association) with idempotent `ADD COLUMN IF NOT EXISTS`.
- `app/schemas/models.py` — `kind` + `reference` validation (must have
  `ref_entity`); `app/services/model_io.py` — round-trips + validates `kind`.
- `tests/test_references.py` — 28 new tests (FK/CHECK live-only, resolution,
  broken_reference hold, re-resolution unblock, junction FKs + composite unique,
  enum & FK reconcile on re-publish).

**Reviewed** (adversarial): no blockers; injection defence, capture-first,
DQ-5 hold/unblock, reconcile resilience all confirmed. 2 majors found
(add-only reconcile of changed enum / changed ref_entity) — **fixed**.

**Known/accepted limitations:** `ON DELETE RESTRICT` is inert under soft-delete
(only bites on hard/retention purge); a reference to an entity whose table is
*not published at all* lands as a row `error` rather than a soft
`broken_reference` (published-but-empty parent — the real DQ-5 case — is held
correctly). `mdm_target_id` is not recomputed on unblock (match key is).

### 2026-07-27 — W2 Roles, Domains & Access ✅ (299 → 344 passed)
Five-tier RBAC, first-class domains, per-domain **conferral** grants, service
elevation. Fully backward compatible (existing 4 roles + global perms unchanged;
empty permission maps ⇒ prior behavior).

**Roles (added, existing kept):** `editor` (workflow write, **cannot** approve),
`approver` (review/approve, **no** data write), `power_user` (editor +
`data:write_direct`). New permission `data:write_direct`.

**Power-user direct edit (bypasses steward review):** `?direct=true` on the data
write verbs. Runs landing→staging→`apply_staging_to_live(enforce_sod=False)` — an
authorized auto-approve *through the pipeline apply function* (never a hand-rolled
live write). Landing is committed FIRST and apply runs in a separate transaction,
so an apply-time error still preserves the captured payload + audit and returns
202 `captured_pending_review` (capture-first upheld). Invalid rows never applied.
403 without `data:write_direct`; service/elevated keys can never use it.

**Domains (DM-1):** new `mdm_meta.domain` table (name, display_name, lifecycle
defaults: requires_approval / retention_days / soft_delete). Entities inherit
domain defaults for unset fields. Admin CRUD `GET/POST/PUT/DELETE /domains`
(`settings:manage`); `GET /domains` for any authenticated user. A domain is a
LOGICAL governance/access boundary within the shared four tier-schemas (no
per-domain Postgres schema — that would break the tier model).

**Per-domain access (AC-3):** `User.domain_roles` `{domain: [role,...]}` CONFERS
those roles' permissions for entities in that domain only — so "editor in finance,
nothing elsewhere" is expressible. Effective perms = global roles ∪ domain_roles
for the entity's domain. New `require_entity_permission(permission, action)` gate
on data + stewardship routes (correct action vocab: approve→`staging:approve`,
etc.). `entity_permissions`/`domain_permissions` remain the read/write-grained
RESTRICTION layer (precedence: entity → domain → global). LDAP
`GroupRoleMapping.domain` feeds `domain_roles` (null = global, unchanged).

**Service elevation (AC-4):** `ApiKey.elevated` + `allowed_domains`; an elevated
key gets cross-domain **write** reach (never approve). Null/blank entity domain
normalized to the seeded `default` domain for predictable scoping. Warning logged
for elevated keys with empty `allowed_domains`.

**Files:** `meta.py` (roles, Domain, domain_roles, ApiKey.elevated/allowed_domains,
GroupRoleMapping.domain), `auth.py` (effective_permissions, conferral,
can_access_entity precedence, elevation), `deps.py` (require_entity_permission,
principal_context), `data.py` (gate + direct path + capture-first rewrite),
`stewardship.py` (gate), `admin.py` (domain CRUD + user permissions), `ldap_auth.py`
(domain-scoped grants), `models_api.py` (domain lifecycle inheritance),
`bootstrap.py` (idempotent migrations + seed `default` domain), `schemas/models.py`.
`tests/test_authz_domains.py` — 45 new tests incl. HTTP conferral + capture-first.

**Reviewed** (adversarial, security-focused): all "must-be-NO" invariants
(editor/approver/service cannot approve or bypass; direct path walled & pipeline-
only; empty maps = prior behavior) CONFIRMED SAFE. 2 majors found (domain grants
only restricted / action-vocab mismatch; direct path lost capture on apply error)
— **both fixed** with HTTP-level tests.

### 2026-07-27 — W3 Governed Workflow ✅ (344 → 367 passed)
Maker-checker workflow modeled as **fixed `mdm_meta` ORM tables** (generated
staging tables untouched):
- `WorkflowTask` — one per staging change request: status
  (`pending_review|changes_requested|rejected|applied|terminated`), submitter,
  `submit_rationale`, `assigned_to`/`claimed_by`, domain, priority.
- `WorkflowEvent` — immutable, ordered decision chain (`seq` per task): step,
  actor, comment, from/to status, ip_address.

**GC-1** multi-step: submission carries a rationale (threaded through the write
verbs `?rationale=` → landing → staging → task); `request_changes` sends a task
back with a mandatory comment (`changes_requested`), editor's next edit returns it
to `pending_review` (loop now enforced — a `changes_requested` row can't be
approved until resubmitted). **GC-3** reject/terminate = zero golden impact,
terminal, excluded from queues, chain retained. **GC-4** `REQUIRE_REVIEW_COMMENTS`
(default true) makes approve/reject/request_changes require a non-empty comment;
`REQUIRE_SUBMIT_RATIONALE` (default false). **GC-5** `GET
/stewardship/{entity}/staging/{id}/workflow` returns the full coherent decision
chain. **GC-6** admin `GET /admin/workflows` (active tasks + age), `.../terminate`,
`.../reassign`. **GC-7** `GET /stewardship/inbox` + `/inbox/counts` (badge:
assigned_to_me / unassigned / changes_requested / total_pending), `claim`/`release`
(double-claim → 409), domain-scoped to what the reviewer can access (W2 conferral).
**AO-2** DB-level `BEFORE UPDATE OR DELETE` triggers make `audit_event` +
`workflow_event` append-only (even the app can't mutate history); every transition
appends an event with actor/comment/from-to/ip.

**Files:** `models/meta.py` (+2 tables), `services/workflow.py` *(new — state
machine + Core writers)*, `pipeline.py` (task sync on the same `conn` as tier
writes + `FOR UPDATE` locks), `stewardship.py` / `admin.py` / `data.py` (endpoints
+ rationale/ip threading), `bootstrap.py` (tables + append-only triggers),
`config.py` (2 settings), `schemas/models.py`, `models_api.py` (delete_entity
terminates the entity's tasks). `tests/test_workflow.py` — 23 tests.

**Reviewed** (adversarial): no blocker; state machine, terminal-state safety,
SoD, append-only triggers, domain-scoped inbox, and the GC-4 test edits all
CONFIRMED correct. 3 majors found (entity-drop orphaned tasks; task/staging
committed in separate transactions → divergence; double-approve race + seq→500)
— **all fixed**: governance state now commits atomically on the same `conn` as
tier writes, staging rows are `FOR UPDATE`-locked in decisions, entity delete
terminates its tasks, seq collisions surface as 409.

**Caveat:** `AuditEvent` (supplementary) still rides the request's separate ORM
transaction by design; the immutable `WorkflowEvent` chain is the atomic source
of truth for governance state.

### 2026-07-27 — W4 Notifications ✅ (367 → 382 passed)
Pluggable notification transport: **dev outbox** (default, zero network I/O) +
**SMTP** for real environments, selected by `NOTIFICATION_TRANSPORT`
(`outbox|smtp|both`).

- **NT-1** emails at workflow transitions (submitted / changes_requested /
  rejected / approved / terminated), each with a **deep-link**
  (`{APP_BASE_URL}/review/{entity}?staging={id}`). Enqueued at the ENDPOINT
  layer AFTER the decision commits.
- **NT-2** `mdm_meta.notification_template` per `(domain, event)` (nullable domain
  = global default): subject/body templates (safe `str.format`, never eval),
  explicit `recipients` or role-resolved (submitted → approvers/stewards who can
  access the domain via W2 conferral; others → submitter/assignee), per-domain
  `from_address`. Built-in defaults when unconfigured. Admin CRUD
  `/admin/notification-templates`.
- **NT-3** SMTP settings = the per-environment service account
  (`SMTP_HOST/PORT/USERNAME/PASSWORD/USE_TLS`, `NOTIFICATION_FROM`); template
  `from_address` overrides per domain.
- Outbox: `mdm_meta.notification` (queue + viewer). `GET /admin/notifications`,
  `POST /admin/notifications/flush` (dispatch queued/failed), `.../{id}/resend`,
  `.../test` (validate SMTP config).

**Safety:** `safe_enqueue` wraps everything in a swallow-all guard and runs only
after the transition has committed on a separate connection — a notification
failure can NEVER break, block, or roll back a workflow transition (verified by
tests that monkeypatch enqueue to throw and assert approve/reject still succeed).
Default `outbox` transport makes zero SMTP/network calls.

**Files:** `services/notifications.py` *(new)*, `models/meta.py` (+2 tables),
`config.py` (transport + SMTP settings), `api/v1/{stewardship,data,admin}.py`
(enqueue wiring + outbox/template endpoints), `schemas/models.py`.
`tests/test_notifications.py` — 15 tests. (Verified by self-review: swallow-all
isolation, post-commit, network-free default, role-scoped recipients.)

### 2026-07-27 — W5 Downstream Distribution + Scheduler ✅ (382 → 397 passed)
- **DD-1** materialized views: new `mdm_pub` schema; each published entity gets
  `mdm_pub.<entity>` (business cols + mdm_id/version/source/timestamps, active
  golden records only) with a UNIQUE index on `mdm_id` (enables
  `REFRESH … CONCURRENTLY`). Created/replaced in the publish path (DROP+CREATE on
  column change; best-effort — never hard-fails publish); dropped with the entity.
- **DD-2** dependency-free stdlib scheduler (`app/services/scheduler.py`): a
  daemon thread on a `threading.Event`, opt-in via `SCHEDULER_ENABLED` (default
  off), started/stopped in the FastAPI lifespan. Jobs: `refresh_views` (REFRESH
  CONCURRENTLY w/ non-concurrent fallback) and `run_retention` (prunes landing +
  history older than the entity's `retention_days`, else its domain default —
  NEVER golden or the append-only audit/workflow tables; cutoff bound as a param).
  Per-job try/except isolates failures; double-start guarded; clean join on stop.
- Admin: `GET /admin/distribution`, `POST /admin/distribution/refresh`,
  `POST /admin/retention/run`, `GET /admin/scheduler`.
- Settings: `SCHEMA_PUBLISH`, `SCHEDULER_ENABLED`, `VIEW_REFRESH_INTERVAL_SECONDS`,
  `RETENTION_INTERVAL_SECONDS`, `RETENTION_ENABLED`.

**Files:** `services/scheduler.py` *(new)*, `services/ddl.py` (matview builders +
reconcile + drop-plan), `api/v1/{models_api,admin}.py`, `main.py` (lifespan),
`config.py`, `db.py`/bootstrap (mdm_pub schema + grants).
`tests/test_distribution.py` — 15 tests. (Verified by self-review of scheduler +
retention: thread isolation, golden/append-only never touched, bind-param cutoff.)

### 2026-07-27 — W6 Extensibility ✅ (397 → 412 passed)
- **EX-1** commit hooks (`app/services/hooks.py`): three points invoked by the
  pipeline — `pre_stage` (cleansing/defaults before staging insert), `pre_commit`
  (before the golden write), `post_commit` (chaining, runs AFTER commit in a fresh
  txn). `@hook(event, entity=None)` + `register_hook`; operator modules loaded via
  `HOOK_MODULES` at startup. **Failure isolation:** every hook runs inside nested
  SAVEPOINTs on BOTH the Core `conn` and the ORM `db`; pre_stage failure → row
  error (invalid staging row), pre_commit failure → clean `PipelineError` abort (no
  golden write), post_commit failure → swallowed + recorded. Never a 500 or
  half-applied write.
- **EX-2** transform registry (`app/services/transforms.py`): named/parameterized
  field transforms beyond the fixed normalization rules (`upper`, `lower`, `map`,
  `default`, `coalesce`, `left`, `regex_replace`, …), operator-extensible.
  Attributes gain a `transforms` list (jsonb) applied after normalization+coercion,
  then **re-coerced** to the column type; errors → invalid staging row.
- **EX-3** field mapping (`mdm_meta.field_mapping` + `app/services/mappings.py`):
  source→target field renaming with optional transform/default, keyed by
  `source_system`, applied at PROMOTION (landing keeps the RAW payload —
  capture-first preserved). Admin CRUD `/admin/field-mappings`.

**Files:** `services/{hooks,transforms,mappings}.py` *(new)*, `pipeline.py`
(3 hook points + mapping + post-commit helper), `validation.py` (transforms +
re-coerce), `models/meta.py` (FieldMapping + Attribute.transforms), `schemas`,
`model_io.py`, `bootstrap.py`, `config.py` (HOOK_MODULES), `admin.py` (mapping
CRUD), `api/v1/{stewardship,data}.py` (post-commit hooks after commit).
`tests/test_extensibility.py` — 15 tests.

**Reviewed** (adversarial): no blocker; capture-first, pre_commit clean-abort, SQL
safety, transform error handling all confirmed. 3 mediums (pre_stage not
savepoint-isolated; post_commit db-writes committed on failure + ran pre-commit;
pre_commit injected key → 500) — **all fixed**: uniform conn+db savepoints,
post_commit moved after the transaction commits, injected keys filtered.

### 2026-07-27 — W7 Observability ✅ (412 → 433 passed)
Structured JSON logging with named streams (`app/services/logging_config.py`):
- `JsonFormatter` — one JSON object/line (timestamp, level, stream, logger,
  message, request-context, `extra={}`, exc_info); **crash-proof** (guarded
  format + `default=str` + fallback line — never raises into a request path).
- Three streams via `stream_logger()`: **integration** (pipeline / references /
  validation), **custom** (hooks / transforms), **platform** (everything else).
  A `ContextFilter` stamps `stream` + request context on every record.
- Request correlation: `contextvars` (`request_id`, `actor`, `method`, `path`);
  `main.py` middleware assigns a uuid4 request-id, adds `X-Request-ID` response
  header, and emits one structured access-log line per request (status,
  duration_ms). Enables ELK/Splunk correlation.
- Config: `LOG_JSON` (default false = human text), `LOG_LEVEL`, `LOG_STREAM_FILES`
  + `LOG_DIR` (per-stream `RotatingFileHandler` for file-based ingestion).
  `GET /admin/logging` reports active config.

**Files:** `services/logging_config.py` *(new)*, `main.py` (configure_logging +
middleware), `deps.py` (actor binding), `admin.py` (logging endpoint),
`pipeline.py`/`references.py`/`validation.py` (integration stream + structured
events), `hooks.py`/`transforms.py` (custom stream), `config.py`.
`tests/test_logging.py` — 21 tests. (Verified: JSON smoke test emits valid line;
default text mode unchanged.)

### 2026-07-27 — W8 UI wiring ✅ (433 → 437 passed)
Surfaced W1–W6 into the no-build React/htm SPA (`app/static/app.js`). Backend
addition: `GET /data/{entity}/options?q=&limit=` (FK dropdown source: mdm_id +
business-key label) with tests.
- **UI-3** FK-aware forms: `RefSelect` debounced autocomplete for `reference`
  fields (review editor + record forms), submits the resolved `mdm_id`.
- **UI-1** power-user direct edit: `RecordForm` create/edit golden records via
  `?direct=true`, gated on `can_direct_edit`.
- **UI-5** targeted search: per-column filter row in `RecordsView`.
- **W3** `InboxView` (per-user badge from `/inbox/counts`, claim/release);
  Request-changes + mandatory approve/reject comments; workflow-history timeline
  modal; broken_reference surfacing + Re-resolve; Admin→Workflows (terminate/
  reassign).
- **W2** Admin→Domains CRUD; per-user permissions editor (domain_roles/
  entity/domain_permissions); elevated API keys; new roles in the matrix.
- **W4** Admin→Notifications (outbox + flush/resend/test, templates CRUD).
- **W5** Admin→Distribution (matviews, refresh, retention, scheduler status).
- **W1/W6** model editor: `reference` type (ref_entity/ref_attribute), enum-values
  editor, transforms editor, entity kind; Admin→Field-mappings CRUD.
- Gating uses the real `/auth/me` context (`can_direct_edit`/`can_approve`/
  `is_admin`/domain_roles); backend remains authoritative.

**Verified:** `node --check app/static/app.js` clean; htm harness rendered all 34
components (0 structural errors); every UI-backing endpoint returns 200 with
correct role context; SPA boots and login page renders cleanly (headless Chrome).
Full authenticated visual walkthrough deferred to W9 (needs example data).
`tests/test_api.py::TestOptionsRoute` — 4 tests.

### 2026-07-27 — W9 Cleanup, examples & run ✅ (437 → 439 passed)
**Cleanup:** removed the old `customer/product/vendor` examples + leftover test
(`t_*`, `dbg_*`) entities. New scripts: `scripts/reset_demo.py` (drops all entity
tiers + matviews, clears meta, keeps `default` domain + break-glass admin) and
`scripts/seed_demo.py` (publishes + seeds via the REAL pipeline). Replaced
`examples/models.yaml`.

**New UC-aligned demo model — 4 domains, 9 entities:**
- `entity_metadata` (UC-1): `owner`, `timezone` (ref), `capability` (ref),
  `service` (master, high-frequency — references owner+timezone, enums, transform),
  `service_capability` (association / M2M).
- `classification` (UC-2): `classification` (master, requires_approval, ref→service).
- `reference_data` (UC-3): `region` (ref), `demand_forecast` (bulk).
- `lookups` (UC-4): `status_code` (category→grouping).

**Seeded dataset:** 39 golden records; 6 inbox items incl. a held
`broken_reference` (SVC-ORPHAN, DQ-5) + a bad-enum (SVC-BADENUM, soft validation);
a rejected + a changes_requested task; junction rows; forecast bulk; a
`legacy_cmdb` field-mapping demo (EX-3); a per-domain notification template;
53→100 notifications in the outbox; 9 matviews populated. Demo users showcase
AC-1/AC-3: `editor_meta`/`approver_meta` (entity_metadata domain roles),
`steward_class` (classification), `power_user1`, `reader1`.

**Running:** `uvicorn` on http://127.0.0.1:8000 with `SCHEDULER_ENABLED=true`.

**3 real issues found via the authenticated visual walkthrough — all fixed:**
1. `/stewardship/queue` used a GLOBAL `staging:read` gate → a domain-only reviewer
   was locked out of the Review-queue view. Now allows domain-conferred access,
   scopes results to reviewable entities, and still 403s a truly role-less user.
2. `principal_context` (/auth/me) computed `permissions`/`can_approve`/`can_write`/
   `can_direct_edit` from GLOBAL roles only → the SPA hid approve/submit controls
   from domain-only reviewers. Now aggregates global ∪ all `domain_roles` (UI hint
   only; routes remain authoritative per-entity).
3. Sidebar footer showed "no roles" for domain-only users → now shows
   "approver @ entity_metadata" etc.
Regression tests added (`tests/test_authz_domains.py`): domain approver's
`can_approve` flag; domain reviewer can load `/queue`; role-less still 403.

**Verified via headless-Chrome (CDP) authenticated screenshots:** dashboard (9
entities / invalid-staged=2), data models, domain-scoped inbox, review queue +
review detail (Claim / Request-changes / Reject / Approve…, resolved reference
UUIDs, FK dropdown + enum select confirmed in `fieldEditor`). Full suite **439
passed**.

---

## HOW TO RUN / RESUME (for the user)

**Start Postgres** (portable): `pg_ctl -D ~/pgsql16/data -l ~/pgsql16/postgres.log start`

**Start the app** (Windows venv):
```
SCHEDULER_ENABLED=true .venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```
Open http://127.0.0.1:8000 — **admin** / value of `LOCAL_ADMIN_PASSWORD` in `.env`.

**Reset + reseed the demo** anytime:
```
.venv/Scripts/python -m scripts.reset_demo && .venv/Scripts/python -m scripts.seed_demo
```

**Demo logins** (password shown by seed_demo): `editor_meta`, `approver_meta`,
`steward_class`, `power_user1`, `reader1`.

**Run tests:** `.venv/Scripts/python -m pytest -q` (needs Postgres).

---

## Git state (as of 2026-07-27, end of session)

All work is **committed and pushed**. Status: everything below is DONE.

- **Remote:** `origin` → https://github.com/NimaIT/open-mdm *(private; created this
  session — the repo had no remote before)*. `.env` is gitignored (never pushed).
- **`master`** → `66e9145` (original base build, unchanged).
- **`feat/functional-requirements-buildout`** → `563c042` — the entire W1–W9
  buildout as ONE commit (49 files, +12,185/−351). Pushed, tracking `origin`.
- **No PR opened yet** — branch is ready for a PR against `master`:
  https://github.com/NimaIT/open-mdm/pull/new/feat/functional-requirements-buildout
- Working tree is **clean** (nothing uncommitted). Full suite: **439 passed**.

### Resume checklist for a fresh session
1. `git log --oneline -3` — confirm you're on `feat/functional-requirements-buildout` at `563c042`.
2. Start Postgres + app (see HOW TO RUN above); reseed the demo if the DB was reset.
3. This buildout is COMPLETE. Open follow-ups if any: open the PR, wire real SMTP,
   or the one logged cosmetic (reference fields in the review editor show the
   resolved UUID rather than a friendly label — the FK autocomplete works).
