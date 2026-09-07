import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.pipeline_runner import start_pipeline
from app.fetchers.url_fetcher import fetch_and_extract

router = APIRouter(prefix="/job-posts", tags=["job-posts"])


@router.post("", response_model=schemas.TailoredOutputOut)
def submit_job_post(
    payload: schemas.JobPostCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    THE single entry point for all capture sources: web app paste box,
    generic URL fetch, and the Chrome extension (source_type="extension").
    See blueprint section 3.5 for the full end-to-end interlink diagram.

    Auth is resolved from the Bearer token -> current_user, exactly like
    the extension's background.js call described in the blueprint.

    The multi-agent pipeline is NOT run in this request — a TailoredOutput
    with status="pending" is persisted and the work is delegated to a
    background thread (app.pipeline_runner). The UI polls
    GET /tailored-outputs/{id} until status is "ready" / "failed".
    """
    raw_text = payload.raw_text
    source_type = payload.source_type if payload.source_type in {"paste", "url", "extension"} else "paste"

    # If only a URL was given (no pasted text), try auto-fetching it.
    # This is the path for normal (non-LinkedIn) URLs.
    if not raw_text and payload.url:
        raw_text = fetch_and_extract(payload.url)
        if source_type != "extension":
            source_type = "url"

    if not raw_text or not raw_text.strip():
        raise HTTPException(
            status_code=400,
            detail="Could not get job post content. For LinkedIn/auth-walled "
                   "links, paste the text directly or use the Chrome extension.",
        )

    job_post = models.JobPost(
        user_id=current_user.id,
        url=payload.url,
        raw_text=raw_text,
        source_type=source_type,
    )
    db.add(job_post)
    db.commit()
    db.refresh(job_post)

    # Resolve which resume to use: explicit resume_id, else latest active one
    resume_query = db.query(models.Resume).filter(models.Resume.user_id == current_user.id)
    if payload.resume_id:
        resume = resume_query.filter(models.Resume.id == payload.resume_id).first()
    else:
        resume = resume_query.filter(models.Resume.is_active == True).order_by(  # noqa: E712
            models.Resume.created_at.desc()
        ).first()

    if not resume:
        raise HTTPException(
            status_code=400,
            detail="No resume found for this account. Upload a resume first via POST /resumes/upload.",
        )

    tailored_output = models.TailoredOutput(
        job_post_id=job_post.id,
        resume_id=resume.id,
        tailored_json=json.dumps({}),
        status="pending",
    )
    db.add(tailored_output)
    db.commit()
    db.refresh(tailored_output)

    # Run the multi-agent pipeline (LangGraph) in the background: parse job ->
    # tailor resume -> draft outreach. The response returns right away with a
    # pending output; poll GET /tailored-outputs/{id} until it is ready.
    start_pipeline(tailored_output.id)

    return schemas.TailoredOutputOut(
        id=tailored_output.id,
        job_post_id=job_post.id,
        resume_id=resume.id,
        status=tailored_output.status,
        docx_path=tailored_output.docx_path,
        tailored_json=tailored_output.tailored_json,
        draft_message=None,
    )
