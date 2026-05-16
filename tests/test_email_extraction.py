"""Tests for heuristic job extraction from email HTML (LinkedIn, Handshake)."""

from app.services.email_job_extract import JobCandidate, extract_all_candidates, passes_quality_gate


LINKEDIN_ALERT_HTML = """
<html><body>
<table>
<tr><td>
  <a href="https://www.linkedin.com/jobs/view/1111111">Aerospace Structures Intern</a>
  <p>Lockheed Martin · Sunnyvale, CA · Hybrid work model</p>
</td></tr>
<tr><td>
  <a href="https://www.linkedin.com/jobs/view/2222222">Propulsion Engineering Co-op</a>
  <p>Blue Origin · Kent, WA · On-site</p>
</td></tr>
</table>
<p><a href="https://www.linkedin.com/help/">Help</a></p>
</body></html>
"""

HANDSHAKE_ALERT_HTML = """
<div>
  <a href="https://joinhandshake.com/jobs/abc-123-def">Mechanical Engineering Intern — Turbines</a>
  <span>Atlanta, GA</span>
</div>
"""


def test_linkedin_extracts_multiple_jobs_with_urls():
    cands, meta = extract_all_candidates(
        from_addr="jobalerts@linkedin.com",
        subject="New jobs matching propulsion",
        snippet="2 new roles",
        plain="",
        html=LINKEDIN_ALERT_HTML,
    )
    assert meta.get("classified_vendor") == "linkedin"
    assert len(cands) >= 2
    urls = [c.job_url for c in cands if c.job_url]
    assert any("linkedin.com/jobs/view/1111111" in u for u in urls)
    assert any("linkedin.com/jobs/view/2222222" in u for u in urls)
    titles = [c.title for c in cands]
    assert any(t and "Structures Intern" in t for t in titles)
    assert any(c.work_mode in ("hybrid", "onsite", "remote", "unknown") for c in cands)


def test_handshake_extracts_job():
    cands, meta = extract_all_candidates(
        from_addr="notifications@joinhandshake.com",
        subject="New job opportunities",
        snippet="Handshake",
        plain="",
        html=HANDSHAKE_ALERT_HTML,
    )
    assert meta.get("classified_vendor") == "handshake"
    assert len(cands) >= 1
    assert cands[0].job_url and "joinhandshake.com" in cands[0].job_url
    assert cands[0].title and "Mechanical" in cands[0].title


def test_no_junk_when_only_generic_subject_and_noise_urls():
    cands, meta = extract_all_candidates(
        from_addr="news@example.com",
        subject="New job opportunities",
        snippet="",
        plain="",
        html="<p>Visit <a href='https://twitter.com/example'>our feed</a></p>",
    )
    assert len(cands) == 0 or meta.get("skipped_reason") == "no_quality_candidates"


def test_passes_quality_gate_requires_signal():
    assert not passes_quality_gate(
        JobCandidate(
            title="New job opportunities",
            company=None,
            location=None,
            work_mode="unknown",
            industry=None,
            job_url=None,
            raw_block="short",
            parser="x",
        )
    )
    assert passes_quality_gate(
        JobCandidate(
            title="Structures Intern",
            company="ACME",
            location=None,
            work_mode="unknown",
            industry=None,
            job_url="https://www.linkedin.com/jobs/view/1",
            raw_block="x" * 100,
            parser="t",
        )
    )
