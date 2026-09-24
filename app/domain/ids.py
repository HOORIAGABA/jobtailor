"""Stable identifiers for every addressable part of a resume.

Every edit the system makes targets an id. The validator checks provenance by
id, the diff attributes changes by id, and the UI links a change back to its
source line by id. So ids must be **stable**: assigned once and never
recomputed.

They are deliberately NOT positional. If `exp.1` were derived from position,
inserting an entry would shift every later id and silently redirect any stored
operation that referenced them. Instead each new entity takes the next value
from a monotonic counter stored on the document itself.
"""
from __future__ import annotations

from typing import Final

SECTION_PREFIX: Final[str] = "sec"

# Short, readable prefixes keep ids legible in logs and in the diff UI.
KIND_PREFIX: Final[dict[str, str]] = {
    "summary": "sum",
    "skills": "skl",
    "experience": "exp",
    "projects": "prj",
    "education": "edu",
    "certifications": "crt",
    "leadership": "ldr",
    "custom": "cst",
}

BULLET_INFIX: Final[str] = "b"


def section_id(kind: str, seq: int | None = None) -> str:
    """`sec.experience`, or `sec.custom.3` when several sections share a kind."""
    base = f"{SECTION_PREFIX}.{kind}"
    return base if seq is None else f"{base}.{seq}"


def item_id(kind: str, seq: int) -> str:
    """`exp.1`, `prj.2`."""
    return f"{KIND_PREFIX.get(kind, 'cst')}.{seq}"


def bullet_id(owner_item_id: str, seq: int) -> str:
    """`exp.1.b.4` — always derived from its item, so lineage is readable."""
    return f"{owner_item_id}.{BULLET_INFIX}.{seq}"


def item_id_of_bullet(bid: str) -> str:
    """`exp.1.b.4` -> `exp.1`. Raises on a malformed bullet id."""
    marker = f".{BULLET_INFIX}."
    if marker not in bid:
        raise ValueError(f"not a bullet id: {bid!r}")
    return bid.rsplit(marker, 1)[0]


def is_bullet_id(value: str) -> bool:
    return f".{BULLET_INFIX}." in value
