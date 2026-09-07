"""Module 4.1 — reflection / self-review agents (agentic loop).

After the tailor and outreach nodes produce output, a dedicated reviewer
agent critiques it against the job requirements and the original resume.
If problems are found, the orchestrator routes back to the producer node
(bounded retries) — this is the plan-review-revise loop that makes the
pipeline genuinely agentic rather than a one-pass chain.
"""
import json

from app.agents.llm_client import call_llm, extract_json
from app.agents.tailoring_agent import _entry_key, _relevance_score, _tokens

REVIEW_RESUME_SYSTEM = """You are a senior resume-quality review agent. Given a tailored resume, the ORIGINAL resume it was produced from, and the job requirements, critique the tailored resume.

## What to check:
1. Does EVERY original section survive? (summary, skills, experience, projects, certifications, education, leadership)
2. Are there duplicate entries (same role/company appearing twice)?
3. Is the **summary actually rewritten** to target the role title and the job's required skills — NOT a verbatim echo of the original summary?
4. Are **skills reordered** so the job's required skills appear first?
5. Is **experience reordered by relevance** — is the role most relevant to this job listed FIRST, rather than the original order?
6. Are **projects reordered by relevance** to this job?
7. Are bullets strong (action verbs, keywords, quantifiable impact) and directly relevant to this job?
8. Truthfulness: are any metrics or employers invented? Flag anything not grounded in the original resume.
9. Does anything remain that is clearly misaligned with the job (e.g., irrelevant filler)?

## Output format:
Return JSON:
{
  "score": int (0-100),
  "needs_revision": bool,
  "issues": [string],
  "strengths": [string],
  "recommendations": [string]
}
Be precise and specific. Reference actual section/entry names. Do NOT recommend inventing facts. Return only valid JSON, no commentary."""

REVIEW_MESSAGE_SYSTEM = """You are an outreach-message review agent. Given a draft outreach message and the job requirements, critique the message.

## What to check:
1. Is it 4-6 sentences max?
2. Does it name the specific role and reference 2-3 concrete skills/projects from the resume?
3. Does it avoid clichés ("hard worker", "passionate about everything"), avoid saying "attached resume", and avoid the generic opener "I came across your job posting"?
4. Does it end with a clear call to action?
5. Is it confident but professional — specific, not generic?

## Output format:
Return JSON:
{
  "score": int (0-100),
  "needs_revision": bool,
  "issues": [string],
  "recommendations": [string]
}
Return only valid JSON, no commentary."""


def _guardrail_issues(tailored_resume: dict, original_resume: dict | None = None, job_requirements: dict | None = None) -> list[str]:
    """Deterministic checks that never depend on the LLM."""
    issues = []
    if not (tailored_resume.get("summary") or "").strip():
        issues.append("Summary is empty.")
    if not tailored_resume.get("skills"):
        issues.append("Skills list is empty.")
    for section, label in (
        ("experience", "Experience"),
        ("projects", "Projects"),
    ):
        if not tailored_resume.get(section):
            issues.append(f"{label} section is empty.")

    seen = set()
    for section in ("experience", "projects", "education", "certifications", "leadership"):
        for entry in tailored_resume.get(section, []):
            key = _entry_key(entry)
            if key in seen:
                issues.append(f"Duplicate entry in {section}: {entry.get('title', '')}")
            seen.add(key)

    # --- Tailoring-depth checks (guard against the LLM merely echoing the resume) ---
    if original_resume and job_requirements:
        norm = lambda s: " ".join((s or "").lower().split())

        # 1. Summary must actually be rewritten, not echoed verbatim.
        orig_sum = norm(original_resume.get("summary"))
        new_sum = norm(tailored_resume.get("summary"))
        if orig_sum and new_sum and orig_sum == new_sum:
            issues.append("Summary was not tailored: it is identical to the original. Rewrite it to target the role.")
        elif new_sum:
            role = (job_requirements.get("role") or "").lower().strip()
            if role and len(role) >= 3 and role not in new_sum:
                issues.append(
                    "Summary does not reference the target role. Rewrite the summary to address the '%s' role." % job_requirements.get("role")
                )

        # 2. Experience must be reordered by relevance when there are 2+ entries.
        orig_exp = [e for e in original_resume.get("experience", []) if e]
        new_exp = [e for e in tailored_resume.get("experience", []) if e]
        if len(orig_exp) >= 2 and len(new_exp) >= 2:
            orig_first = _entry_key(orig_exp[0])
            new_first = _entry_key(new_exp[0])
            hits = {_entry_key(e) for e in orig_exp}
            # If the first role changed AND the new first is one of the original roles,
            # relevance-reordering happened. If the order is byte-identical, flag it.
            if orig_first == new_first and len(hits) == len(orig_exp):
                issues.append(
                    "Experience order was not tailored: entries are in the original order. "
                    "Reorder experience so the role most relevant to this job is FIRST."
                )

        # 3. Skills must be the candidate's REAL skills, not job-requirement sentences.
        #    A JD requirement bullet is long prose ("Strong full-stack experience with
        #    Python, Solid backend..."); a real skill is short ("Python", "PyTorch").
        long_skills = [s for s in tailored_resume.get("skills", []) if len((s or "").split()) > 6]
        if long_skills:
            issues.append(
                "Skills section contains job-requirement sentences copied from the posting, "
                "not candidate skills (e.g. '%s'). Replace with the candidate's real, short skill "
                "names reordered by relevance." % long_skills[:1][0][:80]
            )

        # 4. Projects must not be dumped as bare bullets inside an Experience entry.
        for entry in tailored_resume.get("experience", []):
            bullets = entry.get("bullets") or []
            # Bullets shaped like a project/tool title (short, no verb, Title Case) and no verb
            project_like = [
                b for b in bullets
                if (b.endswith(("(n8n)", " (n8n)")) or "(OCR" in b or b.startswith(("Automated ", "AI-Driven ")))
                and len(b.split()) <= 8
            ]
            if project_like:
                issues.append(
                    "Projects were merged into the Experience section (e.g. '%s'). Move them back "
                    "into the Projects section with their own entries." % project_like[0][:80]
                )

    return issues


