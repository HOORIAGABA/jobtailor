"""S1.0 — a posting arrives in whatever shape it arrives in.

The resume side had four stages before a model saw anything. The job side had
one line: read a file. These tests cover the missing half.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.errors import UnsupportedFileType, UserError
from app.engine.posting import (
    MIN_USABLE_CHARS,
    assess,
    prepare,
    strip_boilerplate,
)
from app.io.jobsource import html_to_text, load_job, looks_like_html

# A LinkedIn job page as a person actually copies it: the posting wrapped in
# navigation, engagement chrome and a footer.
LINKEDIN = """\
Skip to main content
Sign in
Join now

Annovasol
1,204 followers
2 days ago

We are hiring an AI Engineer!
Promoted

About the job
We are looking for an AI Engineer to join our team onsite in Islamabad.

What you will do
- Develop and integrate AI calling and voice agents
- Build and maintain FastAPI services

Requirements
- Strong proficiency in Python
- Experience with FastAPI and building REST APIs
- Hands-on experience with n8n and workflow automation

Easy Apply
See more
42 connections work here
Like · Comment · Share
© 2026 LinkedIn
Privacy Policy
"""

# The thing that is NOT a posting, and the reason this stage exists.
TEASER = """\
Annovasol
1,204 followers
2 days ago

We are hiring! Great team, great culture.
DM me if interested.

Like · Comment · Share
"""


# ── stripping page furniture ──────────────────────────────────────────

def test_feed_chrome_is_removed():
    cleaned, removed = strip_boilerplate(LINKEDIN)
    for gone in ("Sign in", "Easy Apply", "Like · Comment · Share",
                 "42 connections work here", "Privacy Policy"):
        assert gone not in cleaned
        assert gone in removed


def test_the_posting_itself_survives():
    cleaned, _ = strip_boilerplate(LINKEDIN)
    for kept in ("AI Engineer", "Strong proficiency in Python",
                 "Develop and integrate AI calling and voice agents",
                 "Hands-on experience with n8n"):
        assert kept in cleaned


def test_what_was_removed_is_reported_not_discarded():
    """A rule that eats half a posting has to be visible."""
    _, removed = strip_boilerplate(LINKEDIN)
    assert len(removed) >= 8


def test_a_requirement_containing_a_chrome_word_survives():
    """"Apply" appears in postings. Only a line that IS the button goes."""
    text = "Requirements\n- Apply statistical methods to noisy sensor data\nApply now"
    cleaned, removed = strip_boilerplate(text)
    assert "Apply statistical methods" in cleaned
    assert "Apply now" in removed


def test_a_clean_posting_loses_nothing():
    text = "Senior Engineer\n\nRequirements\n- Python\n- Airflow\n"
    cleaned, removed = strip_boilerplate(text)
    assert removed == []
    assert cleaned == text.strip()


# ── saying what is actually there ─────────────────────────────────────

def test_a_real_posting_is_usable():
    cleaned, report = prepare(LINKEDIN)
    assert report.is_usable
    assert report.names_a_role
    assert report.has_requirements
    assert report.has_responsibilities
    assert report.concerns == []


def test_a_hiring_announcement_is_not_a_posting():
    """The failure this stage exists to catch: four model calls spent on a
    post that never had anything to tailor against."""
    _, report = prepare(TEASER)
    assert not report.is_usable
    assert any("teaser" in c for c in report.concerns)
    assert any("no job title" in c for c in report.concerns)
    assert any("DM me" in c for c in report.concerns)


def test_requirements_without_the_work_is_flagged_but_usable():
    text = ("Data Engineer\n\nRequirements\n"
            + "- Strong proficiency in Python and SQL\n" * 8)
    _, report = prepare(text)
    assert report.is_usable
    assert any("no description of the work" in c for c in report.concerns)


def test_a_long_page_with_no_role_is_not_usable():
    text = "About us\n" + ("We value collaboration and curiosity. " * 30)
    _, report = prepare(text)
    assert not report.is_usable
    assert any("no job title" in c for c in report.concerns)


def test_length_alone_does_not_decide():
    """A compact posting that names a role and its requirements beats three
    pages of company boilerplate."""
    compact = (
        "Backend Engineer\n\nWhat you will do\n"
        "- Own the ingestion service and its on-call rotation\n"
        "- Move the nightly load off cron and onto a scheduler that "
        "recovers from a failed source without anyone watching\n\n"
        "Requirements\n- Strong proficiency in Python\n"
        "- Experience with Postgres and message queues\n"
    )
    _, report = prepare(compact)
    assert report.chars >= MIN_USABLE_CHARS
    assert report.is_usable


def test_the_summary_reads_as_a_sentence():
    assert "role" in prepare(LINKEDIN)[1].summary()
    assert prepare(TEASER)[1].summary()


def test_an_empty_input_is_not_usable():
    assert not assess("").is_usable


# ── getting the text in the first place ───────────────────────────────

HTML = """\
<!doctype html><html><head><title>Job</title>
<style>.a{color:red}</style>
<script>window.__DATA__ = {"tracking": "noise"};</script></head>
<body><nav>Sign in</nav>
<h1>AI Engineer</h1>
<div><p>We are looking for an AI Engineer.</p>
<h2>Requirements</h2><ul><li>Strong proficiency in Python</li>
<li>Experience with FastAPI &amp; REST APIs</li></ul></div>
</body></html>"""


def test_script_and_style_never_reach_the_prompt():
    """A naive stripper turns them into a wall of JavaScript."""
    text = html_to_text(HTML)
    assert "window.__DATA__" not in text
    assert "tracking" not in text
    assert "color:red" not in text


def test_html_becomes_readable_lines():
    text = html_to_text(HTML)
    assert "AI Engineer" in text
    assert "Strong proficiency in Python" in text
    # Block elements end a line rather than running together.
    assert "PythonExperience" not in text


def test_entities_are_decoded():
    assert "FastAPI & REST APIs" in html_to_text(HTML)


def test_html_is_recognised():
    assert looks_like_html(HTML)
    assert not looks_like_html("Requirements\n- Python\n- 5 < 10 years")


def test_pasted_html_is_stripped():
    assert "window.__DATA__" not in load_job(HTML)


def test_pasted_text_is_used_as_is():
    assert load_job("AI Engineer\nRequirements\n- Python") == \
        "AI Engineer\nRequirements\n- Python"


def test_a_text_file_is_read(tmp_path: Path):
    path = tmp_path / "job.txt"
    path.write_text(LINKEDIN, encoding="utf-8")
    assert "Strong proficiency in Python" in load_job(str(path), allow_paths=True)


def test_an_html_file_is_stripped(tmp_path: Path):
    path = tmp_path / "job.html"
    path.write_text(HTML, encoding="utf-8")
    text = load_job(str(path), allow_paths=True)
    assert "AI Engineer" in text and "window.__DATA__" not in text


def test_a_pdf_posting_uses_the_same_reader_as_a_resume(tmp_path: Path):
    """A posting saved as a PDF has the same two-column problem."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_textbox(pymupdf.Rect(40, 40, 555, 800), LINKEDIN,
                        fontsize=9, fontname="helv")
    path = tmp_path / "job.pdf"
    doc.save(path)
    doc.close()

    assert "Strong proficiency in Python" in load_job(str(path), allow_paths=True)


