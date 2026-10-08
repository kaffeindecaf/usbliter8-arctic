"""Tests for contribute.py, the offset contribution helper.

The helper shipped with no coverage at all, so its CLI contract is what these
tests pin: exit codes (1 on a bad input, 0 on a read-only report), the message
every refusal prints (an error that prints nothing is the bug class this repo
has shipped before), and cmd_new writing through dump_profile_yaml only into
OFFSETS_DIR (a test must never drop a profile into the real offsets/).

Every profile here uses the fake model iPhone99,9 so a synthetic file can never
be judged against a real profile's recorded evidence.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import contribute  # noqa: E402
import device_offsets  # noqa: E402
import log_utils  # noqa: E402

ROOT = Path(__file__).parent.parent


def _profile(model: str = "iPhone99,9", ios: str = "9.9", entries: dict | None = None) -> dict:
    return {
        "device": "Test Device",
        "model": model,
        "ios_version": ios,
        "build": "00A0000h",
        "soc": "A13",
        "board": "n999ap",
        "apticket": "t8030",
        "patches": entries if entries is not None else {
            "ibss": {"image4_validate_nop": {"offset": 0x1000, "value": "1f2003d5"}},
        },
    }


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _plain(text: str) -> str:
    """Drop ANSI codes: the status rows are built from coloured fragments."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


# ── dispatcher ─────────────────────────────────────────────────────

def test_bare_cli_prints_usage_and_exits_ok(capsys):
    assert contribute.cli([]) == log_utils.EXIT_OK
    out = capsys.readouterr().out
    assert "Commands:" in out
    assert "pr" in out


def test_unknown_command_is_an_error_with_output(capsys):
    assert contribute.cli(["bogus"]) == log_utils.EXIT_ERROR
    out = capsys.readouterr().out
    assert "Unknown command: bogus" in out          # a silent refusal is the bug
    assert "Commands:" in out                       # it prints the usage too


def test_every_documented_command_is_dispatched(capsys):
    """usage() is the advertised table: a command it lists must not fall
    through to 'Unknown command' (the advertised-but-undispatched bug class)."""
    contribute.usage()
    out = _plain(capsys.readouterr().out)
    listed = [line.split()[0] for line in out.splitlines()
              if line.strip().startswith(("new", "status", "pr", "share"))]
    assert listed, "usage() named no commands"
    for cmd in listed:
        contribute.cli([cmd])
        assert f"Unknown command: {cmd}" not in _plain(capsys.readouterr().out)


# ── pr ─────────────────────────────────────────────────────────────

def test_pr_without_a_file_is_a_usage_error(capsys):
    assert contribute.cli(["pr"]) == log_utils.EXIT_ERROR
    assert "Usage: contribute.py pr" in capsys.readouterr().out


def test_pr_on_a_missing_file_reports_and_fails(capsys, tmp_path):
    assert contribute.cli(["pr", str(tmp_path / "nope.yaml")]) == log_utils.EXIT_ERROR
    assert "File not found" in capsys.readouterr().out


def test_pr_refuses_an_invalid_profile(capsys, tmp_path):
    bad = _write(tmp_path / "iPhone99,9_9.9.yaml", _profile(entries={
        "ibss": {"image4_validate_nop": {"offset": 0xDEADBEEF, "value": "1f2003d5"}},
    }))
    assert contribute.cli(["pr", str(bad)]) == log_utils.EXIT_ERROR
    out = capsys.readouterr().out
    assert "invalid entries" in out
    assert "ibss.image4_validate_nop" in out        # the failing entry is named


def test_pr_description_carries_the_profile_facts_and_git_commands(capsys, tmp_path):
    good = _write(tmp_path / "iPhone99,9_9.9.yaml", _profile())
    assert contribute.cli(["pr", str(good)]) == log_utils.EXIT_OK
    out = capsys.readouterr().out
    for needle in ("Test Device", "iPhone99,9", "9.9", "00A0000h", "n999ap",
                   "1 valid", "git add offsets/iPhone99,9_9.9.yaml",
                   "git commit -m", "pytest tests/ -q"):
        assert needle in out, needle


def test_pr_reports_pending_entries_in_the_patch_row(capsys, tmp_path):
    prof = _profile(entries={
        "ibss": {"done": {"offset": 0x1000, "value": "1f2003d5"},
                 "todo": {"offset": 0x2000, "value": "1f2003d5", "pending": True}},
    })
    path = _write(tmp_path / "iPhone99,9_9.9.yaml", prof)
    assert contribute.cli(["pr", str(path)]) == log_utils.EXIT_OK
    assert "1 pending" in capsys.readouterr().out


# ── status ─────────────────────────────────────────────────────────

