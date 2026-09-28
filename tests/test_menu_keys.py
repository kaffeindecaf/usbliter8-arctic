"""The bash wrapper's menu keys must resolve to the row they are printed on.

The menu prints rows with `menu_opt <key> "<Label>"` and resolves the typed key
through one ordered `case`. First match wins, so a row whose key an earlier
pattern already claims is printed but unreachable: `menu_opt c "Coverage"` sat
after `2|c|config` (c opened Configure) and `menu_opt p "Propagate"` after
`8|p|pwn` (p ran the PWN check).

This test asks *bash itself* which row each advertised key resolves to: the
`case` block is copied verbatim into a probe script whose bodies are replaced by
an index echo. A re-implementation in Python could drift from shell semantics
(patterns, case sensitivity); running the real block cannot.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
WRAPPER = ROOT / "usbliter8"

# Label printed on the row -> the command the row must dispatch to.
# A new menu row without an entry here fails test_every_row_has_an_expectation.
LABEL_COMMAND = {
    "Guided Setup": "cmd_guided",
    "Configure": "cmd_config",
    "Build CFW": "cmd_build",
    "Flash": "cmd_flash",
    "SSHRD Boot": "cmd_sshrd",
    "Normal Boot": "cmd_boot",
    "Post-Boot": "cmd_postboot",
    "PWN Status": "cmd_pwn",
    "Health Check": "cmd_health",
    "Offsets": "cmd_offsets",
    "Coverage": "cmd_coverage",
    "Migrate": "cmd_migrate",
    "Propagate": "cmd_propagate",
    "Bootstrap": "cmd_bootstrap",
    "Contribute": "cmd_contribute",
    "Dependencies": "cmd_deps",
    "Explain": "cmd_explain",
    "Quit": "break",
}

_MENU_ROW = re.compile(r'^\s*menu_opt\s+(\S+)\s+"([^"]+)"', re.M)
_CASE_LINE = re.compile(r"^\s*([^)]+?)\)\s*(.*?);;\s*$")
_SHORTCUT_KEY = re.compile(r"\$\{C_FROST\}(\w)\$\{NC\}")


def _run_menu_body() -> str:
    text = WRAPPER.read_text()
    start = text.index("run_menu() {")
    end = text.index("\n}\n", start)
    return text[start:end]


def menu_rows() -> list[tuple[str, str]]:
    """(key, label) for every printed menu row, in print order."""
    return [(m.group(1), m.group(2)) for m in _MENU_ROW.finditer(_run_menu_body())]


def case_rows() -> list[tuple[list[str], str]]:
    """(patterns, body) for every branch of the menu `case`, in match order."""
    body = _run_menu_body()
    block = body[body.index('case "$choice" in'):body.index("esac")]
    rows = []
    for line in block.splitlines()[1:]:
        if not line.strip() or line.strip().startswith("#"):
            continue
        match = _CASE_LINE.match(line)
        assert match, f"unparsed case line: {line!r}"
        rows.append(([p.strip() for p in match.group(1).split("|")], match.group(2)))
    return rows


def _resolve_with_bash(keys: list[str], tmp_path: Path) -> dict[str, str]:
    """Ask bash which branch each key takes, with the bodies kept verbatim."""
    script = ["choice=$1", 'case "$choice" in']
    for index, (patterns, body) in enumerate(case_rows()):
        marker = "body" if patterns == ["*"] else str(index)
        script.append(f'  {"|".join(patterns)}) echo "{marker}" ;;  # {body[:40]}')
    script.append("esac")
    probe = tmp_path / "menu_key_probe.sh"
    probe.write_text("\n".join(script) + "\n")

    resolved = {}
    for key in keys:
        out = subprocess.run(["bash", str(probe), key], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        resolved[key] = out.stdout.strip()
    return resolved


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_every_menu_key_resolves_to_its_own_row(tmp_path):
    rows = case_rows()
    keys = [key for key, _ in menu_rows()]
    resolved = _resolve_with_bash(keys, tmp_path)

    wrong = []
    for (key, label), index in zip(menu_rows(), [resolved[k] for k in keys]):
        if index == "body":
            wrong.append(f"{key!r} ({label}) fell through to the catch-all branch")
            continue
        body = rows[int(index)][1]
        expected = LABEL_COMMAND[label]
        if expected not in body:
            wrong.append(f"{key!r} ({label}) resolved to {body.strip()[:60]!r}")
    assert wrong == [], "menu keys do not reach their own row:\n" + "\n".join(wrong)


def test_no_two_menu_rows_advertise_the_same_key():
    keys = [key for key, _ in menu_rows()]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    assert duplicates == [], duplicates


def test_every_row_has_an_expectation():
    labels = {label for _, label in menu_rows()}
    assert labels == set(LABEL_COMMAND), labels ^ set(LABEL_COMMAND)


def test_advertised_letter_keys_are_listed_as_shortcuts():
    """A letter key a user cannot discover is the same bug one step earlier."""
    text = WRAPPER.read_text()
    block = text[text.index("── shortcuts"):text.index("# ── Main dispatch")]
    documented = set(_SHORTCUT_KEY.findall(block))
    letters = {key for key, _ in menu_rows() if key.isalpha()}
    assert letters - documented == set(), sorted(letters - documented)


def test_shortcut_line_keys_exist_in_the_menu_case():
    """The other direction: no shortcut advertised for a key nothing matches."""
    text = WRAPPER.read_text()
    block = text[text.index("── shortcuts"):text.index("# ── Main dispatch")]
    advertised = set(_SHORTCUT_KEY.findall(block))
    known = {p for patterns, _ in case_rows() for p in patterns}
    assert advertised - known == set(), sorted(advertised - known)
