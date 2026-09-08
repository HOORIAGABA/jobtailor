"""Tailoring agent — rewrites a baseline resume to match a job posting.

Simplified flow: single LLM call with baseline resume + job requirements.
Deterministic post-processing handles reordering, deduplication, and
fabrication filtering regardless of LLM behavior.
"""
import json
import logging
import re
from copy import deepcopy as _deepcopy
from app.agents.llm_client import call_llm, extract_json

logger = logging.getLogger(__name__)


TAILOR_PROMPT = """You are an expert resume tailoring assistant. Given a baseline
resume and job requirements, produce a tailored version of the resume.

## CRITICAL: Section separation rules
- "experience" = PAID WORK ONLY. Jobs with an employer/company, a job title,
  and a date range. Freelance, internships, and contracted roles count.
- "projects" = PERSONAL/ACADEMIC PROJECTS ONLY. Self-built tools, open-source
  contributions, hackathon projects, course projects. These do NOT have an
  employer — the "company" field is typically empty or is a school/platform.
- NEVER move an entry from one section to another. If the original has a
  project in "projects", it MUST stay in "projects" in your output. If the
  original has a job in "experience", it MUST stay in "experience".
- If you are unsure whether something is a job or a project, KEEP it in the
  SAME section as the original.

## What you MUST do (non-negotiable):
1. **Rewrite the summary** from scratch to directly address the ROLE TITLE
   and name 3-5 relevant technologies/skills from the job. Must be DIFFERENT
   from the original summary.
2. **Reorder skills** so the most relevant ones for THIS job appear FIRST.
   Keep ALL real skills — do not drop any.
3. **Rewrite experience bullets** to:
   - Start with strong action verbs (Designed, Built, Deployed, Optimized)
   - Use keywords from the job posting naturally
   - Show impact, not just responsibilities
   - NEVER invent metrics not in the original
4. **Reorder experience entries** by RELEVANCE — most relevant role FIRST.
5. **Reorder projects** by RELEVANCE — most relevant first.
6. **Keep ALL sections** including education, certifications, leadership,
   and any extra_sections (publications, languages, volunteering, etc.)
7. **Preserve ALL original content** — do not fabricate experience, skills,
   employers, or metrics.

## Rules:
- Do NOT invent experience, employers, skills, certifications, or metrics.
- Do NOT fabricate numbers. Keep original metrics as-is.
- You MAY rephrase bullets to use stronger action verbs and keywords.
- You MUST reorder items by relevance — do not keep original order.
- Preserve ALL sections: summary, skills, experience, projects, education,
  certifications, leadership, and any extra_sections.
- Do NOT merge projects into experience or vice versa. They are separate.

## Input format:
{
  "full_resume": { ... baseline resume with all sections ... },
  "job_requirements": { company, role, required_skills, nice_to_have, seniority, keywords },
  "instructions": "Additional tailoring instructions"
}

## Output format:
Return the SAME JSON schema as the input resume (include ALL fields):
{
  "summary_heading": string,
  "summary": string,
  "skills": [string],
  "experience": [{"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}],
  "projects": [{"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}],
  "education": [{"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}],
  "certifications": [{"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}],
  "leadership": [{"heading": string, "title": string, "company": string, "dates": string, "bullets": [string]}],
  "extra_sections": [{"heading": string, "entries": [{"title": string, "company": string, "dates": string, "bullets": [string]}]}]
}

Return only valid JSON, no commentary."""


SECTION_ALIASES = {"management_and_leadership_skills": "leadership"}

_REPLACEMENT_CHARS = {0xFFFD: " ", 0xFFFE: " ", 0xFFFF: " "}


def _tokens(text) -> set[str]:
    import re
    return set(re.findall(r"[a-z0-9][a-z0-9+\-.]*", (text or "").lower()))


def _relevance_score(text: str, job_terms: set[str], title: str = "", company: str = "") -> int:
    if not text and not title:
        return 0
    tokens = _tokens(text)
    score = 0
    for term in job_terms:
        t = term.lower().strip()
        if not t:
            continue
        if t in tokens:
            score += 3
        elif any(t in tok or tok in t for tok in tokens):
            score += 1
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
    terms = set()
    role = job_requirements.get("role") or ""
    if role:
        terms.update(_tokens(role))
    for lst_key in ("required_skills", "nice_to_have", "keywords"):
        for item in job_requirements.get(lst_key) or []:
            terms.update(_tokens(item))
    return terms


