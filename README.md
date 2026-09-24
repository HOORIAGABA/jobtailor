# JobTailor

**An agentic resume-tailoring system that can prove it didn't make anything up.**

Give it a job posting and your resume. It studies the role, rewrites your resume
for it, drafts the recruiter email — and shows you exactly what it changed, why,
and which line of your own resume each change came from. Nothing is sent until
you confirm the recipient, subject and body yourself.

**Repo:** https://github.com/HOORIAGABA/jobtailor

## Status

**184 tests, 0.4s, no API key.**

Every guarantee the product advertises is enforced in deterministic Python and
asserted without calling a model — including an end-to-end test that runs a
resume and a plan through normalize → validate → apply → diff and checks that
nothing was fabricated and nothing was lost.

| Phase | State |
|---|---|
| Domain model + operations | done |
| Engine — normalize, validate, apply, diff | done |
| Engine — evidence matching | **done** |
| LLM client, schema conversion, budget | done |
| Agent — job brief | done |
| Agents — planner, writer | next |
| Agents (brief, planner, writer) | — |
| Pipeline (LangGraph + checkpointing) | — |
| API + UI (diff view, approval gate) | — |
| Evals + tracing + deploy | — |

## Architecture in one line

The model proposes **typed edit operations**; deterministic Python validates and
applies them. 4 LLM calls per run; 11 of 15 stages are plain code.

```
resume ─► parse ─► [confirm] ─┐
                              ├─► evidence ─► plan ─► write
posting ─► brief ─────────────┘                        │
                                                       ▼
                                   validate ─► apply ─► diff
                                                       │
                              [you approve] ◄──────────┘
                                     │
                              render ─► send
```

## Running locally

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows
pip install -r requirements.txt
cp .env.example .env                                 # then fill it in
pytest -q
```

The stack is identical locally and in production — same model, same libraries,
same database engine. Only `.env` values differ.

## Layering

```
api → pipeline → io → agents → engine → domain
```

Enforced in CI by `.importlinter`: `engine` and `domain` cannot import an LLM
client. That is what keeps correctness in code you can test without an API key.

## License

MIT
