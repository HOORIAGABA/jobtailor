"""Outreach agent — drafts a short message for the recruiter.

Sends full resume context (not truncated) so the LLM can reference
specific skills, projects, and experience accurately.
"""
import re
import json
from app.agents.llm_client import call_llm, extract_json

SYSTEM_PROMPT = """You are an expert email writer. Draft a complete, polished,
application email for a specific job, based solely on the candidate's resume
and the job requirements provided.

## Email structure — follow exactly, in order:
1. Subject line: "Application for {ROLE} Position – {Candidate Full Name}"
2. Greeting: "Dear {Company} Hiring Team,"
3. Opening paragraph (2-3 sentences): state interest in the specific role and
   company, then a one-sentence summary of why the candidate is a strong fit.
4. "Relevant Experience Highlights": 2-4 bullet points. Each bullet: role,
   company, and date range (e.g. "Machine Learning Associate at NexPred
   Solutions (Mar–Aug 2026)"), followed by the 1-2 strongest achievements for
   THIS job, taken only from the resume.
5. "Selected Projects" (if the resume has projects): 2-3 bullets, each with the
   project name and its 1-2 strongest achievements relevant to the job.
6. "Technical Skills": one compact sentence listing the comma-separated skills
   most relevant to THIS job (from the resume's skills only).
7. One sentence on education and the top certification, if present in the resume.
8. Closing paragraph (2-3 sentences): enthusiasm for the role/company and a
   clear call to action (e.g. request an interview or further discussion).
9. Signature block using ONLY the candidate's real contact info from the resume:
   {full_name}
   Email: {email}
   Phone: {phone}
   LinkedIn: {linkedin}

## Rules:
- Use ONLY facts present in the resume. NEVER invent experience, employers,
  projects, metrics, skills, or contact details.
- Keep metrics, numbers, titles, and dates exactly as they appear in the resume.
- Reorder and highlight existing content to match the job; do not fabricate.
- Tone: professional, confident, specific — not generic filler.
- This email will be sent with the tailored resume attached, so referencing the
  attachment is fine.

## Input:
The "resume" field is the full parsed resume and includes the candidate's
full_name, email, phone, location, linkedin, and github.

## Output format:
Return JSON: {"subject": string, "message": string}
- "subject" is ONLY the subject line.
- "message" is the full email body starting with the greeting and ending with
  the signature block. Do NOT include the subject inside "message".
Return only valid JSON, no commentary."""


def _estimate_years(resume: dict) -> int | None:
    summary = resume.get("summary", "") if isinstance(resume, dict) else ""
    m = re.search(r'(\d{1,2})\+?\s*years?', summary or "", re.IGNORECASE)
    if m:
        return int(m.group(1))
    year_pattern = re.compile(r'(20\d{2})')
    all_years = []
    for exp in (resume.get("experience") or []) if isinstance(resume, dict) else []:
        dates = exp.get("dates", "") or ""
        years = year_pattern.findall(dates)
        all_years.extend(int(y) for y in years)
    if all_years:
        span = max(all_years) - min(all_years) + 1
        return max(span, 1)
    return None


def _check_fabrication(message: str, tailored_resume: dict) -> str | None:
    msg_lower = message.lower()
    m = re.search(r'(\d{1,2})\+?\s*years?\s*(?:of\s+)?(?:experience|hands-on)', msg_lower)
    if m:
        claimed = int(m.group(1))
        resume_years = _estimate_years(tailored_resume)
        if resume_years and claimed > resume_years + 1:
            return (
                f"Message claims {claimed}+ years of experience but resume indicates ~{resume_years} years. "
                f"Revise to match the candidate's actual experience."
            )
    return None


def draft_outreach_message(job_requirements: dict, tailored_resume: dict, feedback: str | None = None) -> str:
    user_payload = json.dumps({
        "job_requirements": job_requirements,
        "resume": tailored_resume,
        "previous_feedback": feedback or "",
    })
    system = SYSTEM_PROMPT
    if feedback:
        system = SYSTEM_PROMPT + "\n\n## Previous review — fix these issues:\n" + feedback
    response = call_llm(system=system, user=user_payload, json_mode=True)
    try:
        data = extract_json(response)
    except (json.JSONDecodeError, ValueError):
        return ""
    subject = str(data.get("subject", "")).strip()
    msg = data.get("message", "")

    fab_error = _check_fabrication(msg, tailored_resume)
    if fab_error:
        retry_payload = json.dumps({
            "job_requirements": job_requirements,
            "resume": tailored_resume,
            "previous_feedback": (feedback or "") + "\n" + fab_error,
        })
        system2 = SYSTEM_PROMPT + "\n\n## Critical correction:\n" + fab_error
        response2 = call_llm(system=system2, user=retry_payload, json_mode=True)
        try:
            data2 = extract_json(response2)
            msg = data2.get("message", msg)
            subject = str(data2.get("subject", subject)).strip()
        except (json.JSONDecodeError, ValueError):
            pass

    if subject:
        return f"Subject: {subject}\n\n{msg}".strip()
    return msg.strip()
