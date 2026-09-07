# Contributing to JobTailor

Thanks for your interest in contributing! This guide covers how to set up the
repo for development, run the checks, and submit a change.

## Table of contents

- [Development setup](#development-setup)
- [Project layout](#project-layout)
- [Checks you must run](#checks-you-must-run)
- [Code style](#code-style)
- [How to submit a change](#how-to-submit-a-change)
- [Testing notes](#testing-notes)

## Development setup

### Prerequisites

- **Python 3.11** (required — some native wheels are cp311-only).
- **Node.js 18+** (for the `frontend/` Next.js app).
- **Chrome** (only needed for the `extension/`).

### Backend

```powershell
cd backend
C:\path\to\Python311\python.exe -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
Copy-Item .env.example .env      # defaults to the zero-cost mock LLM
python -m app.rendering.build_template
python -m uvicorn app.main:app --reload
```

### Frontend

```powershell
cd frontend
npm install
npm run dev                      # -> http://localhost:3000
```

### Extension

Load `extension/` unpacked from `chrome://extensions` (Developer mode → Load
unpacked).

## Project layout

```
jobtailor/
├── backend/       FastAPI + LangGraph app, routers, agents, renderers, email
├── frontend/      Next.js 14 (React + TypeScript + Tailwind) web app
├── extension/     Chrome Manifest V3 extension (LinkedIn capture)
├── legacy/        Legacy single-file test harness (index.html, reference only)
└── docs/          Guides (setup, architecture, API, modules, navigation, hands-on)
```

## Checks you must run

Run these before opening a PR so CI-style checks pass:

### Backend

```powershell
cd backend
venv\Scripts\activate
python -m compileall app          # syntax check across all modules
python smoke_test.py              # isolated end-to-end test (never touches real data)
```

### Frontend

```powershell
cd frontend
npx tsc --noEmit                  # TypeScript type-check
npm run lint                      # ESLint  (no warnings/errors)
npm run build                     # production build (exit 0)
```

### Extension

There is no automated test suite for the extension yet; verify manually:

1. Load unpacked.
2. Log in (password or Google).
3. Open a LinkedIn post and click **Tailor resume for this post**.
4. Confirm the popup polls to `ready`, then download / approve / send.

## Code style

- **Python:** PEP 8, ~4-space indent, type hints on function signatures, no
  stray debug `print()` calls.
- **TypeScript/React:** match the existing file conventions; keep components
  colocated under `frontend/components/`. Run `npm run lint` before committing.
- **No comments unless they explain *why*** (not *what*).
- Keep secrets out of code — everything configurable goes in `.env`
  (see `backend/.env.example`) with a matching `.env.example` entry.
- Add/update docs in `docs/` when a module or endpoint changes.

## How to submit a change

1. Fork the repo and create a feature branch.
2. Make your change, following the conventions above.
3. Run all the checks in [Checks you must run](#checks-you-must-run).
4. Commit with a concise, conventional message (e.g. `feat: auto-fill profile
   contact fields from uploaded resume`).
5. Open a pull request and describe what changed and how it was verified.

> When adding a backend environment variable, update `backend/.env.example` in
> the same PR so the setup guide stays accurate.

## Testing notes

- `LLM_PROVIDER=mock` runs the whole pipeline with no API key — use it for
  wiring and integration verification.
- `smoke_test.py` is isolated to a temp directory; it never touches your real
  SQLite DB or Chroma data.
- Real email delivery (Gmail API or SMTP) requires credentials and is gated
  behind them; the credential-gating/error paths are what `smoke_test.py` covers.
