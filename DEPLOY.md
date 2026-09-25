# Deploying JobTailor

Two halves, two hosts, both free. The split is forced by one fact: **the model
that makes this cheap runs on a laptop, and no serverless function can reach
it.**

```
  Vercel                    Render                    your laptop
  ──────                    ──────                    ───────────
  web/  (Next.js)  ──────►  app/  (FastAPI)           Ollama
  static, instant           read-only: serves         the full app, able to
  free forever              stored runs, refuses      start runs
                            to start new ones
                                  │
                                  ▼
                            Neon / Supabase
                            Postgres, free
```

## What the public instance honestly is

It **serves runs that already happened and cannot start new ones**. That is not
a limitation dressed up:

- a run takes minutes, and no HTTP request survives that;
- the model is on a machine the internet cannot reach;
- a hosted key would spend one free-tier quota per visitor.

What a public URL *can* offer is the product with the waiting removed — the
diff, the gaps, the draft, the rendered files, the approval gate — all real,
none of it costing a model call. `GET /api/capabilities` says which mode an
instance is in, so the UI hides what it cannot do rather than guessing from the
hostname.

---

## 1. The database

**Neon** (neon.tech) — 3 GiB, no expiry, scales to zero. Create a project, copy
the connection string, and change the scheme:

```
postgres://…            ← what Neon gives you
postgresql+psycopg://…  ← what SQLAlchemy 2 needs
```

(`db/session.py` rewrites the bare `postgres://` form for you, but the explicit
one is clearer in a dashboard.)

Supabase's free 500 MB works too. Artifacts are stored as bytes in the database
— a `.docx` plus a `.pdf` is roughly 50 KB per run, so 500 MB is about ten
thousand applications.

## 2. Secrets

```bash
python -m scripts.keys
```

Three values, none of which has a default in source, each of which refuses at
the point of use rather than falling back:

| variable | signs / encrypts | if you change it |
| --- | --- | --- |
| `JWT_SECRET` | the session cookie | everyone is signed out. Harmless. |
| `CONFIRM_TOKEN_SECRET` | approvals, and the send that follows | open previews must be reloaded. Harmless. |
| `FERNET_KEY` | Gmail refresh tokens at rest | **every stored token becomes unreadable and every user must reconnect Gmail.** |

## 3. The API, on Render

`render.yaml` is in the repo root — connect the repo as a Blueprint and Render
reads it. It sets `ENVIRONMENT=production` and `JOBTAILOR_READ_ONLY=true`, and
asks for the `sync: false` values once.

Set `CORS_ORIGINS` and `FRONTEND_URL` to your Vercel URL **before** the first
sign-in attempt. A wildcard cannot work: browsers reject a credentialed
response whose `Access-Control-Allow-Origin` is `*`, so `*` does not open a
hole — it silently breaks sign-in, and every screen reports "not signed in"
while the network tab shows 200s. `config.check` refuses it for that reason.

What the free tier actually does:

- 750 instance-hours a month — one service running continuously;
- **spins down after 15 minutes idle**, and the next request waits 30–50
  seconds. Survivable for a portfolio link, and the reason the UI says what it
  is waiting for instead of showing a spinner;
- the filesystem is ephemeral. Nothing here writes a file it expects to find
  later — artifacts are bytes in Postgres, which is exactly why `io/render`
  returns bytes rather than paths. v1 stored absolute paths, the host
  restarted, and the rows pointed at files that no longer existed.

**One worker, deliberately.** The rate limiter counts in-process, so two
workers would mean two counters and an effective limit of double what is
written down.

**Vercel cannot host the API.** It runs serverless functions, not a process;
FastAPI with a background thread that outlives a request has nowhere to live
there, and neither does a run that takes minutes.

## 4. The UI, on Vercel

Import the repo, set the root directory to `web/`, and add one variable:

```
NEXT_PUBLIC_API_URL=https://jobtailor-api.onrender.com
```

`vercel.json` sets the security headers. There is no rewrite proxy, on purpose
— see `web/next.config.mjs`: a rewrite would work in development and quietly
become a proxy hop through Vercel's free plan in front of a service that can
take minutes to answer.

## 5. Google sign-in

In the Google Cloud console, add the **production** redirect URI to the same
OAuth client, character for character:

```
https://jobtailor-api.onrender.com/api/auth/google/callback
```

A trailing slash is a `redirect_uri_mismatch`.

While the app is in **Testing**, Google expires refresh tokens after **seven
days**, so "connect Gmail" has to be repeated weekly. That is Google's rule,
not a defect here — but it will confuse anyone you demo this to, so say it.

`gmail.send` is Google's *sensitive* tier and needs a review to publish. Every
other Gmail scope is *restricted* and needs a CASA assessment — roughly six
weeks, renewed annually. **Do not add a second Gmail scope** to make something
work; see `app/io/google.py`.

## 6. Seeding something to look at

A read-only instance with an empty database shows empty lists. Point the seeder
at the production URL once:

```bash
DATABASE_URL="postgresql+psycopg://…" python -m scripts.seed
DATABASE_URL="postgresql+psycopg://…" python -m scripts.ui_demo
```

Neither spends a model call.

## 7. Running the full app locally

The half that can actually tailor:

```bash
ollama serve                       # OLLAMA_CONTEXT_LENGTH=8192, see .env.example
python -m alembic upgrade head
$env:DEV_USER_EMAIL = "you@example.com"     # PowerShell. `set` does not work.
python -m uvicorn app.api.main:app --reload --port 8000

cd web && npm run dev
```

`DEV_USER_EMAIL` is an authentication bypass — every unauthenticated request
becomes that user. It is what makes local work possible and it must never
survive a deploy, so it is **ignored when `ENVIRONMENT=production`** and
`config.check` complains if it is set there. It is deliberately absent from
`render.yaml`.

---

## Before you make the URL public

- [ ] `ENVIRONMENT=production` is set (it turns on `Secure` cookies and the
      `DEV_USER_EMAIL` refusal)
- [ ] `CORS_ORIGINS` names the Vercel origin, not `*`
- [ ] `MAIL_PROVIDER=console` unless you have verified the OAuth client and
      genuinely mean to send
- [ ] `GET /` reports `database: ready` and `google_sign_in: ready`
- [ ] `GET /api/capabilities` reports `read_only: true`
- [ ] the three secrets came from `scripts.keys`, not from `.env.example`
