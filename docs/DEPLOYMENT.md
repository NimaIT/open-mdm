# Deployment

How to run the MDM Platform in a real environment. Read
[`RUNBOOK.md`](RUNBOOK.md) for day-two operations.

---

## 1. What you need

| Requirement | Notes |
|---|---|
| PostgreSQL 13+ | Tested against 16. Needs the `pgcrypto`-provided `gen_random_uuid()`, which is built in from PG 13. |
| Python 3.9+ | Or use the container image, which pins its own runtime. |
| Two database roles | One for DDL, one for runtime DML. See below. |
| A TLS terminator | Nginx, an ALB, Traefik — anything. Do not expose the app directly. |
| An LDAP/AD server | Optional, but the point of this tool in most enterprises. |

No Node.js is required. React is vendored under `app/static/vendor/`, so the
frontend has no build step and the image needs no JavaScript toolchain. This is
deliberate: it makes the application deployable into air-gapped, AD-joined
networks where an `npm install` at build time is not an option.

---

## 2. Database roles: least privilege

Publishing a data model needs `CREATE` on the database. Serving traffic does not.
Splitting these means a compromised application process cannot alter your schema.

```sql
-- Elevated role, used only by the publish/bootstrap operations
CREATE ROLE mdm_ddl LOGIN PASSWORD '<strong-password>';

-- Runtime role, used for every request
CREATE ROLE mdm_app LOGIN PASSWORD '<strong-password>';

CREATE DATABASE mdm OWNER mdm_ddl;
GRANT CONNECT ON DATABASE mdm TO mdm_app;

-- Only if you want the application itself to be able to provision
-- further databases via POST /api/v1/models/cluster/bootstrap
ALTER ROLE mdm_ddl CREATEDB;
```

Then run the bootstrap, which creates the five schemas, the internal metadata
tables, and grants `mdm_app` DML on them — including `ALTER DEFAULT PRIVILEGES`
so tables generated later are reachable without a manual grant:

```bash
.venv/bin/python -m scripts.bootstrap
```

Verify before you go further:

```bash
curl -s "$BASE/api/v1/models/cluster/privileges" -b cookies.txt | jq
```

Each failed check comes back with the exact SQL to fix it. Do not skip this step —
a missing grant otherwise surfaces later as a confusing mid-publish failure.

### Schemas created

| Schema | Contents |
|---|---|
| `mdm_meta` | Entity/attribute definitions, users, API keys, audit, batches |
| `mdm_landing` | Raw inbound writes, one table per entity |
| `mdm_staging` | Typed, validated candidates awaiting review |
| `mdm` | Golden records |
| `mdm_history` | Prior versions of golden records |

Rename them with `SCHEMA_META`, `SCHEMA_LANDING`, `SCHEMA_STAGING`,
`SCHEMA_LIVE`, `SCHEMA_HISTORY` if they collide with something existing.

---

## 3. Configuration

Copy `.env.example` to `.env` and work through it. The values that matter most:

```bash
ENVIRONMENT=production          # marks session cookies Secure
DEBUG=false                     # never true in production: it leaks error detail
SECRET_KEY=<64+ random chars>   # openssl rand -base64 48

PGHOST=db.internal
PGPORT=5432
PGDATABASE=mdm
PGUSER=mdm_app
PGPASSWORD=<runtime password>
PGSSLMODE=require               # verify-full if you have the CA
PG_DDL_USER=mdm_ddl
PG_DDL_PASSWORD=<ddl password>

LDAP_ENABLED=true
LDAP_SERVER=ldaps://dc01.corp.example.com:636
LDAP_USE_SSL=true
LDAP_TLS_VERIFY=true

LOCAL_ADMIN_ENABLED=true        # keep this: it is your break-glass path
LOCAL_ADMIN_PASSWORD=<strong>   # or omit and read the generated one from logs

ENFORCE_SEGREGATION_OF_DUTIES=true
CORS_ORIGINS=https://mdm.corp.example.com
```

A production checklist worth actually ticking:

- [ ] `SECRET_KEY` is random and not the shipped default. Rotating it invalidates all sessions.
- [ ] `DEBUG=false` and `ENVIRONMENT=production`.
- [ ] `PGSSLMODE` is `require` or stricter.
- [ ] `LOCAL_ADMIN_PASSWORD` is strong, stored in your secret manager, and known to more than one person.
- [ ] `CORS_ORIGINS` lists only your real origins.
- [ ] Secrets come from a secret manager, not a committed `.env`.
- [ ] `.env` is in `.gitignore` (it is, by default — keep it that way).

---

## 4. Running it

### Docker Compose

The shipped `docker-compose.yml` brings up PostgreSQL and the application
together — good for evaluation, and a reasonable starting point for a small
internal deployment.

```bash
cp .env.example .env    # edit SECRET_KEY, passwords
docker compose up -d --build
docker compose logs -f app
```

