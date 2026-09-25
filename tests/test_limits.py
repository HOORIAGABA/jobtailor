"""Per-user caps on the endpoints that cost something.

What is being tested is the shape of the limiter, not a guess at the right
numbers — the counts in `api/limits.py` are sized for a person applying for
jobs and will change. The properties below should not.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.limits import Limit, RateLimiter, SEND, limiter
from app.api.main import create_app
from app.db import models
from app.db.session import create_all, engine, reset, session_scope

TINY = Limit(count=2, window=60, what="thing")


@pytest.fixture(autouse=True)
def clean():
    limiter.reset()
    yield
    limiter.reset()


# ══ the limiter itself ════════════════════════════════════════════════

def test_the_allowance_runs_out():
    rl = RateLimiter()
    rl.take("u1", TINY, now=0)
    rl.take("u1", TINY, now=1)
    with pytest.raises(HTTPException) as caught:
        rl.take("u1", TINY, now=2)
    assert caught.value.status_code == 429


def test_the_window_slides_rather_than_resetting_on_the_hour():
    """A fixed window lets someone spend the whole allowance at 11:59 and the
    whole next one at 12:01 — twice the intended rate across two minutes."""
    rl = RateLimiter()
    rl.take("u1", TINY, now=0)
    rl.take("u1", TINY, now=30)

    with pytest.raises(HTTPException):
        rl.take("u1", TINY, now=59)
    # The first event ages out at 60, not at some arbitrary boundary.
    rl.take("u1", TINY, now=61)
    with pytest.raises(HTTPException):
        rl.take("u1", TINY, now=62)


def test_one_persons_limit_is_not_another_persons():
    rl = RateLimiter()
    rl.take("u1", TINY, now=0)
    rl.take("u1", TINY, now=0)
    rl.take("u2", TINY, now=0)      # unaffected


def test_actions_are_counted_separately():
    rl = RateLimiter()
    other = Limit(count=2, window=60, what="other thing")
    rl.take("u1", TINY, now=0)
    rl.take("u1", TINY, now=0)
    rl.take("u1", other, now=0)     # a different allowance


def test_the_refusal_says_when_to_come_back():
    rl = RateLimiter()
    rl.take("u1", TINY, now=0)
    rl.take("u1", TINY, now=0)
    with pytest.raises(HTTPException) as caught:
        rl.take("u1", TINY, now=10)
    assert caught.value.headers["Retry-After"] == "50"
    assert "try again in" in caught.value.detail.lower()


def test_a_request_that_did_no_work_gives_its_token_back():
    """Otherwise a client with a broken retry loop locks a person out of their
    own account by failing repeatedly."""
    rl = RateLimiter()
    rl.take("u1", TINY, now=0)
    rl.give_back("u1", TINY)
    rl.take("u1", TINY, now=0)
    rl.take("u1", TINY, now=0)      # still two available, not one


def test_giving_back_what_was_never_taken_is_harmless():
    RateLimiter().give_back("nobody", TINY)


def test_the_limiter_is_thread_safe():
    """Two uvicorn threads racing on the same counter must not lose an event."""
    import threading

    rl = RateLimiter()
    big = Limit(count=1000, window=600, what="thing")
    def spend():
        for _ in range(100):
            rl.take("u1", big)

    threads = [threading.Thread(target=spend) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 800 taken, 200 left — and the 201st must fail.
    for _ in range(200):
        rl.take("u1", big)
    with pytest.raises(HTTPException):
        rl.take("u1", big)


# ══ over HTTP ═════════════════════════════════════════════════════════

@pytest.fixture
def api(tmp_path, monkeypatch) -> TestClient:
    url = f"sqlite+pysqlite:///{tmp_path / 'limits.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DEV_USER_EMAIL", "dev@example.com")
    monkeypatch.setenv("CONFIRM_TOKEN_SECRET", "a-long-development-secret")
    monkeypatch.setenv("ENVIRONMENT", "development")
    reset(url)
    create_all(engine())
    yield TestClient(create_app(tmp_path / "runs", read_only=False))
    reset()


def test_uploading_too_often_is_429_not_a_bill(api: TestClient, monkeypatch):
    from app.api import resumes as routes

    monkeypatch.setattr(routes, "UPLOAD", Limit(2, 3600, "resume upload"))
    files = {"file": ("cv.txt", b"Jane Doe\nExperience\n- did things",
                      "text/plain")}

    seen = [api.post("/api/resumes", files=files).status_code for _ in range(3)]
    assert seen[-1] == 429


def test_a_refused_send_does_not_spend_the_send_allowance(api: TestClient):
    """Nothing was dispatched, so nothing should have been counted."""
    with session_scope() as s:
        user = models.User(google_sub="dev-local", email="dev@example.com")
        s.add(user)
        s.flush()
        resume = models.Resume(user_id=user.id, filename="cv.pdf",
                               file_sha256="c" * 64, status="confirmed")
        s.add(resume)
        s.flush()
        run = models.Run(user_id=user.id, resume_id=resume.id,
                         status="needs_review")
        s.add(run)
        s.flush()
        run_id = run.id

    body = {"recipient": "a@b.com", "subject": "S", "body": "B",
            "confirm_token": "nonsense"}
    for _ in range(SEND.count + 3):
        # Every one of these is refused before dispatch (the run is not
        # approved), so the allowance should never run out.
        assert api.post(f"/api/runs/{run_id}/send", json=body).status_code == 409


def test_the_development_user_survives_a_failing_request(api: TestClient):
    """★ Found by the rate-limit test returning three different user ids.

    `current_user` created the dev user with a flush, and any request that
    raised rolled it back — so the next request made the user again with a new
    id, and everything keyed on the user id saw a different person every time.
    The limiter was silently defeated on exactly the endpoints most likely to
    fail.
    """
    files = {"file": ("cv.txt", b"too short", "text/plain")}
    for _ in range(3):
        api.post("/api/resumes", files=files)        # each one fails

    with session_scope() as s:
        assert s.query(models.User).count() == 1


def test_concurrent_first_requests_create_one_development_user(api: TestClient):
    """★ Found by loading the demo in a browser, not by a test.

    One page load fires several requests at once — /api/auth/me,
    /api/capabilities, the run — and on a cold database they all found no user,
    all inserted, and all but one got

        IntegrityError: UNIQUE constraint failed: users.google_sub

    as a 500. A test that makes one request at a time can never see it. The
    unique constraint was doing its job; losing the race just had to be treated
    as the ordinary outcome it is.
    """
    import concurrent.futures

    with session_scope() as s:
        s.query(models.User).delete()

    paths = ["/api/auth/me", "/api/capabilities", "/api/resumes",
             "/api/runs", "/api/auth/me", "/api/resumes"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(paths)) as pool:
        codes = list(pool.map(lambda p: api.get(p).status_code, paths))

    assert all(code == 200 for code in codes), codes
    with session_scope() as s:
        assert s.query(models.User).count() == 1
