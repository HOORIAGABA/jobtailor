"""Stage S0 on a real file: extract, parse, normalize, verify.

    python scripts/parse_resume.py path/to/your_resume.pdf
    python scripts/parse_resume.py your_resume.pdf --text-only   # no model call

`--text-only` runs S0.1 alone and prints the extracted text. That is the step
to look at first: if the reading order is wrong there, nothing after it can be
right, and it costs nothing to check.

The full run costs one model call.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.parse import parse_resume
from app.config import Settings, check
from app.domain.errors import (
    BudgetExceeded,
    ExtractionEmpty,
    LLMRateLimited,
    LLMUnavailable,
    SchemaValidationFailed,
    UnsupportedFileType,
)
from app.engine.normalize import normalize
from app.engine.parse_check import check_coverage
from app.io.extract import extract_text
from app.io.llm import BudgetedClient, RunBudget, build_client

DIM, BOLD, GREEN, RED, YELLOW, RESET = (
    "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
)


def head(text: str) -> None:
    print(f"\n{BOLD}{'─' * 70}\n{text}\n{'─' * 70}{RESET}")


def run(path: Path, text_only: bool, verbose: bool) -> int:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format=f"{DIM}  %(levelname)-7s %(name)-22s %(message)s{RESET}",
    )

    data = path.read_bytes()

    # ── S0.1 ──────────────────────────────────────────────────────────
    head(f"1. EXTRACT — {path.name} ({len(data) / 1024:.0f} KB)")
    text = extract_text(data, path.name)
    lines = text.splitlines()
    print(f"  {len(text)} characters, {len(lines)} lines\n")
    for line in lines[:40]:
        print(f"  {DIM}│{RESET} {line}")
    if len(lines) > 40:
        print(f"  {DIM}│ … {len(lines) - 40} more lines{RESET}")

    print(f"\n  {YELLOW}Read those lines in order.{RESET} If a skills line sits in the")
    print("  middle of a job's bullets, the columns were interleaved and the")
    print("  parse cannot recover — that is the bug app/engine/layout.py exists")
    print("  to prevent, and the place to look first if anything below is wrong.")

    if text_only:
        return 0

    # ── S0.2 ──────────────────────────────────────────────────────────
    settings = Settings()
    if problems := check(settings):
        print(f"\n{RED}Configuration:{RESET}\n  - " + "\n  - ".join(problems))
        return 2

    client = BudgetedClient(build_client(settings), RunBudget(max_calls=3))

    head("2. PARSE — one model call, temperature 0")
    raw = parse_resume(text, client)
    for section in raw.sections:
        print(f"  {BOLD}{section.heading or '(no heading)'}{RESET}")
        for entry in section.entries:
            label = " · ".join(p for p in (entry.title, entry.org, entry.dates) if p)
            print(f"    {label or '(untitled entry)'}")
            for bullet in entry.bullets:
                print(f"      {DIM}-{RESET} {bullet[:88]}")

    # ── S0.2 verification ─────────────────────────────────────────────
    head("3. VERIFY — did the parse keep the document?")
    coverage = check_coverage(text, raw)
    print(f"  {coverage.source_lines} source lines, {coverage.parsed_lines} parsed lines")

    if coverage.is_clean:
        print(f"\n  {GREEN}Nothing lost, nothing invented.{RESET}")
    else:
        for line in coverage.dropped:
            print(f"  {RED}missing {RESET} {line[:80]}")
        for line in coverage.invented:
            print(f"  {RED}invented{RESET} {line[:80]}")
        print(f"\n  {YELLOW}These are the lines the confirmation screen asks about.{RESET}")
        print("  Nothing downstream can detect them: every later guarantee is")
        print("  checked against this document, so a missing bullet just quietly")
        print("  stops being part of 'the original'.")

    # ── S0.3 ──────────────────────────────────────────────────────────
    head("4. NORMALIZE — ids and classification, in code")
    doc = normalize(raw)
    for section in doc.sections:
        kinds = f"{section.kind:<14}"
        print(f"  {DIM}{section.id:<20}{RESET} {kinds} {section.heading}")
        for item in section.items:
            dates = f"{item.date_start or '?'} → {item.date_end or 'present'}"
            print(f"    {DIM}{item.id:<18}{RESET} {item.title or '?':<28} {DIM}{dates}{RESET}")
            for bullet in item.bullets:
                print(f"      {DIM}{bullet.id:<16}{RESET} {bullet.text[:64]}")

    print(f"\n  {BOLD}grounding corpus{RESET} ({len(doc.skill_inventory)} terms)")
    print(f"    {', '.join(doc.skill_inventory) or DIM + 'empty — no skills section found' + RESET}")
    print(f"\n  {DIM}Every id above is permanent. Nothing may claim a skill outside")
    print(f"  that corpus.{RESET}")

    print(f"\n{DIM}  {client.budget.calls} model call(s), "
          f"{client.budget.tokens} tokens{RESET}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run stage S0 on a resume file.")
    parser.add_argument("path", type=Path, help="a .pdf, .docx, .txt or .md")
    parser.add_argument("--text-only", action="store_true",
                        help="stop after extraction; makes no model call")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"{RED}No such file: {args.path}{RESET}")
        return 2

    try:
        return run(args.path, args.text_only, args.verbose)
    except (ExtractionEmpty, UnsupportedFileType) as exc:
        print(f"\n{RED}Cannot read that file.{RESET}\n  {exc}")
    except LLMRateLimited as exc:
        print(f"\n{YELLOW}Rate limited.{RESET}\n  {exc}\n"
              f"  Free tiers allow a few calls a minute. Wait and re-run.")
    except LLMUnavailable as exc:
        print(f"\n{RED}The model is not reachable.{RESET}\n  {exc}\n"
              f"  Check LLM_MODEL in .env against your provider's model list.")
    except SchemaValidationFailed as exc:
        print(f"\n{RED}The model would not return the required shape.{RESET}\n  {exc}")
    except BudgetExceeded as exc:
        print(f"\n{RED}Budget exceeded.{RESET}\n  {exc}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
