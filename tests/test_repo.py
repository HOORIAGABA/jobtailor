"""The repository itself: is everything that should ship actually shipping?

This file exists because of a silent failure that no other check could catch.
`.gitignore` contained `runs/` — unanchored, so git matched a directory of that
name at ANY depth — and it swallowed `web/app/runs/`, the Next.js route holding
the progress, gate and send screens. 137 files were committed and the two most
important pages in the product were not among them.

**Nothing failed.** `next build` builds an app with fewer routes perfectly
happily; the test suite does not read the frontend; CI was green. The only
symptom would have been a 404 on the deployed site, at the one screen the whole
product exists to show.

So the guard is not "do these two files exist" — it is "is any source file
invisible to git", which catches the next version of this too.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Where real source lives. Deliberately not the whole tree: `runs/`, `samples/`
# and the databases are ignored on purpose and must stay that way.
SOURCE_DIRS = ("app", "tests", "scripts", "evals", "migrations", "web/app",
               "web/components", "web/lib")

SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".css", ".json", ".mjs", ".yml"}


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True)


@pytest.fixture(scope="module")
def in_a_git_checkout() -> bool:
    try:
        done = _git("rev-parse", "--is-inside-work-tree")
    except FileNotFoundError:                        # pragma: no cover
        pytest.skip("git is not installed")
    if done.returncode != 0:
        pytest.skip("not a git checkout")
    return True


def test_no_source_file_is_invisible_to_git(in_a_git_checkout):
    """★ The check that would have caught the missing gate screen.

    `git check-ignore` answers the only question that matters: would `git add`
    skip this file? Asking git directly beats reimplementing its pattern rules,
    which is exactly where the original bug came from — a leading slash means
    "repo root only" and its absence means "any depth", and the difference is
    invisible until something vanishes.
    """
    candidates = [
        path for directory in SOURCE_DIRS
        for path in (ROOT / directory).rglob("*")
        if path.is_file()
        and path.suffix in SOURCE_SUFFIXES
        and "node_modules" not in path.parts
        and "__pycache__" not in path.parts
        and ".next" not in path.parts
    ]
    assert candidates, "found no source files to check — the paths are wrong"

    relative = [str(p.relative_to(ROOT)).replace("\\", "/") for p in candidates]
    # `check-ignore` exits 0 when it ignored something, 1 when it ignored
    # nothing. 1 is the pass.
    done = _git("check-ignore", "--no-index", *relative)
    ignored = [line for line in done.stdout.splitlines() if line.strip()]

    assert not ignored, (
        "these source files are git-ignored and would never be committed:\n  "
        + "\n  ".join(ignored)
        + "\n\nA directory rule in .gitignore is probably missing its leading "
          "slash: `runs/` matches at any depth, `/runs/` matches the repo root "
          "only."
    )


def test_the_frontend_routes_are_all_present():
    """Named explicitly as well, because these four ARE the product.

    The general check above is the one that generalises; this one makes the
    failure message say which screen is missing instead of listing a path.
    """
    screens = {
        "web/app/page.tsx": "the dashboard",
        "web/app/resumes/[id]/page.tsx": "confirming the parse",
        "web/app/runs/new/page.tsx": "pasting the posting",
        "web/app/runs/[id]/page.tsx": "★ the approval gate",
    }
    missing = [f"{path} — {what}" for path, what in screens.items()
               if not (ROOT / path).exists()]
    assert not missing, "missing screens:\n  " + "\n  ".join(missing)


def test_nothing_sensitive_is_tracked(in_a_git_checkout):
    """The other direction: the ignore rules still have to ignore.

    Anchoring `runs/` to `/runs/` narrowed it, and a narrowed rule is exactly
    how a folder of real resumes ends up public. This asserts the narrowing did
    not go too far.
    """
    tracked = _git("ls-files").stdout.splitlines()
    forbidden = [
        name for name in tracked
        if name == ".env"
        or name.startswith((".env.", "runs/"))
        and not name.endswith(".example")
        or name.endswith((".db", ".db-wal", ".db-shm"))
        or "node_modules/" in name
        or name.startswith(".venv/")
    ]
    assert not forbidden, (
        "these are tracked and should not be:\n  " + "\n  ".join(forbidden))