def _reorder_experience(entries: list[dict], job_terms: set[str]) -> list[dict]:
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
    if not skills:
        return skills
    seen = set()
    cleaned = []
    for s in skills:
        if not s or not s.strip():
            continue
        norm = " ".join(s.lower().split())
        if norm in seen:
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

    extra = out.get("extra_sections", [])
    if isinstance(extra, list):
        cleaned_extra = []
        for section in extra:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading", ""))
            entries = section.get("entries", [])
            if not isinstance(entries, list):
                continue
            cleaned_entries = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                cleaned_entries.append({
                    "title": _clean_text(entry.get("title")),
                    "company": _clean_text(entry.get("company")),
                    "dates": _clean_text(entry.get("dates")).strip("[]()").strip(),
                    "bullets": [_clean_text(b) for b in entry.get("bullets", []) if _clean_text(b)],
                })
            if heading and cleaned_entries:
                cleaned_extra.append({"heading": heading, "entries": cleaned_entries})
        out["extra_sections"] = cleaned_extra

    return out


def _entry_key(entry: dict) -> tuple:
    return (_norm(entry.get("title")), _norm(entry.get("company")))


def _norm(v) -> str:
    s = re.sub(r"[\s\-–—−]+", " ", (v or "").lower())
    return " ".join(s.split())


def _dedupe_entries(entries: list[dict]) -> list[dict]:
    """Deduplicate entries by normalized (title, company) key.

    When two entries share the same key but one has an empty company field,
    they are likely from different section types (e.g. a project vs a job
    with a similar title). Only collapse them if BOTH have a non-empty
    company — otherwise keep both to avoid cross-section contamination.
    """
    merged: list[dict] = []
    idx = {}
    for e in entries:
        key = _entry_key(e)
        if key in idx:
            current = merged[idx[key]]
            both_have_company = _norm(e.get("company")) and _norm(current.get("company"))
            if not both_have_company:
                merged.append(e)
                continue
            if len(e.get("bullets") or []) > len(current.get("bullets") or []):
                merged[idx[key]] = e
        else:
            idx[key] = len(merged)
            merged.append(e)
    return merged


_SKILL_PROSE_NEGATIVES = {
    "excellent", "strong", "sharp", "eye", "detail", "quality", "hands",
    "experience", "skills", "skill", "knowledge", "proven", "track",
    "record", "you", "your", "how", "right", "way", "get", "answer",
    "ask", "known", "familiar", "ability", "expert", "good", "best",
}


def _skill_has_prose_wording(phrase: str) -> bool:
    words = set(re.findall(r"[a-z]+", (phrase or "").lower()))
    return len(words & _SKILL_PROSE_NEGATIVES) >= 2


def _matches_original_skill(phrase: str, original_skills: list[str]) -> bool:
    """A candidate skill must closely match a real resume skill.

    Checks: substring containment (either direction), or shared meaningful
    tokens.  A single common token is NOT enough — it must be a meaningful
    token (length >= 3) to avoid false positives on words like "and", "in".
    """
    p = (phrase or "").lower()
    ptoks = _tokens(p)
    if not ptoks:
        return False
    meaningful_ptoks = {t for t in ptoks if len(t) >= 3}
    for orig in original_skills:
        o = (orig or "").lower()
        if not o:
            continue
        if p in o or o in p:
            return True
        otoks = _tokens(o)
        meaningful_otoks = {t for t in otoks if len(t) >= 3}
        shared = meaningful_ptoks & meaningful_otoks
        if shared:
            return True
    return False


_PROJECT_MARKERS = {
    "built", "building", "developed", "develop", "designed", "design",
    "created", "create", "implemented", "implementing", "made", "github",
    "repository", "repo", "api", "app", "application", "system", "pipeline",
    "etl", "model", "classifier", "dataset", "automation", "automated",
    "framework", "dashboard", "bot", "algorithm", "end-to-end", "e2e",
    "container", "docker", "mlflow", "trained", "scraped",
}

