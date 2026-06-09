"""Unit tests for scoring, query builder, profile parser, and OAuth error handling."""

import pytest

from app.db.models import Job, ProfileSnapshot
from app.services.scoring import (
    score_job_row,
    score_role_focus,
    compute_missing_qualifications,
)
from app.services.email_sync import build_gmail_query


# ---------------------------------------------------------------------------
# Existing: basic score_job_row
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Gmail query builder
# ---------------------------------------------------------------------------

def test_build_gmail_query_default():
    q = build_gmail_query()
    assert "newer_than:30d" in q


def test_build_gmail_query_custom_days():
    q = build_gmail_query(days=60)
    assert "newer_than:60d" in q


def test_build_gmail_query_clamps_days():
    q_low = build_gmail_query(days=1)
    assert "newer_than:7d" in q_low

    q_high = build_gmail_query(days=9999)
    assert "newer_than:180d" in q_high


def test_build_gmail_query_internships_focus():
    q = build_gmail_query(days=30, role_focus="Internships")
    # Should contain intern-related terms
    assert any(t in q.lower() for t in ("intern", "co-op", "student", "summer"))


def test_build_gmail_query_entry_level_focus():
    q = build_gmail_query(days=30, role_focus="Entry-level")
    assert any(t in q.lower() for t in ("entry level", "new grad", "junior", "associate"))


def test_build_gmail_query_any_role_focus():
    q = build_gmail_query(days=30, role_focus="Any role")
    assert "newer_than:30d" in q


def test_build_gmail_query_override():
    override = "from:boss@company.com newer_than:7d"
    q = build_gmail_query(days=30, role_focus="Internships", override=override)
    assert q == override


# ---------------------------------------------------------------------------
# Role-focus scoring
# ---------------------------------------------------------------------------

def test_role_focus_boosts_internship_role():
    blob = "Summer internship program for undergrad students in mechanical engineering"
    score = score_role_focus("Internships", blob)
    assert score >= 85.0, f"Expected >= 85, got {score}"


def test_role_focus_penalises_senior_role_in_intern_mode():
    blob = "Senior engineer position requiring 8+ years of experience"
    score = score_role_focus("Internships", blob, years_min=8)
    assert score <= 30.0, f"Expected <= 30 for senior role in intern mode, got {score}"


def test_role_focus_entry_level_matches_new_grad():
    blob = "New grad / entry-level software engineer, 0-2 years experience"
    score = score_role_focus("Entry-level", blob)
    assert score >= 85.0, f"Expected >= 85, got {score}"


def test_role_focus_any_role_returns_neutral():
    blob = "CEO position requiring 20 years of experience"
    score = score_role_focus("Any role", blob, years_min=20)
    assert score == 65.0


def test_role_focus_present_in_score_breakdown():
    job = Job(
        dedupe_hash="rf_test",
        title="Software Engineering Intern",
        company="Tech Co",
        location=None,
        work_mode="remote",
        job_url=None,
        source_type="manual_text",
        source_ref=None,
        raw_description_text="Summer 2025 internship, Python, data structures, co-op eligible",
        required_skills_json='["python"]',
        preferred_skills_json='[]',
        years_experience_min=None,
    )
    result = score_job_row(job, None, role_focus="Internships")
    assert "role_focus_match" in result["breakdown"]
    assert result["breakdown"]["role_focus_match"]["score"] >= 85.0


# ---------------------------------------------------------------------------
# Missing qualifications
# ---------------------------------------------------------------------------

def test_missing_qualifications_detected():
    job = Job(
        dedupe_hash="mq_test",
        title="ML Engineer Intern",
        company="AI Corp",
        location=None,
        work_mode="unknown",
        job_url=None,
        source_type="manual_text",
        source_ref=None,
        raw_description_text="Requires pytorch, kubernetes, and C++",
        required_skills_json='["pytorch", "kubernetes", "cpp"]',
        preferred_skills_json='[]',
        years_experience_min=None,
    )
    profile_skills = {"python", "numpy"}
    missing = compute_missing_qualifications(profile_skills, job.required_skills_json, job)
    # All three required skills are absent from the profile
    assert len(missing) >= 2
    lowered = [m.lower() for m in missing]
    assert any("pytorch" in m for m in lowered) or any("kubernetes" in m for m in lowered)


