"""Export jobs to Markdown, CSV, or PDF."""

from __future__ import annotations

import json
import re
from datetime import datetime
from io import BytesIO

import pandas as pd
from fpdf import FPDF
from fpdf.enums import WrapMode, XPos, YPos
from sqlalchemy import select

from app.db.models import Job, ProfileSnapshot
from app.db.session import session_scope
from app.services.scoring import alignment_summary, scoring_context_blob


def jobs_to_dataframe(jobs: list[Job]) -> pd.DataFrame:
    with session_scope() as session:
        prof = session.scalar(
            select(ProfileSnapshot).order_by(ProfileSnapshot.id.desc()).limit(1)
        )
        rows: list[dict] = []
        for job in jobs:
            missing = []
            kw = []
            bullets = []
            try:
                if job.missing_qualifications_json:
                    missing = json.loads(job.missing_qualifications_json)
                if job.resume_keywords_json:
                    kw = json.loads(job.resume_keywords_json)
                if job.cover_bullet_points_json:
                    bullets = json.loads(job.cover_bullet_points_json)
            except json.JSONDecodeError:
                pass
            dbg_subj = ""
            dbg_snip = ""
            dbg_parser = ""
            dbg_vendor = ""
            if job.extraction_debug_json:
                try:
                    d = json.loads(job.extraction_debug_json)
                    dbg_subj = str(d.get("email_subject") or "")
                    dbg_snip = str(d.get("email_snippet") or "")[:600]
                    dbg_vendor = str(d.get("classified_vendor") or "")
                    cand = d.get("candidate") or {}
                    dbg_parser = str(cand.get("parser") or "")
                except json.JSONDecodeError:
                    pass
            score_why = ""
            if job.score_breakdown_json:
                try:
                    b = json.loads(job.score_breakdown_json)
                    score_why = str(b.get("explain_summary") or "")
                except json.JSONDecodeError:
                    pass
            rows.append(
                {
                    "id": job.id,
                    "found_at": job.found_at.isoformat() if job.found_at else "",
                    "source_type": job.source_type,
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "work_mode": job.work_mode,
                    "job_url": job.job_url,
                    "industry": job.industry,
                    "score_total": job.score_total,
                    "recommendation_tier": job.recommendation_tier,
                    "status": job.status,
                    "application_difficulty": job.application_difficulty,
                    "email_subject": dbg_subj,
                    "email_snippet": dbg_snip,
                    "classified_vendor": dbg_vendor,
                    "extraction_parser": dbg_parser,
                    "score_reason_summary": score_why,
                    "scoring_context_preview": scoring_context_blob(job)[:1200],
                    "extraction_debug_json": (job.extraction_debug_json or "")[:4000],
                    "missing_qualifications": "; ".join(str(x) for x in missing),
                    "resume_keywords": "; ".join(str(x) for x in kw),
                    "cover_bullet_points": " | ".join(str(x) for x in bullets),
                    "goal_alignment": alignment_summary(job, prof),
                    "raw_snippet": (job.raw_description_text or "")[:800],
                }
            )
    return pd.DataFrame(rows)


def export_csv_bytes(jobs: list[Job]) -> bytes:
    df = jobs_to_dataframe(jobs)
    buf = BytesIO()
    df.to_csv(buf, index=False)
    return buf.getvalue()


def export_markdown(jobs: list[Job], title: str = "Job scan report") -> str:
    with session_scope() as session:
        prof = session.scalar(
            select(ProfileSnapshot).order_by(ProfileSnapshot.id.desc()).limit(1)
        )
        lines = [
            f"# {title}",
            "",
            f"_Generated {datetime.utcnow().isoformat()} UTC_",
            "",
        ]
        for job in jobs:
            tier = job.recommendation_tier or ""
            score = job.score_total if job.score_total is not None else 0.0
            lines.extend(
                [
                    f"## {job.title or 'Untitled'} — {job.company or 'Unknown company'}",
                    "",
                    f"- **Score:** {score:.1f} ({tier})",
                    f"- **Location / mode:** {job.location or 'n/a'} / {job.work_mode}",
                    f"- **Source:** {job.source_type}",
                    f"- **Link:** {job.job_url or 'n/a'}",
                    f"- **Application difficulty:** {job.application_difficulty or 'n/a'}",
                    "",
                ]
            )
            if job.score_breakdown_json:
                try:
                    bd = json.loads(job.score_breakdown_json)
                    if bd.get("explain_summary"):
                        lines.append(f"_Why this score:_ {bd['explain_summary']}")
                        lines.append("")
                    lines.append("_Score breakdown:_")
                    for k, v in bd.items():
                        if isinstance(v, dict) and "score" in v:
                            lines.append(
                                f"- {k.replace('_', ' ')}: **{v['score']}** "
                                f"(weight {v['weight']})"
                            )
                    lines.append("")
                except json.JSONDecodeError:
                    pass
            lines.append(f"_Goal alignment:_ {alignment_summary(job, prof)}")
            lines.append(
                f"_Scoring context preview:_ `{scoring_context_blob(job)[:520].replace(chr(10), ' / ')}`"
            )
            lines.append("")
            if job.missing_qualifications_json:
                try:
                    mq = json.loads(job.missing_qualifications_json)
                    if mq:
                        lines.append("**Missing / weak qualifications:**")
                        for m in mq:
                            lines.append(f"- {m}")
                        lines.append("")
                except json.JSONDecodeError:
                    pass
            if job.resume_keywords_json:
                try:
                    rk = json.loads(job.resume_keywords_json)
                    if rk:
                        lines.append("**Suggested resume keywords:**")
                        lines.append(", ".join(str(x) for x in rk))
                        lines.append("")
                except json.JSONDecodeError:
                    pass
            if job.cover_bullet_points_json:
                try:
                    cb = json.loads(job.cover_bullet_points_json)
                    if cb:
                        lines.append("**Cover letter bullets:**")
                        for b in cb:
                            lines.append(f"- {b}")
                        lines.append("")
                except json.JSONDecodeError:
                    pass
            if job.extraction_debug_json:
                try:
                    exd = json.loads(job.extraction_debug_json)
                    lines.append("**Extraction debug (from email):**")
                    lines.append(f"- Subject: `{exd.get('email_subject', '')}`")
                    lines.append(f"- Snippet: `{str(exd.get('email_snippet', ''))[:400]}`")
                    lines.append(f"- Classified vendor: `{exd.get('classified_vendor', '')}`")
                    cand = exd.get("candidate") or {}
                    lines.append(
                        f"- Parsed fields: title=`{cand.get('title')}` company=`{cand.get('company')}` "
                        f"location=`{cand.get('location')}` url=`{cand.get('job_url')}` parser=`{cand.get('parser')}`"
                    )
                    lines.append("")
                except json.JSONDecodeError:
                    pass
            lines.append("---")
            lines.append("")
    return "\n".join(lines)


