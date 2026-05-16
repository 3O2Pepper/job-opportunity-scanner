"""Parse resumes and profile text; maintain merged profile snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from io import BytesIO
from pathlib import Path

from docx import Document as DocxDocument
from pypdf import PdfReader
from sqlalchemy import select

from app.db.models import ProfileDocument, ProfileSnapshot
from app.db.session import session_scope


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_text_from_pdf(data: bytes) -> str:
    reader = PdfReader(BytesIO(data))
    parts: list[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()


def extract_text_from_docx(data: bytes) -> str:
    doc = DocxDocument(BytesIO(data))
    return "\n".join(p.text for p in doc.paragraphs).strip()


def extract_text_from_upload(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        return extract_text_from_pdf(data)
    if name.endswith(".docx"):
        return extract_text_from_docx(data)
    if name.endswith(".txt"):
        return data.decode("utf-8", errors="replace").strip()
    raise ValueError(f"Unsupported file type: {filename}")


_SIMPLE_SKILL_SPLIT = re.compile(r"[,;/•\n]")


def infer_skills_from_text(text: str, max_skills: int = 80) -> list[str]:
    """Very simple skill tokenization for deterministic scoring."""
    lower = text.lower()
    found: set[str] = set()
    # Common resume section heuristic
    for line in lower.splitlines():
        line = line.strip()
        if not line:
            continue
        if any(h in line for h in ("skills", "tools", "software", "technologies")):
            for chunk in _SIMPLE_SKILL_SPLIT.split(line):
                chunk = chunk.strip()
                if 2 <= len(chunk) <= 40:
                    found.add(chunk)
    # Fallback: keywords that look like tools (caps / mixed)
    for word in re.findall(r"\b[A-Z]{2,10}\b", text):
        found.add(word.lower())
    return sorted(found)[:max_skills]


def infer_years_experience_band(text: str) -> str:
    t = text.lower()
    if any(x in t for x in ("intern", "co-op", "coop", "student")):
        return "intern"
    if any(x in t for x in ("new grad", "recent graduate", "entry level")):
        return "entry"
    m = re.search(r"(\d+)\s*\+\s*years", t)
    if m and int(m.group(1)) <= 2:
        return "early"
    return "general"


def build_structured_profile(merged_text: str) -> dict:
    return {
        "skills": infer_skills_from_text(merged_text),
        "experience_band": infer_years_experience_band(merged_text),
        "raw_length": len(merged_text),
    }


def merge_profile_documents(docs: list[ProfileDocument]) -> str:
    blocks: list[str] = []
    for d in sorted(docs, key=lambda x: (x.kind, x.id)):
        label = d.kind
        if d.title:
            label = f"{d.kind}: {d.title}"
        blocks.append(f"### {label}\n{d.content_text.strip()}")
    return "\n\n".join(blocks).strip()


def refresh_profile_snapshot(session) -> ProfileSnapshot:
    docs = session.scalars(select(ProfileDocument).order_by(ProfileDocument.id.asc())).all()
    merged = merge_profile_documents(docs) if docs else ""
    structured = build_structured_profile(merged) if merged else {}
    snap = session.scalar(
        select(ProfileSnapshot).order_by(ProfileSnapshot.id.desc()).limit(1)
    )
    if snap is None:
        snap = ProfileSnapshot(merged_text=merged, structured_json=json.dumps(structured))
        session.add(snap)
    else:
        snap.merged_text = merged
        snap.structured_json = json.dumps(structured)
    session.commit()
    session.refresh(snap)
    return snap


def save_profile_text(kind: str, content: str, title: str | None = None) -> ProfileDocument:
    content = content.strip()
    if not content:
        raise ValueError("Empty content")
    h = _sha256(content)
    with session_scope() as session:
        existing = session.scalar(
            select(ProfileDocument)
            .where(
                ProfileDocument.content_hash == h,
                ProfileDocument.kind == kind,
            )
            .limit(1)
        )
        if existing:
            refresh_profile_snapshot(session)
            return existing
        doc = ProfileDocument(kind=kind, title=title, content_text=content, content_hash=h)
        session.add(doc)
        session.commit()
        session.refresh(doc)
        refresh_profile_snapshot(session)
        return doc


def save_profile_file(filename: str, data: bytes) -> ProfileDocument:
    text = extract_text_from_upload(filename, data)
    title = Path(filename).name
    return save_profile_text("resume_file", text, title=title)
