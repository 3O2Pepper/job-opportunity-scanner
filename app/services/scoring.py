"""Weighted scoring for jobs against profile snapshot."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from sqlalchemy import select

from app.config import settings
from app.db.models import Job, ProfileSnapshot
from app.db.session import session_scope


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_weights() -> dict:
    return _load_yaml(settings.scoring_weights_path)


def load_interests() -> dict:
    return _load_yaml(settings.interest_keywords_path)


def _expand_skills(skills: list[str], synonyms: dict[str, list[str]]) -> set[str]:
    out: set[str] = set()
    for s in skills:
        low = s.lower().strip()
        if not low:
            continue
        out.add(low)
        for canonical, alts in synonyms.items():
            if low == canonical or low in [a.lower() for a in alts]:
                out.add(canonical.lower())
                out.update(a.lower() for a in alts)
    return out


def scoring_context_blob(job: Job) -> str:
    """Subject + snippet + structured fields + body — for scoring sparse email alerts."""
    parts: list[str] = []
    if job.extraction_debug_json:
        try:
            d = json.loads(job.extraction_debug_json)
            subj = d.get("email_subject")
            snip = d.get("email_snippet")
            if subj:
                parts.append(str(subj))
            if snip:
                parts.append(str(snip))
        except json.JSONDecodeError:
            pass
    for field in (job.title, job.company, job.location, job.industry, job.raw_description_text):
        if field:
            parts.append(str(field))
    return "\n".join(parts)


def score_technical_match(profile_skills: set[str], job_text: str, req_json: str | None, pref_json: str | None) -> float:
    synonyms = load_interests().get("skill_synonyms") or {}
    prof = _expand_skills(list(profile_skills), synonyms)

    req = []
    pref = []
    try:
        if req_json:
            req = json.loads(req_json)
        if pref_json:
            pref = json.loads(pref_json)
    except json.JSONDecodeError:
        pass
    job_skills = _expand_skills([str(x) for x in req + pref], synonyms)

    blob = job_text.lower()
    for s in list(prof):
        if len(s) <= 3:
            continue
        if s in blob:
            job_skills.add(s)

    cfg_interest = load_interests()
    sparse_terms = (cfg_interest.get("sparse_signal_terms") or {}).get("engineering_terms") or []
    short_alert = len(blob) < 700
    sparse_hits = 0
    if sparse_terms and (len(job_skills) < 4 or short_alert):
        for kw in sparse_terms:
            k = str(kw).lower()
            if k in blob:
                job_skills.add(k.replace(" ", "_"))
                sparse_hits += 1

    if not job_skills:
        return 35.0

    inter = len(prof & job_skills)
    union = len(prof | job_skills) or 1
    jaccard = inter / union
    base = 40.0 + 60.0 * jaccard

    if not prof and sparse_hits:
        base = max(base, min(78.0, 42.0 + sparse_hits * 6.5))

    if prof and sparse_hits and short_alert:
        base = min(100.0, base + min(15.0, sparse_hits * 2.5))

    return round(min(100.0, base), 1)


def score_industry_interest(job_blob: str, weights_cfg: dict | None = None) -> float:
    cfg = weights_cfg or load_interests()
    groups = cfg.get("interest_groups") or {}
    total_w = 0.0
    score = 0.0
    text = job_blob.lower()
    for _name, spec in groups.items():
        w = float(spec.get("weight", 1.0))
        terms = spec.get("terms") or []
        hits = sum(1 for t in terms if t.lower() in text)
        total_w += w
        score += w * min(1.0, hits / 3.0)
    if total_w <= 0:
        val = 50.0
    else:
        val = min(100.0, 100.0 * (score / total_w))

    if len(text) < 900:
        role_prefs = cfg.get("role_preferences") or {}
        intern_terms = role_prefs.get("internship_terms") or []
        if any(str(t).lower() in text for t in intern_terms):
            val = min(100.0, val + 14.0)
    return round(val, 1)


def score_experience_fit(profile_band: str, job_min: int | None, job_blob: str) -> float:
    t = job_blob.lower()
    wants_intern = any(x in t for x in ("internship", "intern ", "co-op", "coop"))
    wants_entry = any(x in t for x in ("entry level", "new grad", "early career"))

    if profile_band == "intern":
        if wants_intern:
            return 95.0
        if wants_entry:
            return 70.0
        if job_min is None or job_min <= 1:
            return 65.0
        return max(20.0, 80.0 - job_min * 12.0)

    if profile_band == "entry":
        if wants_intern:
            return 55.0
        if wants_entry or job_min is None or job_min <= 2:
            return 90.0
        return max(25.0, 85.0 - max(0, job_min - 2) * 15.0)

    if profile_band == "early":
        if job_min is None:
            return 75.0
        if job_min <= 3:
            return 85.0
        return max(30.0, 90.0 - (job_min - 3) * 12.0)

    if job_min is None:
        return 70.0
    return max(35.0, 95.0 - max(0, job_min - 2) * 10.0)


def score_location_remote(merged_profile: str, job_mode: str | None, job_location: str | None, job_blob: str) -> float:
    mp = merged_profile.lower()
    prefers_remote = "remote" in mp and "not remote" not in mp
    open_reloc = any(x in mp for x in ("relocat", "relocate", "willing to move"))

    mode = (job_mode or "unknown").lower()
    blob = job_blob.lower()

    if prefers_remote:
        if mode == "remote":
            return 95.0
        if mode == "hybrid":
            return 70.0
        if "remote" in blob:
            return 75.0
        return 35.0

    if open_reloc:
        return 80.0

    return 65.0


def score_company_project(job_blob: str, company: str | None) -> float:
    text = f"{company or ''}\n{job_blob}".lower()
    markers = [
        "aerospace",
        "aircraft",
        "spacecraft",
        "defense",
        "national laboratory",
        "robotics",
        "energy",
        "geothermal",
        "renewable",
        "propulsion",
        "wind tunnel",
    ]
    hits = sum(1 for m in markers if m in text)

    if len(text) < 450:
        eng_kw = ("engineer", "engineering", "mechanical", "aerospace", "systems", "design", "r&d")
        hits += sum(1 for k in eng_kw if k in text)

    return round(min(100.0, 35.0 + hits * 12.0), 1)


def score_ease_of_application(job_url: str | None, job_blob: str, weights: dict) -> float:
    cfg = weights.get("application_difficulty") or {}
    easy_hosts = cfg.get("easy_hosts") or []
    medium_kw = cfg.get("medium_keywords") or []

    url = (job_url or "").lower()
    score = 55.0
    if any(h in url for h in easy_hosts):
        score += 30.0

    low = job_blob.lower()
    if any(k in low for k in medium_kw):
        score -= 20.0

    return max(10.0, min(100.0, round(score, 1)))


def tier_for_score(total: float, weights_doc: dict) -> tuple[str, str]:
    tiers = sorted(weights_doc.get("tiers") or [], key=lambda x: -float(x.get("min", 0)))
    for t in tiers:
        if total >= float(t.get("min", 0)):
            return str(t.get("code")), str(t.get("label"))
    return "skip", "Skip"


def compute_missing_qualifications(profile_skills: set[str], req_json: str | None, job: Job) -> list[str]:
    missing: list[str] = []
    req: list[str] = []
    try:
        if req_json:
            req = json.loads(req_json)
    except json.JSONDecodeError:
        req = []
    blob = scoring_context_blob(job).lower()
    synonyms = load_interests().get("skill_synonyms") or {}
    expanded_prof = _expand_skills(list(profile_skills), synonyms)
    for r in req:
        rl = str(r).lower()
        if rl in expanded_prof:
            continue
        if rl in blob and rl not in expanded_prof:
            missing.append(str(r))
        elif rl not in blob:
            missing.append(str(r))
    return missing[:12]


def suggest_resume_keywords(profile_skills: set[str], req_json: str | None, job: Job) -> list[str]:
    missing = compute_missing_qualifications(profile_skills, req_json, job)
    return missing[:8]


def suggest_cover_bullets(job_title: str | None, company: str | None, interests_hit: str) -> list[str]:
    j = job_title or "this role"
    c = company or "the team"
    return [
        f"Highlight hands-on engineering projects aligned with {interests_hit} for {j}.",
        f"Connect coursework or research to responsibilities listed by {c}.",
        f"Mention collaboration, documentation, and safety-conscious design practices.",
    ]


def estimate_application_difficulty(job_url: str | None, job_blob: str, weights: dict) -> str:
    s = score_ease_of_application(job_url, job_blob, weights)
    if s >= 75:
        return "Easy–Medium"
    if s >= 55:
        return "Medium"
    return "Medium–Hard"


def score_job_row(job: Job, profile: ProfileSnapshot | None, weights_doc: dict | None = None) -> dict:
    weights_doc = weights_doc or load_weights()
    wmap = weights_doc.get("weights") or {}

    merged = profile.merged_text if profile else ""
    structured = {}
    if profile and profile.structured_json:
        try:
            structured = json.loads(profile.structured_json)
        except json.JSONDecodeError:
            structured = {}

    prof_skills = set(str(s).lower() for s in structured.get("skills") or [])
    band = str(structured.get("experience_band") or "general")

    blob = scoring_context_blob(job)

    comp_scores = {
        "technical_skill_match": score_technical_match(prof_skills, blob, job.required_skills_json, job.preferred_skills_json),
        "industry_interest_alignment": score_industry_interest(blob),
        "experience_level_fit": score_experience_fit(band, job.years_experience_min, blob),
        "location_remote_preference": score_location_remote(merged, job.work_mode, job.location, blob),
        "company_project_relevance": score_company_project(blob, job.company),
        "ease_of_application": score_ease_of_application(job.job_url, blob, weights_doc),
    }

    total = 0.0
    breakdown = {}
    for key, val in comp_scores.items():
        weight = float(wmap.get(key, 0))
        weighted = val * weight
        total += weighted
        breakdown[key] = {"score": val, "weight": weight, "weighted": round(weighted, 3)}

    total = round(min(100.0, max(0.0, total)), 2)
    code, label = tier_for_score(total, weights_doc)

    missing = compute_missing_qualifications(prof_skills, job.required_skills_json, job)
    keywords = suggest_resume_keywords(prof_skills, job.required_skills_json, job)
    interests_hit = "aerospace and mechanical engineering impact"
    bullets = suggest_cover_bullets(job.title, job.company, interests_hit)

    explain_parts = [
        f"{k.replace('_', ' ')}: {v['score']} (weight {v['weight']})"
        for k, v in breakdown.items()
        if isinstance(v, dict) and "score" in v
    ]
    reason = "; ".join(explain_parts[:6])

    breakdown["total"] = total
    breakdown["tier_code"] = code
    breakdown["tier_label"] = label
    breakdown["explain_summary"] = reason

    return {
        "total": total,
        "breakdown": breakdown,
        "tier_code": code,
        "tier_label": label,
        "missing_qualifications": missing,
        "resume_keywords": keywords,
        "cover_bullets": bullets,
        "reason": reason,
        "application_difficulty": estimate_application_difficulty(job.job_url, blob, weights_doc),
    }


def apply_scores_to_job(job: Job, result: dict) -> None:
    job.score_total = result["total"]
    job.score_breakdown_json = json.dumps(result["breakdown"])
    job.recommendation_tier = result["tier_code"]
    job.missing_qualifications_json = json.dumps(result["missing_qualifications"])
    job.resume_keywords_json = json.dumps(result["resume_keywords"])
    job.cover_bullet_points_json = json.dumps(result["cover_bullets"])
    job.application_difficulty = result["application_difficulty"]


def recompute_scores(job_ids: list[int] | None = None) -> int:
    """Re-score all jobs or a subset. Returns number of rows updated."""
    weights_doc = load_weights()
    updated = 0
    with session_scope() as session:
        profile = session.scalar(select(ProfileSnapshot).order_by(ProfileSnapshot.id.desc()).limit(1))
        q = select(Job)
        if job_ids:
            q = q.where(Job.id.in_(job_ids))
        jobs = session.scalars(q).all()
        for job in jobs:
            result = score_job_row(job, profile, weights_doc)
            apply_scores_to_job(job, result)
            updated += 1
        session.commit()
    return updated


def alignment_summary(job: Job, profile: ProfileSnapshot | None) -> str:
    if not profile:
        return "Add profile documents to explain goal alignment."
    interests = load_interests().get("interest_groups") or {}
    blob = scoring_context_blob(job).lower()
    hits = []
    for name, spec in interests.items():
        for term in spec.get("terms") or []:
            if term.lower() in blob:
                hits.append(name)
                break
    if not hits:
        return "Alignment is broad—consider tuning keywords in config/interest_keywords.yaml."
    return "Matches your stated interests in: " + ", ".join(sorted(set(hits)))
