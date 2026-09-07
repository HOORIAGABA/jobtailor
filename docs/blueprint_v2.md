# Project Blueprint: AI-Powered Job Application Assistant
### (Agentic Resume Tailoring + Outreach System)

---

## 1. One-line pitch (for resume/portfolio)

> "Built a multi-agent AI system that ingests job postings, tailors a candidate's resume with deterministic relevance reordering and an LLM review loop, generates a formatted document, and drafts personalized outreach — with a human-in-the-loop approval step, deployed across multiple free-tier cloud platforms."

This single sentence hits: **agentic AI, multi-agent orchestration, document generation, backend, cloud deployment, product ownership.**

### 1.1 Why this pitch is structured the way it is

Every clause in that sentence maps to a specific gap reviewers flagged (see the original resume-gap analysis this blueprint responds to). It's written as one sentence deliberately — a resume bullet has to survive a 6-second scan, so density matters more than completeness. Breaking it down clause by clause:

| Clause | Gap it targets | Where it's proven in the codebase |
|---|---|---|
| "multi-agent AI system" | Agentic AI / multi-agent systems | `app/agents/orchestrator.py` — LangGraph `StateGraph` with conditional routing |
| "tailors a candidate's resume" | Applied AI + agentic review loop | `app/agents/tailoring_agent.py` (deterministic reorder) + `app/agents/review_agent.py` (fit-comparison loop) |
| "generates a formatted document" | Document generation, product completeness | `app/rendering/renderer.py` (docxtpl) |
| "human-in-the-loop approval step" | Responsible-AI judgment, product thinking | `POST /tailored-outputs/{id}/approve` gate before send |
| "deployed across multiple free-tier cloud platforms" | Cloud deployment / multi-cloud (beyond Jetson edge) | Backend on Render/Fly.io, DB on Supabase/Neon, frontend on Vercel |

### 1.2 Full set of resume bullets (not just the one-liner)

The single pitch is for the top of a project entry; below it, use 3–4 bullets that each isolate one technical claim so an ATS keyword scan and a human skim both land on the right terms:

- *"Designed and implemented a multi-agent orchestration pipeline (LangGraph) coordinating four specialized agents — parsing, tailoring with a self-review loop, document rendering, and outreach drafting — with conditional error routing between stages."*
- *"Built the tailoring stage to be model-robust: an ATS gap analysis plus deterministic, keyword-driven reordering of experience/projects/skills (most-relevant-first), paired with an agentic review loop that head-to-head scores the tailored resume against the original and forces a revise when fit doesn't improve."*
- *"Developed a FastAPI + PostgreSQL backend (5 tables, JWT auth, 10+ REST endpoints) supporting multi-source job ingestion — direct paste, URL auto-fetch, and a Chrome extension (Manifest V3) for authenticated-session content capture."*
- *"Implemented an ATS-compliant document generation pipeline (docxtpl) converting structured JSON into single-column, parser-safe `.docx` output, validated against ATS layout constraints (no tables, text boxes, or multi-column layouts)."*

### 1.3 Anticipate the follow-up interview questions

A resume line only works if you can defend it live. The likely questions this pitch invites, and where to point:

- **"Walk me through the multi-agent flow."** → Point to `orchestrator.py`: entry point `parse_job` → conditional edge `route_after_parse` → `tailor` → conditional edge `route_after_tailor` → `outreach` → `END`. Emphasize that state (`PipelineState` TypedDict) is threaded through every node, and errors short-circuit to `END` rather than crashing.
- **"How do you make tailoring work with a small local LLM (e.g. Llama 3.1 8B) instead of a frontier model?"** → Honest answer: prompts alone aren't enough. The pipeline adds **deterministic corrections that never depend on the model** — it scores every experience/project/skill for keyword overlap with the job and reorders most-relevant-first in code — plus reviewer guardrails that detect a lazy echo, JD-prose dumped into Skills, and projects merged into Experience, and force a revise. That's how you get reliable output from a weak model.
- **"What happens if the LLM returns malformed JSON?"** → Point to `extract_json()` in `llm_client.py`, which strips code fences and regex-extracts the JSON object before parsing — a real, defensible answer instead of "it doesn't happen."
- **"How do you know the tailored resume doesn't just invent experience?"** → Point to the Tailoring Agent's explicit system-prompt constraint ("Do NOT invent experience... Only rephrase/reorder existing true content") and the human-approval gate as the actual enforcement mechanism, not just prompt trust.

