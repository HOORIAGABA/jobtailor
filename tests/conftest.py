"""Test isolation.

One rule, applied everywhere: **a test never reads the developer's `.env`.**

`Settings()` loads `.env` by design, so a test that constructs one picks up
whatever the person running it happens to have configured. The suite then
passes on a clean checkout, passes in CI — where there is no `.env` — and fails
only on the machine of someone who has actually configured the project. Which
is the worst possible distribution of a failure: it appears to be caused by
setting the project up correctly.

That happened here. `test_config_requires_a_base_url_for_non_google_providers`
asserts that a non-Google provider without `LLM_BASE_URL` is reported as a
configuration problem. With a real `.env` present, `Settings(llm_provider=...)`
inherited a perfectly good base URL from it, no problem was reported, and the
test failed — on the only machines where the project was usable.

The fix is a rule rather than an argument at each call site, because the next
test to construct `Settings` would otherwise reintroduce it.
"""
from __future__ import annotations

import pytest

from app.config import Settings

# Every environment variable the app reads. Cleared for the duration of the
# suite so a shell export cannot leak in either — `.env` is the common case,
# but `$LLM_API_KEY` in a developer's profile is the same bug.
_APP_ENV_VARS = tuple(
    name.upper() for name in Settings.model_fields
)


@pytest.fixture(autouse=True, scope="session")
def _ignore_dotenv() -> None:
    """Detach `Settings` from `.env` for the whole test session."""
    Settings.model_config["env_file"] = None


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove app variables from the process environment for each test."""
    for name in _APP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def settings_from_env(monkeypatch: pytest.MonkeyPatch):
    """Build `Settings` from explicit variables, for tests that want them set.

        def test_x(settings_from_env):
            s = settings_from_env(LLM_PROVIDER="groq", LLM_API_KEY="k")
    """
    def build(**env: str) -> Settings:
        for key, value in env.items():
            monkeypatch.setenv(key.upper(), value)
        return Settings()

    return build
