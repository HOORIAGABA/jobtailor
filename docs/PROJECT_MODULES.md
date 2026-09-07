# Agentic Resume Tailoring + Outreach System Module Guide

This document explains what each module does, how the pieces connect, and what the extension is for.

## Product Goal

The system helps a user:
- upload a resume
- paste or capture a job post
- parse the job requirements
- tailor the resume against the job
- generate a PDF resume (DOCX also produced for email/compat)
- draft a follow-up message
- approve before sending
- optionally send by email

## High-Level Flow

1. User registers or logs in.
2. User uploads a resume.
3. User submits a job post from the frontend or Chrome extension.
4. Backend parses the job and selects relevant resume content.
5. Backend generates a tailored resume and outreach draft.
6. User reviews the result, downloads the PDF, approves it, or sends it.

## Backend Modules

### `backend/app/main.py`
- Creates the FastAPI app.
- Enables CORS for the local frontend and Chrome extension.
- Mounts all routers.
- Creates the database tables on startup.
- Applies tiny SQLite migrations for the `oauth_tokens` / profile / SMTP columns.
- On startup, flags any orphaned `pending` / `processing` outputs as `failed`
  (in case the process restarted mid-pipeline), so the UI never hangs.

### `backend/app/config.py`
- Loads environment variables.
- Stores DB, JWT, LLM, SMTP, and Google OAuth settings.
- Keeps OAuth redirect URIs in one place.

### `backend/app/database.py`
- Builds the SQLAlchemy engine and session factory.
- Exposes the database dependency used by the routers.

### `backend/app/models.py`
- Defines the database schema.
- Stores users, resumes, job posts, tailored outputs, messages, and OAuth tokens.
- `User` also stores resume-header profile fields (`full_name`, `phone`,
  `location`, `linkedin`, `github`) and per-user SMTP credentials
  (`smtp_username`, `smtp_app_password` — write-only, never returned).

### `backend/app/schemas.py`
- Defines request and response shapes with Pydantic.
- `UserOut` exposes the profile fields + `smtp_configured` (never the password).
- `UserUpdate` allows editing profile + per-user SMTP in one request.

### `backend/app/agents/contact_extractor.py`
- Extracts contact details from raw resume text: `email`, `phone`, `location`,
  `linkedin`, `github`, `website`, and `full_name`.
- `location` uses a country-anchored `City, Country` regex that rejects any line
  containing URLs / `@` / `+` or longer than 100 chars (so the SKILLS line like
  `Python, PyTorch, ...` is never mistaken for a location).
- Normalizes LinkedIn / GitHub URLs to a canonical `https://` form.

### `backend/app/auth.py`
- Hashes passwords.
- Verifies passwords.
- Creates JWT access tokens.
- Resolves the current authenticated user.

### `backend/app/routers/auth.py`
- Handles register and login.
- Returns current user info via `GET /auth/me`.
- Updates profile / per-user SMTP via `PUT /auth/me`.
- Starts Google OAuth.
- Exchanges Google authorization codes for backend tokens.
- Stores Google access tokens for the user.
- Gmail API OAuth for password-free sending: `GET /auth/gmail/status` (is the
  API configured / is this user connected), `GET /auth/gmail/connect` (returns
  the consent URL), and `GET /auth/gmail/callback` (exchanges the code and
  stores the refresh token in `User.oauth_tokens["gmail"]`).

### `backend/app/routers/resumes.py`
- Accepts resume uploads.
- Parses PDF/DOCX resumes into structured data.
- Lets the user edit parsed resume data.
- Runs `contact_extractor` on the raw text and routes detected contact info
  (full name, phone, location, LinkedIn, GitHub) into the user profile as a
  **fallback only** — fields the user already filled in are never overwritten.
- Strips email/phone/LinkedIn/GitHub/website out of the parsed summary so the
  rendered header stays clean. Reloads the user in the request's own DB session
  before committing so contact fields persist reliably.

### `backend/app/pipeline_runner.py`
- Executes the LangGraph pipeline on a **background thread** with its own DB
  session, so `POST /job-posts` returns immediately with a `pending` output.
- Persists the tailored resume + draft message, renders DOCX & PDF, and flips
  the output to `ready` (or `failed` with an error message) off the request
  path.

### `backend/app/routers/job_posts.py`
- Accepts pasted text, a URL, or an extension capture.
- Fetches remote postings when needed.
- Persists a `pending` TailoredOutput and delegates to `pipeline_runner`,
  returning the output id right away (the client polls for `ready`).

