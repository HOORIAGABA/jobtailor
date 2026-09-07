"""Module 3.2 from the blueprint — extracts structured requirements
from a raw job posting."""
from app.agents.llm_client import call_llm, extract_json

SYSTEM_PROMPT = """You are a job posting requirements extractor. Given the
raw text of a job posting, extract the following JSON schema exactly.

Schema:
{
  "company": string,
  "role": string,
  "required_skills": [string],
  "nice_to_have": [string],
  "seniority": string,
  "keywords": [string]
}

Rules:
1. `company` = the company/organization name. Look for it at the top of
   the posting, in the header, or in phrases like "Join [Company]" or
   "[Company] is hiring". If not found, use an empty string.
2. `role` = the EXACT job title as posted (e.g. "Senior Backend Engineer",
   not generic "Software Engineer"). Use the full title including seniority.
3. Return only valid JSON, no commentary."""


def parse_job_post(raw_text: str) -> dict:
    response = call_llm(system=SYSTEM_PROMPT, user=raw_text, json_mode=True)
    return extract_json(response)