def test_a_url_is_refused_with_the_reason(tmp_path: Path):
    """v1 shipped httpx.get(user_url, follow_redirects=True) with no guard."""
    with pytest.raises(UserError) as excinfo:
        load_job("https://www.linkedin.com/jobs/view/123456")
    assert "SSRF" in str(excinfo.value)
    assert ".html" in str(excinfo.value)


def test_an_unreadable_format_is_refused(tmp_path: Path):
    path = tmp_path / "job.pages"
    path.write_bytes(b"x" * 100)
    with pytest.raises(UnsupportedFileType):
        load_job(str(path), allow_paths=True)


def test_an_empty_source_is_refused():
    with pytest.raises(UserError):
        load_job("   ")


def test_a_long_paste_is_not_mistaken_for_a_path():
    posting = "AI Engineer\n" + "Requirements: Python, FastAPI. " * 20
    assert load_job(posting) == posting.strip()


# ══ the file-reading boundary ═════════════════════════════════════════

def test_reading_a_server_file_is_off_by_default(tmp_path):
    """★ The path branch returns the contents of any file the process can open.

    On the command line that is the feature. Over HTTP it was arbitrary file
    disclosure, and the worst case was live: `PurePosixPath(".env").suffix` is
    `""`, which the reader treats as plain text, so `{"job": ".env"}` put
    LLM_API_KEY, JWT_SECRET, FERNET_KEY, CONFIRM_TOKEN_SECRET and the Google
    client secret into `run.jd_text` — served back by the stages endpoint to any
    signed-in user.
    """
    secrets = tmp_path / ".env"
    secrets.write_text("LLM_API_KEY=sk-real-key\nJWT_SECRET=hunter2\n", encoding="utf-8")

    with pytest.raises(UserError, match="does not read files from the server"):
        load_job(str(secrets))

    # And the opt-in still works, for the command line.
    assert "sk-real-key" in load_job(str(secrets), allow_paths=True)


def test_an_extensionless_file_is_the_dangerous_case(tmp_path):
    """No suffix means "plain text" to the reader, which is why dotfiles and
    `/etc/passwd`-shaped names were readable rather than refused."""
    target = tmp_path / "id_rsa"
    target.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
    with pytest.raises(UserError, match="does not read files from the server"):
        load_job(str(target))


def test_a_path_that_does_not_exist_is_treated_as_pasted_text():
    """The refusal is about reading files, not about text that looks pathish —
    a posting can legitimately contain a line like `/usr/bin/env`."""
    assert load_job("/no/such/file/anywhere.txt") == "/no/such/file/anywhere.txt"
