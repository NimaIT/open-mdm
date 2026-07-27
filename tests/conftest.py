"""Test fixtures.

These tests run against a real PostgreSQL database, deliberately. The whole
product is DDL generation and transactional promotion — mocking the database
away would test almost nothing of value.

Point them at a scratch database with:
    export MDM_TEST_DSN_HOST=localhost MDM_TEST_DSN_PORT=5432 ...
or rely on the .env used for development. Each run provisions its own schemas
and drops the entities it creates.
"""
import os
import uuid

import pytest
from sqlalchemy import text

os.environ.setdefault("ENVIRONMENT", "test")

from app.config import settings  # noqa: E402
from app.db import get_ddl_engine, get_engine, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    ApiKey,
    Attribute,
    AuditEvent,
    Entity,
    EntityStatus,
    GroupRoleMapping,
    ModelVersion,
    PromotionBatch,
    Role,
    User,
    UserSource,
    WorkflowTask,
)
from app.services.auth import hash_password  # noqa: E402
from app.services.bootstrap import create_metadata_tables, create_schemas  # noqa: E402
from app.services.ddl import apply_plan, build_create_plan, build_drop_plan  # noqa: E402


def _db_available() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("select 1"))
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _db_available(),
    reason="No PostgreSQL reachable — set PGHOST/PGPORT/PGUSER/PGPASSWORD.",
)


@pytest.fixture(scope="session", autouse=True)
def _provision():
    """Ensure schemas and metadata tables exist once per session."""
    if not _db_available():
        yield
        return
    create_schemas()
    create_metadata_tables()
    # Retire orphaned active workflow tasks left by earlier test runs (their
    # entities were dropped, but the append-only event chain blocks deleting the
    # task). Terminating them — an UPDATE, which the AO-2 trigger permits — keeps
    # the shared scratch DB's active-task queries (inbox, admin workflows) clean.
    with get_engine().begin() as c:
        c.execute(
            text(
                "update mdm_meta.workflow_task t "
                "set status = 'terminated' "
                "where t.status in ('pending_review','changes_requested') "
                "and not exists (select 1 from mdm_meta.entity e "
                "                where e.name = t.entity_name)"
            )
        )
    yield


@pytest.fixture
def db():
    """A session that commits on exit.

    Note: fixtures must COMMIT rather than merely flush, because the HTTP tests
    drive the app through its own connections — uncommitted rows are invisible
    there and routes would 404 on entities the test just created.
    """
    with session_scope() as s:
        yield s


@pytest.fixture
def conn():
    with get_engine().begin() as c:
        yield c


def _attr(name, data_type="string", **kw):
    defaults = dict(
        display_name=name.replace("_", " ").title(), length=None,
        numeric_precision=None, numeric_scale=None, is_required=False,
        is_unique=False, is_business_key=False, is_match_key=False,
        is_indexed=False, is_pii=False, default_value=None, validation={},
        normalization=[], position=0,
    )
    defaults.update(kw)
    return Attribute(name=name, data_type=data_type, **defaults)


@pytest.fixture
def entity_factory(db):
    """Create a uniquely-named entity, publish its DDL, and clean up after."""
    created = []

    def _make(attributes=None, *, publish=True, **kw):
        name = kw.pop("name", None) or f"t_{uuid.uuid4().hex[:10]}"
        attrs = attributes or [
            _attr("code", "string", length=40, is_required=True, is_unique=True,
                  is_business_key=True, normalization=["trim", "upper"], position=0),
            _attr("label", "string", length=120, is_required=True,
                  is_match_key=True, position=1),
            _attr("amount", "decimal", numeric_precision=12, numeric_scale=2,
                  validation={"min": 0}, position=2),
            _attr("category", "enum", length=20,
                  validation={"enum": ["alpha", "beta"]}, position=3),
            _attr("active", "boolean", default_value="true", position=4),
        ]
        ent = Entity(
            name=name,
            display_name=kw.pop("display_name", name),
            status=EntityStatus.DRAFT.value,
            requires_approval=kw.pop("requires_approval", True),
            soft_delete=kw.pop("soft_delete", True),
            created_by="pytest", updated_by="pytest", **kw,
        )
        ent.attributes = attrs
        db.add(ent)
        db.flush()
        db.refresh(ent)
        created.append(name)

        if publish:
            with get_ddl_engine().begin() as c:
                apply_plan(c, build_create_plan(ent))
            ent.status = EntityStatus.PUBLISHED.value
            ent.published_version = ent.version
            db.flush()
        # Commit so the app's own connections can see this entity.
        db.commit()
        db.refresh(ent)
        return ent

    yield _make

    # ---- teardown
    # Release this fixture's own transaction FIRST. Opening a second session to
    # delete these rows while this one still holds them deadlocks on
    # transactionid: the delete waits forever on a lock we ourselves hold.
    try:
        db.rollback()
    except Exception:
        pass

    for name in created:
        try:
            with get_ddl_engine().begin() as c:
                apply_plan(c, build_drop_plan(name, cascade=True),
                           allow_destructive=True)
        except Exception:
            pass

    for name in created:
        # One short transaction per entity, so a single failure can't strand
        # locks for the rest of the run.
        try:
            with session_scope() as s:
                ent = s.query(Entity).filter(Entity.name == name).one_or_none()
                if ent:
                    s.query(ModelVersion).filter(
                        ModelVersion.entity_id == ent.id
                    ).delete(synchronize_session=False)
                    s.delete(ent)
                s.query(PromotionBatch).filter(
                    PromotionBatch.entity_name == name
                ).delete(synchronize_session=False)
                # audit_event and workflow_event are append-only at the DB level
                # (AO-2 trigger), so they are deliberately NOT deleted. WorkflowTask
                # rows cannot be deleted either (their immutable events reference
                # them), but they CAN be UPDATEd — so retire any leftover tasks to a
                # terminal status. That keeps active-task queries (inbox, admin
                # workflows) clean across the suite instead of accumulating stale
                # pending tasks for dropped entities.
                s.query(WorkflowTask).filter(
                    WorkflowTask.entity_name == name,
                    WorkflowTask.status.in_(["pending_review", "changes_requested"]),
                ).update({"status": "terminated"}, synchronize_session=False)
        except Exception:
            pass


@pytest.fixture
def make_attr():
    return _attr


@pytest.fixture
def users(db):
    """Two local stewards and an admin, for segregation-of-duties tests."""
    made = []

    def _make(username, roles, password="pw12345"):
        u = db.query(User).filter(User.username == username).one_or_none()
        if u is None:
            u = User(username=username, display_name=username,
                     source=UserSource.LOCAL.value,
                     password_hash=hash_password(password), roles=roles,
                     created_by="pytest")
            db.add(u)
            db.flush()
            made.append(username)
        else:
            u.roles = roles
        db.commit()
        return u

    yield _make

    try:
        db.rollback()   # release locks before deleting from another session
    except Exception:
        pass
    for username in made:
        try:
            with session_scope() as s:
                u = s.query(User).filter(User.username == username).one_or_none()
                if u:
                    s.delete(u)
        except Exception:
            pass
