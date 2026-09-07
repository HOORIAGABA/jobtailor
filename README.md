# JobTailor — Agentic Resume Tailoring + Outreach System

A multi-agent system built with **LangGraph** that ingests a job posting
(paste, URL, or the LinkedIn page via a Chrome extension), tailors your
resume against it, renders an ATS-safe **PDF/DOCX**, and
drafts an outreach message — with a **human-approval gate** before
anything is ever emailed.

> Hands-on end-to-end execution is documented in [`docs/HANDS_ON.md`](docs/HANDS_ON.md).

## Architecture

```
Job post (paste / URL / LinkedIn)          Baseline resume (PDF / DOCX)
            |                                        |
            v                                        v
        ┌──────────────────  FastAPI backend  ──────────────────┐
        │                                                        │
        │  LangGraph orchestrator (agentic loop w/ review)       │
        │   parse_job → tailor → review_resume → outreach →      │
        │   review_message  (bounded retries on reviewer flags)  │
        │                                                        │
        │  Deterministic relevance reordering (most-fit-first) + review loop
        │  Renderers: docxtpl (DOCX) / fpdf2 (PDF)               │
        │  Sender: Gmail API OAuth (no password) → SMTP fallback │
        └────────────────────────────────────────────────────────┘
                       │              │
                       v              v
              Human approval    Download / Email
```

## Feature highlights

- **Agentic pipeline, not a one-pass chain** — every tailoring/outreach stage
  is followed by a reviewer node that feeds feedback back to the producer,
  bounded by `MAX_RESUME_REVISIONS` / `MAX_MESSAGE_REVISIONS`
  (`app/agents/orchestrator.py`, `review_agent.py`).
- **Async job submission** — `POST /job-posts` persists a `pending` output
  and runs the pipeline on a background thread, so a hot-reload or disconnect
  can't orphan your request. The UI polls `GET /tailored-outputs/{id}` until
  it is `ready` (`app/pipeline_runner.py`).
- **Profile + per-user sending** — emails go out from each user's *own* Gmail.
  Preferred path is the **Gmail API via OAuth** ("Connect Gmail", no password
  needed); SMTP + App Password is the automatic fallback when OAuth isn't
  connected (`app/email_utils/gmail_api.py`, `sender.py`).
- **ATS-safe, heading-aware rendering** to both `.docx` and `.pdf`.
- **Human-in-the-loop** — `approve` before `send`; nothing is mailed by default.

## Quick start (local, Python 3.11)

```powershell
cd backend
C:\path\to\Python311\python.exe -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
Copy-Item .env.example .env        # defaults to a zero-cost mock LLM
python -m app.rendering.build_template   # generate the DOCX template once
python -m uvicorn app.main:app --reload
```

Then run the **web app** (separate terminal) and open it:

```powershell
cd frontend
npm install          # first time only
npm run dev          # -> http://localhost:3000
```

