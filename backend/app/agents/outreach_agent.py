"""Module 3.6 from the blueprint — drafts a short outreach message.
Never sends anything itself; output is always routed through the
human-approval step before the Sender module touches it."""
import json
from app.agents.llm_client import call_llm, extract_json

SYSTEM_PROMPT = """You are a professional outreach message writer. Draft a concise, compelling message a candidate could send to a recruiter or hiring manager after applying for a job.

## Guidelines:
- Keep it to 4-6 sentences max. Recruiters are busy.
- Start with a clear statement of interest in the specific role and company.
- Mention 2-3 specific skills or experiences from the tailored resume that directly match the job requirements. Reference actual project names, technologies, or achievements.
- Show you've researched the company or role (reference something specific from the job posting).
- End with a clear call to action (e.g., "I'd welcome the opportunity to discuss how my experience aligns with your team's needs").
- Tone: confident but not arrogant, professional but not stiff, specific not generic.
- Do NOT use clichés like "I'm a hard worker" or "I'm passionate about everything". Show, don't tell.
- Do NOT mention "attached resume" — the resume is already included in the application.
- Do NOT start with "I came across your job posting" — too generic.

## What good looks like:
"With 3 years of hands-on ML engineering experience, including deploying production models on NVIDIA Jetson edge hardware and building 20+ n8n automation workflows, I'm excited about the Junior AI/ML Engineer role at [Company]. My work on the RGB-Thermal aerial detection pipeline (96.5% mAP@50) and enterprise automation systems aligns closely with your team's focus on applied ML and process optimization."

## What NOT to write:
- "I came across your job posting and believe my background is a strong fit"
- "I'm excited about this opportunity"
- "Please find my resume attached"
- Long paragraphs or bullet points
- Generic statements that could apply to any job

## Output format:
Return JSON: {"message": string}
Return only valid JSON, no commentary."""


def draft_outreach_message(job_requirements: dict, tailored_resume: dict, feedback: str | None = None) -> str:
    # Extract specific experience details for the message
    experience_details = []
    for exp in tailored_resume.get("experience", [])[:2]:
        experience_details.append({
            "title": exp.get("title", ""),
            "company": exp.get("company", ""),
            "top_bullets": exp.get("bullets", [])[:2],
        })

    # Extract relevant projects
    project_details = []
    for proj in tailored_resume.get("projects", [])[:2]:
        project_details.append({
            "name": proj.get("title", ""),
            "top_bullets": proj.get("bullets", [])[:1],
        })

    user_payload = json.dumps({
        "job_requirements": job_requirements,
        "resume_summary": tailored_resume.get("summary", ""),
        "top_skills": tailored_resume.get("skills", [])[:5],
        "key_experience": experience_details,
        "key_projects": project_details,
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
    return data.get("message", "")
