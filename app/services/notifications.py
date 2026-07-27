"""Workflow-transition notifications (Workstream 4).

A notification is *enqueued* — rendered and persisted as a ``Notification`` row
(the dev outbox and the delivery queue in one) — after a workflow transition has
already committed. A separate ``dispatch`` step delivers queued rows via the
configured transport.

Safety contract (critical):
  * ``enqueue_notification`` runs AFTER the decision transaction committed and is
    called wrapped in a swallow-all guard at every call site: a notification
    failure can never break, block or roll back a workflow transition.
  * The default transport (``outbox``) performs NO SMTP / network I/O, so tests
    and local runs never touch the network.
  * ``mdm_meta.*`` tables are accessed through the ORM only; rendering uses a
    safe ``str.format`` substitution (never ``eval``) that tolerates missing keys.
"""
import logging
import smtplib
import string
from datetime import datetime, timezone
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Notification, NotificationTemplate, User
from app.services import workflow
from app.services.auth import effective_permissions

log = logging.getLogger(__name__)

# ---- events (mirror the workflow transitions notifications fire at)
EVENT_SUBMITTED = "submitted"
EVENT_CHANGES_REQUESTED = "changes_requested"
EVENT_REJECTED = "rejected"
EVENT_APPROVED = "approved"
EVENT_TERMINATED = "terminated"

EVENTS = (
    EVENT_SUBMITTED,
    EVENT_CHANGES_REQUESTED,
    EVENT_REJECTED,
    EVENT_APPROVED,
    EVENT_TERMINATED,
)

# Statuses on a Notification row.
STATUS_QUEUED = "queued"
STATUS_SENT = "sent"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# Transports.
TRANSPORT_OUTBOX = "outbox"
TRANSPORT_SMTP = "smtp"
TRANSPORT_BOTH = "both"


# ---------------------------------------------------------- built-in templates
# Sensible defaults used when no (domain, event) row and no global-default row
# exists. Substitution context keys: entity, domain, staging_id, record_id,
# actor, comment, deep_link, change_type.
DEFAULT_TEMPLATES: Dict[str, Dict[str, str]] = {
    EVENT_SUBMITTED: {
        "subject": "[MDM] Review needed: {entity} #{staging_id}",
        "body": (
            "A {change_type} change to '{entity}' (domain: {domain}) was submitted "
            "by {actor} and is awaiting review.\n\n"
            "Open the review task: {deep_link}"
        ),
    },
    EVENT_CHANGES_REQUESTED: {
        "subject": "[MDM] Changes requested: {entity} #{staging_id}",
        "body": (
            "{actor} requested changes on your '{entity}' change request "
            "#{staging_id}.\n\nComment: {comment}\n\n"
            "Edit and resubmit it: {deep_link}"
        ),
    },
    EVENT_REJECTED: {
        "subject": "[MDM] Rejected: {entity} #{staging_id}",
        "body": (
            "Your '{entity}' change request #{staging_id} was rejected by "
            "{actor}.\n\nReason: {comment}\n\nDetails: {deep_link}"
        ),
    },
    EVENT_APPROVED: {
        "subject": "[MDM] Approved: {entity} #{staging_id}",
        "body": (
            "Your '{entity}' change request #{staging_id} was approved and applied "
            "to the golden record by {actor}.\n\nNote: {comment}\n\n"
            "View the record: {deep_link}"
        ),
    },
    EVENT_TERMINATED: {
        "subject": "[MDM] Terminated: {entity} #{staging_id}",
        "body": (
            "The '{entity}' change request #{staging_id} was force-closed "
            "(terminated) by {actor}.\n\nReason: {comment}\n\nDetails: {deep_link}"
        ),
    },
}


# ------------------------------------------------------------------ rendering
class _SafeDict(dict):
    """A format mapping that leaves unknown ``{key}`` placeholders untouched
    rather than raising ``KeyError``."""

    def __missing__(self, key):  # pragma: no cover - trivial
        return "{" + key + "}"


