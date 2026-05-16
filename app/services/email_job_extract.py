"""
Heuristic extraction of job postings from alert/recruiter emails (no LLM).

Targets common shapes: LinkedIn, Handshake, Workday, Oracle Cloud, Indeed, Greenhouse/Lever patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from app.services.job_text_utils import (
    guess_skills,
    guess_work_mode,
    normalize_whitespace,
    parse_years_experience,
)


# Generic marketing subjects — do not use as job title if we have alternatives
GENERIC_TITLE_PATTERNS = re.compile(
    r"^(new job matches|job alert|jobs you may be interested in|recommended jobs|"
    r"jobs at|your .*job|weekly digest|job opportunities|new opportunities|"
    r"opportunities for you|handshake digest|apply today|don'?t miss|"
    r"similar jobs|top job picks|job search update)\b",
    re.I,
)

GENERIC_TITLE_EXACT = {
    "job alert",
    "new jobs",
    "new job opportunities",
    "job opportunities",
    "your job alerts",
}


SKIP_URL_SUBSTRINGS = (
    "unsubscribe",
    "opt-out",
    "email-preferences",
    "manage_notifications",
    "linkedin.com/societies/",
    "youtube.com",
    "twitter.com",
    "facebook.com",
    "instagram.com",
    "mailto:",
    "/help/",
    "privacy",
    "settings",
    "trkn.us",  # tracking
)

JOB_BOARD_HOST_HINTS = (
    "linkedin.com/jobs",
    "joinhandshake.com",
    "app.joinhandshake.com",
    "myworkdayjobs.com",
    "oraclecloud.com",
    "taleo.net",
    "avature.net",
    "icims.com",
    "greenhouse.io",
    "boards.greenhouse.io",
    "lever.co",
    "jobs.lever.co",
    "indeed.com",
    "glassdoor.com",
    "smartrecruiters.com",
    "ashbyhq.com",
    "jobvite.com",
    "applytojob.com",
    "taleo.",
    "ultipro.com",
    "adp.com",
)

EMAIL_VENDOR_LINKEDIN = "linkedin"
EMAIL_VENDOR_HANDSHAKE = "handshake"
EMAIL_VENDOR_WORKDAY = "workday"
EMAIL_VENDOR_ORACLE = "oracle"
EMAIL_VENDOR_INDEED = "indeed"
EMAIL_VENDOR_GENERIC = "generic"


@dataclass
class JobCandidate:
    title: str | None
    company: str | None
    location: str | None
    work_mode: str
    industry: str | None
    job_url: str | None
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    years_experience_min: int | None = None
    years_experience_max: int | None = None
    raw_block: str = ""
    parser: str = "generic"
    span_hint: str = ""


def _from_header_email(from_addr: str | None) -> str:
    if not from_addr:
        return ""
    # "Name <email@x.com>" or email only
    m = re.search(r"<([^>]+)>", from_addr)
    if m:
        return m.group(1).lower()
    return from_addr.lower()


def classify_vendor(from_addr: str | None, urls: list[str]) -> str:
    fe = _from_header_email(from_addr)
    blob = " ".join(urls) + " " + fe
    ul = [u.lower() for u in urls]
    if any("myworkdayjobs.com" in u for u in ul):
        return EMAIL_VENDOR_WORKDAY
    if any("oraclecloud.com" in u for u in ul):
        return EMAIL_VENDOR_ORACLE
    if any("indeed.com" in u for u in ul):
        return EMAIL_VENDOR_INDEED
    if "linkedin" in fe or any("linkedin.com" in u for u in ul):
        return EMAIL_VENDOR_LINKEDIN
    if "handshake" in fe or any("joinhandshake.com" in u or "handshake.com" in u for u in ul):
        return EMAIL_VENDOR_HANDSHAKE
    if "indeed" in fe:
        return EMAIL_VENDOR_INDEED
    return EMAIL_VENDOR_GENERIC


def clean_url(url: str) -> str:
    u = url.strip().rstrip(").,;]")
    low = u.lower()
    if "linkedin.com" in low and ("linkedin.com/jobs" in low or "/job/" in low):
        u = u.split("?")[0]
    return u


def is_noise_url(url: str) -> bool:
    low = url.lower()
    return any(s in low for s in SKIP_URL_SUBSTRINGS)


def is_jobish_url(url: str) -> bool:
    low = url.lower()
    if is_noise_url(url):
        return False
    return any(h in low for h in JOB_BOARD_HOST_HINTS)


def prioritize_job_urls(urls: list[str]) -> list[str]:
    ranked: list[tuple[int, str]] = []
    for u in urls:
        cu = clean_url(u)
        if not is_jobish_url(cu):
            continue
        score = 10
        if "linkedin.com/jobs/view" in cu:
            score = 100
        elif "myworkdayjobs.com" in cu or "oraclecloud.com" in cu:
            score = 95
        elif "joinhandshake" in cu or "app.joinhandshake" in cu:
            score = 90
        elif "boards.greenhouse.io" in cu or "jobs.lever.co" in cu or "greenhouse.io" in cu:
            score = 88
        elif "indeed.com" in cu:
            score = 85
        ranked.append((score, cu))
    ranked.sort(key=lambda x: -x[0])
    seen: set[str] = set()
    out: list[str] = []
    for _, u in ranked:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def is_generic_title(title: str | None) -> bool:
    if not title:
        return True
    t = normalize_whitespace(title)
    if not t or len(t) < 3:
        return True
    low = t.lower()
    if low in GENERIC_TITLE_EXACT:
        return True
    if GENERIC_TITLE_PATTERNS.search(low):
        return True
    if low.startswith("re:") and len(low) < 30:
        return True
    return False


def clean_title(title: str | None, fallback: str | None = None) -> str | None:
    if title:
        t = normalize_whitespace(re.sub(r"^\s*(re|fw)\s*:\s*", "", title, flags=re.I))
        if not is_generic_title(t):
            return t[:500]
    if fallback and not is_generic_title(fallback):
        return normalize_whitespace(fallback)[:500]
    return None


def parse_subject_role_company(subject: str) -> tuple[str | None, str | None]:
    """Mirror earlier logic but strip Re:/Fw:."""
    s = normalize_whitespace(re.sub(r"^\s*(re|fw)\s*:\s*", "", subject or "", flags=re.I))
    if not s:
        return None, None
    for sep in [" — ", " – ", " - ", " | ", " \u2013 "]:
        if sep in s:
            a, b = s.split(sep, 1)
            a, b = a.strip(), b.strip()
            if not is_generic_title(a):
                return a, b or None
            return a, b or None
    low = s.lower()
    if " at " in low:
        idx = low.index(" at ")
        left, right = s[:idx].strip(), s[idx + 4 :].strip()
        if not is_generic_title(left):
            return left, right or None
        return left, right or None
    if not is_generic_title(s):
        return s, None
    return None, None


_LOC_PATTERNS = [
    re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*,\s*[A-Z]{2})\b"),  # City, ST
    re.compile(r"location\s*[:\-]\s*(.+?)(?:\n|$)", re.I),
    re.compile(r"based in\s+(.+?)(?:\n|$)", re.I),
    re.compile(r"(\w+(?:\s+\w+)*)\s*·\s*(remote|hybrid|on-?site)", re.I),
]


def extract_location(text: str) -> str | None:
    for pat in _LOC_PATTERNS:
        m = pat.search(text)
        if m:
            loc = normalize_whitespace(m.group(1) if m.lastindex else m.group(0))
            if 3 < len(loc) < 120:
                return loc
    return None


def infer_industry(blob: str, job_url: str | None) -> str | None:
    t = f"{blob}\n{job_url or ''}".lower()
    if any(x in t for x in ("aerospace", "aircraft", "spacecraft", "rocket", "defense", "missile")):
        return "Aerospace & Defense"
    if any(x in t for x in ("renewable", "geothermal", "energy storage", "power generation", "nuclear")):
        return "Energy"
    if "robotics" in t or "automation" in t:
        return "Robotics & Automation"
    if "automotive" in t or "vehicle" in t:
        return "Automotive"
    if "manufacturing" in t or "machining" in t:
        return "Manufacturing"
    if "research" in t and "laboratory" in t:
        return "Research"
    return None


def _linkedin_decode_url(href: str) -> str:
    """Turn LinkedIn redirect links into final URL when possible."""
    low = href.lower()
    if "linkedin.com/safety/go" in low or "/redirect" in low:
        try:
            q = parse_qs(urlparse(href).query)
            for key in ("url", "u"):
                if key in q and q[key]:
                    return unquote(q[key][0])
        except Exception:
            pass
    return href


def extract_from_linkedin_html(html: str, plain: str, subject: str) -> list[JobCandidate]:
    if not html.strip():
        return []
    soup = BeautifulSoup(html, "lxml")
    seen_urls: set[str] = set()
    out: list[JobCandidate] = []

    for a in soup.find_all("a", href=True):
        raw_href = a["href"]
        href = _linkedin_decode_url(raw_href).split("?")[0]
        low = href.lower()
        if "linkedin.com/jobs/view" not in low and "linkedin.com/jobs/collections" not in low:
            continue
        if not is_jobish_url(href):
            continue
        href = clean_url(href)
        if href in seen_urls:
            continue
        seen_urls.add(href)

        link_text = normalize_whitespace(a.get_text(" ", strip=True))
        # Walk up a bit for company line
        parent = a.find_parent(["tr", "table", "div", "td"])
        block = ""
        if parent:
            block = normalize_whitespace(parent.get_text(" ", strip=True))[:3000]
        title = clean_title(link_text, None)
        company = None
        # "Title · Company" in block
        if "·" in block[:200]:
            parts = block.split("·", 1)
            t0 = normalize_whitespace(parts[0])
            if title is None:
                title = clean_title(t0, link_text)
            if len(parts) > 1:
                company = normalize_whitespace(parts[1].split("\n")[0].split("  ")[0])[:200] or None
        location = extract_location(block) or extract_location(plain)
        wm = guess_work_mode(f"{block}\n{plain}")
        ind = infer_industry(block + "\n" + plain, href)
        req, pref = guess_skills(f"{block}\n{plain}")
        ymin, ymax = parse_years_experience(block + "\n" + plain)

        out.append(
            JobCandidate(
                title=title or clean_title(None, link_text),
                company=company,
                location=location,
                work_mode=wm,
                industry=ind,
                job_url=href,
                required_skills=req,
                preferred_skills=pref,
                years_experience_min=ymin,
                years_experience_max=ymax,
                raw_block=block or link_text,
                parser="linkedin_html",
                span_hint="linkedin_html",
            )
        )

    # Single-job fallback: one prominent jobs URL in HTML not caught above
    if not out:
        urls = prioritize_job_urls(re.findall(r"https?://[^\s\"'<>]+", html + "\n" + plain))
        for u in urls:
            if "linkedin.com/jobs" in u.lower():
                title_s, company_s = parse_subject_role_company(subject)
                title = clean_title(title_s, None)
                out.append(
                    JobCandidate(
                        title=title,
                        company=company_s,
                        location=extract_location(plain),
                        work_mode=guess_work_mode(plain),
                        industry=infer_industry(plain, u),
                        job_url=clean_url(u),
                        required_skills=guess_skills(plain)[0],
                        preferred_skills=guess_skills(plain)[1],
                        raw_block=normalize_whitespace(plain)[:3000],
                        parser="linkedin_fallback_url",
                        span_hint="linkedin_fallback",
                    )
                )
                break
    return out


def extract_from_handshake_html(html: str, plain: str, subject: str) -> list[JobCandidate]:
    soup = BeautifulSoup(html, "lxml") if html.strip() else None
    seen: set[str] = set()
    out: list[JobCandidate] = []
    if soup:
        for a in soup.find_all("a", href=True):
            href = clean_url(a["href"].split("?")[0])
            hlow = href.lower()
            if "handshake.com" not in hlow:
                continue
            if "/jobs/" not in hlow and "job_id=" not in hlow and "/emp/jobs" not in hlow:
                continue
            if href in seen:
                continue
            seen.add(href)
            link_text = normalize_whitespace(a.get_text(" ", strip=True))
            parent = a.find_parent(["tr", "div", "table"])
            block = normalize_whitespace(parent.get_text(" ", strip=True))[:2500] if parent else link_text
            title = clean_title(link_text, None) or clean_title(None, link_text)
            location = extract_location(block) or extract_location(plain)
            out.append(
                JobCandidate(
                    title=title,
                    company=None,
                    location=location,
                    work_mode=guess_work_mode(block + "\n" + plain),
                    industry=infer_industry(block + "\n" + plain, href),
                    job_url=href,
                    required_skills=guess_skills(block + "\n" + plain)[0],
                    preferred_skills=guess_skills(block + "\n" + plain)[1],
                    raw_block=block,
                    parser="handshake_html",
                    span_hint="handshake_html",
                )
            )
    if not out:
        for u in prioritize_job_urls(re.findall(r"https?://[^\s\"'<>\+]+", html + "\n" + plain)):
            if "joinhandshake" in u.lower():
                ts, cs = parse_subject_role_company(subject)
                out.append(
                    JobCandidate(
                        title=clean_title(ts, None),
                        company=cs,
                        location=extract_location(plain),
                        work_mode=guess_work_mode(plain),
                        industry=infer_industry(plain, u),
                        job_url=clean_url(u),
                        required_skills=guess_skills(plain)[0],
                        preferred_skills=guess_skills(plain)[1],
                        raw_block=plain[:3000],
                        parser="handshake_url",
                        span_hint="handshake_url",
                    )
                )
                break
    return out


def extract_from_url_lines(plain: str, html: str, subject: str, vendor: str) -> list[JobCandidate]:
    """Each job-like URL gets a slice of nearby plain text."""
    if plain.strip():
        text = plain
    elif html.strip():
        text = BeautifulSoup(html, "lxml").get_text("\n", strip=True)
    else:
        text = ""
    urls = prioritize_job_urls(re.findall(r"https?://[^\s\"'<>]+", text))
    if len(urls) <= 1:
        return []
    lines = text.splitlines()
    out: list[JobCandidate] = []
    for u in urls:
        u_clean = clean_url(u)
        ctx_start = max(0, text.find(u) - 400) if u in text else 0
        ctx = normalize_whitespace(text[ctx_start : ctx_start + 800])
        title = None
        for line in lines:
            if u in line:
                left = line.replace(u, "").strip(" |-—\t")
                if left and len(left) < 120:
                    title = clean_title(left, None)
                break
        if title is None:
            ts, cs = parse_subject_role_company(subject)
            title = clean_title(ts, None)
            company = cs
        else:
            _, company = parse_subject_role_company(subject)
        out.append(
            JobCandidate(
                title=title,
                company=company,
                location=extract_location(ctx),
                work_mode=guess_work_mode(ctx),
                industry=infer_industry(ctx, u_clean),
                job_url=u_clean,
                required_skills=guess_skills(ctx)[0],
                preferred_skills=guess_skills(ctx)[1],
                raw_block=ctx[:2500],
                parser=f"{vendor}_url_line",
                span_hint="multi_url_plain",
            )
        )
    return out


def passes_quality_gate(c: JobCandidate) -> bool:
    """Skip empty junk rows (no actionable link + generic title + no body)."""
    has_url = bool(c.job_url)
    tit = c.title or ""
    title_ok = bool(tit.strip()) and not is_generic_title(tit)
    body_len = len(c.raw_block or "")
    if has_url and title_ok:
        return True
    if has_url and body_len >= 80:
        return True
    if has_url and not is_generic_title(tit):  # short title ok if specific
        return True
    if has_url:  # URL alone still worth keeping if job board
        return True
    if title_ok and body_len >= 120:
        return True
    return False


def merge_candidate_skills(c: JobCandidate, blob: str) -> None:
    r, p = guess_skills(blob)
    if not c.required_skills and r:
        c.required_skills = r
    if not c.preferred_skills and p:
        c.preferred_skills = p


def extract_all_candidates(
    *,
    from_addr: str | None,
    subject: str,
    snippet: str | None,
    plain: str,
    html: str,
) -> tuple[list[JobCandidate], dict[str, Any]]:
    """Returns candidates and global debug meta."""
    combo_plain = plain or ""
    raw_urls = re.findall(r"https?://[^\s\"'<>]+", combo_plain + "\n" + html)
    urls = [clean_url(u) for u in raw_urls if not is_noise_url(u)]
    vendor = classify_vendor(from_addr, urls)
    debug_meta: dict[str, Any] = {
        "classified_vendor": vendor,
        "urls_found_count": len(raw_urls),
        "jobish_urls_count": len(prioritize_job_urls(urls)),
        "from_addr": from_addr,
    }

    candidates: list[JobCandidate] = []

    if vendor == EMAIL_VENDOR_LINKEDIN:
        candidates = extract_from_linkedin_html(html, plain, subject)
        if len(prioritize_job_urls(urls)) > 1 and len(candidates) < 2:
            alt = extract_from_url_lines(plain, html, subject, "linkedin")
            if len(alt) > len(candidates):
                candidates = alt
    elif vendor == EMAIL_VENDOR_HANDSHAKE:
        candidates = extract_from_handshake_html(html, plain, subject)
        if len(prioritize_job_urls(urls)) > 1 and len(candidates) < 2:
            alt = extract_from_url_lines(plain, html, subject, "handshake")
            if len(alt) > len(candidates):
                candidates = alt
    else:
        # Workday / Oracle / Indeed / generic: multi-URL plain text
        pj = prioritize_job_urls(urls)
        if len(pj) > 1:
            candidates = extract_from_url_lines(plain, html, subject, vendor)
        elif len(pj) == 1:
            u = pj[0]
            ctx = normalize_whitespace(combo_plain)[:8000]
            ts, cs = parse_subject_role_company(subject)
            title = clean_title(ts, None)
            candidates.append(
                JobCandidate(
                    title=title,
                    company=cs,
                    location=extract_location(ctx),
                    work_mode=guess_work_mode(ctx + "\n" + (snippet or "")),
                    industry=infer_industry(ctx, u),
                    job_url=u,
                    required_skills=guess_skills(ctx)[0],
                    preferred_skills=guess_skills(ctx)[1],
                    raw_block=ctx[:5000],
                    parser=f"{vendor}_single_url",
                    span_hint="single_board_url",
                )
            )

    if not candidates:
        ts, cs = parse_subject_role_company(subject)
        title = clean_title(ts, None)
        pj = prioritize_job_urls(urls)
        u = pj[0] if pj else None
        blob = normalize_whitespace(f"{subject}\n{snippet or ''}\n{combo_plain}")[:8000]
        if u or (title and not is_generic_title(title or "")):
            c = JobCandidate(
                title=title,
                company=cs,
                location=extract_location(blob),
                work_mode=guess_work_mode(blob),
                industry=infer_industry(blob, u),
                job_url=u,
                required_skills=guess_skills(blob)[0],
                preferred_skills=guess_skills(blob)[1],
                raw_block=blob,
                parser=f"{vendor}_subject_fallback",
                span_hint="subject_fallback",
            )
            merge_candidate_skills(c, blob)
            candidates.append(c)

    # Enrich from full text + dedupe by URL
    full_blob = normalize_whitespace(f"{subject}\n{snippet or ''}\n{combo_plain}")[:12000]
    dedup: dict[str, JobCandidate] = {}
    for i, c in enumerate(candidates):
        merge_candidate_skills(c, c.raw_block + "\n" + full_blob)
        if c.job_url:
            key = c.job_url.lower().split("?")[0]
        else:
            key = f"noturl:{i}:{normalize_whitespace((c.title or '') + (c.company or '')).lower()}"
        prev = dedup.get(key)
        if prev is None or len(c.raw_block or "") > len(prev.raw_block or ""):
            dedup[key] = c

    merged = list(dedup.values())
    filtered = [c for c in merged if passes_quality_gate(c)]
    debug_meta["candidates_before_filter"] = len(merged)
    debug_meta["candidates_after_filter"] = len(filtered)

    # Do not emit duplicate "empty" digest rows: if everything filtered out but we had job URLs, keep one best row
    if not filtered and merged:
        best = max(merged, key=lambda x: len(x.raw_block or "") + (5 if x.job_url else 0))
        if best.job_url or (best.title and not is_generic_title(best.title)):
            filtered = [best]

    if not filtered:
        debug_meta["skipped_reason"] = "no_quality_candidates"
        return [], debug_meta

    return filtered, debug_meta
