"""Pydantic request/response schemas."""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.identifiers import SUPPORTED_TYPES


class AttributeIn(BaseModel):
    name: str = Field(..., description="Physical column name (lower snake_case).")
    display_name: Optional[str] = None
    description: Optional[str] = None
    data_type: str = Field(..., description=f"One of: {', '.join(SUPPORTED_TYPES)}")
    length: Optional[int] = None
    numeric_precision: Optional[int] = None
    numeric_scale: Optional[int] = None
    is_required: bool = False
    is_unique: bool = False
    is_business_key: bool = False
    is_match_key: bool = False
    is_indexed: bool = False
    is_pii: bool = False
    default_value: Optional[str] = None
    validation: Dict[str, Any] = Field(default_factory=dict)
    normalization: List[str] = Field(default_factory=list)
    # Custom transforms (EX-2) applied after normalisation + coercion, in order.
    # Each item is a name (str) or {"fn": name, ...params}.
    transforms: List[Any] = Field(default_factory=list)
    ref_entity: Optional[str] = None
    ref_attribute: Optional[str] = None
    position: int = 0

    @field_validator("data_type")
    @classmethod
    def _known_type(cls, v: str) -> str:
        if v.lower() not in SUPPORTED_TYPES:
            raise ValueError(
                f"Unsupported data_type '{v}'. Supported: {', '.join(SUPPORTED_TYPES)}"
            )
        return v.lower()

    @model_validator(mode="after")
    def _reference_requires_ref_entity(self):
        if self.data_type == "reference" and not self.ref_entity:
            raise ValueError(
                "A 'reference' attribute must specify ref_entity (the parent "
                "entity it points at)."
            )
        return self


class AttributeOut(AttributeIn):
    model_config = ConfigDict(from_attributes=True)
    id: Optional[str] = None

    @field_validator("id", mode="before")
    @classmethod
    def _stringify(cls, v):
        return str(v) if v is not None else None


class EntityIn(BaseModel):
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    domain: Optional[str] = None
    kind: str = "master"
    requires_approval: bool = True
    soft_delete: bool = True
    auto_approve_threshold: Optional[int] = None
    retention_days: Optional[int] = None
    attributes: List[AttributeIn] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        allowed = {"master", "reference", "association"}
        if (v or "master").lower() not in allowed:
            raise ValueError(f"kind must be one of: {', '.join(sorted(allowed))}")
        return (v or "master").lower()


class EntityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    display_name: Optional[str]
    description: Optional[str]
    domain: Optional[str]
    kind: Optional[str] = "master"
    status: str
    version: int
    published_version: Optional[int]
    published_at: Optional[datetime]
    requires_approval: bool
    soft_delete: bool
    attributes: List[AttributeOut] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("id", mode="before")
    @classmethod
    def _stringify(cls, v):
        return str(v)


class EntitySummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    display_name: Optional[str]
    domain: Optional[str]
    status: str
    version: int
    attribute_count: int = 0

    @field_validator("id", mode="before")
    @classmethod
    def _stringify(cls, v):
        return str(v)


class PublishRequest(BaseModel):
    confirm_destructive: bool = Field(
        False,
        description="Must be true to apply column drops or narrowing type changes.",
    )
    dry_run: bool = Field(False, description="Return the SQL without executing it.")
    change_note: Optional[str] = None


class RecordWrite(BaseModel):
    """A single master-data write. Fields are entity-defined, hence free-form."""
    model_config = ConfigDict(extra="allow")


class BulkWrite(BaseModel):
    records: List[Dict[str, Any]]
    operation: str = "INSERT"
    source_system: Optional[str] = None


class StagingEdit(BaseModel):
    updates: Dict[str, Any]


class ReviewDecision(BaseModel):
    note: Optional[str] = None


class RejectDecision(BaseModel):
    reason: str = Field(..., min_length=1)


class RequestChangesDecision(BaseModel):
    """Send a change request back to its submitter. The comment is mandatory so
    the workflow history always records *why* changes were requested (GC-1/GC-4)."""
    comment: str = Field(..., min_length=1)


class ReassignIn(BaseModel):
    """Admin: (re)assign a workflow task to a reviewer (GC-6)."""
    assignee: Optional[str] = None
    note: Optional[str] = None


class TerminateIn(BaseModel):
    """Admin: force-close a stuck workflow task (GC-6). Reason is mandatory."""
    reason: str = Field(..., min_length=1)


class BulkReview(BaseModel):
    staging_ids: List[int]
    note: Optional[str] = None


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int
    username: str
    roles: List[str]
    permissions: List[str]


class ApiKeyCreate(BaseModel):
    name: str
    source_system: Optional[str] = None
    allowed_entities: List[str] = Field(default_factory=list)
    expires_at: Optional[datetime] = None
    # AC-4: an elevated key gets cross-domain write reach (all domains, or those
    # listed in allowed_domains). It still cannot approve.
    elevated: bool = False
    allowed_domains: List[str] = Field(default_factory=list)


class GroupMappingIn(BaseModel):
    group_dn: str
    role: str
    description: Optional[str] = None
    # Optional domain to confine the grant to (null = a global role grant).
    domain: Optional[str] = None


class DomainIn(BaseModel):
    """Create/replace payload for a governance domain."""
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    requires_approval: bool = True
    retention_days: Optional[int] = None
    default_soft_delete: bool = True


class DomainOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    display_name: Optional[str]
    description: Optional[str]
    requires_approval: bool
    retention_days: Optional[int]
    default_soft_delete: bool
    entity_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("id", mode="before")
    @classmethod
    def _stringify(cls, v):
        return str(v)


class NotificationTemplateIn(BaseModel):
    """Create/replace a per-domain (or global, domain=null) notification template
    (NT-2). ``recipients`` is an explicit email list; empty means resolve by role.
    """
    domain: Optional[str] = None
    event: str = Field(..., description="submitted|changes_requested|rejected|"
                                        "approved|terminated")
    subject: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)
    recipients: List[str] = Field(default_factory=list)
    from_address: Optional[str] = None
    enabled: Optional[bool] = True


class NotificationTestIn(BaseModel):
    """Send a test notification to validate SMTP configuration (NT-3)."""
    to_address: str = Field(..., min_length=3)


class UserPermissionsIn(BaseModel):
    """Set a user's access overrides.

    ``entity_permissions`` / ``domain_permissions`` are RESTRICTION allow-lists
    (read/write-grained). ``domain_roles`` is a CONFERRAL map ({domain: [role]})
    that GRANTS the listed roles' permissions within that domain only. Any map
    may be omitted to leave it unchanged.
    """
    entity_permissions: Optional[Dict[str, List[str]]] = None
    domain_permissions: Optional[Dict[str, List[str]]] = None
    domain_roles: Optional[Dict[str, List[str]]] = None


class FieldMappingIn(BaseModel):
    """Create/replace a source->target field mapping (EX-3)."""
    source_system: Optional[str] = None
    entity_name: str
    source_field: str = Field(..., min_length=1)
    target_field: str = Field(..., min_length=1)
    # A transform spec: a name (str), {"fn": name, ...params}, or a list of those.
    transform: Optional[Any] = None
    default_value: Optional[str] = None
    enabled: bool = True


class FieldMappingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    source_system: Optional[str]
    entity_name: str
    source_field: str
    target_field: str
    transform: Optional[Any] = None
    default_value: Optional[str] = None
    enabled: bool = True
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @field_validator("id", mode="before")
    @classmethod
    def _stringify(cls, v):
        return str(v)


class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int
    has_more: bool


class Page(BaseModel):
    meta: PageMeta
    data: List[Dict[str, Any]]
