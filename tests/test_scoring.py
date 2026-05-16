from app.db.models import Job, ProfileSnapshot
from app.services.scoring import score_job_row


def test_score_job_row_without_profile():
    job = Job(
        dedupe_hash="x",
        title="Propulsion Intern",
        company="Example Aerospace",
        location=None,
        work_mode="unknown",
        job_url="https://example.com",
        source_type="manual_text",
        source_ref=None,
        raw_description_text="Internship in propulsion, CFD, Python, aerodynamics",
        required_skills_json='["python","cfd"]',
        preferred_skills_json='[]',
        years_experience_min=None,
    )
    result = score_job_row(job, None)
    assert 0 <= result["total"] <= 100
    assert result["tier_code"] in {"strong_apply", "apply", "maybe", "skip"}
