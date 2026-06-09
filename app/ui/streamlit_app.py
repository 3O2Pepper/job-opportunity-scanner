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
from app.services.email_sync import build_gmail_query, sync_gmail_for_jobs
from app.services.export import export_csv_bytes, export_markdown, export_pdf_bytes
from app.services.job_extract import ingest_manual_link, ingest_manual_text
from app.services.profile_parser import (
    current_snapshot_summary,
    delete_profile_document,
    list_profile_documents,
    refresh_profile_snapshot,
    save_career_goals,
    save_linkedin_file,
    save_profile_file,
    save_profile_text,
)
from app.services.scoring import (
    alignment_summary,
    count_stale_scores,
    recompute_scores,
    scoring_context_blob,
)

JOB_STATUSES = [
    "not_applied",
    "applied",
    "saved",
    "rejected",
    "not_interested",
]

ROLE_FOCUS_OPTIONS = ["Internships", "Entry-level", "Any role"]


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
        q = select(Job)
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
        # Sort: score descending (nulls last), then newest first
        q = q.order_by(
            Job.score_total.desc().nulls_last(),
            Job.found_at.desc(),
        )
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


def _flash(msg: str) -> None:
    st.session_state["_profile_flash"] = msg


def _consume_flash() -> None:
    msg = st.session_state.pop("_profile_flash", None)
    if msg:
        st.success(msg)


# ---------------------------------------------------------------------------
# Profile tab callbacks
# ---------------------------------------------------------------------------

def _save_resume_text_cb() -> None:
    txt = (st.session_state.get("profile_resume_text") or "").strip()
    if txt:
        save_profile_text("resume_text", txt)
        st.session_state["profile_resume_text"] = ""
        _flash("Resume text saved.")


def _save_linkedin_text_cb() -> None:
    txt = (st.session_state.get("profile_linkedin_text") or "").strip()
    if txt:
        save_profile_text("linkedin_export", txt)
        st.session_state["profile_linkedin_text"] = ""
        _flash("LinkedIn text saved.")


def _save_goals_cb() -> None:
    txt = (st.session_state.get("profile_career_goals") or "").strip()
    if txt:
        save_career_goals(txt)
        st.session_state["profile_career_goals"] = ""
        _flash("Career goals saved.")


# ---------------------------------------------------------------------------
# Profile tab renderer
# ---------------------------------------------------------------------------

