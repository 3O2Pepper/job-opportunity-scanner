"""Optional LLM enrichment — only runs when explicitly enabled and keyed."""

from __future__ import annotations

import json

from app.config import settings
from app.db.models import Job


def maybe_enrich_job_with_llm(job: Job) -> None:
    """Best-effort structured extraction; no-op if disabled or missing keys."""
    if not settings.enable_llm:
        return

    prompt_payload = {
        "title": job.title,
        "company": job.company,
        "description_excerpt": (job.raw_description_text or "")[:6000],
    }

    try:
        if settings.llm_provider == "anthropic" and settings.anthropic_api_key:
            text = _call_anthropic(prompt_payload)
        elif settings.openai_api_key:
            text = _call_openai(prompt_payload)
        else:
            return
    except Exception:
        return

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return

    job.title = data.get("title") or job.title
    job.company = data.get("company") or job.company
    job.location = data.get("location") or job.location
    wm = data.get("work_mode")
    if wm in ("remote", "hybrid", "onsite", "unknown"):
        job.work_mode = wm
    job.industry = data.get("industry") or job.industry
    if data.get("required_skills"):
        job.required_skills_json = json.dumps(data["required_skills"])
    if data.get("preferred_skills"):
        job.preferred_skills_json = json.dumps(data["preferred_skills"])
    job.degree_requirements = data.get("degree_requirements") or job.degree_requirements
    job.deadline = data.get("deadline") or job.deadline
    ymin = data.get("years_experience_min")
    ymax = data.get("years_experience_max")
    if ymin is not None:
        try:
            job.years_experience_min = int(ymin)
        except (TypeError, ValueError):
            pass
    if ymax is not None:
        try:
            job.years_experience_max = int(ymax)
        except (TypeError, ValueError):
            pass


def _call_openai(payload: dict) -> str:
    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    schema_hint = (
        "Return ONLY valid JSON with keys: "
        "title, company, location, work_mode (remote|hybrid|onsite|unknown), "
        "required_skills (array of strings), preferred_skills (array), "
        "years_experience_min (int or null), years_experience_max (int or null), "
        "degree_requirements (string or null), industry (string or null), deadline (string or null)."
    )
    completion = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You extract job posting fields from messy text."},
            {"role": "user", "content": schema_hint + "\n\nPayload:\n" + json.dumps(payload)},
        ],
        temperature=0.2,
    )
    return completion.choices[0].message.content or "{}"


def _call_anthropic(payload: dict) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    schema_hint = (
        "Return ONLY valid JSON with keys: "
        "title, company, location, work_mode (remote|hybrid|onsite|unknown), "
        "required_skills (array of strings), preferred_skills (array), "
        "years_experience_min (int or null), years_experience_max (int or null), "
        "degree_requirements (string or null), industry (string or null), deadline (string or null)."
    )
    msg = client.messages.create(
        model="claude-3-haiku-20240307",
        max_tokens=1024,
        messages=[
            {"role": "user", "content": schema_hint + "\n\nPayload:\n" + json.dumps(payload)}
        ],
    )
    parts: list[str] = []
    for block in msg.content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts) or "{}"
