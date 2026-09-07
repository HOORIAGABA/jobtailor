# Hands-On Verification

This is a **working implementation**, verified end-to-end on this machine,
not just a design document. Below is exactly what ran and what passed.

## Verified flow

1. **Health check** — `GET /health` → `200 {"status": "ok"}`
2. **Register / login** — JWT issued for email/password and Google OAuth.
3. **Resume ingestion** — uploaded PDF/DOCX parsed into structured JSON
   (`summary`, `skills`, `experience`, `projects`, …) via the resume-parser
   LLM. The raw text is also run through `contact_extractor`, which detected
   and routed `phone`, `location`, `linkedin`, and `github` into the profile
   (verified: `github` persists and shows up on `GET /auth/me` and in the
   rendered header).
4. **Job post submission** — `POST /job-posts` accepts paste text, a generic
   URL (auto-fetched), or a LinkedIn post via the Chrome extension (URL
   fetched; matching-authored posts confirmed).
5. **Agentic pipeline** — LangGraph `parse_job → tailor → review_resume →
   outreach → review_message`, with reviewer feedback looped back into the
   producers. With the local `llama3.1:8b-instruct` model, a real run was
   observed: resume revised 3× (duplicates removed, summary retargeted to
   the role), outreach message revised 2×.
6. **Async submit + polling** — `POST /job-posts` returns a `pending` output
   immediately; the UI/extension polls `GET /tailored-outputs/{id}` until the
   background thread flips it to `ready`.
7. **Render + download** — DOCX and PDF generated from the tailored JSON with
   the user's profile header (name, phone, location, LinkedIn, GitHub).
   Verified as a valid single-page `%PDF-` file via
   `GET /tailored-outputs/{id}/download`; the full pipeline
   (submit → poll → `ready` → download → approve) was exercised end-to-end
   against a fresh DB with the mock provider.
8. **Human approval** — `POST /tailored-outputs/{id}/approve` flips
   `ready → approved`; the Send endpoint refuses until approval.
9. **Email sending (Gmail API first, SMTP fallback)** — the `send` endpoint
   tries `send_via_gmail_api()` (the logged-in user's connected Gmail, no
   password) and falls back to per-user SMTP + App Password, with `.env`
   `SMTP_*` as the final fallback. The fallback wiring was verified: without
   any credentials the endpoint returns a clear `400` explaining both options
   (connect Gmail with OAuth, or set an App Password), rather than a generic
   failure.

## How sending works now

The sender is intentionally layered so a fresh install "just works" as far as
credentials allow:

```
send_output
  └─ Gmail API (OAuth) if the user connected Gmail   ← preferred, no password
       else Sender SMTP (per-user App Password)
            else .env SMTP_*                          ← shared fallback
```

## How the pipeline runs now (async)

Previously `POST /job-posts` blocked for the whole multi-minute agentic run,
so a hot-reload or dropped connection killed it before an output id was ever
returned (observed: long run in the log, but no download GET ever fired).

Now:

```
POST /job-posts  →  persist TailoredOutput(status=pending)  →  return id (fast)
                             │
                  background thread (app/pipeline_runner.py)
                             │
        run_pipeline() → render DOCX+PDF → status=ready (or failed)
                             │
        GET /tailored-outputs/{id}  ◄── UI polls every ~8s
```

If the server restarts mid-pipeline, startup flags any stuck
`pending/processing` output as `failed` with a clear reason, so the UI never
hangs.

## What isn't testable without real credentials

- A real successful email **send** needs either (a) Gmail API OAuth
  credentials (`GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` in `.env`) + a one-time
  "Connect Gmail" approval, or (b) a genuine Gmail App Password (two-step
  verification enabled). The credential-gating and error paths were verified;
  actual delivery needs one of these.
- Google OAuth sign-in requires `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
  in `backend/.env` (already used live on this machine).

## Smoke test

`backend/smoke_test.py` runs the whole loop headlessly. It is **isolated** to
a throwaway temp directory (`DATABASE_URL` / `CHROMA_PERSIST_DIR` pointed at
temp), so running it never touches real user data:

```powershell
cd backend
venv\Scripts\activate
python smoke_test.py
```

Expected: steps 1–8 pass and print `ALL SMOKE TESTS PASSED`.
