"""Run stored cases through the pipeline and report what happened.

**There is no score.** Every other part of this project refuses to produce a
maximisable number, and an eval harness is the place where that refusal is most
tempting to abandon — a single "quality: 82%" is so much easier to put in a
README. It is also the number that, the moment it exists, the prompts get tuned
against, and tuning a prompt against a number computed from the same model's
output is how a system learns to look good rather than be good.

So a case reports **observations** (what was refused, what was accepted, what
was reported as a gap, what the ATS pass complained about) and **assertions**
(things that must be true, named in the case file). A case passes or fails. A
suite reports counts. Nothing averages.

**Two modes, and the offline one is the default.**

    offline   the case carries the model's answers; zero API calls, runs in CI
    live      a real client; for measuring the parts a script cannot

Offline mode is not a lesser version. What it tests is the *deterministic* half
— the validator, the application, the diff, the ATS pass, the renderer — which
is where every guarantee this product makes actually lives. A regression there
is a broken promise; a regression in the model's phrasing is a Tuesday.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.domain.models import RawResume
from app.engine.normalize import normalize
from app.io.llm import LLMResponse
from app.pipeline.run import tailor

CASES_DIR = Path(__file__).resolve().parent / "cases"

# Which stage a schema belongs to, by the keys it asks for. Dispatching on the
# schema rather than on call order survives stages being reordered, cached or
# skipped — a client that counted calls would answer the wrong stage the first
# time any of that happened.
def _stage_of(schema: dict[str, Any] | None) -> str:
    keys = set((schema or {}).get("properties", {}))
    if "role_narrative" in keys:
        return "brief"
    if keys == {"ops"}:
        return "plan"
    if keys == {"bullets"}:
        return "written"
    if {"subject", "body"} <= keys:
        return "outreach"
    if "sections" in keys:
        return "parse"
    return "unknown"


class ScriptedClient:
    """Replays a case's stored answers. Every stage's own parsing still runs."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def complete(self, *, system: str, user: str, schema=None,
                 max_tokens: int = 2048, temperature: float = 0.2,
                 stage: str = "") -> LLMResponse:
        name = _stage_of(schema)
        if name not in self.answers:
            raise KeyError(
                f"this case has no stored answer for the {name!r} stage; "
                f"record one, or run the case live"
            )
        self.asked.append(name)
        body = json.dumps(self.answers[name])
        return LLMResponse(text=body, prompt_tokens=len(user) // 4,
                           completion_tokens=len(body) // 4,
                           finish_reason="stop")


@dataclass
class Observations:
    """What the run did. Descriptive — nothing here is a target."""
    refused: dict[str, int] = field(default_factory=dict)
    accepted: dict[str, int] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    demonstrated: list[str] = field(default_factory=list)
    declared_only: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    ats_problems: list[str] = field(default_factory=list)
    # S8's own complaints about the draft it produced. Captured rather than
    # logged: the message check is a guarantee like any other, and a guarantee
    # whose output goes to stderr is one nobody is holding anything to. The
    # first run of this suite printed "only 36 words; too thin to say anything
    # specific" four times and recorded none of it.
    outreach_problems: list[str] = field(default_factory=list)
    render_clean: bool = False
    bullets_before: int = 0
    bullets_after: int = 0

    @property
    def bullets_lost(self) -> int:
        return max(0, self.bullets_before - self.bullets_after)


@dataclass
class Result:
    name: str
    observations: Observations
    failures: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def passed(self) -> bool:
        return not self.failures and not self.error


def load_cases(directory: Path = CASES_DIR) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        case.setdefault("name", path.stem)
        cases.append(case)
    return cases


def run_case(case: dict[str, Any], client=None) -> Result:
    """One case. `client` overrides the stored answers, for a live run."""
    name = case.get("name", "unnamed")
    try:
        doc = normalize(RawResume.model_validate(case["resume"]))
        state = tailor(
            doc, case["posting"],
            client or ScriptedClient(case.get("model", {})),
            write_outreach=bool(case.get("model", {}).get("outreach")),
            render_output=True,
        )
    except Exception as exc:                                  # noqa: BLE001
        return Result(name=name, observations=Observations(),
                      error=f"{type(exc).__name__}: {exc}")

    seen = _observe(doc, state)
    return Result(name=name, observations=seen,
                  failures=_check(case.get("expect", {}), seen, state))