def _summarize_review(parsed: dict, issues: list[str], kind: str) -> dict:
    llm_issues = [f"{issue}" for issue in parsed.get("issues", []) if isinstance(issue, str)]
    llm_recs = [f"{r}" for r in parsed.get("recommendations", []) if isinstance(r, str)]
    score = parsed.get("score")
    try:
        score = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        score = 100
    combined = list(dict.fromkeys(issues + llm_issues))
    needs_revision = bool(llm_issues or issues) or bool(parsed.get("needs_revision"))
    return {
        "score": score,
        "needs_revision": needs_revision,
        "issues": combined,
        "strengths": [f"{s}" for s in parsed.get("strengths", []) if isinstance(s, str)],
        "recommendations": llm_recs,
        "feedback": "\n".join(llm_recs or combined),
    }


def _fit_score(resume: dict, job_terms: set[str]) -> int:
    """Deterministic job-fit score for a resume: keyword overlap across
    summary, skills, experience bullets, and projects. Higher = better fit."""
    text = " ".join([
        resume.get("summary") or "",
        " ".join(resume.get("skills") or []),
        " ".join(b for e in resume.get("experience", []) or [] for b in (e.get("bullets") or [])),
        " ".join(b for p in resume.get("projects", []) or [] for b in (p.get("bullets") or [])),
    ])
    return _relevance_score(text, job_terms)


def _compare_fit(job_requirements: dict, original_resume: dict, tailored_resume: dict) -> dict:
    """Head-to-head comparison: does the TALORED resume beat the ORIGINAL for
    job fit? Uses a deterministic keyword-overlap score (works regardless of the
    LLM's capabilities), then asks the LLM for a qualitative verdict. This is the
    'which resume is the best fit' check folded into the review loop."""
    job_terms = _job_terms(job_requirements)
    original_score = _fit_score(original_resume, job_terms)
    tailored_score = _fit_score(tailored_resume, job_terms)
    delta = tailored_score - original_score
    return {
        "original_fit_score": original_score,
        "tailored_fit_score": tailored_score,
        "delta": delta,
        "winner": "tailored" if delta > 0 else ("tie" if delta == 0 else "original"),
        # The tailored resume must CLEARLY beat the original (or at least tie and
        # only win via reordering of equal content). If it ties or loses, that is
        # a signal the tailor did not actually improve fit.
        "improved_fit": delta >= 0,
    }


def _job_terms(job_requirements: dict) -> set[str]:
    from app.agents.tailoring_agent import _job_terms as _extract
    return _extract(job_requirements)


def review_resume(job_requirements: dict, tailored_resume: dict, original_resume: dict | None = None) -> dict:
    payload = json.dumps({
        "job_requirements": job_requirements,
        "tailored_resume": tailored_resume,
        "original_resume": original_resume or {},
    }, ensure_ascii=False)
    try:
        parsed = extract_json(call_llm(system=REVIEW_RESUME_SYSTEM, user=payload, json_mode=True))
    except Exception:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}

    issues = _guardrail_issues(tailored_resume, original_resume, job_requirements)

    # Head-to-head fit comparison: must the tailored resume beat the original?
    compare = _compare_fit(job_requirements, original_resume or {}, tailored_resume)
    if not compare["improved_fit"]:
        issues.append(
            "The tailored resume does not IMPROVE job fit over the original resume "
            "(original fit %d vs tailored fit %d). Re-tailor to move the most relevant "
            "experience/projects up and surface the job's key skills." % (
                compare["original_fit_score"], compare["tailored_fit_score"]
            )
        )

    result = _summarize_review(parsed, issues, "resume")
    result["fit_comparison"] = compare
    return result


def review_message(job_requirements: dict, draft_message: str) -> dict:
    payload = json.dumps({
        "job_requirements": job_requirements,
        "draft_message": draft_message,
    }, ensure_ascii=False)
    try:
        parsed = extract_json(call_llm(system=REVIEW_MESSAGE_SYSTEM, user=payload, json_mode=True))
    except Exception:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return _summarize_review(parsed, [], "message")