def test_no_missing_qualifications_when_profile_matches():
    job = Job(
        dedupe_hash="mq_match_test",
        title="Python Intern",
        company="Startup",
        location=None,
        work_mode="unknown",
        job_url=None,
        source_type="manual_text",
        source_ref=None,
        raw_description_text="Requires python and numpy",
        required_skills_json='["python", "numpy"]',
        preferred_skills_json='[]',
        years_experience_min=None,
    )
    profile_skills = {"python", "numpy", "pandas"}
    missing = compute_missing_qualifications(profile_skills, job.required_skills_json, job)
    assert missing == []


# ---------------------------------------------------------------------------
# Profile parser: text extraction
# ---------------------------------------------------------------------------

def test_profile_parser_extracts_pdf_text():
    """Profile parser can extract text from a minimal in-memory PDF."""
    from io import BytesIO
    from reportlab.lib.pagesizes import letter  # type: ignore
    from reportlab.pdfgen import canvas  # type: ignore
    from app.services.profile_parser import extract_text_from_pdf

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 700, "Python developer with numpy and pandas skills")
    c.save()
    pdf_bytes = buf.getvalue()

    text = extract_text_from_pdf(pdf_bytes)
    assert "python" in text.lower() or "developer" in text.lower()


def test_profile_parser_linkedin_file_saved_as_correct_kind():
    """save_linkedin_file stores the document as linkedin_export kind."""
    import hashlib
    from app.services.profile_parser import extract_text_from_pdf, save_profile_text
    from unittest.mock import patch

    captured = {}

    def fake_save(kind, content, title=None):
        captured["kind"] = kind
        captured["content"] = content
        # Return a minimal mock object
        from app.db.models import ProfileDocument
        doc = ProfileDocument.__new__(ProfileDocument)
        doc.id = 1
        doc.kind = kind
        doc.title = title
        doc.content_text = content
        doc.content_hash = hashlib.sha256(content.encode()).hexdigest()
        from datetime import datetime
        doc.created_at = datetime.utcnow()
        return doc

    with patch("app.services.profile_parser.save_profile_text", side_effect=fake_save):
        from app.services.profile_parser import save_linkedin_file
        save_linkedin_file.__wrapped__ = None  # avoid caching confusion

        # Simulate calling with a tiny text file
        from app.services.profile_parser import extract_text_from_upload
        with patch("app.services.profile_parser.extract_text_from_upload", return_value="LinkedIn profile text"):
            from app.services import profile_parser
            profile_parser.save_profile_text = fake_save
            result = profile_parser.save_linkedin_file("linkedin.pdf", b"fake_pdf")

    assert captured.get("kind") == "linkedin_export"


# ---------------------------------------------------------------------------
# Gmail OAuth error handling
# ---------------------------------------------------------------------------

def test_gmail_client_revoked_token_raises_friendly_error():
    """When the token refresh fails, a RuntimeError with reconnect instructions is raised."""
    from unittest.mock import MagicMock, patch
    from app.services.gmail_client import load_credentials

    # Simulate an expired credential with a refresh_token that fails
    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "fake_refresh_token"
    mock_creds.refresh.side_effect = Exception("Token has been expired or revoked.")

    with patch("app.services.gmail_client.Credentials.from_authorized_user_file", return_value=mock_creds):
        with patch("app.services.gmail_client.settings.gmail_token_path") as mock_path:
            mock_path.exists.return_value = True
            mock_path.__str__ = lambda self: "/fake/token/path"

            with pytest.raises(RuntimeError) as exc_info:
                load_credentials(interactive=False)

    error_msg = str(exc_info.value).lower()
    assert "revoked" in error_msg or "token" in error_msg
    # Should mention deleting the token file or reconnecting
    assert "delete" in error_msg or "reconnect" in error_msg or "gmail_oauth_setup" in error_msg


def test_gmail_client_missing_token_raises_friendly_error():
    """When no token exists and interactive=False, a helpful RuntimeError is raised."""
    from unittest.mock import patch
    from app.services.gmail_client import load_credentials

    with patch("app.services.gmail_client.settings.gmail_token_path") as mock_path:
        mock_path.exists.return_value = False
        mock_path.__str__ = lambda self: "/fake/token/path"
        mock_path.parent.mkdir = lambda **kw: None

        with pytest.raises(RuntimeError) as exc_info:
            load_credentials(interactive=False)

    error_msg = str(exc_info.value)
    assert "gmail_oauth_setup" in error_msg.lower() or "token" in error_msg.lower()
