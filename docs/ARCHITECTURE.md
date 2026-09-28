# Architecture

How JobTailor is put together, and what each piece is responsible for. If you
want to know *why* it is built this way, [`DECISIONS.md`](DECISIONS.md) covers
that — including the bugs that produced each decision.

---

## Layers

```
app/api        1,950 lines   HTTP. FastAPI routes, auth, rate limits.
app/pipeline   1,692 lines   Orchestration. Stage order, the gate, sending.
app/agents     1,905 lines   Model-facing code. One module per prompt.
app/io         2,707 lines   The outside world: LLM client, files, Google, render.
app/db           704 lines   SQLAlchemy models and the session.
app/engine     3,447 lines   ★ All deterministic logic.
app/domain       925 lines   Types, ids, errors, state machines. No dependencies.
```

Dependencies point **downward only**, and this is machine-checked. `.importlinter`
declares three contracts; CI fails the build if any is broken.

### Contract 1 — the layer order

`api → pipeline → agents → io → db → engine → domain`

`agents` sits **above** `io` because an agent uses the LLM client, not the other
way round. `db` sits between `io` and `engine`: it is I/O, so it is below the
stages that use it, and above `engine` because nothing deterministic may read a
row.

### Contract 2 — the one that carries the product's claim

`app.engine` and `app.domain` may not import `httpx`, `requests`, `google`,
`langchain_core`, `langgraph`, `sqlalchemy`, `app.io`, `app.db`, `app.agents` or
`app.pipeline`.

Read what that means: **the validator physically cannot call a model, reach the
network, or read the database.** If a future contributor tries to make the guard
"smarter" by asking an LLM, the build goes red. This is how a design intention
becomes an executable constraint.

### Contract 3

`app.db` may not import the stages whose output it stores, so business rules
cannot migrate into the persistence layer.

---

## The data model

```
tailored = base + validated operations
```

The model returns a list of typed operations, defined as Pydantic models in
`app/domain/ops.py`:

| operation | effect |
| --- | --- |
| `set_summary` | replace the summary paragraph |
| `set_skills` | replace the skills section, in labelled groups |
| `reorder_items` | put the relevant roles higher |
| `reorder_bullets` | put the relevant bullets higher within a role |
| `promote_item` | move an entry to a more prominent section |
| `rewrite_bullet` | reword one bullet, by id, with citations |
| `drop_bullet` | remove one bullet, by id |
| `flag_gap` | record a requirement the résumé cannot support |
| `ask_user` | ask something the model cannot decide |

`flag_gap` and `ask_user` exist so the model has a legitimate way to say "I cannot
do this honestly". Without them, every answer is an edit.

### Why not "give me a better résumé"

Because you cannot check it. Two blobs of prose give you no way to ask which parts
changed, why, or whether a number in the new one existed in the old one.
Attribution, local checking, cheap reverting and the audit trail all fall out of
the operation list; none of them is available over free text.

---

## The pipeline

Stage ids in code are `S0`–`S11`. The UI renders them as plain English — a
waiting person is told "checking every claim", not "S5".

### S0 — ingest

`app/io/extract.py` · `app/agents/parse.py` · `app/engine/parse_check.py` ·
`app/engine/normalize.py`

1. **Extract.** PyMuPDF for PDFs, python-docx for Word. Column detection is ours
   (`app/engine/layout.py`), built from the box coordinates PyMuPDF already
   returns, because every off-the-shelf extractor interleaved the columns of the
   two-column test fixture.
2. **Parse** — a model call, because résumé layout is genuinely unbounded.
3. **Coverage check** — *no model*. Compares the parse against the source text and
   produces two lists: lines in your file that did not reach the parse, and text in
   the parse that is **not in your file**. The second is a hallucination detector on
   the parse step itself.
4. **Normalize and confirm.** Ids are assigned and the parse is shown to the user.
   **On confirm the ids freeze**, and every citation for the rest of the run points
   at them.

The human is in the loop *here* because a parse is a hypothesis, and a mistake at
this step becomes invisible at the step where it matters. Unplaced lines are shown
verbatim: a specific piece of text gets read, where a "looks good?" button over a
rendered document gets clicked.

### S1 — understand the job

`app/engine/posting.py` strips page furniture deterministically; then
`app/agents/job_brief.py` produces a structured brief — role, company,
requirements. Cached on the posting text, so a second résumé against the same job
costs nothing here.

### S2 — link evidence · *no model*

`app/engine/evidence.py` matches each requirement against the résumé's own lines
by exact and alias matching, then grades every requirement:

- **demonstrated** — evidence exists
- **declared only** — you claim it, you do not show it
- **not found**

`S2b` is an optional model call suggesting links for requirements exact matching
could not place. It produces *hints only*, never a strong link, and is skipped
entirely when nothing is unmatched.

### S3 — plan · S4 — write

