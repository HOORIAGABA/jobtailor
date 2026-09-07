# Setup Guide

Complete local setup plus optional integrations (Google OAuth, resume-parser
stack, real LLM, PostgreSQL, email, deployment). Uses **Python 3.11** — all
wheels/bindings (lxml, pydantic-core) are built for cp311.

## 0. Requirements

| Item | Notes |
|---|---|
| Python **3.11** | Required. Do **not** use system Python if it is 3.12/3.14. |
| Windows 10/11 | Tested here; the app also works on macOS/Linux |
| Chrome | For the extension |

If you don't have Python 3.11: <https://www.python.org/downloads/release/python-3110/>

## 1. Create & activate a venv (backend/)

```powershell
cd backend
C:\path\to\Python311\python.exe -m venv venv
venv\Scripts\activate
```

Verify: `python --version` → `Python 3.11.x`.

## 2. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

## 3. Create the environment file

```powershell
Copy-Item .env.example .env
```

Defaults to `LLM_PROVIDER=mock` (zero cost, no API key) and a local SQLite DB.

## 4. Build the DOCX template (once)

```powershell
python -m app.rendering.build_template
```

Generates `backend/app/rendering/../templates/resume_template.docx`. Required
before the renderer can produce tailored resumes.

## 5. Start the API

```powershell
python -m uvicorn app.main:app --reload
```

Server: <http://localhost:8000> · health: <http://localhost:8000/health>

## 6. Verify (optional but recommended)

```powershell
python smoke_test.py        # isolated to a temp dir, never touches real data
```

## 7. Open the web app (Next.js / React)

The production UI lives in `frontend/` (Next.js 14 + TypeScript + Tailwind). In a
second terminal:

```powershell
cd frontend
npm install            # first time only
npm run dev            # -> http://localhost:3000
```

It defaults to the local backend at `http://127.0.0.1:8000`; for a deployed
backend set `NEXT_PUBLIC_API_BASE_URL` in `frontend/.env.local` (see
`frontend/.env.local.example`).

Register → upload a resume → add profile details (optional) → submit a job
post (paste or URL) → the dashboard polls until the pipeline is `ready`, then
lets you download / approve / send.

> **Contact auto-fill:** uploading a resume runs `contact_extractor`, which
> detects email/phone/location/LinkedIn/GitHub from the text and fills them
> into the profile **only if they're still empty** — your manually-set values
> always win. The extracted fields (including GitHub) appear in the rendered
> resume header.

(`legacy/index.html` is a legacy single-page test harness — optional, kept
for quick reference only.)

## 8. Load the Chrome extension

1. `chrome://extensions` → enable **Developer mode**.
2. **Load unpacked** → select `extension/`.
3. Log in (password or Google) in the popup, open a LinkedIn post, click
   **Tailor resume for this post**. The popup polls until ready, then lets
   you download / approve / send.

`API_BASE_URL` in `extension/background.js` = `http://127.0.0.1:8000` (change
for a deployed backend).

---

## Optional: Google OAuth

1. Create OAuth **Web application** credentials in Google Cloud Console.
2. Add redirect URI: `http://127.0.0.1:8000/auth/google/callback`
3. Set in `backend/.env`:
   ```env
   GOOGLE_CLIENT_ID=your_client_id
   GOOGLE_CLIENT_SECRET=your_client_secret
   GOOGLE_REDIRECT_URI=http://127.0.0.1:8000/auth/google/callback
   ```
4. For the extension, the redirect URI is auto-generated via
   `chrome.identity.getRedirectURL("google-oauth")` — add that
   `chrome-extension://…` URI to the allowed redirects too.

## Optional: Resume parser stack

The parser uses its own OpenAI-compatible serving profile so a more reliable
extraction model can be used without changing the rest of the pipeline. Keep
this endpoint running **before** uploading a resume. Uploaded headings
(`WORK EXPERIENCE`, `PROJECTS`, …) are preserved into the rendered DOCX.

