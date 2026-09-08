# JobTailor — Module Map

> Definitive ground-up breakdown of the whole system into modules, submodules,
> and functionalities. We implement / verify each **one at a time**, so this is
> the single source of truth for where we are and where we're going.

Legend: ✅ = built & verified · 🛠 = built, needs verification/fix · ☐ = not yet done

---

## M0. Foundation & Configuration

### M0.1 `backend/app/config.py`
- 🛠 Load `.env` via `pydantic-settings`, anchored to `backend/` (`BASE_DIR`)
- 🛠 DB, JWT, Google OAuth, Gmail API, LLM stack, resume-parser stack, SMTP settings
- 🛠 Resolve SQLite URL to an absolute path
- Functionality to verify: every setting reads from `.env` correctly

### M0.2 `backend/app/database.py`
- 🛠 SQLAlchemy engine + session factory (`SessionLocal`)
- 🛠 `check_same_thread=False` for SQLite (background-thread safety)
- 🛠 `get_db()` FastAPI dependency (per-request session)

### M0.3 `backend/app/main.py`
- 🛠 FastAPI app + lifespan: create tables, SQLite migrations, orphan-output cleanup
- 🛠 CORS middleware
- 🛠 Mount all routers + `/health`

---

## M1. Data Layer

### M1.1 `backend/app/models.py` — SQLAlchemy tables
- ☐ `User` — email, hashed_password, oauth_tokens, profile fields, per-user SMTP
- ☐ `Resume` — label, resume_json, version, is_active
- ☐ `JobPost` — url, raw_text, source_type
- ☐ `TailoredOutput` — tailored_json, docx_path, pdf_path, gap_analysis, status
- ☐ `Message` — draft_text, status, sent_at

### M1.2 `backend/app/schemas.py` — Pydantic request/response shapes
- ☐ Auth: `UserCreate`, `UserLogin`, `UserUpdate`, `TokenResponse`, `UserOut`
- ☐ Resume: `ResumeJSON`, `ResumeSection`, `ExtraSection`, `ResumeOut`
- ☐ Pipeline: `JobPostCreate`, `JobPostOut`, `TailoredOutputOut`

---

## M2. LLM Client — `backend/app/agents/llm_client.py`

- 🛠 `call_llm()` — single unified call (provider: `groq` | `openai_compatible`)
- 🛠 `call_resume_parser_llm()` — separate stack for the parser
- 🛠 `extract_json()` — strip code fences / prose, locate outermost `{}`
- 🛠 json_mode retry without `response_format`
- Functionality to verify: temperature 0.3, 300s timeout, correct headers

---

## M3. Auth System

### M3.1 `backend/app/auth.py`
- 🛠 `hash_password` / `verify_password` (bcrypt)
- 🛠 `create_access_token` (JWT, HS256, 7-day expiry)
- 🛠 `get_current_user` dependency (401 on bad/missing token)

### M3.2 `backend/app/routers/auth.py`
- ☐ `POST /auth/register` — create user → JWT
- ☐ `POST /auth/login` — verify → JWT
- ☐ `POST /auth/logout` — clear oauth_tokens
- ☐ `GET/PUT /auth/me` — profile + per-user SMTP (password write-only)
- ☐ Google OAuth: `/auth/google/login`, `/auth/google/token`, `/auth/google/callback`
- ☐ Gmail API: `/auth/gmail/status`, `/auth/gmail/connect`, `/auth/gmail/callback`

---

## M4. Document Ingestion

### M4.1 `backend/app/fetchers/url_fetcher.py`
- ☐ `fetch_and_extract(url)` — httpx GET + trafilatura main-content extraction
- ☐ 15s timeout, redirects, browser user-agent
- ☐ Known limit: LinkedIn is auth-walled → falls back to paste/extension