def test_status_lists_each_profile_with_its_counts(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(device_offsets, "OFFSETS_DIR", tmp_path)
    _write(tmp_path / "iPhone99,9_9.9.yaml", _profile())
    _write(tmp_path / "iPhone99,8_9.9.yaml", _profile(model="iPhone99,8", entries={
        "ibss": {"bad": {"offset": 0, "value": "1f2003d5"}},
    }))
    assert contribute.cli(["status"]) == log_utils.EXIT_OK
    out = _plain(capsys.readouterr().out)
    assert "Test Device (iPhone99,9) iOS 9.9" in out
    assert "1/1" in out                             # the ready profile's count
    assert "0/1" in out                             # the broken one is listed, not dropped
    assert "iPhone99,8_9.9.yaml (invalid profile)" in out


def test_status_says_so_instead_of_crashing_when_there_are_no_profiles(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(device_offsets, "OFFSETS_DIR", tmp_path / "empty")
    assert contribute.cli(["status"]) == log_utils.EXIT_OK
    assert "No offset profiles yet" in capsys.readouterr().out


# ── new ────────────────────────────────────────────────────────────

def test_new_writes_into_offsets_dir_and_never_into_the_repo(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(contribute, "OFFSETS_DIR", tmp_path)
    before = sorted(p.name for p in (ROOT / "offsets").iterdir())
    assert contribute.cli(["new", "iPhone12,1", "27.0"]) == log_utils.EXIT_OK
    written = tmp_path / "iPhone12,1_27.0.yaml"
    assert written.is_file()
    assert sorted(p.name for p in (ROOT / "offsets").iterdir()) == before   # nothing leaked
    out = capsys.readouterr().out
    assert "Created: iPhone12,1_27.0.yaml" in out
    assert "sentinel" in out                        # the template starts unfilled
    assert yaml.safe_load(written.read_text())["patches"]


def test_new_warns_about_an_unknown_model_but_still_creates(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(contribute, "OFFSETS_DIR", tmp_path)
    assert contribute.cli(["new", "iPhone99,9", "9.9"]) == log_utils.EXIT_OK
    out = capsys.readouterr().out
    assert "not in database" in out
    assert "Known models:" in out
    assert (tmp_path / "iPhone99,9_9.9.yaml").is_file()


def test_new_declined_overwrite_keeps_the_existing_file(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(contribute, "OFFSETS_DIR", tmp_path)
    mine = _write(tmp_path / "iPhone99,9_9.9.yaml", _profile())
    before = mine.read_text()
    monkeypatch.setattr(log_utils, "safe_input", lambda *a, **kw: "n")
    assert contribute.cli(["new", "iPhone99,9", "9.9"]) == log_utils.EXIT_OK
    assert mine.read_text() == before
    assert "Cancelled" in capsys.readouterr().out


def test_new_accepted_overwrite_rewrites_the_file(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(contribute, "OFFSETS_DIR", tmp_path)
    mine = _write(tmp_path / "iPhone99,9_9.9.yaml", {"patches": {}})
    monkeypatch.setattr(log_utils, "safe_input", lambda *a, **kw: "y")
    assert contribute.cli(["new", "iPhone99,9", "9.9"]) == log_utils.EXIT_OK
    reloaded = yaml.safe_load(mine.read_text())
    assert reloaded["model"] == "iPhone99,9"
    assert set(reloaded["patches"])                     # template sections are back


# ── the sentinel counter (fresh profiles are not `pending: true`) ──

SENTINEL_YAML = """\
patches:
  kernel:
    - {name: a, offset: 0xDEADBEEF, value: "1f2003d5"}
    - {name: b, offset: 0, value: "1f2003d5"}
    - {name: c, offset: 0x1000, value: "1f2003d5"}
  ibss:
    image4_validate_nop: {offset: 0xDEADBEEF, value: "1f2003d5"}
    other: {offset: 0x2000, value: "1f2003d5"}
  daemons:
    coreauthd:
      anti_sep_crash: {offset: 0, value: "1f2003d5"}
"""


def test_count_sentinels_walks_list_flat_and_nested_shapes(tmp_path):
    path = tmp_path / "iPhone99,9_9.9.yaml"
    path.write_text(SENTINEL_YAML)
    assert contribute._count_sentinels(path) == 4       # kernel a+b, ibss, daemons.coreauthd


def test_count_sentinels_is_zero_for_missing_or_unusable_files(tmp_path):
    assert contribute._count_sentinels(tmp_path / "nope.yaml") == 0
    not_a_map = tmp_path / "list.yaml"
    not_a_map.write_text("- a\n- b\n")
    assert contribute._count_sentinels(not_a_map) == 0
    broken = tmp_path / "broken.yaml"
    broken.write_text("patches: [\n")
    assert contribute._count_sentinels(broken) == 0
