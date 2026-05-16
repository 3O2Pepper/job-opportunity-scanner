"""Export jobs to Markdown, CSV, or PDF."""

from __future__ import annotations

import json
from datetime import datetime
from io import BytesIO

import pandas as pd
from fpdf import FPDF
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
        self.cell(0, 10, "Job scan report", new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")


def _ascii_pdf_text(text: str) -> str:
    return (text or "").encode("latin-1", errors="replace").decode("latin-1")


def export_pdf_bytes(jobs: list[Job]) -> bytes:
    pdf = _ReportPdf()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    pdf.multi_cell(
        0,
        6,
        _ascii_pdf_text(f"Generated {datetime.utcnow().isoformat()} UTC\nJobs: {len(jobs)}"),
    )
    pdf.ln(4)

    for job in jobs:
        pdf.set_font("Helvetica", "B", 11)
        pdf.multi_cell(
            0,
            6,
            _ascii_pdf_text(f"{job.title or 'Untitled'} — {job.company or 'Unknown'}"),
        )
        pdf.set_font("Helvetica", size=10)
        score = job.score_total if job.score_total is not None else 0.0
        body = (
            f"Score: {score:.1f} ({job.recommendation_tier})\n"
            f"Location: {job.location or 'n/a'} | Mode: {job.work_mode}\n"
            f"Link: {job.job_url or 'n/a'}\n"
            f"Difficulty: {job.application_difficulty or 'n/a'}\n"
        )
        pdf.multi_cell(0, 5, _ascii_pdf_text(body))
        snippet = (job.raw_description_text or "")[:600].replace("\r", " ")
        pdf.set_font("Helvetica", "I", 9)
        pdf.multi_cell(0, 5, _ascii_pdf_text(f"Snippet: {snippet}"))
        pdf.ln(3)

    data = pdf.output(dest="S")
    if isinstance(data, str):
        return data.encode("latin-1")
    return bytes(data)
