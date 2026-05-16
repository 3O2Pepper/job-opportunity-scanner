"""Gmail API client: OAuth token loading and service builder."""

from __future__ import annotations

from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from app.config import settings

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def load_credentials(interactive: bool = False) -> Credentials:
    """Load or refresh OAuth credentials; optionally run interactive consent."""
    cred_path = settings.gmail_credentials_path
    token_path = settings.gmail_token_path
    _ensure_parent(token_path)

    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    if not interactive:
        raise RuntimeError(
            "Gmail token missing or expired. Run: python scripts/gmail_oauth_setup.py"
        )

    if not cred_path.exists():
        raise FileNotFoundError(
            f"Missing OAuth client file at {cred_path}. "
            "Download credentials JSON from Google Cloud Console."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(cred_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def build_gmail_service(interactive: bool = False):
    creds = load_credentials(interactive=interactive)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def test_list_messages(max_results: int = 5) -> list[dict]:
    service = build_gmail_service(interactive=False)
    resp = (
        service.users()
        .messages()
        .list(userId="me", maxResults=max_results)
        .execute()
    )
    return resp.get("messages", [])
