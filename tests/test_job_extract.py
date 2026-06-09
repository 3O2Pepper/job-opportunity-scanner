"""Tests for manual job ingestion helpers."""

import pytest

from app.services.job_extract import ingest_manual_link


def test_ingest_manual_link_rejects_invalid_scheme():
    with pytest.raises(ValueError, match="http:// or https://"):
        ingest_manual_link(None, "ftp://example.com/jobs/1")  # type: ignore[arg-type]
