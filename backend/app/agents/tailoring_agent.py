"""Two-step tailoring for a single job:
1. ATS gap analysis: identify what the job needs vs what the resume has
2. Resume rewrite: tailor the resume based on the gap analysis, then apply
   deterministic relevance reordering (most-relevant-first) that works
   regardless of the LLM's capabilities."""
import json
from app.agents.llm_client import call_llm, extract_json

ANALYSIS_PROMPT = """You are an ATS (Applicant Tracking System) analyst. Analyze the gap between a job description and a candidate's resume.

## Your task:
1. Extract the 15 most important skills/tools/technologies/keywords from the job description
2. Rank them by importance (repetition, placement, required vs preferred)
3. For each keyword, determine:
   - Already present and well-demonstrated in resume
   - Present but weakly demonstrated
   - Missing but can be added from real experience
   - Missing and unsupported
4. Flag any major mismatch between the job and the candidate's background

## Output format:
Return JSON:
{
  "job_keywords": [
    {"keyword": string, "importance": "high"|"medium"|"low", "status": "present"|"weak"|"addable"|"missing"}
  ],
  "missing_requirements": [string],
  "major_mismatches": [string],
  "tailoring_priorities": [string]
}

Return only valid JSON, no commentary."""

TAILOR_PROMPT = """You are an expert resume tailoring assistant. Rewrite a candidate's resume to closely match a specific job posting while keeping ALL content truthful.

## What you MUST do (non-negotiable):
1. **Rewrite the summary from scratch** to directly address the ROLE TITLE from the job and name the 3-5 most relevant technologies/skills from the job description. The summary MUST differ from the original — it must not echo the original text. Target it specifically at THIS role.
2. **Reorder skills** so the most relevant ones for THIS job appear FIRST (lead with the job's required skills).
3. **Rewrite experience bullets** to:
   - Start with strong action verbs (Designed, Built, Deployed, Optimized, Automated, Led, Architected, Scalable, Fine-tuned)
   - Emphasize quantifiable achievements where the ORIGINAL supports them (never invent numbers)
   - Use keywords from the job posting naturally
   - Show impact, not just responsibilities
4. **Reorder experience entries** by RELEVANCE to this job — the most relevant role moves to the TOP (its heading should read "Experience, <Most Recent Most Relevant>"). Reordering is REQUIRED, not optional: pick the most relevant role and put it first.
5. **Keep ALL projects** — reorder them by relevance to this job (most relevant first). Rewrite each project description to highlight the aspects that map to this job's requirements.
6. **Keep ALL certifications, education, leadership** — they add credibility.

## Critical rules:
- Do NOT invent experience, employers, skills, certifications, or metrics not in the original.
- Do NOT fabricate numbers. If the original says "improved performance", keep it — don't add "by 40%".
- You MAY rephrase bullets to use stronger action verbs and industry keywords.
- You MUST reorder items (experience, projects, skills) by relevance to this job — do not keep them in the original order.
- Preserve ALL original sections: summary, skills, experience, projects, education, certifications, leadership.

## Output format:
Return the SAME JSON schema as the input resume (include ALL fields from original):
{
  "summary_heading": string,
  "summary": string,
  "skills": [string],
  "experience": [{"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}],
  "projects": [{"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}],
  "education": [{"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}],
  "certifications": [{"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}],
  "leadership": [{"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}]
}

Return only valid JSON, no commentary."""


SECTION_ALIASES = {"management_and_leadership_skills": "leadership"}

_REPLACEMENT_CHARS = {0xFFFD: " ", 0xFFFE: " ", 0xFFFF: " "}


def _tokens(text) -> set[str]:
    """Lowercased word tokens for keyword-overlap scoring."""
    import re
    return set(re.findall(r"[a-z0-9][a-z0-9+\-.]*", (text or "").lower()))


