# Privacy & data handling

This application is designed for **personal, local use** on your machine.

## What is stored locally

- **SQLite database** (`data/app.db` by default): profile text you paste or upload, extracted job records, scores, and Gmail message metadata you synced (subject, snippet, ids).
- **OAuth token** (`data/tokens/gmail_token.json`): Gmail API refresh token after you consent.
- **Optional Google OAuth client file** (`data/credentials.json`): **not** secret like a password, but treat it like an app credential file.

## Gmail content

The scanner reads email metadata and bodies **only through the Gmail API** after **you** authorize it. Adjust the Gmail query string to limit what is fetched.

## Paid AI APIs

OpenAI and Anthropic calls are **disabled by default**. When enabled, job text may be sent to the provider you configure—only enable this if you accept their terms and pricing.

## Your responsibility

- Do not paste confidential employer information if your threat model does not allow local storage.
- For third-party job URLs, only fetch pages you are permitted to access.
