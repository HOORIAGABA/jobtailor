"""Stage S3 — decide what to change. Emit no prose.

The planner is the judgment stage: it reads the job, the resume and the
evidence linking them, and returns a list of typed edit operations. It does not
rewrite anything — a `RewriteBullet` it emits carries `text=""`, naming a target
for the writer to fill.

Separating "what to change" from "how to word it" is what stopped three agents
editing the same bullets under contradictory rules in the previous version.

The payload is deliberately compact. The planner sees the *brief*, not the raw
posting, and the resume as `id: text` rather than nested objects — it needs to
reason and to name ids, not to admire structure.
"""
from __future__ import annotations

import logging
from typing import Iterable

from app.agents.base import as_json, call_structured, prompt_version
from app.domain.models import EvidenceIndex, JobBrief, ResumeDoc
from app.domain.ops import ADVISORY_OPS, Op, OpList
from app.engine.evidence import elements_by_bullet, job_elements
from app.io.llm import LLMClient

logger = logging.getLogger(__name__)

MAX_OPS = 24


SYSTEM = """\
You are deciding how to adapt one candidate's resume for one specific job.

You return a list of EDIT OPERATIONS. You never return a resume, and you never
write bullet prose — a rewrite you request is filled in by someone else.

WHAT YOU GET
- brief: what the role actually is, the problems it owns, the terms it values
- resume: every line, each with a permanent id like "exp.1.b.2"
- evidence: which resume lines already evidence which parts of the job
- grounding: the skills this resume can support a claim about
- gaps: parts of the job with no supporting line at all

THE ONE RULE
You may only surface what is already there. You cannot add a fact, a number, a
tool or an employer. If the job wants something the resume does not show, that
is a gap to flag, not a hole to fill. A resume that quietly overstates is worse
for the candidate than one that is honestly thin.

OPERATIONS

rewrite_bullet {bullet_id, target_terms, rationale}
  Ask for a line to be reworded. Leave text empty.
  Request one when a line is vague, buries what this job cares about, or uses
  wording far from the role's language WHILE DESCRIBING THE SAME WORK.
  target_terms: only terms the line genuinely already demonstrates. Listing a
  term the line does not support produces keyword stuffing, which is rejected.
  Do not request a rewrite just to insert a keyword. Do not request one for a
  line that is already clear and relevant.

reorder_bullets {item_id, order}
  Reorder lines within one entry. `order` must contain exactly the same ids
  that entry already has — you cannot drop or duplicate. Put the strongest,
  most relevant line first.

reorder_items {section_id, order}
  Reorder entries within a section. Same permutation rule.
  Use for projects and skills. NOT for work experience — see below.

promote_item {item_id, rationale, cites}
  Move one work entry out of date order to the top. Work experience is read
  in reverse-chronological order; breaking that needs a reason, and you get at
  most ONE per section. Use it only when a later role is dramatically more
  relevant than the most recent one.

set_summary {text, cites}
  Write the summary — this is the one place you do write prose, because a
  summary is assembled from facts rather than reworded from a single line.
  2-3 sentences. Every claim must be backed by a line you cite. No adjectives
  that cite nothing ("passionate", "results-driven", "proven track record").

set_skills {groups}
  Rearrange the skills section so this job's skills are read first.

  KEEP EVERY SKILL. This op REPLACES the whole section, so any skill you leave
  out is deleted from the candidate's resume. Their other specialisms are not
  noise — a recruiter who wanted only this job's keywords would not be reading
  a resume. An op that drops a skill is rejected.

  What you SHOULD do:
    - Put the group holding this job's skills first, and that job's skills
      first within it.
    - Merge, split or rename groups so the result reads as one coherent list.
      A heading you invent is fine if it honestly describes what is under it.
    - Everything else follows, in a sensible order.

  Rules:
    - Every group needs a heading. Never an empty label.
    - A skill belongs to exactly one group.
    - Every skill must appear in `grounding`. You may not add one.
    - Write skills the way the resume writes them.

drop_bullet {bullet_id, reason}
  Remove a line that is genuinely irrelevant here. Use sparingly — space is
  rarely the binding constraint, and a dropped line is a change the candidate
  has to review. Never empty an entry.

flag_gap {requirement, severity, closest_evidence}
  The job wants this and the resume does not show it. severity "blocking" for a
  hard requirement, "major" for an important one, "minor" otherwise.
  closest_evidence: line ids that come nearest, or empty.

ask_user {bullet_id, question}
  Ask for a fact only the candidate knows — usually a missing outcome.
  Ask OPEN questions. Never name the tool or number you are hoping for:
  ask "What did you use to schedule these jobs?" and never "Did you use
  Airflow?" — naming it shows them the answer that flatters their resume.

HOW TO DECIDE
1. Read the brief's problems and success signals. That is what matters here.
2. For each, look at the evidence. Strong evidence means a line already shows
   it — consider moving that line up. A gap means nothing shows it — flag it.
3. Rewrite only lines that are genuinely vague or genuinely buried.
4. Leave good lines alone. Doing less is a valid plan, and a resume covered in
   changes is one the candidate cannot review.

Typical output is 4 to 10 operations. Every operation that changes meaning
needs a rationale written for the candidate to read, not for you.

Return only the JSON object."""

