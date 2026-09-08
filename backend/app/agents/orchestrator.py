"""Pipeline orchestrator - bounded review loop (plan -> review -> revise).

Flow: parse_job -> tailor_resume -> review -> (re-tailor max 2x if flagged)
      -> gaps -> draft_outreach -> (optional review_message gate) -> done
"""
import logging
from typing import TypedDict, Optional

from app.agents.job_parser_agent import parse_job_post
from app.agents.tailoring_agent import tailor_resume
from app.agents.outreach_agent import draft_outreach_message
from app.agents.gap_analysis_agent import analyze_missing_requirements
from app.agents.review_agent import review_resume, review_message

logger = logging.getLogger(__name__)

# Bounded retry budget for the agentic loop: the producer is re-run AT MOST this
# many times with reviewer feedback, then we stop regardless (never run away).
MAX_REVIEW_REVISIONS = 2


class PipelineState(TypedDict, total=False):
    job_raw_text: str
    resume_id: str
    resume_json: dict
    job_requirements: dict
    tailored_resume: dict
    missing_requirements: list
    draft_message: str
    review: dict  # last review result (score, feedback, fit_comparison)
    revisions: int  # how many re-tailor passes were performed
    error: Optional[str]


def run_pipeline(job_raw_text: str, resume_id: str, resume_json: dict) -> PipelineState:
    state: PipelineState = {
        "job_raw_text": job_raw_text,
        "resume_id": resume_id,
        "resume_json": resume_json,
        "revisions": 0,
        "error": None,
    }

    # Step 1: Parse job posting
    try:
        state["job_requirements"] = parse_job_post(job_raw_text)
    except Exception as e:
        state["error"] = f"job parsing failed: {e}"
        return state

    # Step 2: Tailor resume
    try:
        state["tailored_resume"] = tailor_resume(
            resume_id=resume_id,
            resume_json=resume_json,
            job_requirements=state["job_requirements"],
        )
    except Exception as e:
        state["error"] = f"tailoring failed: {e}"
        return state

    # Step 3: Bounded review loop - plan -> review -> revise (max 2x).
    # Review is best-effort: if the reviewer itself fails, we keep the tailor.
    best = state["tailored_resume"]
    best_score = 0.0
    revisions = 0
    try:
        review = review_resume(
            job_requirements=state["job_requirements"],
            tailored_resume=best,
            original_resume=resume_json,
        )
        state["review"] = review
        fc = review.get("fit_comparison") or {}
        best_score = fc.get("tailored_fit_score") or 0.0

        while review.get("needs_revision") and revisions < MAX_REVIEW_REVISIONS:
            revisions += 1
            logger.info("Review flagged revision #%d: %s", revisions, review.get("feedback", "")[:200])
            revised = tailor_resume(
                resume_id=resume_id,
                resume_json=resume_json,
                job_requirements=state["job_requirements"],
                feedback=review.get("feedback") or "",
            )
            review = review_resume(
                job_requirements=state["job_requirements"],
                tailored_resume=revised,
                original_resume=resume_json,
            )
            fc = review.get("fit_comparison") or {}
            score = fc.get("tailored_fit_score") or 0.0
            # Keep the revision that improves fit; reviewer feedback may hurt
            # occasionally, so we compare deterministically.
            if score >= best_score:
                best = revised
                best_score = score
            state["review"] = review
        state["tailored_resume"] = best
        state["revisions"] = revisions
    except Exception as e:
        logger.warning("Review loop skipped (%s); using first tailor output", e)
        state["revisions"] = revisions

    # Step 4: Gap analysis - JD requirements the resume doesn't support.
    # Best-effort: a failure here shouldn't block the rest of the pipeline.
    try:
        state["missing_requirements"] = analyze_missing_requirements(
            job_requirements=state["job_requirements"],
            tailored_resume=state["tailored_resume"],
        )
        if not state["missing_requirements"]:
            state["missing_requirements"] = []
    except Exception:
        state["missing_requirements"] = []

    # Step 5: Draft outreach message (with optional reviewer feedback gate).
    try:
        state["draft_message"] = draft_outreach_message(
            job_requirements=state["job_requirements"],
            tailored_resume=state["tailored_resume"],
        )
        try:
            mreview = review_message(
                job_requirements=state["job_requirements"],
                draft_message=state["draft_message"],
                tailored_resume=state["tailored_resume"],
            )
            if mreview.get("needs_revision"):
                state["draft_message"] = draft_outreach_message(
                    job_requirements=state["job_requirements"],
                    tailored_resume=state["tailored_resume"],
                    feedback=mreview.get("feedback") or "",
                )
        except Exception:
            pass  # message review is best-effort
    except Exception as e:
        state["error"] = f"outreach drafting failed: {e}"
        return state

    return state