```env
RESUME_PARSER_PROVIDER=openai_compatible
RESUME_PARSER_BASE_URL=http://localhost:8001/v1
RESUME_PARSER_MODEL=Qwen/Qwen3-8B-Instruct   # or NuExtract-3
```

Latest verified local config (Ollama):
```env
RESUME_PARSER_PROVIDER=openai_compatible
RESUME_PARSER_BASE_URL=http://localhost:11434/v1
RESUME_PARSER_MODEL=llama3.1:8b-instruct-q4_K_M
```

## Optional: real LLM instead of mock

```env
LLM_PROVIDER=groq                      # free, fast, OpenAI-compatible
LLM_API_KEY=your_groq_key
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=llama-3.1-70b-versatile
```

or a local OpenAI-compatible endpoint (e.g. Ollama):

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=llama3.1:8b-instruct-q4_K_M
```

Tailoring, outreach, job parsing, and review all share `app/agents/llm_client.py`.

## Optional: PostgreSQL (Supabase / Neon free tier)

```env
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

Restart — SQLAlchemy handles SQLite and Postgres transparently.

## Optional: email sending

Email comes from **each user's own Gmail**. There are two ways to enable it:

### Gmail API (recommended, password-free)

The app can send with the **Gmail API** via OAuth, so **no App Password is
needed**. A one-time "Connect Gmail" flow (profile section in the web app or
Chrome extension) stores a refresh token, after which every send goes out
authorized as the logged-in user.

1. Open the [Google Cloud Console](https://console.cloud.google.com) and
   create or select a project.
2. Enable the **Gmail API** (APIs & Services → Library → Gmail API → Enable).
3. Go to **APIs & Services → OAuth consent screen**, add the
   `https://www.googleapis.com/auth/gmail.send` scope, and set after your app
   is verified if needed.
4. Create **OAuth client ID → Web application** under APIs & Services →
   Credentials.
5. Add the authorized redirect URI — it must match `GMAIL_REDIRECT_URI`:
   ```
   http://127.0.0.1:8000/auth/gmail/callback
   ```
6. Paste the client ID and secret into `backend/.env` and restart:

   ```env
   GMAIL_CLIENT_ID=your_client_id.apps.googleusercontent.com
   GMAIL_CLIENT_SECRET=your_client_secret
   GMAIL_REDIRECT_URI=http://127.0.0.1:8000/auth/gmail/callback
   ```

Then click **Connect Gmail** in the profile and approve the consent screen
once. The token is stored per-user on their account (`User.oauth_tokens`).

### SMTP + App Password (fallback)

If Gmail API isn't set up, users can instead enter their Gmail address + a
Gmail **App Password** (Google Account → Security → App passwords — requires
two-step verification). The app password is write-only and stored per-user.

`backend/.env` `SMTP_*` remains only a shared fallback:
```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=your_gmail_address@gmail.com
SMTP_APP_PASSWORD=your_gmail_app_password
```

The `send` endpoint always tries Gmail API first, then SMTP:

## Deployment ($0)

- **Backend:** Render / Fly.io free tier (Dockerfile ready).
- **Database:** Supabase / Neon free Postgres (`DATABASE_URL`).
- **Frontend:** Vercel / Netlify.
- **LLM:** Groq free tier.

## Troubleshooting

| Issue | Cause | Fix |
|---|---|---|
| `python` not found / wrong version | System Python is 3.12/3.14 | Use the 3.11 venv: `venv\Scripts\activate` then `python -m pip ...` |
| `ModuleNotFoundError: docx` | Wrong interpreter | Recreate venv with Python 3.11 (step 1) |
| `Failed to build a native library through cargo` (pyo3) | Python 3.12+ with old PyO3 | **Use Python 3.11 only** |
| Download says "still being tailored" | Pipeline still running | Wait — the UI polls automatically; try again in ~10 s |
| Output `failed` with "server restarted" | Process restarted mid-pipeline | Submit the job post again to re-run it |
