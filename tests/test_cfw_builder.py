"""Tests for the CFW builder dry run (`--check-only`).

C1.1: the dry run has to account for every patch site in a profile, through the
same entry filter the applier uses, and it has to say so when a whole section is
not applied by this build path (the profile's `txm` section). A count a user
gates a restore on must be the count a build would write.

No hardware, no network, no real component: synthetic payloads with real
AArch64 words (same trick as tests/test_preflight.py) in a fake IPSW tree.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import cfw_builder  # noqa: E402

ROOT = Path(__file__).parent.parent

# real instruction encodings, little-endian as they sit in the image
W_NOP = bytes.fromhex("1f2003d5")
W_MOV0 = bytes.fromhex("000080d2")
W_BNE = bytes.fromhex("01070054")
W_ADRP = bytes.fromhex("820800b0")
W_ADD = bytes.fromhex("42341f91")
W_BL = bytes.fromhex("65faff97")

PAYLOAD = bytearray(0x4000)
PAYLOAD[0x1000:0x1004] = W_BNE
PAYLOAD[0x1004:0x1008] = W_MOV0
PAYLOAD[0x2000:0x2004] = W_BL
PAYLOAD[0x3000:0x3004] = W_ADRP
PAYLOAD[0x3004:0x3008] = W_ADD

# one site per kind: two plain, one offset-only (never written by the applier),
# one kernel entry that belongs to another board's kernelcache, one daemon whose
# binary only exists when the rootfs is extracted, one section (txm) no code path
# applies at all
PATCHES = {
    "ibss": {
        "image4_validate_nop": {"offset": 0x1000, "value": "1f2003d5"},
        "boot_args_adrp": {"offset": 0x3000, "value": "021200d0"},
        "offset_only": {"offset": 0x1020},
    },
    "ibec": {"keep_nonce_b": {"offset": 0x2000, "value": "0a000014"}},
    "kernel": [
        {"name": "Sandbox remount", "offset": 0x1000, "value": "000080d2c0035fd6"},
        {"name": "Phone only site", "offset": 0x2000, "value": "000080d2c0035fd6",
         "invalid_component": True},
    ],
    "devicetree": {"remove_content_protect": True, "ephemeral_storage": False},
    "restoreramdisk": {"asr_sig_bypass": {"offset": 0x1F650, "value": "1f2003d5"}},
    "txm": {"query_module0": {"offset": 0x39CB0, "value": "000080d2"}},
    "daemons": {"coreauthd": {"anti_sep_crash": {"offset": 0x95C0, "value": "1f2003d5"}}},
}


def _profile(tmp_path: Path, patches: dict, name: str = "iPhone12,1_99.9.yaml") -> Path:
    """A profile name no real profile or evidence file uses."""
    path = tmp_path / name
    path.write_text(yaml.safe_dump({
        "device": "iPhone 11", "model": "iPhone12,1", "ios_version": "27.0",
        "build": "24A437", "soc": "A13", "board": "n104ap", "apticket": "t8030",
        "patches": patches,
    }, sort_keys=False))
    return path


def _tree(tmp_path: Path, *, bare_dmg: bool = False, daemon: str = "") -> Path:
    """Fake IPSW tree: the raw payloads where the resolver looks first."""
    tree = tmp_path / "ipsw"
    (tree / "Firmware" / "dfu").mkdir(parents=True)
    (tree / "ibss.raw").write_bytes(bytes(PAYLOAD))
    (tree / "ibec.raw").write_bytes(bytes(PAYLOAD))
    if bare_dmg:
        (tree / "24A437.dmg").write_bytes(b"not an im4p")
    if daemon:
        (tree / "rootfs").mkdir()
        (tree / "rootfs" / daemon).write_bytes(b"\x00" * 0x100)
    return tree


def _hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _offsets(profile: Path) -> dict:
    return yaml.safe_load(profile.read_text())


# ── the plan: one filter for the applier and the dry run ───────────

def test_every_patchable_site_is_exactly_what_the_applier_writes(tmp_path, monkeypatch):
    """The dry-run count cannot disagree with a real build (C1.1)."""
    profile = _profile(tmp_path, PATCHES)
    offsets = _offsets(profile)
    plan = cfw_builder.dry_run_plan(_tree(tmp_path), offsets)

    written: list[int] = []
    monkeypatch.setattr(cfw_builder, "_patch_at",
                        lambda fp, offset, data: written.append(offset))
    monkeypatch.setattr(cfw_builder, "VERBOSE", False)
    for section in cfw_builder.DICT_SECTIONS:
        written.clear()
        count = cfw_builder._apply_dict_patches(None, offsets["patches"][section], section)
        planned = [item["offset"] for item in plan["patchable"] if item["section"] == section]
        assert written == planned, section
        assert count == len(planned), section


def test_no_site_in_the_profile_is_invisible(tmp_path):
    """Every site-shaped entry lands in exactly one bucket."""
    plan = cfw_builder.dry_run_plan(_tree(tmp_path), _offsets(_profile(tmp_path, PATCHES)))
    counted = plan["counts"]["patchable"] + plan["counts"]["skipped"] + plan["counts"]["unpatched"]
    assert counted == 9    # ibss 3 + ibec 1 + kernel 2 + ramdisk 1 + txm 1 + daemons 1
    assert plan["counts"]["patchable"] == 5
    assert plan["counts"]["skipped"] == 3
    assert plan["counts"]["unpatched"] == 1


def test_counts_match_their_buckets(tmp_path):
    plan = cfw_builder.dry_run_plan(_tree(tmp_path), _offsets(_profile(tmp_path, PATCHES)))
    assert plan["counts"]["patchable"] == len(plan["patchable"])
    assert plan["counts"]["skipped"] == len(plan["skipped"])
    assert plan["counts"]["ops"] == len(plan["ops"])
    assert plan["counts"]["unpatched"] == sum(v["entries"] for v in plan["unpatched"].values())


def test_txm_section_is_reported_not_silently_dropped(tmp_path):
    """The whole txm section is never applied: the plan has to say so."""
    plan = cfw_builder.dry_run_plan(_tree(tmp_path), _offsets(_profile(tmp_path, PATCHES)))
    assert "txm" in plan["unpatched"]
    assert plan["unpatched"]["txm"]["entries"] == 1
    assert "build path" in plan["unpatched"]["txm"]["reason"]
    assert all(item["section"] != "txm" for item in plan["patchable"])


def test_unknown_section_is_reported_unpatched(tmp_path):
    patches = dict(PATCHES)
    patches["sep"] = {"panic_bypass": {"offset": 0x100, "value": "1f2003d5"}}
    plan = cfw_builder.dry_run_plan(_tree(tmp_path), _offsets(_profile(tmp_path, patches)))
    assert plan["unpatched"]["sep"]["entries"] == 1


def test_offset_only_entry_is_not_patchable(tmp_path):
    """It has no value, so the applier never writes it: not a patch count."""
    plan = cfw_builder.dry_run_plan(_tree(tmp_path), _offsets(_profile(tmp_path, PATCHES)))
    skipped = {(i["section"], i["entry"]): i["reason"] for i in plan["skipped"]}
    assert ("ibss", "offset_only") in skipped
    assert "no offset+value" in skipped[("ibss", "offset_only")]


def test_pending_sentinel_is_skipped(tmp_path):
    patches = {"ibss": {"boot_args_adrp": {"offset": 0x3000, "value": "021200d0",
                                           "pending": True}}}
    plan = cfw_builder.dry_run_plan(_tree(tmp_path), {"patches": patches})
    assert plan["counts"]["patchable"] == 0
    assert "pending" in plan["skipped"][0]["reason"]


def test_kernel_entry_from_another_board_is_skipped(tmp_path):
    plan = cfw_builder.dry_run_plan(_tree(tmp_path), _offsets(_profile(tmp_path, PATCHES)))
    skipped = {(i["section"], i["entry"]): i["reason"] for i in plan["skipped"]}
    assert "another kernelcache component" in skipped[("kernel", "Phone only site")]
    assert any(i["entry"] == "Sandbox remount" for i in plan["patchable"])


def test_blocked_section_is_skipped_with_its_reason(tmp_path):
    patches = {"kernel": [{"name": "iPad kernel site", "offset": 0x1000,
                           "value": "000080d2c0035fd6"}]}
    offsets = {"patches": patches,
               "blockers": {"kernel": {"entries": 1, "reason": "no ipad12p offsets"}}}
    plan = cfw_builder.dry_run_plan(_tree(tmp_path), offsets)
    assert plan["counts"]["patchable"] == 0
    assert plan["blocked"] == ["kernel"]
    assert "section blocked" in plan["skipped"][0]["reason"]


def test_bare_dmg_restore_ramdisk_is_skipped(tmp_path):
    plan = cfw_builder.dry_run_plan(_tree(tmp_path, bare_dmg=True),
                                    _offsets(_profile(tmp_path, PATCHES)))
    skipped = {(i["section"], i["entry"]): i["reason"] for i in plan["skipped"]}
    assert "mount + re-sign" in skipped[("restoreramdisk", "asr_sig_bypass")]


def test_daemon_entry_is_patchable_only_when_its_binary_is_there(tmp_path):
    without = cfw_builder.dry_run_plan(_tree(tmp_path / "a"),
                                       _offsets(_profile(tmp_path / "a", PATCHES)))
    entry = ("daemons", "coreauthd.anti_sep_crash")
    assert entry not in {(i["section"], i["entry"]) for i in without["patchable"]}

    with_binary = cfw_builder.dry_run_plan(_tree(tmp_path / "b", daemon="coreauthd"),
                                           _offsets(_profile(tmp_path / "b", PATCHES)))
    assert entry in {(i["section"], i["entry"]) for i in with_binary["patchable"]}


def test_devicetree_flags_are_ops_not_sites(tmp_path):
    plan = cfw_builder.dry_run_plan(_tree(tmp_path), _offsets(_profile(tmp_path, PATCHES)))
    assert plan["ops"] == ["remove_content_protect"]          # falsy flag is not an op
    assert all(item["section"] != "devicetree" for item in plan["patchable"])


# ── the dry run end to end ─────────────────────────────────────────

def test_dry_run_prints_the_counts_and_changes_nothing(tmp_path, monkeypatch, capsys):
    tree = _tree(tmp_path)
    profile = _profile(tmp_path, PATCHES)
    before = _hashes(tree)

    monkeypatch.setattr(cfw_builder, "DRY_RUN", True)
    monkeypatch.setattr(cfw_builder, "FORCE", False)
    cfw_builder.MANIFEST.clear()
    assert cfw_builder.build_cfw(tree, profile) is True

    out = capsys.readouterr().out
    assert "5 patch site(s) would be written" in out
    assert "3 entry/entries would be SKIPPED" in out
    assert "1 txm entry/entries are NOT applied" in out
    assert "[dry-run]" in out
    assert "ibss.image4_validate_nop @ 0x1000" in out
    assert "devicetree.remove_content_protect" in out
    assert _hashes(tree) == before                    # not one byte written
    assert not (tree.parent / "CFW").exists()


def test_check_only_cli_reports_the_counts(tmp_path):
    tree = _tree(tmp_path)
    profile = _profile(tmp_path, PATCHES)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "UL8_NO_DEPS": "1", "UL8_NO_SHARE": "1", "UL8_NO_TESTS": "1",
           "UL8_LOG_FILE": str(tmp_path / "test.log")}
    proc = subprocess.run([sys.executable, "cfw_builder.py", str(tree), str(profile),
                           "--check-only"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    assert "5 patch site(s) would be written" in proc.stdout
    assert "txm" in proc.stdout and "NOT applied" in proc.stdout


def test_check_only_cli_exits_nonzero_when_the_gate_refuses(tmp_path):
    """The exit code is the verdict, so --check-only can gate a restore."""
    profile = tmp_path / "iPhone12,1_99.9.yaml"
    profile.write_text(yaml.safe_dump({
        "device": "iPhone 11", "model": "iPhone12,1", "ios_version": "27.0",
        "build": "24A437", "board": "n104ap",
        "patches": {"ibss": {"sentinel_site": {"offset": 0x0, "value": "?"}}},
    }, sort_keys=False))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "UL8_NO_DEPS": "1", "UL8_NO_SHARE": "1", "UL8_NO_TESTS": "1",
           "UL8_LOG_FILE": str(tmp_path / "test.log")}
    proc = subprocess.run([sys.executable, "cfw_builder.py", str(tmp_path / "nope.ipsw"),
                           str(profile), "--check-only"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 1, proc.stdout
    assert "invalid entry" in proc.stdout
