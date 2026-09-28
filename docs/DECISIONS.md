# Decisions, and the bugs behind them

Most of the non-obvious choices in this codebase exist because something broke.
This file is the record: what went wrong, what it cost, and what changed.

It is here rather than in a wiki because a decision without its cause gets
reverted by the next person who finds it inconvenient.

---

## The design decisions

### D1 · The model returns operations, never a résumé

A language model asked to improve a résumé will improve it by inventing a number.
The only reliable defence is to make prose-in, prose-out impossible: the model
emits typed operations with ids and citations, and a deterministic function
decides whether each is honest.

**Rejected alternative:** prompt the model not to invent. That makes the guarantee
a property of a string, revisable by anyone editing the prompt, and unverifiable.

### D2 · The guard is architecturally prevented from calling a model

`import-linter` forbids `app.engine` and `app.domain` from importing `httpx`,
`google`, `sqlalchemy` or any higher layer.

The point is not tidiness. Six months from now the obvious way to fix a false
positive in the validator will be "ask the model whether this claim is supported".
That would dissolve the guarantee while appearing to improve it. The contract
makes it fail the build instead of a code review.

### D3 · No score, anywhere

Goodhart's law. A match percentage becomes a target, and the cheapest way to raise
it is keyword stuffing — the exact behaviour the product exists to prevent. So the
answer is three named buckets and a list of specific gaps.

Applied to the evaluation too: the eval harness reports per-case outcomes, not an
aggregate number.

### D4 · Two human gates, at the two points where a mistake becomes invisible

**Confirm the parse**, because every later citation is built on ids frozen at that
moment, and an error there is undetectable downstream. **Approve the draft**,
because sending cannot be undone.

The confirm screen shows the unplaced lines *verbatim* rather than rendering the
parsed document with a "looks right?" button. A specific piece of text gets read.
A rendered preview gets a click.

### D5 · Approval is bound to the text by HMAC

Not a "confirmed: true" flag. The token signs the stored draft, so editing after
approving invalidates the approval. Sending needs a second token over the stored
message row.

This is the decision that caught a real bug in the UI (B2 below) rather than
letting it send the wrong email silently.

### D6 · No model id is hardcoded anywhere in source

The provider and model come from configuration. This project has been broken twice
by a model being retired. `config.check()` validates the combination and `GET /`
reports exactly which variable is wrong.

### D7 · A two-model split is supported

Parsing is high-volume and low-judgment; planning and writing are low-volume and
high-judgment. `LLM_*` and `SMART_LLM_*` can point at different providers, so a
small local model can do the bulk work for free while three calls a run go to a
hosted model. On a 6 GB laptop GPU this is the difference between "runs" and
"out of memory".

### D8 · Artifacts are bytes in Postgres, not paths on disk

v1 stored absolute file paths. The host restarted with a clean disk and every row
pointed at a file that no longer existed. Serverless and free-tier hosts have
ephemeral filesystems; a path is a promise the platform does not make.

### D9 · One dependency list, no dev/prod profile switch

`requirements.txt` is identical everywhere. A profile switch is how "works on my
machine" becomes structural.

### D10 · State transitions are a table, not conditionals

One table, one check function, one error. A dozen `if status == ...` checks spread
across an API is how a status field becomes decorative: each site enforces a
slightly different rule and the union of them is not a machine.

### D11 · The public deployment is read-only, and says so

A run takes minutes; the model is on a laptop; a hosted key would spend one
free-tier quota per visitor. Rather than pretend, `GET /api/capabilities` reports
the mode and the UI hides what it cannot do. Read-only is enforced as middleware,
not per route — see B5.

### D12 · No URL fetching

v1 shipped `httpx.get(user_url, follow_redirects=True)`. On a cloud host that
reaches the instance metadata endpoint. The feature stays out until there is an
SSRF guard. A job posting arrives as a paste, which is also how people actually
have one.

---

## The bugs

### B1 · A Barista scored 60 against an ML Engineer's 47

Term matching used substrings, and `ml` matched `html`. The irrelevant CV in the
test set outscored the relevant one.

**Changed:** whole-term and alias matching with a shared tokenizer.
**Lesson:** substring matching on domain terms is always wrong — and the bug was
only visible because the test set contained a deliberately irrelevant résumé.

### B2 · ★ The confirm token made an editable draft impossible to approve

`decide` verified the token against the *submitted* text. No browser can compute
an HMAC without the key, so editing the draft — the entire point of the screen —
always returned 409.

**The test that should have caught it forged a token using the server's own
secret. The test passed and the product did not work.** It was found by driving
the real UI.

**Changed:** split into two bindings — preview→decision over the stored draft, and
decision→send over the stored message row.
**Lesson:** a test that constructs its input with production internals is testing
the internals, not the contract.

### B3 · ★ Arbitrary file read through the job field