### `backend/app/routers/tailored_outputs.py`
- Lists all tailored outputs for the current user.
- Returns tailored output status.
- Serves the generated DOCX/PDF.
- Supports approval (`ready → approved`; send is refused until approved).
- Triggers email sending — tries the Gmail API first (user's connected Gmail,
  no password) and falls back to SMTP + App Password, then `.env`.

### `backend/app/agents/resume_parser_agent.py`
- Converts raw resume text into structured resume fields.
- Preserves visible section headings like `WORK EXPERIENCE` and `PROJECTS` in the parsed JSON.
- Uses a dedicated extraction stack so the parser can be pinned to Qwen3 or NuExtract 3.

### `backend/app/agents/job_parser_agent.py`
- Extracts role requirements, skills, and signals from the job post.

### `backend/app/agents/tailoring_agent.py`
- Two-phase tailoring: ATS gap analysis (`ANALYSIS_PROMPT`) + LLM rewrite
  against the full resume JSON and job requirements.
- Applies **deterministic structural corrections** that never depend on the model:
  experience, projects, and skills are re-scored for keyword overlap with the job
  and reordered **most-relevant-first**.

### `backend/app/agents/outreach_agent.py`
- Drafts the follow-up or outreach message.
- Accepts reviewer feedback to revise on the next pass.

### `backend/app/agents/llm_client.py`
- Wraps the LLM provider (mock | groq | openai_compatible).
- Handles structured JSON outputs and a local heuristic resume-parser fallback.

### `backend/app/agents/orchestrator.py`
- LangGraph StateGraph multi-agent pipeline: parse -> tailor -> review -> outreach -> review.
- Reflection loop: reviewer nodes critique each stage output against the job
  and route back to the producer node with feedback (bounded retries:
  max 2 resume revisions, max 1 message revision) — plan/review/revise.

### `backend/app/agents/review_agent.py`
- Reflection / self-review agents for the tailored resume and the outreach draft.
- LLM critique plus deterministic guardrails (empty summary/skills, missing
  sections, duplicate entries, echoed summary, experience not reordered by
  relevance, JD-prose dumped into Skills, and projects merged into Experience).
- Runs a **head-to-head fit comparison**: deterministically scores the ORIGINAL
  vs the TAILORED resume for job fit (`original_fit_score`, `tailored_fit_score`,
  `winner`, `delta`), and forces a re-tailor (bounded) when fit doesn't improve.
- Emits score, issues, strengths, recommendations fed back into the producer.

### `backend/app/rendering/build_template.py`
- Builds the base DOCX resume template.
- Run this once before generating output documents.

### `backend/app/rendering/renderer.py`
- Fills the DOCX template with tailored resume content.
- Reuses preserved section headings when generating the resume DOCX.
- `contact_info_from_user(user, title)` builds the resume-header contact
  dict from the user's profile (full name, phone, location, LinkedIn, GitHub),
  falling back to the email local-part as a name.

### `backend/app/rendering/pdf_renderer.py`
- Builds an ATS-safe A4 PDF directly with `fpdf2` (no Word/LibreOffice needed).
- Mirrors the DOCX layout: centered header, uppercase section headings, and
  '-' bullet lines for summary, skills, experience, projects, certifications,
  education, leadership.
- The final downloadable file is the PDF:
  `GET /tailored-outputs/{id}/download` serves a generated `.pdf`
  (on-the-fly for outputs without one), and `send` attaches the PDF.

### `backend/app/fetchers/url_fetcher.py`
- Downloads and extracts text from job post URLs.

### `backend/app/email_utils/gmail_api.py`
- Sends email with the **Gmail API** via OAuth (`gmail.send` scope) from the
  logged-in user's own account — the preferred, password-free path.
- Builds the consent URL, exchanges the callback code, and stores a refresh
  token per-user; refreshes tokens transparently and raises a clear
  "Reconnect Gmail" error on `invalid_grant`.

### `backend/app/email_utils/sender.py`
- Sends the approved message and attachments via SMTP (fallback path).
- The tailored-outputs `send` passes the user's OWN Gmail + App Password so
  nothing is hardcoded — each user sends from their own account (`.env` is
  just a final fallback).
- Wraps SMTP authentication/transport failures with clear messages.

### `backend/smoke_test.py`
- Exercises the main backend flow end to end.
- Verifies auth, upload, tailoring, rendering, and draft generation.

## Web app (Next.js / React) — `frontend/`

