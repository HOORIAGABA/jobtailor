"""M5.3 - Date & duration normalization.

Resumes use a huge range of date formats (`[01/03/2026 - 13/08/2026]`,
`Jan 2022 - Present`, `2022-2024`, `Since March 2023`, `2023`). Downstream
logic (tailoring relevance reordering, outreach year-estimation, renderer)
depends on parseable date spans, so we collapse them into:

  display  - a canonical string for rendering
  start    - ISO date 'YYYY-MM-DD' (or 'YYYY-MM' / 'YYYY' granularity)
  end      - ISO date or "" for ongoing/unknown
  ongoing  - bool: True when end is 'Present'/'Current'/not present

Unknown/invalid input never raises: it passes through unchanged with empty
parsed fields so the caller can fall back to the original string.
"""
import re

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

_ONGOING = re.compile(r"\b(present|current|ongoing|now|today|to\s*date)\b", re.IGNORECASE)
_YEAR = r"(20\d{2}|19\d{2})"
_MONTH_NAME = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_MONTH_NUM = r"(?:0?[1-9]|1[0-2])"


def _month_num(name: str) -> int:
    return MONTHS.get(name.lower().rstrip(".").strip()[:3], 0)


def _iso(y: int, m: int | None = None, d: int | None = None) -> str:
    if y and m and d:
        return "%04d-%02d-%02d" % (y, m, d)
    if y and m:
        return "%04d-%02d" % (y, m)
    return "%04d" % y


def _parse_single(s: str):
    """Parse ONE date token (no range) -> (start_ymd, ongoing).

    ymd is (year, month|None, day|None); ongoing indicates 'Present'-like."""
    s = s.strip()
    if _ONGOING.search(s):
        return (None, True)

    # YYYY
    m = re.match(rf"^{_YEAR}$", s)
    if m:
        return ((int(m.group(1)), None, None), False)

    # MM/YYYY or M/YYYY
    m = re.match(rf"^({_MONTH_NUM})/({_YEAR})$", s)
    if m:
        return ((int(m.group(2)), int(m.group(1)), None), False)

    # Mon YYYY   (also 'Mar. 2023', 'March 2023')
    m = re.match(rf"^({_MONTH_NAME})\.?\s+({_YEAR})$", s, re.IGNORECASE)
    if m:
        return ((int(m.group(2)), _month_num(m.group(1)), None), False)

    # DD/MM/YYYY or D/M/YYYY
    m = re.match(rf"^(\d{{1,2}})/(\d{{1,2}})/({_YEAR})$", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return ((y, mo, d), False)

    # DD Mon YYYY
    m = re.match(rf"^(\d{{1,2}})\s+({_MONTH_NAME})\.?\s+({_YEAR})$", s, re.IGNORECASE)
    if m:
        return ((int(m.group(3)), _month_num(m.group(2)), int(m.group(1))), False)

    # Since Mon YYYY / Since YYYY
    m = re.match(rf"^since\s+(?:({_MONTH_NAME})\.?\s+)?({_YEAR})$", s, re.IGNORECASE)
    if m:
        if m.group(1):
            ym = (int(m.group(2)), _month_num(m.group(1)), None)
        else:
            ym = (int(m.group(2)), None, None)
        return (ym, True)

    return (None, False)


def parse_date_range(raw: str) -> dict:
    """Parse a resume date-range string.

    Returns {"display": str, "start": str, "end": str, "ongoing": bool}.
    `start`/`end` are ISO ('YYYY' / 'YYYY-MM' / 'YYYY-MM-DD'); `end` is ""
    when the range is ongoing or the end is unknown. `display` is the original
    string cleaned of bracket noise (never raises on garbage input)."""
    raw = (raw or "").strip().strip("[]()").strip()
    if not raw:
        return {"display": "", "start": "", "end": "", "ongoing": False}

    # Split on range dashes only (not '/', which is a day/month separator).
    parts = re.split(r"\s*(?:-|–|—)\s*", raw)
    if len(parts) == 1:
        start_ymd, ongoing = _parse_single(parts[0])
        return {
            "display": parts[0],
            "start": _iso(*start_ymd) if start_ymd else "",
            "end": "",
            "ongoing": ongoing,
        }

    left = parts[0].strip()
    right = parts[1].strip()

    # Special case 'YYYY - MM/YYYY' etc.: right may itself be a full range-ish.
    start_ymd, _ = _parse_single(left)
    end_ymd, end_ongoing = _parse_single(right)
    end = (_iso(*end_ymd) if end_ymd else "") or ("" if end_ongoing else "")

    return {
        "display": f"{left} - {'Present' if end_ongoing else right}",
        "start": _iso(*start_ymd) if start_ymd else "",
        "end": end,
        "ongoing": bool(end_ongoing) or (not end and not end_ongoing and not start_ymd),
    }


def normalize_date_range(raw: str) -> str:
    """Entry point (M5.3): return a canonical display string.

    Preserves the original wording when normalization is ambiguous."""
    parsed = parse_date_range(raw)
    if parsed["start"] and parsed["end"]:
        return f"{parsed['start']} - {parsed['end']}"
    if parsed["start"] and parsed["ongoing"]:
        return f"{parsed['start']} - Present"
    return parsed["display"] or ""