class _ReportPdf(FPDF):
    def header(self) -> None:
        self.set_font("Helvetica", "B", 12)
        self.cell(self.epw, 10, "Job scan report", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(4)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_x(self.l_margin)
        self.set_font("Helvetica", "I", 8)
        self.cell(self.epw, 10, f"Page {self.page_no()}", align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


_UNICODE_REPLACEMENTS: dict[str, str] = {
    "\u2014": "-",
    "\u2013": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2026": "...",
    "\u00a0": " ",
}

_SOFT_HYPHEN = "\u00ad"
_MAX_UNBROKEN_RUN = 100


def _normalize_pdf_text(text: object) -> str:
    """Make arbitrary job text safe for core Helvetica PDF fonts."""
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    elif not isinstance(text, str):
        text = str(text)

    for src, dst in _UNICODE_REPLACEMENTS.items():
        text = text.replace(src, dst)

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    text = text.encode("latin-1", errors="replace").decode("latin-1")
    return _insert_pdf_break_opportunities(text)


def _insert_pdf_break_opportunities(text: str) -> str:
    """Insert soft hyphens so WORD wrap can break URLs and very long tokens."""
    text = re.sub(r"([/?&=#])", rf"\1{_SOFT_HYPHEN}", text)

    def _split_long_run(match: re.Match[str]) -> str:
        token = match.group(0)
        return _SOFT_HYPHEN.join(
            token[i : i + _MAX_UNBROKEN_RUN] for i in range(0, len(token), _MAX_UNBROKEN_RUN)
        )

    return re.sub(rf"\S{{{_MAX_UNBROKEN_RUN + 1},}}", _split_long_run, text)


def _pdf_multi_cell(
    pdf: FPDF,
    text: object,
    line_height: float,
    *,
    style: str = "",
    size: int | None = None,
) -> None:
    """Render a full-width text block using fpdf2-safe cursor and width settings."""
    normalized = _normalize_pdf_text(text)
    if not normalized.strip():
        return

    if size is not None:
        pdf.set_font("Helvetica", style=style, size=size)
    elif style:
        pdf.set_font("Helvetica", style=style)

    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(
        pdf.epw,
        line_height,
        normalized,
        new_x=XPos.LMARGIN,
        new_y=YPos.NEXT,
        wrapmode=WrapMode.WORD,
    )


def export_pdf_bytes(jobs: list[Job]) -> bytes:
    pdf = _ReportPdf()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    _pdf_multi_cell(
        pdf,
        f"Generated {datetime.utcnow().isoformat()} UTC\nJobs: {len(jobs)}",
        6,
    )
    pdf.ln(4)

    for job in jobs:
        _pdf_multi_cell(
            pdf,
            f"{job.title or 'Untitled'} — {job.company or 'Unknown'}",
            6,
            style="B",
            size=11,
        )
        score = job.score_total if job.score_total is not None else 0.0
        body = (
            f"Score: {score:.1f} ({job.recommendation_tier})\n"
            f"Location: {job.location or 'n/a'} | Mode: {job.work_mode}\n"
            f"Link: {job.job_url or 'n/a'}\n"
            f"Difficulty: {job.application_difficulty or 'n/a'}\n"
        )
        _pdf_multi_cell(pdf, body, 5, size=10)
        snippet = (job.raw_description_text or "")[:600].replace("\r", " ")
        _pdf_multi_cell(pdf, f"Snippet: {snippet}", 5, style="I", size=9)
        pdf.ln(3)

    data = pdf.output()
    if isinstance(data, str):
        return data.encode("latin-1")
    return bytes(data)