def _relevance_score(text: str, job_terms: set[str], title: str = "", company: str = "") -> int:
    """Count how many job terms appear in the given text (token-level, fuzzy).
    Title and company get a large bonus since they are the strongest signal
    of role relevance."""
    if not text and not title:
        return 0
    tokens = _tokens(text)
    score = 0
    for term in job_terms:
        t = term.lower().strip()
        if not t:
            continue
        # prefer a clean whole-token match; else check substring overlap
        if t in tokens:
            score += 3
        elif any(t in tok or tok in t for tok in tokens):
            score += 1

    # Bonus: title and company tokens are strong relevance signals.
    # A title like "AI Automation Engineer" for an "AI Automation Specialist"
    # role should score much higher than a generic "Machine Learning" title.
    title_tokens = _tokens(title)
    company_tokens = _tokens(company)
    for term in job_terms:
        t = term.lower().strip()
        if not t:
            continue
        if t in title_tokens:
            score += 10
        elif any(t in tok or tok in t for tok in title_tokens):
            score += 4
        if t in company_tokens:
            score += 2
    return score


def _job_terms(job_requirements: dict) -> set[str]:
    """Everything worth matching against: role, required skills, keywords."""
    terms = set()
    role = job_requirements.get("role") or ""
    if role:
        terms.update(_tokens(role))
    for lst_key in ("required_skills", "nice_to_have", "keywords"):
        for item in job_requirements.get(lst_key) or []:
            terms.update(_tokens(item))
    return terms


def _reorder_experience(entries: list[dict], job_terms: set[str]) -> list[dict]:
    """Deterministically sort experience by relevance to the job (most fit first).
    This is reliable regardless of whether the LLM actually reordered anything."""
    def score(entry):
        text = " ".join([
            entry.get("title", ""), entry.get("company", ""),
            " ".join(entry.get("bullets", [])),
        ])
        return _relevance_score(text, job_terms, title=entry.get("title", ""), company=entry.get("company", ""))
    return sorted(entries, key=score, reverse=True)


def _reorder_projects(entries: list[dict], job_terms: set[str]) -> list[dict]:
    def score(entry):
        text = " ".join([
            entry.get("title", ""), entry.get("company", ""),
            " ".join(entry.get("bullets", [])),
        ])
        return _relevance_score(text, job_terms, title=entry.get("title", ""), company=entry.get("company", ""))
    return sorted(entries, key=score, reverse=True)


def _reorder_skills_by_relevance(skills: list[str], job_terms: set[str]) -> list[str]:
    """Sort the candidate's REAL skills by overlap with the job, most relevant first.
    Never returns JD-requirement prose; only entries already in the candidate's skill set.
    Also deduplicates and filters out verbose phrases (>4 words)."""
    if not skills:
        return skills
    # Deduplicate and filter in one pass
    seen = set()
    cleaned = []
    for s in skills:
        if not s or not s.strip():
            continue
        norm = " ".join(s.lower().split())
        if norm in seen:
            continue
        if len(s.split()) > 4:
            continue
        seen.add(norm)
        cleaned.append(s.strip())
    return sorted(
        cleaned,
        key=lambda s: _relevance_score(s, job_terms),
        reverse=True,
    )


def _clean_text(value) -> str:
    if not isinstance(value, str):
        return ""
    s = value.translate(_REPLACEMENT_CHARS)
    return " ".join(s.split()).strip()


