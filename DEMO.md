# Recording the demo: sign in → upload → tailor → approve → send, all in the UI

Everything below runs on your laptop. The public Vercel instance is read-only
and is not part of this — it is where the recording's *result* can be shown
afterwards, because your laptop and Vercel share the same Neon database.

There are three levels. Each one adds a real dependency; do them in order and
record at whichever level you reach.

| level | what is real | what you need |
| --- | --- | --- |
| **A** | the whole product, with a scripted model | nothing — 5 minutes |
| **B** | + a real model on your GPU | Ollama, `llama3.2:3b` |
| **C** | + real Google sign-in and a real Gmail send | an OAuth client in Google Cloud |

Why sign-in and sending "did not work" so far: they were never configured, not
broken. `DEV_USER_EMAIL` in `.env` signs you in as a local development identity
so the product runs with no Google at all, and `MAIL_PROVIDER=console` renders
the email without dispatching it. Both are deliberate defaults — the product
must be usable with no credentials — and both are what level C replaces.
`GET http://localhost:8000/` always tells you which of these is still missing.

---

## Level A — the real UI, a scripted model (no GPU, no keys)

`scripts/fake_model.py` is an OpenAI-compatible server that answers every
model call from the demo script. **Only the model's answers are canned.**
Extraction, the coverage check, evidence matching, the validator, apply, diff,
render and the send path all run for real — so the `fabricated_number`
refusal on the gate is the real guard refusing a real invented statistic.

Four terminals.

```powershell
# 1 — the "model"
cd D:\jobtailor
.\.venv\Scripts\Activate.ps1
python -m scripts.fake_model            # listens on :11434, like Ollama

# 2 — the API
cd D:\jobtailor
.\.venv\Scripts\Activate.ps1
$env:DEV_USER_EMAIL = "demo@jobtailor.example"
$env:LLM_PROVIDER = "ollama"
$env:LLM_BASE_URL = "http://127.0.0.1:11434/v1"
$env:LLM_MODEL = "demo"
$env:MAIL_PROVIDER = "console"
python -m alembic upgrade head
python -m uvicorn app.api.main:app --reload --port 8000

# 3 — the UI
cd D:\jobtailor\web
npm run dev

# 4 — the demo résumé (once)
cd D:\jobtailor
.\.venv\Scripts\Activate.ps1
python -m scripts.demo_files            # writes demo\Priya_Raman_CV.docx and demo\posting.txt
```

Then in the browser at `http://localhost:3000/app`:

1. **Upload** `demo\Priya_Raman_CV.docx`. You land on the confirm screen.
2. **Confirm** — "This is right — confirm". Ids freeze here.
3. **New** → paste `demo\posting.txt` → **Tailor it**.
4. Watch the stages arrive. The gate shows: 2 changes, 1 refused
   (`fabricated_number` — the writer proposed "35%", the résumé has no such
   number), 2 gaps, 1 question, three grades and no score.
5. **Approve**. The token binds your approval to this exact text.
6. **Produce the email**. Console backend: the `.eml` is written, nothing is
   sent. Download it and open it in Outlook to show it is a real message: it
   opens "Dear Hiring Team,", is signed with your name, and carries one
   attachment — the tailored résumé as `<Your_Name>_<Role>.pdf`.

**The cache.** Parses and briefs are cached on content in `.cache\`. Uploading
the same file twice costs no model call — good for a second take. To force a
fresh parse, delete `.cache\`.

---

## Level B — a real model on the RTX 4050

Replace terminal 1 with Ollama. 6 GB of VRAM rules out an 8B model; `3b` fits
with room for the context window:

```powershell
ollama pull llama3.2:3b
$env:OLLAMA_CONTEXT_LENGTH = "8192"       # the trap: the default silently truncates the résumé
ollama serve
```

Terminal 2 changes one line: `$env:LLM_MODEL = "llama3.2:3b"`. Then
`python scripts\check_model.py` before you press record — it makes one call
and tells you whether the model answers in the schema.

What changes on screen: the changes, the refusals and the email are now the
model's own, so they differ every run. **Upload your own résumé.** That is the
demo — the guard working on a document it has never seen. Expect a refusal or
two; if there are none, say so on camera: "nothing was invented this time, and
the tool says so rather than inventing something to show".

A 3B model will sometimes produce a plan the validator refuses entirely. That
is the product working, not failing — but for a recording, run it twice first
and record the take you like.

The two-model split (`SMART_LLM_*` in `.env.example`) sends the three
judgment calls to a hosted model and keeps parsing local. Better output, one
free-tier key. Optional.

---

## Level C — real Google sign-in and a real Gmail send

### C1. One-time setup in Google Cloud (about ten minutes)

1. <https://console.cloud.google.com> → create a project, e.g. `jobtailor`.
2. **APIs & Services → Library** → enable **Gmail API**.
3. **APIs & Services → OAuth consent screen**
   - User type **External**, app name `JobTailor`, your email as support and
     developer contact.
   - **Scopes**: add exactly these three plus one —
     `openid`, `.../auth/userinfo.email`, `.../auth/userinfo.profile`, and
     `https://www.googleapis.com/auth/gmail.send`.
     **Not one more Gmail scope.** `gmail.send` is Google's *sensitive* tier;
     every other Gmail scope is *restricted* and would put the app into a
     CASA security assessment.
   - **Test users**: add the Gmail address you will sign in with. The app
     stays in *Testing*; nobody else can sign in, which is what you want.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**
   - Type **Web application**.
   - Authorised redirect URI, character for character:
     `http://localhost:8000/api/auth/google/callback`
   - Copy the client id and secret.

