"""Drive S10 -> S9 -> S11 on a stored run, with no model and no network.

Renders the confirmed resume, puts the two files on a run, approves the draft
through the real gate, and sends it through the console backend — then prints
the `.eml` that would have gone out. Zero API calls, which is the point: the
dangerous stage is demonstrable without credentials.

    DATABASE_URL=sqlite+pysqlite:///send_demo.db python -m scripts.send_demo
"""
from __future__ import annotations

import sys

from app.db import models
from app.db.session import session_scope
from app.domain.models import ResumeDoc
from app.io import render
from app.io.mail import ConsoleSender
from app.pipeline import gate, send

SECRET = "demo-secret-not-for-production"

DRAFT = {
    "recipient": "careers@annova.ai",
    "recipient_candidates": ["careers@annova.ai"],
    "subject": "AI Engineer — Hooria Attas",
    "body": (
        "Hello,\n\n"
        "I'm applying for the AI Engineer role. I've built and shipped "
        "retrieval pipelines in Python and FastAPI, and the attached resume "
        "lists the projects those came from.\n\n"
        "Happy to talk this week if it's useful.\n\n"
        "Hooria Attas"
    ),
    "problems": [],
    "cites": [],
}


def main() -> int:
    with session_scope() as s:
        resume = next((r for r in s.query(models.Resume).all()
                       if (r.doc_json or {}).get("sections")), None)
        if resume is None:
            print("no confirmed resume with a parsed document in this database")
            return 1

        run = (s.query(models.Run)
               .filter(models.Run.status == "needs_review").first())
        if run is None:
            print("no run awaiting review in this database")
            return 1

        document = ResumeDoc.model_validate(resume.doc_json)
        rendered = render.render(document, role="AI Engineer")
        files = {
            "resume_docx": (render.filename_for(document, "AI Engineer", "docx"),
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document", rendered.docx),
            "resume_pdf": (render.filename_for(document, "AI Engineer", "pdf"),
                           "application/pdf", rendered.pdf),
        }
        for stage, (name, media, data) in files.items():
            existing = (s.query(models.Artifact)
                        .filter(models.Artifact.run_id == run.id,
                                models.Artifact.stage == stage).first())
            if existing is None:
                s.add(models.Artifact(
                    run_id=run.id, stage=stage, filename=name,
                    content_type=media, bytes=len(data), blob=data))
        run.outreach_json = dict(DRAFT)
        s.flush()
        print(f"RENDER   clean={rendered.is_clean} " + ", ".join(
            f"{name} {len(data)}B" for name, _, data in files.values()))

        preview = gate.preview(s, run, SECRET)
        print(f"PREVIEW  to={preview['recipient']} token={preview['confirm_token'][:24]}…")

        gate.decide(s, run, decision="approve", secret=SECRET,
                    recipient=preview["recipient"], subject=preview["subject"],
                    body=preview["body"],
                    confirm_token=preview["confirm_token"])
        print(f"APPROVE  status={run.status}")

        user = s.query(models.User).filter(models.User.id == run.user_id).one()
        token = preview["confirm_token"]
        sender = ConsoleSender()
        out = send.send(s, run, user, sender, secret=SECRET,
                        recipient=preview["recipient"],
                        subject=preview["subject"], body=preview["body"],
                        confirm_token=token)
        print(f"SEND     status={out['status']} provider={out['provider']} "
              f"dispatched={out['dispatched']} attachments={out['attachments']}")

        try:
            send.send(s, run, user, sender, secret=SECRET,
                      recipient=preview["recipient"],
                      subject=preview["subject"], body=preview["body"],
                      confirm_token=token)
            print("RETRY    !! sent twice — the guard did not hold")
            return 1
        except send.AlreadySent as exc:
            print(f"RETRY    refused: {exc}")

        rows = (s.query(models.SendAudit)
                .order_by(models.SendAudit.attempted_at).all())
        print(f"AUDIT    {[r.outcome for r in rows]}")

        raw = sender.sent[0][1]
        print("\n── the message ────────────────────────────────────────")
        head, _, _ = raw.decode("utf-8", "replace").partition("\n\n")
        print(head[:900])
    return 0


if __name__ == "__main__":
    sys.exit(main())