### M4.2 `backend/app/routers/resumes.py` — upload flow
- ✅ `_extract_text_from_upload()` — PDF (pdfplumber) / DOCX (python-docx), 10MB cap
- ✅ **FIXED 2026-09-07 (root cause of "no project titles")**: the DOCX branch only read `document.paragraphs`, which silently **skips table cells and text boxes**. Resumes with 2-column / skill-matrix layouts put titles inside tables → the LLM never received them. Now `_extract_docx_content()` walks the body in reading order (paragraphs + table cells + text boxes) so content is forwarded to the LLM as-is; PDF branch adds `layout=True` + `extract_tables()`. Covered by `tests/test_upload_extraction.py` (2/2).
- ✅ `POST /resumes/upload` — extract → parse → contact-extract → save Resume
- ✅ **NEW**: the extracted `raw_text` is now persisted on `Resume.raw_text` (column + SQLite migration, exposed as `ResumeOut.raw_text`) so a bad parse is always reproducible from the exact input instead of guessing — this is how the `53bf2eb5…` blank-title parse became diagnosable in code.
- ✅ `PUT /resumes/{id}` — human edit, bump version
- ✅ `GET /resumes` — list

---

## M5. Resume Parser — ⭐ ROOT CAUSE OF CURRENT COMPLAINT

This is where the jumbled output actually originates. Everything downstream
(tailoring, merge, render) inherits whatever shape the parser produces — so
fixing M7's merge logic without fixing M5 will only mask the symptom on some
inputs and re-expose it on others. Fix M5 first, then verify M7 still behaves
correctly as a safety net (not as the primary fix).

### M5.1 `backend/app/agents/resume_parser_agent.py`

| # | Symptom observed | Likely cause | Concrete fix | Status |
|---|---|---|---|---|
| 1 | `location` comes back empty | Resume has `City: Lahore \| Country: Pakistan` as a labeled field, not a single free-text "City, Country" line the prompt expects | Update prompt with an explicit example showing labeled `City:` / `Country:` input → combined `"Lahore, Pakistan"` output. Add a post-parse fallback: if `location` is empty, run the same regex `contact_extractor` uses on the raw text before giving up. | ✅ Done — `_fix_labeled_location` + `_apply_contact_fallback`; verified `"Lahore, Pakistan"` |
| 2 | `github` comes back empty | A GitHub URL exists in the raw text but sits outside where the model is looking (e.g., in a bullet, footer, or next to LinkedIn without its own label) | Add an explicit instruction: "Scan the *entire* document, not just the header block, for a github.com URL." Add the same regex fallback as location — `contact_extractor.extract_contact_info()` already does this reliably; wire its result in as a fallback whenever the LLM field is empty. | ✅ Done — prompt says scan entire doc; `_apply_contact_fallback`; verified `github.com/hooriaattas` |
| 3 | Projects (`AI-Driven Malware Detection`, `Automated Timetable Alarm System`) land under `experience` | The prompt doesn't give the model a hard rule for what makes something "experience" vs "project" | Add a **binary, explicit classification rule** | ✅ Done — binary rule + Example 2 few-shot; verified both projects → `projects` |
| 4 | Skills are section headings instead of atomic skills | Resume groups skills into labeled subgroups; model preserves the label as a skill | **(a)** prompt: `skills` must be flat, never a category/subgroup heading. **(b)** `_looks_like_group_heading()` heuristic + `_clean_skills()` flattening in `_validate_output`. | ✅ Done — group-heading heuristic + flatten; parenthesized multi-tool skills preserved (comma-split aware); verified clean flat list |
| 5 | (general) Regressing silently on a different resume format | Prompt-only fixes fragile across resume styles | Build a fixture set (3–5 real anonymized resumes) and re-run the parser against all after every prompt change. | 🛠 In progress — **61 tests passing**: `tests/test_resume_parser_guards.py` (29, deterministic guards), `test_date_utils.py` (12), `test_education_split.py` (7), `test_review_loop.py` (4), `test_tailoring_postprocess.py` (5), `test_upload_extraction.py` (2); `tests/verify_hooria_resume.py` is the real-resume E2E (9 structural checks) |
| 6 | **Prompt got too big for the local 8B model** — output regressed worse than before (skills `[]`, experience `[]`, empty cert/title skeletons) | The prompt had grown to ~250 dense lines (rules + 2 full examples); a small local model drained content into empty structure | **Rewritten as a lean ~70-line prompt** (`SYSTEM_PROMPT`): exact-only extraction, don't invent, empty fields for missing content, terse rules. **No few-shot examples.** Plus `_drop_empty_entries()` so a stray empty skeleton never ships. Verified: 6 projects w/ titles+bullets, 3 paid roles, `BS Computer Science`+GPA, 18 flat skills, full summary, `Lahore, Pakistan`, github — 9/9 E2E checks PASS. | ✅ Done — prompt simplified; see M5.1 continued. |
| 7 | **Second, different resume** (uploaded 2026-09-07 18:53, `53bf2eb5…`) parsed with ALL project/cert titles empty (bullets+dates present), `location: "Google, Slack"`, invented `management_and_leadership_skills` key, duplicate project rows | LLM drained titles into empty strings on a different layout; raw source text was never persisted so the exact input could not be reproduced | **(a)** persist extracted `raw_text` on `Resume` (new column + migration) so any bad parse is reproducible; **(b)** `_restore_missing_titles()` — bounded one-shot re-query that recovers drained titles **only if the title appears verbatim in the resume text** (never invented); **(c)** fold invented keys (`management_and_leadership_skills` → `leadership`) + `_fold_invented_keys()` catch-all → `extra_sections`; **(d)** extend `_LOCATION_BLOCKED_TOKENS` + extractor `_NON_LOCATION_WORDS` with platform names (`google`, `slack`, …); **(e)** `_dedupe_entries()` in parser. | ✅ Done — needs re-upload of resume #2 to confirm on the real file |

