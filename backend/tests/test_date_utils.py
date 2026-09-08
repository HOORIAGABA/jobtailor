import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agents.date_utils import parse_date_range, normalize_date_range


def test_year_range():
    r = parse_date_range("2022-2024")
    assert r["start"] == "2022" and r["end"] == "2024" and r["ongoing"] is False
    assert normalize_date_range("2022-2024") == "2022 - 2024"


def test_month_year_range():
    r = parse_date_range("06/2021 - 12/2022")
    assert r["start"] == "2021-06" and r["end"] == "2022-12" and r["ongoing"] is False


def test_month_name_range():
    r = parse_date_range("Jan 2022 - Present")
    assert r["start"] == "2022-01" and r["end"] == "" and r["ongoing"] is True
    assert normalize_date_range("Jan 2022 - Present") == "2022-01 - Present"


def test_bracketed_full_dates():
    r = parse_date_range("[01/03/2026 - 13/08/2026]")
    assert r["start"] == "2026-03-01" and r["end"] == "2026-08-13"
    assert r["ongoing"] is False


def test_single_year():
    r = parse_date_range("2023")
    assert r["start"] == "2023" and r["end"] == "" and r["ongoing"] is False


def test_dd_mon_yyyy_range():
    r = parse_date_range("10/06/2025 - 19/12/2025")
    # Ambiguous MM/DD vs DD/MM: we keep DD/MM (PK/EU convention)
    assert r["start"] == "2025-06-10" and r["end"] == "2025-12-19"


def test_since_form():
    r = parse_date_range("Since March 2023")
    assert r["start"] == "2023-03" and r["ongoing"] is True


def test_since_year_only():
    r = parse_date_range("Since 2023")
    assert r["start"] == "2023" and r["ongoing"] is True


def test_current_variant():
    r = parse_date_range("Aug 2024 - Current")
    assert r["start"] == "2024-08" and r["end"] == "" and r["ongoing"] is True


def test_garbage_passthrough():
    r = parse_date_range("hopefully soon")
    assert r["display"] == "hopefully soon"
    assert r["start"] == "" and r["end"] == ""


def test_empty():
    r = parse_date_range("")
    assert r["display"] == "" and r["start"] == "" and r["end"] == ""


def test_calibi_recent():
    # The exact Hooria resume format
    r = parse_date_range("[01/12/2025 - 28/02/2026]")
    assert r["start"] == "2025-12-01" and r["end"] == "2026-02-28"


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)