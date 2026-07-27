"""Fixed internal tables: model metadata, identity, audit.

These are the only tables declared statically. Every *master data* table is
generated at runtime from the metadata held here.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.db import Base

META = {"schema": settings.SCHEMA_META}


def _uuid_col(**kw):
    return mapped_column(UUID(as_uuid=True), default=uuid.uuid4, **kw)


class EntityStatus(str, enum.Enum):
    DRAFT = "draft"          # editable, no DDL deployed
    PUBLISHED = "published"  # DDL deployed and in sync
    MODIFIED = "modified"    # DDL deployed but model has since changed
    DEPRECATED = "deprecated"


class UserSource(str, enum.Enum):
    LDAP = "ldap"
    LOCAL = "local"
    SERVICE = "service"


class Role(str, enum.Enum):
    ADMIN = "admin"
    STEWARD = "steward"        # legacy = editor + approver (unchanged)
    READER = "reader"
    SERVICE = "service"
    # Workstream 2 — finer-grained human roles that split the legacy steward.
    EDITOR = "editor"          # writes via the workflow only; cannot approve
    APPROVER = "approver"      # reviews and decides; does not submit data
    POWER_USER = "power_user"  # editor + authorised direct-to-live edits


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=True)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=True)


# --------------------------------------------------------------- data model
class Entity(TimestampMixin, Base):
    """A logical master-data entity (Customer, Product, ...).

    Publishing an entity generates four physical tables — landing, staging,
    live and history.
    """

    __tablename__ = "entity"
    __table_args__ = (
        UniqueConstraint("name", name="uq_entity_name"),
        CheckConstraint("name ~ '^[a-z][a-z0-9_]*$'", name="ck_entity_name_snake"),
        META,
    )

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    domain: Mapped[str] = mapped_column(String(100), nullable=True)
    # Entity role: 'master' (a golden-record entity), 'reference' (a lookup /
    # source-of-truth table) or 'association' (a junction table for a
    # many-to-many via two reference attributes).
    kind: Mapped[str] = mapped_column(String(20), default="master", nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=EntityStatus.DRAFT.value, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    published_version: Mapped[int] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)

    # Stewardship behaviour
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    soft_delete: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_approve_threshold: Mapped[int] = mapped_column(Integer, nullable=True)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=True)

    attributes: Mapped[list["Attribute"]] = relationship(
        back_populates="entity",
        cascade="all, delete-orphan",
        order_by="Attribute.position",
        lazy="selectin",
    )
    versions: Mapped[list["ModelVersion"]] = relationship(
        back_populates="entity", cascade="all, delete-orphan"
    )

    # -------------------------------------------------- convenience accessors
    @property
    def match_keys(self) -> list["Attribute"]:
        return [a for a in self.attributes if a.is_match_key]

    @property
    def business_key(self) -> list["Attribute"]:
        return [a for a in self.attributes if a.is_business_key]

    @property
    def is_deployed(self) -> bool:
        return self.status in (EntityStatus.PUBLISHED.value, EntityStatus.MODIFIED.value)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Entity {self.name} v{self.version} {self.status}>"


class Domain(TimestampMixin, Base):
    """A logical governance / access boundary that groups entities.

    A domain is *not* a separate PostgreSQL schema. The four tier-schemas
    (``mdm_landing`` / ``mdm_staging`` / ``mdm`` / ``mdm_history``) are shared by
    every entity regardless of domain — carving a physical schema per domain
    would fracture the tier model. A domain is instead the unit that

      * scopes per-domain authorisation (see ``User.domain_permissions`` and
        the ``domain`` column on ``GroupRoleMapping``), and
      * supplies lifecycle DEFAULTS an entity inherits when it leaves the
        corresponding field unset (``requires_approval``, ``retention_days``,
        ``default_soft_delete``).

    ``Entity.domain`` references ``Domain.name`` by convention (no hard FK, so a
    domain can be renamed or an entity can name a not-yet-created domain without
    a constraint violation).
    """

    __tablename__ = "domain"
    __table_args__ = (
        UniqueConstraint("name", name="uq_domain_name"),
        CheckConstraint("name ~ '^[a-z][a-z0-9_]*$'", name="ck_domain_name_snake"),
        META,
    )

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)

    # Lifecycle defaults inherited by member entities when their own value is
    # unset. Kept deliberately small — a domain is a policy boundary, not a
    # second copy of the entity model.
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=True)
    default_soft_delete: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Domain {self.name}>"


class Attribute(TimestampMixin, Base):
    """One field of an entity — maps to a physical column."""

    __tablename__ = "attribute"
    __table_args__ = (
        UniqueConstraint("entity_id", "name", name="uq_attribute_entity_name"),
        CheckConstraint("name ~ '^[a-z][a-z0-9_]*$'", name="ck_attribute_name_snake"),
        META,
    )

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{settings.SCHEMA_META}.entity.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    length: Mapped[int] = mapped_column(Integer, nullable=True)
    numeric_precision: Mapped[int] = mapped_column(Integer, nullable=True)
    numeric_scale: Mapped[int] = mapped_column(Integer, nullable=True)

    is_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_unique: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_business_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_match_key: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_indexed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_pii: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    default_value: Mapped[str] = mapped_column(Text, nullable=True)
    # Validation: regex, min/max, min_length/max_length, enum[]
    validation: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Normalisation applied before matching: trim, lower, upper, strip_punctuation
    normalization: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Extensibility (EX-2): ordered custom transforms applied AFTER normalisation
    # and coercion. Each item is a name (str) or {"fn": name, ...params}.
    transforms: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )

    # Optional reference to another entity (lookup / FK)
    ref_entity: Mapped[str] = mapped_column(String(63), nullable=True)
    ref_attribute: Mapped[str] = mapped_column(String(63), nullable=True)

    entity: Mapped["Entity"] = relationship(back_populates="attributes")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Attribute {self.name}:{self.data_type}>"


class FieldMapping(TimestampMixin, Base):
    """Source-schema -> target-schema field mapping (EX-3).

    Applied during promotion (landing -> staging), keyed by the landing row's
    ``mdm_source_system`` (``NULL`` == applies to every source). Renames
    ``source_field`` to ``target_field`` with an optional transform spec and an
    optional default. This is metadata only — it never changes the raw landing
    payload (capture-first).
    """

    __tablename__ = "field_mapping"
    __table_args__ = (
        UniqueConstraint(
            "source_system", "entity_name", "source_field", "target_field",
            name="uq_field_mapping",
        ),
        Index("ix_field_mapping_entity", "entity_name"),
        Index("ix_field_mapping_source_system", "source_system"),
        META,
    )

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    # NULL == applies to all source systems.
    source_system: Mapped[str] = mapped_column(String(100), nullable=True)
    entity_name: Mapped[str] = mapped_column(String(63), nullable=False)
    source_field: Mapped[str] = mapped_column(String(128), nullable=False)
    target_field: Mapped[str] = mapped_column(String(63), nullable=False)
    # Optional transform spec (str name, {"fn": name, ...}, or a list of those).
    transform: Mapped[dict] = mapped_column(JSONB, nullable=True)
    default_value: Mapped[str] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<FieldMapping {self.entity_name} {self.source_field}"
                f"->{self.target_field}>")


class ModelVersion(Base):
    """Immutable snapshot of an entity definition, written on every change."""

    __tablename__ = "model_version"
    __table_args__ = (
        UniqueConstraint("entity_id", "version", name="uq_model_version"),
        META,
    )

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{settings.SCHEMA_META}.entity.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    change_note: Mapped[str] = mapped_column(Text, nullable=True)
    applied_ddl: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(255), nullable=True)

    entity: Mapped["Entity"] = relationship(back_populates="versions")


# ------------------------------------------------------------------ identity
class User(TimestampMixin, Base):
    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("username", name="uq_user_username"), META)

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(
        String(20), default=UserSource.LDAP.value, nullable=False
    )
    # Only populated for local break-glass accounts.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=True)
    dn: Mapped[str] = mapped_column(Text, nullable=True)
    roles: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Optional per-entity overrides: {"customer": ["read","write"]}
    entity_permissions: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Optional per-domain overrides: {"finance": ["read","write","approve"]}.
    # Consulted after entity_permissions and before the global role permission
    # (see can_access_entity). Empty map == no restriction, fall through.
    # This is a RESTRICTION layer (narrows an already-granted capability).
    domain_permissions: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    # Optional per-domain ROLE grants: {"finance": ["editor","approver"]}.
    # Unlike domain_permissions, this is a CONFERRAL layer: it GRANTS the listed
    # roles' permissions *within that domain only*, so a user with no global roles
    # can act inside their granted domain and nowhere else (AC-3). Evaluated by
    # effective_permissions(); empty map == no conferral, identical to before.
    domain_roles: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def has_role(self, role: str) -> bool:
        return role in (self.roles or [])

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} {self.roles}>"


class GroupRoleMapping(TimestampMixin, Base):
    """Maps an LDAP/AD group DN onto an application role."""

    __tablename__ = "group_role_mapping"
    __table_args__ = (
        UniqueConstraint("group_dn", "role", name="uq_group_role"),
        META,
    )

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    group_dn: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    # Optional domain the grant is confined to. NULL == a global role grant,
    # which is the only shape legacy mappings have — so existing behaviour is
    # unchanged. A non-null domain grants the role *within that domain only*.
    domain: Mapped[str] = mapped_column(String(63), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ApiKey(TimestampMixin, Base):
    """Machine-to-machine credential. Writes into landing only."""

    __tablename__ = "api_key"
    __table_args__ = (UniqueConstraint("key_prefix", name="uq_api_key_prefix"), META)

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    source_system: Mapped[str] = mapped_column(String(100), nullable=True)
    allowed_entities: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # AC-4 privilege elevation. An elevated key gets cross-domain *write* reach
    # (all domains, or those in allowed_domains) that no human role is given —
    # but never approval power: the service role still lacks staging:approve.
    elevated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allowed_domains: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------- audit
class AuditEvent(Base):
    """Append-only audit trail covering every consequential action."""

    __tablename__ = "audit_event"
    __table_args__ = (
        Index("ix_audit_entity_record", "entity_name", "record_id"),
        Index("ix_audit_occurred_at", "occurred_at"),
        META,
    )

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    actor: Mapped[str] = mapped_column(String(255), nullable=True)
    actor_roles: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_name: Mapped[str] = mapped_column(String(63), nullable=True)
    record_id: Mapped[str] = mapped_column(String(64), nullable=True)
    tier: Mapped[str] = mapped_column(String(20), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    before_value: Mapped[dict] = mapped_column(JSONB, nullable=True)
    after_value: Mapped[dict] = mapped_column(JSONB, nullable=True)
    ip_address: Mapped[str] = mapped_column(String(64), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


# ----------------------------------------------------- governed change workflow
class WorkflowTask(TimestampMixin, Base):
    """The governance state of one staging change request (Workstream 3).

    The generated staging row remains the *data*; this row is the *decision
    state* — its status, assignment, submission rationale and (via
    ``WorkflowEvent``) its full, unfragmented history. There is exactly one task
    per staging row. ``staging_id`` is a plain bigint, not a real FK, because the
    staging table is generated per entity and unknown at import time.
    """

    __tablename__ = "workflow_task"
    __table_args__ = (
        UniqueConstraint("entity_name", "staging_id", name="uq_workflow_task_staging"),
        Index("ix_workflow_task_status", "status"),
        Index("ix_workflow_task_entity", "entity_name"),
        Index("ix_workflow_task_assigned", "assigned_to"),
        Index("ix_workflow_task_claimed", "claimed_by"),
        META,
    )

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    entity_name: Mapped[str] = mapped_column(String(63), nullable=False)
    staging_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    domain: Mapped[str] = mapped_column(String(100), nullable=True)
    # pending_review|changes_requested|rejected|applied|terminated
    status: Mapped[str] = mapped_column(
        String(32), default="pending_review", nullable=False
    )
    submitted_by: Mapped[str] = mapped_column(String(255), nullable=True)
    submit_rationale: Mapped[str] = mapped_column(Text, nullable=True)
    assigned_to: Mapped[str] = mapped_column(String(255), nullable=True)
    claimed_by: Mapped[str] = mapped_column(String(255), nullable=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    change_type: Mapped[str] = mapped_column(String(20), nullable=True)

    events: Mapped[list["WorkflowEvent"]] = relationship(
        back_populates="task", order_by="WorkflowEvent.seq", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<WorkflowTask {self.entity_name}#{self.staging_id} {self.status}>"


class WorkflowEvent(Base):
    """One immutable step in a change request's decision chain (Workstream 3).

    Append-only at the database level: a BEFORE UPDATE OR DELETE trigger
    (installed by bootstrap) rejects any mutation, so history cannot be rewritten
    even by the application (AO-2).
    """

    __tablename__ = "workflow_event"
    __table_args__ = (
        UniqueConstraint("task_id", "seq", name="uq_workflow_event_seq"),
        Index("ix_workflow_event_task", "task_id"),
        META,
    )

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{settings.SCHEMA_META}.workflow_task.id"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # submit|claim|release|edit|request_changes|approve|reject|reassign|terminate|apply
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=True)
    actor_roles: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    comment: Mapped[str] = mapped_column(Text, nullable=True)
    from_status: Mapped[str] = mapped_column(String(32), nullable=True)
    to_status: Mapped[str] = mapped_column(String(32), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ip_address: Mapped[str] = mapped_column(String(64), nullable=True)

    task: Mapped["WorkflowTask"] = relationship(back_populates="events")


# ------------------------------------------------------------- notifications
class NotificationTemplate(TimestampMixin, Base):
    """Per-domain (or global) template for a workflow-transition notification (W4).

    ``domain`` NULL means the global default used for any domain lacking a
    specific row. ``event`` is one of the workflow transitions notifications are
    sent at. ``recipients`` is an explicit jsonb list of email addresses; when
    empty, recipients are resolved by role scoped to the domain. ``subject`` and
    ``body`` are ``str.format``-style templates rendered over a safe context.
    """

    __tablename__ = "notification_template"
    __table_args__ = (
        UniqueConstraint("domain", "event", name="uq_notification_template_de"),
        Index("ix_notification_template_event", "event"),
        META,
    )

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    # NULL == the global default for this event.
    domain: Mapped[str] = mapped_column(String(100), nullable=True)
    # submitted|changes_requested|rejected|approved|terminated
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Explicit recipient emails; empty list == resolve by role for the domain.
    recipients: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # Optional per-domain From override (else settings.NOTIFICATION_FROM).
    from_address: Mapped[str] = mapped_column(String(320), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Notification(Base):
    """A single notification — the dev outbox AND the delivery queue (W4).

    A row is inserted (status ``queued`` or ``skipped``) after a workflow
    transition commits. ``dispatch`` later moves it to ``sent`` / ``failed``.
    """

    __tablename__ = "notification"
    __table_args__ = (
        Index("ix_notification_status", "status"),
        Index("ix_notification_domain", "domain"),
        Index("ix_notification_event", "event"),
        Index("ix_notification_created_at", "created_at"),
        META,
    )

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    domain: Mapped[str] = mapped_column(String(100), nullable=True)
    entity_name: Mapped[str] = mapped_column(String(63), nullable=True)
    staging_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=True)
    to_addresses: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    from_address: Mapped[str] = mapped_column(String(320), nullable=True)
    subject: Mapped[str] = mapped_column(Text, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=True)
    deep_link: Mapped[str] = mapped_column(Text, nullable=True)
    # queued|sent|failed|skipped
    status: Mapped[str] = mapped_column(
        String(20), default="queued", nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class PromotionBatch(Base):
    """Tracks a landing -> staging or staging -> live promotion run."""

    __tablename__ = "promotion_batch"
    __table_args__ = (META,)

    id: Mapped[uuid.UUID] = _uuid_col(primary_key=True)
    entity_name: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    rows_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_ok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    triggered_by: Mapped[str] = mapped_column(String(255), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=True)
