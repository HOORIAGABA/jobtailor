"""Seed a public demo with a synthetic candidate. No model calls, no real people.

    python -m scripts.demo_seed
    python -m scripts.demo_seed --reset       # replace an existing demo

**Why synthetic.** The obvious way to fill a demo is to seed whatever is in the
developer's own database, and the developer's own database holds their real CV:
a name, an address, a phone number, an employment history. A portfolio link is a
public link. Nobody should have to choose between showing their work and
publishing their personal data, so this invents a candidate instead.

**The run is real even though the model is not.** This does not write a diff by
hand. It builds a resume, runs the ACTUAL pipeline — S1 through S10, the same
`tailor()` the product calls — and persists whatever comes out. A scripted
client supplies the model's four answers, so it costs nothing and is identical
every time, but the evidence matching, the validation, the application, the
diff, the ATS pass and the render are all genuinely computed.

That matters for a demo more than it looks. The refusal on the gate screen is
not a decorative example: the writer really does propose "cut processing time by
35%", that number really is absent from the resume, and S5 really does refuse
it. Someone reading the screen is looking at the guarantee working, not at a
mock-up of it working.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime, timezone

from app.db import models
from app.db.session import session_scope
from app.domain.models import RawResume
from app.engine.normalize import normalize
from app.io import render
from app.pipeline.run import tailor

logger = logging.getLogger("demo")

DEMO_EMAIL = "demo@jobtailor.example"
DEMO_SUB = "demo-synthetic-candidate"

# ── the candidate ─────────────────────────────────────────────────────
#
# Plausible rather than impressive. A demo built around a flawless CV shows
# nothing, because the product's entire job is to be honest about the gap
# between a person and a posting — and a candidate with no gaps has no gap to be
# honest about. This one is two years in, genuinely good at part of the role and
# genuinely missing another part.

RESUME = RawResume.model_validate({
    "contact": {
        "full_name": "Priya Raman",
        "email": "priya.raman@example.com",
        "phone": "+44 20 7946 0102",
        "location": "Manchester, UK",
        "linkedin": "linkedin.com/in/priya-raman-example",
        "github": "github.com/priya-raman-example",
        "website": "",
    },
    "summary": "Data analyst who builds internal tooling and reporting.",
    "sections": [
        {"heading": "EXPERIENCE", "entries": [
            {"title": "Data Analyst", "org": "Northwind Retail",
             "dates": "Mar 2023 - Present", "bullets": [
                 "Built a reporting dashboard that replaced a manual "
                 "spreadsheet the team rebuilt every Monday",
                 "Wrote Python jobs that load sales data into PostgreSQL "
                 "each night",
                 "Rewrote the weekly stock report after the warehouse team "
                 "said they could not read the old one",
             ]},
            {"title": "Junior Analyst", "org": "Brightside Insurance",
             "dates": "Sep 2021 - Feb 2023", "bullets": [
                 "Contributed to a migration off a legacy reporting system",
                 "Answered ad-hoc data questions for the claims team",
             ]},
        ]},
        {"heading": "PROJECTS", "entries": [
            {"title": "Allotment tracker", "org": "", "dates": "2024",
             "bullets": [
                 "A small FastAPI service that records what I planted and "
                 "when, with a PostgreSQL database behind it",
             ]},
        ]},
        {"heading": "EDUCATION", "entries": [
            {"title": "BSc Mathematics", "org": "University of Leeds",
             "dates": "2018 - 2021", "bullets": []},
        ]},
        {"heading": "TECHNICAL SKILLS", "entries": [
            {"title": "", "org": "", "dates": "", "bullets": [
                "Languages: Python, SQL",
                "Data: PostgreSQL, pandas, dbt",
                "Other: Git, Docker",
            ]},
        ]},
    ],
})

POSTING = """Backend Engineer — Halverson Labs
Manchester (hybrid) · Permanent

