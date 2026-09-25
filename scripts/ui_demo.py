"""Put one run in front of the gate, so the UI has something real to render.

    python -m scripts.ui_demo

Renders the confirmed resume into a run awaiting review and fills in the diff,
the gaps, the standing and the outreach draft. Zero model calls — it reads a
parse that already exists and writes the shapes the review screen consumes.

This is for looking at the interface, not for measuring the pipeline. The diff
and the draft here are written by hand; a real run produces them from a real
posting and takes several minutes.
"""
from __future__ import annotations

import sys

from app.db import models
from app.db.session import session_scope
from app.domain.models import ResumeDoc
from app.io import render

DRAFT = {
    "recipient": "careers@annova.ai",
    "recipient_candidates": ["careers@annova.ai", "talent@annova.ai"],
    "subject": "AI Engineer — Hooria Attas",
    "body": (
        "Hello,\n\n"
        "I'm applying for the AI Engineer role. I've built and shipped "
        "retrieval pipelines in Python and FastAPI, and the attached resume "
        "lists the projects those came from.\n\n"
        "Happy to talk this week if that's useful.\n\n"
        "Hooria Attas"
    ),
    "problems": ['"deep experience" is vague — say what you built instead'],
    "cites": ["exp.1.b.1", "prj.2.b.1"],
}

DIFF = {
    "changes": [
        {"op_id": "op1", "op": "set_summary", "where": "summary",
         "before": "Software engineer with a broad interest in AI.",
         "after": "Backend engineer building retrieval pipelines in Python "
                  "and FastAPI.",
         "why": "The posting names retrieval and FastAPI in its first "
                "requirement."},
        {"op_id": "op2", "op": "promote_item", "where": "experience.2",
         "after": "Moved above the teaching role.",
         "why": "Closest match to the role's day-to-day."},
        {"op_id": "op3", "op": "set_skills", "where": "skills",
         "after": "Python, FastAPI, PostgreSQL, Docker — job terms first, "
                  "then your own.",
         "why": "An ATS reads the first line of a skills block."},
    ],
    "gaps": [
        {"requirement": "Kubernetes",
         "why": "nothing in your resume shows it — left alone rather than "
                "invented"},
        {"requirement": "5+ years commercial experience",
         "why": "your resume shows 2 years and two internships"},
    ],
    "questions": [
        "Did the retrieval pipeline serve live traffic? If so, at what volume "
        "— that is the number this posting is asking for."
    ],
    "rejections": [
        {"op_id": "op4", "rule": "no_unsupported_number",
         "why": 'proposed "improved latency by 40%" and no line in your resume '
                "mentions latency"},
    ],
}

STANDING = {
    "demonstrated": ["Python", "FastAPI", "PostgreSQL", "retrieval"],
    "declared_only": ["Docker", "CI/CD"],
    "not_found": ["Kubernetes", "Terraform"],
}

ROLE = "AI Engineer"


def main() -> int:
    with session_scope() as session:
        resume = next((r for r in session.query(models.Resume).all()
                       if (r.doc_json or {}).get("sections")), None)
        run = (session.query(models.Run)
               .filter(models.Run.status == "needs_review").first())
        if resume is None or run is None:
            print("This database has no parsed resume, or no run awaiting "
                  "review. Run `python -m scripts.seed` first.")
            return 1

        document = ResumeDoc.model_validate(resume.doc_json)
        rendered = render.render(document, role=ROLE)
        files = (
            ("resume_docx", render.filename_for(document, ROLE, "docx"),
             "application/vnd.openxmlformats-officedocument."
             "wordprocessingml.document", rendered.docx),
            ("resume_pdf", render.filename_for(document, ROLE, "pdf"),
             "application/pdf", rendered.pdf),
        )
        for stage, name, media, data in files:
            existing = (session.query(models.Artifact)
                        .filter(models.Artifact.run_id == run.id,
                                models.Artifact.stage == stage).first())
            if existing is None:
                session.add(models.Artifact(
                    run_id=run.id, stage=stage, filename=name,
                    content_type=media, bytes=len(data), blob=data))

        run.outreach_json = dict(DRAFT)
        run.diff_json = dict(DIFF)
        run.standing_json = dict(STANDING)
        run.role, run.company = ROLE, "Annova"
        run.accepted_json = [{"op_id": c["op_id"], "op": c["op"]}
                             for c in DIFF["changes"]]
        run.llm_calls, run.tokens = 7, 21486

        print(f"Ready: /runs/{run.id}")
        print(f"  render clean: {rendered.is_clean}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