### M5.1 continued — `_validate_output(data)`
- ✅ Enforce required keys + correct types (existing)
- ✅ **NEW**: `_looks_like_group_heading(skill: str) -> bool` — the heuristic from fix #4 above
- ✅ **NEW**: `_clean_skills()` + `_split_skill_string()` (comma-aware, paren-safe flattening)
- ✅ **NEW**: `_normalize_link()` — canonical `https://linkedin.com/in/…` / `https://github.com/…`
- ✅ **NEW**: contact-field fallback chain — `_apply_contact_fallback()` fills `location`/`github`/`linkedin`/`phone`/`email`/`full_name` from regex when LLM field is empty
- ✅ `_fix_labeled_location()` — labeled `City:`/`Country:` → `"City, Country"`
- ✅ Ensure `skills` is fully flattened (no nested lists/dicts survive to the final array)
- ✅ **NEW (schema guard)**: `_SECTION_ALIASES` folds invented keys (`work_experience`, `professional_experience`, `management_and_leadership_skills`, …) into canonical sections; `_normalize_entry_fields()` maps `position`→`title`, `city`/`country`→`location`; `_fold_invented_keys()` catch-all sends any *other* invented entry-list key into `extra_sections` (content never silently dropped)
- ✅ **NEW (honesty guard)**: `_guard_location()`/`_looks_like_tech()` reject a location that is really a tools phrase (e.g. `"Numpy, Matplotlib"`, `"Google, Slack"`) so the regex fallback can fill the real one; `_drop_empty_entries()`/`_drop_empty_extra_sections()` strip empty placeholder skeletons the model emits; `_dedupe_entries()` collapses duplicate rows the model emits twice
- ✅ **NEW (title repair)**: `_restore_missing_titles()` — bounded one-shot re-query that recovers entry titles the model drained (empty title on a entry that has bullets/dates); each patched title must appear **verbatim** in the resume text (`_title_is_in_raw`) or it is discarded, preserving the never-invent guarantee. Covered by `test_restore_missing_titles_patches_verbatim_only` / `test_restore_missing_titles_noop_when_none_missing`.
- ⭐ **Prompt rewritten lean (2026-09-07)**: the SYSTEM_PROMPT was cut from ~250 dense lines (accumulated rules + 2 full examples) to a concise ~70-line "extract only what is written, never invent, keep it simple" prompt. Small local models (Llama 3.1 8B) cannot follow an over-specified prompt — the bloated one caused output to degrade below the previous baseline (empty skills/experience, empty cert/title skeletons). Few-shot examples and verbose sub-rules removed; the surviving rules are: exact-verbatim extraction, experience-needs-an-employer, flat skills (no group headings) but extract **every** tool/language named *anywhere*, preserve dates/bullets/headings verbatim, extra_sections for everything else.

### M5.2 `backend/app/agents/contact_extractor.py` — regex fallback
- ☐ Email, phone, location, LinkedIn, GitHub, website, full_name extraction
- ☐ Normalize LinkedIn/GitHub to canonical `https://` form
- ☐ Fallback-only: never overwrite user-set profile values
- ☐ **NEW ROLE**: also serves as the fallback source for M5.1's fix #1/#2 above — not just for profile auto-fill on upload, but inline within the parser's own validation step

