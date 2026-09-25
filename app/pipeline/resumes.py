"""S0.1–S0.4 against a stored resume: parse it, let the user fix it, freeze it.

**The whole point of this module is which half re-runs.**

```
a NEW file      S0.1 extract → S0.2 parse (1–3 model calls) → S0.3 normalize
an EDIT         .............................................. S0.3 normalize
```

The user editing the parse is not asking for a better parse — they are
supplying the correct answer. Re-running the parser on the same bytes returns
the same mistake and throws the correction away, and the parse cache is keyed
on the file's hash, so the "re-parse" would be a cache hit handing back the
exact object that was just corrected. `normalize` is pure, instant and free,
and is also what *must* re-run, because it assigns the ids, parses the dates
and rebuilds the skill inventory — all of which change when a bullet is added.

**The edit targets `RawResume`, and that is safe only before confirmation.**
`RawResume` mirrors the document: headings verbatim, no ids. `ResumeDoc` is the
addressable model whose ids an `EvidenceLink` or an `Op` will name a week later.
Regenerating those ids during the edit loop breaks nothing because no link and
no op exists yet — which is exactly what "immutable once confirmed" means, and
why the gate is where ids freeze rather than where the parse finishes.

No HTTP here. The API layer above calls these and turns the results into
responses; the tests below call them directly, which is how a rule that has to
hold everywhere avoids being enforced only in a route handler.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.agents.parse import parse_resume
from app.db.models import Resume
from app.domain.errors import IllegalTransition, UserError
from app.domain.models import RawResume, ResumeDoc
from app.engine.normalize import normalize
from app.engine.parse_check import check_coverage
from app.io.cache import Cache, NullCache
from app.io.extract import extract_text
from app.io.llm import LLMClient

logger = logging.getLogger(__name__)

MAX_RESUME_BYTES = 10 * 1024 * 1024
SUPPORTED_SUFFIXES = (".pdf", ".docx", ".txt", ".md")


class StaleEdit(UserError):
    """The draft was written from a version that is no longer current.

    Two tabs, or a stale client. Refusing is the whole reason `revision` exists:
    a silent last-write-wins would lose the corrections made in the other tab,
    and the user would have no way to know it happened.
    """


# ── creating ──────────────────────────────────────────────────────────

def create(session: Session, user_id: str, filename: str, data: bytes) -> Resume:
    """Record the upload. Nothing is read yet.

    The bytes are hashed, not stored. A resume is personal data and nothing
    downstream needs the file again: the parse works from the extracted text,
    which is kept, and an edit does not re-parse at all.
    """
    if not data:
        raise UserError("the file is empty")
    if len(data) > MAX_RESUME_BYTES:
        raise UserError(
            f"the file is {len(data):,} bytes; the limit is "
            f"{MAX_RESUME_BYTES:,}"
        )
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if suffix not in SUPPORTED_SUFFIXES:
        raise UserError(
            f"cannot read {suffix or 'a file with no extension'}; supported: "
            f"{', '.join(SUPPORTED_SUFFIXES)}"
        )

    digest = hashlib.sha256(data).hexdigest()
    version = 1 + (
        session.query(Resume)
        .filter(Resume.user_id == user_id, Resume.file_sha256 == digest)
        .count()
    )

    resume = Resume(
        user_id=user_id, filename=filename, file_sha256=digest,
        file_bytes=len(data), version=version, status="uploaded",
    )
    session.add(resume)
    session.flush()
    return resume


def read(session: Session, resume: Resume, data: bytes,
         client: LLMClient, cache: Cache | None = None) -> Resume:
    """S0.1–S0.3. The only place in this module that can cost a model call.

    Leaves the resume in `needs_confirm`: the parse is a hypothesis, and the
    next thing that happens to it is a person looking at it.
    """
    resume.move_to("parsing")
    cache = cache or NullCache()

    try:
        text = extract_text(data, resume.filename)
        resume.extract_text = text

        from app.io.cache import key_for
        cache_key = key_for(data)
        raw = cache.get("parse", cache_key, RawResume)
        if raw is None:
            raw = parse_resume(text, client)
            cache.set("parse", cache_key, raw)
        else:
            logger.info("Reusing the cached parse of %s", resume.filename)

        _store(resume, raw, text)
    except Exception as exc:                              # noqa: BLE001
        resume.status = "failed"
        resume.error = f"{type(exc).__name__}: {exc}"
        raise

    resume.move_to("needs_confirm")
    return resume


def reparse(session: Session, resume: Resume, client: LLMClient) -> Resume:
    """Try the parser again on the stored text, after a failure.

    Works without the original file because the parser takes text, not bytes —
    which is the second reason `extract_text` is kept. Deliberately NOT part of
    the edit loop: this costs model calls and exists for a parse that errored,
    not for one that merely got something wrong.
    """
    if not resume.extract_text:
        raise UserError("nothing to re-parse — upload the file again")
    resume.move_to("parsing")
    try:
        raw = parse_resume(resume.extract_text, client)
        _store(resume, raw, resume.extract_text)
    except Exception as exc:                              # noqa: BLE001
        resume.status = "failed"
        resume.error = f"{type(exc).__name__}: {exc}"
        raise
    resume.move_to("needs_confirm")
    return resume


# ── the edit loop ─────────────────────────────────────────────────────

def apply_draft(session: Session, resume: Resume, raw: RawResume,
                expected_revision: int | None = None) -> Resume:
    """The user's correction. **No model call, ever.**

    Takes the whole corrected `RawResume` rather than a patch. A patch language
    would be a second representation of the document to keep consistent with
    the first, and the document is small enough that sending it whole is
    cheaper than inventing one.

    Coverage is recomputed against the stored extract text, so the screen can
    tell the user how many of the originally-missing lines they have now placed
    — which turns a list of complaints into a task with an end.
    """
    if resume.status != "needs_confirm":
        raise IllegalTransition(
            f"a resume can only be edited while it needs confirming; "
            f"this one is {resume.status!r}"
        )
    if expected_revision is not None and expected_revision != resume.revision:
        raise StaleEdit(
            f"this draft was written from revision {expected_revision} and the "
            f"resume is now at {resume.revision} — reload before editing again"
        )

    _store(resume, raw, resume.extract_text)
    resume.revision += 1
    # Explicit and legal: the edit loop is the one self-transition in either
    # machine, and going through `move_to` keeps that fact in one place.
    resume.move_to("needs_confirm")
    return resume


def confirm(session: Session, resume: Resume) -> Resume:
    """Freeze it. Ids are permanent from here.

    Refuses an empty document: confirming a parse that found nothing would
    produce a resume every later stage treats as real and empty, and the
    failure would surface as "the planner proposed nothing" three stages later.
    """
    doc = document(resume)
    if doc is None or not doc.sections:
        raise UserError(
            "this parse has no sections — add at least one before confirming, "
            "or upload a different file"
        )
    resume.move_to("confirmed")
    logger.info("Resume %s confirmed: %d sections, %d bullets",
                resume.id, len(doc.sections), len(list(doc.all_bullets())))
    return resume


def supersede(session: Session, resume: Resume) -> Resume:
    """Retire a confirmed resume when a newer version is confirmed.

    Runs keep pointing at the version they used, so an application from March
    still shows the document that was actually sent.
    """
    resume.move_to("superseded")
    return resume


# ── reading ───────────────────────────────────────────────────────────

def draft(resume: Resume) -> RawResume | None:
    """The editable hypothesis."""
    return RawResume.model_validate(resume.raw_json) if resume.raw_json else None


def document(resume: Resume) -> ResumeDoc | None:
    """The addressable document. Frozen once `confirmed`."""
    return ResumeDoc.model_validate(resume.doc_json) if resume.doc_json else None


def summary(resume: Resume) -> dict[str, Any]:
    """What a list row or a confirm screen needs, without the whole document."""
    doc = document(resume)
    coverage = resume.coverage_json or {}
    return {
        "id": resume.id,
        "filename": resume.filename,
        "version": resume.version,
        "status": resume.status,
        "revision": resume.revision,
        "error": resume.error,
        "sections": [
            {"kind": s.kind, "heading": s.heading, "items": len(s.items),
             "bullets": sum(len(i.bullets) for i in s.items)}
            for s in (doc.sections if doc else [])
        ],
        "skills": len(doc.skill_inventory) if doc else 0,
        # The edit screen. Not a debug field — these are the exact source lines
        # the parse lost, and asking about a specific piece of text is the only
        # kind of confirmation prompt anyone completes.
        "unplaced_lines": list(coverage.get("dropped", [])),
        "invented_lines": list(coverage.get("invented", [])),
        "structure_warnings": list(coverage.get("structure", [])),
        "parse_is_clean": not (
            coverage.get("dropped") or coverage.get("invented")
            or coverage.get("structure")
        ),
    }


# ── internals ─────────────────────────────────────────────────────────

def _store(resume: Resume, raw: RawResume, text: str) -> None:
    """Persist a parse — from the model or from the user — and re-derive.

    One function for both paths so that an edit and a parse cannot diverge in
    what they leave behind. The coverage check runs on both for the same
    reason: after an edit it is the progress bar, and after a parse it is the
    complaint.
    """
    resume.raw_json = raw.model_dump()
    # `ParseCoverage` is a frozen dataclass, not a pydantic model — it is
    # engine-layer output and the engine has no pydantic dependency to spare.
    resume.coverage_json = (
        dataclasses.asdict(check_coverage(text, raw)) if text else None)
    resume.doc_json = normalize(raw).model_dump()
    resume.error = ""
