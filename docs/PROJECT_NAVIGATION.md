# Agentic Resume Tailoring + Outreach System Project Navigation

This is the navigation guide for understanding the codebase and running it step by step.
For a module-by-module breakdown, see `docs/PROJECT_MODULES.md`.

## What This Project Does

This system is a FastAPI app that:
- ingests a resume
- ingests a job post
- parses the job requirements
- tailors the resume (deterministic relevance reordering + guardrails)
- renders a DOCX resume
- drafts outreach text
- supports human approval before sending
- can send via Gmail API (OAuth) or SMTP
- can capture LinkedIn posts through the Chrome extension
- can run the resume parser on a dedicated Qwen3 or NuExtract 3 stack

## Recommended Runtime

Use **Python 3.11**.

Why:
- the repo was verified with cp311 wheels
- Python 3.14 causes build issues for some dependencies
- the project is stable in a 3.11 venv

## Project Layout

### `backend/`
Main application.

#### `backend/app/main.py`
- creates the FastAPI app
- sets CORS
- mounts routers
- creates tables on startup

#### `backend/app/config.py`
- reads `.env`
- stores DB, JWT, LLM, SMTP settings

#### `backend/app/database.py`
- SQLAlchemy engine/session setup

#### `backend/app/models.py`
- database tables
- `User`
- `Resume`
- `JobPost`
- `TailoredOutput`
- `Message`

#### `backend/app/schemas.py`
- Pydantic request/response models
- heading-aware resume schema

#### `backend/app/auth.py`
- password hashing
- JWT creation/verification
- current user dependency

#### `backend/app/routers/auth.py`
- register/login
- Google OAuth login flow
- logout
- `/auth/me` — return / update the profile (full name, phone, location,
  LinkedIn, GitHub, and SMTP credentials)
- `/auth/gmail/status`, `/auth/gmail/connect`, `/auth/gmail/callback` —
  Gmail API OAuth (password-free sending)

#### `backend/app/routers/resumes.py`
- upload resume
- parse PDF/DOCX
- edit parsed resume
- runs `contact_extractor` on the raw text and routes detected contact info
  (full name, phone, location, LinkedIn, GitHub) into the profile as a
  **fallback only** — user-set fields are never overwritten

#### `backend/app/routers/job_posts.py`
- submit job post
- fetch if URL is provided
- run the full pipeline

#### `backend/app/routers/tailored_outputs.py`
- get tailored output
- download DOCX/PDF
- approve output
- send email

#### `backend/app/agents/`
- `resume_parser_agent.py`
- `job_parser_agent.py`
- `tailoring_agent.py`
- `outreach_agent.py`
- `review_agent.py`
- `orchestrator.py`
- `llm_client.py`
- `contact_extractor.py` — extractions for email/phone/location/LinkedIn/GitHub
  from raw resume text

#### `backend/app/rendering/`
- DOCX template creation
- resume rendering
- preserves parsed headings in the output DOCX

#### `backend/app/fetchers/url_fetcher.py`
- URL extraction with `httpx` and `trafilatura`

#### `backend/app/email_utils/`
- `gmail_api.py` — Gmail API OAuth sender (consent URL, token exchange for
  `gmail.send`, refresh, RFC822 send with PDF attachment)
- `sender.py` — SMTP sending with attachment

#### `backend/smoke_test.py`
- end-to-end test of the main flow

### `extension/`
Chrome extension.

- `manifest.json` defines permissions and scripts
- `content_script.js` reads LinkedIn post text
- `background.js` owns auth token and backend calls
- `popup.html` and `popup.js` are the extension UI

### `frontend/`
Next.js 14 (React + TypeScript + Tailwind) production web app.

- `app/page.tsx` / `app/layout.tsx` — landing page and root layout.
- `app/login/page.tsx` — login / register (plus Google OAuth popup).
- `app/dashboard/page.tsx` — the main dashboard (auth-gated).
- `components/dashboard/` — ProfilePanel (incl. Connect Gmail + GitHub),
  ResumePanel, JobPanel, ResultPanel (polling + approve/send/download),
  HistoryPanel.
- `lib/api.ts` — typed client for every backend endpoint (token in localStorage).
- `lib/auth-context.tsx` — React auth provider.
- Configure the backend URL with `NEXT_PUBLIC_API_BASE_URL` (default
  `http://127.0.0.1:8000`).

### `legacy/`
Legacy single-page test harness (`index.html`, optional, kept for quick reference).

### `docs/`
Recipes / references (setup, run, architecture):

- **`SETUP_GUIDE.md`** — full local setup + optional integrations (OAuth,
  resume-parser stack, real LLM, Postgres, email, deployment) + troubleshooting.
- **`HANDS_ON.md`** — what was verified live, including the new async submit
  flow, contact-extraction routing, and per-user email sending.
