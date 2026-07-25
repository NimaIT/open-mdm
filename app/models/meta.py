"""Fixed internal tables: model metadata, identity, audit.

These are the only tables declared statically. Every *master data* table is
generated at runtime from the metadata held here.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
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
    STEWARD = "steward"
    READER = "reader"
    SERVICE = "service"


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

    # Optional reference to another entity (lookup / FK)
    ref_entity: Mapped[str] = mapped_column(String(63), nullable=True)
    ref_attribute: Mapped[str] = mapped_column(String(63), nullable=True)

    entity: Mapped["Entity"] = relationship(back_populates="attributes")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Attribute {self.name}:{self.data_type}>"


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
