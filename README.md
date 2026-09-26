# JobTailor

[![CI](https://github.com/HOORIAGABA/jobtailor/actions/workflows/ci.yml/badge.svg)](https://github.com/HOORIAGABA/jobtailor/actions/workflows/ci.yml)

**A resume-tailoring system that can prove it didn't make anything up.**

Give it a job posting and your resume. It studies the role, rewrites your resume
for it, drafts the recruiter email — and shows you exactly what it changed, why,
and which line of your own resume each change came from. Nothing is sent until
you have read it and pressed Approve.

The one thing it refuses to do is invent. If the posting wants Kubernetes and
your resume has never mentioned Kubernetes, it reports a gap. It does not
quietly add "exposure to Kubernetes" to your skills. **That refusal is enforced
by rules in Python, not by asking a model nicely** — and the architecture is
built around making it checkable.

## Status

**951 tests, 5 eval cases, 3 architecture contracts — and not one of them needs
an API key.**

Every stage S0.1 → S11 is built: parse, confirm, brief, evidence, plan, write,
validate, apply, diff, outreach, render, approve, send. Google sign-in,
Gmail sending, and a four-screen frontend are in.

| part | state |
| --- | --- |
| Domain model + typed operations | done |
| Engine — normalize, validate, apply, diff, ATS | done |
| Engine — evidence matching, three-grade standing | done |
| Agents — parse, brief, planner, writer, outreach | done |
| Pipeline, persistence, run log | done |
| ★ Approval gate (S9) + sending (S11) | done |
| Google sign-in, Gmail API, encrypted tokens | done |
| Frontend — upload, confirm, gate, send | done |
| Evals + CI + deployment config | done |
| Sent by Gmail against a live account | not yet |

## The one idea

**The model never returns a resume.** It returns typed edit operations —
`RewriteBullet(bullet_id="exp.1.b.2", text="…", rationale="…")` — and
deterministic Python validates each one before anything is applied.

```
tailored = base + validated operations
```

Everything else falls out of that. The diff is free, because an operation
already names its target and its reason. The check is local, because you compare
one new sentence against one old line. Reverting one change is free. The audit
trail is free.

```
resume ─► extract ─► parse ─► normalize ─► ★ you confirm the parse
                                                      │
posting ─► brief ─► evidence ─────────────────────────┤
                                                      ▼
                          plan ─► write ─► ★ validate ─► apply ─► diff
                                                      │
                                     outreach ─► render
                                                      │
                                      ★ you approve ──┴─► send
```

Seven model calls. Five deterministic stages between them. One human gate before
anything leaves the machine.

## How the honesty guarantee works

Claims are not all the same, so they are not checked the same way:

| class | example | the rule |
| --- | --- | --- |
| **A** asserted fact | "reduced latency 40%" | must appear **verbatim** in the original line. No inference. |
| **B** framing | "built" → "owned" | must be entailed by cited evidence; must not escalate seniority or just inject keywords. |
| **C** named capability | "Airflow" | must exist somewhere in your resume. |

`.importlinter` enforces in CI that `engine` and `domain` **cannot import** an
LLM client, `httpx`, or `sqlalchemy`. That is what makes "the guard is
deterministic" a fact a machine checks rather than a claim in a README.

**There is no score anywhere** — no match percentage, no coverage number. A
number that rises when keywords are inserted makes keyword stuffing the optimal
strategy, and the model being measured is the one doing the inserting.

## See it work

```bash
python -m scripts.eval -v      # 5 cases, zero API calls
```

Each case runs a resume and a posting through the real pipeline with a scripted
model, and reports what the validator refused:

```
pass  fabricated_number
      refused    fabricated_numberx1
      applied    flag_gapx1, set_summaryx1
      gaps       Kubernetes
      standing   shown 2 / claimed 0 / absent 2
      render     verified
```

One of the five exists to stop the suite lying: `honest_rewrite` asserts that a
*grounded* rewrite is **accepted**, because the other four are all satisfied by
a validator that refuses everything.

## Running it

```bash
python -m venv .venv && .venv\Scripts\activate      # Windows
pip install -r requirements.txt
cp .env.example .env                                 # then fill it in
python -m scripts.keys                               # the three signing keys
python -m alembic upgrade head

pytest -q
python -m uvicorn app.api.main:app --reload --port 8000
```

And the interface:

```bash
cd web && npm install && npm run dev
```

Open <http://localhost:3000>. `GET http://localhost:8000/` reports whether the
database, the model and sign-in are configured, and names the fix for each one
that is not.

**The model can be local and free.** Ollama supports grammar-constrained
generation, so the schema restricts the sampler token by token and the model
*cannot* emit malformed JSON. See `.env.example` — including the trap where
Ollama silently truncates a prompt longer than its context window.

## Deploying

`DEPLOY.md` has the full runbook. The short version: two Vercel projects — the
UI, and the API as a single Python function — with the database on Neon, all
free, and the public instance serves runs that already happened rather than
pretending it can start new ones. A run takes minutes, no HTTP request survives
that, and the model that makes it cheap is on a laptop the internet cannot reach.

The browser only ever sees the UI's origin; `/api/*` is rewritten to the API. That
is not tidiness — a `SameSite=Lax` cookie is never attached to a cross-site
`fetch`, and `vercel.app` is a public suffix, so two Vercel subdomains are two
sites. `web/next.config.mjs` has the full story.

## Layering

```
api → pipeline → agents → io → db → engine → domain
```

## What is written down

`claude/SYSTEM-GUIDE.md` is the complete walkthrough — every stage, every
connection, and an honest inventory of what is still weak. It includes the bugs
that shaped the design, because most of the non-obvious decisions here exist
because something broke:

- a **Barista entry scored 60 against an ML Engineer's 47**, because term
  matching used substrings and `ml` matched `html`;
- the keyword-stuffing check **rewarded exactly what it existed to catch** —
  appending lowers set similarity, so the longer the keyword list the more
  "changed" the sentence scored;
- the approval token was verified against the *submitted* text, which made an
  editable draft impossible to approve — **the test passed and the product did
  not work**, because the test forged a token with the server secret;
- `POST /api/runs {"job": ".env"}` returned the secrets file.

## License

MIT
