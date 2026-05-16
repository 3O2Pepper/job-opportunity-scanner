"""Streamlit dashboard for the personal job scanner."""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.config import PROJECT_ROOT, settings
from app.db.models import Job, ProfileSnapshot
from app.db.session import init_db_tables, session_scope
from app.services.email_sync import sync_gmail_for_jobs
from app.services.export import export_csv_bytes, export_markdown, export_pdf_bytes
from app.services.job_extract import ingest_manual_link, ingest_manual_text
from app.services.profile_parser import refresh_profile_snapshot, save_profile_file, save_profile_text
from app.services.scoring import alignment_summary, recompute_scores, scoring_context_blob

JOB_STATUSES = [
    "not_applied",
    "applied",
    "saved",
    "rejected",
    "not_interested",
]


def _ensure_startup() -> None:
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "data" / "tokens").mkdir(parents=True, exist_ok=True)
    init_db_tables()


def _fetch_jobs(
    *,
    min_score: float | None,
    show_all_scores: bool,
    company_q: str,
    industry_q: str,
    location_q: str,
    status_filter: list[str] | None,
    date_from: date | None,
    date_to: date | None,
) -> list[Job]:
    with session_scope() as session:
        q = select(Job).order_by(Job.found_at.desc())
        if not show_all_scores and min_score is not None:
            q = q.where(Job.score_total.is_not(None)).where(Job.score_total >= min_score)
        if company_q.strip():
            like = f"%{company_q.strip().lower()}%"
            q = q.where(Job.company.is_not(None)).where(Job.company.ilike(like))
        if industry_q.strip():
            like = f"%{industry_q.strip().lower()}%"
            q = q.where(Job.industry.is_not(None)).where(Job.industry.ilike(like))
        if location_q.strip():
            like = f"%{location_q.strip().lower()}%"
            q = q.where(Job.location.is_not(None)).where(Job.location.ilike(like))
        if status_filter:
            q = q.where(Job.status.in_(status_filter))
        if date_from is not None:
            dt_from = datetime.combine(date_from, datetime.min.time())
            q = q.where(Job.found_at >= dt_from)
        if date_to is not None:
            dt_to = datetime.combine(date_to, datetime.max.time())
            q = q.where(Job.found_at <= dt_to)
        return list(session.scalars(q).all())


def _recent_job_ids(limit: int = 400) -> list[int]:
    with session_scope() as session:
        return list(
            session.scalars(select(Job.id).order_by(Job.found_at.desc()).limit(limit)).all()
        )


def _update_job_status(job_id: int, new_status: str) -> None:
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job:
            job.status = new_status
            session.commit()


