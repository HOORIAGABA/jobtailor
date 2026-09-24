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


class SchemaValidationFailed(UpstreamError):
    """The model returned something that is not the requested shape, twice."""


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