### M5.3 Date & duration normalization — **NEW**

Dates are one of the highest-leverage fields for tailoring's relevance
reordering and for the fabrication check in outreach — if they're malformed,
those downstream features silently degrade even when sections are correctly
classified.

- ✅ `backend/app/agents/date_utils.py` — `parse_date_range()` / `_parse_single()` / `normalize_date_range()`; collapses `"Jan 2022 - Present"`, `"2022-2024"`, `"06/2021–12/2022"`, `"Since March 2023"` into canonical ISO (`YYYY` / `YYYY-MM` / `YYYY-MM-DD`) + `ongoing` flag + display string.
- ✅ Ongoing roles/projects (`"Present"`, `"Current"`, `"Ongoing"`, no end date) recognized consistently — feeds `_relevance_score` and outreach `_estimate_years`.
- ✅ Invalid ranges flagged; single-year and year-month handled at matching granularity; garbage/ambiguous passthrough with original display preserved.
- ✅ Stored as `date_start`/`date_end`/`date_ongoing` on every entry (experience/projects/education/certifications/leadership/extra_sections) via `_normalize_entry_metadata()`; canonical `dates` display kept.
- ✅ `tests/test_date_utils.py` — 12/12 passing.

### M5.4 Education & certifications parsing — **NEW**

Currently lumped in with "capture all sections" but not given the same
scrutiny as experience/projects — worth calling out explicitly since degree
name, institution, and graduation date each has its own common failure mode.

- ✅ `_split_education()` + `_degree_match()`/`_extract_degree()`/`_extract_field()` — `"BS Computer Science, FAST-NUCES"` → `{degree: "BS", field: "Computer Science", institution: "FAST-NUCES"}`; `"Master of Science in Data Science"` → `MS/Data Science`; `"Bachelor of Science"` → `BS/""`. Wired into `_validate_output` for the `education` key.
- ✅ GPA/honors captured only if explicitly present.
- ✅ Certifications distinguished from courses/MOOCs (prompt rule: certs = issued credential w/ issuer+date; courses stay under skills).
- ✅ In-progress education flagged via `_is_education_in_progress()` (no end date / ongoing / future graduation year → `in_progress: true`).
- ✅ `tests/test_education_split.py` — 7/7 passing.

### M5.5 `extra_sections` handling — **NEW**

This is the most under-specified part of the current parser (publications,
languages, volunteering, awards) and is exactly where content silently gets
dropped when the model doesn't recognize a heading it hasn't seen before.

- ✅ Whitelist-free fall-through: prompt rule "Extra sections (NEVER DROP CONTENT)" — any unrecognized heading goes into `extra_sections` verbatim, never discarded; standard sections that are present must also all be captured under their exact keys.
- ✅ Original heading text preserved verbatim in `extra_sections[].heading`.
- ✅ Language proficiency entries (`"English (Fluent)"`) go to `extra_sections`, not into `skills`.

### M5.6 Full-name & header edge cases — **NEW**

- ☐ Multi-part names (compound surnames, middle names, honorifics like "Dr.", "Eng.") should not be truncated to just first+last. → **🛠 Not pushed further — `contact_extractor` already holds 4 words/line; no sample has exceeded it; revisit if a resume needs it.**
- ✅ No-name fallback: if `full_name` is still empty after regex extraction, derive one from the email local-part (`sara.ali@gmail.com` → `Sara Ali`) — `_apply_contact_fallback()`; covered by `test_email_local_part_name_fallback`.
- ✅ Never confuse a section heading that happens to be the first line (`"CURRICULUM VITAE"`, `"RESUME"`, `"CV"`) for the name — prompt rule #1 + extractor `name_kw` guard.

### M5.7 Layout & extraction robustness — **NEW**

These live one layer below the LLM prompt (in how text reaches the parser at
all) but directly cause the same "jumbled" symptom if the raw text itself is
already scrambled before the LLM ever sees it.