def _normalize_resume(resume_json: dict) -> dict:
    """Map parser alias keys to canonical section names used downstream and
    scrub corrupt characters / duplicated employer artifacts in entries."""
    if not isinstance(resume_json, dict):
        return {}
    out = dict(resume_json)
    for alias, canonical in SECTION_ALIASES.items():
        if alias in out and not out.get(canonical):
            out[canonical] = out[alias]

    for key in ("experience", "projects", "education", "certifications", "leadership"):
        cleaned_entries = []
        for entry in out.get(key, []) or []:
            if not isinstance(entry, dict):
                continue
            entry = dict(entry)
            title = _clean_text(entry.get("title"))
            company = _clean_text(entry.get("company"))
            if company and title:
                t = title
                # strip a trailing " - Company" / "Company  company" artifact
                for sep in (" -", " –", " —", " |", " ·"):
                    marker = f"{sep} {company}"
                    if t.lower().endswith(marker.lower()):
                        t = t[: -len(marker)].strip()
                        break
                if t.lower().endswith(" " + company.lower()) and t.lower() != company.lower():
                    t = t[: -len(" " + company)].strip()
                title = t
            entry["title"] = title
            entry["company"] = company
            entry["dates"] = _clean_text(entry.get("dates")).strip("[]()").strip()
            entry["bullets"] = [_clean_text(b) for b in entry.get("bullets", []) if _clean_text(b)]
            cleaned_entries.append(entry)
        out[key] = cleaned_entries
    return out


def _entry_key(entry: dict) -> tuple:
    """Dedupe key: same role at same employer = same entry, even when the
    LLM's echo differs in dates or bullet wording."""
    def norm(v):
        return " ".join((v or "").lower().split())
    return (
        norm(entry.get("title")),
        norm(entry.get("company")),
    )


def _norm(v) -> str:
    """Normalize a string for comparison: lowercase, collapse whitespace."""
    return " ".join((v or "").lower().split())


def _dedupe_entries(entries: list[dict]) -> list[dict]:
    """Collapse duplicate entries (same title/company/dates) that small LLMs
    produce when they echo the resume back — keeps the entry with the most
    bullets (usually the rewritten/tailored one)."""
    merged: list[dict] = []
    idx = {}
    for e in entries:
        key = _entry_key(e)
        if key in idx:
            current = merged[idx[key]]
            if len(e.get("bullets") or []) > len(current.get("bullets") or []):
                merged[idx[key]] = e
        else:
            idx[key] = len(merged)
            merged.append(e)
    return merged


def _merge_missing_sections(tailored: dict, original: dict) -> dict:
    """Ensure NO original section is lost, even if the LLM drops it.
    For each section, prefer the LLM's version if it has content,
    otherwise fall back to the original resume's version."""
    result = dict(tailored)

    # Sections that must never be lost
    list_sections = ["experience", "projects", "education", "certifications", "leadership"]
    for key in list_sections:
        llm_val = result.get(key)
        orig_val = original.get(key, [])
        if not orig_val:
            for alias, canonical in SECTION_ALIASES.items():
                if key == canonical and original.get(alias):
                    orig_val = original[alias]
        if not llm_val:
            result[key] = _dedupe_entries(orig_val)
        else:
            # If LLM kept some entries but dropped others, merge back missing ones.
            # Two-pass matching: exact key first, then title-only fuzzy match
            # to handle LLM rephrasing company names.
            if orig_val:
                llm_keys = {_entry_key(o) for o in llm_val}
                # Pass 1: entries with exact title+company match already in LLM output
                # Pass 2: entries missing — check by title alone for fuzzy match
                llm_titles = {_norm(o.get("title")) for o in llm_val}
                missing = []
                for o in orig_val:
                    o_key = _entry_key(o)
                    if o_key in llm_keys:
                        continue  # exact match exists
                    o_title = _norm(o.get("title"))
                    if o_title and o_title in llm_titles:
                        continue  # LLM has an entry with same title (company may be rephrased)
                    missing.append(o)
                result[key] = _dedupe_entries(llm_val + missing)
            else:
                result[key] = _dedupe_entries(llm_val)

    # Scalar/string sections
    if not result.get("summary"):
        result["summary"] = original.get("summary", "")
    if not result.get("summary_heading"):
        result["summary_heading"] = original.get("summary_heading", "Summary")

    # Skills: deduplicate and merge individual missing skills.
    # The LLM may reorder or rephrase skills, but should never drop real ones.
    orig_skills = original.get("skills") or []
    llm_skills = result.get("skills") or []
    if orig_skills and llm_skills:
        # Deduplicate: normalize (lowercase, collapse whitespace) and compare.
        # Also filter out entries that are too long (>4 words = a sentence, not a skill).
        cleaned = []
        seen_norms = set()
        for s in llm_skills:
            if not s or not s.strip():
                continue
            norm = " ".join(s.lower().split())
            if norm in seen_norms:
                continue
            # Skip verbose phrases that are clearly not skill names
            if len(s.split()) > 4:
                continue
            seen_norms.add(norm)
            cleaned.append(s.strip())
        # Add any original skills not yet present
        for s in orig_skills:
            if not s or not s.strip():
                continue
            norm = " ".join(s.lower().split())
            if norm not in seen_norms and len(s.split()) <= 4:
                seen_norms.add(norm)
                cleaned.append(s.strip())
        result["skills"] = cleaned
    elif not llm_skills and orig_skills:
        result["skills"] = orig_skills

    return result