Log in (register) in the browser and use the dashboard. Full setup (Python 3.11 reason,
Google OAuth, Gmail API, resume-parser stack, PostgreSQL swap, deployment) is in
[`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md).

> Tip: point the web app at a non-local backend with
> `NEXT_PUBLIC_API_BASE_URL` in `frontend/.env.local` (defaults to
> `http://127.0.0.1:8000`). See `frontend/.env.local.example`.

Run the end-to-end smoke test (isolated to a temp dir — it never touches
your real data):

```powershell
python smoke_test.py
```

### Using a real LLM instead of the mock

Set in `backend/.env` and restart:

```env
# Groq (free, OpenAI-compatible)
LLM_PROVIDER=groq
LLM_API_KEY=your_key_here
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=llama-3.1-70b-versatile

# …or any local OpenAI-compatible endpoint (e.g. Ollama)
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=llama3.1:8b-instruct-q4_K_M
```

### Resume parser stack

The parser uses its own OpenAI-compatible service (keep it running before
uploading a resume). See [`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md#resume-parser-stack).

## Project layout

```
jobtailor/
├── backend/
│   ├── app/
│   │   ├── main.py                 FastAPI app + startup migrations
│   │   ├── config.py               env-based settings (anchored to backend/)
│   │   ├── models.py / schemas.py  SQLAlchemy models / Pydantic schemas
│   │   ├── auth.py                 JWT + password hashing
│   │   ├── pipeline_runner.py      background-thread pipeline executor  ← async submit
│   │   ├── routers/                auth, resumes, job_posts, tailored_outputs
│   │   ├── agents/                 llm_client, resume/job parser, tailoring,
│   │   │                           outreach, review_agent, orchestrator, resume_parser_agent
│   │   ├── rendering/              build_template, renderer (docxtpl), pdf_renderer
│   │   ├── fetchers/url_fetcher.py trafilatura URL extraction
│   │   └── email_utils/           gmail_api.py (Gmail OAuth), sender.py (SMTP)
│   ├── smoke_test.py               isolated end-to-end test
│   ├── requirements.txt  Dockerfile  .env.example
├── frontend/                      Next.js 14 (React + TypeScript + Tailwind)
│   ├── app/                       app router pages (home, login, dashboard)
│   ├── components/                dashboard panels (profile, resume, job, result, history)
│   └── lib/                       API client + auth context
├── extension/                      Chrome Manifest V3 extension
├── legacy/index.html             legacy single-file test harness (optional)
├── docs/                           architecture + setup + run + contribution guides
├── docker-compose.yml
├── LICENSE
└── README.md
```

See [`docs/PROJECT_NAVIGATION.md`](docs/PROJECT_NAVIGATION.md) for a
module-by-module map. Frontend UI lives in **`frontend/`** (Next.js/React); the
older `legacy/index.html` is a minimal throwaway test page kept for
reference.

## Chrome extension

1. `chrome://extensions` → enable Developer mode → **Load unpacked** →
   select `extension/`.
2. Log in (password or Google) in the popup, open a LinkedIn post, click
   **Tailor resume for this post**.
3. The popup polls until the pipeline is `ready`, then lets you
   download / approve / send.

`API_BASE_URL` in `extension/background.js` defaults to
`http://127.0.0.1:8000` (update it for a deployed backend).

## Sending the tailored resume by email

Sending requires approving the output first (human-in-the-loop). There are
two ways to enable delivery:

1. **Gmail API (recommended, no password)** — the profile has a
   **Connect Gmail** button. Once you've added `GMAIL_CLIENT_ID` /
   `GMAIL_CLIENT_SECRET` / `GMAIL_REDIRECT_URI` to `backend/.env`, clicking it
   opens a Google consent screen; after you approve once, the app sends from
   your own Gmail without ever seeing a password. See
   [`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md#gmail-api-recommended-password-free-sending).
2. **SMTP + App Password (fallback)** — enter your Gmail + an App Password in
   the profile. `.env` `SMTP_*` is only a shared last resort.

## Deployment ($0 hosting)

- **Backend:** Render or Fly.io (Dockerfile included).
- **Database:** swap `DATABASE_URL` to Supabase/Neon Postgres — no code change.
- **Frontend:** Vercel/Netlify.
- **LLM:** Groq free tier.

Details in [`docs/SETUP_GUIDE.md`](docs/SETUP_GUIDE.md).

## Known limitations / honest gaps

- Tailoring quality depends on the LLM; the pipeline applies deterministic
  corrections (relevance reordering, guardrails) that work regardless of model,
  but a weak local model can still produce weaker prose than a stronger one.
- The LinkedIn DOM selectors in `content_script.js` are best-effort and need
  periodic maintenance as LinkedIn changes its frontend.
- Gmail API sending needs OAuth credentials in `.env` (`GMAIL_CLIENT_ID` /
  `GMAIL_CLIENT_SECRET`) to send without an App Password; until then the SMTP
  fallback applies.
- Test coverage is a single end-to-end `smoke_test.py` (working, but not
  exhaustive unit coverage).