def render(template_str: Optional[str], context: Dict[str, object]) -> str:
    """Safe ``str.format``-style substitution. Never ``eval``; never crashes on a
    missing key or a malformed placeholder (falls back to the raw template)."""
    if not template_str:
        return ""
    try:
        return string.Formatter().vformat(template_str, (), _SafeDict(context))
    except Exception:  # noqa: BLE001 - a bad template must never break enqueue
        log.warning("notification template render failed; using raw template")
        return template_str


def build_deep_link(entity_name: str, staging_id: Optional[int]) -> str:
    """A stable deep-link back to the review task for a record.

    Route (honoured by the UI workstream): ``/review/{entity}?staging={id}``.
    """
    base = (settings.APP_BASE_URL or "").rstrip("/")
    if staging_id is None:
        return f"{base}/review/{entity_name}"
    return f"{base}/review/{entity_name}?staging={staging_id}"


# ------------------------------------------------------------ template lookup
class _ResolvedTemplate:
    __slots__ = ("subject", "body", "recipients", "from_address", "enabled", "source")

    def __init__(self, subject, body, recipients, from_address, enabled, source):
        self.subject = subject
        self.body = body
        self.recipients = recipients or []
        self.from_address = from_address
        self.enabled = enabled
        self.source = source


def resolve_template(db: Session, domain: Optional[str], event: str) -> _ResolvedTemplate:
    """Pick the template for (domain, event): a domain-specific row wins, then the
    global-default row (domain IS NULL), then the built-in default."""
    row = None
    if domain:
        row = (
            db.query(NotificationTemplate)
            .filter(NotificationTemplate.domain == domain,
                    NotificationTemplate.event == event)
            .one_or_none()
        )
    if row is None:
        row = (
            db.query(NotificationTemplate)
            .filter(NotificationTemplate.domain.is_(None),
                    NotificationTemplate.event == event)
            .one_or_none()
        )
    if row is not None:
        return _ResolvedTemplate(
            row.subject, row.body, list(row.recipients or []),
            row.from_address, row.enabled,
            "domain" if row.domain else "global",
        )
    default = DEFAULT_TEMPLATES.get(event, {})
    return _ResolvedTemplate(
        default.get("subject", "[MDM] {entity} #{staging_id}"),
        default.get("body", "{deep_link}"),
        [], None, True, "builtin",
    )


# --------------------------------------------------------- recipient resolution
def _domain_approvers(db: Session, domain: Optional[str]) -> List[str]:
    """Emails of active users who can approve in ``domain`` — global
    admin/steward/approver roles OR a domain_roles conferral on that domain."""
    out: List[str] = []
    users = db.query(User).filter(User.is_active.is_(True)).all()
    for u in users:
        if not u.email:
            continue
        if "staging:approve" in effective_permissions(u, domain):
            out.append(u.email)
    return out


def _email_for(db: Session, username: Optional[str]) -> Optional[str]:
    if not username:
        return None
    u = db.query(User).filter(User.username == username).one_or_none()
    return u.email if u and u.email else None


def resolve_recipients(
    db: Session, *, event: str, template: _ResolvedTemplate, domain: Optional[str],
    submitter: Optional[str], assignee: Optional[str] = None,
) -> List[str]:
    """Explicit template recipients if set; else resolve by role for the domain.

    ``submitted`` -> approvers/stewards for the domain; the submitter-facing
    events -> the submitter (plus the assignee for ``terminated``).
    """
    if template.recipients:
        return list(dict.fromkeys(template.recipients))

    emails: List[str] = []
    if event == EVENT_SUBMITTED:
        emails = _domain_approvers(db, domain)
    else:
        sub = _email_for(db, submitter)
        if sub:
            emails.append(sub)
        if event == EVENT_TERMINATED:
            asg = _email_for(db, assignee)
            if asg:
                emails.append(asg)
    # De-duplicate, preserve order.
    return list(dict.fromkeys(emails))


