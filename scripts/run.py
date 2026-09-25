"""The whole pipeline, on your files, with every stage saved.

    python scripts/run.py --resume my_cv.pdf --job job.txt
    python scripts/run.py --resume my_cv.pdf --job https://... --quiet

Four model calls: parse, brief, plan, prose. Everything else is deterministic.

Every stage lands in `runs/<timestamp>_<name>/` as it is produced, so a run
that fails in the middle still leaves the files that explain why. `--quiet`
prints only the summary and relies on those files.

This is S0.1 through S7. It stops where a human would be asked to approve,
because the approval gate (S9), rendering (S10) and sending (S11) are not
built yet.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings, check
from app.domain.errors import JobTailorError, ModelOutputError
from app.io.llm import (
    BudgetedClient, RunBudget, build_client, merged_call_log,
)
from app.io.cache import FileCache, NullCache
from app.io.jobsource import load_job
from app.io.runlog import NullRunLog, RunLog
from app.io.render import filename_for
from app.pipeline.run import RunState, proof_checks, run

DIM, BOLD, GREEN, RED, YELLOW, RESET = (
    "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
)


def head(text: str) -> None:
    print(f"\n{BOLD}{'─' * 70}\n{text}\n{'─' * 70}{RESET}")


def show(state: RunState) -> None:
    """The parts a person actually needs to read. The rest is on disk."""
    doc, diff = state.doc, state.diff

    head("PARSE — did the document survive?")
    coverage = state.coverage
    if coverage is None:
        print("  (not run)")
    elif coverage.is_clean:
        print(f"  {GREEN}{coverage.summary()}{RESET}")
    else:
        print(f"  {RED}{coverage.summary()}{RESET}")
        for problem in coverage.structure:
            print(f"    {RED}structure{RESET}  {problem}")
        for line in coverage.dropped[:8]:
            print(f"    {RED}missing  {RESET}  {line[:72]}")
        for line in coverage.invented[:8]:
            print(f"    {RED}invented {RESET}  {line[:72]}")

    if doc is not None:
        sections = ", ".join(f"{s.kind}({len(s.items)})" for s in doc.sections)
        print(f"  {DIM}{sections}{RESET}")
        print(f"  {DIM}grounding corpus: "
              f"{', '.join(doc.skill_inventory) or 'empty'}{RESET}")

    if state.posting is not None and state.posting.removed:
        head("POSTING — page furniture removed before reading")
        print(f"  {len(state.posting.removed)} line(s) stripped, "
              f"{state.posting.chars} characters left")
        for line in state.posting.removed[:8]:
            print(f"    {DIM}-{RESET} {line[:70]}")
        if len(state.posting.removed) > 8:
            print(f"    {DIM}… {len(state.posting.removed) - 8} more{RESET}")

    if state.brief is not None:
        head("JOB — what the model understood")
        brief = state.brief
        print(f"  {brief.role or '?'} at {brief.company or '?'}  {DIM}({brief.tone}){RESET}")
        required = [t.term for t in brief.terms if t.required]
        print(f"  required: {', '.join(required) or DIM + 'none marked' + RESET}")

        # Three grades, because the system's confidence differs between them.
        # The old single list said "gap" as a fact; lexical matching only ever
        # established that no bullet NAMED the term.
        grades = [
            ("demonstrated", GREEN, "a bullet shows this work"),
            ("declared_only", YELLOW, "listed in skills, no bullet shows it"),
            ("not_found", RED, "not found by name — check if a bullet covers it"),
        ]
        for grade, colour, note in grades:
            terms = state.standing.get(grade) or []
            if terms:
                print(f"  {colour}{grade:<14}{RESET} {', '.join(terms)}")
                print(f"  {DIM}{'':<14} {note}{RESET}")

    head("PLAN — what was requested, and what the guard refused")
    print(f"  {len(state.ops)} operation(s) requested, "
          f"{GREEN}{len(state.accepted)} accepted{RESET}, "
          f"{RED}{len(state.rejected)} rejected{RESET}")
    for reason in state.dropped_ops:
        print(f"    {RED}dropped {RESET}  {reason}")
    for r in state.rejected:
        print(f"    {RED}{r.code}{RESET}  {r.op_kind} — {r.detail[:70]}")
    if not state.ops:
        print(f"    {YELLOW}the model produced no operations{RESET}")

    if diff is not None:
        head("REVIEW — what the candidate would approve")
        for c in diff.changes:
            print(f"  {BOLD}{c.op_kind}{RESET} {DIM}{c.label or c.ref_id}{RESET}")
            print(f"    {RED}-{RESET} {c.before[:96]}")
            print(f"    {GREEN}+{RESET} {c.after[:96]}")
            if c.rationale:
                print(f"    {DIM}why: {c.rationale[:88]}{RESET}")
        for gap in diff.gaps:
            print(f"  {YELLOW}gap{RESET} [{gap.severity}] {gap.requirement[:70]}")
        for q in diff.questions:
            print(f"  {YELLOW}ask{RESET} {q.question[:76]}")
        if diff.is_empty():
            print(f"  {DIM}no changes — doing less is a valid plan{RESET}")


def print_proof(state: RunState, client, smart=None) -> None:
    head("PROOF — the guarantees, checked")
    for label, ok in proof_checks(state).items():
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        print(f"  {mark}  {label.replace('_', ' ')}")

    # Both clients share one budget, so its counters already cover the run;
    # only the per-call log lives on each client.
    entries = list(client.log)
    if smart is not None and smart is not client:
        entries += list(smart.log)
    entries.sort(key=lambda e: e.get("started", 0))

    print(f"\n{DIM}  {client.budget.calls}/{client.budget.max_calls} calls, "
          f"{client.budget.tokens} tokens{RESET}")
    if client.budget.calls == 0:
        print(f"{DIM}    every stage came from the cache{RESET}")
    for entry in entries:
        print(f"{DIM}    {entry['stage']:<10} {entry['prompt_tokens']:>6} in "
              f"{entry['completion_tokens']:>6} out {entry['ms']:>7} ms{RESET}")


def read_job(source: str) -> str:
    """A path, pasted HTML, a saved page, a PDF, or the posting itself.

    `allow_paths=True` because this is the command line: the person running it
    owns the machine and is naming their own file. The HTTP callers do not pass
    it — see `io.jobsource.load_job` for what that would mean.
    """
    return load_job(source, allow_paths=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run S0–S7 on real files.")
    parser.add_argument("--resume", type=Path, required=True,
                        help=".pdf, .docx, .txt or .md")
    parser.add_argument("--job", required=True,
                        help="a file holding the job posting, or the text itself")
    parser.add_argument("--runs", type=Path, default=Path("runs"))
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--hints", action="store_true",
                        help="one extra call: ask about requirements exact "
                             "matching could not place (hints only)")
    parser.add_argument("--fresh", action="store_true",
                        help="ignore cached stages and call the model again")
    parser.add_argument("--cache", type=Path, default=Path(".cache"),
                        help="where cached stage results live (default: .cache/)")
    parser.add_argument("--quiet", action="store_true",
                        help="print the summary only; read the rest on disk")
    parser.add_argument("--out", type=Path, default=Path("out"),
                        help="where the tailored .docx and .pdf are written")
    parser.add_argument("--outreach", action="store_true",
                        help="S8 — draft the recruiter email (one extra call)")
    parser.add_argument("--no-render", action="store_true",
                        help="skip S10 — no .docx or .pdf is produced")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format=f"{DIM}  %(levelname)-7s %(name)-22s %(message)s{RESET}",
    )

    if not args.resume.exists():
        print(f"{RED}No such file: {args.resume}{RESET}")
        return 2

    settings = Settings()
    if problems := check(settings):
        print(f"\n{RED}Configuration:{RESET}\n  - " + "\n  - ".join(problems))
        return 2

    try:
        jd_text = read_job(args.job)
    except JobTailorError as exc:
        print(f"\n{RED}Cannot read the job posting.{RESET}\n  {exc}")
        return 2
    data = args.resume.read_bytes()

    log: RunLog = NullRunLog() if args.no_save else RunLog.create(
        args.resume.stem, root=args.runs
    )
    log.note(provider=settings.llm_provider, model=settings.llm_model,
             job_chars=len(jd_text))

    # One budget, shared by both clients, so the ceiling covers the whole run
    # however the work is split between them.
    budget = RunBudget(max_calls=settings.max_llm_calls_per_run,
                       max_tokens=settings.max_tokens_per_run)
    client = BudgetedClient(build_client(settings), budget)
    smart = client
    if settings.has_smart_model:
        smart = BudgetedClient(build_client(settings, role="smart"), budget)
        smart_settings = settings.for_role("smart")
        log.note(smart_provider=smart_settings.llm_provider,
                 smart_model=smart_settings.llm_model)
        print(f"{DIM}  parse: {settings.llm_model}   "
              f"judgment: {smart_settings.llm_model}{RESET}")

    store = NullCache() if args.fresh else FileCache(args.cache)

    try:
        state = run(data, args.resume.name, jd_text, client, log=log,
                    cache=store, smart_client=smart,
                    semantic_hints=args.hints,
                    render_output=not args.no_render,
                    write_outreach=args.outreach)
    except JobTailorError as exc:
        log.note(calls=client.budget.calls, tokens=client.budget.tokens,
                 call_log=merged_call_log(client, smart))
        # The error tells the reader to look at what the model actually said.
        # Until now nothing wrote it, so the advice was unfalsifiable: the
        # difference between "spending the budget thinking" and "the input is
        # too long" is visible in the first line of the response and nowhere
        # else.
        raw = exc.raw if isinstance(exc, ModelOutputError) else ""
        raw_file = None
        if raw:
            stage = getattr(exc, "stage", "") or "model"
            raw_file = log.text(f"{stage}_response", raw)
        log.finish(f"{type(exc).__name__}: {exc}")
        print(f"\n{RED}{type(exc).__name__}{RESET}\n  {exc}")
        if raw_file is not None:
            print(f"\n{DIM}  What the model actually returned: {raw_file}"
                  f"\n  ({len(raw)} chars){RESET}")
        if not args.no_save:
            print(f"\n{DIM}  Stages that completed are in {log.directory}{RESET}")
        return 1

    log.finish()          # run() already recorded the calls and the proof

    if not args.quiet:
        show(state)
    print_outreach(state)
    print_proof(state, client, smart)
    write_deliverables(state, args.out)

    if not args.no_save:
        print(f"\n{DIM}  every stage written to {log.directory}{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def write_deliverables(state: RunState, out_dir: Path) -> None:
    """The point of the whole pipeline: files a person can open.

    Written next to the run folder rather than inside it, because a run folder
    is a debugging record and these are the output. The name carries the
    candidate and the role, so a folder of applications stays readable.
    """
    rendered = state.rendered
    if rendered is None or state.tailored is None:
        return

    head("RESUME — the files")
    out_dir.mkdir(parents=True, exist_ok=True)
    role = state.brief.role if state.brief else ""

    for data, suffix in ((rendered.docx, "docx"), (rendered.pdf, "pdf")):
        path = out_dir / filename_for(state.tailored, role, suffix)
        path.write_bytes(data)
        print(f"  {GREEN}{path}{RESET}")

    for check in rendered.checks:
        mark = f"{GREEN}PASS{RESET}" if check.is_clean else f"{YELLOW}CHECK{RESET}"
        print(f"  {mark}  {check.summary()}")
        for line in check.missing[:3]:
            print(f"{DIM}      unreadable: {line[:70]}{RESET}")
        for problem in check.ats_problems[:5]:
            print(f"{DIM}      ats: {problem[:90]}{RESET}")

    if rendered.is_clean:
        print(f"{DIM}  single column, no tables, standard headings, "
              f"ASCII only — read back with the same extractor "
              f"the pipeline uses on an upload{RESET}")


def print_outreach(state: RunState) -> None:
    """The draft, its recipient, and what is wrong with it — before anyone sends."""
    message = state.outreach
    if message is None:
        return

    head("OUTREACH — the draft, for a human to approve")
    print(f"  To:      {message.recipient or YELLOW + 'no address found' + RESET}")
    if len(message.recipient_candidates) > 1:
        others = ", ".join(message.recipient_candidates[1:4])
        print(f"{DIM}           alternatives: {others}{RESET}")
    print(f"  Subject: {message.subject}")
    print()
    for line in message.body.splitlines():
        print(f"    {line}")
    if message.cites:
        print(f"\n{DIM}  cites: {', '.join(message.cites)}{RESET}")

    if message.is_clean:
        print(f"\n  {GREEN}PASS{RESET}  grounded, no invented numbers, "
              f"claims nothing the resume cannot show")
    else:
        for problem in message.problems:
            print(f"\n  {YELLOW}CHECK{RESET} {problem}")
    print(f"\n{DIM}  Nothing is sent: S9 (approval) and S11 (send) are not built.{RESET}")
