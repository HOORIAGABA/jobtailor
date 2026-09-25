"""Stage S0.1 — an uploaded file becomes text that preserves reading order.

The only thing this stage owes the rest of the system is **faithful order**. It
does not classify, structure or clean; it turns bytes into the lines a human
would read off the page, in the sequence they would read them.

Three readers, one entry point:

| Extension        | Reader        | The hard part                        |
|------------------|---------------|--------------------------------------|
| `.pdf`           | PyMuPDF       | column detection (`engine.layout`)   |
| `.docx`          | python-docx   | tables, which hold layout not data   |
| `.txt` / `.md`   | decode        | encoding                             |

**Why not `pymupdf4llm`.** The design document specified it, on the grounds
that its block-level layout analysis emits markdown with headings. Measured
against the two-column fixture it interleaves the columns exactly as
`pdfplumber` does — worse, in fact, since it also discards the line breaks that
made the damage visible. So the column work is done here, from box coordinates,
by `engine.layout`. One fewer dependency and a result that is tested rather
than assumed.

**Why tables matter in `.docx`.** `python-docx`'s `document.paragraphs` skips
everything inside a table, and a large share of resume templates are built as
an invisible two-column table. Reading only the paragraphs of one of those
returns almost nothing — and returns it without error, which is the dangerous
part. The body is walked element by element instead.
"""
from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath

from app.domain.errors import ExtractionEmpty, UnsupportedFileType
from app.engine.layout import TextBlock, blocks_to_text, reading_order

logger = logging.getLogger(__name__)

MAX_BYTES = 10 * 1024 * 1024
SUPPORTED = (".pdf", ".docx", ".txt", ".md")

# Under this many characters, a "successful" extraction is a scanned page whose
# only text layer is a stray watermark. Better to say so than to hand the
# parser three words and let it invent a resume around them.
MIN_USEFUL_CHARS = 120


def suffix_of(filename: str) -> str:
    return PurePosixPath(filename or "").suffix.lower()


def extract_text(data: bytes, filename: str) -> str:
    """Bytes in, ordered plain text out. Raises rather than returning junk."""
    if not data:
        raise ExtractionEmpty(f"{filename or 'The file'} is empty.")
    if len(data) > MAX_BYTES:
        raise UnsupportedFileType(
            f"{filename} is {len(data) / 1e6:.1f} MB; the limit is "
            f"{MAX_BYTES / 1e6:.0f} MB."
        )

    suffix = suffix_of(filename)
    if suffix == ".pdf":
        text = _from_pdf(data)
    elif suffix == ".docx":
        text = _from_docx(data)
    elif suffix in (".txt", ".md"):
        text = _from_plain(data)
    else:
        raise UnsupportedFileType(
            f"Cannot read {suffix or 'a file with no extension'}. "
            f"Supported: {', '.join(SUPPORTED)}."
        )

    text = _tidy(text)
    if len(text) < MIN_USEFUL_CHARS:
        raise ExtractionEmpty(
            f"Almost no text came out of {filename} "
            f"({len(text)} characters). If it is a scan or an exported image, "
            f"there is no text layer to read — please upload a text PDF or a "
            f".docx."
        )

    logger.info("Extracted %d characters from %s", len(text), filename)
    return text


# ── PDF ───────────────────────────────────────────────────────────────

def _from_pdf(data: bytes) -> str:
    """Per page: read the text boxes, order them, join.

    Ordering is per page on purpose. A gutter is a property of one page's
    layout — resumes routinely put a sidebar on page 1 and run full width on
    page 2, and a document-wide decision gets one of them wrong.
    """
    import pymupdf                       # imported here: heavy, and optional

    pages: list[str] = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.is_encrypted and not doc.authenticate(""):
            raise ExtractionEmpty(
                "This PDF is password-protected, so its text cannot be read. "
                "Please save an unprotected copy."
            )
        for page in doc:
            blocks = [
                TextBlock(x0=b[0], y0=b[1], x1=b[2], y1=b[3], text=b[4])
                for b in page.get_text("blocks")
                if isinstance(b[4], str) and b[4].strip()
            ]
            ordered = reading_order(blocks, page.rect.width)
            if ordered:
                pages.append(blocks_to_text(ordered))

    return "\n\n".join(pages)


# ── DOCX ──────────────────────────────────────────────────────────────

