"""Extract structured job fields from email bodies, pasted text, or fetched pages."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.db.models import EmailMessage, Job
from app.services.email_job_extract import extract_all_candidates
from app.services.job_text_utils import (
    guess_skills,
    guess_work_mode,
    normalize_whitespace,
    parse_years_experience,
)
from app.services.llm_enrichment import maybe_enrich_job_with_llm

_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)


def strip_html(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    return normalize_whitespace(soup.get_text(" ", strip=True))


def extract_urls(text: str) -> list[str]:
    found = _URL_RE.findall(text or "")
    cleaned = []
    for u in found:
        u = u.rstrip(").,;]")
        cleaned.append(u)
    seen: set[str] = set()
    out: list[str] = []
    for u in cleaned:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def make_dedupe_hash(
    gmail_message_id: str | None,
    job_url: str | None,
    title: str | None,
    company: str | None,
    slot_index: int,
    raw_fallback: str,
) -> str:
    """Stable per-email + per-opportunity; avoids duplicate empty rows from resync."""
    nu = (job_url or "").strip().lower().split("?")[0]
    nt = normalize_whitespace((title or "").lower())[:240]
    nc = normalize_whitespace((company or "").lower())[:240]
    gid = (gmail_message_id or "no_gmail").strip()
    base = f"{gid}|{nu}|{nt}|{nc}|{slot_index}"
    if len(nu) < 8 and len(nt) < 4:
        h = hashlib.sha256(raw_fallback[:1200].encode("utf-8")).hexdigest()[:24]
        base += f"|{h}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def fetch_public_job_page(url: str, timeout: float = 15.0) -> str:
    """Fetch a URL you supplied (respect robots/terms yourself)."""
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        r = client.get(url, headers={"User-Agent": "JobScannerPersonal/1.0"})
        r.raise_for_status()
        return r.text


def build_job_record(
    *,
    title: str | None,
    company: str | None,
    location: str | None,
    industry: str | None,
    work_mode: str,
    job_url: str | None,
    source_type: str,
    source_ref: str | None,
    raw_description_text: str,
    email_row: EmailMessage | None = None,
    dedupe_hash: str,
    required_skills: list[str] | None = None,
    preferred_skills: list[str] | None = None,
    years_experience_min: int | None = None,
    years_experience_max: int | None = None,
    extraction_debug_json: str | None = None,
) -> Job:
    blob = raw_description_text or ""
    req_sk = required_skills if required_skills is not None else guess_skills(blob)[0]
    pref_sk = preferred_skills if preferred_skills is not None else guess_skills(blob)[1]
    y_min, y_max = years_experience_min, years_experience_max
    if y_min is None and y_max is None:
        y_min, y_max = parse_years_experience(blob)

    return Job(
        dedupe_hash=dedupe_hash,
        title=title,
        company=company,
        location=location,
        work_mode=work_mode or guess_work_mode(blob),
        job_url=job_url,
        source_type=source_type,
        source_ref=source_ref,
        found_at=datetime.utcnow(),
        required_skills_json=json.dumps(req_sk),
        preferred_skills_json=json.dumps(pref_sk),
        years_experience_min=y_min,
        years_experience_max=y_max,
        degree_requirements=None,
        industry=industry,
        deadline=None,
        raw_description_text=blob[:50_000],
        email_message_id=email_row.id if email_row else None,
        extraction_debug_json=extraction_debug_json,
    )


def upsert_job(session, job: Job) -> tuple[Job, bool]:
    existing = session.scalar(select(Job).where(Job.dedupe_hash == job.dedupe_hash))
    if existing:
        if not existing.job_url and job.job_url:
            existing.job_url = job.job_url
        if existing.email_message_id is None and job.email_message_id:
            existing.email_message_id = job.email_message_id
        if job.title and (not existing.title or len(job.title) > len(existing.title or "")):
            existing.title = job.title
        if job.company and not existing.company:
            existing.company = job.company
        if job.location and not existing.location:
            existing.location = job.location
        if job.industry and not existing.industry:
            existing.industry = job.industry
        if job.raw_description_text and len(job.raw_description_text) > len(
            existing.raw_description_text or ""
        ):
            existing.raw_description_text = job.raw_description_text
        if job.extraction_debug_json:
            existing.extraction_debug_json = job.extraction_debug_json
        if job.required_skills_json and job.required_skills_json != "[]":
            existing.required_skills_json = job.required_skills_json
        if job.preferred_skills_json and job.preferred_skills_json != "[]":
            existing.preferred_skills_json = job.preferred_skills_json
        session.commit()
        session.refresh(existing)
        return existing, False
    session.add(job)
    session.commit()
    session.refresh(job)
    return job, True


def ingest_email_message(
    session,
    *,
    email_row: EmailMessage,
    subject: str,
    plain_body: str,
    html_body: str,
) -> int:
    body_text = plain_body.strip() if plain_body.strip() else strip_html(html_body)
    combo = f"{subject}\n{body_text}"

    candidates, meta = extract_all_candidates(
        from_addr=email_row.from_addr,
        subject=subject or "",
        snippet=email_row.snippet,
        plain=plain_body,
        html=html_body,
    )

    if not candidates:
        return 0

    created_count = 0
    for idx, cand in enumerate(candidates):
        raw_for_dedupe = normalize_whitespace(
            f"{subject}\n{email_row.snippet or ''}\n{combo}\n{cand.raw_block}"
        )
        dh = make_dedupe_hash(
            email_row.gmail_message_id,
            cand.job_url,
            cand.title,
            cand.company,
            idx,
            raw_for_dedupe,
        )
        per_debug = {
            **meta,
            "email_subject": subject,
            "email_snippet": email_row.snippet,
            "candidate_index": idx,
            "candidate": {
                "title": cand.title,
                "company": cand.company,
                "location": cand.location,
                "work_mode": cand.work_mode,
                "industry": cand.industry,
                "job_url": cand.job_url,
                "parser": cand.parser,
                "span_hint": cand.span_hint,
            },
        }
        job = build_job_record(
            title=cand.title,
            company=cand.company,
            location=cand.location,
            industry=cand.industry,
            work_mode=cand.work_mode,
            job_url=cand.job_url,
            source_type="gmail",
            source_ref=email_row.gmail_message_id,
            raw_description_text=normalize_whitespace(
                f"{subject}\n{cand.raw_block}\n{combo}"
            )[:50_000],
            email_row=email_row,
            dedupe_hash=dh,
            required_skills=cand.required_skills,
            preferred_skills=cand.preferred_skills,
            years_experience_min=cand.years_experience_min,
            years_experience_max=cand.years_experience_max,
            extraction_debug_json=json.dumps(per_debug, ensure_ascii=False),
        )
        saved, created = upsert_job(session, job)
        maybe_enrich_job_with_llm(saved)
        if created:
            created_count += 1
        session.commit()
        session.refresh(saved)

    return created_count


def ingest_manual_text(session, text: str, title_hint: str | None = None) -> Job:
    text = text.strip()
    urls = extract_urls(text)
    job_url = urls[0] if urls else None
    title = title_hint
    company = None
    if not title:
        title = text.splitlines()[0][:200] if text else "Manual entry"
    dh = make_dedupe_hash(None, job_url, title, company, 0, text)
    job = build_job_record(
        title=title,
        company=company,
        location=None,
        industry=None,
        work_mode=guess_work_mode(text),
        job_url=job_url,
        source_type="manual_text",
        source_ref=None,
        raw_description_text=text,
        email_row=None,
        dedupe_hash=dh,
    )
    j, _ = upsert_job(session, job)
    maybe_enrich_job_with_llm(j)
    session.commit()
    session.refresh(j)
    return j


def ingest_manual_link(session, url: str, fetch_body: bool = True) -> Job:
    parsed_url = urlparse(url.strip())
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        raise ValueError("URL must start with http:// or https:// and include a host.")

    body = ""
    fetch_error: str | None = None
    if fetch_body:
        try:
            html = fetch_public_job_page(url)
            body = strip_html(html)
        except Exception as exc:
            fetch_error = str(exc)
            body = ""
    raw = body or url
    company_guess = parsed_url.netloc.replace("www.", "")
    dh = make_dedupe_hash(None, url, None, company_guess, 0, raw)
    debug_json = None
    if fetch_error:
        debug_json = json.dumps(
            {"fetch_error": fetch_error, "url": url},
            ensure_ascii=False,
        )
    job = build_job_record(
        title=None,
        company=company_guess,
        location=None,
        industry=None,
        work_mode=guess_work_mode(raw),
        job_url=url,
        source_type="manual_link",
        source_ref=url,
        raw_description_text=raw[:50_000],
        email_row=None,
        dedupe_hash=dh,
        extraction_debug_json=debug_json,
    )
    j, _ = upsert_job(session, job)
    maybe_enrich_job_with_llm(j)
    session.commit()
    session.refresh(j)
    return j