For production, point the app at your managed database instead and drop the `db`
service.

### Container image directly

```bash
docker build -t mdm-platform:1.0.0 .
docker run -d --name mdm -p 8000:8000 --env-file .env mdm-platform:1.0.0
```

### Without containers (systemd)

```ini
# /etc/systemd/system/mdm.service
[Unit]
Description=MDM Platform
After=network-online.target

[Service]
User=mdm
WorkingDirectory=/opt/mdm
EnvironmentFile=/opt/mdm/.env
ExecStart=/opt/mdm/.venv/bin/uvicorn app.main:app \
          --host 127.0.0.1 --port 8000 --workers 4 --proxy-headers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Bind to localhost and let your reverse proxy handle TLS and the public interface.

---

## 5. Reverse proxy

```nginx
server {
    listen 443 ssl http2;
    server_name mdm.corp.example.com;

    ssl_certificate     /etc/ssl/certs/mdm.crt;
    ssl_certificate_key /etc/ssl/private/mdm.key;

    # CSV/model imports can be large
    client_max_body_size 64m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

`X-Forwarded-For` matters: the audit log records the client IP from it, so
without it every event is attributed to the proxy.

---

## 6. Scaling

All state lives in PostgreSQL, so the application scales horizontally with no
sticky sessions — session tokens are signed JWTs, verified independently by any
instance.

- **Workers:** start with `--workers = 2 × CPU cores`. Each worker holds its own
  connection pool (`DB_POOL_SIZE`, default 10), so total connections are
  `instances × workers × (DB_POOL_SIZE + DB_MAX_OVERFLOW)`. Size your
  `max_connections` or put PgBouncer in front before you scale out.
- **High write volume:** set `AUTO_PROMOTE_LANDING=false` so landing→staging
  validation stops running inside the request, then drive
  `POST /api/v1/stewardship/{entity}/promote` from a scheduler. Writes become a
  single INSERT and latency drops sharply.
- **Read-heavy reporting:** point BI tools at a read replica of the `mdm` schema.
  Golden records are a normal, fully-typed relational schema — that is the whole
  point of generating real tables rather than storing documents.

---

## 7. Health checks

| Endpoint | Use | Behaviour |
|---|---|---|
| `/health` | liveness | Returns 200 whenever the process is up. Never touches the database — a database blip must not trigger a pod restart loop. |
| `/ready` | readiness | Returns 200 only when the database is reachable; 503 otherwise. Use this to gate traffic. |

Kubernetes:

```yaml
livenessProbe:
  httpGet: { path: /health, port: 8000 }
  initialDelaySeconds: 10
  periodSeconds: 20
readinessProbe:
  httpGet: { path: /ready, port: 8000 }
  initialDelaySeconds: 5
  periodSeconds: 10
```

---

## 8. Upgrades

1. Back up the database. `mdm_history` is what makes point-in-time
   reconstruction of golden records possible — do not exclude it.
2. Deploy the new version to one instance and watch `/ready` plus the logs.
3. Internal metadata tables are created with `create_all`, which is additive and
   idempotent. Alembic is included for when a future release needs a real
   migration; run `alembic upgrade head` if the release notes say so.
4. Generated master-data tables are untouched by an application upgrade — they
   only change when you publish a model.
5. Roll forward the remaining instances.

Rollback: redeploy the previous image. Because upgrades do not rewrite
master-data tables, an application rollback is safe as long as no model was
published against the new version.

---

## 9. Backup

```bash
# Everything
pg_dump -Fc -d mdm -f mdm-$(date +%F).dump

# Definitions only — useful for rebuilding an environment's structure
pg_dump -Fc -d mdm -n mdm_meta -f mdm-meta-$(date +%F).dump
```

You can also export the models as files and commit them to Git, which gives you
a reviewable, diffable history of your data model independent of database
backups:

```bash
curl -s "$BASE/api/v1/models/export/all?fmt=yaml" -b cookies.txt -o models.yaml
```

That is the recommended way to promote a data model from staging to production:
export from one environment, import to the next, publish deliberately.

---

## 10. Security notes

- Sessions are `HttpOnly`, `SameSite=Lax`, and `Secure` when
  `ENVIRONMENT=production`.
- API keys are stored as SHA-256 hashes; the plaintext is shown exactly once at
  issue time and compared in constant time thereafter.
- Local admin passwords are bcrypt-hashed. LDAP passwords are never stored or
  compared — verification is a bind against the directory.
- Every identifier used in generated DDL is validated against a strict allow-list
  and quoted; every value is a bind parameter. See
  [`ARCHITECTURE.md`](ARCHITECTURE.md) §4.
- Roles are re-read from the database on every request rather than trusted from
  the token, so revoking access takes effect immediately instead of at token
  expiry.
- Consider network-restricting the DDL role to the application hosts only.