_PUBLIC_URL_MARKERS = ("http://", "https://", "github.com", "bitbucket")


def _is_project_like(entry: dict) -> bool:
    """Project-shaped entry: never had a company or employment dates, and its
    bullets/title read like a build log rather than a job description."""
    company_n = _norm(entry.get("company"))
    if company_n:
        return False
    text = " ".join(
        [
            entry.get("title") or "",
            " ".join(entry.get("bullets") or entry.get("bullet_points") or []),
        ]
    ).lower()
    if any(u in text for u in _PUBLIC_URL_MARKERS):
        return True
    hits = sum(1 for m in _PROJECT_MARKERS if m in text)
    return hits >= 2


def _reclassify_entries(tailored: dict, original: dict) -> dict:
    """Detect entries placed in the wrong section and move them back,
    in-place on `tailored`. Returns `tailored` for chaining.

    Heuristics:
    - experience → projects when: (a) no company AND title matches an original
      project, OR (b) no company AND the entry is project-shaped
      (`_is_project_like`: build vocabulary / public URL in bullets).
    - projects → experience when: has a company AND title matches an original
      job.

    Case (b) is the safety net for a fully misclassified *original* resume:
    it lets `_merge_missing_sections` reclassify the source before matching,
    instead of merging a jumbled original back under the wrong heading.
    """
    llm_exp = tailored.get("experience") or []
    llm_proj = tailored.get("projects") or []
    orig_exp = original.get("experience") or []
    orig_proj = original.get("projects") or []

    if llm_exp:
        orig_proj_titles = {_norm(p.get("title")): p for p in orig_proj if _norm(p.get("title"))}
        to_move_to_projects = []
        kept_exp = []
        for entry in llm_exp:
            title_n = _norm(entry.get("title"))
            company_n = _norm(entry.get("company"))
            if not company_n and title_n and title_n in orig_proj_titles:
                reason = "matches original project"
            elif not company_n and _is_project_like(entry):
                reason = "project-shaped entry (no company, build vocabulary/URL)"
            else:
                kept_exp.append(entry)
                continue
            logger.debug("Reclassifying '%s' from experience → projects (%s)", entry.get("title"), reason)
            to_move_to_projects.append(entry)
        if to_move_to_projects:
            tailored["experience"] = kept_exp
            tailored["projects"] = to_move_to_projects + llm_proj

    llm_exp = tailored.get("experience") or []
    llm_proj = tailored.get("projects") or []

    if not llm_proj or not orig_exp:
        return tailored
    orig_exp_titles = {_norm(e.get("title")): e for e in orig_exp if _norm(e.get("title"))}
    to_move_to_experience = []
    kept_proj = []
    for entry in llm_proj:
        title_n = _norm(entry.get("title"))
        company_n = _norm(entry.get("company"))
        if company_n and title_n and title_n in orig_exp_titles:
            to_move_to_experience.append(entry)
            logger.debug("Reclassifying '%s' from projects → experience (matches original job)", entry.get("title"))
        else:
            kept_proj.append(entry)
    if to_move_to_experience:
        tailored["projects"] = kept_proj
        tailored["experience"] = llm_exp + to_move_to_experience
    return tailored


