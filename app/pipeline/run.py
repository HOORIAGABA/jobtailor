"""The stages, composed into a run — and every one of them written to disk.

Until now the two halves of this system were reachable only through two
separate scripts, and only the first half saved anything. `parse_resume.py`
wrote four files and stopped at S0.3; `try_it.py` ran S1–S7 against a resume
hardcoded in Python and printed everything to a terminal, where it scrolled
away. So the stages that actually decide what happens to a resume — the plan,
what the validator refused, the diff a candidate would approve — left no trace
at all.

This module is where the stages are composed, and it writes each one out as it
is produced:

    runs/2026-09-24T14-32-10_HooriaAttas/
      00_extract.txt        S0.1  reading order
      01_parse.json         S0.2  the model's structure
      02_coverage.json      S0.2v what it dropped or invented
      03_normalize.json     S0.3  ids, classification, grounding corpus
      04_brief.json         S1    what the model understood about the job
      05_evidence.json      S2    which lines back which requirements
      06_plan.json          S3    the operations requested
      07_written.json       S4    the prose that filled them
      08_validation.json    S5    accepted, and every rejection with its reason
      09_tailored.json      S6    the resulting document
      10_diff.json          S7    the review payload
      manifest.json         input hash, model, tokens, timings, proof checks

**Written as produced, not at the end.** A run that crashes in S5 still leaves
S0 through S4 on disk, and those are the files that explain the crash. Writing
a bundle at the end would lose exactly the runs worth reading.

**The stages know nothing about this.** Each one stays a pure function of its
inputs; the log is passed in and defaults to a `NullRunLog` that writes
nothing. Composition and observation live here, in the pipeline layer, which is
also where LangGraph will sit when checkpointing and the S0.4/S9 interrupts
arrive. The stage functions will not change when it does.

**Why not a database yet.** Files are diffable, greppable, and need no service
running. A run folder from before a prompt change and one from after can be
compared with `diff`, which turns "it feels worse" into a named line.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from app.agents.evidence_hints import find_hints, merge as merge_hints
from app.agents.job_brief import build_job_brief
from app.agents.parse import parse_resume
from app.agents.planner import plan
from app.agents.outreach import draft_outreach
from app.agents.writer import write_bullets
from app.domain.errors import ModelOutputError, UserError
from app.domain.models import Outreach, EvidenceIndex, JobBrief, RawResume, ResumeDoc
from app.domain.ops import Op
from app.engine.apply import apply_ops
from app.engine.diff import Diff, build_diff
from app.engine.evidence import (
    compute_evidence,
    terms_by_standing,
    undemonstrated_terms,
)
from app.engine.normalize import normalize
from app.engine.parse_check import ParseCoverage, check_coverage
from app.engine.posting import PostingAssessment, prepare
from app.engine.validator import grounding_corpus, validate
from app.io.cache import Cache, NullCache, key_for
from app.io.extract import extract_text
from app.io.llm import LLMClient, merged_call_log
from app.io.render import Rendered, filename_for, render as render_resume
from app.io.runlog import NullRunLog, RunLog

logger = logging.getLogger(__name__)

# Shorter than this is not a posting. A real one is hundreds of characters.
MIN_POSTING_CHARS = 120


@dataclass
class RunState:
    """Everything one run produced. Partially filled when a stage fails."""
    # S0
    text: str = ""
    doc: ResumeDoc | None = None
    coverage: ParseCoverage | None = None
    # S1–S2
    brief: JobBrief | None = None
    index: EvidenceIndex | None = None
    # S3–S5
    ops: list[Op] = field(default_factory=list)
    dropped_ops: list[str] = field(default_factory=list)
    accepted: list[Op] = field(default_factory=list)
    rejected: list = field(default_factory=list)
    # S6–S7
    tailored: ResumeDoc | None = None
    diff: Diff | None = None
    standing: dict[str, list[str]] = field(default_factory=dict)
    posting: PostingAssessment | None = None
    hint_drops: list[str] = field(default_factory=list)
    # S8
    outreach: Outreach | None = None
    # S10
    rendered: Rendered | None = None

    @property
    def gaps(self) -> list[str]:
        if self.brief is None or self.index is None:
            return []
        return undemonstrated_terms(self.brief, self.index)


# ── S0 · ingest ───────────────────────────────────────────────────────

def ingest(
    data: bytes,
    filename: str,
    client: LLMClient,
    *,
    log: RunLog | None = None,
    state: RunState | None = None,
    cache: Cache | None = None,
) -> RunState:
    """File bytes to an addressable document. One model call.

    The coverage check runs here rather than being left to the caller: a lossy
    parse is invisible to every later stage, so the one place it can be caught
    is immediately after it happens.
    """
    log = log or NullRunLog()
    state = state or RunState()
    cache = cache or NullCache()

    log.input_file(filename, data)
    state.text = extract_text(data, filename)
    log.text("extract", state.text)

    # Keyed on the FILE, not the extracted text: the same upload must reuse its
    # parse even if the extractor is tuned between runs, and an edited resume
    # must not. On a three-call-a-day tier this is the difference between a run
    # that can finish and one that dies in the same place forever.
    cache_key = key_for(data)
    if (hit := cache.get("parse", cache_key, RawResume)) is not None:
        logger.info("Reusing the cached parse of %s", filename)
        raw = hit
        log.note(parse_from_cache=True)
        log.json("parse", raw)
        state.coverage = check_coverage(state.text, raw)
        log.json("coverage", state.coverage)
        state.doc = normalize(raw)
        log.json("normalize", state.doc)
        return state

    try:
        raw = parse_resume(state.text, client)
    except ModelOutputError as exc:
        # The response IS the evidence. Without it, "the JSON was incomplete"
        # cannot distinguish a model that rambled from one cut off mid-string
        # from one that answered in prose.
        if exc.raw:
            log.text("parse_failed_raw", exc.raw)
        raise
    cache.set("parse", cache_key, raw)
    log.json("parse", raw)

    state.coverage = check_coverage(state.text, raw)
    log.json("coverage", state.coverage)
    if not state.coverage.is_clean:
        logger.warning("Parse is not clean: %s", state.coverage.summary())

    state.doc = normalize(raw)
    log.json("normalize", state.doc)
    return state


# ── S1–S7 · tailor ────────────────────────────────────────────────────

def tailor(
    doc: ResumeDoc,
    jd_text: str,
    client: LLMClient,
    *,
    log: RunLog | None = None,
    state: RunState | None = None,
    confirmed_capabilities: Iterable[str] = (),
    cache: Cache | None = None,
    semantic_hints: bool = False,
    render_output: bool = True,
    write_outreach: bool = False,
) -> RunState:
    """An addressable document plus a posting, to a reviewable diff and files.

    Three model calls: the brief, the plan, the prose. Everything between and
    after them is deterministic — evidence, validation, application, the diff
    and the rendered .docx/.pdf.
    """
    log = log or NullRunLog()
    state = state or RunState()
    state.doc = doc

    # S1.0 — strip page furniture and say what is actually here. A posting
    # arrives as a LinkedIn page, a saved tab or a paste, and a "we're hiring,
    # DM me" post produces an empty brief, an empty plan, and a result
    # indistinguishable from a model that decided to change nothing.
    jd_text, state.posting = prepare(jd_text)
    log.json("posting", state.posting)
    if state.posting.removed:
        logger.info("Stripped %d lines of page furniture", len(state.posting.removed))

    if not state.posting.is_usable:
        raise UserError(
            "This does not look like a job posting: "
            + state.posting.summary()
            + "\n  Tailoring needs a role and what it asks for. A hiring "
              "announcement has neither."
        )

    # S1 — understand the job. Cached on the posting, so a second resume
    # against the same job costs nothing here.
    store = cache or NullCache()
    brief_key = key_for(jd_text)
    state.brief = store.get("brief", brief_key, JobBrief)
    if state.brief is None:
        state.brief = build_job_brief(jd_text, client)
        store.set("brief", brief_key, state.brief)
    else:
        log.note(brief_from_cache=True)
    log.json("brief", state.brief)
    # In the manifest as well as the artifact: a run list wants the role and
    # company without opening every brief.
    log.note(role=state.brief.role, company=state.brief.company)

    # S2 — link evidence (no model)
    state.index = compute_evidence(state.brief, doc)

    # S2b — optional: ask about what exact matching could not place. Hints
    # only, and skipped entirely when nothing is unmatched. See
    # `agents.evidence_hints` for why this may never produce a strong link.
    if semantic_hints:
        state.hint_drops = []
        hints = find_hints(state.brief, doc, state.index, client,
                           dropped=state.hint_drops)
        if hints:
            log.json("evidence_lexical", {
                "links": [l.model_dump() for l in state.index.links],
                "unmatched_elements": state.index.unmatched_elements,
            })
            state.index = merge_hints(state.index, hints)
    state.standing = terms_by_standing(state.brief, doc, state.index)
    log.json("evidence", {
        "links": [l.model_dump() for l in state.index.links],
        "unmatched_elements": state.index.unmatched_elements,
        # Three grades, not one flat list. `not_found` means this matcher did
        # not find the term, which is not the same as the candidate not having
        # it — see engine.evidence.term_standing.
        "term_standing": state.standing,
        "undemonstrated_terms": state.gaps,
    })

    # S3 — decide what to change
    corpus = grounding_corpus(doc, confirmed_capabilities)
    state.dropped_ops = []
    state.ops = plan(state.brief, doc, state.index, corpus, client,
                     dropped=state.dropped_ops)
    log.json("plan", {
        "operations": [o.model_dump() for o in state.ops],
        "dropped_before_validation": state.dropped_ops,
    })

    # S4 — write the prose for the targeted lines
    state.ops = write_bullets(state.ops, doc, state.brief, client)
    log.json("written", [o.model_dump() for o in state.ops])

    # S5 — the honesty guard (no model)
    result = validate(state.ops, doc, confirmed_capabilities=confirmed_capabilities)
    state.accepted, state.rejected = result.accepted, result.rejected
    log.json("validation", {
        "accepted": [o.model_dump() for o in result.accepted],
        "rejected": [r.model_dump() for r in result.rejected],
        "fabrication_count": result.fabrication_count,
    })

    # S6 — apply, S7 — diff (no model)
    state.tailored = apply_ops(doc, state.accepted)
    log.json("tailored", state.tailored)

    state.diff = build_diff(doc, state.tailored, state.accepted, state.rejected)
    log.json("diff", state.diff)

    # S8 — the message. One model call, and opt-in: the resume is useful on
    # its own, and on a free tier the last call is the one most likely to be
    # refused. The draft carries its own problem list rather than raising —
    # a flawed message a person can fix beats no message.
    if write_outreach:
        state.outreach = draft_outreach(
            state.brief, state.tailored, state.index, client,
            jd_text=jd_text, standing=state.standing,
            confirmed=tuple(confirmed_capabilities),
        )
        log.json("outreach", state.outreach)

    # S10 — the deliverable. No model, and it reads its own output back, so a
    # run that reaches here always leaves files a person can actually open.
    if render_output:
        role = state.brief.role if state.brief else ""
        state.rendered = render_resume(state.tailored, role=role)
        log.bytes("resume_docx", state.rendered.docx, ".docx")
        log.bytes("resume_pdf", state.rendered.pdf, ".pdf")
        log.text("resume_ats", state.rendered.text)
        log.note(
            resume_filename=filename_for(state.tailored, role),
            render_checks=[c.summary() for c in state.rendered.checks],
            render_clean=state.rendered.is_clean,
        )

    return state


# ── the whole thing ───────────────────────────────────────────────────

def run(
    resume_bytes: bytes,
    filename: str,
    jd_text: str,
    client: LLMClient,
    *,
    log: RunLog | None = None,
    confirmed_capabilities: Iterable[str] = (),
    cache: Cache | None = None,
    smart_client: LLMClient | None = None,
    semantic_hints: bool = False,
    render_output: bool = True,
    write_outreach: bool = False,
) -> RunState:
    """S0.1–S8 and S10: a resume file and a posting, to files a person can open.

    `smart_client` runs the judgment stages when the two are configured
    separately. The split is deliberate: the parse needs MANY calls and little
    capability, the planner needs ONE call and a lot of it.

    Four model calls, all in S0.2–S4. S10 renders afterwards with none, so the
    deliverable costs nothing to regenerate. The human gates (S0.4 and S9),
    the outreach draft (S8) and sending (S11) are not built; this stops where
    a person would be asked to approve.
    """
    log = log or NullRunLog()
    cache = cache or NullCache()
    state = ingest(resume_bytes, filename, client, log=log, cache=cache)
    assert state.doc is not None
    state = tailor(state.doc, jd_text, smart_client or client, log=log,
                   state=state, confirmed_capabilities=confirmed_capabilities,
                   cache=cache, semantic_hints=semantic_hints,
                   render_output=render_output,
                   write_outreach=write_outreach)
    # Accounting belongs to the thing being accounted. This used to live in
    # `scripts/run.py`, so a run started any other way — the API, a test, a
    # notebook — produced a manifest claiming no model calls at all.
    entries = merged_call_log(client, smart_client)
    budget = getattr(client, "budget", None)
    log.note(
        calls=getattr(budget, "calls", len(entries)),
        tokens=getattr(budget, "tokens", 0),
        call_log=entries,
        proof=proof_checks(state),
    )
    return state


# ── the guarantees, checked ───────────────────────────────────────────

def proof_checks(state: RunState) -> dict[str, bool]:
    """The invariants a run must not break, recorded in the manifest.

    Written to disk rather than only printed, so "did this ever regress" is a
    question the run folders can answer.
    """
    doc, tailored, diff = state.doc, state.tailored, state.diff
    if doc is None or tailored is None or diff is None:
        return {}

    dropped = [op for op in state.accepted if op.op == "drop_bullet"]
    return {
        "no_content_silently_lost":
            len(list(tailored.all_bullets()))
            == len(list(doc.all_bullets())) - len(dropped),
        # Headings alone are not the section. This compared two lists of
        # strings and passed a run whose skills section kept its heading while
        # losing five of its eight lines, including the candidate's entire
        # computer-vision group. A section that survives in name only has not
        # survived, so the check now asks whether each one still has content.
        "every_section_survived":
            [(s.heading, bool(s.items)) for s in tailored.sections]
            == [(s.heading, bool(s.items)) for s in doc.sections],
        "every_change_attributable":
            all(c.op_id for c in diff.changes),
        "parse_verified":
            state.coverage is None or state.coverage.is_clean,
    }
