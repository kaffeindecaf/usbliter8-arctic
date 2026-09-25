"""Tests for `profile_gen.py bootstrap` (checklist O1.3).

The command turns docs/BOOTSTRAPPING.md into a per-device run sheet, so what
matters is that every fact it prints comes from the repo rather than from prose:
the component name from DEVICE_DB (never from the board id, which is the bug
that made the iPad 9 fail every build), the existing profiles from offsets/,
the components from research/extracted/, and the kernel siblings from the
kernelcache component. It must also never write anything.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import profile_gen  # noqa: E402

ROOT = Path(__file__).parent.parent
ANSI = re.compile(r"\033\[[0-9;]*m")


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    monkeypatch.setenv("UL8_LOG_FILE", str(tmp_path / "usbliter8.log"))
    import log_utils
    log_utils.configure(path=tmp_path / "usbliter8.log", level="DEBUG", enabled=True)
    yield
    log_utils.configure(path=log_utils.DEFAULT_LOG_FILE, level="WARN", enabled=True)
    log_utils._state["explicit"] = False


def _sheet(model: str, ios: str, build: str = "") -> dict:
    """bootstrap_data for a model that must be in DEVICE_DB."""
    data = profile_gen.bootstrap_data(model, ios, build)
    assert data is not None, f"{model} is missing from DEVICE_DB"
    return data


def _stage(data: dict, stage_id: int) -> dict:
    return [stage for stage in data["stages"] if stage["id"] == stage_id][0]


def _sources(data: dict) -> list[dict]:
    return _stage(data, 4)["sources"]


def _run(args: list[str], tmp_path: Path) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ, UL8_LOG_FILE=str(tmp_path / "cli.log"), UL8_NO_DEPS="1")
    return subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True,
                          text=True, env=env, timeout=180)


# ── device data ─────────────────────────────────────────────────────

def test_unknown_model_returns_none():
    assert profile_gen.bootstrap_data("iPhone99,9", "27.0") is None


def test_component_names_come_from_the_device_database():
    """The board id is not the component name: j181ap boots iBSS.ipad12p."""
    data = _sheet("iPad12,1", "27.0b4")
    assert data["board"] == "j181ap"
    assert data["ibss_component"] == "iBSS.ipad12p.RELEASE.im4p"
    assert data["component_stem"] == "ipad12p"
    assert data["kernel_component"] == "kernelcache.release.ipad12p"
    assert data["ibss_component"] in _stage(data, 1)["detail"]


def test_every_stage_is_complete_and_ordered():
    data = _sheet("iPhone11,6", "26.0", "24A100")
    ids = [stage["id"] for stage in data["stages"]]
    assert ids == list(range(1, len(ids) + 1))
    for stage in data["stages"]:
        assert stage["title"] and stage["expected"]
        assert stage["commands"], stage["id"]
        assert all(isinstance(c, str) and c for c in stage["commands"])


def test_confidence_floor_is_stated_with_its_tiers():
    data = _sheet("iPhone11,6", "26.0")
    tiers = {tier["confidence"] for tier in data["confidence_tiers"]}
    assert tiers == {"0.95", "0.90", "0.60", "0.30"}
    verify = _stage(data, 6)
    assert "--record" in " ".join(verify["commands"])
    assert "verified" in verify["expected"]


def test_a12_device_is_told_ios_27_dropped_it():
    data = _sheet("iPhone11,6", "26.0")
    assert data["soc"] == "A12"
    assert any("iOS 27 dropped the A12" in note for note in data["notes"])


def test_a12_device_has_no_same_device_base():
    data = _sheet("iPhone11,8", "26.0")
    assert data["existing_profiles"] == []
    assert any("no profile at all" in note for note in data["notes"])
    labels = [source["label"] for source in _sources(data)]
    assert any("cross-device" in label for label in labels)
    # exactly one source is marked as the one to start with
    assert [s["recommended"] for s in _sources(data)].count(True) == 1


def test_existing_profile_makes_migrate_the_first_source():
    data = _sheet("iPhone12,5", "27.0b4", "24A5390f")
    newest = data["existing_profiles"][-1]
    assert newest["file"] == "iPhone12,5_27.0b3.yaml"
    migrate = _sources(data)[0]
    assert migrate["recommended"] is True
    command = " ".join(migrate["commands"])
    assert "profile_gen.py migrate" in command
    assert f"offsets/{newest['file']}" in command
    assert "offsets/iPhone12,5_27.0b4.yaml" in command
    assert "--checkpoint" in command           # a killed run resumes


def test_board_sharing_a_bootloader_image_yields_a_fill_source():
    """iPhone12,5 boots iBSS.d421 like iPhone12,3, so its sites are fingerprintable."""
    data = _sheet("iPhone12,5", "27.0b4")
    assert data["stem_siblings"] == ["iPhone12,3"]
    assert data["fill_base"] == "iPhone12,3_27.0b3.yaml"
    fill = [s for s in _sources(data) if "sibling" in s["label"]][0]
    assert "--sections ibss,ibec,txm" in " ".join(fill["commands"])


def test_unique_kernelcache_means_nothing_may_inherit_the_kernel():
    data = _sheet("iPhone12,8", "27.0b3")
    assert data["kernel_siblings"] == []
    kernel_stage = _stage(data, 5)
    assert "nothing may inherit" in kernel_stage["expected"]
    assert "kernelcache.release.iphone12c" in kernel_stage["expected"]
    assert any("blockers:" in c for c in kernel_stage["commands"])


def test_shared_kernelcache_is_named_as_inheritable():
    data = _sheet("iPhone12,5", "27.0b3")
    assert data["kernel_siblings"] == ["iPhone12,3"]
    # one sibling reads as a singular, three read as a plural
    assert "iPhone12,3 ships the same component" in _stage(data, 5)["expected"]
    a12 = _stage(_sheet("iPhone11,6", "26.0"), 5)["expected"]
    assert "iPhone11,2 and iPhone11,4 ship the same component" in a12


def test_components_already_on_disk_are_reused(tmp_path, monkeypatch):
    """A fetched component dir replaces the fetch step instead of hiding it."""
    extracted = tmp_path / "iPhone123_27.0b3_24A5380h"
    extracted.mkdir()
    monkeypatch.setattr(profile_gen, "component_dir_for",
                        lambda model, ios, build: extracted)
    data = _sheet("iPhone12,3", "27.0b3", "24A5380h")
    assert data["component_dir"] == str(extracted)
    assert "already fetched" in _stage(data, 3)["commands"][0]


# ── command line ────────────────────────────────────────────────────

def test_usage_and_unknown_device_exit_nonzero(capsys):
    assert profile_gen.cmd_bootstrap([]) == 1
    assert "Usage: bootstrap" in capsys.readouterr().out

    assert profile_gen.cmd_bootstrap(["iPhone99,9", "27.0"]) == 1
    out = capsys.readouterr().out
    assert "Unknown device" in out
    assert "iPhone12,3" in out                     # lists what it does know


def test_json_is_one_clean_object(capsys):
    assert profile_gen.cmd_bootstrap(["iPhone12,3", "27.0b4", "--json"]) == 0
    out = capsys.readouterr().out
    assert ANSI.search(out) is None
    data = json.loads(out)
    assert data["model"] == "iPhone12,3"
    assert data["stages"][0]["commands"]


def test_human_output_names_the_device_and_the_profile_target(capsys):
    assert profile_gen.cmd_bootstrap(["iPad12,1", "27.0b4"]) == 0
    out = capsys.readouterr().out
    assert "iPad 9" in out
    assert "offsets/iPad12,1_27.0b4.yaml" in out
    assert "iBSS.ipad12p.RELEASE.im4p" in out       # not iBSS.j181


def test_verb_is_wired_through_the_launcher(tmp_path):
    out = _run(["ul8.py", "bootstrap", "iPhone12,8", "27.0b3", "--json"], tmp_path)
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["ibss_component"] == "iBSS.d79.RELEASE.im4p"
    assert "unknown subcommand" not in out.stdout


def test_launcher_rejects_an_unknown_device_with_exit_1(tmp_path):
    out = _run(["ul8.py", "bootstrap", "iPhone99,9", "27.0"], tmp_path)
    assert out.returncode == 1
    assert "Unknown device" in out.stdout


def test_bootstrap_writes_nothing(tmp_path):
    """A run sheet is read-only: no profile, no evidence, no work dir."""
    before = sorted(p.name for p in (ROOT / "offsets").iterdir())
    _run(["ul8.py", "bootstrap", "iPhone11,6", "26.0", "--json"], tmp_path)
    after = sorted(p.name for p in (ROOT / "offsets").iterdir())
    assert before == after
    assert not (ROOT / "offsets" / "iPhone11,6_26.0.yaml").exists()