# ------------------------------------------------------------------- enqueue
def enqueue_notification(
    db: Session,
    *,
    event: str,
    entity,
    staging_id: Optional[int],
    task_id: Optional[str] = None,
    actor: Optional[str] = None,
    comment: Optional[str] = None,
    change_type: Optional[str] = None,
    record_id: Optional[str] = None,
) -> Optional[Notification]:
    """Resolve template + recipients + deep-link, render, and INSERT a
    ``Notification`` row (status ``queued``, or ``skipped`` when disabled / no
    recipients). Returns the row, or ``None`` when notifications are globally off.

    MUST be called AFTER the decision transaction has committed, and wrapped in a
    swallow-all guard by the caller — a failure here must never affect the
    transition (SAFETY RULE 1). Performs no network I/O (SAFETY RULE 2): delivery
    is a separate ``dispatch`` step.
    """
    if not settings.NOTIFICATIONS_ENABLED:
        return None

    entity_name = getattr(entity, "name", None)
    domain = getattr(entity, "domain", None)

    # Load the task to recover the submitter / assignee for recipient resolution.
    submitter = assignee = None
    resolved_task_id = task_id
    if entity_name is not None and staging_id is not None:
        task = workflow.get_task(db, entity_name, staging_id)
        if task is not None:
            submitter = task.submitted_by
            assignee = task.assigned_to or task.claimed_by
            resolved_task_id = resolved_task_id or str(task.id)

    template = resolve_template(db, domain, event)
    deep_link = build_deep_link(entity_name or "", staging_id)
    recipients = resolve_recipients(
        db, event=event, template=template, domain=domain,
        submitter=submitter, assignee=assignee,
    )

    context = {
        "entity": entity_name or "",
        "domain": domain or "",
        "staging_id": staging_id if staging_id is not None else "",
        "record_id": record_id or "",
        "actor": actor or "",
        "comment": comment or "",
        "deep_link": deep_link,
        "change_type": change_type or "",
    }
    subject = render(template.subject, context)
    body = render(template.body, context)
    from_address = template.from_address or settings.NOTIFICATION_FROM

    if not template.enabled:
        status = STATUS_SKIPPED
    elif not recipients:
        status = STATUS_SKIPPED
    else:
        status = STATUS_QUEUED

    notif = Notification(
        event=event,
        domain=domain,
        entity_name=entity_name,
        staging_id=staging_id,
        task_id=resolved_task_id,
        to_addresses=recipients,
        from_address=from_address,
        subject=subject,
        body=body,
        deep_link=deep_link,
        status=status,
    )
    db.add(notif)
    db.flush()
    # PII / recipient metadata is not logged at INFO (SAFETY RULE 3).
    log.debug("enqueued notification event=%s entity=%s staging=%s status=%s",
              event, entity_name, staging_id, status)
    return notif


def safe_enqueue(db: Session, **kwargs) -> Optional[Notification]:
    """Swallow-all wrapper around :func:`enqueue_notification` for use at the
    endpoint layer AFTER a transition has committed (SAFETY RULE 1).

    Any exception — including a failure inside ``enqueue_notification`` — is
    logged and swallowed so a notification problem can never break, block or roll
    back the workflow transition that just succeeded. Looks the target function up
    on the module at call time so tests can monkeypatch it.
    """
    try:
        return enqueue_notification(db, **kwargs)
    except Exception:  # noqa: BLE001 - notifications must never break a transition
        log.warning(
            "notification enqueue failed for event=%s (swallowed)",
            kwargs.get("event"),
        )
        return None


# ------------------------------------------------------------------ transports
class OutboxTransport:
    """Dev transport: the row is already persisted, so 'delivery' is a no-op.
    Performs NO network I/O."""

    def send(self, notif: Notification) -> None:
        return None


