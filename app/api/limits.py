"""Per-user caps on the endpoints that cost something.

Three things here are worth spending money on and therefore worth capping:
uploading a resume (model calls plus memory), starting a run (seven model calls
against a free-tier quota), and sending (a Gmail quota and an irreversible
action). Everything else is a database read.

**This is an in-process counter, and that is a real limitation.** Two workers
mean two independent counters, so the effective limit doubles; a restart clears
it. The honest alternatives are Redis (another service, another thing to keep
alive on a free tier) or a database table (a write on every request). For a
single-worker deployment serving one person's applications, a dictionary is the
right size of solution, and the failure mode of getting it wrong is "the limit
was twice as generous as intended" rather than "the limit did not exist".

It is written so that swapping the store is a change to one class.

**A sliding window, not a fixed one.** A fixed window lets someone spend the
whole allowance at 11:59 and the whole next allowance at 12:01 — twice the
intended rate across two minutes. Keeping the timestamps costs a few bytes per
user and removes the edge entirely.

**Only successful requests count.** The decrement happens on the way in, but a
request refused before it does any work — a 404, a validation error — gives the
token back. Otherwise a client with a bug in its retry loop locks a person out
of their own account by failing repeatedly.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limit:
    """`count` actions per `window` seconds."""
    count: int
    window: float
    what: str

    @property
    def human_window(self) -> str:
        if self.window >= 3600:
            hours = round(self.window / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''}"
        minutes = round(self.window / 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"


# Sized for a person applying for jobs, not for a service.
#
# Twenty runs an hour is more applications than anyone writes in a day, and it
# is well inside a Google AI Studio free tier at seven calls each. Ten sends an
# hour is above any honest use and far below the point where Gmail starts
# treating an account as a sender of bulk mail — which is a consequence that
# lands on the person's real address, not on this application.
UPLOAD = Limit(count=20, window=3600, what="resume upload")
RUN = Limit(count=20, window=3600, what="tailoring run")
SEND = Limit(count=10, window=3600, what="sent message")


class RateLimiter:
    """Sliding-window counts, keyed on (user, action). Thread-safe."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def take(self, user_id: str, limit: Limit, *, now: float | None = None
             ) -> None:
        """Consume one. Raises 429 when the window is full."""
        moment = now if now is not None else time.monotonic()
        key = (user_id, limit.what)
        with self._lock:
            events = self._events[key]
            cutoff = moment - limit.window
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit.count:
                retry_after = max(1, int(events[0] + limit.window - moment))
                logger.warning("Rate limit hit: %s for user %s", limit.what,
                               user_id)
                raise HTTPException(
                    429,
                    f"That is {limit.count} {limit.what}s in {limit.human_window}, "
                    f"which is the limit. Try again in "
                    f"{_friendly(retry_after)}.",
                    headers={"Retry-After": str(retry_after)},
                )
            events.append(moment)

    def give_back(self, user_id: str, limit: Limit) -> None:
        """Return the most recent token, for a request that did no work.

        A 404 or a validation error should not spend someone's allowance —
        otherwise a client with a broken retry loop locks a person out of their
        own account by failing repeatedly.
        """
        with self._lock:
            events = self._events.get((user_id, limit.what))
            if events:
                events.pop()

    def reset(self) -> None:
        """For tests. Never called at runtime."""
        with self._lock:
            self._events.clear()


def _friendly(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = round(seconds / 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


# One limiter for the process. Module-level because the counts have to be shared
# across requests, which is the entire point.
limiter = RateLimiter()
