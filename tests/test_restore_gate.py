"""The gate in front of an erase, and the two work-dir traps around it.

`restore_device()` erases the device, so it has to check the tree it is about to
build the CFW from (a tree that no longer matches its build marker refuses, and
the override is deliberate). It also has to say so when the work dir cannot be
checked at all - and when that work dir's Ramdisk is the SSHRD chain rather than
Apple's, which an SSHRD boot leaves behind.

Everything runs in dry-run mode with the prompt stubbed: no hardware, no erase.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import boot_chain  # noqa: E402
import cfw_builder  # noqa: E402
import img4wrap  # noqa: E402
import log_utils  # noqa: E402

W_BNE = bytes.fromhex("01070054")
W_NOP = bytes.fromhex("1f2003d5")

PAYLOAD = bytearray(0x2000)
PAYLOAD[0x100:0x104] = W_BNE

# the files restore_device insists on before it does anything
REQUIRED = ("make_cfw.py", "restore_cfw.sh", "tss_proxy_server.py")


@pytest.fixture(autouse=True)
def _dry_run(monkeypatch, tmp_path):
    boot_chain.DRY_RUN = True
    monkeypatch.chdir(tmp_path)                     # no stray log file in the repo
    monkeypatch.setattr(log_utils, "safe_input", lambda prompt="": "YES")
    yield
    boot_chain.DRY_RUN = False


def _work_dir(tmp_path: Path, *, built: bool = True) -> Path:
    work = tmp_path / "work"
    (work / "Firmware" / "dfu").mkdir(parents=True)
    dest = work / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    dest.write_bytes(img4wrap.wrap(bytes(PAYLOAD), "ibss", "mBoot-20457"))
    for name in REQUIRED:
        (work / name).write_text("#!/bin/sh\nexit 0\n")
    if built:
        cfw_builder.MANIFEST.clear()
        cfw_builder.PUBLISHED.clear()
        assert cfw_builder.publish_component(dest, _raw(tmp_path), "ibss", "ibss",
                                            [(0x100, W_NOP)], ipsw_dir=work,
                                            original=dest.read_bytes()) is True
        cfw_builder._note("ibss", "patched", "1 entries → iBSS.d421.RELEASE.im4p")
        profile = tmp_path / "iPhone12,1_99.9.yaml"
        profile.write_text(yaml.safe_dump({
            "model": "iPhone12,1", "board": "n104ap", "ios_version": "27.0", "build": "24A437",
            "patches": {"ibss": {"nop": {"offset": 0x100, "value": W_NOP.hex()}}},
        }, sort_keys=False))
        cfw_builder.write_build_marker(work, profile, yaml.safe_load(profile.read_text()))
    return work


def _raw(tmp_path: Path) -> Path:
    raw = tmp_path / "iBSS.raw"
    data = bytearray(PAYLOAD)
    data[0x100:0x104] = W_NOP
    raw.write_bytes(bytes(data))
    return raw


def _run(work: Path, capsys) -> tuple[bool, str]:
    result = boot_chain.restore_device(work)
    return result, capsys.readouterr().out


def test_a_tree_that_matches_its_marker_is_allowed_through(tmp_path, capsys):
    work = _work_dir(tmp_path)
    ok, out = _run(work, capsys)

    assert ok is True
    assert "Build verified" in out


def test_a_tree_that_no_longer_matches_its_marker_refuses_the_restore(tmp_path, capsys):
    work = _work_dir(tmp_path)
    dest = work / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    unwrapped = img4wrap.unwrap_file(dest)
    payload = bytearray(unwrapped.payload)
    payload[0x100] ^= 0xFF                          # a hand-edited component
    dest.write_bytes(img4wrap.wrap(bytes(payload), unwrapped.fourcc, unwrapped.description))

    ok, out = _run(work, capsys)

    assert ok is False
    assert "Refusing to restore" in out
    assert "changed since the build" in out


def test_the_override_is_deliberate_and_needs_the_env_var(tmp_path, capsys, monkeypatch):
    work = _work_dir(tmp_path)
    dest = work / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    unwrapped = img4wrap.unwrap_file(dest)
    payload = bytearray(unwrapped.payload)
    payload[0x100] ^= 0xFF
    dest.write_bytes(img4wrap.wrap(bytes(payload), unwrapped.fourcc, unwrapped.description))

    monkeypatch.setenv(boot_chain.FORCE_RESTORE_ENV, "1")
    ok, out = _run(work, capsys)

    assert ok is True
    assert "failed verification anyway" in out


def test_a_work_dir_without_a_marker_says_it_cannot_be_checked(tmp_path, capsys):
    work = _work_dir(tmp_path, built=False)
    ok, out = _run(work, capsys)

    assert ok is True
    assert "No build marker" in out


def test_restore_warns_when_the_work_dir_holds_the_sshrd_ramdisk(tmp_path, capsys):
    work = _work_dir(tmp_path)
    (work / boot_chain.SSHRD_MARKER).write_text("Ramdisk replaced\n")

    ok, out = _run(work, capsys)

    assert ok is True
    assert "SSHRD chain" in out and boot_chain.RAMDISK_BACKUP in out


def test_sshrd_boot_keeps_the_ramdisk_it_replaces(tmp_path, monkeypatch, capsys):
    work = _work_dir(tmp_path)
    ramdisk = work / "Ramdisk"
    (ramdisk / "nested").mkdir(parents=True)
    (ramdisk / "RestoreRamdisk.apple").write_bytes(b"apple ramdisk")
    ssh_bak = work / "Ramdisk_SSH_bak"
    ssh_bak.mkdir()
    (ssh_bak / "RestoreRamdisk.ssh").write_bytes(b"ssh ramdisk")
    (work / "boot_rd.sh").write_text("#!/bin/sh\nexit 0\n")
    boot_chain.DRY_RUN = True

    assert boot_chain.sshrd_boot(work) is True
    out = capsys.readouterr().out

    # the Apple ramdisk is kept, the SSH chain is in place, and the note says so
    kept = work / boot_chain.RAMDISK_BACKUP
    assert (kept / "RestoreRamdisk.apple").read_bytes() == b"apple ramdisk"
    assert (ramdisk / "RestoreRamdisk.ssh").exists()
    assert (work / boot_chain.SSHRD_MARKER).is_file()
    assert "kept at" in out


def test_a_second_sshrd_boot_does_not_overwrite_the_kept_ramdisk(tmp_path, monkeypatch):
    """The first copy is the real original: a later swap must not clobber it."""
    work = _work_dir(tmp_path)
    ramdisk = work / "Ramdisk"
    ramdisk.mkdir()
    (ramdisk / "RestoreRamdisk.apple").write_bytes(b"apple ramdisk")
    (work / "Ramdisk_SSH_bak").mkdir()
    (work / "Ramdisk_SSH_bak" / "RestoreRamdisk.ssh").write_bytes(b"ssh")
    (work / "boot_rd.sh").write_text("#!/bin/sh\nexit 0\n")

    boot_chain.sshrd_boot(work)
    (ramdisk / "RestoreRamdisk.ssh2").write_bytes(b"second chain")
    boot_chain.sshrd_boot(work)

    kept = work / boot_chain.RAMDISK_BACKUP
    assert (kept / "RestoreRamdisk.apple").read_bytes() == b"apple ramdisk"