---

## 2. System Architecture

```
┌─────────────┐      ┌──────────────────┐      ┌────────────────────┐
│   Frontend   │ ───▶ │   FastAPI Backend │ ───▶ │   Postgres DB       │
│ (Next.js/    │      │  (auth, jobs,     │      │  (users, resumes,   │
│  React)      │ ◀─── │   orchestration)  │ ◀─── │   posts, outputs)   │
└─────────────┘      └──────────────────┘      └────────────────────┘
                              │
                              ▼
                 ┌─────────────────────────┐
                 │   Agent Orchestrator     │
                 │   (LangGraph / CrewAI)   │
                 └─────────────────────────┘
                     │        │        │
         ┌───────────┘        │        └───────────┐
         ▼                    ▼                     ▼
 ┌───────────────┐   ┌────────────────┐    ┌──────────────────┐
 │ Fetcher Agent  │   │ Parser Agent   │    │ Tailoring Agent   │
 │ (URL/paste)    │   │ (extract reqs) │    │ (LLM + reorder)   │
 └───────────────┘   └────────────────┘    └──────────────────┘
                                                      │
                                                      ▼
                                          ┌──────────────────────┐
                                          │ Renderer              │
                                          │ (docxtpl → PDF)        │
                                          └──────────────────────┘
                                                      │
                                                      ▼
                                          ┌──────────────────────┐
                                          │ Outreach Agent         │
                                          │ (drafts email/message) │
                                          └──────────────────────┘
                                                      │
                                                      ▼
                                          ┌──────────────────────┐
                                          │ Human Approval (UI)    │
                                          └──────────────────────┘
                                                      │
                                                      ▼
                                          ┌──────────────────────┐
                                          │ Email Sender (Gmail API)│
                                          └──────────────────────┘
```

### 2.1 Why this shape, specifically

This is a **layered pipeline architecture with a stateful orchestration core**, not a simple request/response CRUD app, and not a fully autonomous agent loop either. That middle ground is deliberate:

- **Not simple CRUD** — because the interesting engineering problem (and the interesting resume content) is in the agent coordination and the model-robust tailoring/review loop, not in database plumbing. A pure CRUD app wouldn't touch most of the target gaps.
- **Not a fully autonomous loop** (e.g., an agent that decides its own next actions with no fixed graph) — because job-application content going out under someone's real name needs predictable, auditable behavior. A fixed graph with conditional error-routing is auditable: you can point to exactly which node ran, in what order, with what state, for any given output. A free-form autonomous agent is much harder to debug and to explain in an interview.

### 2.2 Request lifecycle, traced end-to-end

Tracing a single request end-to-end (this matches what `smoke_test.py` actually exercises):

1. **Client → Backend**: Frontend (or Chrome extension) sends `POST /job-posts` with a Bearer JWT and either `raw_text` or `url`.
2. **Auth resolution**: `get_current_user` dependency (in `app/auth.py`) decodes the JWT, loads the `User` row. Every downstream step is scoped to this `user_id` — no request touches another user's data.
3. **Content resolution**: if only a `url` was given, `fetch_and_extract()` (trafilatura) tries to pull the text; if that fails or `raw_text` was provided directly, that's used as-is.
4. **Resume resolution**: the caller can pass an explicit `resume_id`, or the backend resolves to the user's most-recently-created active resume — this is what lets the Chrome extension submit *only* the captured post text and still get correctly tailored output.
5. **Pipeline invocation**: `run_pipeline()` builds (once, cached) and invokes the compiled LangGraph. State flows `parse_job → tailor → outreach`, each node populating more of the `PipelineState` dict.
6. **Persistence**: a `JobPost` row, a `TailoredOutput` row (with the rendered `.docx` path), and a `Message` row (draft outreach text) are all written in this one request.
7. **Response**: the API returns the tailored JSON, the docx path, and the draft message in a single `TailoredOutputOut` payload — the frontend or extension popup can render all of it without a second round-trip.

