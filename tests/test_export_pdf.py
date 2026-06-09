"""Tests for PDF export (fpdf2 cursor width and text normalization edge cases)."""

from fpdf import FPDF
from fpdf.errors import FPDFException

from app.db.models import Job
from app.services.export import _normalize_pdf_text, _pdf_multi_cell, export_pdf_bytes


def _sample_job(**overrides) -> Job:
    base = dict(
        dedupe_hash="test-hash",
        title="Propulsion Intern",
        company="Example Aerospace",
        location="Sunnyvale, CA",
        work_mode="hybrid",
        job_url="https://example.com/jobs/123",
        source_type="manual_text",
        raw_description_text="Internship in propulsion, CFD, Python.",
        score_total=82.5,
        recommendation_tier="apply",
        application_difficulty="medium",
    )
    base.update(overrides)
    return Job(**base)


def test_fpdf_zero_width_when_x_at_right_margin():
    """Root cause: multi_cell(w=0) after default new_x=RIGHT leaves no printable width."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    pdf.multi_cell(0, 6, "Job title block")
    effective_w = pdf.w - pdf.r_margin - pdf.x
    assert effective_w < 1.0

    with_raises = False
    try:
        pdf.multi_cell(0, 5, "Score: 80.0 (apply)")
    except FPDFException:
        with_raises = True
    assert with_raises


def test_export_pdf_bytes_succeeds_for_typical_job():
    """Regression: export must not fail between title and body blocks."""
    pdf_bytes = export_pdf_bytes([_sample_job()])
    assert pdf_bytes.startswith(b"%PDF")
    assert len(pdf_bytes) > 500


def test_export_pdf_bytes_handles_edge_case_fields():
    pdf_bytes = export_pdf_bytes(
        [
            _sample_job(
                title="Role — Company™",
                company="Société Générale",
                job_url="https://example.com/" + "path/" * 60 + "id=abc",
                raw_description_text="A" * 800,
                location=None,
                recommendation_tier=None,
            ),
            _sample_job(
                title="",
                company="",
                job_url=None,
                raw_description_text="",
            ),
        ]
    )
    assert pdf_bytes.startswith(b"%PDF")


def test_normalize_pdf_text_replaces_unicode_and_breaks_long_tokens():
    long_token = "x" * 150
    normalized = _normalize_pdf_text(f"Title — {long_token}")
    assert "—" not in normalized
    assert "\u00ad" in normalized


def test_pdf_multi_cell_skips_blank_text_without_error():
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=10)
    _pdf_multi_cell(pdf, "   \n  ", 5)
    assert pdf.page_no() == 1
