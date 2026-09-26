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

Two processes. Locally they are two origins that happen to be same-*site*
(`localhost` differing only by port), so the session cookie works. **Deployed,
they are one origin** — `next.config.mjs` proxies `/api/*`, and the long comment
at the top of that file explains why that is not optional.

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

## `npm audit` reports two findings, and they are not ignored

**`next` is pinned to the newest 15.5.x**, not to a caret range, and not left at
whatever was current when this was written. `15.5.4` shipped with
[CVE-2025-66478](https://nextjs.org/blog/CVE-2025-66478) — a CVSS 10.0 remote
code execution in the React Server Components protocol, which affects exactly
this configuration (15.x, App Router). It is fixed at 15.5.7, and a further two
criticals and a run of high-severity middleware and SSRF issues are fixed up to
15.5.24, so the pin is the latest of the line rather than the minimum that clears
one advisory.

What remains after that is **two findings against the `postcss` that ships inside
`next`**, and `npm audit` offers exactly one remedy for them: `next@16`, a major
upgrade. They are left, deliberately:

- both require **processing attacker-controlled CSS** — a `sourceMappingURL` in a
  comment pointing at a file it should not read. The CSS here is
  `app/globals.css` and Tailwind's output. Nothing untrusted reaches PostCSS.
- it is a **build-time** dependency. It does not exist in the deployed bundle.

`npm audit` reports on the shape of the dependency tree, not on whether a path to
the vulnerability exists in this application. The distinction is the whole reason
`npm audit fix --force` is not the answer: it would have replaced a
non-exploitable build-time finding with an unreviewed major framework upgrade,
the day before a first deploy.

Moving to Next 16 is a reasonable follow-up. It is not a security fix for this
app.

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
