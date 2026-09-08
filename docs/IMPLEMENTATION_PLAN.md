# JobTailor — Workflow Implementation Plan

> Definitive plan reflecting **what the user actually wants**. This is the single
> source of truth. The `MODULE_MAP.md` is the module/status checklist that backs
> it up; this doc explains the end-to-end workflow and which module owns each step.

## 1. Locked Architecture (decisions made)

- **No RAG / no vector store.** A single small ~1-page resume does not benefit
  from chunking + retrieval — loading the whole structured JSON into the prompt
  is strictly more accurate. RAG would only add complexity and re-introduce
  cross-section jumbling.
- **Baseline = structured JSON** (parsed from upload OR built/edited via the
  frontend UI). Section-by-section, faithful to the source.
- **Bounded agentic review loop** on the resume-tailor step only:
  `tailor → retrieve baseline → review (fit + structure) → re-tailor` (max 2×).
  Outreach = one-shot generate + fabrication guard. This is the only genuinely
  "agentic" part, and it is bounded so it never runs away or blocks.
- **Human approval gate before send.** Nothing leaves the user's inbox without
  explicit approval.
- **Send from the user's own email** (Gmail API preferred, per-user SMTP fallback).

---

## 2. User-Facing Workflow

```
                     ┌────────────────────────────────────────────┐
                     │ 1. BASELINE RESUME (two equal input paths)  │
                     │    a) Upload PDF/DOCX → section-by-section  │
                     │       parse → structured JSON (saved)       │
                     │    b) Build/edit every section in the UI    │
                     │       (no document required)                │
                     └───────────────────┬────────────────────────┘
                                         │ Resume saved under the user's id (is_active)
                                         ▼
                     ┌────────────────────────────────────────────┐
                     │ 2. JOB CAPTURE (three input paths)         │
                     │    a) Paste job post text                  │
                     │    b) Share a URL (auto-fetch)             │
                     │    c) Chrome extension on a LinkedIn post  │
                     └───────────────────┬────────────────────────┘
                                         │ job → JobPost (pending)
                                         ▼
                     ┌────────────────────────────────────────────┐
                     │ 3. PIPELINE (background, async)            │
                     │    parse_job → tailor(resume) → review loop │
                     │    → gaps → draft_message → render DOCX+PDF │
                     └───────────────────┬────────────────────────┘
                                         │ TailoredOutput → ready (poll)
                                         ▼
                     ┌────────────────────────────────────────────┐
                     │ 4. HUMAN APPROVAL (dashboard / extension)  │
                     │    Preview tailored resume + draft message →│
                     │    gap checklist → Approve                 │
                     └───────────────────┬────────────────────────┘
                                         ▼
                     ┌────────────────────────────────────────────┐
                     │ 5. SEND from the user's own email          │
                     │    Gmail API (preferred) / per-user SMTP   │
                     │    / .env SMTP fallback                    │
                     └────────────────────────────────────────────┘
```

---

## 3. Step-by-Step → Module Map

### Step 1 — Baseline Resume
| Functionality | Module / Submodule |
|---|---|
| Extract bytes from PDF/DOCX (10MB cap) | **M4.2** `routers/resumes.py::_extract_text_from_upload` |
| Parse section-by-section into structured JSON | **M5.1** `agents/resume_parser_agent.py::parse_resume_text` |
| Regex fallback for contact (email/phone/location/LinkedIn/GitHub) | **M5.2** `agents/contact_extractor.py` |
| **Fixes to land**: `location`, `github` populated; projects stay in `projects`; skills flattened (drop subgroup headings) | **M5.1** `_validate_output` + prompt hardening |
| Save under user id, `is_active`; human edit bumps version | **M4.2** / **M1.1** `Resume` |
| Build/edit every section in the UI (no upload) | **M13.4** `ResumePanel` + **M4.2** `PUT /resumes/{id}` |