### `frontend/lib/api.ts`
- Typed client for every backend endpoint (register/login, profile, Connect
  Gmail, resume upload, job submit, polling, approve/send/download).
- Stores the JWT in `localStorage` and attaches it as a `Bearer` header.
- Reads the backend URL from `NEXT_PUBLIC_API_BASE_URL` (default
  `http://127.0.0.1:8000`).

### `frontend/lib/auth-context.tsx`
- React context exposing `user`, `login`, `register`, `logout`, `refresh`.
- On mount, validates the stored token via `GET /auth/me`.

### `frontend/app/login/page.tsx`
- Login / register form and Google OAuth (popup + `postMessage` callback).

### `frontend/app/dashboard/page.tsx`
- Auth-gated dashboard composing the five panels below.

### `frontend/components/dashboard/*`
- `ProfilePanel` — edits resume-header (incl. GitHub) + SMTP fields, and the
  **Connect Gmail** button (OAuth popup) with a live
  `GET /auth/gmail/status` indicator.
- `ResumePanel` — uploads/parses a resume and lists resumes for selection.
- `JobPanel` — submits a job post (paste text or URL).
- `ResultPanel` — polls `GET /tailored-outputs/{id}` until `ready`/`failed`,
  renders the tailored resume + outreach message, and drives Download /
  Approve / Send.
- `HistoryPanel` — lists past outputs with status badges; select one to view.

### `legacy/index.html` (legacy)
- Outdated single-file test harness, kept only for quick reference.

## Chrome Extension

### Purpose
- Capture a LinkedIn post directly from the page.
- Send the captured text to the backend.
- Let the user tailor a resume without copy-pasting manually.
- Show full results inline with download, approve, and send actions.
- Maintain a history of all past tailored outputs.

### `extension/manifest.json`
- Declares permissions, background worker, popup, and content script.

### `extension/content_script.js`
- Reads the visible LinkedIn post text from the page.
- Does not call the backend directly.

### `extension/background.js`
- Stores the auth token.
- Handles login (email/password and Google OAuth via `chrome.identity`).
- Verifies auth via `GET /auth/me` and shows user email.
- Pre-flight resume check via `GET /resumes`.
- Sends captured job text to the backend.
- Lists past tailored outputs via `GET /tailored-outputs`.
- Downloads the PDF via `GET /tailored-outputs/{id}/download`.
- Approves outputs via `POST /tailored-outputs/{id}/approve`.
- Sends outputs via `POST /tailored-outputs/{id}/send`.
- Clears auth token on logout.

### `extension/popup.html`
- Three views: tailor (main), result (after tailoring), history (past outputs).
- Login view with email/password and Google OAuth buttons.
- User email display after login.
- Resume warning banner if no resume uploaded.
- Profile form with full name, phone, location, LinkedIn, GitHub, and SMTP
  (Gmail + App Password), plus the **Connect Gmail** button.
- Result view with status badge, draft message, and action buttons (Download, Approve, Send).
- History view listing all past outputs with status badges.

### `extension/popup.js`
- View switching between tailor, result, and history views.
- Pre-flight resume check before tailoring.
- Displays tailored result with status badge and action buttons.
- Handles download, approve, send, and history actions.
- Logout handler.

## Data Flow

### Resume Path
1. Upload resume.
2. Parse document into structured fields.
3. Store the parsed resume.
4. Use that data during tailoring.

### Job Path
1. Paste a job post or capture it from LinkedIn.
2. Parse the posting.
3. Match the posting against resume evidence.
4. Render a tailored DOCX.
5. Draft outreach text.

### Approval Path
1. User reviews the tailored output (from frontend or extension).
2. User approves or downloads it.
3. User can send the draft email via Gmail API (if connected) or SMTP.
4. All actions available from both the web frontend and Chrome extension.

## Current Notes

- Python 3.11 is the supported runtime.
- `LLM_PROVIDER=mock` is the easiest local test mode.
- Google OAuth requires the exact redirect URI to be registered in Google Cloud Console.
- Gmail API sending requires `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` in
  `.env` and a one-time "Connect Gmail" approval; otherwise SMTP fallback applies.
- The resume parser is intended to run against an OpenAI-compatible Qwen3 or NuExtract 3 endpoint.
- The extension is for LinkedIn capture and browser-side convenience, not as the core backend.
- Job submission is **async**: `POST /job-posts` returns immediately; clients
  poll `GET /tailored-outputs/{id}` until status is `ready` or `failed`.
