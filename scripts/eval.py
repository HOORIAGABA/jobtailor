"""Run the eval cases and say what happened.

    python -m scripts.eval              every case, offline, zero API calls
    python -m scripts.eval --case fabricated_number
    python -m scripts.eval --live       against the configured model
    python -m scripts.eval --verbose    the observations, not just pass/fail

Exits non-zero if any case fails, so it can gate a commit.

**Offline is the default and is not a lesser mode.** The stored answers replay
through every stage's real parsing and validation, so what is exercised is the
deterministic half — the validator, the application, the diff, the ATS pass, the
renderer. That is where every guarantee this product makes actually lives. A
regression there is a broken promise; a change in the model's phrasing is a
Tuesday.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from evals.harness import CASES_DIR, Result, load_cases, run_case

GREEN, RED, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m")


def _live_client():
    """The configured model. Only built when asked for — it costs quota."""
    from app.config import Settings, require_valid
    from app.io.llm import build_client

    settings = Settings()
    require_valid(settings)
    return build_client(settings)


def _print(result: Result, verbose: bool) -> None:
    mark = f"{GREEN}pass{RESET}" if result.passed else f"{RED}FAIL{RESET}"
    print(f"  {mark}  {result.name}")

    if result.error:
        print(f"        {RED}{result.error}{RESET}")
        return
    for failure in result.failures:
        print(f"        {RED}{failure}{RESET}")

    if not verbose:
        return
    seen = result.observations
    print(f"        {DIM}refused    "
          f"{', '.join(f'{k}x{v}' for k, v in sorted(seen.refused.items())) or '-'}{RESET}")
    print(f"        {DIM}applied    "
          f"{', '.join(f'{k}x{v}' for k, v in sorted(seen.accepted.items())) or '-'}{RESET}")
    print(f"        {DIM}gaps       {', '.join(seen.gaps) or '-'}{RESET}")
    print(f"        {DIM}standing   "
          f"shown {len(seen.demonstrated)} / claimed {len(seen.declared_only)} "
          f"/ absent {len(seen.not_found)}{RESET}")
    print(f"        {DIM}bullets    {seen.bullets_before} -> {seen.bullets_after}"
          f"{'  LOST ' + str(seen.bullets_lost) if seen.bullets_lost else ''}{RESET}")
    print(f"        {DIM}ats        {'; '.join(seen.ats_problems) or 'clean'}{RESET}")
    print(f"        {DIM}message    "
          f"{'; '.join(seen.outreach_problems) or 'clean'}{RESET}")
    print(f"        {DIM}render     "
          f"{'verified' if seen.render_clean else 'NOT verified'}{RESET}")


def main() -> int:
    # The stages log their own warnings, and a stage complaining about a draft
    # is an OBSERVATION this harness records — not a line that belongs in the
    # middle of the report. Anything a case should be judged on is in
    # `Observations`; anything else is noise here.
    logging.disable(logging.WARNING)

    parser = argparse.ArgumentParser(description="Run the eval cases.")
    parser.add_argument("--case", default="", help="run one case by name")
    parser.add_argument("--live", action="store_true",
                        help="use the configured model instead of the stored "
                             "answers. Spends quota.")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--dir", default=str(CASES_DIR))
    args = parser.parse_args()

    cases = load_cases(Path(args.dir))
    if args.case:
        cases = [c for c in cases if c["name"] == args.case]
        if not cases:
            print(f"no case named {args.case!r}")
            return 2
    if not cases:
        print(f"no cases in {args.dir}")
        return 2

    client = _live_client() if args.live else None
    where = "against the configured model" if args.live else "offline"
    print(f"\n{BOLD}{len(cases)} case(s), {where}{RESET}\n")

    results = [run_case(case, client) for case in cases]
    for result in results:
        _print(result, args.verbose)

    failed = [r for r in results if not r.passed]
    print()
    if failed:
        print(f"{RED}{len(failed)} of {len(results)} failed{RESET}: "
              f"{', '.join(r.name for r in failed)}\n")
        return 1
    print(f"{GREEN}all {len(results)} passed{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
