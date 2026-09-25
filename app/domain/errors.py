"""Error taxonomy.

Three kinds, because they need three different responses:

* `UserError`      — the person can fix it. Show them the message. 4xx.
* `UpstreamError`  — someone else's fault, probably transient. Retry. 5xx.
* `InvariantViolation` — a bug in this system. Fail loudly, never catch broadly.

The last one matters most. The previous version wrapped nearly every stage in
`except Exception: pass`, so a broken stage degraded quietly into a worse resume
instead of a visible failure. A run that cannot uphold its guarantees must stop,
not continue with them switched off.
"""
from __future__ import annotations


class JobTailorError(Exception):
    """Base for everything this system raises deliberately."""


# ── the user can fix it ───────────────────────────────────────────────

class UserError(JobTailorError):
    status_code = 400


class UnsupportedFileType(UserError):
    pass


class ExtractionEmpty(UserError):
    """A PDF with no text layer, usually a scan."""
    status_code = 422


class UnsafeURL(UserError):
    pass


# ── someone else's fault ──────────────────────────────────────────────

class UpstreamError(JobTailorError):
    status_code = 502


class LLMUnavailable(UpstreamError):
    """The model is missing, retired, or the key is wrong.

    Raised at startup by the model probe so a retired model id fails on boot
    with a clear message rather than three minutes into a pipeline run.
    """
    status_code = 503


class LLMRateLimited(UpstreamError):
    status_code = 429


class ModelOutputError(UpstreamError):
    """A failure whose evidence is the text the model produced.

    Carries `raw` so the pipeline can write it to the run folder. Without it,
    "the JSON was incomplete" is unactionable: you cannot tell a model that
    rambled from one that was cut off mid-sentence from one that answered in
    prose. The response is the artifact, and discarding it on failure throws
    away the only thing that explains the failure.

    `stage` travels with it so the saved file can be named after the stage
    rather than its position in the run. A file called `05_model_response.txt`
    answers the wrong question: the reader knows when it happened and needs to
    know *which call* it was.
    """

    def __init__(self, message: str, raw: str = "", stage: str = "") -> None:
        super().__init__(message)
        self.raw = raw
        self.stage = stage


class SchemaValidationFailed(ModelOutputError):
    """The model returned something that is not the requested shape, twice."""


class ResponseTruncated(ModelOutputError):
    """The model was still writing when it hit the token ceiling.

    Deliberately NOT a `SchemaValidationFailed`. Truncated JSON fails schema
    validation, so the two look identical from the outside — and conflating
    them sends you to rewrite a prompt when the fix is a number. Measured: a
    7,660-character resume was cut at exactly its 4,213-token ceiling, twice,
    and reported itself as "the model would not follow the schema" both times.
    """


# ── our fault ─────────────────────────────────────────────────────────

class InvariantViolation(JobTailorError):
    status_code = 500


class IdNotFound(InvariantViolation):
    pass


class IllegalTransition(InvariantViolation):
    pass


class BudgetExceeded(InvariantViolation):
    """More model calls or tokens than a run is allowed (I7).

    Deliberately an InvariantViolation: silently truncating a run would produce
    a half-tailored resume that looks finished.
    """
