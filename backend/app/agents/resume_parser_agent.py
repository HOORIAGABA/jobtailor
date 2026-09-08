"""Resume parser - converts raw resume text into structured JSON.

Extracts ALL content including name, contact info, and any extra sections
the resume contains (publications, languages, volunteering, awards, etc.).
"""
import re

from app.agents.llm_client import call_resume_parser_llm, extract_json

SYSTEM_PROMPT = """You are a resume parser. Read the resume text and output ONLY the JSON below.

Extract exactly what is written in the resume. Never invent information and never
add placeholder text. If a section or field is absent, leave it empty ("" for
strings, [] for lists).

{
  "full_name": "",
  "email": "",
  "phone": "",
  "location": "",
  "linkedin": "",
  "github": "",
  "summary": "",
  "skills": [],
  "experience": [],
  "projects": [],
  "education": [],
  "certifications": [],
  "leadership": [],
  "extra_sections": []
}

Entry shape used in experience / projects / education / certifications / leadership:
{"heading": "", "title": "", "company": "", "dates": "", "bullets": []}

extra_sections item: {"heading": "", "entries": [ same entry shape ]}

Rules:
- full_name: the person's name, usually the first line. Never take "RESUME"/"CV"
  as the name.
- email, phone, location, linkedin, github: copy them exactly as they appear,
  from anywhere in the document.
- experience: only entries that name an employer/company. title = job title,
  company = employer.
- projects: entries with no employer/company (personal or academic work).
  When in doubt, classify as a project, not paid employment.
- education: include the school and the degree. When a degree is written out
  (e.g. "Bachelor of Science in Computer Science"), write it as "BS Computer Science".
- certifications: named certificates with their issuing body, e.g.
  "AWS Solutions Architect - Amazon Web Services".
- skills: a FLAT list of EVERY individual skill/tool/language named anywhere in
  the resume (skills section, bullets, project descriptions, tools list), e.g.
  ["Python", "PyTorch", "FastAPI", "TensorRT", "ONNX"]. Never include a category
  or group heading such as "Tools & Technologies" as a skill.
- summary: the profile/summary paragraph, written verbatim.
- bullets: the bullet lines exactly as written - do not summarize or rewrite.
- dates: keep the original text as-is (e.g. "Jan 2022 - Present").
- heading: the original section heading as it appears (e.g. "WORK EXPERIENCE").
- extra_sections: every other section. NEVER DROP CONTENT - any section that
  does not fit the keys above, keep it here with its original heading. Never
  silently discard content.
- Output valid JSON only, with no other text.
"""


