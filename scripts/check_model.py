"""Is the configured model actually usable? Three probes, ~3 calls.

    python scripts/check_model.py

Run this after changing `.env`, before spending a rate limit finding out the
hard way. Each probe exists because the failure it catches is **silent** — the
run does not error, it just produces something subtly wrong three stages later.

1. REACHABLE   — the endpoint answers at all, with the configured model id.
2. SCHEMA      — the model honours a JSON schema. A provider that ignores
                 `response_format` returns prose, and the pipeline then burns
                 its retry on a shape problem that no prompt can fix.
3. CONTEXT     — the model can still see the START of a long prompt.

The third is the one that ruins an afternoon. Ollama picks a context size from
available VRAM (4k, 32k, 256k) and **silently truncates anything longer** — and
its OpenAI-compatible endpoint has no field for context length, so no client
can raise it per request. A parse window plus this project's system prompt is
around 2,200 tokens, so a 4k context is fine and a 2k one quietly eats half the
resume. There is no error. The only symptom is a parse that lost things.

The probe plants a marker at the very beginning of a long prompt and asks for
it back. If the model cannot repeat it, the beginning was cut off.

Fixes, when the context probe fails:

    OLLAMA_CONTEXT_LENGTH=8192 ollama serve      # server-wide, simplest
    ollama ps                                    # shows what actually loaded

`OLLAMA_NUM_CTX` is not a variable Ollama reads. It does nothing.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, Field

from app.agents.base import as_json, call_structured
from app.config import Settings, check
from app.domain.errors import JobTailorError
from app.io.llm import build_client
from app.io.schema import to_provider_schema

DIM, BOLD, GREEN, RED, YELLOW, RESET = (
    "\033[2m", "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[0m"
)

MARKER = "ZX-4417-QP"
# Comfortably longer than a parse window plus the system prompt, so a model
# that passes here has room for the real thing.
PROBE_TOKENS = 3000
FILLER = "The candidate worked on data pipelines for the reporting team. "


class Echo(BaseModel):
    marker: str = Field(description="The code you were asked to remember.")


class Shape(BaseModel):
    city: str = Field(description="The capital city named in the question.")
    year: int = Field(description="The year named in the question.")


def ok(label: str, detail: str = "") -> None:
    print(f"  {GREEN}PASS{RESET}  {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def bad(label: str, detail: str = "") -> None:
    print(f"  {RED}FAIL{RESET}  {label}" + (f"  {DIM}{detail}{RESET}" if detail else ""))


def probe_schema(client) -> bool:
    """Does the model return the requested shape, with the right types?"""
    try:
        answer = call_structured(
            client,
            system="Answer with the requested JSON object and nothing else.",
            user="The capital of France is Paris. The year is 1889.",
            schema_model=Shape, max_tokens=256, temperature=0.0, stage="probe",
        )
    except JobTailorError as exc:
        bad("schema", f"{type(exc).__name__}: {str(exc)[:70]}")
        return False

    if answer.city.strip().lower() != "paris" or answer.year != 1889:
        bad("schema", f"returned {answer.model_dump()} — shape held, content did not")
        return False
    ok("schema", "typed JSON came back correct")
    return True


def probe_context(client, target_tokens: int = PROBE_TOKENS) -> bool:
    """Can the model still see the beginning of a long prompt?

    A marker at position zero, filler after it, and a question at the end. If
    the answer is wrong, the front of the prompt was silently discarded.
    """
    filler = FILLER * max(1, (target_tokens * 4) // len(FILLER))
    user = as_json({
        "remember_this_code": MARKER,
        "notes": filler,
        "question": "Repeat the code from remember_this_code, exactly.",
    })
    approx = len(user) // 4
    try:
        answer = call_structured(
            client,
            system=f"Repeat the code you were given. It is {MARKER!r}-shaped.",
            user=user, schema_model=Echo, max_tokens=128, temperature=0.0,
            stage="probe",
        )
    except JobTailorError as exc:
        bad("context", f"{type(exc).__name__}: {str(exc)[:70]}")
        return False

    if MARKER not in answer.marker:
        bad("context", f"~{approx} tokens in, the start was gone "
                       f"(got {answer.marker[:24]!r})")
        print(f"\n  {YELLOW}The prompt was truncated before the model saw it.{RESET}")
        print("  Ollama picks a context size from free VRAM and cuts anything")
        print("  longer, with no error. Its OpenAI endpoint has no field for")
        print("  this, so no client can raise it per request. Fix the server:")
        print(f"\n    {BOLD}OLLAMA_CONTEXT_LENGTH=8192 ollama serve{RESET}")
        print(f"    {BOLD}ollama ps{RESET}   {DIM}# CONTEXT column shows what loaded{RESET}")
        print(f"\n  {DIM}OLLAMA_NUM_CTX is not read by Ollama. It does nothing.{RESET}")
        return False

    ok("context", f"~{approx} tokens, start still visible")
    return True


# A real run: three parse windows, then the brief, the plan and the prose.
CALLS_PER_RUN = 6
TOKENS_OUT_PER_CALL = 700


def probe_speed(client) -> float:
    """How fast is this model, and what does a whole run therefore cost?

    A local model is slow rather than broken, and the difference matters:
    "timed out" sends you looking for a fault, while "11.9 tokens a second"
    tells you to raise a number or pick a smaller model. Measured once here,
    up front, instead of discovered 45 seconds into a pipeline.
    """
    import time

    started = time.monotonic()
    try:
        call_structured(
            client,
            system="Answer with the requested JSON object and nothing else.",
            user="The capital of France is Paris. The year is 1889.",
            schema_model=Shape, max_tokens=256, temperature=0.0, stage="probe",
        )
    except JobTailorError:
        return 0.0
    elapsed = max(time.monotonic() - started, 1e-6)

    print(f"\n{BOLD}speed{RESET}")
    print(f"  {elapsed:.1f}s for a trivial call")

    # A short call is mostly fixed overhead, so this is a floor on what a real
    # one costs, not an estimate of it.
    run_seconds = elapsed * CALLS_PER_RUN
    if elapsed < 3:
        print(f"  {GREEN}fast{RESET} — a full run should be well under a minute")
    elif elapsed < 15:
        print(f"  {YELLOW}moderate{RESET} — a full run is likely several minutes")
        print(f"  {DIM}a real parse window is far bigger than this probe{RESET}")
    else:
        print(f"  {RED}slow{RESET} — at least {run_seconds / 60:.0f} minutes for a "
              f"full run, probably much more")
        print(f"\n  {DIM}Check that Ollama is on the GPU, not the CPU:{RESET}")
        print(f"    {BOLD}ollama ps{RESET}   {DIM}# PROCESSOR column{RESET}")
        print(f"  {DIM}A smaller model is the other lever.{RESET}")

    timeout = Settings().llm_timeout_seconds
    if elapsed * 8 > timeout:
        print(f"\n  {YELLOW}LLM_TIMEOUT_SECONDS is {timeout:.0f}.{RESET} A real parse "
              f"window is many times")
        print("  this probe, so raise it before the pipeline gives up mid-answer.")
    return elapsed


def main() -> int:
    logging.basicConfig(level=logging.WARNING,
                        format=f"{DIM}  %(levelname)-7s %(message)s{RESET}")
    settings = Settings()
    if problems := check(settings):
        print(f"{RED}Configuration:{RESET}\n  - " + "\n  - ".join(problems))
        return 2

    print(f"\n{BOLD}{settings.llm_model}{RESET}  "
          f"{DIM}via {settings.llm_provider}"
          f"{' at ' + settings.llm_base_url if settings.llm_base_url else ''}{RESET}")
    if settings.llm_reasoning_effort:
        print(f"{DIM}reasoning_effort={settings.llm_reasoning_effort}{RESET}")
    print()

    client = build_client(settings)

    try:
        reachable = probe_schema(client)
    except Exception as exc:                                  # noqa: BLE001
        bad("reachable", str(exc)[:80])
        return 1
    if not reachable:
        return 1

    passed = probe_context(client)
    probe_speed(client)

    if passed:
        print(f"\n  {GREEN}Ready.{RESET} Try:")
        print("    python scripts/run.py --resume samples/two_column.pdf --job job.txt")
    else:
        print(f"\n  {YELLOW}Fix the context size before running the pipeline —{RESET}")
        print("  a truncated prompt produces a parse that lost things, with no error.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
