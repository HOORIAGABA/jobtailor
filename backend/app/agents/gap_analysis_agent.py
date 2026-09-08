"""Gap analysis agent — compares job requirements against the resume.

Flags JD requirements the candidate's resume does not support, so the user
knows what to add or clarify. Complements the tailoring step and the outreach
email (the email only claims facts that ARE in the resume).
"""
import json
from app.agents.llm_client import call_llm, extract_json


GAP_PROMPT = """You are a resume gap-analysis assistant. Given a job
description and a candidate's tailored resume, find the job requirements the
resume does NOT support.

## What to compare:
Compare the job requirements (role, required_skills, nice_to_have, keywords)
against the FULL content of the resume (summary, skills, experience, projects,
education, certifications, extra sections, projects, etc.).

## Output JSON:
{
  "missing_requirements": [
    {"requirement": string, "note": string}
  ]
}

## Rules:
- Only list JD items the resume does NOT clearly support.
- "requirement": name the exact requirement/technology/domain area from the job.
- "note": a short explanation of why it looks missing, plus any TRANSFERABLE
  evidence the resume already has (e.g. "Resume has OpenCV/feature-extraction
  work but no facial-landmark or MediaPipe experiments").
- Do NOT list things the resume already supports.
- If the resume supports everything, return an empty list.
- Keep the list concise (under 8 items). No fabrication.

## Input:
{
  "job_requirements": {company, role, required_skills, nice_to_have, seniority, keywords},
  "resume": { the tailored resume with all sections }
}
Return only valid JSON, no commentary."""


def analyze_missing_requirements(job_requirements: dict, tailored_resume: dict) -> list[dict]:
    payload = json.dumps({
        "job_requirements": job_requirements,
        "resume": tailored_resume,
    })
    try:
        response = call_llm(system=GAP_PROMPT, user=payload, json_mode=True)
        data = extract_json(response)
    except Exception:
        return []

    items = data.get("missing_requirements", [])
    if not isinstance(items, list):
        return []
    clean = []
    for item in items:
        if not isinstance(item, dict):
            continue
        requirement = str(item.get("requirement", "")).strip()
        note = str(item.get("note", "")).strip()
        if not requirement:
            continue
        clean.append({"requirement": requirement, "note": note})
    return clean