"""Pydantic request/response schemas."""
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    requires_approval: bool = True
    soft_delete: bool = True
    auto_approve_threshold: Optional[int] = None
    retention_days: Optional[int] = None
    attributes: List[AttributeIn] = Field(default_factory=list)


class EntityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    display_name: Optional[str]
    description: Optional[str]
    domain: Optional[str]
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


class GroupMappingIn(BaseModel):
    group_dn: str
    role: str
    description: Optional[str] = None


class PageMeta(BaseModel):
    total: int
    limit: int
    offset: int
    has_more: bool


class Page(BaseModel):
    meta: PageMeta
    data: List[Dict[str, Any]]
