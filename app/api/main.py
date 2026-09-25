"""The HTTP layer. Two modes, one app.

**Read-only mode** is the public deployment. It serves runs that already
happened — every stage, the diff, the outreach draft, the rendered resume — and
refuses to start a new one. This is not a limitation dressed up: a run takes
around eight minutes on a local model, no HTTP request survives that, the model
itself is on a laptop the internet cannot reach, and a hosted key would spend a
free-tier quota per visitor. What a public URL can honestly offer is the product
with the waiting removed.

**Full mode** is what runs on the machine that has Ollama. Same app, same UI,
plus the ability to start a run.

The mode is a deployment setting, not a code path the frontend has to know
about: `GET /api/capabilities` says what this instance can do, and the UI hides
what it cannot. A frontend that guessed from the hostname would be wrong the
first time someone ran it somewhere else.

**Starting a run returns immediately.** `POST /api/runs` creates the run and
answers `202` with an id; progress arrives on the SSE stream and the result is
fetched when it is done. The alternative — hold the connection for eight minutes
— fails behind every proxy there is. v1's version of this lost a run to a
15-minute idle spin-down killing a daemon thread, which is the same lesson from
the other side: work that outlives a request needs to be recoverable, and the
thing that makes it recoverable here is that every stage is already written to
disk as it completes.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import auth as auth_router
from app.api import gate as gate_router
from app.api import resumes as resumes_router
from app.api import runs as runs_router
from app.api import send as send_router
from app.api.archive import Archive, RunNotFound
from app.api.runner import Runner
from app.config import Settings

logger = logging.getLogger(__name__)

RUNS_DIR = Path("runs")

# Set on a deployment to serve stored runs and refuse to start new ones. See
# `create_app` for why this is explicit rather than inferred.
READ_ONLY_ENV = "JOBTAILOR_READ_ONLY"

# Set on a read-only instance to give anonymous visitors an identity, so a
# public demo can show the runs it was seeded with. It resolves ONLY when the
# instance is read-only, which is what stops it becoming `DEV_USER_EMAIL` with
# a new name: on a writable instance it does nothing at all, so it cannot be
# the thing that lets a stranger upload, approve or send.
DEMO_EMAIL_ENV = "DEMO_USER_EMAIL"

# Methods that cannot change anything, so a read-only instance may serve them.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# The one exception. Signing out only clears a cookie the browser already has,
# and a demo you cannot sign out of is a demo that has trapped you.
ALWAYS_ALLOWED = frozenset({"/api/auth/logout"})

MEDIA = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}


def create_app(
    runs_dir: Path | None = None,
    *,
    read_only: bool | None = None,
) -> FastAPI:
    """Build the app. Arguments exist so tests can point it at a tmp directory."""
    archive = Archive(runs_dir or RUNS_DIR)
    runner = Runner(archive.root)

    # Read-only unless explicitly told otherwise: the safe default for the mode
    # that is exposed to the internet.
    #
    # Three sources, in order of how explicit they are. The argument is for
    # tests. `JOBTAILOR_READ_ONLY` is for a deployment, and it exists because a
    # hosted instance must be able to say "serve stored runs only" without
    # relying on the absence of a model to imply it — a stray `LLM_API_KEY` in
    # the environment would otherwise silently turn a public URL into something
    # that spends a quota per visitor. Falling back to "can this instance reach
    # a model at all" keeps a laptop working with no configuration.
    if read_only is None:
        declared = os.environ.get(READ_ONLY_ENV, "").strip().lower()
        if declared in ("1", "true", "yes", "on"):
            locked = True
        elif declared in ("0", "false", "no", "off"):
            locked = False
        else:
            locked = runner.can_start is False
    else:
        locked = read_only

    # The router reads it to refuse a start; putting it on the app rather
    # than closing over it keeps one answer for 'is this instance writable'.
    app_state_read_only = locked

    app = FastAPI(
        title="JobTailor",
        summary="Tailors a resume to a posting, and shows its work.",
        version="0.1.0",
    )
    app.state.read_only = app_state_read_only

    # ★ Read-only has to mean read-only, and until this existed it did not.
    #
    # Exactly one endpoint checked the flag — `POST /api/runs` — so on a public
    # instance a visitor could still confirm a resume, approve a draft and press
    # send. With the console backend nothing would leave the machine, but the
    # state would change under whoever else was looking at it, and "read-only"
    # would be a claim the software did not keep.
    #
    # Middleware rather than a dependency on each route: a route that has to
    # remember to opt in is a route that will forget, and the one that forgets
    # will be the one added last, by someone who did not read this comment.
    @app.middleware("http")
    async def refuse_writes_when_read_only(request, call_next):
        if (app_state_read_only
                and request.method not in SAFE_METHODS
                and request.url.path not in ALWAYS_ALLOWED):
            return JSONResponse(
                status_code=403,
                content={"detail":
                         "This instance serves stored runs and cannot change "
                         "anything. Run JobTailor locally to tailor a resume — "
                         "it needs a model on the same machine."},
            )
        return await call_next(request)

    # `allow_origins=["*"]` was correct while nothing here was private, and is
    # now impossible: a browser refuses to send cookies to a wildcard origin, so
    # a session cookie and a wildcard cannot both exist. The origins come from
    # CORS_ORIGINS, which defaults to the Next.js dev server.
    #
    # This is the good direction for the trade to go. A wildcard plus credentials
    # would mean any page on the internet could call this API as the signed-in
    # user, which is CSRF with the door held open.
    origins = Settings().cors_origin_list
    app.add_middleware(
        CORSMiddleware, allow_origins=origins, allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"],
    )

    # The resume half of the product: upload, confirm, edit. Mounted even in
    # read-only mode — `current_user` refuses without a signed-in identity, and
    # the public instance has none, so the routes exist and answer 401 rather
    # than vanishing. A UI that has to discover which endpoints are mounted is a
    # UI that guesses.
    app.include_router(auth_router.router)
    app.include_router(resumes_router.router)
    app.include_router(runs_router.router)
    app.include_router(gate_router.router)
    app.include_router(send_router.router)

    @app.get("/")
    def index() -> dict[str, Any]:
        """What is here, and whether it is set up.

        A bare 404 at the root is technically right and tells a person nothing.
        This is the first URL anyone opens, so it is the right place to answer
        "is the database migrated" and "do I have an identity" — both of which
        otherwise surface much later as a confusing 500 or a 401 on a form
        submit.
        """
        return {
            "service": "JobTailor",
            "docs": "/docs",
            "read_only": locked,
            "setup": _setup_state(),
            "endpoints": sorted(
                route.path for route in app.routes
                if getattr(route, "path", "").startswith("/api/")
            ),
        }

    @app.get("/api/capabilities")
    def capabilities() -> dict[str, Any]:
        """What this instance can do, so the UI does not have to guess."""
        return {
            "read_only": locked,
            "can_start_runs": not locked and runner.can_start,
            "why": (
                "This instance serves runs that already happened. Starting a "
                "run needs a local model, and takes around eight minutes."
                if locked else
                f"Ready to run against {runner.model or 'the configured model'}."
            ),
            "stages_built": [
                "S0.1 extract", "S0.2 parse", "S0.3 normalize", "S1 brief",
                "S2 evidence", "S3 plan", "S4 write", "S5 validate",
                "S0.4 confirm", "S6 apply", "S7 diff", "S8 outreach",
                "S9 approve", "S10 render", "S11 send (console)",
            ],
            "stages_not_built": [],
            # Says plainly whether this instance can put mail on the wire.
            "mail_provider": _mail_provider(),
        }

    return app


def _mail_provider() -> str:
    """Which backend a send would use, as a sentence the UI can show.

    The UI needs this to label the button honestly. "Send" over a console
    sender is a lie, and the person finds out when the recruiter does not
    reply.
    """
    from app.config import Settings

    name = (Settings().mail_provider or "console").strip().lower()
    return {
        "console": "console — renders the message and sends nothing",
        "gmail_api": ("gmail_api — sends through Gmail as the signed-in user, "
                      "once they connect it at the approval step"),
    }.get(name, name)


def _setup_state() -> dict[str, Any]:
    """Is this instance actually usable, and if not, what is missing.

    Every check answers with the command that fixes it. A setup problem that
    reports only the symptom costs a person twenty minutes of guessing; the
    fix is always one line and there is no reason not to print it.
    """
    import os

    from sqlalchemy import inspect

    from app.api.deps import DEV_EMAIL_ENV
    from app.config import Settings, check
    from app.db.session import engine

    state: dict[str, Any] = {}

    try:
        tables = set(inspect(engine()).get_table_names())
        missing = {"users", "resumes", "runs"} - tables
        state["database"] = (
            "ready" if not missing else
            f"not migrated — run `python -m alembic upgrade head` "
            f"(missing: {sorted(missing)})"
        )
    except Exception as exc:                              # noqa: BLE001
        state["database"] = f"unreachable: {type(exc).__name__}: {exc}"

    settings = Settings()

    signin: list[str] = []
    if not settings.google_client_id or not settings.google_client_secret:
        signin.append(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set — create an "
            "OAuth client (Web application) in the Google Cloud console and add "
            f"{settings.google_redirect_uri} as an authorised redirect URI")
    if not settings.jwt_secret:
        signin.append("JWT_SECRET is not set, so a session cookie cannot be "
                      "signed — run `python -m scripts.keys`")
    if not settings.fernet_key:
        signin.append("FERNET_KEY is not set, so a Gmail refresh token cannot "
                      "be stored encrypted (and will not be stored otherwise) "
                      "— run `python -m scripts.keys`")
    state["google_sign_in"] = "ready" if not signin else "; ".join(signin)

    if os.environ.get(DEV_EMAIL_ENV, "").strip():
        state["identity"] = (
            f"local development user ({DEV_EMAIL_ENV} is set). A real session "
            f"cookie takes precedence over it."
        )
    elif not signin:
        state["identity"] = "sign in at /api/auth/google/start"
    else:
        state["identity"] = (
            f"none — set {DEV_EMAIL_ENV} to work locally without Google. In "
            f'PowerShell that is `$env:{DEV_EMAIL_ENV} = "you@example.com"`, '
            f"not `set`, which sets a shell variable the process cannot see."
        )

    try:
        problems = check(Settings())
        state["model"] = "ready" if not problems else "; ".join(problems)
    except Exception as exc:                              # noqa: BLE001
        state["model"] = f"not configured: {exc}"

    return state


# How often the folder is checked for a newly written stage. A stage takes tens
# of seconds, so a second is responsive without being a busy loop.
# A run that has produced nothing for this long is not going to.


app = create_app()