- ✅ **Tables used for layout** (common in skill-matrix / two-column resumes):
  DOCX extraction now includes table cells + text boxes in document order
  (`_extract_docx_content` in M4.2); PDF adds `extract_tables()` merge +
  `layout=True`. This was the actual cause of "parsed projects have no titles" —
  title lines lived in table cells and never reached the LLM.
- ☐ Multi-column resume layouts: detect suspiciously short alternating lines and extract column-aware rather than raw reading order — still open for single-`extract_text()` PDFs.
- ☐ Non-ASCII / Unicode artifacts (smart quotes, non-breaking spaces, ligatures) normalization — still open.

### M5.8 Parser-level testing — extends M5.1 fix #5
- ☐ Fixture set covers, at minimum: labeled contact fields, subgrouped skills, projects-styled-as-jobs, multi-column layout, unrecognized `extra_sections` headings, in-progress education, and a resume with no dates on a project.
- ☐ Each fixture has an expected-output JSON checked into the repo — regressions become a diff, not a manual re-read of parsed JSON.
- ☐ Run this fixture set specifically after **any** prompt change to `resume_parser_agent.py`, not just at release time — prompt tweaks are the most common source of silent regressions on inputs not in front of you at the time.

---

## M6. Job Parser — `backend/app/agents/job_parser_agent.py`

- ☐ `parse_job_post(raw_text)` → `{company, role, required_skills, nice_to_have, seniority, keywords}`
- ☐ Verify: exact role title, accurate company, sensible skills split

---

## M7. Tailoring — `backend/app/agents/tailoring_agent.py`

Even with M5 fixed at the source, tailoring's own post-processing needs to be
a **safety net**, not a source of new bugs — right now `_merge_missing_sections`
can reintroduce jumbling that the LLM tailoring step didn't cause, simply by
re-merging against an already-misclassified original.

### M7.1 LLM rewrite
- ☐ `tailor_resume()` — single LLM call, summary rewrite + relevance reorder
- ☐ Prompt enforces strict section separation: *"experience = paid work only; projects = personal/academic only — preserve whatever classification the input JSON already has, do not re-classify."* (Tailoring should never re-decide experience-vs-project; that decision belongs to M5, made once.)

### M7.2 Deterministic post-processing
- ✅ `_normalize_resume()` — aliases, clean text, strip company-dup from titles
- ✅ **`_reclassify_entries(resume_json)`** — last-resort safety net. Now **marker-based**, not just title-matching: an experience entry with no company AND project-shaped bullets (build vocabulary like "built"/"pipeline"/"docker"/API, or a public URL) moves to `projects`, even when the *original* resume itself is misclassified. See `test_tailoring_postprocess.py`.
- ✅ `_merge_missing_sections()` — restores dropped sections, filters prose skills, keeps split real skills. **Runs `_reclassify_entries` on a deepcopy of the original too** before matching, so a misclassified original no longer merges back under the wrong section (verified by `test_merge_missing_sections_reclassifies_original_before_match`).
- ✅ `_dedupe_entries()` — dash-normalized dedup, **section-safe**: never collapses two entries that share a title but have no company on either (cross-section contamination guard), verified by `test_dedup_keeps_companyless_entries_apart`.
- ✅ `_reorder_experience` / `_reorder_projects` / `_reorder_skills_by_relevance()`
- ✅ `_matches_original_skill()` — meaningful-token match only (length ≥ 3)
- ✅ `_skill_has_prose_wording()` — filters JD-prose sentences from skills
- ✅ `tests/test_tailoring_postprocess.py` — 5/5 passing

---

## M8. Review Loop + Gap Analysis

### M8.1 `backend/app/agents/review_agent.py` — bounded agentic loop
- ✅ `review_message()` — critique draft message vs job — wired as an optional gate in M10.1
- ✅ `_guardrail_issues()` — deterministic checks: empty sections, dupes, un-tailored summary, unreordered experience, JD-prose in skills, **projects merged into experience** (this guardrail is your automated regression check for the M5/M7 fixes above)
- ✅ `_compare_fit()` — head-to-head keyword-overlap fit score (original vs tailored); tailored must not regress
- ✅ **WIRED (was "TO WIRE")**: `orchestrator.run_pipeline()` now calls `review_resume()` after tailor and re-runs the producer when `needs_revision`, bounded at `MAX_REVIEW_REVISIONS = 2`, keeping the highest-fit revision; review failure skips the loop gracefully instead of killing the pipeline. Covered by `tests/test_review_loop.py` (4/4 passing).