- **`PROJECT_MODULES.md`** — module-by-module breakdown.
- **`PROJECT_NAVIGATION.md`** — this navigation guide.
- **`ARCHITECTURE.md`** — high-level component diagram and data flow.
- **`API.md`** — endpoint reference for the backend REST API.
- **`CONTRIBUTING.md`** — how to set up, lint, test, and open PRs.
- **`blueprint_v2.md`** — the original design blueprint (architecture, data
  flow, 25-section spec).

## Detailed Run Steps

### 1. Create a Python 3.11 venv
Run this from `backend/`:

```powershell
C:\Users\hoori\AppData\Local\Programs\Python\Python311\python.exe -m venv venv
venv\Scripts\activate
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

Check versions:

```powershell
python --version
python -m pip --version
```

Expected:
- Python 3.11.x
- pip pointing inside `backend\venv`

### 3. Create `.env`

Copy the example:

```powershell
copy .env.example .env
```

Important settings:
- `DATABASE_URL=sqlite:///./data/jobtailor.db`
- `LLM_PROVIDER=mock` for free local testing
- optional `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
- optional Gmail API `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REDIRECT_URI`
- optional SMTP settings
- optional `RESUME_PARSER_PROVIDER`, `RESUME_PARSER_BASE_URL`, `RESUME_PARSER_MODEL`

### 4. Build the DOCX template

```powershell
python -m app.rendering.build_template
```

This creates the resume template the renderer uses.

### 5. Start the API

```powershell
python -m uvicorn app.main:app --reload
```

### 6. Test the backend

Run:

```powershell
python smoke_test.py
```

What it proves:
- auth works
- resume upload works
- job post ingestion works
- job parser works
- tailoring works
- DOCX generation works
- outreach drafting works

### 7. Open the web app (Next.js)

From `frontend/`:

```powershell
cd frontend
npm install      # first time only
npm run dev      # -> http://localhost:3000
```

Use it to (all in the dashboard):
- register / log in
- update profile and connect Gmail (optional)
- upload resume
- submit a job post
- approve, download, and send the output

### 8. Load the Chrome extension

1. Open `chrome://extensions`
2. Enable Developer mode
3. Click Load unpacked
4. Select the `extension/` folder
5. Use the popup to log in
6. Open LinkedIn and click Tailor Resume

## OAuth Setup

If you want Google login:

1. In Google Cloud Console, create an OAuth client
2. Choose **Web application**
3. Add redirect URI:

```text
http://127.0.0.1:8000/auth/google/callback
```

4. Put the client ID and secret in `.env`

For the extension, the login flow uses the extension redirect URI automatically.

### Gmail API (password-free sending)

Sending by email uses the **Gmail API** via OAuth when configured — no App
Password needed:

1. Enable the **Gmail API** and add the `gmail.send` OAuth scope in Google
   Cloud Console.
2. Create a **Web application** OAuth client.
3. Add redirect URI:
   ```
   http://127.0.0.1:8000/auth/gmail/callback
   ```
4. Set in `.env`: `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REDIRECT_URI`.
5. In the app profile, click **Connect Gmail** and approve the consent screen
   once. The token is stored per-user; SMTP + App Password remains the fallback.

Resume parser stack example:

Start the parser service before resume upload.

```env
RESUME_PARSER_PROVIDER=openai_compatible
RESUME_PARSER_BASE_URL=http://localhost:8001/v1
RESUME_PARSER_MODEL=Qwen/Qwen3-8B-Instruct
```

Switch to NuExtract 3 by setting `RESUME_PARSER_MODEL=NuExtract-3`.

## What to Test in Order

### Backend only
1. `python -m uvicorn app.main:app --reload`
2. `python smoke_test.py`

### Frontend (Next.js)
1. `cd frontend && npm run dev` → http://localhost:3000
2. register/login
3. upload resume
4. submit a post

### Extension
1. load unpacked extension
2. open LinkedIn
3. log in in the popup
4. capture a post

## Common Problems

### `ModuleNotFoundError: docx`
You are not using Python 3.11 or the venv is not active.

### `pyo3` / `maturin` / `pydantic-core` build errors
You are probably on Python 3.14. Recreate the venv using Python 3.11.

### Smoke test download step fails on Windows
The test writes to `/tmp/...` which is a Linux path. The DOCX is still generated correctly.

## Working Mental Model

Think of the app as 4 layers:

1. **Inputs**
- resume upload
- job post input
- extension capture

2. **Agents**
- resume parser
- job parser
- tailoring
- outreach

3. **Resume Shape**
- preserved section headings
- structured sections with bullets

4. **Output**
- DOCX resume
- outreach draft
- approval state

5. **Delivery**
- download
- email send
- extension view

## Best Order to Learn the Code

1. `backend/app/models.py`
2. `backend/app/schemas.py`
3. `backend/app/routers/resumes.py`
4. `backend/app/routers/job_posts.py`
5. `backend/app/agents/orchestrator.py`
6. `backend/app/agents/tailoring_agent.py`
7. `backend/app/rendering/renderer.py`
8. `extension/background.js`
9. `frontend/lib/api.ts` + `frontend/app/dashboard/page.tsx`

## Status

The core pipeline is implemented and verified. This guide is the detailed map for understanding it and testing it locally.
