import json
import io
from fastapi import APIRouter, Depends, UploadFile, File, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app import models, schemas
from app.auth import get_current_user
from app.agents.contact_extractor import extract_contact_info
from app.agents.resume_parser_agent import parse_resume_text

router = APIRouter(prefix="/resumes", tags=["resumes"])


MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


def _extract_text_from_upload(file: UploadFile) -> str:
    content = file.file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE // (1024 * 1024)} MB.",
        )
    filename = (file.filename or "").lower()

    if filename.endswith(".pdf"):
        import pdfplumber
        text_parts = []
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
        return "\n".join(text_parts)

    if filename.endswith(".docx"):
        import docx
        document = docx.Document(io.BytesIO(content))
        return "\n".join(p.text for p in document.paragraphs)

    raise HTTPException(status_code=400, detail="Only .pdf and .docx files are supported")


@router.post("/upload", response_model=schemas.ResumeOut)
def upload_resume(
    file: UploadFile = File(...),
    label: str = "Default",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """
    Module 3.0: extract raw text -> Resume Parser Agent structures it ->
    saved as the baseline resume_json. The frontend should show this
    result to the user for review/edit (PUT /resumes/{id}) before it's
    relied on by the tailoring pipeline.
    """
    raw_text = _extract_text_from_upload(file)
    if not raw_text.strip():
        raise HTTPException(status_code=400, detail="Could not extract any text from the uploaded file")

    structured = parse_resume_text(raw_text)

    # Use LLM-extracted contact info as primary, regex as fallback.
    # The LLM extracts full_name, email, phone, location, linkedin, github
    # directly from the resume text — more robust than regex patterns.
    # Reload the user in THIS request's db session so changes persist
    # (get_current_user may use a different session than db).
    user = db.query(models.User).filter(models.User.id == current_user.id).first()

    # Primary: LLM-extracted fields from the parsed resume
    llm_contact = {
        "full_name": structured.pop("full_name", None),
        "email": structured.pop("email", None),
        "phone": structured.pop("phone", None),
        "location": structured.pop("location", None),
        "linkedin": structured.pop("linkedin", None),
        "github": structured.pop("github", None),
    }
    # Fallback: regex extraction for anything the LLM missed
    regex_contact = extract_contact_info(raw_text)

    # Merge: LLM wins, regex fills gaps
    contact = {}
    for key in ("full_name", "email", "phone", "location", "linkedin", "github"):
        val = llm_contact.get(key) or regex_contact.get(key)
        if val:
            contact[key] = val

    # Route into user profile as fallback only — user-set fields win
    if contact:
        if not user.full_name and contact.get("full_name"):
            user.full_name = contact["full_name"]
        if not user.phone and contact.get("phone"):
            user.phone = contact["phone"]
        if not user.location and contact.get("location"):
            user.location = contact["location"]
        if not user.linkedin and contact.get("linkedin"):
            user.linkedin = contact["linkedin"]
        if not user.github and contact.get("github"):
            user.github = contact["github"]
    db.commit()
    db.refresh(user)

    # Strip accidentally-included contact details from the summary so
    # the rendered header stays clean (email/phone/LinkedIn/GitHub don't
    # belong in the professional summary). Use normalized comparison to
    # handle formatting differences (e.g. "+92 3036800002" vs "(0300) 368-00002").
    import re as _re
    summary = structured.get("summary") or ""
    for key in ("email", "phone", "linkedin", "github", "website"):
        val = contact.get(key) or regex_contact.get(key)
        if not val:
            continue
        # Exact match first
        if val in summary:
            summary = summary.replace(val, "").strip()
            continue
        # Normalized match for phone numbers (strip non-digits)
        if key == "phone":
            val_digits = _re.sub(r"\D", "", val)
            if len(val_digits) >= 7:
                # Find and remove phone-like sequences that match the digit pattern
                for m in _re.finditer(r"[\d\s\-+().]{7,}", summary):
                    candidate_digits = _re.sub(r"\D", "", m.group())
                    if val_digits in candidate_digits or candidate_digits in val_digits:
                        summary = summary[:m.start()] + summary[m.end():]
                        summary = summary.strip()
                        break
        # Normalized match for URLs (lowercase, strip trailing slash)
        elif key in ("linkedin", "github", "website"):
            val_norm = val.lower().rstrip("/")
            for m in _re.finditer(r"https?://[^\s\"'<>]+", summary, _re.IGNORECASE):
                if val_norm in m.group().lower().rstrip("/"):
                    summary = summary[:m.start()] + summary[m.end():]
                    summary = summary.strip()
                    break
    structured["summary"] = summary

    resume = models.Resume(
        user_id=current_user.id,
        label=label,
        resume_json=json.dumps(structured),
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)

    return schemas.ResumeOut(
        id=resume.id, label=resume.label, version=resume.version,
        resume_json=structured,
    )


@router.put("/{resume_id}", response_model=schemas.ResumeOut)
def update_resume(
    resume_id: str,
    payload: schemas.ResumeJSON,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Human-in-the-loop correction step: user edits the parsed resume
    before it's saved as the version the pipeline will use."""
    resume = db.query(models.Resume).filter(
        models.Resume.id == resume_id, models.Resume.user_id == current_user.id
    ).first()
    if not resume:
        raise HTTPException(status_code=404, detail="Resume not found")

    resume.resume_json = payload.model_dump_json()
    resume.version += 1
    db.commit()
    db.refresh(resume)

    return schemas.ResumeOut(
        id=resume.id, label=resume.label, version=resume.version,
        resume_json=json.loads(resume.resume_json),
    )


@router.get("", response_model=list[schemas.ResumeOut])
def list_resumes(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    resumes = db.query(models.Resume).filter(models.Resume.user_id == current_user.id).all()
    return [
        schemas.ResumeOut(id=r.id, label=r.label, version=r.version, resume_json=json.loads(r.resume_json))
        for r in resumes
    ]