### M8.2 `backend/app/agents/gap_analysis_agent.py`
- ☐ `analyze_missing_requirements(job_requirements, tailored_resume)` → `[{requirement, note}]`
- ☐ Empty list when resume supports everything
- ☐ Best-effort — failure never blocks pipeline

---

## M9. Outreach — `backend/app/agents/outreach_agent.py`

- ☐ `draft_outreach_message()` → full application email
- ☐ Structure: subject → greeting → opening → highlights → projects → skills → education → closing → signature
- ☐ `_check_fabrication()` — years-claimed guard + auto-retry
- ☐ Output `"Subject: ...\n\n{body}"`, parsed by `sender.split_subject_line`

---

## M10. Pipeline Orchestration

### M10.1 `backend/app/agents/orchestrator.py`
- 🛠 `run_pipeline()` — sequential: parse_job → tailor → **review (bounded re-tailor ≤2×)** → gaps → outreach — **REWRITTEN and wired**
- ✅ `review_resume()` `needs_revision` interpreted; re-calls `tailor_resume` with feedback; revision count tracked (`state["revisions"]`); keeps the best (highest-fit) revision — `MAX_REVIEW_REVISIONS = 2`
- ✅ `review_message()` optional gate on the draft (one re-draft on `needs_revision`)
- ✅ `PipelineState` TypedDict (incl. `error`, always present)
- ✅ Error propagation: parse/tailor/outreach failures return `state["error"]`
- 🛠 Remaining: verified via `tests/test_review_loop.py`; end-to-end against a real job post + LLM pending (M15 smoke)

### M10.2 `backend/app/pipeline_runner.py`
- ☐ `start_pipeline()` — fire-and-forget daemon thread
- ☐ `_run_pipeline_for_output()` — own DB session, status transitions, render DOCX/PDF, persist Message, set `ready`/`failed`

### M10.3 `backend/app/routers/job_posts.py`
- ☐ `POST /job-posts` — resolve resume, create JobPost + pending TailoredOutput, start pipeline, return id immediately
- ☐ Auto-fetch URL when only url given

---

## M11. Rendering

### M11.1 `backend/app/rendering/renderer.py` — DOCX
- ☐ `render_resume()` — ATS-safe DOCX (python-docx), Calibri, centered header, plain-text headings, real bullets
- ☐ `contact_info_from_user()` — header fields from profile
- ⚠ KNOWN QUIRK: header email comes from profile/account email, not resume JSON

### M11.2 `backend/app/rendering/pdf_renderer.py` — PDF
- ☐ `render_resume_pdf()` — A4 fpdf2, mirrors DOCX layout, punctuation-mapped
- ☐ Calibri from `C:\Windows\Fonts` fallback to Helvetica

### M11.3 `backend/app/rendering/build_template.py`
- ☐ Reference docxtpl template (not runtime)

### M11.4 `backend/app/routers/tailored_outputs.py`
- ☐ `GET /tailored-outputs`, `GET /{id}` (polling)
- ☐ `GET /{id}/download` — PDF (pipeline PDF → sibling → on-the-fly render)
- ☐ `POST /{id}/approve` — human-in-the-loop gate
- ☐ `POST /{id}/send` — Gmail API → per-user SMTP → .env SMTP
- ☐ ownership checks (IDOR protection)

---

## M12. Email Sending

### M12.1 `backend/app/email_utils/sender.py` — SMTP fallback
- ☐ `split_subject_line()` — extract `Subject:` from draft
- ☐ `send_email_with_attachment()` — MIME multipart, STARTTLS, per-user creds override

### M12.2 `backend/app/email_utils/gmail_api.py` — Gmail API (preferred)
- ☐ `gmail_configured()` / `build_authorization_url()` / `exchange_callback_code()` / `store_gmail_token()`
- ☐ `send_via_gmail_api()` — `gmail.send` scope, refresh token handling, `invalid_grant` → reconnect prompt

---

## M13. Frontend (Next.js) — `frontend/`

### M13.1 `lib/api.ts`
- ☐ Typed client for every endpoint, JWT in localStorage, base URL auto-detect

