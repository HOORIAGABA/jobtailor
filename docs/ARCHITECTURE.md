# Architecture

This document gives a high-level view of how the components fit together,
and how data flows through the system.

## Component diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                            CLIENTS                                      │
│                                                                         │
│   frontend/ (Next.js)          extension/ (Chrome)     legacy/ (ref)    │
│   React + TypeScript            Manifest V3            index.html       │
│   Tailwind CSS                  popup + content script                   │
└────────────┬──────────────────────────┬─────────────────────┬───────────┘
             │  JWT Bearer header       │  chrome.runtime     │ (reference)
             ▼                          ▼                     │
┌─────────────────────────────────────────────────────────────────────────┐
│                          FastAPI backend                                 │
│                          app/main.py                                     │
│                                                                         │
│  ┌───────────────┐  ┌──────────────┐  ┌──────────────────────────────┐  │
│  │  /auth         │  │  /resumes     │  │  /job-posts                   │  │
│  │  register      │  │  upload       │  │  paste / URL / extension      │  │
│  │  login         │  │  parse PDF/   │  │  → persist pending output     │  │
│  │  profile CRUD  │  │  DOCX into    │  │  → return id immediately      │  │
│  │  Gmail OAuth   │  │  structured   │  │                               │  │
│  └───────────────┘  │  JSON +       │  │  ┌─────────────────────────┐  │  │
│                      │  auto-fill    │  │  │  background thread       │  │  │
│  ┌───────────────┐  │  contact info  │  │  │  pipeline_runner.py      │  │  │
│  │  /tailored-    │  │  into profile │  │  │  run_pipeline()          │  │  │
│  │  outputs       │  └──────────────┘  │  │  parse → tailor → review  │  │  │
│  │  poll / status │                    │  │  → outreach → review      │  │  │
│  │  download PDF  │  ┌──────────────┐  │  │  → render DOCX + PDF      │  │  │
│  │  approve       │  │  /resumes     │  │  │  → status = ready         │  │  │
│  │  send (email)  │  │  list         │  │  └─────────────────────────┘  │  │
│  └───────────────┘  └──────────────┘  └──────────────────────────────┘  │
│                                                                         │
│  ┌────────────────────────────────────────────────────────────────────┐  │
│  │  agents/                                                            │  │
│  │  ┌────────────────┐  ┌────────────────┐  ┌──────────────────────┐  │  │
│  │  │ resume_parser   │  │ job_parser      │  │ tailoring_agent       │  │  │
│  │  │ _agent.py       │  │ _agent.py       │  │ (2-phase: analyze     │  │  │
│  │  │                 │  │                 │  │  + rewrite, with      │  │  │
│  │  │ Heuristic       │  │ LLM extraction  │  │  relevance reorder)   │  │  │
│  │  │ fallback +      │  │ of requirements │  │                       │  │  │
│  │  │ LLM (if avail.) │  │ and signals     │  └──────────────────────┘  │  │
│  │  └────────────────┘  └────────────────┘                              │  │
│  │  ┌────────────────┐  ┌────────────────┐  ┌──────────────────────┐  │  │
│  │  │ contact_        │  │ outreach_agent  │  │ review_agent          │  │  │
│  │  │ extractor.py    │  │ _agent.py       │  │ _agent.py             │  │  │
│  │  │                 │  │                 │  │                       │  │  │
│  │  │ Regex-based     │  │ Drafts follow-  │  │ Critique + score vs   │  │  │
│  │  │ extraction of   │  │ up/outreach     │  │ original resume;      │  │  │
│  │  │ email/phone/    │  │ email from the  │  │ bounded re-tailor     │  │  │
│  │  │ location/       │  │ job post +      │  │ when fit doesn't      │  │  │
│  │  │ linkedin/       │  │ tailored resume │  │ improve               │  │  │
│  │  │ github          │  └────────────────┘  └──────────────────────┘  │  │
│  │  └────────────────┘                                                 │  │
│  │  ┌────────────────┐  ┌──────────────────────────────────────────┐  │  │
│  │  │ llm_client.py   │  │ orchestrator.py (LangGraph)              │  │  │
│  │  │                 │  │                                         │  │  │
│  │  │ Mock / Groq /   │  │ StateGraph: parse_job → tailor →       │  │  │
│  │  │ openai_compat   │  │ review_resume → outreach →             │  │  │
│  │  │ providers +     │  │ review_message                         │  │  │
│  │  │ heuristic       │  │ (bounded retries on reviewer flags)    │  │  │
│  │  │ fallback        │  └──────────────────────────────────────────┘  │  │
│  │  └────────────────┘                                                 │  │
│  └────────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌───────────────────────┐  ┌──────────────────────┐                    │
│  │ rendering/             │  │ email_utils/          │                    │
│  │ build_template.py      │  │ gmail_api.py          │ sender.py         │
│  │ renderer.py (docxtpl)  │  │ (Gmail API OAuth,     │ (SMTP +           │
│  │ pdf_renderer.py        │  │  no password needed)  │  App Password     │
│  │ (fpdf2, A4 ATS-safe)  │  └──────────────────────┘  fallback)         │
│  └───────────────────────┘                                               │
│                                                                         │
│  ┌───────────────────────┐  ┌──────────────────────┐                    │
│  │ config.py              │  │ database.py            │                    │
│  │ .env loader            │  │ SQLAlchemy engine +    │                    │
│  │ (DB, JWT, LLM, SMTP,  │  │ per-request session    │                    │
│  │  Google, Gmail)        │  │ factory                │                    │
│  └───────────────────────┘  └──────────────────────┘                    │
│                                                                         │
│  ┌───────────────────────┐                                               │
│  │ models.py              │                                               │
│  │ User, Resume, JobPost, │                                               │
│  │ TailoredOutput, Message│                                               │
│  └───────────────────────┘                                               │
└─────────────────────────────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Storage                                                                 │
│                                                                         │
│  SQLite (default)          PostgreSQL (swap via DATABASE_URL)           │
│  backend/data/jobtailor.db  Supabase / Neon free tier                   │
│                                                                         │
│  Tables: users, resumes, job_posts, tailored_outputs, messages,         │
│          oauth_tokens                                                   │
└─────────────────────────────────────────────────────────────────────────┘
```

## Data flow

### 1. Resume path

```
PDF / DOCX uploaded
      │
      ▼
  Extract raw text (pdfplumber / python-docx)
      │
      ├──► Resume parser LLM  →  structured JSON
      │       (summary, skills, experience, projects)
      │
      ├──► Contact extractor  →  email/phone/location/LinkedIn/GitHub
      │       └─► auto-fill into user profile (fallback only)
      │
      └──► Strip contact details from summary
           (so rendered header stays clean)
