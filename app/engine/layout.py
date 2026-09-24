"""Reading order for a page of positioned text. Pure geometry — no PDF library.

**The two-column resume is the failure mode that matters.** A PDF stores text
as boxes at coordinates; it does not store "this is a sidebar". Every naive
extractor walks boxes roughly top-to-bottom and produces this:

    R. KHAN                  EXPERIENCE
    r.khan@example.com
    SKILLS                   Data Analyst
    Python, SQL              - Worked on data pipelines

    →  "R. KHAN EXPERIENCE r.khan@example.com SKILLS Data Analyst Python, SQL"

Nothing downstream recovers from that. The parser sees a skills line wedged
between a job title and its bullets, and the resume it produces is wrong in a
way the candidate will not notice until a recruiter does.

Measured on the fixture in `tests/fixtures/`, both `pdfplumber.extract_text`
and `pymupdf4llm.to_markdown` at default settings interleave the columns. This
module fixes it, and does so *without* a layout model: the geometry is already
in the box coordinates.

**The algorithm.**

1. Find the **gutter** — the widest vertical band that no text box crosses,
   with real content on both sides. A single-column page has no such band, and
   the answer is `None`.
2. Boxes that *do* cross the gutter are full-width — a name header, a horizontal
   rule, a footer. Each one **splits the page into bands**, because content
   above a full-width heading and content below it are not the same column run.
3. Within a band, read the left column top-to-bottom, then the right column.

Kept in `engine/` rather than beside the PDF reader for two reasons: it is pure
(boxes in, boxes out), so it is unit-tested with hand-written rectangles and no
PDF at all; and the layer rule then guarantees it cannot grow an I/O dependency
later.
"""
from __future__ import annotations

from dataclasses import dataclass

# A gutter narrower than this is more likely the space between two columns of a
# table, or the indent of a bullet, than a genuine page division.
MIN_GUTTER_WIDTH = 12.0

# Only search the middle of the page. A "gutter" found at 5% of the width is
# the left margin, not a column boundary.
SEARCH_LOW, SEARCH_HIGH = 0.25, 0.75

# Below this many boxes there is not enough evidence for a column split, and a
# short page (a one-line cover note) would otherwise split on its own margins.
MIN_BOXES = 4
MIN_BOXES_PER_COLUMN = 2

# A box wider than this fraction of the page is a full-width element — a name
# header, a section rule, a footer. It is excluded from gutter detection
# entirely, because it necessarily crosses every candidate boundary and would
# otherwise veto a split that plainly exists: a sidebar resume with the
# candidate's name across the top would be read as one column.
#
# Excluding them is safe for genuinely single-column pages. There, most boxes
# ARE full width, so removing them leaves too few to judge and the answer is
# `None` — which is the right answer for one column anyway.
FULL_WIDTH_RATIO = 0.6


@dataclass(frozen=True)
class TextBlock:
    """One positioned run of text. Origin top-left, y increasing downward."""
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    def crosses(self, x: float) -> bool:
        return self.x0 < x < self.x1

    def is_full_width(self, page_width: float) -> bool:
        return page_width > 0 and (self.x1 - self.x0) >= page_width * FULL_WIDTH_RATIO


def find_gutter(blocks: list[TextBlock], page_width: float) -> float | None:
    """The x of the widest clear vertical band, or `None` for one column.

    Candidates are the block edges themselves rather than a fixed scan step:
    a gutter boundary always lies between the right edge of one block and the
    left edge of another, so there is nothing in between worth testing.
    """
    if page_width <= 0:
        return None

    # Full-width elements are dividers, not evidence. See FULL_WIDTH_RATIO.
    columned = [b for b in blocks if not b.is_full_width(page_width)]
    if len(columned) < MIN_BOXES:
        return None

    low, high = page_width * SEARCH_LOW, page_width * SEARCH_HIGH
    edges = sorted({b.x0 for b in columned} | {b.x1 for b in columned})

    best: float | None = None
    best_width = 0.0

    for left_edge, right_edge in zip(edges, edges[1:]):
        mid = (left_edge + right_edge) / 2
        if not low <= mid <= high:
            continue
        if any(b.crosses(mid) for b in columned):
            continue

        left = [b for b in columned if b.x1 <= mid]
        right = [b for b in columned if b.x0 >= mid]
        if len(left) < MIN_BOXES_PER_COLUMN or len(right) < MIN_BOXES_PER_COLUMN:
            continue

        width = min(b.x0 for b in right) - max(b.x1 for b in left)
        if width >= MIN_GUTTER_WIDTH and width > best_width:
            best, best_width = mid, width

    return best


def reading_order(blocks: list[TextBlock], page_width: float) -> list[TextBlock]:
    """Sort blocks the way a person reads them.

    One column: plain top-to-bottom, left-to-right. Two columns: the left
    column is read out completely before the right one, within each band.
    """
    usable = [b for b in blocks if b.text.strip()]
    if not usable:
        return []

    gutter = find_gutter(usable, page_width)
    if gutter is None:
        return sorted(usable, key=lambda b: (round(b.y0, 1), b.x0))

    dividers = sorted((b for b in usable if b.crosses(gutter)), key=lambda b: b.y0)
    columned = [b for b in usable if not b.crosses(gutter)]

    ordered: list[TextBlock] = []
    band_top = float("-inf")

    # `None` closes the final band, below the last divider.
    for divider in [*dividers, None]:
        band_bottom = divider.y0 if divider is not None else float("inf")
        band = [b for b in columned if band_top <= b.y0 < band_bottom]
        # False sorts before True, so the left column comes out first.
        band.sort(key=lambda b: (b.x0 >= gutter, round(b.y0, 1), b.x0))
        ordered.extend(band)
        if divider is not None:
            ordered.append(divider)
            band_top = divider.y1

    return ordered


def blocks_to_text(blocks: list[TextBlock]) -> str:
    """Join ordered blocks into plain text, one blank line between blocks."""
    parts = [b.text.strip() for b in blocks if b.text.strip()]
    return "\n\n".join(parts)
