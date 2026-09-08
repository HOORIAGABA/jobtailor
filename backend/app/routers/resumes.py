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
                text_parts.append(page.extract_text(layout=True) or "")
                # Designed resumes often place content in tables - forward it too.
                for row in page.extract_tables() or []:
                    cells = [c.replace("\n", " ") if c else "" for c in row]
                    if any(cells):
                        text_parts.append(" | ".join(cells))
        return "\n".join(text_parts)

    if filename.endswith(".docx"):
        return _extract_docx_content(content)

    raise HTTPException(status_code=400, detail="Only .pdf and .docx files are supported")


def _extract_docx_content(content: bytes) -> str:
    """Extract ALL text from a .docx, not just body paragraphs.

    python-docx's `paragraphs` property skips tables and text boxes entirely -
    resumes that use a 2-column or skill-matrix layout put section content
    (including project/job titles) inside table cells, so the LLM never saw it.
    Walk the document body in reading order: paragraphs + tables (row/cell),
    then append any text-box content (designed sidebars).
    """
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn

    document = docx.Document(io.BytesIO(content))
    parts = []

    def _paragraph_lines(paragraphs):
        for p in paragraphs:
            if p.text.strip():
                parts.append(p.text)

    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            _paragraph_lines([Paragraph(child, document)])
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            for row in table.rows:
                for cell in row.cells:
                    _paragraph_lines(cell.paragraphs)

    # Text boxes / sidebars (w:txbxContent) are not exposed by python-docx.
    for tb in document.element.body.iter(qn("w:txbxContent")):
        box_text = " ".join(t.text or "" for t in tb.iter(qn("w:t")) if t.text)
        if box_text.strip():
            parts.append(box_text)

    return "\n".join(parts)


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

    # Auto-fill user profile from parsed contact fields (if user hasn't set them).
    # Contact fields STAY in the resume_json — they are part of the baseline resume.
    user = db.query(models.User).filter(models.User.id == current_user.id).first()

    regex_contact = extract_contact_info(raw_text)

    profile_updates = {}
    for key in ("full_name", "phone", "location", "linkedin", "github"):
        parsed_val = structured.get(key)
        regex_val = regex_contact.get(key)
        val = parsed_val or regex_val
        if val and not getattr(user, key, None):
            profile_updates[key] = val

    if profile_updates:
        for k, v in profile_updates.items():
            setattr(user, k, v)
        db.commit()
        db.refresh(user)

    # Strip contact details from summary so rendered header stays clean
    import re as _re
    summary = structured.get("summary") or ""
    for key in ("email", "phone", "linkedin", "github"):
        val = structured.get(key) or regex_contact.get(key)
        if not val:
            continue
        if val in summary:
            summary = summary.replace(val, "").strip()
            continue
        if key == "phone":
            val_digits = _re.sub(r"\D", "", val)
            if len(val_digits) >= 7:
                for m in _re.finditer(r"[\d\s\-+().]{7,}", summary):
                    candidate_digits = _re.sub(r"\D", "", m.group())
                    if val_digits in candidate_digits or candidate_digits in val_digits:
                        summary = summary[:m.start()] + summary[m.end():]
                        summary = summary.strip()
                        break
        elif key in ("linkedin", "github"):
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
        raw_text=raw_text,
    )
    db.add(resume)
    db.commit()
    db.refresh(resume)

    return schemas.ResumeOut(
        id=resume.id, label=resume.label, version=resume.version,
        resume_json=structured, raw_text=raw_text,
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
        schemas.ResumeOut(
            id=r.id, label=r.label, version=r.version,
            resume_json=json.loads(r.resume_json), raw_text=r.raw_text,
        )
        for r in resumes
    ]