def main() -> None:
    st.set_page_config(page_title="Job Opportunity Scanner", layout="wide")
    _ensure_startup()

    st.title("Personal job opportunity scanner")
    st.caption(
        "Reads Gmail only after you authorize OAuth. No LinkedIn scraping or auto-apply."
    )

    with st.sidebar:
        st.header("Workflow")
        if st.button("Recompute all scores"):
            n = recompute_scores()
            st.success(f"Updated scores for {n} jobs.")

        st.subheader("Gmail sync")
        query = st.text_area("Gmail query", value=settings.gmail_query, height=120)
        max_res = st.number_input(
            "Max messages",
            min_value=5,
            max_value=500,
            value=int(settings.gmail_sync_max_results),
        )
        oauth_help = st.checkbox(
            "Allow interactive OAuth from dashboard (opens browser)",
            value=False,
        )
        if st.button("Sync Gmail now"):
            try:
                with session_scope() as session:
                    synced, created = sync_gmail_for_jobs(
                        session,
                        query=query,
                        max_results=int(max_res),
                        interactive_oauth=oauth_help,
                    )
                st.success(
                    f"Synced {synced} messages; created ~{created} new job rows."
                )
                recompute_scores()
            except Exception as exc:
                st.error(str(exc))
                st.info(
                    "Tip: run `python scripts/gmail_oauth_setup.py` once from the project folder."
                )

        st.divider()
        st.subheader("Optional LLM extraction")
        st.warning(
            "Paid APIs (OpenAI/Anthropic) are disabled by default. "
            "Set ENABLE_LLM=true and keys in `.env` only if you choose to use them."
        )

    tabs = st.tabs(["Profile", "Manual jobs", "Recommendations", "Export", "Debug"])

    with tabs[0]:
        st.subheader("Resume & profile")
        uploaded = st.file_uploader("Upload resume (PDF/DOCX/TXT)", type=["pdf", "docx", "txt"])
        if uploaded and st.button("Save uploaded resume"):
            data = uploaded.read()
            save_profile_file(uploaded.name, data)
            st.success("Resume saved.")

        resume_text = st.text_area("Or paste resume text")
        if st.button("Save resume text") and resume_text.strip():
            save_profile_text("resume_text", resume_text.strip())
            st.success("Resume text saved.")

        linkedin_text = st.text_area("LinkedIn export / profile text")
        if st.button("Save LinkedIn text") and linkedin_text.strip():
            save_profile_text("linkedin_export", linkedin_text.strip())
            st.success("LinkedIn text saved.")

        prefs = st.text_area("Career goals & preferences (location, remote, industries)")
        if st.button("Save preferences") and prefs.strip():
            save_profile_text("preferences", prefs.strip())
            st.success("Preferences saved.")

        if st.button("Rebuild profile snapshot"):
            with session_scope() as session:
                refresh_profile_snapshot(session)
            st.success("Snapshot rebuilt.")

    with tabs[1]:
        st.subheader("Paste job description")
        manual_title = st.text_input("Optional title hint")
        manual_body = st.text_area("Job description text", height=220)
        if st.button("Add pasted job") and manual_body.strip():
            with session_scope() as session:
                ingest_manual_text(session, manual_body.strip(), title_hint=manual_title or None)
            recompute_scores()
            st.success("Job ingested.")

        st.divider()
        st.subheader("Manual URL")
        url = st.text_input("Job posting URL (public page you are allowed to fetch)")
        fetch_body = st.checkbox("Fetch page text (GET)", value=True)
        if st.button("Add URL job") and url.strip():
            with session_scope() as session:
                ingest_manual_link(session, url.strip(), fetch_body=fetch_body)
            recompute_scores()
            st.success("Job ingested from URL.")

    with tabs[2]:
        st.subheader("Recommended jobs")
        show_all = st.checkbox("Show all scores (include below 70)", value=False)
        min_score = None if show_all else st.slider("Minimum score", 0, 100, 70)
        c1, c2, c3 = st.columns(3)
        with c1:
            company_q = st.text_input("Company contains")
        with c2:
            industry_q = st.text_input("Industry contains")
        with c3:
            location_q = st.text_input("Location contains")

        statuses = JOB_STATUSES
        status_filter = st.multiselect("Statuses", statuses, default=["not_applied", "saved"])

        d1, d2 = st.columns(2)
        with d1:
            use_from = st.checkbox("Filter found-after date", key="use_from")
            date_from = st.date_input("Found after", disabled=not use_from, key="date_from")
        with d2:
            use_to = st.checkbox("Filter found-before date", key="use_to")
            date_to = st.date_input("Found before", disabled=not use_to, key="date_to")

        jobs = _fetch_jobs(
            min_score=min_score,
            show_all_scores=show_all,
            company_q=company_q,
            industry_q=industry_q,
            location_q=location_q,
            status_filter=status_filter or None,
            date_from=date_from if use_from else None,
            date_to=date_to if use_to else None,
        )

        if not jobs:
            st.info("No jobs match filters. Sync Gmail or add manual postings.")
        else:
            rows = []
            for j in jobs:
                rows.append(
                    {
                        "id": j.id,
                        "score": j.score_total,
                        "tier": j.recommendation_tier,
                        "title": j.title,
                        "company": j.company,
                        "location": j.location,
                        "mode": j.work_mode,
                        "industry": j.industry,
                        "source": j.source_type,
                        "found_at": j.found_at,
                        "status": j.status,
                        "link": j.job_url,
                    }
                )
            df = pd.DataFrame(rows)
            st.dataframe(df, use_container_width=True, hide_index=True)

            pick = st.selectbox("Select job id for details / status update", [j.id for j in jobs])
            job = next(j for j in jobs if j.id == pick)
            col_a, col_b = st.columns(2)
            with col_a:
                try:
                    bd = json.loads(job.score_breakdown_json or "{}")
                    summ = bd.get("explain_summary") or ""
                    if summ:
                        st.markdown(f"**Why this score:** {summ}")
                except json.JSONDecodeError:
                    pass
                with session_scope() as session:
                    prof = session.scalar(
                        select(ProfileSnapshot).order_by(ProfileSnapshot.id.desc()).limit(1)
                    )
                    st.markdown(f"**Goal alignment:** {alignment_summary(job, prof)}")
                st.markdown(f"**Score breakdown (JSON):** `{job.score_breakdown_json}`")
                st.markdown(f"**Missing qualifications:** `{job.missing_qualifications_json}`")
                st.markdown(f"**Resume keywords:** `{job.resume_keywords_json}`")
            with col_b:
                st.markdown(f"**Cover bullets:** `{job.cover_bullet_points_json}`")
                st.markdown(f"**Difficulty:** {job.application_difficulty}")
                idx = statuses.index(job.status) if job.status in statuses else 0
                new_status = st.selectbox(
                    "Update status",
                    statuses,
                    index=idx,
                )
                if st.button("Save status"):
                    _update_job_status(job.id, new_status)
                    st.success("Status updated. Use 'R' or tweak filters to refresh.")

    with tabs[3]:
        st.subheader("Export filtered jobs")
        st.caption("Uses the same filter types as the Recommendations tab.")

        show_all_e = st.checkbox("Include all scores", value=False, key="exp_all")
        min_score_e = None if show_all_e else st.slider(
            "Minimum score",
            0,
            100,
            70,
            key="exp_min",
        )

        xe1, xe2, xe3 = st.columns(3)
        with xe1:
            company_eq = st.text_input("Company contains", key="exp_company")
        with xe2:
            industry_eq = st.text_input("Industry contains", key="exp_industry")
        with xe3:
            location_eq = st.text_input("Location contains", key="exp_location")

        status_filter_e = st.multiselect(
            "Statuses",
            JOB_STATUSES,
            default=["not_applied", "saved"],
            key="exp_status",
        )

        ed1, ed2 = st.columns(2)
        with ed1:
            use_from_e = st.checkbox("Filter found-after date", key="exp_use_from")
            date_from_e = st.date_input(
                "Found after",
                disabled=not use_from_e,
                key="exp_date_from",
            )
        with ed2:
            use_to_e = st.checkbox("Filter found-before date", key="exp_use_to")
            date_to_e = st.date_input(
                "Found before",
                disabled=not use_to_e,
                key="exp_date_to",
            )

        jobs_for_export = _fetch_jobs(
            min_score=min_score_e,
            show_all_scores=show_all_e,
            company_q=company_eq,
            industry_q=industry_eq,
            location_q=location_eq,
            status_filter=status_filter_e or None,
            date_from=date_from_e if use_from_e else None,
            date_to=date_to_e if use_to_e else None,
        )
        st.write(f"{len(jobs_for_export)} jobs selected for export.")

        md = export_markdown(jobs_for_export)
        csv_bytes = export_csv_bytes(jobs_for_export)
        pdf_bytes = export_pdf_bytes(jobs_for_export)

        st.download_button(
            "Download Markdown",
            data=md,
            file_name="jobs_report.md",
            mime="text/markdown",
        )
        st.download_button(
            "Download CSV",
            data=csv_bytes,
            file_name="jobs_report.csv",
            mime="text/csv",
        )
        st.download_button(
            "Download PDF",
            data=pdf_bytes,
            file_name="jobs_report.pdf",
            mime="application/pdf",
        )

    with tabs[4]:
        st.subheader("Extraction & scoring debug")
        st.caption(
            "Inspect Gmail subject/snippet, parser output, and the scoring context blob."
        )
        ids_dbg = _recent_job_ids()
        if not ids_dbg:
            st.info("No jobs in the database yet.")
        else:
            jid = st.selectbox("Job id", ids_dbg)
            with session_scope() as session:
                jdbg = session.get(Job, jid)
            if jdbg:
                st.markdown(
                    f"**Stored title / company:** {jdbg.title} — {jdbg.company}"
                )
                st.markdown(
                    f"**Score:** {jdbg.score_total} — tier `{jdbg.recommendation_tier}` — "
                    f"mode `{jdbg.work_mode}`"
                )
                if jdbg.extraction_debug_json:
                    st.subheader("extraction_debug_json")
                    try:
                        st.json(json.loads(jdbg.extraction_debug_json))
                    except json.JSONDecodeError:
                        st.code(jdbg.extraction_debug_json)
                st.subheader("Scoring context (subject + snippet + fields + body)")
                st.code((scoring_context_blob(jdbg) or "(empty)")[:12000])
                st.subheader("score_breakdown_json")
                try:
                    st.json(json.loads(jdbg.score_breakdown_json or "{}"))
                except json.JSONDecodeError:
                    st.code(jdbg.score_breakdown_json or "")
                st.subheader("raw_description_text (trimmed)")
                st.text((jdbg.raw_description_text or "")[:8000])


if __name__ == "__main__":
    main()
