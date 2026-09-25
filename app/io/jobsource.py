"""Stage S1.0a — a job posting arrives in whatever shape it arrives in.

The original goal for this project said the input is "a job post (link, paste,
or browser-extension capture)". The code accepted a text file. This module is
the missing half: one entry point that takes what a person actually has and
returns text.

    a .txt or .md file          decode
    a .pdf or .docx             io.extract, the same reader resumes use
    a .html file or pasted HTML strip tags
    text pasted on the command line   use it as-is

**HTML is stripped with rules, not parsed.** A saved LinkedIn or Indeed page is
markup around a posting, and pulling the text out needs tag removal and entity
decoding, not a DOM. `<script>` and `<style>` go first — their contents are
text to a naive stripper and would otherwise arrive as a wall of JavaScript in
the model's prompt. This is not a browser: it will not run a page that builds
itself from JSON, and for those the answer is to copy the posting text, which
is what most people do anyway.

**A URL is deliberately not fetched here.** Doing it safely needs the SSRF
guard — scheme and host validation, a manual redirect loop, and IP checks at
every hop — because a URL that arrives from a user is an instruction to make a
request from wherever this code is running. v1 shipped
`httpx.get(user_url, follow_redirects=True)` with none of that, which on a
cloud host reaches the metadata endpoint and every internal service. That guard
belongs in the API layer with the rest of the request handling, and until it
exists this module says so rather than growing a quiet copy of the bug.
"""
from __future__ import annotations

import html as html_module
import logging
import re
from pathlib import Path, PurePosixPath

from app.domain.errors import UnsupportedFileType, UserError
from app.io.extract import extract_text

logger = logging.getLogger(__name__)

# Everything inside these is machine text, not posting text.
_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|head)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
# Tags that mean "a line ends here" rather than "text continues".
_BREAKS = re.compile(
    r"</?(br|p|div|li|tr|h[1-6]|section|article|ul|ol|table)\b[^>]*>",
    re.IGNORECASE,
)
_ANY_TAG = re.compile(r"<[^>]+>")
_LOOKS_LIKE_HTML = re.compile(r"<\s*(html|body|div|p|br|span|li)\b", re.IGNORECASE)

DOCUMENT_SUFFIXES = (".pdf", ".docx")
TEXT_SUFFIXES = (".txt", ".md")
HTML_SUFFIXES = (".html", ".htm")


def looks_like_html(text: str) -> bool:
    return bool(_LOOKS_LIKE_HTML.search(text or ""))


def html_to_text(markup: str) -> str:
    """Tags out, entities decoded, one line per block element.

    Order matters: script and style blocks are removed before any tag
    stripping, or their contents survive as text.
    """
    body = _DROP_BLOCKS.sub(" ", markup or "")
    body = _BREAKS.sub("\n", body)
    body = _ANY_TAG.sub(" ", body)
    body = html_module.unescape(body)

    lines = [" ".join(line.split()) for line in body.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def load_job(source: str, *, allow_paths: bool = False) -> str:
    """A path, a URL, pasted HTML or literal text — always text out.

    **`allow_paths` defaults to False, and that default is the security
    boundary.** The path branch reads any file the server process can open and
    returns its contents to the caller. On the command line that is the feature:
    the person running it owns the machine and is naming their own file. Over
    HTTP it is arbitrary file disclosure, and the worst case is not theoretical
    — `PurePosixPath(".env").suffix` is `""`, which the reader below treats as
    "plain text", so `{"job": ".env"}` returned `LLM_API_KEY`, `JWT_SECRET`,
    `FERNET_KEY`, `CONFIRM_TOKEN_SECRET` and the Google client secret into
    `run.jd_text`, from where `GET /api/runs/{id}/stages/posting` served them
    back. Any signed-in user could do it, including the local development
    identity.

    The opt-in is a keyword argument rather than a check inside the API layer so
    that the safe behaviour is what you get by forgetting. A new caller that
    does not think about this question gets the answer that cannot leak.
    """
    text = (source or "").strip()
    if not text:
        raise UserError("No job posting given.")

    if text.lower().startswith(("http://", "https://")):
        raise UserError(
            "Fetching a posting by URL is not built yet: doing it safely needs "
            "the SSRF guard that belongs in the API layer, and a URL from a "
            "user is a request made from wherever this runs.\n"
            "  For now, copy the posting text into a .txt file, or save the "
            "page and pass the .html."
        )

    if allow_paths:
        path = _as_path(text)
        if path is not None:
            return _from_file(path)
    elif _as_path(text) is not None:
        # Named an existing file and was not allowed to read it. Say so, rather
        # than silently treating "/etc/passwd" as the text of a job posting and
        # sending that to a model.
        raise UserError(
            "This looks like a file path, and this endpoint does not read "
            "files from the server. Paste the posting text instead."
        )

    # Not a path: the posting itself, pasted.
    return html_to_text(text) if looks_like_html(text) else text


def _as_path(text: str) -> Path | None:
    """A path only if it exists — a pasted posting is not a filename."""
    if len(text) > 260 or "\n" in text:
        return None
    try:
        candidate = Path(text)
        return candidate if candidate.exists() and candidate.is_file() else None
    except OSError:
        return None


def _from_file(path: Path) -> str:
    suffix = PurePosixPath(path.name).suffix.lower()
    data = path.read_bytes()

    if suffix in DOCUMENT_SUFFIXES:
        # The same reader the resume uses, column detection included: a
        # posting saved as a PDF has the same two-column problem.
        return extract_text(data, path.name)

    if suffix in HTML_SUFFIXES:
        return html_to_text(_decode(data))

    if suffix in TEXT_SUFFIXES or not suffix:
        body = _decode(data)
        return html_to_text(body) if looks_like_html(body) else body

    raise UnsupportedFileType(
        f"Cannot read a job posting from {suffix!r}. Supported: "
        f"{', '.join(TEXT_SUFFIXES + HTML_SUFFIXES + DOCUMENT_SUFFIXES)}."
    )


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")