def _from_docx(data: bytes) -> str:
    """Walk the body in document order, descending into tables.

    A cell's paragraphs are joined with newlines and cells with a tab, so a
    table used for two-column layout comes out as readable lines rather than
    one run-on string.
    """
    import io

    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(io.BytesIO(data))
    body = document.element.body
    lines: list[str] = []

    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            text = Paragraph(child, document).text.strip()
            if text:
                lines.append(text)
        elif tag == "tbl":
            for row in Table(child, document).rows:
                cells = [
                    "\n".join(p.text.strip() for p in cell.paragraphs if p.text.strip())
                    for cell in row.cells
                ]
                # A single-cell row is a layout wrapper, not a real table row.
                joined = "\t".join(c for c in cells if c) if len(cells) > 1 else \
                    next((c for c in cells if c), "")
                if joined.strip():
                    lines.append(joined)

    return "\n".join(lines)


# ── plain text ────────────────────────────────────────────────────────

def _from_plain(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ── shared cleanup ────────────────────────────────────────────────────

_MD_LINK = re.compile(r"\[([^\]\n]*)\]\((?:[^)\s]+)\)")
_BULLET_START = re.compile(r"^[-*]\s+|^\d{1,2}[.)]\s+")
# A heading: no lowercase letters, or very short and title-shaped. Used only to
# stop a bullet from swallowing the heading that follows it.
_HEADINGISH = re.compile(r"^[^a-z]{3,}$")

# Zero-width and formatting characters. These are not whitespace to Python, so
# a line containing only one survives `" ".join(line.split())` as a non-empty
# line — which is why a resume came out with double blank lines where the
# blank-run collapse should have caught them.
_INVISIBLE = "​‌‍⁠­﻿᠎"


def strip_markdown_links(text: str) -> str:
    """`[label](url)` -> `label`. Keeps the visible text, drops the target.

    Some PDF readers emit markdown for link annotations. The target is almost
    always a URL-ified copy of the label on a resume ("www.example.com" linking
    to "https://www.example.com"), so keeping both doubles the text and gives
    the parser two candidate values for one field.
    """
    return _MD_LINK.sub(lambda m: m.group(1).strip() or "", text)


def unwrap_bullets(lines: list[str]) -> list[str]:
    """Rejoin a bullet that the PDF wrapped across several lines.

    A bullet in a PDF is one logical claim printed over two or three lines:

        - Designed and deployed a production multi-modal RGB-Thermal
        object-detection pipeline achieving 96.5% mAP@50, exported to
        ONNX and deployed on NVIDIA Jetson edge hardware.

    Left as three lines it becomes three chances for the parser to drop or
    mangle part of a sentence, and `engine.parse_check` compares line by line,
    so a correctly parsed single bullet would look like two missing lines.

    A bullet runs until the next bullet, a blank line, or a heading. Those
    three stops are what keep this from swallowing the section title that
    follows the last bullet of an entry.
    """
    out: list[str] = []
    in_bullet = False

    for line in lines:
        if not line:
            in_bullet = False
            out.append(line)
        elif _BULLET_START.match(line):
            in_bullet = True
            out.append(line)
        elif in_bullet and not _HEADINGISH.match(line):
            out[-1] = f"{out[-1]} {line}"
        else:
            in_bullet = False
            out.append(line)

    return out


def _tidy(text: str) -> str:
    """Normalise the characters and line structure PDFs are full of.

    Bullet glyphs become a plain hyphen so the parser sees one bullet marker
    instead of nine, and the ligatures PDF fonts use are expanded — `ﬁ` would
    otherwise make `workflow` unsearchable as `workﬂow`.
    """
    replacements = {
        " ": " ", " ": " ", " ": " ",
        "‘": "'", "’": "'", "“": '"', "”": '"',
        "–": "-", "—": "-", "−": "-",
        "ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff",
        "•": "-", "●": "-", "▪": "-", "·": "-",
        "◦": "-", "⁃": "-", "‣": "-", "∙": "-",
        **{c: "" for c in _INVISIBLE},
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)

    text = strip_markdown_links(text)

    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = unwrap_bullets(lines)

    out: list[str] = []
    blanks = 0
    for line in lines:
        if line:
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks == 1:          # collapse runs of blank lines to one
                out.append("")
    return "\n".join(out).strip()
