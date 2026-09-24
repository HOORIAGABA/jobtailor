"""Run the whole pipeline once, against the real model, and print what happened.

    python scripts/try_it.py

This is the first time the pieces are wired together. It uses a built-in sample
resume and job posting so nothing else needs to exist yet — no database, no
upload, no web page.

What it shows you:
  * what the model understood about the job
  * which of your lines evidence which parts of it
  * what the planner wanted to change
  * WHAT THE VALIDATOR REFUSED, and why
  * the final diff a reviewer would approve

Costs 3 model calls (~15k tokens).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.job_brief import build_job_brief
from app.agents.planner import plan
from app.agents.writer import write_bullets
from app.config import Settings, check
from app.domain.errors import BudgetExceeded, LLMRateLimited, LLMUnavailable
from app.domain.models import Contact, RawEntry, RawResume, RawSection
from app.engine.apply import apply_ops
from app.engine.diff import build_diff
from app.engine.evidence import compute_evidence, undemonstrated_terms
from app.engine.normalize import normalize
from app.engine.validator import grounding_corpus, validate
from app.io.llm import BudgetedClient, RunBudget, build_client

DIM, BOLD, GREEN, RED, YELLOW, RESET = (
    "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
)


def setup_logging(verbose: bool) -> None:
    """Without this, every logger.info in the pipeline goes nowhere.

    Python's default handler only surfaces WARNING and above, so the "dropped
    N operations" diagnostics were being written and discarded — which is how
    an empty plan looked like a silent mystery.
    """
    import logging
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format=f"{DIM}  %(levelname)-7s %(name)-24s %(message)s{RESET}",
    )


def head(text: str) -> None:
    print(f"\n{BOLD}{'─' * 70}\n{text}\n{'─' * 70}{RESET}")


SAMPLE_RESUME = RawResume(
    contact=Contact(full_name="R. Khan", email="r.khan@example.com"),
    summary="Data analyst with three years of experience in reporting and analytics.",
    sections=[
        RawSection(heading="WORK EXPERIENCE", entries=[
            RawEntry(
                title="Data Analyst", org="Acme Corp", dates="Jan 2022 - Present",
                bullets=[
                    "Worked on data pipelines for the reporting team",
                    "Built a dashboard that reduced manual reporting by 12 hours a week",
                    "Responsible for weekly stakeholder reports",
                ],
            ),
            RawEntry(
                title="Analytics Intern", org="Beta Labs", dates="2021",
                bullets=[
                    "Helped with a data migration between two systems",
                    "Wrote SQL queries for the marketing team",
                ],
            ),
        ]),
        RawSection(heading="PROJECTS", entries=[
            RawEntry(
                title="Churn predictor", dates="2023",
                bullets=[
                    "Trained a model to predict customer churn using scikit-learn",
                    "Deployed it as a scheduled job that ran nightly with retries",
                ],
            ),
        ]),
        RawSection(heading="TECHNICAL SKILLS", entries=[
            RawEntry(bullets=[
                "Languages: Python, SQL",
                "Tools: Airflow, Docker, scikit-learn, Pandas",
            ]),
        ]),
        RawSection(heading="EDUCATION", entries=[
            RawEntry(title="BSc Computer Science", org="University of Example",
                     dates="2017 - 2021"),
        ]),
    ],
)

SAMPLE_JOB = """\
Machine Learning Engineer — Nimbus

Nimbus builds forecasting tools for logistics companies. We are a team of nine
and we ship every week.

You will own our model training and serving pipelines end to end. Right now
retraining is a manual process that one person runs by hand, and it takes most
of a day. We want it automated, monitored, and boring.

You will also work with the data team to build the feature pipelines the models
depend on, and help decide what we measure.

Required: strong Python, experience with PyTorch, and hands-on experience
orchestrating data workflows (we use Airflow).
Nice to have: Kubernetes, experience with forecasting problems.

