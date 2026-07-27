"""Application configuration, sourced from environment variables / .env."""
from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---------------------------------------------------------------- app
    APP_NAME: str = "MDM Platform"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    API_PREFIX: str = "/api/v1"

    # ------------------------------------------------- observability (AO-1)
    # Structured JSON logging to stdout (for ELK/Splunk ingestion). Off by
    # default so local runs keep human-readable text logs; enable per env.
    LOG_JSON: bool = False
    LOG_LEVEL: str = "INFO"
    # Also mirror each named stream (platform/integration/custom) to its own
    # rotating JSON file under LOG_DIR, for file-based log shipping.
    LOG_STREAM_FILES: bool = False
    LOG_DIR: Optional[str] = None
    SECRET_KEY: str = Field(
        default="CHANGE-ME-dev-only-secret-key-not-for-production",
        description="Signing key for session cookies and JWTs.",
    )
    SESSION_COOKIE_NAME: str = "mdm_session"
    ACCESS_TOKEN_TTL_MINUTES: int = 480

    # ------------------------------------------------------- target cluster
    # Runtime (DML) connection — least privilege.
    PGHOST: str = "localhost"
    PGPORT: int = 5432
    PGDATABASE: str = "mdm"
    PGUSER: str = "mdm_app"
    PGPASSWORD: str = ""
    PGSSLMODE: str = "prefer"

    # DDL connection — elevated, used only when publishing models / provisioning.
    # Falls back to the runtime credentials when unset.
    PG_DDL_USER: Optional[str] = None
    PG_DDL_PASSWORD: Optional[str] = None

    # Maintenance DB used for CREATE DATABASE (cannot run inside the target db).
    PG_MAINTENANCE_DATABASE: str = "postgres"

    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False

    # ------------------------------------------------------------- schemas
    SCHEMA_META: str = "mdm_meta"
    SCHEMA_LANDING: str = "mdm_landing"
    SCHEMA_STAGING: str = "mdm_staging"
    SCHEMA_LIVE: str = "mdm"
    SCHEMA_HISTORY: str = "mdm_history"
    # Downstream distribution schema (Workstream 5). Holds one materialized view
    # per published entity — the stable, queryable surface for BI / APIs.
    SCHEMA_PUBLISH: str = "mdm_pub"

    # ---------------------------------------------------------------- LDAP
    LDAP_ENABLED: bool = False
    LDAP_SERVER: str = "ldap://localhost:389"
    LDAP_USE_SSL: bool = False
    LDAP_START_TLS: bool = True
    LDAP_TLS_VERIFY: bool = True
    LDAP_CA_CERT_FILE: Optional[str] = None
    LDAP_BIND_DN: Optional[str] = None
    LDAP_BIND_PASSWORD: Optional[str] = None
    LDAP_BASE_DN: str = "DC=example,DC=com"
    LDAP_USER_SEARCH_BASE: Optional[str] = None
    LDAP_USER_FILTER: str = "(&(objectClass=user)(sAMAccountName={username}))"
    LDAP_ATTR_USERNAME: str = "sAMAccountName"
    LDAP_ATTR_EMAIL: str = "mail"
    LDAP_ATTR_DISPLAY_NAME: str = "displayName"
    LDAP_GROUP_SEARCH_BASE: Optional[str] = None
    LDAP_NESTED_GROUPS: bool = True
    LDAP_TIMEOUT_SECONDS: int = 10
    # Directory-group DN -> role mappings (overridable in the database).
    LDAP_ADMIN_GROUP_DN: Optional[str] = None
    LDAP_STEWARD_GROUP_DN: Optional[str] = None
    LDAP_READER_GROUP_DN: Optional[str] = None

    # ------------------------------------------------- local break-glass admin
    LOCAL_ADMIN_ENABLED: bool = True
    LOCAL_ADMIN_USERNAME: str = "admin"
    LOCAL_ADMIN_PASSWORD: Optional[str] = None

    # ------------------------------------------------------------ behaviour
    # A steward may not approve a record they submitted or last edited.
    ENFORCE_SEGREGATION_OF_DUTIES: bool = True
    # Governed change management (Workstream 3). When on, every review decision
    # (approve / reject / request_changes) must carry a non-empty comment, so the
    # workflow history records *why* each transition happened (GC-4).
    REQUIRE_REVIEW_COMMENTS: bool = True
    # When on, a submitter must supply a rationale on the write path before the
    # change is accepted. Off by default so machine ingestion is not broken; the
    # field is always threaded through so a UI can require it (GC-4).
    REQUIRE_SUBMIT_RATIONALE: bool = False
    ALLOW_DESTRUCTIVE_DDL: bool = False
    AUTO_PROMOTE_LANDING: bool = True
    # Extensibility (EX-1): importable module paths loaded at startup so operators
    # can register pipeline hooks / custom transforms by dropping in a module.
    # Import failures are logged, not fatal. Accepts a comma-separated env value.
    HOOK_MODULES: List[str] = []
    SOFT_DELETE: bool = True
    MAX_BULK_ROWS: int = 10_000
    CORS_ORIGINS: List[str] = ["http://localhost:5173", "http://localhost:8000"]

    # ----------------------------------------------------- notifications (W4)
    # Master switch. When off, enqueue_notification is a no-op (no rows, no I/O).
    NOTIFICATIONS_ENABLED: bool = True
    # Transport selection: 'outbox' (dev — record only, NO network I/O),
    # 'smtp' (real send), or 'both' (record AND send). Default 'outbox' keeps
    # local runs / tests entirely off the network.
    NOTIFICATION_TRANSPORT: str = "outbox"
    # Base URL used to build deep-links back to a record/task in the UI.
    APP_BASE_URL: str = "http://localhost:8000"
    # Default From address; a template's from_address overrides per domain.
    NOTIFICATION_FROM: str = "mdm@localhost"
    # SMTP service account (the per-environment sender). prod vs non-prod differ
    # purely by these env vars.
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: Optional[str] = None
    SMTP_PASSWORD: Optional[str] = None
    SMTP_USE_TLS: bool = True
    SMTP_TIMEOUT: int = 10

    # ------------------------------------- scheduler & distribution (W5)
    # In-process, stdlib-thread scheduler. OFF by default so tests / local runs
    # never spawn background threads unexpectedly; enabled explicitly per env.
    SCHEDULER_ENABLED: bool = False
    # How often the scheduler refreshes every entity's mdm_pub materialized view.
    VIEW_REFRESH_INTERVAL_SECONDS: int = 600
    # How often the retention job prunes aged landing / history rows.
    RETENTION_INTERVAL_SECONDS: int = 3600
    # Whether the scheduled retention job runs at all. The manual admin endpoint
    # runs regardless of this switch.
    RETENTION_ENABLED: bool = True

    @field_validator("CORS_ORIGINS", "HOOK_MODULES", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    # ------------------------------------------------------------ derived
    def _dsn(self, user: str, password: str, database: str) -> str:
        from urllib.parse import quote_plus

        pw = f":{quote_plus(password)}" if password else ""
        return (
            f"postgresql+psycopg://{quote_plus(user)}{pw}"
            f"@{self.PGHOST}:{self.PGPORT}/{database}?sslmode={self.PGSSLMODE}"
        )

    @property
    def runtime_dsn(self) -> str:
        return self._dsn(self.PGUSER, self.PGPASSWORD, self.PGDATABASE)

    @property
    def ddl_dsn(self) -> str:
        return self._dsn(
            self.PG_DDL_USER or self.PGUSER,
            self.PG_DDL_PASSWORD if self.PG_DDL_USER else self.PGPASSWORD,
            self.PGDATABASE,
        )

    @property
    def maintenance_dsn(self) -> str:
        return self._dsn(
            self.PG_DDL_USER or self.PGUSER,
            self.PG_DDL_PASSWORD if self.PG_DDL_USER else self.PGPASSWORD,
            self.PG_MAINTENANCE_DATABASE,
        )

    @property
    def all_schemas(self) -> List[str]:
        return [
            self.SCHEMA_META,
            self.SCHEMA_LANDING,
            self.SCHEMA_STAGING,
            self.SCHEMA_LIVE,
            self.SCHEMA_HISTORY,
            self.SCHEMA_PUBLISH,
        ]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