PROMPT_VERSION = prompt_version(SYSTEM)


# ── payload ───────────────────────────────────────────────────────────

def build_payload(
    brief: JobBrief,
    doc: ResumeDoc,
    index: EvidenceIndex,
    grounding: Iterable[str],
) -> dict:
    """Compact view for the planner: ids and text, no nesting for its own sake."""
    elements = {e.id: e.text for e in job_elements(brief)}

    return {
        "brief": {
            "role": brief.role,
            "company": brief.company,
            "seniority": brief.seniority,
            "narrative": brief.role_narrative,
            "tone": brief.tone,
            "problems": [
                {"id": f"problem.{i}", "text": p.statement, "priority": p.priority}
                for i, p in enumerate(brief.problems_to_solve)
            ],
            "success_signals": [s.statement for s in brief.success_signals],
            "hard_requirements": [r.statement for r in brief.hard_requirements],
            "terms": [
                {"term": t.term, "weight": t.weight, "required": t.required}
                for t in brief.terms
            ],
            "posting_excerpt": brief.excerpt,
        },
        "resume": {
            "summary": doc.summary,
            "sections": [
                {
                    "section_id": s.id,
                    "kind": s.kind,
                    "heading": s.heading,
                    "items": [
                        {
                            "item_id": i.id,
                            "title": i.title,
                            "org": i.org,
                            "dates": i.dates,
                            "bullets": {b.id: b.text for b in i.bullets},
                        }
                        for i in s.items
                    ],
                }
                for s in doc.sections
            ],
        },
        "evidence": {
            "by_bullet": {
                bid: [elements.get(eid, eid) for eid in eids]
                for bid, eids in elements_by_bullet(index).items()
            },
            "strong_bullets": sorted(
                {l.bullet_id for l in index.links if l.strength == "strong"}
            ),
        },
        "gaps": [elements.get(eid, eid) for eid in index.unmatched_elements],
        "grounding": sorted(grounding),
    }


# ── validation of the model's own output ──────────────────────────────

def _targets_exist(op: Op, doc: ResumeDoc) -> bool:
    """Drop operations pointing at ids that do not exist.

    The validator would reject these anyway, but dropping them here keeps the
    rejection list meaningful: it should show claims that were refused on
    substance, not the model mistyping an id.
    """
    kind = op.op
    if kind in ("rewrite_bullet", "drop_bullet"):
        return doc.bullet(op.bullet_id) is not None
    if kind == "reorder_bullets":
        return doc.item(op.item_id) is not None
    if kind == "reorder_items":
        return doc.section(op.section_id) is not None
    if kind == "promote_item":
        return doc.item(op.item_id) is not None
    if kind == "ask_user":
        return not op.bullet_id or doc.bullet(op.bullet_id) is not None
    return True


def clean_ops(
    ops: list[Op],
    doc: ResumeDoc,
    max_ops: int = MAX_OPS,
    dropped: list[str] | None = None,
) -> list[Op]:
    """Assign ids, drop unresolvable targets, cap the batch.

    `dropped` collects a readable reason per discarded op. A plan that silently
    shrinks to nothing is indistinguishable from a model that said nothing, and
    those need very different fixes — so the reasons are returned, not only
    logged.
    """
    kept: list[Op] = []
    for n, op in enumerate(ops, start=1):
        if not _targets_exist(op, doc):
            target = (getattr(op, "bullet_id", "") or getattr(op, "item_id", "")
                      or getattr(op, "section_id", ""))
            reason = f"{op.op}: no such id {target!r}"
            logger.info("Dropped %s", reason)
            if dropped is not None:
                dropped.append(reason)
            continue
        op.op_id = f"op{n}"
        kept.append(op)
        if len(kept) >= max_ops:
            logger.warning("Plan truncated at %d operations", max_ops)
            break
    return kept


def rewrite_targets(ops: list[Op]) -> list[Op]:
    """The RewriteBullet ops awaiting prose from the writer."""
    return [op for op in ops if op.op == "rewrite_bullet"]


# ── the stage ─────────────────────────────────────────────────────────

def plan(
    brief: JobBrief,
    doc: ResumeDoc,
    index: EvidenceIndex,
    grounding: Iterable[str],
    client: LLMClient,
    *,
    max_tokens: int = 3072,
    dropped: list[str] | None = None,
) -> list[Op]:
    """One model call. Returns operations, never prose."""
    result = call_structured(
        client,
        system=SYSTEM,
        user=as_json(build_payload(brief, doc, index, grounding)),
        schema_model=OpList,
        max_tokens=max_tokens,
        temperature=0.2,
        stage="planner",
    )
    returned = len(result.ops)
    ops = clean_ops(result.ops, doc, dropped=dropped)

    if returned and not ops:
        logger.warning(
            "The model returned %d operations and none survived id checking. "
            "Usually it invented ids rather than using the ones it was given.",
            returned,
        )
    elif not returned:
        logger.warning("The model returned an empty plan.")

    logger.info(
        "Plan: %d operations (%d rewrites, %d advisory)",
        len(ops),
        len(rewrite_targets(ops)),
        sum(1 for o in ops if o.op in ADVISORY_OPS),
    )
    return ops
