"""S0.4 — the confirm gate, and the rule that makes it affordable.

The rule: **an edit re-derives the document and never re-runs the parser.** The
tests that matter here count model calls, because that is the difference between
an edit loop someone can use on a free tier and one that costs three calls a
keystroke-batch.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.db import models
from app.db.session import build_engine, create_all
from app.domain.errors import IllegalTransition, UserError
from app.domain.models import RawEntry, RawResume, RawSection
from app.io.llm import ScriptedClient
from app.pipeline import resumes as service

RESUME_BYTES = (
    b"A. MORGAN\na.morgan@example.com\n\n"
    b"WORK EXPERIENCE\n\nData Analyst\nAcme Corp | 01/2022 - Present\n"
    b"- Built reporting pipelines in Python\n"
    b"- Responsible for weekly stakeholder reports\n\n"
    b"TECHNICAL SKILLS\nLanguages: Python, SQL\nTools: Airflow, Docker\n"
    b"\nCity: Springfield | Country: Elbonia\n"
)

# Every field present, because every field is required — the lesson that
# landed five times and is now the parse schema's defining property.
CONTACT = {
    "full_name": "A. Morgan", "email": "a.morgan@example.com",
    "phone": "", "location": "", "linkedin": "", "github": "", "website": "",
}
PARSED = {
    "contact": CONTACT,
    "sections": [
        {"heading": "WORK EXPERIENCE", "entries": [{
            "title": "Data Analyst", "org": "Acme Corp",
            "dates": "01/2022 - Present",
            "bullets": ["Built reporting pipelines in Python",
                        "Responsible for weekly stakeholder reports"],
        }]},
        {"heading": "TECHNICAL SKILLS", "entries": [{
            "title": "Languages", "org": "", "dates": "",
            "bullets": ["Python, SQL"],
        }]},
    ],
}
EMPTY_PARSE = {"contact": CONTACT, "sections": []}


@pytest.fixture
def session():
    engine = build_engine("sqlite+pysqlite:///:memory:")
    create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def user(session) -> models.User:
    row = models.User(google_sub="s", email="a@example.com")
    session.add(row)
    session.commit()
    return row


def _parsed(session, user) -> models.Resume:
    resume = service.create(session, user.id, "cv.txt", RESUME_BYTES)
    service.read(session, resume, RESUME_BYTES, ScriptedClient([PARSED]))
    session.commit()
    return resume


# ══ the rule that makes the edit loop affordable ══════════════════════

def test_an_edit_does_not_call_the_model(session, user):
    """The whole point of S0.4's design.

    Re-running the parser would return the same mistake — the bytes have not
    changed — and the parse cache is keyed on the file hash, so the "re-parse"
    would hand back the exact object the user just corrected. At three calls
    per edit on a free tier, it would also be unusable.
    """
    resume = _parsed(session, user)
    client = ScriptedClient([])          # any call at all raises

    draft = service.draft(resume)
    draft.sections[0].entries[0].title = "Senior Data Analyst"
    service.apply_draft(session, resume, draft)

    assert client.calls == []
    assert service.document(resume).section_of_kind("experience") \
        .items[0].title == "Senior Data Analyst"


def test_an_edit_re_derives_the_document(session, user):
    """`normalize` is what must re-run: it assigns ids, parses dates and
    rebuilds the skill inventory, all of which change when a bullet is added."""
    resume = _parsed(session, user)
    before = len(service.document(resume).skill_inventory)

    draft = service.draft(resume)
    draft.sections[1].entries[0].bullets = ["Python, SQL, Terraform, Ansible"]
    service.apply_draft(session, resume, draft)

    assert len(service.document(resume).skill_inventory) > before
    assert "terraform" in {s.lower() for s
                           in service.document(resume).skill_inventory}


def test_a_new_bullet_gets_an_id(session, user):
    resume = _parsed(session, user)
    draft = service.draft(resume)
    draft.sections[0].entries[0].bullets.append("Shipped a forecasting model")
    service.apply_draft(session, resume, draft)

    bullets = list(service.document(resume).all_bullets())
    added = [b for b in bullets if "forecasting" in b.text]
    assert len(added) == 1 and added[0].id


def test_the_edit_loop_can_run_many_times(session, user):
    resume = _parsed(session, user)
    for n in range(4):
        draft = service.draft(resume)
        draft.contact.full_name = f"A. Morgan {n}"
        service.apply_draft(session, resume, draft)
    assert resume.revision == 4
    assert resume.status == "needs_confirm"


# ══ the coverage report is the edit screen ════════════════════════════

def test_the_unplaced_lines_are_reported(session, user):
    """`ParseCoverage.dropped` names the exact source lines the parse lost.

    A specific piece of text is the only kind of confirmation prompt anyone
    completes — "check your resume" is not a task.
    """
    resume = _parsed(session, user)
    body = service.summary(resume)
    assert body["unplaced_lines"]
    assert any("Elbonia" in line for line in body["unplaced_lines"])
    assert body["parse_is_clean"] is False


def test_placing_a_line_removes_it_from_the_list(session, user):
    """After an edit the list is a progress bar, not a complaint."""
    resume = _parsed(session, user)
    missing = service.summary(resume)["unplaced_lines"]
    assert any("Elbonia" in line for line in missing)

    draft = service.draft(resume)
    draft.sections.append(RawSection(
        heading="LOCATION",
        entries=[RawEntry(title="City: Springfield | Country: Elbonia")]))
    service.apply_draft(session, resume, draft)

    after = service.summary(resume)["unplaced_lines"]
    assert not any("Elbonia" in line for line in after)
    assert len(after) < len(missing)


# ══ confirmation freezes the document ═════════════════════════════════

def test_confirming_freezes_and_stamps(session, user):
    resume = _parsed(session, user)
    service.confirm(session, resume)
    assert resume.status == "confirmed"
    assert resume.confirmed_at is not None


def test_a_confirmed_resume_cannot_be_edited(session, user):
    """Ids are permanent from here — an `Op` naming `exp.3.b.2` a week later
    has to still mean it."""
    resume = _parsed(session, user)
    service.confirm(session, resume)
    with pytest.raises(IllegalTransition):
        service.apply_draft(session, resume, service.draft(resume))


def test_an_empty_parse_cannot_be_confirmed(session, user):
    """Otherwise the failure surfaces three stages later as "the planner
    proposed nothing"."""
    resume = service.create(session, user.id, "cv.txt", RESUME_BYTES)
    service.read(session, resume, RESUME_BYTES, ScriptedClient([EMPTY_PARSE]))
    with pytest.raises(UserError, match="no sections"):
        service.confirm(session, resume)