def _render_profile_tab() -> None:
    _consume_flash()

    snap = current_snapshot_summary()
    st.subheader("Current profile snapshot")
    if snap is None:
        st.info("No profile snapshot yet. Add a resume, LinkedIn export, or career goals below.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Snapshot id", snap["id"])
        m2.metric("Experience band", snap["experience_band"])
        m3.metric("Inferred skills", len(snap["skills"]))
        m4.metric("Merged length", f"{snap['merged_length']:,}")
        st.caption(f"Last updated: {snap['updated_at']:%Y-%m-%d %H:%M:%S}")
        if snap["skills"]:
            preview_skills = snap["skills"][:24]
            chips = " ".join(f"`{s}`" for s in preview_skills)
            extra = (
                f" _(+{len(snap['skills']) - len(preview_skills)} more)_"
                if len(snap["skills"]) > len(preview_skills)
                else ""
            )
            st.markdown(f"**Inferred skills:** {chips}{extra}")
        if snap.get("career_goals"):
            with st.expander("Career goals summary"):
                st.text(snap["career_goals"][:400])
        with st.expander("Merged profile text (first 600 chars)"):
            st.text(snap["merged_preview"] or "(empty)")

    # Stale-score banner
    stale, total_scored, _current_snap_id = count_stale_scores()
    if total_scored > 0:
        if stale > 0:
            cols = st.columns([4, 1])
            cols[0].warning(
                f"{stale} of {total_scored} scored jobs were scored against an older profile. "
                "Rescore so recommendations reflect your current resume."
            )
            role_focus = st.session_state.get("sidebar_role_focus", settings.gmail_role_focus)
            if cols[1].button("Rescore now", key="profile_rescore_now"):
                n = recompute_scores(role_focus=role_focus)
                _flash(f"Rescored {n} jobs against the current snapshot.")
                st.rerun()
        else:
            st.caption(f"All {total_scored} scored jobs reflect the current profile snapshot.")

    st.divider()
    st.subheader("Saved profile documents")
    docs = list_profile_documents()
    if not docs:
        st.caption("Nothing saved yet.")
    else:
        for d in docs:
            label = f"`{d['kind']}` — {d['title'] or '(untitled)'} · {d['length']:,} chars · {d['created_at']:%Y-%m-%d %H:%M}"
            with st.expander(label):
                st.text(d["preview"] or "(empty)")
                st.caption(f"hash {d['hash_short']} · id {d['id']}")
                if st.button("Delete", key=f"profile_del_{d['id']}"):
                    if delete_profile_document(d["id"]):
                        _flash(f"Deleted document #{d['id']}. Snapshot refreshed.")
                        st.rerun()

    st.divider()

    # ── Section 1: Resume ──────────────────────────────────────────────────
    st.subheader("📄 Resume")
    st.caption("Upload a PDF, DOCX, or TXT file, or paste your resume text directly.")

    resume_col1, resume_col2 = st.columns([1, 1], gap="large")
    with resume_col1:
        resume_upload = st.file_uploader(
            "Upload resume (PDF / DOCX / TXT)",
            type=["pdf", "docx", "txt"],
            key="profile_resume_upload",
        )
        if resume_upload is not None:
            if st.button("Save uploaded resume", key="profile_save_resume_upload"):
                try:
                    data = resume_upload.read()
                    save_profile_file(resume_upload.name, data)
                    _flash(f"Saved resume file: {resume_upload.name}")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    with resume_col2:
        st.text_area("Or paste resume text", key="profile_resume_text", height=150)
        st.button("Save resume text", on_click=_save_resume_text_cb, key="profile_save_resume_text_btn")

    st.divider()

    # ── Section 2: LinkedIn ────────────────────────────────────────────────
    st.subheader("🔗 LinkedIn export")
    st.caption(
        "Export your LinkedIn profile as a PDF (Me → Save to PDF) or copy-paste the text. "
        "This is stored separately from your resume so both can inform scoring."
    )

    li_col1, li_col2 = st.columns([1, 1], gap="large")
    with li_col1:
        linkedin_upload = st.file_uploader(
            "Upload LinkedIn PDF / export",
            type=["pdf", "txt"],
            key="profile_linkedin_upload",
        )
        if linkedin_upload is not None:
            if st.button("Save LinkedIn file", key="profile_save_linkedin_upload"):
                try:
                    data = linkedin_upload.read()
                    save_linkedin_file(linkedin_upload.name, data)
                    _flash(f"Saved LinkedIn export: {linkedin_upload.name}")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    with li_col2:
        st.text_area("Or paste LinkedIn profile text", key="profile_linkedin_text", height=150)
        st.button("Save LinkedIn text", on_click=_save_linkedin_text_cb, key="profile_save_linkedin_text_btn")

    st.divider()

    # ── Section 3: Career goals ────────────────────────────────────────────
    st.subheader("🎯 Career goals")
    st.caption(
        "Describe your target roles, industries, preferred locations, remote preference, "
        "and anything else that helps rank opportunities. Example: "
        "\"Seeking aerospace/robotics internships, open to remote or Bay Area, "
        "interested in propulsion, CFD, Python.\""
    )

    st.text_area(
        "Career goals & preferences",
        key="profile_career_goals",
        height=150,
        placeholder=(
            "e.g. Looking for summer internships in aerospace or robotics. "
            "Prefer remote or San Francisco Bay Area. Skills: Python, MATLAB, SolidWorks."
        ),
    )
    st.button("Save career goals", on_click=_save_goals_cb, key="profile_save_goals_btn")

    st.divider()
    if st.button("Rebuild profile snapshot", key="profile_rebuild_snapshot"):
        with session_scope() as session:
            refresh_profile_snapshot(session)
        _flash("Snapshot rebuilt from current documents.")
        st.rerun()


# ---------------------------------------------------------------------------
# Recommendations tab
# ---------------------------------------------------------------------------

def _tier_emoji(tier: str | None) -> str:
    return {
        "strong_apply": "🟢",
        "apply": "🔵",
        "maybe": "🟡",
        "skip": "🔴",
    }.get(tier or "", "⚪")


