"""Stage S0 on real files: extract, parse, verify, normalize — and save it all.

    python scripts/parse_resume.py samples/two_column.pdf
    python scripts/parse_resume.py samples/ --text-only     # whole folder, no calls
    python scripts/parse_resume.py my_cv.pdf --no-save      # terminal only

Every run writes a folder under `runs/` holding each stage's output, because a
failure at S5 is usually explained by the S0 text that has already scrolled
away. `--text-only` stops after extraction and costs nothing, which is the
right first check on any new resume: if the reading order is wrong there,
nothing after it can be right.

Pointed at a directory, it processes every resume in it and prints one line
each. That is the regression view — the same twenty resumes after a prompt
change, and you can see which ones moved.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents.parse import parse_resume
from app.config import Settings, check
from app.domain.errors import (
    BudgetExceeded,
    ExtractionEmpty,
    JobTailorError,
    LLMRateLimited,
    LLMUnavailable,
    SchemaValidationFailed,
    UnsupportedFileType,
)
from app.engine.normalize import normalize
from app.engine.parse_check import check_coverage
from app.io.extract import SUPPORTED, extract_text
from app.io.llm import BudgetedClient, RunBudget, build_client
from app.io.runlog import NullRunLog, RunLog

DIM, BOLD, GREEN, RED, YELLOW, RESET = (
    "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
)

# Free tiers commonly allow 5 requests a minute. One parse is one call, so a
# folder of resumes hits the limit almost immediately without a pause.
BATCH_PAUSE_SECONDS = 13.0


def head(text: str) -> None:
    print(f"\n{BOLD}{'─' * 70}\n{text}\n{'─' * 70}{RESET}")


# ── one resume ────────────────────────────────────────────────────────

def process(
    path: Path,
    *,
    text_only: bool,
    log: RunLog,
    settings: Settings | None,
    quiet: bool = False,
    client=None,
) -> dict:
    """Run S0 over one file. Returns a summary row; writes artifacts to `log`.

    `client` is injectable so the whole path — extract, parse, verify,
    normalize, and every artifact written — is testable with a scripted model
    and no network. A debugging tool that is itself only verified by running it
    is not much of a tool.
    """
    data = path.read_bytes()
    log.input_file(path, data)
    summary: dict = {"file": path.name}

    # ── S0.1 ──────────────────────────────────────────────────────────
    text = extract_text(data, path.name)
    log.text("extract", text)
    lines = text.splitlines()
    summary |= {"chars": len(text), "lines": len(lines)}

    if not quiet:
        head(f"1. EXTRACT — {path.name} ({len(data) / 1024:.0f} KB)")
        print(f"  {len(text)} characters, {len(lines)} lines\n")
        for line in lines[:40]:
            print(f"  {DIM}│{RESET} {line}")
        if len(lines) > 40:
            print(f"  {DIM}│ … {len(lines) - 40} more lines{RESET}")
        print(f"\n  {YELLOW}Read those lines in order.{RESET} A skills line sitting in the")
        print("  middle of a job's bullets means the columns were interleaved,")
        print("  and no later stage can recover from that.")

    if text_only:
        return summary

    # ── S0.2 ──────────────────────────────────────────────────────────
    if client is None:
        assert settings is not None
        client = BudgetedClient(build_client(settings), RunBudget(max_calls=3))
    if settings is not None:
        log.note(provider=settings.llm_provider, model=settings.llm_model)

    raw = parse_resume(text, client)
    log.json("parse", raw)

    entries = sum(len(s.entries) for s in raw.sections)
    bullets = sum(len(e.bullets) for s in raw.sections for e in s.entries)
    summary |= {"sections": len(raw.sections), "entries": entries, "bullets": bullets}

    if not quiet:
        head("2. PARSE — one model call, temperature 0")
        for section in raw.sections:
            print(f"  {BOLD}{section.heading or '(no heading)'}{RESET}")
            for entry in section.entries:
                label = " · ".join(p for p in (entry.title, entry.org, entry.dates) if p)
                print(f"    {label or '(untitled entry)'}")
                for bullet in entry.bullets:
                    print(f"      {DIM}-{RESET} {bullet[:86]}")

    # ── S0.2v ─────────────────────────────────────────────────────────
    coverage = check_coverage(text, raw)
    log.json("coverage", coverage)
    summary |= {"dropped": len(coverage.dropped), "invented": len(coverage.invented)}

    if not quiet:
        head("3. VERIFY — did the parse keep the document?")
        print(f"  {coverage.source_lines} source lines, "
              f"{coverage.parsed_lines} parsed lines")
        if coverage.is_clean:
            print(f"\n  {GREEN}Nothing lost, nothing invented.{RESET}")
        else:
            for line in coverage.dropped:
                print(f"  {RED}missing {RESET} {line[:80]}")
            for line in coverage.invented:
                print(f"  {RED}invented{RESET} {line[:80]}")
            print(f"\n  {YELLOW}These are what the confirmation screen asks about.{RESET}")
            print("  No later stage can detect them: every guarantee downstream")
            print("  is checked against this document, so a missing bullet just")
            print("  quietly stops being part of 'the original'.")

    # ── S0.3 ──────────────────────────────────────────────────────────
    doc = normalize(raw)
    log.json("normalize", doc)
    summary["skills"] = len(doc.skill_inventory)

    if not quiet:
        head("4. NORMALIZE — ids and classification, in code")
        for section in doc.sections:
            print(f"  {DIM}{section.id:<20}{RESET} {section.kind:<14} {section.heading}")
            for item in section.items:
                dates = f"{item.date_start or '?'} → {item.date_end or 'present'}"
                print(f"    {DIM}{item.id:<18}{RESET} {item.title or '?':<26} "
                      f"{DIM}{dates}{RESET}")
                for bullet in item.bullets:
                    print(f"      {DIM}{bullet.id:<16}{RESET} {bullet.text[:62]}")
        print(f"\n  {BOLD}grounding corpus{RESET} ({len(doc.skill_inventory)} terms)")
        print("    " + (", ".join(doc.skill_inventory)
                        or f"{DIM}empty — no skills section found{RESET}"))

    log.note(calls=client.budget.calls, tokens=client.budget.tokens,
             call_log=client.log)
    summary |= {"calls": client.budget.calls, "tokens": client.budget.tokens}
    return summary


# ── a folder of resumes ───────────────────────────────────────────────

# `.md` is a supported resume format, so a folder's own README would otherwise
# be parsed as somebody's CV — and then reported as a resume that lost every
# line. Documentation and dotfiles are skipped by name.
_NOT_A_RESUME = {"readme", "license", "licence", "changelog", "contributing"}


def resumes_in(directory: Path) -> list[Path]:
    return sorted(
        p for p in directory.iterdir()
        if p.is_file()
        and p.suffix.lower() in SUPPORTED
        and not p.name.startswith(".")
        and p.stem.lower() not in _NOT_A_RESUME
    )


def print_table(rows: list[dict], text_only: bool) -> None:
    head(f"SUMMARY — {len(rows)} file(s)")
    if text_only:
        columns = [("file", 34), ("chars", 8), ("lines", 7), ("status", 30)]
    else:
        columns = [("file", 28), ("lines", 6), ("entries", 8), ("bullets", 8),
                   ("dropped", 8), ("invented", 9), ("status", 18)]

    print("  " + "".join(f"{BOLD}{name:<{width}}{RESET}" for name, width in columns))
    for row in rows:
        cells = []
        for name, width in columns:
            value = row.get(name, "")
            colour = ""
            if name in ("dropped", "invented") and isinstance(value, int) and value:
                colour = RED
            if name == "status" and value.startswith("ok"):
                colour = GREEN
            elif name == "status" and value:
                colour = RED
            cells.append(f"{colour}{str(value):<{width}}{RESET}")
        print("  " + "".join(cells))


# ── entry point ───────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Run stage S0 on resumes.")
    parser.add_argument("path", type=Path, help="a resume file, or a folder of them")
    parser.add_argument("--text-only", action="store_true",
                        help="stop after extraction; makes no model call")
    parser.add_argument("--no-save", action="store_true",
                        help="print only; write no run folder")
    parser.add_argument("--runs", type=Path, default=Path("runs"),
                        help="where run folders go (default: runs/)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format=f"{DIM}  %(levelname)-7s %(name)-22s %(message)s{RESET}",
    )

    if not args.path.exists():
        print(f"{RED}No such file or folder: {args.path}{RESET}")
        return 2

    if args.path.is_dir():
        files = resumes_in(args.path)
        if not files:
            print(f"{RED}No readable resumes in {args.path}{RESET}")
            print(f"  Looking for: {', '.join(SUPPORTED)}")
            return 2
    else:
        files = [args.path]

    settings = None
    if not args.text_only:
        settings = Settings()
        if problems := check(settings):
            print(f"\n{RED}Configuration:{RESET}\n  - " + "\n  - ".join(problems))
            return 2

    batch = len(files) > 1
    rows: list[dict] = []
    folders: list[Path] = []

    for n, file in enumerate(files):
        if batch and n and not args.text_only:
            print(f"{DIM}  pausing {BATCH_PAUSE_SECONDS:.0f}s for the rate limit…{RESET}")
            time.sleep(BATCH_PAUSE_SECONDS)

        log: RunLog = NullRunLog() if args.no_save else RunLog.create(
            file.stem, root=args.runs
        )
        row: dict = {"file": file.name, "status": ""}
        try:
            row |= process(file, text_only=args.text_only, log=log,
                           settings=settings, quiet=batch)
            row["status"] = "ok"
            log.finish()
        except (ExtractionEmpty, UnsupportedFileType) as exc:
            row["status"] = "unreadable"
            log.finish(str(exc))
            report(file, "Cannot read that file.", exc, batch)
        except LLMRateLimited as exc:
            row["status"] = "rate limited"
            log.finish(str(exc))
            report(file, "Rate limited — free tiers allow a few calls a minute.",
                   exc, batch)
        except LLMUnavailable as exc:
            row["status"] = "model error"
            log.finish(str(exc))
            report(file, "The model is not reachable. Check LLM_MODEL in .env.",
                   exc, batch)
        except SchemaValidationFailed as exc:
            row["status"] = "bad shape"
            log.finish(str(exc))
            report(file, "The model would not return the required shape.", exc, batch)
        except (BudgetExceeded, JobTailorError) as exc:
            row["status"] = type(exc).__name__
            log.finish(str(exc))
            report(file, "Run stopped.", exc, batch)

        rows.append(row)
        if not args.no_save:
            folders.append(log.directory)

    if batch:
        print_table(rows, args.text_only)

    if folders:
        print(f"\n{DIM}  artifacts written to:{RESET}")
        for folder in folders[:6]:
            print(f"    {folder}")
        if len(folders) > 6:
            print(f"    {DIM}… and {len(folders) - 6} more{RESET}")
        print(f"\n{DIM}  Each folder holds every stage's output. Diff two of them")
        print(f"  after a prompt change to see exactly what moved.{RESET}")

    return 0 if all(r["status"] == "ok" for r in rows) else 1


def report(file: Path, headline: str, exc: Exception, batch: bool) -> None:
    if batch:
        return                              # the table carries it
    print(f"\n{RED}{headline}{RESET}\n  {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