To apply, send your CV to careers@nimbus.example.
"""


def main() -> int:
    setup_logging("-v" in sys.argv or "--verbose" in sys.argv)
    settings = Settings()
    if problems := check(settings):
        print(f"{RED}Cannot run — configuration is incomplete:{RESET}")
        for p in problems:
            print(f"  - {p}")
        print(f"\n{DIM}Copy .env.example to .env and fill in LLM_API_KEY and "
              f"LLM_MODEL.{RESET}")
        return 1

    print(f"{DIM}model: {settings.llm_model}{RESET}")
    client = BudgetedClient(
        build_client(settings),
        RunBudget(max_calls=settings.max_llm_calls_per_run,
                  max_tokens=settings.max_tokens_per_run),
    )

    # ── S0 ingest (the parse is faked here; the PDF reader isn't built yet) ──
    doc = normalize(SAMPLE_RESUME)
    head("1. RESUME — every line has a permanent id")
    for section in doc.sections:
        print(f"  {BOLD}{section.heading}{RESET}  {DIM}[{section.kind}]{RESET}")
        for item in section.items:
            label = " · ".join(p for p in (item.title, item.org) if p)
            print(f"    {DIM}{item.id}{RESET}  {label}")
            for b in item.bullets:
                print(f"      {DIM}{b.id}{RESET}  {b.text[:64]}")
    print(f"\n  {DIM}skill inventory: {', '.join(doc.skill_inventory)}{RESET}")

    # ── S1 job brief ──────────────────────────────────────────────────
    head("2. JOB BRIEF — what the model understood (1 call)")
    brief = build_job_brief(SAMPLE_JOB, client)
    print(f"  role      {brief.role} at {brief.company}")
    print(f"  tone      {brief.tone}")
    print(f"  recruiter {brief.recruiter_email or '(none stated)'}")
    print(f"\n  {BOLD}narrative{RESET}\n    {brief.role_narrative}")
    print(f"\n  {BOLD}problems{RESET}")
    for p in brief.problems_to_solve:
        print(f"    [{p.priority}] {p.statement}")
    print(f"\n  {BOLD}terms{RESET}")
    for t in brief.terms:
        req = "required" if t.required else "nice to have"
        print(f"    {t.term:<18} weight {t.weight:>2}  {DIM}{req}{RESET}")

    # ── S2 evidence (no model) ────────────────────────────────────────
    head("3. EVIDENCE — which lines back which parts of the job (0 calls)")
    index = compute_evidence(brief, doc)
    strong = [l for l in index.links if l.strength == "strong"]
    print(f"  {len(strong)} strong links, {len(index.links) - len(strong)} hints")
    seen = set()
    for link in strong:
        if link.bullet_id in seen:
            continue                      # one line can back several elements
        seen.add(link.bullet_id)
        print(f"    {GREEN}strong{RESET}  {link.bullet_id:<14} "
              f"{DIM}{doc.text_of(link.bullet_id)[:48]}{RESET}")
    print(f"  {DIM}(skills-section lines are declarations, not evidence){RESET}")
    if gaps := undemonstrated_terms(brief, index):
        print(f"\n  {YELLOW}no evidence for: {', '.join(gaps)}{RESET}")

    # ── S3 plan ───────────────────────────────────────────────────────
    head("4. PLAN — what to change (1 call)")
    corpus = grounding_corpus(doc)
    discarded: list[str] = []
    ops = plan(brief, doc, index, corpus, client, dropped=discarded)
    if not ops:
        print(f"  {YELLOW}the model produced no usable operations{RESET}")
    for reason in discarded:
        print(f"    {RED}dropped{RESET}  {reason}")
    for op in ops:
        target = getattr(op, "bullet_id", "") or getattr(op, "item_id", "") \
            or getattr(op, "section_id", "") or getattr(op, "requirement", "")
        print(f"    {op.op_id:<5} {op.op:<16} {DIM}{target}{RESET}")

    # ── S4 write ──────────────────────────────────────────────────────
    head("5. WRITE — prose for the targeted lines (1 call)")
    ops = write_bullets(ops, doc, brief, client)
    for op in ops:
        if op.op == "rewrite_bullet":
            print(f"    {DIM}before{RESET}  {doc.text_of(op.bullet_id)}")
            print(f"    {BOLD}after {RESET}  {op.text}\n")

    # ── S5 validate (no model) — the important part ───────────────────
    head("6. VALIDATE — what the guard refused (0 calls)")
    result = validate(ops, doc, confirmed_capabilities=[])
    print(f"  {GREEN}accepted {len(result.accepted)}{RESET}   "
          f"{RED}rejected {len(result.rejected)}{RESET}")
    for r in result.rejected:
        print(f"    {RED}{r.code}{RESET}  {r.op_kind}")
        print(f"      {r.detail}")
        if r.ask_user:
            print(f"      {DIM}→ will ask the candidate instead{RESET}")
    if not result.rejected:
        print(f"    {DIM}nothing refused this run{RESET}")

    # ── S6/S7 apply and diff (no model) ───────────────────────────────
    tailored = apply_ops(doc, result.accepted)
    review = build_diff(doc, tailored, result.accepted, result.rejected)

    head("7. REVIEW — what the candidate would approve (0 calls)")
    for c in review.changes:
        print(f"  {BOLD}{c.op_kind}{RESET}  {DIM}{c.label or c.ref_id}{RESET}")
        print(f"    - {c.before[:100]}")
        print(f"    + {c.after[:100]}")
        if c.rationale:
            print(f"    {DIM}why: {c.rationale}{RESET}")
        if c.cites:
            print(f"    {DIM}cites: {', '.join(c.cites)}{RESET}")
        print()
    for gap in review.gaps:
        print(f"  {YELLOW}gap{RESET}  [{gap.severity}] {gap.requirement}")
    for q in review.questions:
        print(f"  {YELLOW}ask{RESET}  {q.question}")
        print(f"       {DIM}about: {q.context[:60]}{RESET}")

    # ── proof ─────────────────────────────────────────────────────────
    head("8. PROOF — the guarantees, checked")
    rendered = " ".join(b.text for b in tailored.all_bullets()) + " " + tailored.summary
    before_lines = len(list(doc.all_bullets()))
    after_lines = len(list(tailored.all_bullets()))
    dropped = [op.bullet_id for op in result.accepted if op.op == "drop_bullet"]

    checks = [
        ("no content silently lost",
         after_lines == before_lines - len(dropped)),
        ("every section survived",
         [s.heading for s in tailored.sections] == [s.heading for s in doc.sections]),
        ("every change attributable to an operation",
         all(c.op_id for c in review.changes)),
        (f"within the call budget ({client.budget.calls}/{client.budget.max_calls})",
         client.budget.calls <= client.budget.max_calls),
    ]
    for label, ok in checks:
        print(f"  {GREEN + 'PASS' if ok else RED + 'FAIL'}{RESET}  {label}")

    print(f"\n  {DIM}{client.budget.calls} model calls, "
          f"{client.budget.tokens} tokens{RESET}")
    for entry in client.log:
        print(f"    {DIM}{entry['stage']:<12} {entry['prompt_tokens']:>6} in  "
              f"{entry['completion_tokens']:>5} out  {entry['ms']:>6} ms{RESET}")

    return 0 if all(ok for _, ok in checks) else 1


def run() -> int:
    """Turn the expected failures into advice instead of a stack trace.

    A rate limit is not a bug — it is the free tier working as documented — so
    it should read like a note, not a crash.
    """
    try:
        return main()
    except LLMRateLimited as exc:
        print(f"\n{RED}Rate limited.{RESET} {exc}")
        print(f"\n{DIM}Free tiers are capped per minute — Gemini 3.8 Flash is 5.")
        print("Options:")
        print("  1. wait a minute and run it again")
        print("  2. try a model with a higher allowance, e.g.")
        print("     LLM_MODEL=gemini-3.5-flash       (or gemini-3.5-flash-lite)")
        print(f"  3. check your actual limits: https://aistudio.google.com/rate-limit{RESET}")
        return 2
    except LLMUnavailable as exc:
        print(f"\n{RED}Could not reach the model.{RESET} {exc}")
        print(f"\n{DIM}If the model id was retired, pick the current one from"
              f" https://aistudio.google.com and update LLM_MODEL in .env.{RESET}")
        return 2
    except BudgetExceeded as exc:
        print(f"\n{RED}Budget exceeded.{RESET} {exc}")
        return 3


if __name__ == "__main__":
    raise SystemExit(run())
