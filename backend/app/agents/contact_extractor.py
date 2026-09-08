import re
from typing import Any

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\s\-().]{7,}\d)(?!\d)")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_SITE_RE = re.compile(r"^https?://(?:www\.)?([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", re.IGNORECASE)

_LINKEDIN_URL = re.compile(r"linkedin\.com/in/([A-Za-z0-9\-]+)", re.IGNORECASE)
_GITHUB_URL = re.compile(r"github\.com/([A-Za-z0-9.\-]+)", re.IGNORECASE)
_TWITTER_URL = re.compile(r"(?:twitter|x)\.com/([A-Za-z0-9_]+)", re.IGNORECASE)

_NON_LOCATION_WORDS = re.compile(
    r"(python|skills|experience|built|designed|achieved|using|framework|technologies|technologies|"
    r"pipeline|model|docker|kubernetes|react|node|java|c\+\+|typescript|sql|database|api|rest|"
    r"aws|gcp|azure|tensorflow|pytorch|llama|openai|langgraph|n8n|tensor|"
    r"fast|engineer|associate|specialist|developer|intern|lead|senior|junior|"
    r"automation|machine|learning|computer|vision|processing|data|science|"
    r"numpy|matplotlib|matplot|pandas|scikit|sklearn|scipy|keras|opencv|cv2|"
    r"yolo|flask|fastapi|django|uvicorn|fastify|jenkins|linux|ubuntu|windows|"
    r"mysql|mongodb|postgres|postgresql|redis|excel|tableau|powerbi|"
    r"github|gitlab|bitbucket|streamlit|gradio|selenium|beautifulsoup|"
    r"huggingface|transformers|langchain|chromadb|pinecone|weaviate|airflow|"
    r"visualization|charts|dashboards|frameworks|methodologies|agile|scrum|kubernetes|"
    r"google|slack|gmail|calendar|sheets|docs|drive|meet|gdrive|clickup|openai|"
    r"gemini|whatsapp|twilio|jira|notion|trello|asana)",
    re.IGNORECASE,
)

_PLACE = re.compile(
    r"(?<![a-zA-Z])([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)(?![a-zA-Z])"
)

def _github(text: str) -> str | None:
    m = _GITHUB_URL.search(text)
    if m:
        return "https://github.com/" + m.group(1)
    return None

def _linkedin(text: str) -> str | None:
    m = _LINKEDIN_URL.search(text)
    if m:
        return "https://linkedin.com/in/" + m.group(1)
    return None

def _looks_like_location(line: str) -> bool:
    """Return True if line resembles 'City, Country/State'."""
    # must contain a comma with something on both sides
    idx = line.find(",")
    if idx < 0 or idx == 0 or idx == len(line) - 1:
        return False
    # both halves should be non-trivial and not a non-location phrase
    before = line[:idx].strip()
    after = line[idx+1:].strip()
    if len(before) < 2 or len(after) < 2:
        return False
    if _NON_LOCATION_WORDS.search(line):
        return False
    # after the comma should start with a capital letter and look like a place
    if not after[0].isupper():
        return False
    # either a 2-letter country code or a place-like trailing token
    if re.search(r",\s*[A-Z]{2}\s*$", line) or _PLACE.search(line):
        return True
    # generic: has a comma, both sides capitalized-ish, no non-location words
    return bool(re.match(r"^[A-Z][^,]+,\s*[A-Z]", line))

def _normalize_phone(raw: str) -> str:
    """Strip mismatched parens and extra spaces from a phone number.
    +92) 3036800002 → +92 3036800002"""
    import re
    s = raw.strip()
    # remove lone closing parens with no opening: +92) → +92
    s = re.sub(r"(?<!\()\)", "", s)
    # remove lone opening parens with no closing: (92 → 92
    s = re.sub(r"\((?!\d{2,3}\))", "", s)
    # collapse multiple spaces
    s = " ".join(s.split())
    return s


def extract_contact_info(text: str) -> dict[str, Any]:
    """Scan raw resume text and return detected contact fields:
    {email, phone, location, linkedin, github, website}.

    Intended as a *fallback only*: it populates fields the user
    hasn't already filled in, so their explicit profile values
    always win.
    """
    out: dict[str, Any] = {}
    joined = "\n".join(text.splitlines())

    emails = _EMAIL_RE.findall(joined)
    if emails:
        out["email"] = emails[0]

    phones = _PHONE_RE.findall(joined)
    if phones:
        out["phone"] = _normalize_phone(phones[0])

    ln = _linkedin(joined)
    if ln:
        out["linkedin"] = ln

    gh = _github(joined)
    if gh:
        out["github"] = gh

    # Personal website (anything that looks like a site but isn't a known platform).
    for m in _URL_RE.finditer(joined):
        host = _SITE_RE.match(m.group(0))
        if not host:
            continue
        h = host.group(1).lower()
        if any(p in h for p in ("linkedin.com", "github.com", "twitter.com", "x.com")):
            continue
        if "website" not in out:
            out["website"] = m.group(0)

    # Location: find ALL "City, Country" candidates and pick the best one.
    # Use _looks_like_location to filter out false positives like "Ms, Fast".
    candidates = []
    for m in _PLACE.finditer(joined):
        candidate = m.group(0).strip()
        if _looks_like_location(candidate):
            candidates.append(candidate)
    if candidates:
        # prefer the longest match (most specific location)
        out["location"] = max(candidates, key=len)

    # Full name: top-of-resume line that is 2-4 words, each capitalised,
    # no digits, not a known heading keyword.
    heading_kw = {
        "summary", "experience", "education", "projects", "skills",
        "certifications", "leadership", "professional", "employment",
        "work history", "references", "awards", "publications",
        "languages", "interests", "volunteer", "training",
        "education and training", "projects experience",
    }
    name_kw = {"curriculum", "vitae", "resume", "cv"}
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        lowered = s.lower()
        if any(kw in lowered for kw in heading_kw):
            break
        if any(kw in lowered for kw in name_kw):
            continue
        if re.search(r"\d", s):
            continue
        words = s.split()
        if 2 <= len(words) <= 4 and all(w and w[0].isupper() for w in words):
            out["full_name"] = s
            break

    return out
