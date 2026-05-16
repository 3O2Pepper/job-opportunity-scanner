# Personal job opportunity scanner

Local-first Python tool that ingests job leads from **Gmail (OAuth)**, **pasted descriptions**, and **URLs you provide**, then scores them against your resume/profile using a transparent weighted rubric. **Optional** OpenAI/Anthropic enrichment is **off by default**.

## Quick start (Windows)

```powershell
cd "C:\Users\enzos\Documents\Job aplications"
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
streamlit run app/ui/streamlit_app.py
```

Run Streamlit from the **project root** so imports like `app.config` resolve.

## Gmail setup (Google Cloud)

1. Create a Google Cloud project and enable the **Gmail API**.
2. Configure the **OAuth consent screen** (External test users can include only your account during testing).
3. Create **OAuth client ID** credentials of type **Desktop app**.
4. Download the JSON file and save it as `data/credentials.json` (or set `GMAIL_CREDENTIALS_PATH` in `.env`).
5. Run the one-time consent helper:

```powershell
python scripts/gmail_oauth_setup.py
```

This stores a refresh token at `data/tokens/gmail_token.json`. Scope used: `https://www.googleapis.com/auth/gmail.readonly`.

Edit `GMAIL_QUERY` in `.env` to narrow which messages are scanned.

## Configuration

| Env variable | Purpose |
|--------------|---------|
| `DATABASE_URL` | SQLite URL (default writes under `data/app.db`) |
| `GMAIL_CREDENTIALS_PATH` | OAuth client JSON path |
| `GMAIL_TOKEN_PATH` | Saved token path |
| `GMAIL_QUERY` | Gmail search query for sync |
| `GMAIL_SYNC_MAX_RESULTS` | Cap per sync |
| `ENABLE_LLM` | `false` by default |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | Only if you enable LLM |

YAML knobs:

- [`config/scoring_weights.yaml`](config/scoring_weights.yaml) — component weights and tier cutoffs.
- [`config/interest_keywords.yaml`](config/interest_keywords.yaml) — industry/skill interest keywords.

## Safety / legal posture

- **No LinkedIn scraping** and **no auto-apply**. Public URLs are fetched only when **you** submit them; obey site terms and robots rules yourself.
- Gmail access is **read-only** and **user-authorized**.
- See [`docs/privacy.md`](docs/privacy.md) for what is stored locally.

## Tests

```powershell
pytest
```

## Project layout

- `app/` — Python package (DB models, services, Streamlit UI)
- `config/` — scoring YAML
- `data/` — local DB + tokens (gitignored at runtime)
- `scripts/` — OAuth helper
- `docs/` — privacy notes