### M13.2 `lib/auth-context.tsx`
- ☐ `user`, `login`, `register`, `logout`, `refresh`

### M13.3 Pages
- ☐ `app/login/page.tsx` — login/register + Google OAuth popup
- ☐ `app/dashboard/page.tsx` — auth-gated 2-column layout

### M13.4 Dashboard panels — `components/dashboard/`
- ☐ `ProfilePanel` — profile + SMTP + Connect Gmail
- ☐ `ResumePanel` — upload/parse + select
- ☐ `JobPanel` — paste text or URL submit
- ☐ `ResultPanel` — poll, preview, gap checklist, download/approve/send
- ☐ `HistoryPanel` — past outputs + status badges
- ☐ `StatusBadge.tsx`

---

## M14. Chrome Extension — `extension/`

### M14.1 `manifest.json`
- ☐ MV3, permissions, content script on linkedin

### M14.2 `content_script.js`
- ☐ Read LinkedIn post text from page (read-only DOM)

### M14.3 `background.js`
- ☐ Service worker: owns token, all backend calls, Google OAuth via `chrome.identity`
- ☐ Actions: LOGIN, LOGIN_WITH_GOOGLE, CHECK_AUTH, CHECK_RESUME, UPDATE_PROFILE, LOGOUT, TAILOR_CURRENT_POST, GET_STATUS, LIST_OUTPUTS, APPROVE_OUTPUT, SEND_OUTPUT, DOWNLOAD_OUTPUT, GET_GMAIL_STATUS, CONNECT_GMAIL

### M14.4 `popup.html` + `popup.js`
- ☐ Login / main / tailor / result / history views
- ☐ Inline profile editor, Gmail connect, polling, action buttons

---

## M15. End-to-End Verification — `backend/smoke_test.py`
- ☐ Full pipeline smoke test: auth → upload → submit → poll → render → approve → send
- ☐ **NEW**: include the M5 fixture set (3–5 resume styles) as part of the smoke test so parser regressions are caught automatically, not just discovered via manual complaint

---

## Current Status Summary

**Cleaned** ✅: generated outputs removed, pipeline DB records cleared, stale WORKFLOW.md deleted.
**Working baseline**: 1 real user (`hooriaattas425@gmail.com`), 1 resume (Hooria Attas), empty pipeline tables.

**Next up — in this order**:
1. ✅ **M5.1** — fixed at the source: contact-field fallback (#1/#2), hard experience/project classification rule with few-shot examples (#3), flat-skills enforcement (#4). **All fixes verified against the real Hooria Attas resume** (6/6 defect checks pass).
2. ✅ **M5.3–M5.6** — date normalization (`date_utils.py`, 12 tests), education/certification field-splitting (7 tests), `extra_sections` fall-through, and name/header edge cases (email local-part fallback). Guard suite 17/17.
3. ✅ **M5.7 / M4.2** — extraction is now table/text-box aware (root cause of the first "no titles" complaint); `raw_text` persisted on `Resume` so bad parses are reproducible. Multi-column + Unicode cleanup still open.
4. ✅ **M8.1 wiring** — the built review loop is now LIVE in `orchestrator.run_pipeline()`: bounded re-tailor (max 2×) keeps the highest-fit revision, review failure skips gracefully (4/4 tests).
5. ✅ **M7.2** — `_reclassify_entries()` is marker-based + runs on a copy of the original before merge; dedup is section-safe (5/5 tests).
6. ✅ **M5.1 continued (2nd-resume failures, 2026-09-07)** — title-repair pass (`_restore_missing_titles`, verbatim-only), invented-key folding (`management_and_leadership_skills` → `leadership`, catch-all → `extra_sections`), platform-word location guard (`"Google, Slack"`), parser dedup, `raw_text` persistence. **61 tests passing across 6 suites**; `tests/verify_hooria_resume.py` is the real-resume E2E (9 checks).
7. ⬜ **Next** — user re-uploads resume #2 (now captures raw_text + runs title-repair) to confirm the blank-title parse is fixed on the actual file; then fixture set (M5.8), M4.1/M4.2 ingestion, M8.2/M9 end-to-end LLM check, M12 email send, frontend M13, extension M14, then **M15 smoke_test.py** (full pipeline: auth → upload → submit → poll → render → approve → send).