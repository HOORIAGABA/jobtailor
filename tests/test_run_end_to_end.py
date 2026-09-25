"""★ The whole run, S1 → S10, with a scripted model and no network.

This closes the two gaps that had stood open longest: **S4 (the writer) had
never executed**, and **S5 (the validator) had never refused anything on a live
run** — only on hand-written fixtures fed straight to `validate()`.

The difference matters. A fixture proves the validator's logic. This proves the
*wiring*: that the planner's targets reach the writer, that the writer's prose
comes back attached to the right bullet ids, that the validator sees the written
text rather than the planner's empty placeholder, that a refusal removes the
operation from what gets applied, and that the refusal survives into the diff a
person reads at the gate.

**The model is scripted, not mocked.** `ScriptedClient` answers whatever schema
it is handed with a real JSON object, so every stage runs its own parsing,
validation and retry logic exactly as it would against a provider. What is
removed is the network and the non-determinism, not the code path.

The scenario is chosen so the validator must refuse:

    the resume says   "Built a dashboard that saved the team a day a week"
    the writer says   "Cut reporting latency by 40% by rebuilding the dashboard"

`40%` appears nowhere in the resume. That is a Class A claim — an asserted
number — and Class A must appear verbatim in the origin. It has to be refused,
the original line has to survive, and the person has to be told.
"""
from __future__ import annotations

import json

import pytest

from app.domain.models import Contact, RawEntry, RawResume, RawSection
from app.engine.normalize import normalize
from app.io.llm import LLMResponse
from app.pipeline.run import tailor

# ── the candidate ─────────────────────────────────────────────────────

RESUME = RawResume(
    contact=Contact(full_name="R. Khan", email="r.khan@example.com",
                    phone="+1 555 0100", location="Remote",
                    linkedin="", github="", website=""),
    summary="Data analyst working on reporting and internal tooling.",
    sections=[
        RawSection(heading="WORK EXPERIENCE", entries=[
            RawEntry(title="Data Analyst", org="Acme Corp",
                     dates="Jan 2022 - Present", bullets=[
                         "Built a dashboard that saved the team a day a week",
                         "Wrote Python scripts to load data into PostgreSQL",
                     ]),
            RawEntry(title="Intern", org="Beta Labs", dates="2021", bullets=[
                "Helped migrate reports off a legacy system",
            ]),
        ]),
        RawSection(heading="TECHNICAL SKILLS", entries=[
            RawEntry(bullets=["Languages: Python, SQL",
                              "Tools: PostgreSQL, Docker"]),
        ]),
    ],
)

POSTING = """
Backend Engineer — Annova

We are hiring a Backend Engineer to own our data platform. You will build and
maintain services in Python and FastAPI, and work with PostgreSQL at scale.

Requirements
- Strong proficiency in Python
- Experience with FastAPI or a similar framework
- Comfortable with PostgreSQL
- Kubernetes experience is a plus

Send your application to careers@annova.example.com.
"""

# The bullet the planner targets and the writer rewrites.
TARGET = "exp.1.b.1"

# ── the scripted model ────────────────────────────────────────────────

BRIEF = {
    "company": "Annova",
    "role": "Backend Engineer",
    "seniority": "mid",
    "tone": "direct",
    "role_narrative": (
        "Own the data platform. Build and maintain Python services. Work with "
        "PostgreSQL at scale. Support the team's reporting needs."
    ),
    "problems_to_solve": [
        {"statement": "The data platform needs an owner.",
         "start": 0, "end": 60},
    ],
    "success_signals": [
        {"statement": "Services in Python and FastAPI are maintained.",
         "start": 0, "end": 60},
    ],
    "hard_requirements": [
        {"statement": "Strong proficiency in Python", "start": 0, "end": 30},
        {"statement": "Experience with FastAPI", "start": 0, "end": 30},
        {"statement": "Comfortable with PostgreSQL", "start": 0, "end": 30},
    ],
    "terms": [
        {"term": "Python", "kind": "skill", "aliases": [], "required": True,
         "weight": 10},
        {"term": "FastAPI", "kind": "tool", "aliases": [], "required": True,
         "weight": 8},
        {"term": "PostgreSQL", "kind": "tool", "aliases": ["postgres"],
         "required": True, "weight": 7},
        {"term": "Kubernetes", "kind": "tool", "aliases": ["k8s"],
         "required": False, "weight": 3},
    ],
}

PLAN = {"ops": [
    {"op": "set_summary",
     "text": "Backend-leaning data analyst who builds Python services and "
             "works with PostgreSQL.",
     "rationale": "The posting leads with Python and PostgreSQL.",
     "cites": ["exp.1.b.2"]},
    {"op": "rewrite_bullet", "bullet_id": TARGET, "text": "",
     "target_terms": ["Python", "PostgreSQL"],
     "rationale": "State the reporting work in the platform language the "
                  "posting uses."},
    {"op": "flag_gap", "requirement": "Kubernetes",
     "severity": "minor",
     "closest_evidence": []},
]}

# ★ The fabricated number. `40%` is nowhere in the resume.
FABRICATED = "Cut reporting latency by 40% by rebuilding the team dashboard"

WRITTEN = {"bullets": [{"bullet_id": TARGET, "text": FABRICATED}]}

OUTREACH = {
    "subject": "Backend Engineer — R. Khan",
    "body": ("Hello,\n\nI build reporting tooling in Python and load data into "
             "PostgreSQL. The attached resume has the detail.\n\nR. Khan"),
    "cites": ["exp.1.b.2"],
}