### 2.3 Why FastAPI specifically (not Flask/Django/Express)

- **Async-native** — matters here because LLM calls and embedding calls are I/O- and compute-bound respectively; FastAPI's `async def` support (even though this implementation currently runs the pipeline synchronously inside a threadpool via `run_in_threadpool`, visible in the stack traces during testing) gives a clean upgrade path to background tasks/queues later without a framework rewrite.
- **Pydantic-native request/response validation** — every schema in `app/schemas.py` is enforced automatically; malformed input (e.g., an `UploadFile` that isn't `.pdf`/`.docx`) fails fast with a structured 422/400 rather than propagating a bad state deep into the pipeline.
- **Auto-generated OpenAPI docs** (`/docs`) — genuinely useful during development (used throughout this build to sanity-check payload shapes) and it's a free deliverable to show in a portfolio walkthrough.

### 2.4 Why Postgres (with SQLite as the zero-cost local default)

The implementation defaults to **SQLite** (`sqlite:///./data/jobtailor.db`) for local development and the free-tier deploy path, and switches to Postgres purely via the `DATABASE_URL` environment variable — no code changes, because SQLAlchemy abstracts the dialect. This is worth stating explicitly in interviews: it demonstrates you understand the difference between "the database I develop against" and "the database that scales," and that a clean data-access layer shouldn't hardcode that choice.

### 2.5 Why the Agent Orchestrator sits where it does

Note in the diagram that the Orchestrator sits *between* the backend and the individual agents — not as one more agent, but as the coordination layer. This maps directly to the actual code structure: `app/routers/job_posts.py` never imports an individual agent module directly; it only calls `run_pipeline()` from `orchestrator.py`. That's a deliberate seam — it means you could swap LangGraph for CrewAI or AutoGen later by rewriting `orchestrator.py` alone, without touching the router, the database layer, or any individual agent's logic. That kind of seam is exactly the sort of design decision worth narrating in a system-design interview.

---

## 3. Core Modules

### 3.0 Resume Ingestion (Resume Parser Agent)

Handles converting the user's uploaded baseline resume (PDF or DOCX) into the structured JSON schema the rest of the pipeline depends on.

**Flow:**
```
User uploads PDF/DOCX
   → Raw text extraction (pdfplumber for PDF, python-docx for DOCX)
   → Resume Parser Agent (LLM call) structures text into schema
   → Editable preview shown to user (pre-filled form)
   → User confirms/edits → saved as baseline resume_json
```

**Why an LLM step instead of pure rule-based parsing:** resumes vary wildly in layout (columns, tables, inconsistent headings), so rule-based extraction alone is unreliable. Extract raw text first (cheap, deterministic), then let an LLM do the structuring — more robust, and it's a legitimate additional agent in your pipeline.

**Extraction libraries:**
- PDF: `pdfplumber` or `PyMuPDF` (free) — note: layout (columns/tables) can get jumbled in the raw text; this is exactly why the LLM structuring step matters.
- DOCX: `python-docx` (free) — preserves heading styles, generally cleaner extraction than PDF.

**Parser Agent prompt template (concept):**
```
System: You are a resume parsing assistant. Given raw resume text,
extract it into the following JSON schema exactly. If a field is
missing, use an empty string or empty list — do not invent content.

Schema:
{
  "summary": string,
  "skills": [string],
  "experience": [
    {"title": string, "company": string, "dates": string, "bullets": [string]}
  ],
  "projects": [
    {"name": string, "description": string, "bullets": [string]}
  ],
  "education": [
    {"degree": string, "institution": string, "dates": string}
  ]
}

Return only valid JSON, no commentary.

User: <raw extracted resume text here>
```

**Human-in-the-loop step (important):** always show the parsed result in an editable form before saving as the baseline. LLM extraction will occasionally misclassify a bullet or miss a section — cheap to catch with a quick user review, and consistent with the approval-step philosophy used elsewhere in this system.

#### 3.0.1 Actual implementation, file by file

This module exists in the codebase as three cooperating pieces, not one function — worth understanding the split because it's a clean example of separating *I/O extraction* from *semantic structuring*:

- **`app/routers/resumes.py` → `_extract_text_from_upload()`**: takes the raw `UploadFile`, branches on file extension, and returns plain text. For PDFs it iterates `pdfplumber`'s `pdf.pages` and joins `page.extract_text()` output; for DOCX it joins every `document.paragraphs[i].text`. This function does zero semantic work — it's pure I/O, and deliberately kept dumb so it's easy to unit test in isolation from anything LLM-related.
- **`app/agents/resume_parser_agent.py` → `parse_resume_text()`**: takes that plain text and calls the shared `call_llm()` wrapper with the system prompt shown above, then runs the result through `extract_json()` to strip any prose/code-fence wrapping the model might add around the JSON.
- **`app/routers/resumes.py` → `upload_resume()` endpoint**: wires the two together — extract, then parse, then persist as a new `Resume` row with `resume_json` stored as a serialized string — and returns the parsed structure to the caller in the same response, so a frontend can render the review/edit form immediately without a second request.

#### 3.0.2 The edit/correction path (closing the human-in-the-loop loop)

The blueprint's "show an editable preview" instruction has a concrete endpoint behind it: `PUT /resumes/{resume_id}`, which accepts a full `ResumeJSON` payload (validated by the Pydantic schema in `app/schemas.py`) and **increments the resume's `version` field** on every edit. This matters for two reasons:

1. It gives you an honest audit trail — you can always tell whether a given `TailoredOutput` was generated from a fresh LLM-parsed resume or a user-corrected one, by cross-referencing `version`.
2. It means the correction step isn't a UI nicety bolted on top — it's a first-class, separately-versioned state in the data model (`resumes.version`), which is the kind of detail that signals real product thinking rather than a demo hack.

#### 3.0.3 Failure modes actually handled

- **Unsupported file type** (`.txt`, `.rtf`, an image, etc.): `_extract_text_from_upload()` raises an explicit `HTTPException(400, "Only .pdf and .docx files are supported")` rather than failing silently or crashing further down the pipeline.
- **Empty/unreadable extraction** (e.g., a scanned/image-only PDF with no embedded text layer): the endpoint checks `if not raw_text.strip()` and returns a 400 with a clear message, instead of sending empty text into the LLM and getting a nonsense structured resume back. Note this is a real gap worth stating honestly in a portfolio write-up: **OCR for scanned resumes is not implemented** — a good "next step" to mention if asked what you'd add next.
- **Malformed LLM JSON output**: handled centrally by `extract_json()` in `llm_client.py` (regex-based JSON-object extraction after stripping code fences), shared by every agent in the pipeline — not re-implemented per-agent.

#### 3.0.4 What's different between the blueprint's `education` schema and the shipped code

Worth noting explicitly rather than glossing over: the blueprint's original schema sketch used `{"degree", "institution", "dates"}` for education entries and `{"name", "description", "bullets"}` for projects, but the actual shipped `ResumeJSON` schema (`app/schemas.py`) unifies `experience`, `projects`, and `education` under one shared `ResumeSection` shape (`{"title", "company", "dates", "bullets"}`), reusing `company` loosely as "institution" for education rows. This was a deliberate simplification made during implementation to keep the docxtpl template and the Tailoring Agent's rewrite logic working against a single shared shape instead of three slightly different ones — a good example of a design decision that looks different in code than in the original whiteboard sketch, and a good thing to be able to explain if asked "does your implementation match your design doc exactly?" (the honest answer is: not perfectly, and here's why the deviation was reasonable).

---

### 3.1 Ingestion (Fetcher Agent)
- **Normal URLs** (careers pages, Indeed, Wellfound): auto-fetch with `httpx` + extract main content with `trafilatura`.
- **LinkedIn / auth-walled links**: fallback to user-pasted text, or a **Chrome extension** (Option B, detailed below) that reads the post from the user's own logged-in session (no scraping/ToS violation).
- Store: `job_posts(id, user_id, url, raw_text, source_type, created_at)`

#### Option B — Chrome Extension (for LinkedIn / auth-walled posts)

This is the technically correct way to get LinkedIn post content: it runs inside the **user's own browser, on their own logged-in session** — you're never impersonating a bot or storing their credentials, so it doesn't touch LinkedIn's anti-scraping/ToS issues.

**How it works:**
1. User installs the extension (loaded "unpacked" for a portfolio project — no Chrome Web Store fee needed).
2. User browses LinkedIn normally and opens a job/hiring post.
3. They click the extension's toolbar icon, or a small injected "Tailor Resume" button next to the post.
4. A **content script** reads the post's text directly from the page DOM (title, body, poster name, any listed link/email in the post).
5. Extension sends that text to your backend via a `fetch()` POST call (with the user's auth token stored in extension storage).
6. Backend treats it exactly like a pasted-text submission — same `job_posts` table, `source_type = 'extension'`.
7. Extension displays the full result inline: tailored resume preview, draft message, status badge, and action buttons (Download .docx, Approve, Send).
8. Extension shows a history view of all past tailored outputs, so the user never needs to open the web app.

**Key features (full parity with frontend):**
- **Pre-flight resume check**: Before tailoring, the extension calls `GET /resumes` to verify the user has uploaded a resume. If not, it shows "No resume on file" instead of letting the backend error.
- **User identity display**: After login, shows the logged-in email so the user can confirm it matches their frontend account.
- **Result view**: After tailoring, shows status badge (ready/approved/sent/failed), draft message preview, and three action buttons.
- **Download .docx**: Downloads the tailored resume file directly from the extension.
- **Approve**: Approves the tailored output (required before sending).
- **Send**: Sends the approved tailored resume via email.
- **History view**: Lists all past tailored outputs with status badges and skill previews. Click any to view details and take action.
- **Logout**: Clears the stored auth token.

**Minimal file structure:**
```
extension/
├── manifest.json
├── content_script.js   (reads post content from the page)
├── popup.html           (UI: login, tailor, result view, history)
├── popup.js             (popup logic, view switching, action handlers)
└── background.js        (auth token, API calls, download/approve/send)
```

**manifest.json (Manifest V3, free):**
```json
{
  "manifest_version": 3,
  "name": "Resume Tailor",
  "version": "1.0",
  "permissions": ["activeTab", "storage"],
  "host_permissions": ["https://www.linkedin.com/*"],
  "action": { "default_popup": "popup.html" },
  "content_scripts": [
    {
      "matches": ["https://www.linkedin.com/*"],
      "js": ["content_script.js"]
    }
  ],
  "background": { "service_worker": "background.js" }
}
```

**content_script.js (concept, not exact selectors — LinkedIn's DOM changes often):**
```javascript
function extractPostText() {
  // Runs only in the user's own authenticated page context
  const postEl = document.querySelector('[data-test-id="main-feed-activity-card"]')
                 || document.querySelector('.feed-shared-update-v2');
  return postEl ? postEl.innerText : null;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.action === "GET_POST_TEXT") {
    sendResponse({ text: extractPostText(), url: window.location.href });
  }
});
```

**background.js (sends captured text to your backend):**
```javascript
async function sendPostToBackend(text, url, authToken) {
  await fetch("https://your-backend.onrender.com/api/job-posts", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Authorization": `Bearer ${authToken}`
    },
    body: JSON.stringify({ raw_text: text, url, source_type: "extension" })
  });
}
```

**Extension message actions (background.js):**

| Action | Description |
|--------|-------------|
| `LOGIN` | Email/password login |
| `LOGIN_WITH_GOOGLE` | Google OAuth via `chrome.identity` |
| `CHECK_AUTH` | Returns `{loggedIn, email}` after verifying token |
| `CHECK_RESUME` | Returns `{exists: bool}` — pre-flight check |
| `TAILOR_CURRENT_POST` | Captures LinkedIn post + submits to pipeline |
| `GET_STATUS` | Fetches a specific tailored output by ID |
| `LIST_OUTPUTS` | Lists all past tailored outputs for the user |
| `APPROVE_OUTPUT` | Approves a tailored output |
| `SEND_OUTPUT` | Sends the approved output via email |
| `DOWNLOAD_OUTPUT` | Downloads the tailored .docx file |
| `LOGOUT` | Clears stored auth token |

**Notes:**
- Keep scope minimal: read-only DOM access, no auto-posting or auto-connecting on the user's behalf — that avoids ToS risk entirely and keeps this squarely in "reads what the user can already see" territory.
- CSS selectors will need periodic maintenance since LinkedIn's frontend changes; mention this as a known limitation in your writeup — it shows awareness of real-world maintenance tradeoffs.
- This piece alone is a good standalone resume line: *"Built a Chrome extension (Manifest V3) to securely capture user-authorized page content and integrate it with a backend API."*
- The extension achieves full feature parity with the web frontend — users can complete the entire workflow (tailor, review, download, approve, send) without ever opening the web app.

### 3.2 Parser Agent
- Input: raw job post text
- Output: structured JSON — `{role, required_skills, nice_to_have, seniority, keywords}`
- Implementation: LLM call with structured-output prompting (or function calling)

### 3.3 Tailoring Agent
- Base resume stored as **structured JSON**, not raw text:
```json
{
  "summary": "...",
  "skills": ["Python", "FastAPI", "..."],
  "experience": [
    {"title": "...", "company": "...", "bullets": ["...", "..."]}
  ],
  "projects": [...],
  "education": [...]
}
```
- Two-phase:
  1. ATS gap analysis (`ANALYSIS_PROMPT`) — extract the job's priority keywords and
     classify each as already-present / weak / addable / missing.
  2. Rewrite via LLM against the full resume JSON + job requirements.
- **Deterministic corrections** applied regardless of LLM behavior (this is what makes
  weak local models reliable): experience, projects, and skills are re-scored by
  keyword overlap with the job and reordered **most-relevant-first** in code.
- Guardrails in the reviewer flag an echoed summary, unchanged experience order,
  JD-prose dumped into Skills, and projects merged into Experience; a head-to-head
  "tailored vs original" fit score forces a re-tailor when fit doesn't improve.
- Output: same JSON schema, tailored

### 3.4 Renderer
- Use `docxtpl` (Jinja-style templating for Word docs) with a pre-built resume template
- Fill placeholders from tailored JSON → export `.docx`
- Optional: convert to PDF with `docx2pdf` or LibreOffice headless (free)
- Save to: `outputs/resume_{job_id}_{timestamp}.pdf`

#### Is the output ATS-acceptable?

Yes, **if the template is designed for it** — the risk isn't DOCX vs. PDF, it's *layout complexity*. Most ATS parsing failures come from formatting choices, not file format itself. Rules to bake into your `docxtpl` template:

**Do:**
- Single-column layout, top-to-bottom flow (no side-by-side columns)
- Standard section headings as plain text: "Experience", "Education", "Skills" (not stylized graphics or icons as headings)
- Standard fonts (Calibri, Arial, Times New Roman) — avoid decorative/custom fonts
- Bullet points using simple `•` or `-` characters, not custom bullet graphics
- Dates and job titles as plain text, not inside text boxes
- Keep contact info in the main body text, not in a header/footer (some ATS parsers skip headers/footers entirely)

**Avoid:**
- Tables for layout (some ATS parsers read table cells out of order or skip them)
- Text boxes (frequently invisible to parsers entirely)
- Multi-column resumes (common modern-template trap — visually nice, but many ATS read left-to-right across the whole line, scrambling column content)
- Images, icons, graphics, charts, headshots
- Text embedded as an image (obviously unreadable by any parser)

**Output format — DOCX vs PDF:**
- **DOCX is generally the safer default for ATS** — it's structured XML under the hood, easier for parsers to extract cleanly.
- **PDF is fine too**, as long as it's a genuinely text-based PDF (not a scanned image) and avoids the layout traps above — most modern ATS (Workday, Greenhouse, Lever, iCIMS) parse well-structured PDFs fine now.
- Safest move: **generate DOCX as the canonical output**, and optionally also export a PDF version from the same template for human readers — that way you cover both cases without maintaining two designs.

**Testing tip worth mentioning in your writeup:** run your generated resumes through a free ATS-checker tool (e.g., Jobscan's free tier) as a validation step — this is a nice detail to mention since it shows you tested the actual constraint, not just assumed it.

### 3.5 End-to-End Integration Flow (Resume Ingestion ↔ Chrome Extension)

This section ties module 3.0 (Resume Ingestion) and Option B (Chrome Extension, under 3.1) together — they connect through the **user's auth token**, not by passing the resume around directly. The resume lives once in the DB; the extension just triggers the pipeline that reads it.

**Step 1 — One-time setup: Resume Ingestion**
```
User logs into web app
   → Uploads baseline resume (PDF/DOCX)
   → Parser Agent structures it → user reviews/edits → saved as resume_json
   → Stored in DB: resumes(id, user_id, resume_json, version)
   → User issued an auth token (JWT/session token)
```

**Step 2 — One-time setup: Extension**
```
User installs Chrome extension
   → Extension prompts login (opens web app login, or token-paste flow)
   → Auth token stored in chrome.storage.local
   → This token links the extension to that specific user_id
```

**Step 3 — Day-to-day use (the actual interlink moment)**
```
User browses LinkedIn → sees a hiring post
   → Clicks "Tailor Resume" (extension button/popup)
   → content_script.js grabs post text from the page
   → background.js sends:
        POST /api/job-posts
        Headers: Authorization: Bearer <stored auth token>
        Body: { raw_text, url, source_type: "extension" }
   → Backend authenticates token → resolves to user_id
   → Backend looks up that user_id's latest resume_json (from Step 1)
   → Runs full pipeline (Parser → Tailoring Agent → Renderer → Outreach)
        using: job post (just captured) + resume_json (already stored)
   → Result saved to tailored_outputs, linked to that user_id
   → Extension popup displays result inline:
        - Status badge (ready/approved/sent/failed)
        - Draft message preview
        - Download .docx button
        - Approve button
        - Send button (after approval)
   → User can also view history of all past outputs
```

**Key design point:** the extension never handles the resume itself — it's a thin capture tool that only (1) reads post text off the page and (2) sends it plus the auth token to the backend. Everything else (which resume, tailoring, rendering) happens server-side, keyed off `user_id`. The extension now has full feature parity with the web frontend — users can complete the entire workflow without opening the web app.

**Edge cases to handle:**
- **No resume uploaded yet** → extension pre-flight check calls `GET /resumes` before tailoring; if empty, shows "No resume on file. Upload one at the web app first." instead of letting the backend return 400.
- **Token expiry** → extension verifies token via `GET /auth/me` on every popup open; if invalid, clears token and prompts re-login.
- **Multiple resume versions** → if users keep more than one (e.g., different resumes per job type), popup shows a dropdown to pick which one; resolved server-side by `resume_id` instead of defaulting to "latest."
- **User identity mismatch** → extension displays the logged-in email so the user can confirm it matches their frontend account. If they used Google OAuth on the extension but email/password on the frontend (or vice versa), they'll see two different emails and know to use the same login method.

---

### 3.6 Outreach Agent
- Drafts a short message/email referencing the specific job post + candidate fit
- Output stored as draft, not auto-sent

### 3.7 Human-in-the-loop Approval
- UI shows: tailored resume preview (rendered PDF) + draft message
- User edits/approves before anything is sent
- **This is a deliberate design choice** — call it out in your writeup as responsible-AI / product judgment

### 3.8 Sender
- Gmail API (free quota) with OAuth per-user, or SMTP with app password
- Attach the generated PDF to the email
- Log sent status in DB

---

## 4. Orchestration Layer (the "agentic" piece)

Use **LangGraph** (recommended over plain LangChain for this — better for multi-step, stateful agent flows) or **CrewAI**:

- Define each module above as a node/agent with a clear single responsibility
- Orchestrator manages state: `{job_post, parsed_reqs, tailored_resume, draft_message, approval_status}`
- Conditional edges: e.g., if fetch fails → route to "ask user to paste text" node
- This is what lets you legitimately write **"multi-agent system"** and **"LangChain/LangGraph"** on your resume

---

## 5. Data Model (Postgres)

```
users(id, email, oauth_tokens, created_at)
resumes(id, user_id, resume_json, version, created_at)
job_posts(id, user_id, url, raw_text, source_type, created_at)
tailored_outputs(id, job_post_id, resume_id, tailored_json, pdf_path, created_at)
messages(id, tailored_output_id, draft_text, status, sent_at)
```

---

## 6. Tech Stack (all $0)

| Layer | Tool | Cost |
|---|---|---|
| Orchestration | LangGraph or CrewAI | Free |
| LLM | Groq (fast, free tier) or Gemini free tier | Free |
| Backend | FastAPI | Free |
| DB | Postgres via Supabase/Neon free tier | Free |
| Doc generation | docxtpl + LibreOffice headless | Free |
| Frontend | Next.js/React | Free |
| Email | Gmail API free quota | Free |
| Content extraction | trafilatura | Free |
| Backend hosting | Render / Fly.io free tier | Free |
| Frontend hosting | Vercel | Free |
| Containerization | Docker | Free |
| Browser extension (optional) | Chrome Manifest V3 | Free (unpacked, no publish needed) |

---

## 7. Build Order (suggested, ~5–6 weekends)

1. **Week 1** — Resume JSON schema + docxtpl template + renderer (get a hardcoded resume rendering to PDF)
2. **Week 2** — FastAPI backend + Postgres models + auth (basic email/password or OAuth)
3. **Week 3** — Parser + Tailoring agents wired with LangGraph (gap analysis + deterministic relevance reordering + review loop)
4. **Week 4** — Fetcher (trafilatura) + paste fallback + frontend job-post submission form
5. **Week 5** — Outreach agent + Gmail API sending + approval UI
6. **Week 6** — Docker + deploy backend (Render), DB (Supabase), frontend (Vercel); polish + record demo video

---

## 8. What to explicitly write in your resume/portfolio afterward

- "Designed and built a multi-agent system (LangGraph) for automated job-application tailoring, with an agentic self-review loop and deterministic relevance reordering that keeps output reliable even on a small local LLM."
- "Built and deployed a production FastAPI + Postgres backend across multiple free-tier cloud providers (Render, Supabase, Vercel), containerized with Docker."
- "Implemented document generation pipeline (docxtpl) converting structured JSON into formatted Word/PDF resumes."
- "Integrated Gmail API for automated, human-approved outreach messaging."
- "Designed human-in-the-loop approval flow to ensure responsible use of AI-generated outbound communication."

---

## 9. Honest scope note

This project **will not** cover: facial recognition, image/video generation, dedicated voice AI, or TensorFlow specifically. Don't force these in — pick separate, small, honestly-scoped side projects for those if you need them (e.g., a tiny Hugging-Face-diffusion-based headshot generator for image-gen). Stretching one project to cover everything reads as unfocused to reviewers; a few well-scoped projects read as intentional.