def _observe(base, state) -> Observations:
    from app.engine.ats import problems
    from app.engine.evidence import terms_by_standing

    seen = Observations()
    for reject in state.rejected:
        seen.refused[reject.code] = seen.refused.get(reject.code, 0) + 1
    for op in state.accepted:
        kind = getattr(op, "op", "?")
        seen.accepted[kind] = seen.accepted.get(kind, 0) + 1

    diff = state.diff
    if diff is not None:
        payload = diff.model_dump() if hasattr(diff, "model_dump") else diff
        seen.gaps = [str(g.get("requirement", "")) for g in
                     (payload.get("gaps") or [])]
        seen.questions = [str(q) for q in (payload.get("questions") or [])]

    if state.brief is not None and state.doc is not None and state.index:
        standing = terms_by_standing(state.brief, state.doc, state.index)
        seen.demonstrated = list(standing.get("demonstrated", []))
        seen.declared_only = list(standing.get("declared_only", []))
        seen.not_found = list(standing.get("not_found", []))

    seen.bullets_before = _count_bullets(base)
    if state.tailored is not None:
        seen.bullets_after = _count_bullets(state.tailored)
        seen.ats_problems = list(problems(_ats_resume(state)))
    if state.outreach is not None:
        seen.outreach_problems = list(state.outreach.problems)
    if state.rendered is not None:
        seen.render_clean = state.rendered.is_clean
    return seen


def _ats_resume(state):
    from app.engine.ats import build
    return build(state.tailored, "")


def _count_bullets(doc) -> int:
    return sum(len(item.bullets) for section in doc.sections
               for item in section.items)


def _check(expect: dict[str, Any], seen: Observations, state) -> list[str]:
    """Compare against what the case says must be true.

    Each key is a separate claim, and every one that fails is reported — a
    harness that stops at the first failure makes you run it once per problem.
    """
    failures: list[str] = []
    tailored = json.dumps(state.tailored.model_dump()) if state.tailored else ""

    for rule in expect.get("refusals", []):
        if rule not in seen.refused:
            failures.append(
                f"expected a {rule!r} refusal; refusals were "
                f"{sorted(seen.refused) or 'none'}")

    for kind in expect.get("accepts", []):
        if kind not in seen.accepted:
            failures.append(
                f"expected {kind!r} to be applied; applied were "
                f"{sorted(seen.accepted) or 'none'}")

    for kind in expect.get("rejects_op", []):
        if kind in seen.accepted:
            failures.append(f"{kind!r} was applied and should not have been")

    for text in expect.get("absent_from_resume", []):
        if text in tailored:
            failures.append(f"{text!r} reached the tailored resume")

    for text in expect.get("present_in_resume", []):
        if text not in tailored:
            failures.append(f"{text!r} is missing from the tailored resume")

    for requirement in expect.get("gaps_include", []):
        if not any(requirement.lower() in gap.lower() for gap in seen.gaps):
            failures.append(
                f"expected a gap for {requirement!r}; gaps were {seen.gaps}")

    if expect.get("render_clean") and not seen.render_clean:
        failures.append("the rendered files did not survive being read back")

    ceiling = expect.get("max_bullets_lost")
    if ceiling is not None and seen.bullets_lost > ceiling:
        failures.append(
            f"lost {seen.bullets_lost} bullets, ceiling is {ceiling}")

    if expect.get("no_ats_problems") and seen.ats_problems:
        failures.append(f"ATS problems: {seen.ats_problems}")

    for fragment in expect.get("outreach_problems_include", []):
        if not any(fragment.lower() in p.lower()
                   for p in seen.outreach_problems):
            failures.append(
                f"expected the message check to complain about {fragment!r}; "
                f"it said {seen.outreach_problems or 'nothing'}")

    if expect.get("no_outreach_problems") and seen.outreach_problems:
        failures.append(f"outreach problems: {seen.outreach_problems}")

    return failures


def run_all(directory: Path = CASES_DIR, client=None) -> list[Result]:
    return [run_case(case, client) for case in load_cases(directory)]
