"""Synchronize Gmail messages and persist email metadata."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from email.utils import parsedate_to_datetime

from sqlalchemy import select

from app.config import settings
from app.db.models import EmailMessage, SyncState
from app.services.gmail_client import build_gmail_service
from app.services.job_extract import ingest_email_message

# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------

_BASE_SENDERS = (
    "from:jobs-listings@linkedin.com OR from:linkedin.com OR from:indeed.com "
    "OR from:glassdoor.com OR from:handshake.com"
)

_ROLE_FOCUS_TERMS: dict[str, str] = {
    "Internships": (
        "subject:(internship) OR subject:(intern) OR subject:(co-op) "
        "OR subject:(coop) OR subject:(summer) OR subject:(student) "
        "OR subject:(early talent) OR subject:(job alert) OR subject:(career)"
    ),
    "Entry-level": (
        "subject:(entry level) OR subject:(new grad) OR subject:(early career) "
        "OR subject:(associate) OR subject:(junior) OR subject:(job alert) OR subject:(career)"
    ),
    "Any role": (
        "subject:(job alert) OR subject:(career) OR subject:(opportunity) "
        "OR subject:(position) OR subject:(opening) OR subject:(internship)"
    ),
}


def build_gmail_query(
    days: int = 30,
    role_focus: str = "Internships",
    override: str | None = None,
) -> str:
    """Build a Gmail search query from scan parameters.

    Parameters
    ----------
    days:
        How many days back to scan (7–180). Defaults to 30.
    role_focus:
        One of ``"Internships"``, ``"Entry-level"``, or ``"Any role"``.
    override:
        If non-empty, returned verbatim (advanced query override).

    Returns
    -------
    str
        A valid Gmail search query string.
    """
    if override and override.strip():
        return override.strip()

    days = max(7, min(180, int(days)))
    focus = _ROLE_FOCUS_TERMS.get(role_focus, _ROLE_FOCUS_TERMS["Internships"])
    return f"({_BASE_SENDERS} OR {focus}) newer_than:{days}d"


def _decode_body_part(part: dict) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    padded = data.replace("-", "+").replace("_", "/")
    pad_len = (4 - len(padded) % 4) % 4
    padded += "=" * pad_len
    return base64.b64decode(padded).decode("utf-8", errors="replace")


def extract_plain_and_html(payload: dict) -> tuple[str, str]:
    """Walk MIME tree for text/plain and text/html."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict) -> None:
        mime = part.get("mimeType", "")
        body_data = part.get("body", {}).get("data")
        if body_data:
            text = _decode_body_part(part)
            if mime == "text/plain":
                plain_parts.append(text)
            elif mime == "text/html":
                html_parts.append(text)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    return "\n".join(plain_parts), "\n".join(html_parts)


def _header(headers: list[dict], name: str) -> str | None:
    name_l = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_l:
            return h.get("value")
    return None


def upsert_email_row(session, gmail_msg_meta: dict, full_msg: dict) -> EmailMessage:
    mid = gmail_msg_meta["id"]
    payload = full_msg.get("payload", {})
    headers = payload.get("headers", [])
    from_addr = _header(headers, "From")
    subject = _header(headers, "Subject")
    date_hdr = _header(headers, "Date")
    received_at = None
    if date_hdr:
        try:
            received_at = parsedate_to_datetime(date_hdr)
            if received_at and received_at.tzinfo:
                received_at = received_at.astimezone(tz=None).replace(tzinfo=None)
        except (TypeError, ValueError, OverflowError):
            received_at = None

    snippet = full_msg.get("snippet")
    label_ids = full_msg.get("labelIds") or []
    labels_json = json.dumps(label_ids)

    existing = session.scalar(select(EmailMessage).where(EmailMessage.gmail_message_id == mid))
    if existing:
        existing.from_addr = from_addr
        existing.subject = subject
        existing.received_at = received_at
        existing.snippet = snippet
        existing.labels = labels_json
        existing.thread_id = full_msg.get("threadId")
        session.commit()
        session.refresh(existing)
        return existing

    row = EmailMessage(
        gmail_message_id=mid,
        thread_id=full_msg.get("threadId"),
        from_addr=from_addr,
        subject=subject,
        received_at=received_at,
        snippet=snippet,
        raw_headers_json=json.dumps(headers),
        labels=labels_json,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def update_sync_state(session, query: str, history_id: str | None = None) -> None:
    row = session.scalar(select(SyncState).order_by(SyncState.id.desc()).limit(1))
    if row is None:
        row = SyncState(last_history_id=history_id, last_sync_at=datetime.utcnow(), query_used=query)
        session.add(row)
    else:
        row.last_history_id = history_id or row.last_history_id
        row.last_sync_at = datetime.utcnow()
        row.query_used = query
    session.commit()


def sync_gmail_for_jobs(
    session,
    *,
    query: str | None = None,
    max_results: int | None = None,
    interactive_oauth: bool = False,
) -> tuple[int, int, int]:
    """
    List messages matching query, fetch bodies, store EmailMessage rows, extract Jobs.
    Returns (messages_synced, jobs_created, messages_skipped).
    """
    q = query if query is not None else settings.gmail_query
    max_res = max_results if max_results is not None else settings.gmail_sync_max_results
    service = build_gmail_service(interactive=interactive_oauth)

    resp = service.users().messages().list(userId="me", q=q, maxResults=max_res).execute()
    messages = resp.get("messages", []) or []

    synced = 0
    jobs_created = 0
    skipped = 0
    last_history_id = resp.get("historyId")

    for m in messages:
        mid = m.get("id")
        if not mid:
            skipped += 1
            continue
        try:
            full = (
                service.users()
                .messages()
                .get(userId="me", id=mid, format="full")
                .execute()
            )
            payload = full.get("payload", {})
            plain, html = extract_plain_and_html(payload)
            email_row = upsert_email_row(session, m, full)
            created = ingest_email_message(
                session,
                email_row=email_row,
                subject=email_row.subject or "",
                plain_body=plain,
                html_body=html,
            )
            jobs_created += created
            synced += 1
        except Exception:
            skipped += 1
            continue

    update_sync_state(session, q, history_id=str(last_history_id) if last_history_id else None)
    return synced, jobs_created, skipped