`POST /api/runs {"job": ".env"}` returned the contents of `.env` — the LLM key,
all three signing secrets and the Google client secret — to any signed-in user,
because the job-source loader accepted a path as well as text.

**Changed:** `load_job(source, *, allow_paths=False)`. The capability still exists
for the CLI; the HTTP layer cannot reach it.
**Lesson:** a helper that is safe in one caller is a vulnerability in another. Make
the dangerous mode opt-in at the call site.

### B4 · The idempotency check ran after the status check

Retrying a send answered *"this run is 'sent'; only an approved run may be sent"* —
true, and actively misleading.

**Changed:** `sent_at` is checked first.
**Lesson:** with ordered guards, the most specific answer has to come first.

### B5 · Read-only was not read-only

Exactly one endpoint checked the flag, so on a public instance a visitor could
confirm a résumé, approve a draft and press send. And the test fixture had been
passing `read_only=True` and then uploading successfully — a test asserting the
bug.

**Changed:** middleware refusing every unsafe method before it reaches a handler.
**Lesson:** a rule that each route must remember to apply is a rule the next route
will forget.

### B6 · Seven Windows-only bugs, every one invisible on Linux

The worst: `time.monotonic()` advances in ~15.6 ms steps on Windows, so several
events inside one step shared a timestamp and the call log's ordering collapsed.
The send audit had the same problem, and its tie-break was a random hex id — so the
audit trail came out in random order.

**Changed:** a sequence counter (**count, don't time**), a schema migration for it,
and a Windows leg in CI.

Also in this family: a 32767-character environment variable limit reached through a
pytest parametrize id; `cp1252` as the default encoding refusing to read an em
dash; an open SQLite file that cannot be deleted on Windows (POSIX allows unlinking
an open file, so it leaked silently on Linux and failed loudly on Windows).

**Lesson:** "green on Linux" is not "green".

### B7 · The keyword-stuffing check rewarded stuffing

It used Jaccard similarity to measure how much a line had changed, and appending to
a list *lowers* Jaccard — so a longer keyword list scored as "more changed" and
passed the check that existed to catch it.

**Changed:** structural detection of appending.
**Lesson:** found by an eval case, not by a unit test. Adversarial cases find what
example-based tests cannot.

### B8 · Four wrong field names in the frontend types

`questions` was typed `string[]` and is a list of objects, so React was handed an
object as a child and the gate screen threw. Behind that crash, three more were
silently wrong: a change's `op_kind` / `label` / `rationale` had been written as
`op` / `where` / `why`.

Those render as `undefined`, which React prints as nothing — so the diff would have
shown every change **with no reason attached**, on a product whose entire claim is
that every change carries its reason.

**Changed:** the types were rewritten from the API rather than from memory.
**Lesson:** the type checker cannot catch a type that is confidently wrong. Generate
clients from the schema.

### B9 · `.gitignore` ate the most important screen

`runs/` was unanchored, so it also matched `web/app/runs/`. The approval gate — the
single most important page in the product — was pushed missing, and nothing failed.

**Changed:** anchored to `/runs/`, and `tests/test_repo.py` now runs
`git check-ignore` over every source file.
**Lesson:** an ignore rule is a pattern, not a path.

### B10 · Two readers for one fact: which database is this

`Settings` read `.env`; `db/session.py` read `os.environ`; pydantic-settings puts
neither into the other. So the production connection string in `.env` produced two
different answers, and `alembic upgrade head` migrated the local SQLite file and
printed success.

Immediately after: the URL normaliser handled `postgres://` — the Heroku form every
blog post mentions — and not `postgresql://`, which is what Neon actually gives you.
So the common case was the broken one, and it failed as `ModuleNotFoundError: No
module named 'psycopg2'` from inside a dialect module, which reads as a missing
package and invites installing a second unwanted driver.

**Changed:** one producer for the value, a named `normalise_url()` covering both
bare schemes, and a test asserting `Settings` and the engine agree.

### B11 · A CSS mask ate the hero

`mask-image` on the landing section faded out the headline, the lede and both
buttons along with the decorative dot grid it was meant for — because a mask
applies to an element *and everything inside it*.

**Changed:** the texture moved to a `::before` layer with `isolation: isolate`.
**Lesson:** it looked like a broken theme, and the cause was one property on the
wrong element.

### B12 · ★ The gate showed `0 refused` on every live run

`tailor()` logged the validator's verdict as a `validation` stage. `DbRunLog`
had no column for that stage name, so it became a checkpoint row and nothing
else; `accepted_json` and `rejected_json` were written only by the seed scripts.
The accounting — model calls, tokens, the proof block — lived in `run()`, which
the API's worker never calls (it starts from a confirmed parse and calls
`tailor()` directly).

