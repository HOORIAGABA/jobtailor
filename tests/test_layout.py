"""Reading order, tested with hand-written rectangles and no PDF at all.

That is the point of keeping the geometry in `engine/`: the two-column bug is
reproducible in four lines of coordinates, so the fix is pinned by a test that
runs in microseconds and cannot break because a PDF library changed.
"""
from __future__ import annotations

from app.engine.layout import (
    TextBlock,
    blocks_to_text,
    find_gutter,
    reading_order,
)

PAGE = 595.0          # A4 points


def block(x0, y0, x1, y1, text) -> TextBlock:
    return TextBlock(x0=x0, y0=y0, x1=x1, y1=y1, text=text)


def texts(blocks) -> list[str]:
    return [b.text for b in blocks]


# ── single column ─────────────────────────────────────────────────────

def test_a_single_column_page_has_no_gutter():
    blocks = [
        block(40, 40, 555, 60, "NAME"),
        block(40, 80, 555, 100, "EXPERIENCE"),
        block(40, 120, 555, 140, "Data Analyst"),
        block(40, 160, 555, 180, "- did a thing"),
    ]
    assert find_gutter(blocks, PAGE) is None


def test_a_single_column_page_reads_top_to_bottom():
    blocks = [
        block(40, 160, 555, 180, "third"),
        block(40, 40, 555, 60, "first"),
        block(40, 100, 555, 120, "second"),
    ]
    assert texts(reading_order(blocks, PAGE)) == ["first", "second", "third"]


# ── two columns: the bug this module exists for ───────────────────────

def two_column_page() -> list[TextBlock]:
    """A sidebar resume, blocks deliberately shuffled.

    Naive extractors sort by y and return
    "NAME / EXPERIENCE / SKILLS / Data Analyst / Python, SQL / - did a thing".
    """
    return [
        block(40, 40, 140, 60, "NAME"),
        block(220, 40, 555, 60, "EXPERIENCE"),
        block(40, 100, 140, 120, "SKILLS"),
        block(220, 100, 555, 120, "Data Analyst"),
        block(40, 140, 140, 160, "Python, SQL"),
        block(220, 140, 555, 160, "- did a thing"),
    ]


def test_two_columns_are_found():
    gutter = find_gutter(two_column_page(), PAGE)
    assert gutter is not None
    assert 140 < gutter < 220


def test_the_left_column_is_read_out_before_the_right():
    order = texts(reading_order(two_column_page(), PAGE))
    assert order == [
        "NAME", "SKILLS", "Python, SQL",
        "EXPERIENCE", "Data Analyst", "- did a thing",
    ]


def test_columns_are_not_interleaved():
    """The regression in one assertion: every left line precedes every right."""
    order = texts(reading_order(two_column_page(), PAGE))
    left = {"NAME", "SKILLS", "Python, SQL"}
    last_left = max(i for i, t in enumerate(order) if t in left)
    first_right = min(i for i, t in enumerate(order) if t not in left)
    assert last_left < first_right


# ── full-width blocks divide the page into bands ──────────────────────

def test_a_full_width_header_comes_first_and_does_not_break_the_split():
    blocks = [
        block(40, 20, 555, 40, "R. KHAN — full width header"),
        block(40, 60, 140, 80, "SKILLS"),
        block(220, 60, 555, 80, "EXPERIENCE"),
        block(40, 100, 140, 120, "Python"),
        block(220, 100, 555, 120, "Data Analyst"),
    ]
    assert texts(reading_order(blocks, PAGE)) == [
        "R. KHAN — full width header",
        "SKILLS", "Python",
        "EXPERIENCE", "Data Analyst",
    ]


def test_a_full_width_band_divider_separates_the_column_runs():
    """Content above a full-width heading must not be read with content below.

    Without banding, both left-column blocks would be emitted together and the
    footer's column would be attached to the wrong half of the page.
    """
    blocks = [
        block(40, 40, 140, 60, "left top"),
        block(220, 40, 555, 60, "right top"),
        block(40, 90, 555, 110, "=== PUBLICATIONS ==="),
        block(40, 140, 140, 160, "left bottom"),
        block(220, 140, 555, 160, "right bottom"),
    ]
    assert texts(reading_order(blocks, PAGE)) == [
        "left top", "right top",
        "=== PUBLICATIONS ===",
        "left bottom", "right bottom",
    ]


# ── things that must NOT be treated as a gutter ───────────────────────

def test_a_narrow_gap_is_not_a_gutter():
    """Two words with a few points between them are a line, not two columns."""
    blocks = [
        block(40, 40, 290, 60, "Data Analyst"),
        block(295, 40, 555, 60, "Jan 2022 - Present"),
        block(40, 80, 290, 100, "Intern"),
        block(295, 80, 555, 100, "2021"),
    ]
    assert find_gutter(blocks, PAGE) is None


def test_a_gap_at_the_margin_is_not_a_gutter():
    """A hanging indent creates a clear band near the left edge. Ignored."""
    blocks = [
        block(40, 40, 60, 60, "1."),
        block(120, 40, 555, 60, "first item"),
        block(40, 80, 60, 100, "2."),
        block(120, 80, 555, 100, "second item"),
    ]
    assert find_gutter(blocks, PAGE) is None


def test_too_few_blocks_to_judge():
    blocks = [
        block(40, 40, 140, 60, "a"),
        block(220, 40, 555, 60, "b"),
    ]
    assert find_gutter(blocks, PAGE) is None


def test_a_lonely_block_does_not_make_a_column():
    """One stray box on the right is a page number, not a second column."""
    blocks = [
        block(40, 40, 140, 60, "one"),
        block(40, 80, 140, 100, "two"),
        block(40, 120, 140, 140, "three"),
        block(520, 800, 555, 815, "2"),
    ]
    assert find_gutter(blocks, PAGE) is None


# ── joining ───────────────────────────────────────────────────────────

def test_blank_blocks_are_dropped():
    blocks = [
        block(40, 40, 555, 60, "kept"),
        block(40, 80, 555, 100, "   "),
        block(40, 120, 555, 140, "also kept"),
    ]
    assert texts(reading_order(blocks, PAGE)) == ["kept", "also kept"]


def test_blocks_to_text_separates_with_a_blank_line():
    assert blocks_to_text([
        block(0, 0, 10, 10, " a "),
        block(0, 20, 10, 30, "b"),
    ]) == "a\n\nb"


def test_an_empty_page_is_empty_not_an_error():
    assert reading_order([], PAGE) == []
