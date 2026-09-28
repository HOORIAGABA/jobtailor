"""The honesty guarantee. Deterministic, no LLM.

Every operation is checked here before anything is applied. The checks are
rule-based rather than model-judged for three reasons: these properties are
decidable, rules cost nothing, and an LLM judge is gameable by the same model it
judges.

Claims are not all the same, so they are not all checked the same way:

  Class A  asserted fact   numbers, employers, titles, dates
                           -> must appear verbatim in the origin. No inference.
  Class B  framing         describing real work in the role's language
                           -> must be entailed by cited evidence, and must not
                              escalate seniority or merely inject keywords.
  Class C  named capability  "Airflow", "PyTorch"
                           -> must be in the grounding corpus.

Treating all three alike is what made the earlier design block legitimate
rewording while still leaving an escalation loophole open.

A rejection is never a run failure. The original is kept, a question may be
raised with the candidate, and the run continues.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from app.domain.models import ResumeDoc
from app.engine.normalize import existing_skills, skill_key
from app.engine.text import tokens as text_tokens
from app.domain.ops import (
    ADVISORY_OPS, DropBullet, Op, PromoteItem, Reject, ReorderBullets,
    ReorderItems, RewriteBullet, SetSkills, SetSummary,
)

# ── grounding corpus ──────────────────────────────────────────────────

def grounding_corpus(doc: ResumeDoc, confirmed: Iterable[str] = ()) -> set[str]:
    """THE single authority on what may be claimed.

    Two sources: what the document says, and what the candidate has explicitly
    confirmed. They were previously two parallel authorities, which is the same
    shape of bug as having three keyword tables — one function, one answer.

    `confirmed` comes from the capability ledger and is human-supplied. Inference
    may propose an entry; only a person may add one.
    """
    corpus = {s.lower().strip() for s in doc.skill_inventory if s.strip()}
    corpus |= {c.lower().strip() for c in confirmed if c and c.strip()}
    return corpus


# ── extraction helpers ────────────────────────────────────────────────

# ★ A number is its digits AND its unit, and this regex is why.
#
# The first version was `\d+(?:[.,]\d+)?` with commas stripped, which made a
# number a bare digit string. Two fabrications walked straight through it:
#
#   "Reduced latency by 40 ms"  ->  "Reduced latency by 40%"     accepted
#   "900 tickets, 12,000 users" ->  "12,000,900 sessions"        accepted
#
# The first because the unit was discarded, so `40` matched `40`. The second
# because `(?:[.,]\d+)?` allows only ONE group, so `12,000` tokenised as
# `{"12", "000"}` and a 1,000x inflation was assembled from parts "present in
# the original".
#
# So: grouped thousands are one literal, and a SHORT trailing unit (ms, km, MB,
# k, %) is carried with the value. The unit is capped at four letters with a
# negative lookahead so `900 tickets` stays `900` — attaching an ordinary noun
# would reject every honest rewording of the words around a number.
_NUMBER = re.compile(
    r"(?P<value>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<unit>\s*%|\s*[A-Za-z]{1,4}(?![A-Za-z]))?"
)

# Tokenisation lives in engine.text so every comparison in the system uses the
# same notion of a word. Three disagreeing tokenisers is what the previous
# version had.
_tokens = text_tokens

# The same word shape as `engine.text`, kept as a span-yielding pattern because
# `entities` needs to know WHERE a token sits, not only what it is.
_WORD_SPAN = re.compile(r"(?:\.[A-Za-z]|[A-Za-z0-9])[A-Za-z0-9+#.\-]*")

# Words that are capitalized for grammatical reasons, not because they name
# something. Without this every sentence-initial word looks like an entity.
_NOT_ENTITIES = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "as", "is", "are", "was", "were", "be", "been", "this",
    "that", "these", "those", "it", "we", "i", "my", "our", "their", "his",
    "her", "led", "built", "created", "designed", "developed", "implemented",
    "managed", "improved", "reduced", "increased", "delivered", "launched",
    "owned", "drove", "worked", "helped", "supported", "maintained", "wrote",
    "analyzed", "automated", "migrated", "scaled", "shipped", "used",
})


def numbers(text: str) -> set[str]:
    """Numeric literals with their units, normalised for comparison.

    `1,200` and `1200` compare equal; `40 ms`, `40%` and `40` do not. The unit
    is folded to lowercase and its spacing removed, so `40 MB` == `40mb`.
    """
    found: set[str] = set()
    for match in _NUMBER.finditer(text or ""):
        value = match.group("value").replace(",", "")
        unit = (match.group("unit") or "").strip().lower()
        found.add(value + unit)
    return found


# Words that open a sentence and name nothing. Only consulted for a token that
# is sentence-initial, where the capital is grammar rather than evidence.
#
# This list exists because of a measured false positive, and the measurement is
# worth keeping: a perfectly ordinary covering letter —
#
#     "… Happy to talk this week if it is useful."
#     "… If that is a hard requirement rather than a preference …"
#
# had `Happy` and `If` reported as "named thing(s) the resume does not support".
# The old rule skipped only the FIRST word of the whole text, so in anything
# longer than one sentence every sentence opener was read as a capability
# claim. On a real draft that is a warning on every letter, which is how a
# person learns to ignore the warnings — and the warnings are the product.
_SENTENCE_OPENERS = frozenset({
    "happy", "glad", "pleased", "thanks", "thank", "please", "best", "regards",
    "hello", "hi", "dear", "if", "while", "although", "though", "since",
    "given", "having", "would", "could", "should", "may", "might", "must",
    "here", "there", "they", "you", "your", "when", "where", "what", "why",
    "how", "so", "then", "also", "however", "unfortunately", "additionally",
    "finally", "first", "second", "next", "after", "before", "during", "both",
    "either", "neither", "not", "no", "yes", "one", "two", "more", "most",
    "many", "some", "any", "all", "each", "every", "such", "very", "just",
    "only", "even", "still", "again", "about", "over", "under", "between",
    "without", "within", "into", "out", "per", "via", "than", "because",
    "therefore", "thus", "hence", "meanwhile", "moreover", "furthermore",
    "overall", "currently", "recently", "previously", "today", "now", "as",
    "at", "for", "from", "with", "by",
})

# What ends a sentence, for the purpose of "is the next capital grammatical".
# A newline counts: a bullet list has no full stops and every line starts with
# a capital.
_SENTENCE_END = re.compile(r"[.!?:;\n\r•\-–—]\s*$")


def _sentence_starts(text: str) -> set[int]:
    """Character offsets where a new sentence begins."""
    starts = {0}
    for match in re.finditer(r"[.!?:;\n\r•]|(?<=\s)[-–—](?=\s)", text or ""):
        after = match.end()
        while after < len(text) and text[after].isspace():
            after += 1
        starts.add(after)
    return starts


def entities(text: str) -> set[str]:
    """Candidate named things: proper nouns, acronyms, tool-shaped tokens.

    Deliberately over-inclusive on tokens that *look* like names, and
    position-aware about capitals: a capital at the start of a sentence is
    grammar, so it only counts as a name when the word is not one of the
    ordinary openers above.

    **The residual false negative is accepted deliberately.** A capability named
    only at the very start of a sentence — "Kubernetes is required here." — and
    nowhere else is missed. That is the right way round: this set is used to
    REFUSE claims, so a false positive blocks an honest sentence while a false
    negative lets one through to the other checks and to the human at the gate.
    A capability that appears anywhere else in the text is still caught, and in
    practice a letter that mentions a tool mentions it mid-sentence too.
    """
    body = text or ""
    starts = _sentence_starts(body)
    found: set[str] = set()

    for match in _WORD_SPAN.finditer(body):
        tok = match.group().rstrip(".-") or match.group()
        low = tok.lower()
        if low in _NOT_ENTITIES or len(tok) < 2:
            continue

        is_acronym = tok.isupper() and len(tok) >= 2
        is_toolish = any(c in tok for c in "+#.") and any(c.isalpha() for c in tok)
        # An acronym or a tool-shaped token is a strong signal wherever it sits.
        if is_acronym or is_toolish:
            found.add(low)
            continue

        if not tok[0].isupper():
            continue
        if match.start() in starts and low in _SENTENCE_OPENERS:
            continue                      # grammar, not a name
        if match.start() == 0:
            continue                      # the very first word, as before
        found.add(low)
    return found


def content_tokens(text: str) -> list[str]:
    return [t.lower() for t in _tokens(text)]


# ── Class B: seniority escalation ─────────────────────────────────────

WEAK_VERBS = frozenset({
    "assisted", "assist", "helped", "help", "supported", "support",
    "contributed", "contribute", "participated", "participate", "involved",
    "shadowed", "observed",
})

OWNERSHIP_VERBS = frozenset({
    "led", "lead", "drove", "drive", "owned", "own", "spearheaded", "spearhead",
    "headed", "head", "directed", "direct", "architected", "architect",
    "founded", "established", "orchestrated",
})

# Scope claims that quietly inflate impact when they weren't in the original.
SCOPE_MARKERS = (
    "team of", "company-wide", "companywide", "org-wide", "end-to-end",
    "end to end", "from scratch", "cross-functional", "enterprise-wide",
    "across the company", "at scale",
)


def escalates_seniority(origin: str, rewritten: str) -> str | None:
    """Return a reason when a rewrite inflates the candidate's role.

    "helped with the migration" -> "drove the migration" describes the same work
    and asserts no new number, so the Class-A check passes it. It is still a
    promotion the resume does not support.
    """
    o, r = set(content_tokens(origin)), set(content_tokens(rewritten))

    # ★ A new ownership verb is the violation, whether or not the origin had a
    # weak one.
    #
    # The first version required `o & WEAK_VERBS` — the origin had to already
    # say "helped" or "assisted" for the check to fire at all. So a neutral
    # origin was a free promotion:
    #
    #   "Fixed bugs in the payments service"
    #     -> "Directed the payments service rebuild"          accepted
    #
    # No new number and both verbs are in `_NOT_ENTITIES`, so Class A and
    # Class C saw nothing either. The docstring above says this function
    # rejects a rewrite that "inflates the candidate's role"; it now does.
    claimed = (r & OWNERSHIP_VERBS) - (o & OWNERSHIP_VERBS)
    if claimed:
        weak = sorted(o & WEAK_VERBS)
        return (
            f"origin uses {weak}; rewrite claims {sorted(claimed)}"
            if weak else
            f"rewrite claims {sorted(claimed)}, which the original does not"
        )
    ol, rl = (origin or "").lower(), (rewritten or "").lower()
    for marker in SCOPE_MARKERS:
        if marker in rl and marker not in ol:
            return f"rewrite adds unsupported scope claim {marker!r}"
    return None


# ── Class B: keyword stuffing ─────────────────────────────────────────

_STUFF_SIMILARITY = 0.85
# Three, from the shape of real bullets rather than from theory: a bullet that
# names four or more distinct tools has stopped describing work and started
# listing inventory. Raising it weakens the count rule below, which is the only
# density rule that applies when the model declares no target terms.
_MAX_TERMS_PER_BULLET = 3
_MAX_TERM_DENSITY = 0.25


def _strip_terms(text: str, terms: Sequence[str]) -> list[str]:
    toks = content_tokens(text)
    drop = {t.lower() for term in terms for t in _tokens(term)}
    return [t for t in toks if t not in drop]


def _jaccard(a: Sequence[str], b: Sequence[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# The words that join a list of keywords to a sentence. They are the entire
# grammatical cost of appending "using X, Y and Z" to a bullet, so a rewrite
# whose only new words are these has added no meaning.
_LIST_GLUE = frozenset({
    "using", "with", "including", "and", "or", "plus", "via", "through",
    "leveraging", "utilizing", "utilising", "across", "in", "on", "for",
    "such", "as", "well", "alongside", "featuring", "powered", "by", "built",
})


def is_keyword_stuffing(origin: str, rewritten: str, terms: Sequence[str]) -> bool:
    """True when the rewrite added keywords and changed nothing else.

    Two shapes, because stuffing has two shapes and only one of them is a
    similarity problem.

    **Substitution** — the sentence is reworded around inserted terms. Strip the
    terms from both sides; if what remains is essentially the same sentence, the
    rewrite decorated rather than changed it. That is the Jaccard check.

    **Appending** — `"…a day a week"` becomes `"…a day a week using Python, SQL,
    PostgreSQL and Docker"`. Jaccard *misses this*, and the reason is worth
    stating: appending anything lowers set similarity, so the longer the list of
    keywords, the more "changed" the sentence scores. The check rewarded exactly
    what it existed to catch.

    Found by an eval case. A bullet stuffed with four grounded capability names
    scored 0.69 — under the 0.85 threshold — and was accepted. It survived the
    entity rule too, because every name really was in the resume: this is the
    version of stuffing that nothing else in the validator can see.

    So appending is checked structurally instead: if every new word is either a
    capability name or the grammar needed to bolt a list onto a sentence, no
    meaning was added, however long the addition is.

    `terms` is the job's vocabulary. Capability-shaped words the *resume* owns
    count too — `entities()` finds them — because stuffing with your own real
    skills is still stuffing, and it is the only kind that gets this far.
    """
    origin, rewritten = origin or "", rewritten or ""
    vocabulary = list(terms) + list(entities(rewritten))

    present_before = {t.lower() for t in vocabulary if t.lower() in origin.lower()}
    present_after = {t.lower() for t in vocabulary if t.lower() in rewritten.lower()}
    if not (present_after - present_before):
        return False                      # no keywords were added at all

    kept_origin = _strip_terms(origin, vocabulary)
    kept_rewritten = _strip_terms(rewritten, vocabulary)

    # Appending: nothing was removed, and everything new is glue.
    added = [w for w in kept_rewritten if w not in set(kept_origin)]
    removed = [w for w in kept_origin if w not in set(kept_rewritten)]
    if not removed and all(word in _LIST_GLUE for word in added):
        return True

    # Substitution: the sentence around the terms did not really move.
    return _jaccard(kept_origin, kept_rewritten) > _STUFF_SIMILARITY


def term_density_violation(text: str, terms: Sequence[str]) -> str | None:
    low = (text or "").lower()
    hits = [t for t in terms if t and t.lower() in low]
    if len(hits) > _MAX_TERMS_PER_BULLET:
        return f"{len(hits)} job terms in one bullet (max {_MAX_TERMS_PER_BULLET})"
    words = len(content_tokens(text))
    if words and len(hits) / words > _MAX_TERM_DENSITY:
        return f"term density {len(hits)}/{words} exceeds {_MAX_TERM_DENSITY}"
    return None


# ── result ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ValidationResult:
    accepted: list[Op]
    rejected: list[Reject]

    @property
    def fabrication_count(self) -> int:
        return sum(
            1 for r in self.rejected
            if r.code in ("fabricated_number", "unsupported_entity")
        )


# ── per-op checks ─────────────────────────────────────────────────────

def _reject(op: Op, code, detail: str, ask: bool = False) -> Reject:
    return Reject(op_id=op.op_id, op_kind=op.op, code=code, detail=detail, ask_user=ask)


def _check_rewrite(op: RewriteBullet, doc: ResumeDoc, corpus: set[str]) -> Reject | None:
    origin = doc.bullet(op.bullet_id)
    if origin is None:
        return _reject(op, "unknown_id", f"no bullet {op.bullet_id!r}")
    if not (op.text or "").strip():
        return _reject(op, "unknown_id", "rewrite has no text")

    # Class A — numbers must come from the origin. This is why the writer is
    # not asked to declare which numbers it used: a self-report can lie.
    invented = numbers(op.text) - numbers(origin.text)
    if invented:
        return _reject(
            op, "fabricated_number",
            f"introduces {sorted(invented)} not present in the original bullet",
            ask=True,
        )

    # Class C — named things must be in the origin or the grounding corpus.
    unsupported = entities(op.text) - entities(origin.text) - corpus
    if unsupported:
        return _reject(
            op, "unsupported_entity",
            f"names {sorted(unsupported)} which the resume does not support",
            ask=True,
        )

    # Class B — framing may not inflate, and may not be decoration.
    if (reason := escalates_seniority(origin.text, op.text)):
        return _reject(op, "seniority_escalation", reason)
    # ★ These two run ALWAYS. They used to be behind `if op.target_terms:`.
    #
    # `target_terms` comes from the planner — the model. So the model decided
    # whether its own rewrite got checked for keyword stuffing, and the planner
    # prompt tells it that listing terms is what triggers rejection. Declaring
    # nothing was both permitted and incentivised.
    #
    # This is verbatim the anti-pattern `domain/ops.py` records as already
    # removed once: "An earlier draft had the writer declare `numbers_used` /
    # `terms_used`, which the validator then trusted. A model that
    # under-declares evades the fabrication guard entirely." It survived here.
    #
    # The vocabulary now comes from the code when the model supplies none: what
    # the rewrite names that the original did not.
    declared = list(op.target_terms)
    terms = declared or sorted(entities(op.text) - entities(origin.text))
    if terms:
        if is_keyword_stuffing(origin.text, op.text, terms):
            return _reject(op, "keyword_stuffing",
                           "only keywords changed; the bullet says the same thing")

        # Density splits, and the split is not a compromise — the two halves of
        # `term_density_violation` measure different things.
        #
        # The RATIO half is calibrated for terms the planner deliberately
        # targeted, where a high proportion means padding. Applied to every
        # newly-named entity it fires on honest writing: "Built reporting
        # services in .NET and C++" is two names in seven words, 0.29, over the
        # 0.25 bound — and that rewrite is entirely legitimate. (It is a
        # regression test: `test_dotted_tool_names_match_the_corpus`.)
        #
        # The COUNT half is robust either way. Four or more new names in one
        # bullet is cramming whatever the model declared, so that is the rule
        # that applies when the model declared nothing.
        if declared:
            if (reason := term_density_violation(op.text, declared)):
                return _reject(op, "term_density", reason)
        elif len(terms) > _MAX_TERMS_PER_BULLET:
            return _reject(
                op, "term_density",
                f"{len(terms)} names new to this bullet "
                f"(max {_MAX_TERMS_PER_BULLET})")
    return None


def _document_text(doc: ResumeDoc) -> str:
    """Everything the resume says, as one string.

    The grounding corpus for a summary. A summary legitimately draws on the
    whole document rather than on one bullet, so restricting it to the cited
    lines would reject honest summaries; requiring every number and every named
    thing to appear SOMEWHERE in the resume is the rule that is both true and
    enforceable.
    """
    parts: list[str] = [doc.summary or ""]
    for item in doc.all_items():
        parts.extend(x for x in (item.title, item.org, item.dates) if x)
        parts.extend(b.text for b in item.bullets)
    parts.extend(doc.skill_inventory)
    return " \n".join(p for p in parts if p)


def _check_set_summary(op: SetSummary, doc: ResumeDoc, corpus: set[str]) -> Reject | None:
    """★ The summary is checked like everything else. It was not checked at all.

    This branch used to verify two things: that `cites` was non-empty, and that
    the cited ids resolved. No Class A, no Class B, no Class C — on the ONE
    operation where the model writes free prose, and into the block that sits
    at the top of the page where a recruiter reads first.

    What that accepted, verified against a resume containing none of it:

        "Senior ML lead who grew revenue 400% at Google, managing a
         team of 30 engineers using Kubernetes."

    `fabrication_count` reported 0 and `apply_ops` wrote it to `doc.summary`.
    It laundered further: `engine.message` seeds the email's legal-number set
    from `numbers(doc.summary)`, so an invented figure in the summary became
    quotable in the outreach email as well.

    A test asserted the accepting behaviour, which is why nothing complained.
    """
    if not op.cites:
        return _reject(op, "missing_citation",
                       "a summary must cite the bullets backing its claims")
    if (missing := [c for c in op.cites if not doc.has(c)]):
        return _reject(op, "unknown_id", f"cites unknown ids {missing}")
    if not (op.text or "").strip():
        return _reject(op, "unknown_id", "summary has no text")

    grounding = _document_text(doc)

    # Class A — every number must already be somewhere on the resume.
    invented = numbers(op.text) - numbers(grounding)
    if invented:
        return _reject(
            op, "fabricated_number",
            f"summary introduces {sorted(invented)}, which appears nowhere in "
            f"the resume",
            ask=True,
        )

    # Class C — every named capability must be on the resume or in the corpus.
    unsupported = entities(op.text) - entities(grounding) - corpus
    if unsupported:
        return _reject(
            op, "unsupported_entity",
            f"summary names {sorted(unsupported)} which the resume does not "
            f"support",
            ask=True,
        )

    # Class B — a summary may frame, but it may not promote. The origin here is
    # the whole document: if no bullet anywhere claims ownership, the summary
    # may not either.
    if (reason := escalates_seniority(grounding, op.text)):
        return _reject(op, "seniority_escalation", reason)
    return None


def _check_set_skills(op: SetSkills, doc: ResumeDoc, corpus: set[str]) -> Reject | None:
    """Reorder and regroup freely. Do not lose anything.

    **This rule exists because of a specific accepted edit.** On
    `runs/2026-09-25T07-51-32`, `set_skills` took a skills section of 8 grouped
    lines down to 3 unlabelled ones and deleted the candidate's entire computer
    vision group — YOLO, VGG16, VGG19, MobileNet V3, ResNet50, object detection
    — which is what most of their project work is about.

    The validator accepted it, correctly by its own rules: nothing was
    invented, so `fabrication_count` was 0. **Deleting true content was simply
    not a thing this stage checked.** The run's `no_content_silently_lost`
    proof check did catch it (48 bullets in, 43 out, no `drop_bullet` op) — but
    a proof check is a report printed after the edit has been applied, and by
    then the op is already in the list the candidate is asked to approve. A
    guard that reports is not a guard that refuses.

    `set_skills` is a whole-section replacement, so it is the one op that can
    quietly shrink the document. The rules below make the replacement
    non-destructive while leaving the model complete freedom over arrangement:

    1. **Nothing is lost.** Every skill on the page must still be on the page.
    2. **Every group is labelled.** The failing op emitted three groups with
       `label: ""`, which renders as three anonymous rows.
    3. **No skill appears twice.** A term in two groups is a contradiction
       about where it belongs.
    4. **Nothing is invented** — the pre-existing corpus check, unchanged.

    Regrouping, renaming a group, merging two groups and reordering are all
    still allowed, which is the point: the planner needs a way to put the
    job's skills first, and taking that away would only push the damage into
    some other op.
    """
    proposed: dict[str, str] = {}
    duplicates: list[str] = []
    for group in op.groups:
        for skill in group.skills:
            key = skill_key(skill)
            if not key:
                continue
            if key in proposed:
                duplicates.append(skill)
            proposed[key] = skill

    existing = existing_skills(doc)
    if (lost := sorted(existing[k] for k in existing if k not in proposed)):
        return _reject(
            op, "skills_dropped",
            f"would remove {len(lost)} skill(s) already on the resume: "
            f"{lost}. Regroup and reorder freely, but keep every one.",
        )

    if (unlabelled := sum(1 for g in op.groups if g.skills and not g.label.strip())):
        return _reject(op, "unlabelled_group",
                       f"{unlabelled} group(s) have no heading")

    if duplicates:
        return _reject(op, "duplicate_skill",
                       f"listed in more than one group: {sorted(set(duplicates))}")

    # An empty corpus means we cannot ground anything, so we do not filter —
    # rejecting every skill would be worse than allowing them. This is also the
    # "resume had no skills section" case, where the model composes one from
    # scratch and there is nothing to compare it against.
    unsupported = {s for k, s in proposed.items() if k not in corpus}
    if corpus and unsupported:
        return _reject(op, "unsupported_entity",
                       f"skills not evidenced by the resume: {sorted(unsupported)}",
                       ask=True)
    return None


def _check_reorder(op, doc: ResumeDoc) -> Reject | None:
    if isinstance(op, ReorderItems):
        section = doc.section(op.section_id)
        if section is None:
            return _reject(op, "unknown_id", f"no section {op.section_id!r}")
        existing = section.item_ids()
    else:
        item = doc.item(op.item_id)
        if item is None:
            return _reject(op, "unknown_id", f"no item {op.item_id!r}")
        existing = item.bullet_ids()
    # Multiset equality: a reorder can neither drop nor duplicate.
    if sorted(op.order) != sorted(existing):
        return _reject(op, "not_a_permutation",
                       f"expected a permutation of {existing}, got {op.order}")
    return None


def validate(
    ops: Sequence[Op],
    doc: ResumeDoc,
    confirmed_capabilities: Iterable[str] = (),
) -> ValidationResult:
    """Check every op against the base document. Never mutates anything."""
    corpus = grounding_corpus(doc, confirmed_capabilities)
    accepted: list[Op] = []
    rejected: list[Reject] = []
    promotions: dict[str, int] = {}

    for op in ops:
        if op.op in ADVISORY_OPS:
            accepted.append(op)          # never mutates; routes to the UI
            continue

        problem: Reject | None = None

        if isinstance(op, RewriteBullet):
            problem = _check_rewrite(op, doc, corpus)

        elif isinstance(op, (ReorderItems, ReorderBullets)):
            problem = _check_reorder(op, doc)

        elif isinstance(op, PromoteItem):
            if doc.item(op.item_id) is None:
                problem = _reject(op, "unknown_id", f"no item {op.item_id!r}")
            elif not op.rationale.strip():
                problem = _reject(op, "missing_citation",
                                  "promotion out of chronological order needs a rationale")
            else:
                section = doc.section_of_item(op.item_id)
                key = section.id if section else "?"
                promotions[key] = promotions.get(key, 0) + 1
                if promotions[key] > 1:
                    problem = _reject(op, "too_many_promotions",
                                      f"more than one promotion in {key}")

        elif isinstance(op, SetSkills):
            problem = _check_set_skills(op, doc, corpus)

        elif isinstance(op, SetSummary):
            problem = _check_set_summary(op, doc, corpus)

        elif isinstance(op, DropBullet):
            item = doc.item(_safe_item_of(op.bullet_id))
            if item is None or doc.bullet(op.bullet_id) is None:
                problem = _reject(op, "unknown_id", f"no bullet {op.bullet_id!r}")
            elif len(item.bullets) <= 1:
                problem = _reject(op, "would_empty_item",
                                  "dropping this would leave the entry with no bullets")

        (rejected if problem else accepted).append(problem or op)

    return ValidationResult(accepted=accepted, rejected=rejected)


def _safe_item_of(bid: str) -> str:
    from app.domain.ids import item_id_of_bullet
    try:
        return item_id_of_bullet(bid)
    except ValueError:
        return ""
