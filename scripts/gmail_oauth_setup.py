"""First-time Gmail OAuth consent (desktop flow)."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.gmail_client import load_credentials


def main() -> None:
    print("Opening browser for Gmail authorization (readonly scope)...")
    load_credentials(interactive=True)
    print("Saved token. You can run the Streamlit app and sync Gmail.")


if __name__ == "__main__":
    main()