We are a team of nine building the data platform that every other team at
Halverson depends on. You would own the services that move data in, the ones
that serve it back out, and the reporting layer on top.

What you would actually do
- Build and maintain Python services, mostly FastAPI
- Work with PostgreSQL at a scale where the query plan matters
- Take the reporting pipeline off the analytics team's hands
- Share an on-call rotation for the services you own

What we are looking for
- Strong proficiency in Python
- Experience with FastAPI or a comparable framework
- Comfortable with PostgreSQL beyond writing SELECTs
- Experience running services on Kubernetes
- 4+ years in a backend or data engineering role

We read every application. Send yours to careers@halverson.example with
anything you think we should see.
"""

# ── what the model would have said ────────────────────────────────────
#
# Four answers, one per model call. Written to be a realistic plan rather than a
# flattering one: three changes the resume supports, and one rewrite that
# invents a number, so the gate shows a refusal alongside the accepted changes.

BRIEF = {
    "company": "Halverson Labs",
    "role": "Backend Engineer",
    "seniority": "mid",
    "tone": "direct",
    "role_narrative": (
        "Own the data platform nine other engineers depend on. Build and "
        "maintain Python services, mostly FastAPI. Work with PostgreSQL where "
        "the query plan matters. Take the reporting pipeline off the analytics "
        "team. Share an on-call rotation."
    ),
    # The offsets have to quote the claim: S1 drops any element whose span
    # does not contain its own statement, which is the grounding rule working.
    # Getting these wrong the first time silently produced a brief with zero
    # requirements — and a demo that showed nothing.
    "problems_to_solve": [
        {"statement": "Take the reporting pipeline off the analytics team",
         "start": 420, "end": 470},
        {"statement": "Build and maintain Python services, mostly FastAPI",
         "start": 304, "end": 354},
    ],
    "success_signals": [
        {"statement": "Build and maintain Python services, mostly FastAPI",
         "start": 304, "end": 354},
        {"statement": "Comfortable with PostgreSQL", "start": 642, "end": 669},
    ],
    "hard_requirements": [
        {"statement": "Strong proficiency in Python", "start": 559, "end": 587},
        {"statement": "Experience with FastAPI", "start": 590, "end": 613},
        {"statement": "Comfortable with PostgreSQL", "start": 642, "end": 669},
        {"statement": "Experience running services on Kubernetes",
         "start": 695, "end": 736},
        {"statement": "4+ years in a backend or data engineering role",
         "start": 739, "end": 785},
    ],
    "terms": [
        {"term": "Python", "kind": "skill", "aliases": [], "required": True, "weight": 10},
        {"term": "FastAPI", "kind": "tool", "aliases": [], "required": True, "weight": 9},
        {"term": "PostgreSQL", "kind": "tool", "aliases": ["postgres"], "required": True, "weight": 8},
        {"term": "Kubernetes", "kind": "tool", "aliases": ["k8s"], "required": True, "weight": 6},
        {"term": "Docker", "kind": "tool", "aliases": [], "required": False, "weight": 4},
        {"term": "on-call", "kind": "domain", "aliases": ["oncall"], "required": False, "weight": 3},
    ],
}

PLAN = {"ops": [
    {"op": "set_summary",
     "text": "Analyst moving into backend work: Python services and "
             "PostgreSQL for reporting, plus a FastAPI side project.",
     "rationale": "The posting leads with Python, FastAPI and PostgreSQL, and "
                  "the resume has all three — the old summary said none of them.",
     "cites": ["exp.1.b.2", "prj.3.b.1"]},
    {"op": "promote_item", "item_id": "prj.3",
     "rationale": "The only FastAPI work on the resume, and FastAPI is the "
                  "posting's second requirement.",
     "cites": ["prj.3.b.1"]},
    {"op": "rewrite_bullet", "bullet_id": "exp.1.b.2", "text": "",
     "target_terms": ["Python", "PostgreSQL"],
     "rationale": "Say the nightly load in the platform language the posting "
                  "uses."},
    # ★ The one that gets refused. See WRITTEN below.
    {"op": "rewrite_bullet", "bullet_id": "exp.1.b.1", "text": "",
     "target_terms": ["Python"],
     "rationale": "Lead with the impact of replacing the manual spreadsheet."},
    {"op": "flag_gap", "requirement": "Kubernetes", "severity": "major",
     "closest_evidence": []},
    {"op": "flag_gap", "requirement": "4+ years in a backend role",
     "severity": "major", "closest_evidence": []},
    {"op": "ask_user", "bullet_id": "exp.1.b.1",
     "question": "Roughly how long did the Monday spreadsheet take the team "
                 "before the dashboard replaced it? A real number here would "
                 "be the strongest line on the resume."},
]}

WRITTEN = {"bullets": [
    {"bullet_id": "exp.1.b.2",
     "text": "Wrote the nightly Python jobs that load sales data into "
             "PostgreSQL"},
    # ★ "35%" appears nowhere in the resume. S5 must refuse this, and the
    # demo exists to show that happening.
    {"bullet_id": "exp.1.b.1",
     "text": "Cut weekly reporting time by 35% with a dashboard that replaced "
             "a manual spreadsheet"},
]}

OUTREACH = {
    "subject": "Backend Engineer — Priya Raman",
    "body": (
        "Hello,\n\n"
        "I am applying for the Backend Engineer role. Most of my day is "
        "Python and PostgreSQL — I write the nightly loads behind our "
        "reporting and I built the dashboard the team uses instead of a "
        "spreadsheet. The FastAPI experience below is a side project "
        "rather than production work, and I would rather say so than let it "
        "read as more than it is.\n\n"
        "I have not run anything on Kubernetes and I am two years short of "
        "the four you ask for. If either is firm, I would rather know now "
        "than take up your time.\n\n"
        "The attached resume has the detail. Happy to talk if that is "
        "useful.\n\n"
        "Priya Raman"
    ),
    "cites": ["exp.1.b.2", "prj.3.b.1"],
}

ANSWERS = {"brief": BRIEF, "plan": PLAN, "written": WRITTEN,
           "outreach": OUTREACH}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="delete the existing demo user and its runs first")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")

    # Imported here so the harness stays a test-time dependency of the evals
    # rather than of the application.
    from evals.harness import ScriptedClient

    document = normalize(RESUME)
    state = tailor(document, POSTING, ScriptedClient(ANSWERS),
                   write_outreach=True, render_output=True)

    if state.tailored is None or state.rendered is None:
        print("the pipeline produced nothing to seed")
        return 1

    with session_scope() as session:
        if args.reset:
            _remove_existing(session)

        user = _user(session)
        resume = _resume(session, user, document)
        run = _run(session, user, resume, state)
        _artifacts(session, run, state)

        session.flush()
        print(f"\nSeeded the demo as {DEMO_EMAIL}")
        print(f"  resume   {resume.filename}  ({len(document.sections)} sections)")
        print(f"  run      /runs/{run.id}")
        print(f"  status   {run.status}")
        print(f"  applied  {len(state.accepted)}   refused {len(state.rejected)}")
        for reject in state.rejected:
            print(f"           ↳ {reject.op_kind}: {reject.code} — {reject.detail}")
    return 0


# ── persistence ───────────────────────────────────────────────────────

def _remove_existing(session) -> None:
    user = (session.query(models.User)
            .filter(models.User.google_sub == DEMO_SUB).one_or_none())
    if user is None:
        return
    runs = session.query(models.Run).filter(models.Run.user_id == user.id).all()
    for run in runs:
        for message in session.query(models.Message).filter(
                models.Message.run_id == run.id):
            session.query(models.SendAudit).filter(
                models.SendAudit.message_id == message.id).delete()
            session.delete(message)
        session.query(models.Artifact).filter(
            models.Artifact.run_id == run.id).delete()
        session.query(models.RunCheckpoint).filter(
            models.RunCheckpoint.run_id == run.id).delete()
        session.query(models.OpFeedback).filter(
            models.OpFeedback.run_id == run.id).delete()
        session.delete(run)
    session.query(models.Resume).filter(
        models.Resume.user_id == user.id).delete()
    session.delete(user)
    session.flush()
    logger.info("Removed the previous demo")


def _user(session) -> models.User:
    user = (session.query(models.User)
            .filter(models.User.google_sub == DEMO_SUB).one_or_none())
    if user is None:
        user = models.User(google_sub=DEMO_SUB, email=DEMO_EMAIL,
                           name="Priya Raman (demo)")
        session.add(user)
        session.flush()
    return user


def _resume(session, user, document) -> models.Resume:
    text = "\n".join(
        [document.contact.full_name]
        + [bullet.text for section in document.sections
           for item in section.items for bullet in item.bullets]
    )
    resume = models.Resume(
        user_id=user.id, version=1, filename="Priya_Raman_CV.pdf",
        file_sha256=hashlib.sha256(text.encode()).hexdigest(),
        file_bytes=len(text.encode()), extract_text=text,
        raw_json=RESUME.model_dump(), doc_json=document.model_dump(),
        coverage_json={"dropped": [], "invented": [], "structure": []},
        status="confirmed", revision=1,
        confirmed_at=datetime.now(timezone.utc),
    )
    session.add(resume)
    session.flush()
    return resume


def _run(session, user, resume, state) -> models.Run:
    """Everything the review screen reads, straight off the real run state."""
    diff = state.diff.model_dump() if hasattr(state.diff, "model_dump") \
        else (state.diff or {})
    run = models.Run(
        user_id=user.id, resume_id=resume.id,
        status="needs_review", stage="outreach",
        jd_text=POSTING,
        jd_sha256=hashlib.sha256(POSTING.encode()).hexdigest(),
        role=state.brief.role if state.brief else "Backend Engineer",
        company=state.brief.company if state.brief else "Halverson Labs",
        brief_json=state.brief.model_dump() if state.brief else None,
        standing_json=state.standing,
        ops_json=[op.model_dump() for op in state.ops],
        accepted_json=[op.model_dump() for op in state.accepted],
        rejected_json=[r.model_dump() for r in state.rejected],
        tailored_json=state.tailored.model_dump(),
        diff_json=diff,
        outreach_json=state.outreach.model_dump() if state.outreach else None,
        # Honest about the demo: a scripted client made no calls and spent no
        # tokens, and the manifest should not claim otherwise.
        llm_calls=0, tokens=0,
        call_log_json=[],
        proof_json={"seeded": True, "model_calls": 0},
    )
    session.add(run)
    session.flush()

    for stage in ("posting", "brief", "evidence", "plan", "written",
                  "validation", "tailored", "diff", "outreach",
                  "resume_docx", "resume_pdf"):
        session.add(models.RunCheckpoint(run_id=run.id, stage=stage,
                                         state_json={}))
    session.flush()
    return run


def _artifacts(session, run, state) -> None:
    role = state.brief.role if state.brief else ""
    files = (
        ("resume_docx", render.filename_for(state.tailored, role, "docx"),
         "application/vnd.openxmlformats-officedocument."
         "wordprocessingml.document", state.rendered.docx),
        ("resume_pdf", render.filename_for(state.tailored, role, "pdf"),
         "application/pdf", state.rendered.pdf),
        ("resume_ats", render.filename_for(state.tailored, role, "txt"),
         "text/plain; charset=utf-8", state.rendered.text.encode("utf-8")),
    )
    for stage, name, media, data in files:
        session.add(models.Artifact(
            run_id=run.id, stage=stage, filename=name, content_type=media,
            bytes=len(data), blob=data))
    session.flush()


if __name__ == "__main__":
    sys.exit(main())
