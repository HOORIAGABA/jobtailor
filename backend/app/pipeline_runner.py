"""
Background pipeline runner.

The tailoring pipeline (NRJ agentic loop) can take several minutes on a local
model, so the POST /job-posts handler no longer blocks on it. Instead it
persists a TailoredOutput with status="pending" and hands the work to a
daemon thread here, which uses its OWN database session (request sessions
are closed when the HTTP response is sent) so the caller gets an output id
immediately and the UI polls /tailored-outputs/{id} until it is ready.
"""
import json
import threading
import traceback

from sqlalchemy.orm import Session

from app import models
from app.database import SessionLocal
from app.agents.orchestrator import run_pipeline
from app.rendering.renderer import render_resume, contact_info_from_user
from app.rendering.pdf_renderer import render_resume_pdf


def _mark_status(db: Session, output_id: str, status: str, payload: dict | None = None) -> None:
    output = db.query(models.TailoredOutput).filter(models.TailoredOutput.id == output_id).first()
    if not output:
        return
    output.status = status
    if payload is not None:
        try:
            existing = json.loads(output.tailored_json or "{}")
        except Exception:
            existing = {}
        existing.update(payload)
        output.tailored_json = json.dumps(existing)
    db.commit()


def _run_pipeline_for_output(output_id: str) -> None:
    db = SessionLocal()
    try:
        output = db.query(models.TailoredOutput).filter(
            models.TailoredOutput.id == output_id
        ).first()
        if not output:
            return
        job_post = db.query(models.JobPost).filter(
            models.JobPost.id == output.job_post_id
        ).first()
        resume = db.query(models.Resume).filter(
            models.Resume.id == output.resume_id
        ).first()
        user = db.query(models.User).filter(
            models.User.id == job_post.user_id
        ).first() if job_post else None
        if not (job_post and resume and user):
            _mark_status(db, output_id, "failed", {"error": "Missing job post / resume / user."})
            return

        output.status = "processing"
        db.commit()

        resume_json = json.loads(resume.resume_json)
        result = run_pipeline(
            job_raw_text=job_post.raw_text,
            resume_id=resume.id,
            resume_json=resume_json,
        )

        if result.get("error"):
            _mark_status(db, output_id, "failed", {"error": result["error"]})
            return

        tailored_resume = result.get("tailored_resume") or {}
        contact_info = contact_info_from_user(
            user,
            title=(result.get("job_requirements") or {}).get("role", ""),
        )
        docx_path = render_resume(
            tailored_json=tailored_resume,
            output_filename=f"resume_{job_post.id}.docx",
            contact_info=contact_info,
        )
        pdf_path = None
        try:
            pdf_path = render_resume_pdf(
                tailored_json=tailored_resume,
                output_filename=f"resume_{job_post.id}.pdf",
                contact_info=contact_info,
            )
        except Exception:
            traceback.print_exc()

        output = db.query(models.TailoredOutput).filter(
            models.TailoredOutput.id == output_id
        ).first()
        output.tailored_json = json.dumps(tailored_resume)
        output.gap_analysis = json.dumps(result.get("missing_requirements") or [])
        output.docx_path = docx_path
        output.pdf_path = pdf_path
        output.status = "ready"
        db.add(
            models.Message(
                tailored_output_id=output.id,
                draft_text=result.get("draft_message", ""),
                status="draft",
            )
        )
        db.commit()
    except Exception:
        traceback.print_exc()
        try:
            _mark_status(db, output_id, "failed", {"error": traceback.format_exc()[-1000:]})
        except Exception:
            pass
    finally:
        db.close()


def start_pipeline(output_id: str) -> None:
    """Fire-and-forget: run the agentic pipeline for an already-persisted
    TailoredOutput on a daemon thread. Returns immediately."""
    threading.Thread(
        target=_run_pipeline_for_output,
        args=(output_id,),
        daemon=True,
        name=f"pipeline-{output_id[:8]}",
    ).start()