def _find_resume_dict(d: dict, depth: int = 0) -> dict:
    """Recursively search a possibly-nested LLM response for a dict that
    looks like a resume (has 'experience' or 'summary' key). Handles any
    wrapper the LLM might produce: {full_resume: {...}}, {result: {...}},
    {data: {full_resume: {...}}}, etc. Max 3 levels deep."""
    if depth > 3:
        return {}
    if not isinstance(d, dict):
        return {}

    # Direct match: has resume-like keys
    resume_keys = {"experience", "summary", "skills", "projects", "education"}
    if resume_keys & set(d.keys()):
        return d

    # Search nested dicts
    for v in d.values():
        if isinstance(v, dict):
            found = _find_resume_dict(v, depth + 1)
            if found:
                return found
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            # LLM might wrap in a list
            found = _find_resume_dict(v[0], depth + 1)
            if found:
                return found
    return {}


def tailor_resume(resume_id: str, resume_json: dict, job_requirements: dict, feedback: str | None = None) -> dict:
    resume_json = _normalize_resume(resume_json)

    try:
        # 1. Step 1: ATS Gap Analysis
        analysis_payload = json.dumps({
            "job_requirements": job_requirements,
            "resume": resume_json,
        })
        analysis_response = call_llm(system=ANALYSIS_PROMPT, user=analysis_payload, json_mode=True)
        gap_analysis = extract_json(analysis_response)
    except Exception:
        gap_analysis = {}

    try:
        # 2. Step 2: Tailor resume based on gap analysis
        role = job_requirements.get("role") or ""
        job_terms = _job_terms(job_requirements)
        instructions = (
            "Tailor this resume for the role '%s'. "
            "The SKILLS section must contain ONLY the candidate's real skills from the "
            "original resume (you may reorder them; do NOT copy job-requirement sentences "
            "into skills). "
            "The SUMMARY must name the role title and the 3-5 candidate real skills that "
            "best match this job. "
            "MUST: (1) rewrite the summary to target this role; (2) reorder experience so "
            "the MOST RELEVANT role to this job is FIRST; (3) reorder projects so the most "
            "relevant is FIRST; (4) keep ALL sections and all content truthful."
            % (role,)
        )
        tailoring_payload = json.dumps({
            "full_resume": resume_json,
            "job_requirements": job_requirements,
            "gap_analysis": gap_analysis,
            "previous_feedback": feedback or "",
            "instructions": instructions + ("\nAddress the previous review feedback exactly: " + feedback if feedback else ""),
        })
        response = call_llm(system=TAILOR_PROMPT, user=tailoring_payload, json_mode=True)
        parsed = extract_json(response)
    except Exception:
        parsed = {}

    # Small models often echo the ENTIRE input payload back instead of
    # returning just the tailored resume. If that happened, recover the
    # intended resume fields from any nested wrapper. Recursively search
    # up to 3 levels deep for a dict that looks like a resume (has
    # "experience" or "summary" key).
    base = _find_resume_dict(parsed)

    tailored = {}
    for key in ("summary_heading", "summary", "skills",
                "experience", "projects", "education",
                "certifications", "leadership"):
        # Prefer the top-level field if it has content, else the echo
        top = parsed.get(key)
        echoed = base.get(key)
        if not echoed:
            for alias, canonical in SECTION_ALIASES.items():
                if key == canonical and base.get(alias):
                    echoed = base[alias]
        if key in ("summary", "summary_heading", "skills"):
            taken = top if top else echoed
        else:
            taken = top if top else (echoed or [])
        if taken:
            tailored[key] = taken

    # 5. Merge back anything the LLM dropped or produced empty
    tailored = _merge_missing_sections(tailored, resume_json)

    # 6. Deterministic structural corrections — reliable regardless of how the
    #    (possibly small/local) LLM behaved:
    #    - reorder experience + projects by job relevance (most fit first)
    #    - reorder skills by job relevance, keeping ONLY real candidate skills
    job_terms = _job_terms(job_requirements)
    if isinstance(tailored.get("experience"), list):
        tailored["experience"] = _reorder_experience(tailored["experience"], job_terms)
    if isinstance(tailored.get("projects"), list):
        tailored["projects"] = _reorder_projects(tailored["projects"], job_terms)
    if isinstance(tailored.get("skills"), list):
        tailored["skills"] = _reorder_skills_by_relevance(tailored["skills"], job_terms)

    # 7. Post-processing: filter synthetic entries and deduplicate
    if isinstance(tailored.get("experience"), list):
        tailored["experience"] = _filter_experience(tailored["experience"], resume_json)
    if isinstance(tailored.get("projects"), list):
        tailored["projects"] = _dedupe_projects(tailored["projects"])

    return tailored