Split deliberately. `app/agents/planner.py` decides *which* operations to attempt;
`app/agents/writer.py` writes prose for the targeted lines only. The two have
different failure modes, and separating them lets the validator reject the writing
without discarding the plan.

### S5 — the honesty guard · *no model*

`app/engine/validator.py`. Each operation is classified and checked:

- **Class A — asserted facts.** Numbers, employers, titles, dates. Must appear
  **verbatim** in the source.
- **Class B — framing.** Rewording is allowed, but must be entailed by the
  original and must not escalate seniority or inject keywords.
- **Class C — named capabilities.** A skill must exist in the grounding corpus.

Thirteen reject codes: `fabricated_number`, `unsupported_entity`,
`seniority_escalation`, `keyword_stuffing`, `term_density`, `not_a_permutation`,
`unknown_id`, `missing_citation`, `too_many_promotions`, `would_empty_item`,
`skills_dropped`, `unlabelled_group`, `duplicate_skill`.

Two are structural rather than semantic, which shows the shape of the thinking:
`not_a_permutation` catches a reorder that adds or loses an item (a reorder must
be a permutation, and that is checkable), and `would_empty_item` catches a set of
drops that would leave a role with no bullets.

### S6 — apply · S7 — diff · *no model*

`app/engine/apply.py` applies the survivors; `app/engine/diff.py` produces what
the gate renders.

### S8 — outreach

`app/agents/outreach.py` drafts the email. Claims in the email are held to the
same standard as claims in the résumé.

### S10 — render

`app/io/render.py` — `.docx` via python-docx, `.pdf` via fpdf2, then **read back
and verified**: the rendered files are re-opened and checked to contain what they
should.

Artifacts are stored as **bytes in Postgres**, not as paths. v1 stored absolute
paths, the host restarted with a clean disk, and every row pointed at a file that
no longer existed.

### S9 — the gate

`app/pipeline/gate.py`. Preview → decide. Approve or reject over the whole plan,
nothing in between. Rejecting blocks sending and **keeps the files**: the likeliest
real rejection is a good résumé with a clumsy email, and a gate that destroyed the
résumé to punish the email would make the honest answer the expensive one.

### S11 — send

`app/pipeline/send.py`. Five ordered steps, and the order is load-bearing — the
idempotency check comes first, so retrying a successful send says "already sent"
rather than "only an approved run may be sent".

The audit row is written **before** dispatch, is append-only, and stores hashes
rather than contents.

---

## State

Two state machines, in `app/domain/status.py`:

```
résumé:  uploaded → parsing → needs_confirm → confirmed → superseded | failed
run:     created → tailoring → needs_review → approved | rejected
                                            → sending → sent | failed
```

**The transitions are data, not conditionals** — one table, one check function,
one error type. A dozen `if status == ...` checks spread across the API is how a
status field becomes decorative: every site enforces a slightly different rule and
the union of them is not a machine.

The only legal self-transition in either machine is `needs_confirm →
needs_confirm`, which is the edit loop.

---

## The two bindings

**Preview → decision.** When the gate shows a draft, the API issues an HMAC over
the *stored* draft. Approving sends it back for verification.

**Decision → send.** Approving issues a second HMAC over the stored message row.
Sending requires it.

Details that matter:

- payloads are **length-prefixed** before hashing, so `("ab","c")` and `("a","bc")`
  cannot collide;
- comparison uses `hmac.compare_digest`, never `==`;
- the expiry lives **inside** the signed payload, so a client cannot edit it;
- the session cookie is an HMAC over a length-prefixed payload rather than a JWT,
  so **no library reads an `alg` field out of attacker-supplied input** — `alg:
  none` and RS256-to-HS256 confusion are attacks on exactly that.

---

## Deployment topology

```
  YOUR LAPTOP                      THE INTERNET
  ───────────                      ────────────
  Ollama (the model)               Vercel · web/    Next.js UI
  the full FastAPI app             Vercel · app/    FastAPI, one function,
  can START runs                                    READ-ONLY
      │                                  │
      └──────────────▶ Neon · Postgres ◀─┘
```

The public instance serves runs that already happened. It cannot start new ones: a
run takes minutes and no HTTP request survives that; the model is on a machine the
internet cannot reach; a hosted key would spend one free-tier quota per visitor.
Read-only is enforced as **middleware**, not per route — any unsafe HTTP method is
refused with 403 before it reaches a handler.

The browser is given **one origin**: the UI proxies `/api/*` to the API. A
`SameSite=Lax` cookie is never attached to a cross-site `fetch`, and `vercel.app`
is on the Public Suffix List, so two Vercel subdomains are two different sites.
`SameSite=None; Secure` would be a third-party cookie, which Safari and Firefox
block by default. Full reasoning in [`../DEPLOY.md`](../DEPLOY.md).