def _merge_missing_sections(tailored: dict, original: dict) -> dict:
    result = dict(tailored)

    # ---- Step 0: Reclassify misplaced entries ----
    # If the LLM put a project into experience (or vice versa), move it back
    # before the merge, so the merge doesn't add duplicates. Also reclassify a
    # COPY of the original so a parser-misclassified source doesn't get merged
    # back in under the wrong heading either.
    _reclassify_entries(result, original)
    original = _reclassify_entries(_deepcopy(original), original)

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
            if orig_val:
                llm_keys = {_entry_key(o) for o in llm_val}
                llm_titles = {_norm(o.get("title")) for o in llm_val}
                missing = []
                for o in orig_val:
                    o_key = _entry_key(o)
                    if o_key in llm_keys:
                        continue
                    o_title = _norm(o.get("title"))
                    if o_title and o_title in llm_titles:
                        continue
                    missing.append(o)
                result[key] = _dedupe_entries(llm_val + missing)
            else:
                result[key] = _dedupe_entries(llm_val)

    if not result.get("summary"):
        result["summary"] = original.get("summary", "")
    if not result.get("summary_heading"):
        result["summary_heading"] = original.get("summary_heading", "Summary")

    orig_skills = original.get("skills") or []
    llm_skills = result.get("skills") or []
    if orig_skills:
        cleaned = []
        seen_norms = set()
        for s in llm_skills:
            if not s or not s.strip():
                continue
            norm = " ".join(s.lower().split())
            if norm in seen_norms:
                continue
            if _skill_has_prose_wording(s) or not _matches_original_skill(s, orig_skills):
                continue
            seen_norms.add(norm)
            cleaned.append(s.strip())
        for s in orig_skills:
            if not s or not s.strip():
                continue
            norm = " ".join(s.lower().split())
            if norm not in seen_norms:
                seen_norms.add(norm)
                cleaned.append(s.strip())
        result["skills"] = cleaned
    elif llm_skills:
        result["skills"] = [_clean_text(s) for s in llm_skills if _clean_text(s)]

    # Merge extra sections: keep LLM's version, restore any dropped ones
    orig_extra = original.get("extra_sections", [])
    llm_extra = result.get("extra_sections", [])
    if orig_extra:
        llm_headings = {e.get("heading", "").lower() for e in llm_extra if isinstance(e, dict)}
        for orig_section in orig_extra:
            if isinstance(orig_section, dict):
                h = orig_section.get("heading", "").lower()
                if h and h not in llm_headings:
                    llm_extra.append(orig_section)
        result["extra_sections"] = llm_extra

    return result


def _find_resume_dict(d: dict, depth: int = 0) -> dict:
    if depth > 3:
        return {}
    if not isinstance(d, dict):
        return {}
    resume_keys = {"experience", "summary", "skills", "projects", "education"}
    if resume_keys & set(d.keys()):
        return d
    for v in d.values():
        if isinstance(v, dict):
            found = _find_resume_dict(v, depth + 1)
            if found:
                return found
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            found = _find_resume_dict(v[0], depth + 1)
            if found:
                return found
    return {}


def tailor_resume(resume_id: str, resume_json: dict, job_requirements: dict, feedback: str | None = None) -> dict:
    resume_json = _normalize_resume(resume_json)

    role = job_requirements.get("role") or ""
    company = job_requirements.get("company") or ""
    instructions = (
        "Tailor this resume for the role '%s' at '%s'. "
        "Rewrite the summary to target this role and name the top skills. "
        "Reorder experience and projects so the most relevant are FIRST. "
        "Reorder skills so job-required skills appear first. "
        "Rewrite experience bullets to use job keywords naturally. "
        "Keep ALL sections including extra_sections. "
        "Do NOT fabricate any content."
        % (role, company)
    )
    if feedback:
        instructions += "\n\nAddress this review feedback: " + feedback

    payload = json.dumps({
        "full_resume": resume_json,
        "job_requirements": job_requirements,
        "instructions": instructions,
    })

    try:
        response = call_llm(system=TAILOR_PROMPT, user=payload, json_mode=True)
        parsed = extract_json(response)
    except Exception:
        parsed = {}

    base = _find_resume_dict(parsed)

    tailored = {}
    for key in ("summary_heading", "summary", "skills",
                "experience", "projects", "education",
                "certifications", "leadership", "extra_sections"):
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

    logger.info(
        "LLM raw output — experience=%d, projects=%d, skills=%d",
        len(tailored.get("experience") or []),
        len(tailored.get("projects") or []),
        len(tailored.get("skills") or []),
    )

    tailored = _merge_missing_sections(tailored, resume_json)

    logger.info(
        "After merge — experience=%d, projects=%d, skills=%d",
        len(tailored.get("experience") or []),
        len(tailored.get("projects") or []),
        len(tailored.get("skills") or []),
    )

    job_terms = _job_terms(job_requirements)
    if isinstance(tailored.get("experience"), list):
        tailored["experience"] = _reorder_experience(tailored["experience"], job_terms)
    if isinstance(tailored.get("projects"), list):
        tailored["projects"] = _reorder_projects(tailored["projects"], job_terms)
    if isinstance(tailored.get("skills"), list):
        tailored["skills"] = _reorder_skills_by_relevance(tailored["skills"], job_terms)

    return tailored
