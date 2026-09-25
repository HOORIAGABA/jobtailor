# JobTailor — the interface

Next.js App Router, Tailwind v4, no state library. Four screens, which are the
four moments of the product:

| route            | what happens                                           |
| ---------------- | ------------------------------------------------------ |
| `/`              | sign in, upload a resume, pick up whatever is waiting   |
| `/resumes/[id]`  | ★ check the parse, fix it, confirm it                   |
| `/runs/new`      | paste the posting                                       |
| `/runs/[id]`     | ★ progress → the gate → send                            |

## Running it

Two processes, two origins. They are separate on purpose — see
`next.config.mjs` for why there is no rewrite proxy.

```bash
# 1  the API  (from the repo root)
python -m alembic upgrade head
$env:DEV_USER_EMAIL = "you@example.com"        # PowerShell. `set` does not work.
$env:CONFIRM_TOKEN_SECRET = "any-long-random-string"
python -m uvicorn app.api.main:app --reload --port 8000

# 2  the UI
cd web
cp .env.local.example .env.local
npm install
npm run dev
```

Open <http://localhost:3000>. If every screen says "not signed in", the API is
not naming this origin in `CORS_ORIGINS` — the browser is dropping the cookie,
and the symptom looks like an auth bug rather than a CORS one.

## Something to look at

With no runs in the database there is nothing to render. Seed one:

```bash
python -m scripts.seed        # past runs from runs/ into the database
python -m scripts.ui_demo     # puts one of them in front of the gate
```

Neither spends a model call.

## Two decisions worth knowing about

**Nothing is kept in the browser.** No `localStorage`, no token cache. After
approving, the send token is fetched again from
`GET /api/runs/{id}/approved`, so closing the tab does not strand a run in
`approved` with no way to send it.

**The editable fields are seeded exactly once.** The first build re-seeded them
whenever the run was re-read, which silently replaced what the person was typing
with the stored draft — and the approval then failed with a 409, because the
token covered the original text while the form held the edit. The API was right
and the UI was wrong. `seeded` in `app/runs/[id]/page.tsx` is the guard, and the
comment there says why.