def _split_skill_string(s: str) -> list[str]:
    """Split a skill string on commas, but NOT inside parentheses/brackets
    (e.g. 'Transfert learning architectures (VGG16, VGG19, MobileNet V3, ...)'
    must stay whole, while 'Python, PyTorch' splits into two skills)."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


def _flatten_skill_items(val, out: list) -> None:
    """Recursively collect leaf skill strings from a (possibly nested) value."""
    if isinstance(val, str):
        for piece in _split_skill_string(val):
            if piece:
                out.append(piece)
    elif isinstance(val, list):
        for item in val:
            _flatten_skill_items(item, out)
    elif isinstance(val, dict):
        for child in val.values():
            _flatten_skill_items(child, out)


def _looks_like_group_heading(skill: str) -> bool:
    """Heuristic: is this a skill-section SUBGROUP HEADING rather than an actual
    skill? Headings are things like 'Computer Vision & Image Processing',
    'AI/ML Frameworks & Techniques', 'Backend & Production Pipelines',
    'Automation & Integration' - labels, not the atomizers they organize.

    A real skill is a short tool/tech/competency name (`YOLO`, `PyTorch`,
    `FastAPI`, `n8n`). A heading is longer, connective, and abstract."""
    s = skill.strip()
    if not s or s.lower() in {
        "technical skills", "skills", "tools", "technologies", "languages",
        "core skills", "key skills", "certifications", "frameworks",
    }:
        return True

    words = s.split()
    # 'n8n', 'YOLO', 'FastAPI' are single tokens - never headings.
    if len(words) == 1:
        return False

    lower = s.lower()
    # Ends in an abstract group noun.
    if lower.endswith(("frameworks", "techniques", "tools", "skills", "stacks",
                       "libraries", "platforms", "technologies", "methods")):
        return True
    # Contains & or 'and' joining two abstract nouns -> almost always a heading.
    if "&" in s or " and " in lower:
        return True
    # "Core Competencies" -- a short heading whose words are ALL generic
    # competency/group nouns (no concrete tool is named).
    generic = {
        "core", "key", "technical", "programming", "professional", "soft",
        "competencies", "skills", "tools", "technologies", "languages",
        "frameworks", "libraries", "platforms", "domains", "areas", "expertise",
        "strengths", "stacks", "capabilities", "interests", "concepts", "basics",
        "fundamentals", "miscellaneous", "other",
    }
    if all(w.lower() in generic for w in words):
        return True
    # Long abstract phrases with no recognizable concrete tool token.
    return False


def _clean_skills(raw_skills) -> list[str]:
    """Flatten nested skills and drop subgroup-heading labels."""
    items: list[str] = []
    _flatten_skill_items(raw_skills, items)
    cleaned = []
    for skill in items:
        if _looks_like_group_heading(skill):
            continue
        if skill not in cleaned:
            cleaned.append(skill)
    return cleaned


def _split_education(entry: dict) -> dict:
    """M5.4: split the education entry into degree / field / institution and
    flag in-progress education.

    The LLM usually emits title/company (e.g. title="BS Computer Science",
    company="FAST-NUCES"). We go one step further and split degree vs field
    deterministically, and set `in_progress` from the date_end (empty or future
    graduation date => in progress). Never invents GPA/honors."""
    title = str(entry.get("title", "")).strip()
    company = str(entry.get("company", "")).strip()

    degree = entry.get("degree")
    field = entry.get("field")
    inst = entry.get("institution")

    if not degree or not isinstance(degree, str) or not degree.strip():
        degree = _extract_degree(title)
    if not field or not isinstance(field, str) or not field.strip():
        field = _extract_field(title)
    if not inst or not isinstance(inst, str) or not inst.strip():
        inst = company

    entry["degree"] = degree or ""
    entry["field"] = field or ""
    entry["institution"] = inst or ""

    if entry.get("in_progress") is None:
        entry["in_progress"] = _is_education_in_progress(entry)
    return entry


_DEGREE_ABBREVS = [
    "BS", "BSc", "MS", "MSc", "PhD", "BBA", "MBA", "BA", "MA", "BEng", "MEng",
    "BE", "ME", "B.Tech", "M.Tech", "BTech", "MTech", "LLB", "DDS", "MD", "JD",
    "EdD", "MFA", "BSN", "MPH", "MEd",
]
_DEGREE_WORDS = ["associate", "associates", "bachelor", "bachelors", "master",
                 "masters", "doctor", "diploma", "dual degree"]


def _degree_match(title: str):
    """Return (normalized_degree, chars_consumed_in_original) or (None, None).

    Abbreviations ('BS', 'B.Sc.', 'BBA') must be the resume's leading token
    (dots/spaces-insensitive equality with the abbreviation); full degree words
    ('Bachelor', 'Master', ...) must be a leading word."""
    t = title.strip()
    if not t:
        return None, None
    tl = t.lower()
    first_token = tl.split()[0] if tl.split() else tl
    first_token = re.sub(r"[.\s]", "", first_token)

    for tok in _DEGREE_ABBREVS:
        tok_norm = tok.lower().replace(".", "").replace(" ", "")
        if first_token == tok_norm:
            norm = tok_norm.upper()
            norm = {"BSC": "BS", "MSC": "MS", "BBA": "BBA", "MBA": "MBA",
                    "BTECH": "BTech", "MTECH": "MTech", "BENG": "BEng",
                    "MENG": "MEng", "PHD": "PhD", "LLB": "LLB", "DDS": "DDS",
                    "BSN": "BSN", "MFA": "MFA", "MPH": "MPH", "MED": "MEd",
                    }.get(norm, norm)
            return norm, len(t.split()[0])

    for word in sorted(_DEGREE_WORDS, key=len, reverse=True):
        if tl.startswith(word):
            norm = {
                "bachelor": "BS", "bachelors": "BS",
                "master": "MS", "masters": "MS",
                "doctor": "PhD",
                "associate": "Associate", "associates": "Associate",
                "diploma": "Diploma",
                "dual degree": "Dual",
            }[word]
            return norm, len(word)
    return None, None


def _extract_degree(title: str) -> str:
    norm, _ = _degree_match(title)
    return norm or ""


def _extract_field(title: str) -> str:
    _, consumed = _degree_match(title)
    if consumed is None:
        return ""
    rest = title[consumed:].strip()
    leading = title.split()[0] if title.split() else ""
    leading_norm = re.sub(r"[.\s]", "", leading).lower()
    is_abbrev = any(leading_norm == tok.lower().replace(".", "").replace(" ", "")
                    for tok in _DEGREE_ABBREVS)

    # Abbreviation form: 'BS Computer Science' -> 'Computer Science' (major).
    if is_abbrev:
        return rest.strip(" ,:|-（）()")

    # Full-name form:
    # 'Master of Science in Data Science' -> 'Data Science'
    # 'Bachelor of Science'              -> '' (no specific major)
    m = re.match(r"^(?:(?:of|in)\s+)?([A-Za-z &\-]+?)(?:\s+in\s+(.+))?$", rest)
    if m:
        degree_subject = m.group(1).strip()
        in_field = (m.group(2) or "").strip()
        if in_field:
            return in_field
        if degree_subject.lower() in {
            "science", "engineering", "arts", "business administration",
            "philosophy", "fine arts", "law", "medicine", "education",
            "public health", "computer science", "applied science", "studies",
        }:
            return ""
        return degree_subject
    return rest.strip(" ,:|-（）()")


def _is_education_in_progress(entry: dict) -> bool:
    """A degree is in progress if it has no end date OR its end date is clearly
    in the future (expected graduation)."""
    end = str(entry.get("date_end") or "").strip()
    if entry.get("date_ongoing"):
        return True
    if not end:
        return True  # no end date -> treat as ongoing
    m = re.match(r"^(\d{4})(?:-\d{2})?(?:-\d{2})?$", end)
    if not m:
        return False
    year = int(m.group(1))
    from datetime import date as _date

    return year > _date.today().year


def _normalize_entry_metadata(entry: dict) -> dict:
    """Canonicalize the date fields on a resume entry (M5.3).

    Keeps `dates` as a clean display string and attaches parsed `date_start`,
    `date_end`, `date_ongoing` so downstream (tailoring relevance reorder,
    outreach year estimation, renderer) never have to re-parse free text."""
    from app.agents.date_utils import parse_date_range

    entry = dict(entry)
    raw_dates = str(entry.get("dates", "")).strip()
    if raw_dates:
        parsed = parse_date_range(raw_dates)
        entry["dates"] = parsed["display"] or raw_dates
        entry["date_start"] = parsed["start"]
        entry["date_end"] = parsed["end"]
        entry["date_ongoing"] = parsed["ongoing"]
    else:
        entry.setdefault("date_start", "")
        entry.setdefault("date_end", "")
        entry.setdefault("date_ongoing", False)
    return entry


def _normalize_link(value: str) -> str:
    """Normalize a LinkedIn/GitHub handle to a canonical https:// URL so the
    renderer and email never emit a bare handle like 'www.linkedin.com/in/x'."""
    s = value.strip()
    if not s:
        return s
    low = s.lower()
    if "linkedin.com" in low and not low.startswith("http"):
        slug = re.sub(r"^.*?linkedin\.com(/in/[\w.\-]+).*$", r"\1", s)
        return "https://linkedin.com" + slug
    if "github.com" in low and not low.startswith("http"):
        slug = re.sub(r"^.*?github\.com(/[\w.\-]+).*$", r"\1", s)
        return "https://github.com" + slug
    return s


# LLMs occasionally invent their own top-level section keys (observed:
# "work_experience", "management_and_leadership_skills"). Map them onto the
# canonical schema before validation so the entries get full M5.3/M5.4
# normalization instead of surviving as orphans.
_SECTION_ALIASES = {
    "work_experience": "experience",
    "work_experiences": "experience",
    "professional_experience": "experience",
    "employment_experience": "experience",
    "employment_history": "experience",
    "work_history": "experience",
    "technologies": "skills",
    "technology": "skills",
    "management_and_leadership_skills": "leadership",
    "management_and_leadership": "leadership",
    "leadership_experience": "leadership",
    "volunteer_experience": "leadership",
    "volunteering": "leadership",
}

# Canonical top-level keys - everything else the LLM emits as a list of entry
# dicts is folded into extra_sections so content is never silently dropped.
_CANONICAL_KEYS = {
    "full_name", "email", "phone", "location", "linkedin", "github",
    "summary_heading", "summary", "skills", "experience", "projects",
    "education", "certifications", "leadership", "extra_sections",
}

# Words that prove a "location" value is really a tool stack / skill list.
_LOCATION_BLOCKED_TOKENS = {
    "numpy", "matplotlib", "pandas", "scipy", "scikit", "sklearn", "keras",
    "pytorch", "tensorflow", "opencv", "yolo", "flask", "fastapi", "django",
    "uvicorn", "docker", "kubernetes", "jenkins", "linux", "mysql", "mongodb",
    "postgres", "postgresql", "redis", "github", "gitlab", "n8n", "mlflow",
    "langchain", "rag", "gpt", "llm", "streamlit", "airflow", "pipeline",
    "api", "dataset", "algorithm", "framework", "automation", "bot",
    # SaaS/API platforms that leak into location (observed: "Google, Slack").
    "google", "slack", "clickup", "jira", "trello", "notion", "asana",
    "openai", "gemini", "anthropic", "davinci", "whatsapp", "twilio",
    "crm", "saas", "mailchimp", "zapier", "postman", "figma", "hubspot",
    "salesforce", "aws", "azure", "gcp", "firebase", "vercel", "sentry",
    "prometheus", "grafana", "dockerhub", "pypi", "coursera", "udemy",
    "datacamp", "forage",
}


def _looks_like_tech(value) -> bool:
    """True when a 'location' value is actually a skills/tool phrase."""
    toks = {t.lower() for t in re.findall(r"[a-z][a-z0-9.#+\-]*", str(value).lower())}
    return bool(toks & _LOCATION_BLOCKED_TOKENS)


def _guard_location(result: dict) -> dict:
    """Discard an implausible LLM location so the deterministic fallback can
    fill the real one (observed garbage: 'Numpy, Matplotlib' as location)."""
    if _looks_like_tech(result.get("location")):
        result["location"] = ""
    return result


def _normalize_entry_fields(entry: dict) -> dict:
    """Map LLM field-name drift to schema keys (position -> title, city +
    country -> location) before metadata normalization."""
    title = str(entry.get("title", "")).strip()
    if not title:
        entry["title"] = str(entry.get("position", "")).strip()
    city = str(entry.get("city", "")).strip()
    country = str(entry.get("country", "")).strip()
    if city or country:
        entry["location"] = ", ".join(p for p in (city, country) if p)
    # Drop drift-key noise when they carry nothing (keep output schema-clean).
    for k in ("position", "city", "country"):
        entry.pop(k, None)
    return entry


def _drop_empty_entries(entries) -> list:
    """Remove entries the model emitted with NO real content (e.g. two blank
    skeleton certification objects). An entry counts as empty only if every
    field - heading, title, degree/field/institution, company, dates, bullets -
    is blank. A date-only education row is still real and is kept."""
    cleaned = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        parts = [
            str(e.get(k, ""))
            for k in ("heading", "title", "degree", "field", "institution",
                      "company", "dates", "location")
        ]
        parts += [str(b) for b in (e.get("bullets") or []) if b]
        if "".join(parts).strip():
            cleaned.append(e)
    return cleaned


def _drop_empty_extra_sections(sections) -> list:
    cleaned = []
    for s in sections or []:
        if not isinstance(s, dict):
            continue
        heading = str(s.get("heading", "")).strip()
        entries = _drop_empty_entries(s.get("entries"))
        if heading or entries:
            cleaned.append({"heading": heading, "entries": entries})
    return cleaned


def _entry_key(entry: dict) -> str:
    """Dedupe key: title + dates (+ company when present). Identical rows the
    LLM emitted twice (observed duplicate project entries) collapse to one."""
    title = re.sub(r"\s+", " ", str(entry.get("title", "")).strip()).lower()
    dates = re.sub(r"\s+", " ", str(entry.get("dates", "")).strip()).lower()
    company = re.sub(r"\s+", " ", str(entry.get("company", "")).strip()).lower()
    for part in (title, dates):
        if part:
            return f"{part}|{company or ''}"
    return ""


def _dedupe_entries(entries) -> list:
    seen = set()
    deduped = []
    for e in entries:
        key = _entry_key(e)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(e)
    return deduped


def _fold_invented_keys(result: dict) -> dict:
    """Any remaining top-level key the LLM invented (not an alias, not part of
    the canonical schema) that holds a list of entry dicts is preserved as an
    extra_sections block - content is never silently dropped."""
    folded = []
    for key in list(result.keys()):
        if key in _CANONICAL_KEYS or key in _SECTION_ALIASES:
            continue
        val = result[key]
        if not isinstance(val, list) or not val or not all(isinstance(x, dict) for x in val):
            continue
        folded.append({"heading": key.replace("_", " ").upper(), "entries": val})
        result.pop(key, None)
    if folded:
        extra = result.get("extra_sections")
        if not isinstance(extra, list):
            extra = []
        result["extra_sections"] = extra + folded
    return result


def _validate_output(data: dict) -> dict:
    """Ensure all required keys exist with correct types."""
    result = dict(data)

    # Alias normalization: fold invented keys into canonical sections.
    for alias, canonical in _SECTION_ALIASES.items():
        alias_val = result.get(alias)
        if not isinstance(alias_val, list) or not alias_val:
            continue
        existing = result.get(canonical)
        if not isinstance(existing, list):
            existing = []
        result[canonical] = existing + alias_val
        result.pop(alias, None)

    # Any other invented entry-list keys -> extra_sections (no silent drop).
    result = _fold_invented_keys(result)

    for key in ("full_name", "email", "phone", "location", "linkedin", "github",
                "summary_heading", "summary"):
        if not isinstance(result.get(key), str):
            result[key] = ""

    result["linkedin"] = _normalize_link(result.get("linkedin", ""))
    result["github"] = _normalize_link(result.get("github", ""))

    result["skills"] = _clean_skills(result.get("skills"))

    for key in ("experience", "projects", "education", "certifications", "leadership"):
        val = result.get(key)
        if not isinstance(val, list):
            result[key] = []
        else:
            cleaned = []
            for entry in val:
                if not isinstance(entry, dict):
                    continue
                normalized = _normalize_entry_metadata(_normalize_entry_fields({
                    "heading": str(entry.get("heading", "")),
                    "title": str(entry.get("title", "")),
                    "position": str(entry.get("position", "")),
                    "city": str(entry.get("city", "")),
                    "country": str(entry.get("country", "")),
                    "company": str(entry.get("company", "")),
                    "dates": str(entry.get("dates", "")),
                    "bullets": [str(b) for b in entry.get("bullets", []) if b],
                }))
                if key == "education":
                    _split_education(normalized)
                cleaned.append(normalized)
            result[key] = cleaned

    extra = result.get("extra_sections")
    if not isinstance(extra, list):
        result["extra_sections"] = []
    else:
        cleaned_extra = []
        for section in extra:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading", ""))
            entries = section.get("entries", [])
            if not isinstance(entries, list):
                entries = []
            cleaned_entries = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                cleaned_entries.append(_normalize_entry_metadata({
                    "title": str(entry.get("title", "")),
                    "company": str(entry.get("company", "")),
                    "dates": str(entry.get("dates", "")),
                    "bullets": [str(b) for b in entry.get("bullets", []) if b],
                }))
            if heading and cleaned_entries:
                cleaned_extra.append({"heading": heading, "entries": cleaned_entries})
        result["extra_sections"] = _drop_empty_extra_sections(cleaned_extra)

    # Do not ship placeholder entries the model invented with no content.
    for key in ("experience", "projects", "education", "certifications", "leadership"):
        result[key] = _drop_empty_entries(result.get(key))

    # Collapse duplicate entries the model emitted twice.
    for key in ("experience", "projects", "certifications", "leadership"):
        result[key] = _dedupe_entries(result.get(key))

    return result


def _apply_contact_fallback(result: dict, raw_text: str) -> dict:
    """Fill empty contact fields from the deterministic regex extractor.
    The LLM can miss location/github/etc. when they appear in labeled fields,
    footers, or bullets - the regex pass is a reliable backstop. Explicit LLM
    values always win; we only fill empties."""
    from app.agents.contact_extractor import extract_contact_info

    detected = extract_contact_info(raw_text)
    for field in ("email", "phone", "location", "linkedin", "github", "full_name"):
        if not (result.get(field) or "").strip() and (detected.get(field) or "").strip():
            result[field] = detected[field]

    # M5.6: if no name could be found (e.g. name rendered as an image/logo),
    # derive a placeholder from the email's local part - never leave it blank.
    if not (result.get("full_name") or "").strip():
        email = (result.get("email") or "").strip()
        if email:
            local = email.split("@")[0].strip()
            if local:
                name = " ".join(w.capitalize() for w in re.split(r"[._\-+]+", local) if w)
                if name:
                    result["full_name"] = name
    return result


def parse_resume_text(raw_text: str) -> dict:
    """Parse raw resume text into structured JSON via LLM, with a deterministic
    contact-field fallback and skill/classification guards."""
    response = call_resume_parser_llm(system=SYSTEM_PROMPT, user=raw_text, json_mode=True)
    parsed = extract_json(response)
    result = _validate_output(parsed)
    # Recover entry titles the model drained (only when content exists; each
    # patched title must appear verbatim in the resume text).
    result = _restore_missing_titles(result, raw_text)
    # Discard an implausible LLM location (e.g. a tools phrase) BEFORE the
    # fallback, so the deterministic extractor fills in the real location.
    result = _guard_location(result)
    result = _apply_contact_fallback(result, raw_text)

    # Fix #1 (location: labeled 'City:'/'Country:'): if location is still empty
    # AND the raw text has a labeled City field, combine it deterministically.
    if not (result.get("location") or "").strip():
        _fix_labeled_location(result, raw_text)
    return result


def _fix_labeled_location(result: dict, raw_text: str) -> None:
    """Handle the labeled 'City: X | Country: Y' (or 'City: X'/'Country: Y')
    pattern that the LLM sometimes fails to combine into a single location."""
    import re

    _SEP = r"(?:[|\n]|$)"
    city = re.search(r"City[:\s]+([^|\n]*?)" + _SEP, raw_text, re.I)
    country = re.search(r"Country[:\s]+([^|\n]*?)" + _SEP, raw_text, re.I)
    parts = []
    if city and city.group(1).strip():
        parts.append(re.sub(r"\s+", " ", city.group(1).strip()))
    if country and country.group(1).strip():
        parts.append(re.sub(r"\s+", " ", country.group(1).strip()))
    if parts:
        result["location"] = ", ".join(parts)


_REPAIR_PROMPT = """Entries below are missing their "title", but each entry has other
content. The original resume text is provided. For each entry, find the true title
VERBATIM in the resume text. Return ONLY JSON:
{"titles": [{"id": <number>, "title": "<title or empty>"}]}
Use an empty string when the resume really has no title for that entry."""


def _missing_title_entries(result: dict) -> dict:
    """Map section -> [entry index] for entries that have real content but an
    empty title (the observed 'project/cert titles drained' failure)."""
    missing = {}
    for key in ("experience", "projects", "certifications", "leadership"):
        for i, e in enumerate(result.get(key) or []):
            title = str(e.get("title", "")).strip()
            body = [str(e.get(k, "")) for k in ("dates", "company")]
            body += [str(b) for b in (e.get("bullets") or []) if b]
            if not title and "".join(body).strip():
                missing.setdefault(key, []).append(i)
    return missing


def _title_is_in_raw(title: str, raw_text: str) -> bool:
    """Only accept a repaired title that literally appears in the resume text -
    keeps the 'never invent content' guarantee even if the repair model
    hallucinates a plausible-looking title."""
    t = re.sub(r"\s+", " ", title).strip().lower()
    if not t:
        return True  # explicit "no title" is allowed
    hay = re.sub(r"\s+", " ", raw_text).lower()
    return t in hay


def _restore_missing_titles(result: dict, raw_text: str) -> dict:
    """Bounded one-shot repair: when the parse drained entry titles, re-ask the
    model to recover them from the original text. Each patch title must appear
    verbatim in the resume text or it is discarded (never invented)."""
    missing = _missing_title_entries(result)
    if not missing:
        return result

    patch_map = []
    payload = []
    n = 0
    for key in ("experience", "projects", "certifications", "leadership"):
        for i in missing.get(key, []):
            e = result[key][i]
            bullets = "; ".join([str(b) for b in (e.get("bullets") or [])][:2])
            payload.append(
                f"[{n}] dates={e.get('dates', '') or '-'} bullets={bullets or '-'}"
            )
            patch_map.append((key, i))
            n += 1

    user_msg = "--- entries with missing titles ---\n" \
        + "\n".join(payload) \
        + "\n\n--- resume text ---\n" + raw_text

    try:
        response = call_resume_parser_llm(system=_REPAIR_PROMPT, user=user_msg, json_mode=True)
        repairs = extract_json(response)
    except Exception:
        return result

    titles = {}
    if isinstance(repairs, dict):
        raw = repairs.get("titles", repairs)
        if isinstance(raw, dict):
            titles = {str(k): v for k, v in raw.items()}
        elif isinstance(raw, list):
            titles = {str(x.get("id")): str(x.get("title", "")) for x in raw
                      if isinstance(x, dict) and "id" in x}
    elif isinstance(repairs, list):
        titles = {str(x.get("id")): str(x.get("title", "")) for x in repairs
                  if isinstance(x, dict) and "id" in x}

    for (key, idx), tid in zip(patch_map, range(n)):
        title = titles.get(str(tid), "")
        if title and _title_is_in_raw(title, raw_text):
            result[key][idx]["title"] = re.sub(r"\s+", " ", title).strip()
    return result