_FREELANCE_KEYWORDS = frozenset({
    "freelance", "self-employed", "self employed", "founder",
    "independent", "consultant", "contractor", "sole proprietor",
    "personal", "own", "proprietor",
})


def _filter_experience(entries: list[dict], original: dict) -> list[dict]:
    """Remove experience entries that the LLM fabricated (no company name)
    and that don't exist in the original resume. Legitimate entries with a
    company, or freelance/self-employed/founder roles, are always kept."""
    orig_titles = set()
    for e in (original.get("experience") or []):
        t = _clean_text(e.get("title", "")).lower()
        if t:
            orig_titles.add(t)

    filtered = []
    for e in entries:
        company = _clean_text(e.get("company", ""))
        title = _clean_text(e.get("title", "")).lower()
        if company:
            filtered.append(e)
        elif title in orig_titles:
            # entry exists in original resume — keep it even without company
            filtered.append(e)
        elif any(kw in title for kw in _FREELANCE_KEYWORDS):
            # freelance/founder/self-employed entry — legitimately has no company
            filtered.append(e)
        # else: synthetic LLM entry with no company and not in original — drop
    return filtered


def _dedupe_projects(entries: list[dict]) -> list[dict]:
    """Remove duplicate project entries based on title keyword overlap.
    Uses strict matching to avoid false positives:
    - Short titles (<=3 words) must be nearly identical (>=80% overlap)
    - Longer titles use >=60% overlap threshold
    - Minimum 2 overlapping words required in all cases"""
    if len(entries) <= 1:
        return entries

    seen: list[tuple[str, set[str]]] = []
    deduped: list[dict] = []
    for e in entries:
        title = _clean_text(e.get("title", "")).lower()
        title_words = set(title.split())
        n_words = len(title_words)
        if n_words == 0:
            deduped.append(e)
            continue

        # Adaptive threshold: stricter for short titles
        if n_words <= 3:
            min_overlap = max(2, int(n_words * 0.8))
        else:
            min_overlap = max(2, int(n_words * 0.6))

        is_dup = False
        for _, seen_words in seen:
            overlap = title_words & seen_words
            if len(overlap) >= min_overlap:
                is_dup = True
                break
        if not is_dup:
            seen.append((title, title_words))
            deduped.append(e)
    return deduped