So the seeded demo showed a refusal, and a real run from the UI showed
`accepted: 0, rejected: 0`, zero model calls and an empty proof — on the exact
screen whose job is to show the refusals. Every test passed, because every test
of that screen used seeded data.

**Changed:** one function, `columns_for(stage, payload)`, is the only place a
stage maps to run columns, and `validation` splits into two of them; the
accounting is `account()`, called by every entry point, including on failure.
`tests/test_run_api.py` now drives the demo case through
`pipeline.runs.execute` — the product path — and asserts the verdict reaches the
row and the API.
**Lesson:** the sixth instance of the pattern below. The fixture stood in for
the product.

### B13 · ★ Two requests could both send

Step 1 of `send` checked `sent_at`; step 3 wrote the audit and moved the run
to `sending`. Between them is a read-then-write, and two requests — a
double-click, a retry racing the original — could both read `approved` and
`NULL` before either wrote. Both passed, both dispatched.

**Changed:** the move to `sending` is `UPDATE … WHERE status = 'approved'`, and
the request whose UPDATE matched zero rows is told the message is already on
its way. The transition table still decides *legality*; the database decides
*once*. `send_audit` gained a unique `(message_id, seq)` as the second lock.
**Lesson:** an idempotency column is necessary and not sufficient. The check
and the write have to be one statement.

### B14 · A single non-ASCII character was a 500

`hmac.compare_digest(str, str)` raises `TypeError` if either string is not
ASCII. Both the approval token and the session cookie are client input, so
`session=…é` was an unhandled exception on every authenticated route and a
500 on the send endpoint — the one place a person will press the button again.

**Changed:** compared as bytes; the expiry field is checked for the exact shape
the issuer writes (`int()` accepts `٣`).

### B15 · Read-only could connect Gmail and could not disconnect it

The read-only middleware refuses unsafe methods. `GET /google/callback` is a
safe method that writes — it stored the refresh token — and `DELETE /google`,
which revokes it, was refused. The one asymmetry a consent flow must never
have.

**Changed:** `DELETE /google` is always allowed, like sign-out (both are a
person withdrawing something). A read-only instance refuses to *start* the
connect flow, and a send grant that reaches the callback anyway is revoked at
Google rather than stored — or dropped, which would leave a live grant nothing
can see.

### B16 · Two smaller ones

- The worker's setup block — load the résumé, validate it, move to
  `tailoring` — ran outside the handlers, so a `doc_json` that no longer
  validated raised out of the thread and left the run in `created` with no
  error. Now inside the `try`.
- `_fail` assigned `run.status = "failed"` directly: the only status write in
  the codebase that bypassed the transition table. Now `move_to`.
- `.env` decoding: a BOM from Notepad and a cp1252 em dash were both crashes
  at import time. `Settings` reads UTF-8 explicitly; the fallback reader
  tolerates both.

### B17 · Every primary button was a black pill with no label

`button { color: inherit }` sat in `globals.css` outside any `@layer`. Tailwind
v4 puts every utility in `@layer utilities`, and an unlayered rule beats a
layered one regardless of order or specificity — so `text-ground` lost, and
"Tailor it", "Approve" and "Send it" rendered ink on ink. The computed style
said so; nothing else did.

**Changed:** the element rules moved into `@layer base`.
**Lesson:** found by screenshotting the flow end to end through a browser
(`DEMO.md`, level A), which is also how F1 would have been found. CSS has no
type checker; a screenshot is the test.

---

## The pattern

Six of these share a shape: **something reported success and did nothing.**

1. v1 ran migrations as SQLite `PRAGMA` statements, which no-op on Postgres. The
   schema never changed and nothing said so. (Why Alembic is here.)
2. B9 — the ignore rule swallowed a directory and every check passed.
3. B10 — a local SQLite file stood in for the production database.
4. B10 again — psycopg2 stood in for psycopg 3, and announced itself as a missing
   package.
5. B2 — the test forged its own input and passed while the product was broken.
6. B12 — every test of the gate screen read seeded rows, so the path that
   writes them on a real run was never exercised.

The common cause is a **plausible default** standing in for the thing that was
asked for: a local file, an ignored directory, a legacy driver, a no-op pragma.
Each is reasonable in the absence of instruction, and each is silent about having
been chosen.

The fix has been the same every time: make the two things that must agree compare
themselves in a test, and name the transformation rather than leaving it implicit.

---

## Known, unfixed

- **The rate limiter counts in-process.** A serverless function scales by adding
  processes, so the effective limit is the written one times the number of
  instances. Harmless while the public instance refuses every mutating method; it
  must move into the database before that changes.
- **Tailoring quality is unmeasured.** The evals pin safety, not usefulness.
- **`langgraph`, `langchain-core` and `langfuse` are in `requirements.txt` and
  nothing imports them.** Left over from an earlier design where the pipeline was
  to be a graph; it is eighty lines of explicit Python instead. They should be
  removed.