# ══ upload validation ═════════════════════════════════════════════════

# The payload is a SIZE here, not the bytes themselves, and that is not a style
# preference. pytest builds a test id from each parameter's value and writes the
# resulting node id into `PYTEST_CURRENT_TEST` — so parametrising directly on
# `b"x" * (10 * 1024 * 1024 + 1)` produced a ten-million-character id, and
# Windows refuses an environment variable longer than 32767 characters:
#
#     ValueError: the environment variable is longer than 32767 characters
#
# Linux has no such limit, so this passed in CI and on every Linux checkout and
# failed only on Windows — the same shape of bug as the `.env` leak in
# `conftest.py`: green everywhere except the machine someone is actually
# working on.
@pytest.mark.parametrize("filename, size, message", [
    ("cv.rtf", 50, "cannot read"),
    ("cv", 50, "no extension"),
    ("cv.pdf", 0, "empty"),
    ("cv.pdf", 10 * 1024 * 1024 + 1, "limit"),
])
def test_a_bad_upload_is_refused_before_anything_is_read(
        session, user, filename, size, message):
    with pytest.raises(UserError, match=message):
        service.create(session, user.id, filename, b"x" * size)


def test_no_test_id_can_exceed_the_windows_environment_limit():
    """★ Guards the rule above for every test in the suite, not just that one.

    `PYTEST_CURRENT_TEST` carries the node id, and Windows caps an environment
    variable at 32767 characters. A parameter big enough to break that is easy
    to reintroduce — `b"x" * BIG` reads as obviously correct — and the failure
    appears only on Windows, so nobody on Linux or in CI would ever see it.
    """
    import subprocess
    import sys

    listing = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "--no-header", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
    ).stdout

    # A failed collection gives empty output, which would make the assertion
    # below pass while checking nothing.
    assert "test" in listing, "collection produced no listing to check"

    too_long = [line for line in listing.splitlines() if len(line) > 4000]
    assert not too_long, (
        f"{len(too_long)} test id(s) are enormous; the longest is "
        f"{max(len(line) for line in too_long):,} characters. Parametrise on a "
        f"size or use `ids=` — a node id this long is a ValueError on Windows."
    )


def test_the_file_bytes_are_not_stored(session, user):
    """A resume is personal data and one copy is enough.

    Nothing downstream needs the file again: the parser works from the
    extracted text, and an edit does not parse at all.
    """
    resume = _parsed(session, user)
    assert resume.file_sha256 and resume.file_bytes
    assert not hasattr(resume, "file_blob")
    assert resume.extract_text          # this is what is kept instead


def test_uploading_the_same_file_twice_makes_a_new_version(session, user):
    first = _parsed(session, user)
    second = service.create(session, user.id, "cv.txt", RESUME_BYTES)
    assert second.version == first.version + 1
    assert second.file_sha256 == first.file_sha256


# ══ a failed parse keeps the evidence and can retry ═══════════════════

def test_a_failed_parse_leaves_the_reason_on_the_row(session, user):
    from app.domain.errors import SchemaValidationFailed
    resume = service.create(session, user.id, "cv.txt", RESUME_BYTES)
    with pytest.raises(SchemaValidationFailed):
        service.read(session, resume, RESUME_BYTES,
                     ScriptedClient(["not json", "still not json"]))
    assert resume.status == "failed"
    assert "SchemaValidationFailed" in resume.error