def _render_recommendations_tab(role_focus: str) -> None:
    st.subheader("Recommended jobs")

    filter_col1, filter_col2 = st.columns([3, 1])
    with filter_col1:
        show_all = st.checkbox("Show all scores (include below 70)", value=False, key="rec_show_all")
    with filter_col2:
        if st.button("Recompute scores", key="rec_rescore"):
            n = recompute_scores(role_focus=role_focus)
            st.success(f"Scored {n} jobs.")
            st.rerun()

    min_score = None if show_all else st.slider("Minimum score", 0, 100, 70, key="rec_min_score")

    fc1, fc2, fc3 = st.columns(3)
    with fc1:
        company_q = st.text_input("Company contains", key="rec_company")
    with fc2:
        industry_q = st.text_input("Industry contains", key="rec_industry")
    with fc3:
        location_q = st.text_input("Location contains", key="rec_location")

    status_filter = st.multiselect(
        "Statuses", JOB_STATUSES, default=["not_applied", "saved"], key="rec_status"
    )

    d1, d2 = st.columns(2)
    with d1:
        use_from = st.checkbox("Filter found-after date", key="rec_use_from")
        date_from = st.date_input("Found after", disabled=not use_from, key="rec_date_from")
    with d2:
        use_to = st.checkbox("Filter found-before date", key="rec_use_to")
        date_to = st.date_input("Found before", disabled=not use_to, key="rec_date_to")

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
        return

    # Build display rows
    rows = []
    for j in jobs:
        missing: list[str] = []
        why = ""
        try:
            if j.missing_qualifications_json:
                missing = json.loads(j.missing_qualifications_json)
        except json.JSONDecodeError:
            pass
        try:
            if j.score_breakdown_json:
                bd = json.loads(j.score_breakdown_json)
                why = bd.get("explain_summary") or ""
        except json.JSONDecodeError:
            pass

        tier_label = ""
        if j.recommendation_tier:
            tier_label = f"{_tier_emoji(j.recommendation_tier)} {j.recommendation_tier.replace('_', ' ').title()}"

        rows.append(
            {
                "ID": j.id,
                "Score": j.score_total,
                "Recommendation": tier_label,
                "Title": j.title or "—",
                "Company": j.company or "—",
                "Location": j.location or "—",
                "Deadline": j.deadline or "—",
                "Missing items": ", ".join(missing[:4]) if missing else "—",
                "Why": (why[:120] + "…") if len(why) > 120 else why,
                "Apply": j.job_url or "",
                "Status": j.status,
                "Found": j.found_at.strftime("%Y-%m-%d") if j.found_at else "—",
                "_id": j.id,
            }
        )

    df_display = pd.DataFrame(rows).drop(columns=["_id"])
    # Make Apply column render as clickable links via column_config
    st.dataframe(
        df_display,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Apply": st.column_config.LinkColumn("Apply", display_text="Apply ↗"),
            "Score": st.column_config.NumberColumn("Score", format="%.0f", min_value=0, max_value=100),
        },
    )

    st.divider()
    st.subheader("Job details")
    job_options = {f"#{j.id} — {j.title or 'Untitled'} @ {j.company or '?'}": j.id for j in jobs}
    selected_label = st.selectbox("Select a job", list(job_options.keys()), key="rec_job_select")
    if selected_label is None:
        return

    selected_id = job_options[selected_label]
    job = next(j for j in jobs if j.id == selected_id)

    detail_left, detail_right = st.columns(2)

    with detail_left:
        st.markdown(f"### {_tier_emoji(job.recommendation_tier)} {job.title or 'Untitled'}")
        st.markdown(f"**Company:** {job.company or '—'}  \n**Location:** {job.location or '—'}  \n**Mode:** {job.work_mode or '—'}  \n**Deadline:** {job.deadline or '—'}")
        if job.job_url:
            st.markdown(f"**Apply:** [{job.job_url}]({job.job_url})")
        st.markdown(f"**Score:** `{job.score_total}`  |  **Tier:** `{job.recommendation_tier}`  |  **Difficulty:** {job.application_difficulty or '—'}")

        # Goal alignment
        with session_scope() as session:
            prof = session.scalar(
                select(ProfileSnapshot).order_by(ProfileSnapshot.id.desc()).limit(1)
            )
            alignment = alignment_summary(job, prof)
        st.markdown(f"**Goal alignment:** {alignment}")

        # Why this score
        try:
            bd = json.loads(job.score_breakdown_json or "{}")
            summ = bd.get("explain_summary") or ""
            if summ:
                with st.expander("Why this score"):
                    for part in summ.split(";"):
                        part = part.strip()
                        if part:
                            st.caption(part)
        except json.JSONDecodeError:
            pass

        # Source email
        try:
            if job.extraction_debug_json:
                dbg = json.loads(job.extraction_debug_json)
                src_subj = dbg.get("email_subject")
                src_snip = dbg.get("email_snippet")
                if src_subj or src_snip:
                    with st.expander("Source email"):
                        if src_subj:
                            st.caption(f"**Subject:** {src_subj}")
                        if src_snip:
                            st.caption(f"**Snippet:** {src_snip}")
        except json.JSONDecodeError:
            pass

    with detail_right:
        # Missing qualifications
        missing: list[str] = []
        try:
            if job.missing_qualifications_json:
                missing = json.loads(job.missing_qualifications_json)
        except json.JSONDecodeError:
            pass
        st.markdown("**Missing qualifications / skills**")
        if missing:
            for m in missing:
                st.markdown(f"- {m}")
        else:
            st.caption("None identified — you meet the listed requirements.")

        # Resume keywords to add
        keywords: list[str] = []
        try:
            if job.resume_keywords_json:
                keywords = json.loads(job.resume_keywords_json)
        except json.JSONDecodeError:
            pass
        st.markdown("**Suggested resume keywords**")
        if keywords:
            st.markdown(", ".join(f"`{k}`" for k in keywords))
        else:
            st.caption("No additional keywords suggested.")

        # Cover letter bullets
        bullets: list[str] = []
        try:
            if job.cover_bullet_points_json:
                bullets = json.loads(job.cover_bullet_points_json)
        except json.JSONDecodeError:
            pass
        st.markdown("**Suggested cover letter bullets**")
        for b in bullets:
            st.markdown(f"- {b}")

        # Status update
        st.divider()
        idx = JOB_STATUSES.index(job.status) if job.status in JOB_STATUSES else 0
        new_status = st.selectbox("Update status", JOB_STATUSES, index=idx, key="rec_status_select")
        if st.button("Save status", key="rec_save_status"):
            _update_job_status(job.id, new_status)
            st.success("Status updated.")


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Job Opportunity Scanner", layout="wide")
    _ensure_startup()

    st.title("Personal job opportunity scanner")
    st.caption(
        "Reads Gmail only after you authorize OAuth. No LinkedIn scraping or auto-apply."
    )

    # ── Sidebar ──────────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("Gmail sync")

        role_focus = st.selectbox(
            "Role focus",
            ROLE_FOCUS_OPTIONS,
            index=ROLE_FOCUS_OPTIONS.index(settings.gmail_role_focus)
            if settings.gmail_role_focus in ROLE_FOCUS_OPTIONS
            else 0,
            key="sidebar_role_focus",
        )
        scan_days = st.slider(
            "Scan window (days)",
            min_value=7,
            max_value=180,
            value=int(settings.gmail_scan_days),
            step=7,
            key="sidebar_scan_days",
        )
        max_res = st.number_input(
            "Max messages",
            min_value=5,
            max_value=500,
            value=int(settings.gmail_sync_max_results),
            key="sidebar_max_res",
        )

        auto_query = build_gmail_query(days=scan_days, role_focus=role_focus)
        with st.expander("Advanced query override"):
            query_override = st.text_area(
                "Override query (leave blank to use auto-built query)",
                value="",
                height=90,
                key="sidebar_query_override",
                help="Fill this in only if you want full control. Leave blank to use the controls above.",
            )
        final_query = query_override.strip() if query_override.strip() else auto_query
        st.caption(f"Active query: `{final_query[:120]}{'…' if len(final_query) > 120 else ''}`")

        oauth_help = st.checkbox(
            "Allow interactive OAuth (opens browser)",
            value=False,
            key="sidebar_oauth",
        )

        if st.button("Sync Gmail now", key="sidebar_sync"):
            try:
                with session_scope() as session:
                    synced, created, skipped = sync_gmail_for_jobs(
                        session,
                        query=final_query,
                        max_results=int(max_res),
                        interactive_oauth=oauth_help,
                    )
                msg = f"Synced {synced} messages; created ~{created} new job rows."
                if skipped:
                    msg += f" ({skipped} message(s) skipped due to fetch/parse errors.)"
                st.success(msg)
                recompute_scores(role_focus=role_focus)
                st.rerun()
            except RuntimeError as exc:
                msg = str(exc)
                st.error("Gmail sync failed.")
                # Surface friendly "Reconnect Gmail" instructions for token errors
                if "revoked" in msg.lower() or "token" in msg.lower() or "reconnect" in msg.lower():
                    st.warning(
                        "**Reconnect Gmail**\n\n"
                        f"Your Gmail token has expired or been revoked. "
                        f"Delete `data/tokens/gmail_token.json` and run "
                        "`python scripts/gmail_oauth_setup.py` to reconnect."
                    )
                with st.expander("Error details"):
                    st.code(msg)
            except Exception as exc:
                st.error(str(exc))
                st.info(
                    "Tip: run `python scripts/gmail_oauth_setup.py` once from the project folder."
                )

        st.divider()
        st.subheader("Workflow")
        if st.button("Recompute all scores", key="sidebar_rescore_all"):
            n = recompute_scores(role_focus=role_focus)
            st.success(f"Updated scores for {n} jobs.")

        st.divider()
        st.subheader("Optional LLM extraction")
        st.warning(
            "Paid APIs (OpenAI/Anthropic) are disabled by default. "
            "Set ENABLE_LLM=true and keys in `.env` only if you choose to use them."
        )

    # ── Tabs — Recommendations first as main working screen ─────────────────
    tabs = st.tabs(["Recommendations", "Profile", "Manual jobs", "Export", "Debug"])

    with tabs[0]:
        _render_recommendations_tab(role_focus=role_focus)

    with tabs[1]:
        _render_profile_tab()

    with tabs[2]:
        st.subheader("Paste job description")
        manual_title = st.text_input("Optional title hint", key="manual_title")
        manual_body = st.text_area("Job description text", height=220, key="manual_body")
        if st.button("Add pasted job", key="manual_add_text") and manual_body.strip():
            with session_scope() as session:
                ingest_manual_text(session, manual_body.strip(), title_hint=manual_title or None)
            recompute_scores(role_focus=role_focus)
            st.success("Job ingested.")

        st.divider()
        st.subheader("Manual URL")
        url = st.text_input("Job posting URL (public page you are allowed to fetch)", key="manual_url")
        fetch_body = st.checkbox("Fetch page text (GET)", value=True, key="manual_fetch_body")
        if st.button("Add URL job", key="manual_add_url") and url.strip():
            try:
                with session_scope() as session:
                    job = ingest_manual_link(session, url.strip(), fetch_body=fetch_body)
                recompute_scores(role_focus=role_focus)
                if job.extraction_debug_json and "fetch_error" in job.extraction_debug_json:
                    st.warning(
                        "Job saved from URL, but the page body could not be fetched. "
                        "Scoring will use the URL only."
                    )
                else:
                    st.success("Job ingested from URL.")
            except ValueError as exc:
                st.error(str(exc))

    with tabs[3]:
        st.subheader("Export filtered jobs")
        st.caption("Uses the same filter types as the Recommendations tab.")

        show_all_e = st.checkbox("Include all scores", value=False, key="exp_all")
        min_score_e = None if show_all_e else st.slider("Minimum score", 0, 100, 70, key="exp_min")

        xe1, xe2, xe3 = st.columns(3)
        with xe1:
            company_eq = st.text_input("Company contains", key="exp_company")
        with xe2:
            industry_eq = st.text_input("Industry contains", key="exp_industry")
        with xe3:
            location_eq = st.text_input("Location contains", key="exp_location")

        status_filter_e = st.multiselect(
            "Statuses", JOB_STATUSES, default=["not_applied", "saved"], key="exp_status"
        )

        ed1, ed2 = st.columns(2)
        with ed1:
            use_from_e = st.checkbox("Filter found-after date", key="exp_use_from")
            date_from_e = st.date_input("Found after", disabled=not use_from_e, key="exp_date_from")
        with ed2:
            use_to_e = st.checkbox("Filter found-before date", key="exp_use_to")
            date_to_e = st.date_input("Found before", disabled=not use_to_e, key="exp_date_to")

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

        st.download_button("Download Markdown", data=md, file_name="jobs_report.md", mime="text/markdown")
        st.download_button("Download CSV", data=csv_bytes, file_name="jobs_report.csv", mime="text/csv")
        st.download_button("Download PDF", data=pdf_bytes, file_name="jobs_report.pdf", mime="application/pdf")

    with tabs[4]:
        st.subheader("Extraction & scoring debug")
        st.caption("Inspect Gmail subject/snippet, parser output, and the scoring context blob.")
        ids_dbg = _recent_job_ids()
        if not ids_dbg:
            st.info("No jobs in the database yet.")
        else:
            jid = st.selectbox("Job id", ids_dbg, key="debug_job_id")
            with session_scope() as session:
                jdbg = session.get(Job, jid)
            if jdbg:
                st.markdown(f"**Stored title / company:** {jdbg.title} — {jdbg.company}")
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
