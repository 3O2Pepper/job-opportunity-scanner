"""Shared text helpers for job parsing (keeps job_extract ↔ email_job_extract import cycle free)."""

from __future__ import annotations

import re

_YEAR_EXP_RE = re.compile(
    r"(?P<n>\d+)\s*[\+\-]?\s*(?:years?|yrs?)\s+(?:of\s+)?experience",
    re.I,
)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def guess_work_mode(text: str) -> str:
    t = text.lower()
    if "hybrid" in t:
        return "hybrid"
    if "remote" in t and "no remote" not in t and "not remote" not in t:
        return "remote"
    if any(x in t for x in ("on-site", "onsite", "on site")):
        return "onsite"
    return "unknown"


def guess_skills(text: str) -> tuple[list[str], list[str]]:
    t = text.lower()
    candidates = [
        "python",
        "matlab",
        "c++",
        "cfd",
        "fea",
        "solidworks",
        "catia",
        "nx",
        "ansys",
        "abaqus",
        "composites",
        "machining",
        "aerodynamics",
        "controls",
        "robotics",
        "thermal",
        "hvac",
        "cad",
        "sql",
        "linux",
        "mechanical engineering",
        "aerospace",
        "propulsion",
        "matlab simulink",
    ]
    hits = []
    for w in candidates:
        if w in t and w not in hits:
            hits.append(w)
    required = hits[:12]
    preferred = hits[12:24]
    return required, preferred


def parse_years_experience(text: str) -> tuple[int | None, int | None]:
    m = _YEAR_EXP_RE.search(text)
    if not m:
        return None, None
    n = int(m.group("n"))
    return n, None