class SMTPTransport:
    """Real delivery over SMTP using the per-environment service account."""

    def send(self, notif: Notification) -> None:
        host = settings.SMTP_HOST
        if not host:
            raise RuntimeError(
                "SMTP transport selected but SMTP_HOST is not configured."
            )
        recipients = list(notif.to_addresses or [])
        if not recipients:
            raise RuntimeError("Notification has no recipients.")

        msg = MIMEText(notif.body or "", _charset="utf-8")
        msg["Subject"] = notif.subject or ""
        msg["From"] = notif.from_address or settings.NOTIFICATION_FROM
        msg["To"] = ", ".join(recipients)

        smtp = smtplib.SMTP(host, settings.SMTP_PORT, timeout=settings.SMTP_TIMEOUT)
        try:
            smtp.ehlo()
            if settings.SMTP_USE_TLS:
                smtp.starttls()
                smtp.ehlo()
            if settings.SMTP_USERNAME:
                smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD or "")
            smtp.sendmail(msg["From"], recipients, msg.as_string())
        finally:
            try:
                smtp.quit()
            except Exception:  # noqa: BLE001
                pass


def _transport_for(name: str):
    if name in (TRANSPORT_SMTP, TRANSPORT_BOTH):
        return SMTPTransport()
    return OutboxTransport()


# ------------------------------------------------------------------- dispatch
def dispatch(
    db: Session,
    notification_ids: Optional[Sequence[str]] = None,
    *,
    transport: Optional[str] = None,
) -> Dict[str, int]:
    """Deliver ``queued`` / ``failed`` notifications per the configured transport.

    ``outbox`` marks rows ``sent`` with no network I/O (delivered-to-outbox);
    ``smtp`` / ``both`` actually send via SMTP, marking ``sent`` or ``failed``
    (+error) and incrementing ``attempts``. Returns per-status counts.
    """
    transport_name = transport or settings.NOTIFICATION_TRANSPORT
    tx = _transport_for(transport_name)

    q = db.query(Notification).filter(
        Notification.status.in_([STATUS_QUEUED, STATUS_FAILED])
    )
    if notification_ids:
        q = q.filter(Notification.id.in_(list(notification_ids)))
    rows = q.order_by(Notification.created_at.asc()).all()

    counts = {"sent": 0, "failed": 0, "skipped": 0, "total": len(rows)}
    now = datetime.now(timezone.utc)
    for notif in rows:
        if not (notif.to_addresses or []):
            notif.status = STATUS_SKIPPED
            counts["skipped"] += 1
            continue
        notif.attempts = (notif.attempts or 0) + 1
        try:
            tx.send(notif)
            notif.status = STATUS_SENT
            notif.sent_at = now
            notif.error = None
            counts["sent"] += 1
        except Exception as exc:  # noqa: BLE001 - record, never propagate
            notif.status = STATUS_FAILED
            notif.error = str(exc)
            counts["failed"] += 1
            log.warning("notification %s delivery failed: %s", notif.id, exc)
    db.flush()
    return counts


def send_test(db: Session, to_address: str) -> Notification:
    """Create and attempt to deliver a one-off test notification via SMTP, so an
    operator can validate the SMTP service-account configuration."""
    notif = Notification(
        event="test",
        to_addresses=[to_address],
        from_address=settings.NOTIFICATION_FROM,
        subject="[MDM] Test notification",
        body="This is a test notification confirming SMTP configuration.",
        deep_link=build_deep_link("_test", None),
        status=STATUS_QUEUED,
    )
    db.add(notif)
    db.flush()
    notif.attempts = 1
    try:
        SMTPTransport().send(notif)
        notif.status = STATUS_SENT
        notif.sent_at = datetime.now(timezone.utc)
    except Exception as exc:  # noqa: BLE001
        notif.status = STATUS_FAILED
        notif.error = str(exc)
    db.flush()
    return notif


def to_dict(notif: Notification) -> Dict[str, object]:
    return {
        "id": str(notif.id),
        "created_at": notif.created_at,
        "event": notif.event,
        "domain": notif.domain,
        "entity_name": notif.entity_name,
        "staging_id": notif.staging_id,
        "task_id": str(notif.task_id) if notif.task_id else None,
        "to_addresses": notif.to_addresses,
        "from_address": notif.from_address,
        "subject": notif.subject,
        "body": notif.body,
        "deep_link": notif.deep_link,
        "status": notif.status,
        "sent_at": notif.sent_at,
        "error": notif.error,
        "attempts": notif.attempts,
    }