### Step 2 — Job Capture
| Functionality | Module / Submodule |
|---|---|
| Paste text | **M13.4** `JobPanel` → **M10.3** `POST /job-posts` |
| URL (auto-fetch via trafilatura) | **M4.1** `fetchers/url_fetcher.py` |
| LinkedIn post via extension | **M14** `content_script.js` + `background.js` (TAILOR_CURRENT_POST) |
| Create `JobPost` + `pending TailoredOutput` | **M10.3** `routers/job_posts.py` |

### Step 3 — Pipeline (background, async)
| Functionality | Module / Submodule |
|---|---|
| Async launch (daemon thread), own DB session, status transitions | **M10.2** `pipeline_runner.py` |
| Parse job → `{company, role, required, nice_to_have, seniority, keywords}` | **M6** `agents/job_parser_agent.py` |
| Tailor resume (summary rewrite + relevance reorder) | **M7.1** `agents/tailoring_agent.py::tailor_resume` |
| Deterministic post-processing: reclassify project/experience, merge dropped sections, dedupe, reorder | **M7.2** `_reclassify_entries`, `_merge_missing_sections`, `_dedupe_entries`, `_reorder_*` |
| **Review loop** — score fit/structure; re-tailor ≤2× if it regressed | **M8** `review_agent.py` (wire into `orchestrator`) |
| Gap analysis checklist | **M8** `agents/gap_analysis_agent.py` |
| Draft full application email (subject+body) | **M9** `agents/outreach_agent.py` + `_check_fabrication` |
| Render DOCX + PDF | **M11.1** `renderer.py`, **M11.2** `pdf_renderer.py` |
| Persist `Message`, set `ready`/`failed` | **M10.2** |

### Step 4 — Human Approval
| Functionality | Module / Submodule |
|---|---|
| Poll output state | **M11.4** `GET /tailored-outputs/{id}` |
| Preview tailored resume + draft + gap checklist | **M13.4** `ResultPanel` |
| Approve (gate) | **M11.4** `POST /{id}/approve` |

### Step 5 — Send (from user's own email)
| Functionality | Module / Submodule |
|---|---|
| Gmail API preferred (OAuth, refresh token) | **M12.2** `email_utils/gmail_api.py` |
| Per-user SMTP fallback | **M12.1** `email_utils/sender.py` |
| `.env` SMTP last resort | **M12.1** |
| `split_subject_line` on the draft | **M12.1** |
| Ownership/IDOR checks on all output endpoints | **M11.4** |

---

## 4. Cross-Cutting Modules (verified once, reused everywhere)

| Module | Purpose |
|---|---|
| **M0** Foundation/Config | `.env`, DB engine, FastAPI app, CORS, lifespan |
| **M1** Data Layer | SQLAlchemy models + Pydantic schemas |
| **M2** LLM Client | One unified `call_llm()` + separate parser stack + `extract_json()` |
| **M3** Auth | bcrypt, JWT, Google OAuth, Gmail connect |
| **M13** Frontend | Next.js dashboard (profile/resume/job/result/history panels) |
| **M14** Chrome Extension | Capture current post, run pipeline, approve/send |
| **M15** E2E | `backend/smoke_test.py` full flow |

---

## 5. Build Order (one module at a time, verify each)

1. **M5** Resume Parser — land the 4 reported fixes (location, github, projects, skills). *Currently in progress.*
2. **M6** Job Parser — verify role/company/skills accuracy.
3. **M7** Tailoring — verify section separation post-processing (already much fixed).
4. **M8** Review loop + Gap Analysis — wire `review_agent.py` into `orchestrator.py`.
5. **M9** Outreach — verify full application email output.
6. **M10** Orchestration + runner — verify async flow + status transitions.
7. **M11** Rendering — verify DOCX/PDF + approve/send endpoints.
8. **M12** Email sending — Gmail API + SMTP fallbacks.
9. **M13/M14/M15** Frontend, extension, E2E smoke test.

Legend: **M5** in progress · M6–M15 pending (per-module status in `MODULE_MAP.md`).
