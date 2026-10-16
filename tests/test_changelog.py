"""Guards for `CHANGELOG.md`.

The changelog is a release artefact, so it stays anchored to the code it
describes: the top `## <version> - <date>` heading names `version.VERSION`, the
dated sections run newest first, every date is a real past date, and the file is
in the house style (no em dashes). A changelog that drifts from `version.py` is
worse than none, so the drift is a test failure instead of a review habit.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

import safety_check
import version

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

# "## 0.2.0-beta - 2026-10-15"
RELEASE_HEADING = re.compile(r"^## (\S+) - (\d{4}-\d{2}-\d{2})\s*$", re.M)
# every dated section heading, release or work round: "## <version> - <date>" or "### <date>"
DATED_HEADING = re.compile(r"^#{2,3} (?:\S+ - )?(\d{4}-\d{2}-\d{2})\s*$", re.M)


@pytest.fixture
def changelog() -> str:
    assert CHANGELOG.is_file(), "CHANGELOG.md is missing (item D1.6)"
    return CHANGELOG.read_text(encoding="utf-8")


def test_changelog_is_a_dated_document(changelog):
    assert changelog.startswith("# changelog\n")
    assert RELEASE_HEADING.search(changelog), "no '## <version> - <date>' heading"
    assert DATED_HEADING.search(changelog), "no dated section"


def test_top_release_heading_names_the_current_version(changelog):
    match = RELEASE_HEADING.search(changelog)
    assert match, "no '## <version> - <date>' heading"
    assert match.group(1) == version.VERSION, (
        f"CHANGELOG.md says {match.group(1)}, version.py says {version.VERSION}"
    )


def test_older_releases_are_listed_newest_first(changelog):
    dates = [date.fromisoformat(m.group(2)) for m in RELEASE_HEADING.finditer(changelog)]
    assert dates, "no release heading"
    assert dates == sorted(dates, reverse=True), f"release sections out of order: {dates}"


def test_dated_sections_run_newest_first(changelog):
    dates = [date.fromisoformat(d) for d in DATED_HEADING.findall(changelog)]
    assert dates, "no dated section"
    assert dates == sorted(dates, reverse=True), f"dated sections out of order: {dates}"


def test_no_dated_section_is_in_the_future(changelog):
    today = date.today()
    future = [d for d in DATED_HEADING.findall(changelog) if date.fromisoformat(d) > today]
    assert not future, f"dated sections in the future: {future}"


def test_no_em_dashes(changelog):
    assert "\u2014" not in changelog, "house style: no em dashes in docs"


def test_the_safety_gate_scans_the_changelog(tmp_path, monkeypatch):
    """The docs-style check owns CHANGELOG.md now, not only the README."""
    monkeypatch.setattr(safety_check, "ROOT", tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# changelog\na \u2014 b\n", encoding="utf-8")
    problems = safety_check.check_docs_style(["CHANGELOG.md"])
    assert problems and "CHANGELOG.md" in problems[0]
