# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versioning follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `github` profile field end-to-end: SQLAlchemy column, Pydantic
  `UserUpdate`/`UserOut`, sqlite startup migration, profile-edit loop,
  rendered contact line, and `GitHub` input on the web + extension UI.
- `backend/app/agents/contact_extractor.py`: extracts email, phone, location,
  LinkedIn, GitHub, website, and full name from raw resume text (country-
  anchored location regex, URL normalization).
- Contact auto-fill on resume upload: detected contact details are routed into
  the profile **as a fallback only** — user-set fields are never overwritten.
- Contact details are stripped from the parsed summary so the rendered header
  stays clean.

### Fixed
- `GET /auth/me` returned `github: null` even after it was persisted — the
  `_user_out()` serializer was missing the `github` field.
- `main.py` startup `create_all` only created the `tailored_outputs` table
  because only `TailoredOutput` was imported; now all models are imported so
  `users`/`resumes`/`job_posts`/`messages` are created on fresh databases.
- Contact extraction location regex matched the SKILLS line / long header;
  tightened to a country-anchored pattern rejecting URLs / `@` / `+` / >100 chars.
- Contact fields on upload were not persisting because `get_current_user` and
  the route used different DB sessions; the user is now reloaded in the
  request's own session before `commit()`.
- Frontend inputs rendered near-invisible grey text in dark mode; pinned
  input/textarea/select colors in `frontend/app/globals.css`.

### Changed
- Production web app directory renamed `web/` → `frontend/`; legacy single-file
  harness moved from `frontend/index.html` → `legacy/index.html`. Docs, configs,
  and package name (`jobtailor-frontend`) updated accordingly.
- Docs updated to cover the `github` field and contact-extraction routing.

## [0.1.0] - 2026-09-01

Initial working implementation, verified end-to-end:

- FastAPI backend with LangGraph agentic pipeline
  (`parse_job → tailor → review_resume → outreach → review_message`).
- Async job submission + polling (`pipeline_runner` background thread).
- Deterministic relevance reordering and review guardrails.
- DOCX + ATS-safe PDF rendering.
- Per-user email sending (Gmail API OAuth preferred, SMTP fallback).
- Next.js web app, Chrome extension (LinkedIn capture), and smoke test.
