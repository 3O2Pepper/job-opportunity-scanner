"""SQLAlchemy ORM models."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ProfileDocKind(str, enum.Enum):
    resume_file = "resume_file"
    resume_text = "resume_text"
    linkedin_export = "linkedin_export"
    preferences = "preferences"


class JobStatus(str, enum.Enum):
    not_applied = "not_applied"
    applied = "applied"
    saved = "saved"
    rejected = "rejected"
    not_interested = "not_interested"


class WorkMode(str, enum.Enum):
    remote = "remote"
    hybrid = "hybrid"
    onsite = "onsite"
    unknown = "unknown"


class SourceType(str, enum.Enum):
    gmail = "gmail"
    manual_link = "manual_link"
    manual_text = "manual_text"
    rss = "rss"


class ProfileDocument(Base):
    __tablename__ = "profile_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ProfileSnapshot(Base):
    __tablename__ = "profile_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merged_text: Mapped[str] = mapped_column(Text, nullable=False)
    structured_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gmail_message_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    from_addr: Mapped[str | None] = mapped_column(String(512), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_headers_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    labels: Mapped[str | None] = mapped_column(Text, nullable=True)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dedupe_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    company: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    location: Mapped[str | None] = mapped_column(String(512), nullable=True)
    work_mode: Mapped[str] = mapped_column(String(32), default=WorkMode.unknown.value)
    job_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_ref: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    found_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    required_skills_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    preferred_skills_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    years_experience_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    years_experience_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    degree_requirements: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    deadline: Mapped[str | None] = mapped_column(String(128), nullable=True)
    raw_description_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_debug_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    score_total: Mapped[float | None] = mapped_column(nullable=True, index=True)
    score_breakdown_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommendation_tier: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    missing_qualifications_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_keywords_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_bullet_points_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    application_difficulty: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(String(32), default=JobStatus.not_applied.value, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    email_message_id: Mapped[int | None] = mapped_column(ForeignKey("email_messages.id"), nullable=True)
    email_message: Mapped[EmailMessage | None] = relationship()

    __table_args__ = (UniqueConstraint("dedupe_hash", name="uq_jobs_dedupe_hash"),)


class SyncState(Base):
    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    last_history_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    query_used: Mapped[str | None] = mapped_column(Text, nullable=True)


Index("ix_jobs_company_title", Job.company, Job.title)
