# Backend API Reference

Base URL: `http://127.0.0.1:8000` (local dev) — configurable via `NEXT_PUBLIC_API_BASE_URL` in the frontend.

All endpoints return JSON. Auth endpoints (`/auth/me`) require a `Bearer` token in the `Authorization` header. Token-based endpoints return `401` when the token is missing or expired.

## Auth

### `POST /auth/register`

Create a new account and return a JWT.

**Body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `email` | string | yes | Login email |
| `password` | string | yes | Min 8 chars |
| `full_name` | string | no | Display name for the resume header |

**Response:** `TokenResponse`

```json
{ "access_token": "<jwt>", "token_type": "bearer" }
```

---

### `POST /auth/login`

Return a JWT for an existing account.

**Body:** `{ "email": "...", "password": "..." }`

**Response:** `TokenResponse`

---

### `POST /auth/logout`

Revoke the current session (extension sign-out). Returns `{ "ok": true }`.

---

### `GET /auth/me`

Return the full profile for the authenticated user.

**Response:** `UserOut`

```json
{
  "id": "...",
  "email": "...",
  "full_name": "...",
  "phone": "...",
  "location": "...",
  "linkedin": "...",
  "github": "...",
  "smtp_username": null,
  "smtp_configured": false
}
```

`smtp_app_password` is never returned (write-only). `github` is `null` until the user edits it or uploads a resume whose `contact_extractor` detects one.

---

### `PUT /auth/me`

Update the profile and optional SMTP credentials.

**Body:** `UserUpdate` (all fields optional — only present fields are overwritten)

```json
{
  "full_name": "Hooria Attas",
  "phone": "+923036800002",
  "location": "Islamabad, Pakistan",
  "linkedin": "https://linkedin.com/in/...",
  "github": "https://github.com/..."
}
```

**Response:** `UserOut` (same shape as `GET /auth/me`).

---

### `GET /auth/google/login`

Redirects to Google's consent screen. Requires `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` in `backend/.env`.

---

### `POST /auth/google/token`

Exchange a Google authorization code for a backend JWT.

**Body:** `{ "code": "...", "redirect_uri": "..." }`

**Response:** `TokenResponse`

---

### `GET /auth/gmail/status`

Check whether the Gmail API is configured and whether the current user has connected.

**Response:**

```json
{ "configured": true, "connected": true, "email": "hooria@..." }
```

`configured` is `false` when `GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` are missing from `.env`. `connected` is `false` until the user clicks **Connect Gmail** and approves the consent screen.

---

### `GET /auth/gmail/connect`

Returns a Google consent URL; open it in a new tab to approve once.

**Response:** `{ "auth_url": "https://accounts.google.com/o/oauth2/..." }`

---

### `GET /auth/gmail/callback`

Exchanges the Gmail OAuth callback code and stores the refresh token per-user.

---

## Resumes

All endpoints under `/resumes` require authentication.

### `POST /resumes/upload`

Upload a PDF or DOCX resume.

**Request:** `multipart/form-data` with field `file`.

On success, the backend:
1. Extracts raw text from the file.
2. Calls the resume-parser LLM to produce structured JSON (`summary`, `skills`, `experience`, `projects`, …).
3. Runs `contact_extractor` on the raw text and routes detected contact fields (email, phone, location, LinkedIn, GitHub) into the user profile **only if they're currently empty** — user-set values are never overwritten.
4. Strips email / phone / LinkedIn / GitHub / website out of the parsed summary so the rendered header stays clean.
5. Stores the resume as a `Resume` record.

**Response:** `ResumeOut`

---

### `PUT /resumes/{resume_id}`

Update the structured parsed resume data (body matches the resume schema).

---

### `GET /resumes`

List all resumes for the current user.

**Response:** `ResumeOut[]`

---

## Job Posts

### `POST /job-posts`

Submit a job post. Returns immediately with a `pending` TailoredOutput id — the pipeline runs on a background thread.

**Body:**

```json
{
  "raw_text": "We are hiring an Applied AI Product Engineer...",
  "source_type": "paste|url|extension",
  "url": "https://linkedin.com/posts/... (optional)"
}
```

**Response:** `TailoredOutputOut`

The `status` field starts as `pending` and flips to `ready` or `failed` once the background thread completes. The frontend/extension polls `GET /tailored-outputs/{output_id}` until ready.

---

## Tailored Outputs

All endpoints under `/tailored-outputs` require authentication.

### `GET /tailored-outputs`

List all tailored outputs for the current user (newest first).

**Response:** `TailoredOutputOut[]`

---

### `GET /tailored-outputs/{output_id}`

Return a single tailored output by id.

**Response:** `TailoredOutputOut`

```json
{
  "id": "...",
  "job_post_id": "...",
  "status": "ready",
  "tailored_json": "{ ... }",
  "draft_message": "...",
  "error": null,
  "fit_score": 0.92,
  "original_fit_score": 0.34,
  "tailored_fit_score": 0.92,
  "winner": "tailored",
  "delta": 0.58
}
```

---

### `GET /tailored-outputs/{output_id}/download`

Download the generated PDF for this tailored output.

**Response:** `application/pdf` binary (57 835 bytes for a typical one-page resume).

---

### `POST /tailored-outputs/{output_id}/approve`

Flip a `ready` output to `approved`. Sending is refused until approval.

**Response:**

```json
{ "status": "approved" }
```

---

### `POST /tailored-outputs/{output_id}/send`

Send the outreach email with the PDF attached. Requires approval first.

Tries in order:
1. **Gmail API** — the user's connected Gmail (no password needed).
2. **Per-user SMTP** — the user's stored `smtp_app_password`.
3. **`.env` SMTP** — the shared fallback.

Returns a clear `400` if no credential path is available, telling the user to connect Gmail or set an App Password.

---

## Pipeline lifecycle

```
POST /job-posts
  └─ persist TailoredOutput(status=pending)  →  return id (fast)
                               │
                    background thread (pipeline_runner.py)
                               │
         parse_job → tailor → review_resume → outreach → review_message
                               │
         render DOCX + PDF  →  status = ready (or failed with error)
                               │
         GET /tailored-outputs/{id}  ◄── UI polls every ~8–10 s
```

If the server restarts mid-pipeline, startup flags any stuck `pending`/`processing` output as `failed` with a clear reason so the UI never hangs.