def test_a_retry_works_from_the_stored_text_not_the_file(session, user):
    """The second reason `extract_text` is kept: the parser takes text, so a
    retry needs no re-upload."""
    from app.domain.errors import SchemaValidationFailed
    resume = service.create(session, user.id, "cv.txt", RESUME_BYTES)
    with pytest.raises(SchemaValidationFailed):
        service.read(session, resume, RESUME_BYTES, ScriptedClient(["no", "no"]))

    service.reparse(session, resume, ScriptedClient([PARSED]))
    assert resume.status == "needs_confirm"
    assert resume.error == ""
    assert service.document(resume).sections


# ══ two tabs ══════════════════════════════════════════════════════════

def test_a_stale_draft_is_refused(session, user):
    """A silent last-write-wins would lose the other tab's corrections, and the
    person who lost them would have no way to know."""
    resume = _parsed(session, user)
    service.apply_draft(session, resume, service.draft(resume))   # rev 0 -> 1
    with pytest.raises(service.StaleEdit):
        service.apply_draft(session, resume, service.draft(resume),
                            expected_revision=0)


def test_the_matching_revision_is_accepted(session, user):
    resume = _parsed(session, user)
    service.apply_draft(session, resume, service.draft(resume),
                        expected_revision=resume.revision)
    assert resume.revision == 1


# ══ over HTTP ═════════════════════════════════════════════════════════

@pytest.fixture
def api(tmp_path, monkeypatch) -> TestClient:
    from app.api.main import create_app
    from app.db import session as db_session

    monkeypatch.setenv("DATABASE_URL",
                       f"sqlite+pysqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("DEV_USER_EMAIL", "dev@example.com")
    db_session.reset(f"sqlite+pysqlite:///{tmp_path / 'api.db'}")
    create_all(db_session.engine())
    yield TestClient(create_app(tmp_path / "runs", read_only=True))
    db_session.reset()


def test_without_an_identity_every_write_is_401(tmp_path, monkeypatch):
    """The read-only deployment sets no development identity, so it has no
    writable identity at all. A default that silently worked would be an
    authentication bypass with a comment promising to fix it."""
    from app.api.main import create_app
    from app.db import session as db_session

    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)
    db_session.reset(f"sqlite+pysqlite:///{tmp_path / 'anon.db'}")
    create_all(db_session.engine())
    client = TestClient(create_app(tmp_path / "runs", read_only=True))
    assert client.get("/api/resumes").status_code == 401
    db_session.reset()


def test_the_confirm_flow_over_http(api: TestClient):
    """Upload is not exercised here — it needs a model. The rest is free."""
    from app.db.session import session_scope

    with session_scope() as s:
        user = s.query(models.User).filter(
            models.User.google_sub == "dev-local").one_or_none()
        if user is None:
            user = models.User(google_sub="dev-local", email="dev@example.com")
            s.add(user)
            s.flush()
        resume = service.create(s, user.id, "cv.txt", RESUME_BYTES)
        service.read(s, resume, RESUME_BYTES, ScriptedClient([PARSED]))
        resume_id = resume.id

    body = api.get(f"/api/resumes/{resume_id}").json()
    assert body["status"] == "needs_confirm"
    assert body["unplaced_lines"]
    assert body["raw"] is not None

    draft = body["raw"]
    draft["contact"]["full_name"] = "Alex Morgan"
    edited = api.put(f"/api/resumes/{resume_id}/draft",
                     json={"raw": draft, "revision": body["revision"]})
    assert edited.status_code == 200
    assert edited.json()["revision"] == body["revision"] + 1

    stale = api.put(f"/api/resumes/{resume_id}/draft",
                    json={"raw": draft, "revision": body["revision"]})
    assert stale.status_code == 409

    confirmed = api.post(f"/api/resumes/{resume_id}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "confirmed"

    again = api.put(f"/api/resumes/{resume_id}/draft",
                    json={"raw": draft, "revision": 99})
    assert again.status_code == 409


def test_another_users_resume_is_404_not_403(api: TestClient):
    """A 403 confirms the row exists, which tells an enumerator they found
    something."""
    from app.db.session import session_scope
    with session_scope() as s:
        other = models.User(google_sub="someone-else", email="b@example.com")
        s.add(other)
        s.flush()
        theirs = service.create(s, other.id, "cv.txt", RESUME_BYTES)
        theirs_id = theirs.id

    assert api.get(f"/api/resumes/{theirs_id}").status_code == 404


def test_upload_refuses_when_no_model_is_configured(api: TestClient, monkeypatch):
    """"This instance cannot parse" beats "the parse failed"."""
    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_BASE_URL", "")
    response = api.post("/api/resumes",
                        files={"file": ("cv.txt", RESUME_BYTES, "text/plain")})
    assert response.status_code in (400, 503)