```

### 2. Job submission path

```
Job post text (paste / URL / extension capture)
      │
      ▼
  POST /job-posts
      │
      ├─ persist TailoredOutput(status=pending)  →  return id (fast)
      │
      └─ background thread (pipeline_runner.py)
              │
              ▼
         LangGraph orchestrator
              │
              ├─ parse_job       → job_requirements (skills, signals, tone)
              ├─ tailor          → tailored resume JSON (most-relevant-first reorder)
              ├─ review_resume   → critique + fit comparison (original vs tailored)
              │   └─ re-tailor if fit didn't improve (bounded)
              ├─ outreach        → follow-up email draft
              └─ review_message  → critique + revise (bounded)
              │
              ▼
         Render DOCX + PDF
         Flip status → ready (or failed)
```

### 3. Approval & delivery path

```
GET /tailored-outputs/{id}
      │
      ├─ Download PDF  →  GET /tailored-outputs/{id}/download
      │
      ├─ Approve       →  POST /tailored-outputs/{id}/approve
      │                   (ready → approved)
      │
      └─ Send          →  POST /tailored-outputs/{id}/send
                          │
                          ├─ Gmail API (OAuth) if connected  ← preferred
                          ├─ Per-user SMTP + App Password
                          └─ .env SMTP fallback
```

## LangGraph orchestrator

The pipeline is a `StateGraph` in `backend/app/agents/orchestrator.py`:

```
parse_job ──► tailor ──► review_resume ──► outreach ──► review_message
                ▲                                │
                └──── re-tailor (bounded) ◄──────┘
                           ▲
                  re-draft message (bounded)
```

Each producer node feeds its output to a reviewer node. The reviewer
emits `{ pass: bool, issues: [], feedback: "..." }`. When `pass` is false,
the feedback is routed back to the producer node with a retry count. Retries
are bounded by `MAX_RESUME_REVISIONS` and `MAX_MESSAGE_REVISIONS` to prevent
infinite loops.

### Deterministic guardrails (no LLM)

Even without a real LLM, the review agent applies these structural checks:

- Empty summary or skills
- Missing / duplicate experience or project entries
- JD prose leaked into the Skills section
- Projects merged into Experience
- Experience not reordered by relevance

### Fit comparison

A deterministic scorer computes keyword-overlap `fit_score` for both the
**original** and **tailored** resume against the job requirements. The
`winner`, `delta`, `original_fit_score`, and `tailored_fit_score` are
persisted on the `TailoredOutput` record. When the delta is negative or
zero (tailoring made it worse), the pipeline forces a bounded re-tailor.

## Session management

Every incoming request gets its own `SessionLocal` instance (via the
`get_db` dependency). Route-level DB operations use this per-request
session. The background pipeline thread creates its own independent
session to avoid cross-request contamination.

A critical detail: `get_current_user` resolves the user using one session
instance, but a route that needs to `commit()` changes must re-query the
user within the *route's own session* — otherwise the commit may silently
drop changes. This applies to the resume-upload contact-routing path
(`resumes.py:58`).

## Rendering

Two parallel renderers produce the tailored resume:

- **DOCX** (`renderer.py`) — fills a `docxtpl` template with the tailored
  JSON and preserves the original section headings.
- **PDF** (`pdf_renderer.py`) — builds an ATS-safe A4 PDF directly with
  `fpdf2` (no Word/LibreOffice dependency). This is the default download
  and the file attached to emails.

Both receive the same `contact_info_from_user(user, title)` dict, which
maps the user's profile fields (name, phone, location, LinkedIn, GitHub,
email) into the header.