class ScriptedClient:
    """Answers each stage by the shape of the schema it is handed.

    Dispatching on the schema rather than on call order is deliberate: the
    stages may be reordered, cached, or skipped, and a client that counts calls
    would answer the wrong stage the first time any of that happened.
    """

    def __init__(self, **overrides: dict) -> None:
        self.answers = {"brief": BRIEF, "plan": PLAN, "written": WRITTEN,
                        "outreach": OUTREACH}
        self.answers.update(overrides)
        self.asked: list[str] = []

    def complete(self, *, system: str, user: str, schema=None,
                 max_tokens: int = 2048, temperature: float = 0.2,
                 stage: str = "") -> LLMResponse:
        keys = set((schema or {}).get("properties", {}))
        if "role_narrative" in keys:
            name = "brief"
        elif keys == {"ops"}:
            name = "plan"
        elif keys == {"bullets"}:
            name = "written"
        elif "subject" in keys and "body" in keys:
            name = "outreach"
        else:                                             # pragma: no cover
            raise AssertionError(f"unscripted stage, schema keys {keys}")

        self.asked.append(name)
        body = json.dumps(self.answers[name])
        return LLMResponse(text=body, prompt_tokens=len(user) // 4,
                           completion_tokens=len(body) // 4,
                           finish_reason="stop")


@pytest.fixture
def state():
    doc = normalize(RESUME)
    return tailor(doc, POSTING, ScriptedClient(), write_outreach=True,
                  render_output=True)


# ══ S4 executed ═══════════════════════════════════════════════════════

def test_every_stage_that_needs_a_model_was_asked():
    """★ Before this test, S4 had never run. Not once, in any environment."""
    client = ScriptedClient()
    tailor(normalize(RESUME), POSTING, client, write_outreach=True)
    assert client.asked == ["brief", "plan", "written", "outreach"]


def test_the_writer_filled_the_target_the_planner_named(state):
    """The planner emits `text=""` — a target, not prose. If the writer's output
    never reaches the op, the validator sees an empty string and the whole
    rewrite path is dead code that still passes its unit tests."""
    rewrites = [op for op in state.ops
                if getattr(op, "op", "") == "rewrite_bullet"]
    assert len(rewrites) == 1
    assert rewrites[0].bullet_id == TARGET
    assert rewrites[0].text == FABRICATED       # written, not empty


# ══ S5 refused ════════════════════════════════════════════════════════

def test_the_fabricated_number_is_refused_on_a_live_run(state):
    """★ Before this test, S5 had never refused anything outside a fixture.

    `40%` appears nowhere in the resume. A Class A claim — an asserted number —
    must appear verbatim in the origin, and inference is not allowed to supply
    it.
    """
    refused = state.rejected
    assert refused, "the validator accepted a number that is not in the resume"
    assert {r.op_kind for r in refused} == {"rewrite_bullet"}
    reasons = " ".join(f"{r.code} {r.detail}" for r in refused).lower()
    assert "40" in reasons or "number" in reasons


def test_the_refused_rewrite_did_not_reach_the_document(state):
    """A refusal that still lands is not a refusal."""
    text = json.dumps(state.tailored.model_dump())
    assert "40%" not in text
    assert "saved the team a day a week" in text       # the original survives


def test_the_run_did_not_fail_because_of_the_refusal(state):
    """A rejection is never a run failure. The original is kept and the run
    continues — otherwise one bad sentence costs the whole application."""
    assert state.tailored is not None
    assert state.diff is not None
    assert state.rendered is not None


def test_the_operations_that_passed_were_applied(state):
    """The refusal must be surgical: everything else still lands."""
    summary = state.tailored.summary
    assert "PostgreSQL" in summary
    accepted_kinds = {getattr(op, "op", "") for op in state.accepted}
    assert "set_summary" in accepted_kinds
    assert "rewrite_bullet" not in accepted_kinds


# ══ the refusal reaches the person ════════════════════════════════════

def test_the_refusal_is_visible_in_the_diff(state):
    """A refusal you cannot see is a refusal you cannot check."""
    payload = state.diff.model_dump() if hasattr(state.diff, "model_dump") \
        else state.diff
    rejections = payload.get("rejections") or []
    assert rejections, "the gate would show nothing about the refused rewrite"


def test_the_gap_the_resume_cannot_cover_is_reported_not_invented(state):
    """Kubernetes is in the posting and in no bullet. The honest answer is a
    gap; the dishonest one is a skills-list entry."""
    payload = state.diff.model_dump() if hasattr(state.diff, "model_dump") \
        else state.diff
    gaps = json.dumps(payload.get("gaps") or [])
    assert "Kubernetes" in gaps
    assert "Kubernetes" not in json.dumps(state.tailored.model_dump())


# ══ the honest path still works ═══════════════════════════════════════

def test_a_rewrite_grounded_in_the_resume_is_accepted():
    """The mirror image: the same pipeline, a claim the resume supports.

    Without this, "the validator refuses things" could be satisfied by a
    validator that refuses everything.
    """
    honest = dict(WRITTEN)
    honest = {"bullets": [{
        "bullet_id": TARGET,
        "text": "Built a reporting dashboard that saved the team a day a week",
    }]}
    state = tailor(normalize(RESUME), POSTING,
                   ScriptedClient(written=honest), render_output=False)

    assert not [r for r in state.rejected if r.op_kind == "rewrite_bullet"]
    assert "reporting dashboard" in json.dumps(state.tailored.model_dump())


def test_the_deliverables_are_produced_and_read_back(state):
    """S10 renders and then re-reads its own output. An unverified render is a
    claim."""
    assert state.rendered.docx[:2] == b"PK"          # a real zip container
    assert state.rendered.pdf[:5] == b"%PDF-"
    assert state.rendered.is_clean


def test_the_outreach_draft_does_not_claim_the_gap(state):
    """S8's message check: the letter must not assert what the standing
    analysis put in `not_found`."""
    assert state.outreach is not None
    assert "Kubernetes" not in state.outreach.body
