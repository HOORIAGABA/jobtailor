"""The deployment configuration, checked against the code it points at.

Every assertion here guards a failure that has exactly one symptom — a URL that
answers 404, or 500, on a deployment — and no earlier one. There is no type
checker for a string in a TOML file, the CI matrix does not deploy, and the
suite that proves the product works proves it against `create_app()` in process,
which is the one arrangement that cannot notice that Vercel is looking for the
app somewhere else.

So these tests read the config files as data and check them against the real
module tree. They are boring on purpose: a rename, a moved file or a hand-edited
JSON key turns one of them red locally, which is thirty seconds instead of a
deploy, a cold start and a guess.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.main import create_app

ROOT = Path(__file__).resolve().parent.parent


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _entrypoint() -> str:
    return _pyproject()["tool"]["vercel"]["entrypoint"]


# ── the entrypoint ────────────────────────────────────────────────────────


def test_the_vercel_entrypoint_names_a_real_fastapi_app() -> None:
    """`module:name` has to resolve, and the thing it resolves to has to be an app.

    Vercel finds a `FastAPI` instance named `app` by convention at a handful of
    filenames; ours is at `app/api/main.py`, deeper than any of them, so the
    entrypoint is declared explicitly and nothing but a deployment ever reads it.
    """
    module_path, _, attribute = _entrypoint().partition(":")
    assert module_path and attribute, (
        f"entrypoint {_entrypoint()!r} is not in `module.path:name` form")

    module = __import__(module_path, fromlist=[attribute])
    target = getattr(module, attribute, None)

    assert isinstance(target, FastAPI), (
        f"{_entrypoint()} is {type(target).__name__}, not a FastAPI instance. "
        f"Vercel imports exactly this and nothing else.")


def test_the_entrypoint_module_is_a_file_that_exists() -> None:
    """Vercel resolves the dotted path to a file and keys `functions` by it."""
    module_path, _, _ = _entrypoint().partition(":")
    expected = ROOT / (module_path.replace(".", "/") + ".py")
    assert expected.is_file(), f"{expected} does not exist"


def test_vercel_json_configures_the_resolved_entrypoint_file() -> None:
    """The `functions` key is a path, not a module — and it is easy to drift.

    A key that matches nothing is not an error on Vercel's side: the function is
    built with the defaults and the `maxDuration` written here is silently
    ignored, which is the kind of setting whose absence is only discovered by a
    request that was allowed to hang.
    """
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    module_path, _, _ = _entrypoint().partition(":")
    resolved = module_path.replace(".", "/") + ".py"

    assert list(config["functions"]) == [resolved], (
        f"vercel.json keys {list(config['functions'])}, "
        f"but the entrypoint resolves to {resolved}")

    # Hobby's ceiling is 300s and its default; this is deliberately far below it.
    # A read-only instance reads a row and returns bytes — anything that takes
    # longer is stuck, and 300 seconds of stuck is 300 seconds of billed wait.
    assert 5 <= config["functions"][resolved]["maxDuration"] <= 60


def test_there_is_no_project_table_in_pyproject() -> None:
    """A `[project]` table changes how Vercel installs dependencies.

    With one present it installs from `pyproject.toml`; the pinned set lives in
    `requirements.txt` (STACK.md D-17, one dependency list and no profile
    switch), so a `[project]` table added for some unrelated tooling reason would
    quietly deploy a different set of packages than the one that was tested.
    """
    assert "project" not in _pyproject()


# ── one origin ────────────────────────────────────────────────────────────


def test_the_ui_proxies_only_the_api_prefix() -> None:
    """The rewrite is what makes the session cookie first-party.

    A string check, because parsing JavaScript from pytest is not worth it — but
    deleting the rewrite is precisely the change someone makes while tidying, and
    its absence costs a deployment where sign-in appears to work and every
    request afterwards answers 401. The `/api/:path*` prefix matters too: a
    catch-all would proxy the pages as well.
    """
    config = (ROOT / "web" / "next.config.mjs").read_text(encoding="utf-8")
    assert "API_ORIGIN" in config
    assert '"/api/:path*"' in config
    assert '"/:path*"' not in config


# ── the shape a serverless instance actually has ──────────────────────────


def test_the_app_boots_with_no_runs_directory(tmp_path: Path) -> None:
    """A function's filesystem is read-only and starts empty.

    `RUNS_DIR` is a relative path that exists on a laptop and does not exist in a
    deployment bundle. Nothing may create it at import time — a `mkdir` in a
    constructor would turn every cold start into a 500 — and nothing may assume
    it is there.
    """
    missing = tmp_path / "no-such-runs-dir"
    app = create_app(missing, read_only=True)

    with TestClient(app) as client:
        assert client.get("/api/capabilities").status_code == 200

    assert not missing.exists(), (
        f"{missing} was created by importing the app; a deployment's filesystem "
        f"is read-only outside /tmp and a cold start would fail")


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_a_read_only_instance_refuses_every_unsafe_method(
    tmp_path: Path, method: str,
) -> None:
    """The property the public URL is deployed on, asserted at the edge.

    Tested elsewhere per endpoint; tested here as the blanket rule, because the
    endpoint that forgets will be the one added after those tests were written.
    """
    app = create_app(tmp_path / "runs", read_only=True)
    with TestClient(app) as client:
        response = client.request(method, "/api/runs")
    assert response.status_code == 403
