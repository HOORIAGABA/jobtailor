# Deploying JobTailor

Three pieces, all free, and one of them stays on your desk. That last part is
forced by a single fact: **the model that makes this cheap runs on a laptop, and
nothing on the internet can reach it.**

```
  Vercel — project 1              Vercel — project 2         your laptop
  ──────────────────              ──────────────────         ───────────
  web/  (Next.js)                 app/  (FastAPI)            Ollama
  the only origin the    ──────►  one Python function        the full app, the
  browser ever sees      /api/*   read-only: serves          only place a run
                                  stored runs, refuses       can start
                                  to start new ones
                                          │
                                          ▼
                                    Neon (Postgres, free)
                                          ▲
                                          └──────────────────────┘
                                          the laptop writes here too
```

The browser talks to **one** origin. `/api/*` is rewritten to the API project by
`web/next.config.mjs`; nothing in the UI ever names the API's hostname. That is
not tidiness, it is the only arrangement in which anyone can be signed in — see
[One origin, and why](#one-origin-and-why).

## What the public instance honestly is

It **serves runs that already happened and cannot start new ones.** Not a
limitation dressed up:

- a run takes minutes, and no HTTP request survives that;
- the model is on a machine the internet cannot reach;
- a hosted key would spend one free-tier quota per visitor.

What a public URL *can* offer is the product with the waiting removed — the diff,
the gaps, the draft, the rendered files, the approval gate — all real, none of it
costing a model call. `GET /api/capabilities` says which mode an instance is in,
so the UI hides what it cannot do rather than guessing from the hostname.

---

## 1. The database

**Neon** (neon.tech) — 3 GiB, no expiry, scales to zero, no card.

Create the project in **AWS us-east-1**. Vercel Functions run in `iad1` by
default, which is us-east-1; a Neon project in Frankfurt means every query pays
an Atlantic crossing twice, and on a page that makes four of them that is the
whole response time.

Neon gives you two connection strings. **Take the pooled one** — the host has
`-pooler` in it:

```
postgresql://user:pass@ep-thing-123-pooler.us-east-1.aws.neon.tech/neondb?sslmode=require
                                  ^^^^^^^
```

The direct string works too, right up until traffic arrives. A Vercel Function
scales by starting more instances, each with its own connection pool, and it is
the database that runs out of connections first. The pooled endpoint is PgBouncer
in front of Postgres and exists for exactly this shape of client.
`app/db/session.py` bounds the pool per instance (5 + 5) and recycles at 300s so
it drops a connection before PgBouncer does; the comment there explains why
`prepare_threshold` is *not* set, which is the one piece of folklore you will be
told to apply.

**Paste it exactly as Neon gives it to you.** `db/session.py` names the driver
for you — `postgres://` and `postgresql://` both become
`postgresql+psycopg://`, and an explicitly named driver is left alone.

This paragraph used to say "change the scheme by hand", and that instruction
existed because the code only handled `postgres://` (the Heroku form) and not
`postgresql://` (what Neon, Supabase and Render actually hand out). SQLAlchemy's
default driver for an unqualified `postgresql://` is **psycopg2**, which this
project does not install, so the failure was:

```
File ".../sqlalchemy/dialects/postgresql/psycopg2.py", line 690
ModuleNotFoundError: No module named 'psycopg2'
```

Nothing in that says "your URL does not name a driver" — it reads as a missing
package, and the obvious response is to `pip install psycopg2`, which installs a
second unwanted driver and makes the symptom go away for the wrong reason. Fixed
in the code; `tests/test_db.py` covers both bare forms and asserts an explicit
driver is never overridden.

## 2. Secrets

```bash
python -m scripts.keys
```

Three values, none with a default in source, each refusing at the point of use
rather than falling back to something insecure:

| variable | signs / encrypts | if you change it |
| --- | --- | --- |
| `JWT_SECRET` | the session cookie | everyone is signed out. Harmless. |
| `CONFIRM_TOKEN_SECRET` | approvals, and the send that follows | open previews must be reloaded. Harmless. |
| `FERNET_KEY` | Gmail refresh tokens at rest | **every stored token becomes unreadable and every user must reconnect Gmail.** |

## 3. Migrations, from your laptop

There is no start command on a serverless host, so nothing runs `alembic upgrade
head` for you — and that is the better arrangement. A migration at deploy time
means a schema change can fail a build, and a migration at import time means it
races every other instance starting at the same moment.

Run it once, pointed at Neon, from the machine you are reading this on:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://…-pooler…/neondb?sslmode=require"
python -m alembic upgrade head
python -m alembic current          # should print the head revision
```

**The shell variable, not `.env`, and this is a real choice.** Both work —
`db/session.py` reads the process environment first and falls back to `.env`, and
there is a test asserting `Settings` and the engine agree, because for a while
they did not and `DATABASE_URL` in `.env` migrated the local SQLite file while
reporting success.

But a production URL sitting in `.env` is *persistent*. It points every later
command at Neon — `scripts.seed`, a local `uvicorn`, the next
`alembic upgrade head` you meant for your laptop — and none of them will mention
it. `$env:DATABASE_URL` lives in one PowerShell window and dies with it, which is
the right lifetime for "act on production once". If you do put it in `.env`, take
it out again afterwards.

Repeat it after any deploy that includes a new file in `migrations/versions/`.
`GET /` on the deployed API reports `database: ready` or names the missing
tables, so you never have to guess whether you remembered.

## 4. The API, on Vercel

A **second Vercel project** from the same repository, with the root directory
left at the repository root.

Vercel's Python runtime looks for a `FastAPI` instance named `app` at `app.py`,
`index.py`, `server.py`, `main.py`, `wsgi.py` or `asgi.py` — in the root, or
inside `src/` or `app/`. Ours is at `app/api/main.py`, one level deeper than any
of those, so `pyproject.toml` says where it is:

```toml
[tool.vercel]
entrypoint = "app.api.main:app"
```

`vercel.json` in the root caps the function at 30 seconds. Hobby's default and
maximum are both 300, and 300 seconds of a stuck request is 300 seconds of
provisioned memory you are waiting on; a read-only instance reads a row and
returns bytes, so anything past 30 is stuck rather than slow.
`tests/test_deploy.py` checks that the entrypoint still imports and that the
`vercel.json` key still matches the file it resolves to — a rename otherwise has
its first symptom on a deployed URL.

Environment variables, all in **Production** (and Preview, if you want preview
deployments to work at all):

| variable | value |
| --- | --- |
| `ENVIRONMENT` | `production` |
| `JOBTAILOR_READ_ONLY` | `true` |
| `DEMO_USER_EMAIL` | `demo@jobtailor.example` |
| `DATABASE_URL` | the pooled Neon string, `postgresql+psycopg://…` |
| `JWT_SECRET` | from `scripts.keys` |
| `FERNET_KEY` | from `scripts.keys` |
| `CONFIRM_TOKEN_SECRET` | from `scripts.keys` |
| `MAIL_PROVIDER` | `console` |
| `CORS_ORIGINS` | the UI's origin, e.g. `https://jobtailor.vercel.app` |
| `FRONTEND_URL` | the same |
| `GOOGLE_CLIENT_ID` | see §6 |
| `GOOGLE_CLIENT_SECRET` | see §6 |
| `GOOGLE_REDIRECT_URI` | `https://<the UI origin>/api/auth/google/callback` |

`DEV_USER_EMAIL` is deliberately absent. It is an authentication bypass — every
unauthenticated request becomes that user — and it is *ignored* when
`ENVIRONMENT=production`, and `config.check` complains if it is set there. Three
layers, because one of them will be the one you forget.

`DEMO_USER_EMAIL` gives an anonymous visitor the seeded demo account, so the
public URL shows the product instead of an empty list. It resolves **only** on a
read-only instance, and a read-only instance refuses every unsafe HTTP method, so
the worst a visitor can do with it is read. On a writable instance it does
nothing at all — which is what stops it being `DEV_USER_EMAIL` in a disguise.

**One function, and the rate limiter knows it.** `api/limits.py` counts in
process. Vercel scales by adding instances, so each instance enforces its own
window and the effective limit is the written one times the number of instances.
On a read-only deployment the limiter is belt-and-braces (every mutating method
is already 403) and this does not matter. If this instance is ever made writable,
the counter has to move into the database first.

## 5. The UI, on Vercel

The **first** project: import the repo, set the root directory to `web/`.

| variable | value |
| --- | --- |
| `NEXT_PUBLIC_API_URL` | `/` |
| `API_ORIGIN` | the API project's URL, e.g. `https://jobtailor-api.vercel.app` |

`NEXT_PUBLIC_API_URL=/` means "this origin": `lib/api.ts` normalises it to an
empty prefix and requests `/api/…` relative. `API_ORIGIN` is read at build time
by `next.config.mjs` to write the rewrite, and is never shipped to the browser.

`web/vercel.json` sets the security headers.

### The order that avoids an hour of confusion

Each project needs the other's URL, so the first deploy of each is wrong and
that is expected:

1. Deploy the UI. Note its URL.
2. Deploy the API. Note its URL.
3. Set `API_ORIGIN` on the UI; set `CORS_ORIGINS`, `FRONTEND_URL` and
   `GOOGLE_REDIRECT_URI` on the API.
4. Redeploy both. A Vercel environment variable is read at build time, so saving
   it is not enough — nothing changes until a new build.

### One origin, and why

`web/next.config.mjs` used to argue *against* a rewrite, and the argument was
wrong in a way worth keeping written down.

CORS and `SameSite` answer different questions. CORS decides whether a response
may be *read* by a page from another origin, and `allow_credentials` is the
server saying "I will accept a cookie". `SameSite` decides whether the browser
*attaches* the cookie at all. The session cookie is `SameSite=Lax`, and Lax means:
send this on a top-level navigation, never on a cross-site `fetch`.

`jobtailor.vercel.app` and `jobtailor-api.vercel.app` look like one site and are
not — `vercel.app` is on the Public Suffix List, so they are separate registrable
domains and every `fetch` between them is cross-site. Sign-in would have
completed, the cookie would have been stored, and every request after it would
have answered 401 with the cookie sitting in the jar unsent. Nothing local would
have caught it: `localhost:3000` and `localhost:8000` differ only by port, and
`SameSite` ignores ports.

`SameSite=None; Secure` is the usual answer and is not one here — that is a
third-party cookie, which Safari's ITP and Firefox's Total Cookie Protection block
outright. It turns "broken" into "broken in some browsers".

So the browser is given one origin, the UI proxies `/api/*`, and every cookie is
first-party — including the one Google's callback sets, which is why
`GOOGLE_REDIRECT_URI` names the **UI** host. `Set-Cookie` is attributed to the
origin the browser asked, not the one that answered behind the proxy.

The old objection to a rewrite — "a proxy hop through Vercel on a free plan, in
front of a service that can take eight minutes to answer" — expired when the API
moved here. The service that takes eight minutes is the one with the model, and
it is never deployed.

## 6. Google sign-in

In the Google Cloud console, add the **production** redirect URI to the same
OAuth client, character for character:

```
https://jobtailor.vercel.app/api/auth/google/callback
```

The UI host, not the API host — see above. A trailing slash is a
`redirect_uri_mismatch`.

While the app is in **Testing**, Google expires refresh tokens after **seven
days**, so "connect Gmail" has to be repeated weekly. That is Google's rule, not
a defect here — but it will confuse anyone you demo this to, so say it first.

`gmail.send` is Google's *sensitive* tier and needs a review to publish. Every
other Gmail scope is *restricted* and needs a CASA assessment — roughly six
weeks, renewed annually. **Do not add a second Gmail scope** to make something
work; `app/io/google.py` has the full reasoning.

On a read-only instance signing in is close to pointless: it swaps the seeded
demo account for your own empty one. It is left mounted because a UI that has to
discover which endpoints exist is a UI that guesses.

## 7. Seeding something to look at

A read-only instance with an empty database shows empty lists. Point the seeder
at Neon once, from your laptop:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://…-pooler…/neondb?sslmode=require"
python -m scripts.demo_seed
```

It spends no model call, and the run it writes is a **real** one: a scripted
client supplies the model's four answers, and the evidence matching, the
validation, the application, the diff, the ATS pass and the render are all
genuinely computed. So the refusal visible on the gate screen is the guarantee
working, not a mock-up of it — the writer really does propose "cut processing time
by 35%", that number really is absent from the resume, and S5 really does refuse
it.

**Use `demo_seed` and not `scripts.seed` or `scripts.ui_demo` for anything
public.** Those two import from your local `runs\` folder, which holds your real
CV: name, address, phone number, employment history. `demo_seed` invents a
candidate. Nobody should have to choose between showing their work and publishing
their personal data.

## 8. Running the full app locally

The half that can actually tailor:

```powershell
ollama serve                       # OLLAMA_CONTEXT_LENGTH=8192, see .env.example
python -m alembic upgrade head
$env:DEV_USER_EMAIL = "you@example.com"     # PowerShell. `set` does not work.
python -m uvicorn app.api.main:app --reload --port 8000

cd web; npm run dev
```

With `API_ORIGIN` and `NEXT_PUBLIC_API_URL` unset — which is what a fresh
checkout does — the UI talks to `http://localhost:8000` directly, as it always
has. Set `API_ORIGIN=http://localhost:8000` and `NEXT_PUBLIC_API_URL=/` in
`web/.env.local` if you would rather develop through the same proxy the
deployment uses.

---

## Limits that will actually bite

| limit | number | what it means here |
| --- | --- | --- |
| function bundle | 500 MB uncompressed (Python) | ours is around 150 MB installed. `pymupdf` alone is 120 MB and a read-only instance never opens a PDF, so there is room to trim if that ever changes. |
| request/response body | 4.5 MB | the resume upload cap is 10 MB. Read-only refuses uploads anyway, but a **writable** Vercel deployment could not accept a large resume — that is a reason this design keeps uploads on the laptop, not an oversight. |
| duration | 300s on Hobby, capped to 30 here | fine for reads. Not fine for a run, which is the other reason runs stay local. |
| region | one, `iad1` by default | match the Neon region to it. |
| Hobby plan | non-commercial use only | fine for a portfolio; not fine the day this has customers. |

## Why not Render

`render.yaml` used to live here, and the Blueprint flow asks for a credit card
before it will deploy a service the config already pins to `plan: free` —
*"Please enter your payment information to select an instance type with higher
limits"*. Render's own free-tier documentation never mentions a card requirement,
and there are open, unanswered threads on their forum about exactly this.

Moving to Vercel was better on the merits anyway, once the reasoning behind
"Vercel cannot host the API" was checked instead of repeated. That claim rested
on a run taking minutes — but the public instance is read-only and never starts a
run. It reads rows from Postgres and returns bytes, which is what a function is
good at. Cold start goes from 30–50 seconds to about one, both halves live on one
platform, there is no 15-minute spin-down, and the blueprint config that nobody
could deploy is gone rather than left to rot.

## Before you make the URL public

- [ ] `ENVIRONMENT=production` is set (it turns on `Secure` cookies and the
      `DEV_USER_EMAIL` refusal)
- [ ] `DATABASE_URL` uses the **`-pooler`** Neon host
- [ ] `alembic current` against Neon prints the head revision
- [ ] `CORS_ORIGINS` names the UI origin, not `*` (`config.check` refuses `*`,
      because a browser will not send a cookie to a wildcard origin and sign-in
      would break silently while the network tab showed 200s)
- [ ] `API_ORIGIN` is set on the UI project **and** it has been rebuilt since
- [ ] `GOOGLE_REDIRECT_URI` names the UI host, ends in
      `/api/auth/google/callback`, and matches the OAuth client exactly
- [ ] `MAIL_PROVIDER=console` unless you have verified the OAuth client and
      genuinely mean to send
- [ ] `GET /` reports `database: ready` and `google_sign_in: ready`
- [ ] `GET /api/capabilities` reports `read_only: true`
- [ ] a mutating request is refused — `curl -X POST https://<ui>/api/runs`
      answers 403
- [ ] the three secrets came from `scripts.keys`, not from `.env.example`
