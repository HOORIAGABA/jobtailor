"""Module 3.0 from the blueprint — converts raw resume text into the
structured JSON schema the rest of the pipeline depends on.

This agent is wired to a dedicated extraction stack so it can run on a
Qwen3 or NuExtract 3 serving endpoint without affecting the rest of the
pipeline.
"""
from app.agents.llm_client import call_resume_parser_llm, extract_json

SYSTEM_PROMPT = """You are a resume parsing assistant. Given raw resume text,
extract it into the following JSON schema exactly. If a field is missing,
use an empty string or empty list — do invent content.

Schema:
{
  "full_name": string,
  "email": string,
  "phone": string,
  "location": string,
  "linkedin": string,
  "github": string,
  "summary_heading": string,
  "summary": string,
  "skills": [string],
  "experience": [
    {"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}
  ],
  "projects": [
    {"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}
  ],
  "education": [
    {"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}
  ],
  "certifications": [
    {"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}
  ],
  "leadership": [
    {"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}
  ]
}

Rules:
1. Extract EVERY section of the resume — do not skip any section. Common
   section headings include (but are not limited to): "WORK EXPERIENCE",
   "EXPERIENCE", "PROFESSIONAL EXPERIENCE", "PROJECTS", "PROJECT
   EXPERIENCE", "EDUCATION", "EDUCATION AND TRAINING", "EDUCATION &
   TRAINING", "ACADEMIC BACKGROUND", "SKILLS", "TECHNICAL SKILLS",
   "SUMMARY", "PROFESSIONAL SUMMARY", "PROFILE", "CERTIFICATIONS",
   "PUBLICATIONS", "AWARDS", "VOLUNTEER", "LANGUAGES", "INTERESTS",
   "REFERENCES", "AWARDS AND HONORS", "COURSES", "TRAINING",
   "LEADERSHIP", "LEADERSHIP & COMMUNITY", "MANAGEMENT AND LEADERSHIP
   SKILLS", "COMMUNITY", "VOLUNTEERING", "HONORS",
   "CERTIFICATES", "LICENSES", "LICENSURE".
   If a section doesn't fit any category above, still extract it — put
   its heading in the `heading` field of the most relevant array
   (experience for work-related, projects for personal/academic work,
   education for academic, skills for skill lists,
   certifications for certificates/licenses/courses,
   leadership for volunteer/community/awards/honors) or skip only if
   no array matches at all.
2. Preserve the original section heading in the `heading` field.
3. Each experience/project entry MUST have at least one bullet. Group all
   lines between one title line and the next title line as bullets of the
   same entry.
4. `title` = job title or project name. `company` = employer or context.
   If the resume has "Company Name" on its own line before the job title,
   put it in `company`.
5. `dates` = the date range string (e.g. "Jan 2022 - Present"). Keep as-is.
6. Contact info (phone, email, location, LinkedIn, GitHub) does NOT belong
   in summary. Extract it into the top-level contact fields instead.
7. For skills, extract ALL technical skills mentioned anywhere in the resume.
8. For education, extract degree name as `title`, institution as `company`,
   and date range as `dates`. Education entries have no bullets.
9. For certifications, extract the certificate/course name as `title` and
   the issuing organization as `company` (e.g. "University of Michigan").
10. For leadership/volunteer/community/awards, extract the role (e.g.
   "Co-Ambassador, Google Developer Groups") as `title`.
11. NEVER skip a section. If you see any heading that looks like a resume
   section, you MUST extract its content into the appropriate array.
12. Return only valid JSON, no commentary.

Example input:
HOORIA ATTAS
hooria@gaba.com | +92 3036800002 | https://linkedin.com/in/hooria-attas | https://github.com/hooria-attas | Islamabad, Pakistan
WORK EXPERIENCE
Acme Corp
Senior DevOps Engineer | Jan 2024 - Present
- Designed and deployed cloud infrastructure on AWS
- Automated CI/CD pipelines reducing deploy time by 60%

PROJECTS
Infrastructure-as-Code Toolkit (2024)
- Built Terraform modules for multi-cloud deployments

EDUCATION AND TRAINING
01/10/2021 - 17/07/2025 Islamabad, Pakistan
BACHELOR OF SCIENCE IN COMPUTER SCIENCE Institute of Space Technology

Example output:
{
  "full_name": "Hooria Attas",
  "email": "hooria@gaba.com",
  "phone": "+92 3036800002",
  "location": "Islamabad, Pakistan",
  "linkedin": "https://linkedin.com/in/hooria-attas",
  "github": "https://github.com/hooria-attas",
  "summary_heading": "Summary",
  "summary": "",
  "skills": [],
  "experience": [
    {"heading": "WORK EXPERIENCE", "title": "Senior DevOps Engineer", "company": "Acme Corp", "dates": "Jan 2024 - Present", "bullets": ["Designed and deployed cloud infrastructure on AWS", "Automated CI/CD pipelines reducing deploy time by 60%"]}
  ],
  "projects": [
    {"heading": "PROJECTS", "title": "Infrastructure-as-Code Toolkit", "company": "", "dates": "2024", "bullets": ["Built Terraform modules for multi-cloud deployments"]}
  ],
  "education": [
    {"heading": "EDUCATION AND TRAINING", "title": "BACHELOR OF SCIENCE IN COMPUTER SCIENCE", "company": "Institute of Space Technology", "dates": "01/10/2021 - 17/07/2025", "bullets": []}
  ],
  "certifications": [
    {"heading": "CERTIFICATIONS", "title": "Intro to ChatGPT and Generative AI", "company": "365 Data Science", "dates": "", "bullets": []}
  ],
  "leadership": [
    {"heading": "MANAGEMENT AND LEADERSHIP SKILLS", "title": "Co-Ambassador, Google Developer Groups on Campus", "company": "", "dates": "", "bullets": ["Organized technical workshops for students"]}
  ]
}"""


def parse_resume_text(raw_text: str) -> dict:
    response = call_resume_parser_llm(system=SYSTEM_PROMPT, user=raw_text, json_mode=True)
    return extract_json(response)