### C2. `.env`

```powershell
python -m scripts.keys                  # prints three fresh secrets
```

Put these in `D:\jobtailor\.env` (the file stays uncommitted):

```
GOOGLE_CLIENT_ID=…apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=…
GOOGLE_REDIRECT_URI=http://localhost:8000/api/auth/google/callback
FRONTEND_URL=http://localhost:3000
JWT_SECRET=…            from scripts.keys
FERNET_KEY=…            from scripts.keys
CONFIRM_TOKEN_SECRET=…  from scripts.keys
MAIL_PROVIDER=gmail_api
```

And **remove `DEV_USER_EMAIL`** — from `.env` and from the terminal
(`Remove-Item Env:DEV_USER_EMAIL`). While it is set, the API signs every
request in as the dev identity and the Google button is pointless. A real
session cookie takes precedence over it, but a recording should not depend
on precedence.

Restart the API. `GET http://localhost:8000/` must now say `google_sign_in:
ready` and `identity: Google sign-in`.

### C3. The flow on camera

1. `http://localhost:3000` — the landing page. **Sign in with Google.** Google
   asks for identity only (three scopes, no Gmail). Back on `/app`, signed in,
   with an empty dashboard — this is your account, not the demo's.
2. Upload your résumé → confirm → new application → paste a real posting →
   Tailor it → the gate.
3. **Approve.** The send panel appears and says *Gmail*. Press **Connect
   Gmail** (top bar or the panel). This is the *second* consent screen and it
   asks for exactly one thing: *Send email on your behalf*. That is
   incremental consent — the app never asked for it at sign-in.
4. **Send it.** The audit row is written before Gmail is called. The screen
   turns to *Sent* with Gmail's message id, and the email is in your Sent
   folder with the tailored résumé attached as `<Your_Name>_<Role>.pdf`.
   (The `.docx` stays in the run's Files list for you — one attachment reads
   better in a recruiter's inbox than two copies of the same résumé.)
5. Press **Send it** again if you like: `409 already sent at …`. Once.

Send it to yourself. A recruiter address in a recording is a recruiter
address in a recording forever.

### Known Google-side behaviour

- **Testing mode expires refresh tokens after seven days.** "Connect Gmail"
  has to be repeated weekly. Google's rule; say it before someone asks.
- The consent screen shows an *unverified app* warning in Testing. Click
  *Continue*. Verification is only needed to publish.
- `redirect_uri_mismatch` means the URI in the console and
  `GOOGLE_REDIRECT_URI` differ — usually a trailing slash or `127.0.0.1`
  versus `localhost`.

---

## The recording, screen by screen

Ninety seconds is enough. What to say over each screen:

| screen | the line |
| --- | --- |
| landing | "Résumé tailoring that can prove it didn't invent anything. The model never returns a résumé — it returns typed edits, and deterministic Python checks each one against the original." |
| confirm | "The first human gate. Every citation downstream points at these lines, so a parse error here would be invisible later. Unplaced lines are shown verbatim." |
| tailoring | "Six model calls; everything between them is deterministic. The validator can't reach the network — the import linter fails the build if it could." |
| gate | "Every change with its reason and the line it came from. This one was refused: the model proposed 35%, the résumé has no such number. And there's no score — a score becomes a target, and keyword stuffing is how you raise it." |
| approve → send | "Approval is an HMAC over this exact text. Edit one character and it's void. Sending needs a second one. The audit row is written *before* Gmail is called." |
| sent | "Once. Press it again and it says already sent." |

## What is verified

The level-A flow was driven end to end through the real UI in a browser —
upload, confirm, run, gate, approve, send — against the API and the scripted
model, with a screenshot of every screen. It found one bug that no test could:
every primary button rendered as a black pill with no label (an unlayered
`button { color: inherit }` beat Tailwind's utility layer). Fixed in
`web/app/globals.css`.

Level C cannot be driven without a Google account and has not been. The
pieces have unit tests: the two consent flows, the scope list, encrypted
storage, revocation, the callback's state check, and the read-only rules.
