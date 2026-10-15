"""H1.1 - the prebuilt UF2s are pinned by sha256, not only by UF2 magic.

`validate_uf2()` proves a file is *a* UF2: any rebuilt firmware with the same
8-byte header passes it, and it is that firmware the RP2350 then runs against
the device for every boot. A pin is the difference between "it looks like
firmware" and "it is the build this toolkit was tested with", so the download
refuses a mismatch instead of retrying it (the same bytes would come back), and
the guided setup, the health check and the board line say which of the two they
actually checked.

The one real pin is the copy in `firmware/` the rig flashed, so the last test
asserts the pin and the file agree whenever that (gitignored) file is on disk.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import hardware_guide

ROOT = Path(__file__).resolve().parent.parent
REAL_UF2 = ROOT / "firmware" / "usbliter8.waveshare_rp2350_usb_a.uf2"
OTHER_HASH = "0" * 64


def _uf2_bytes(filler: bytes = b"\x01" * 1200) -> bytes:
    """Bytes that pass the magic check: 'UF2\\n' plus the second magic dword."""
    return b"\x55\x46\x32\x0a" + b"\x57\x51\x5d\x9e" + filler


@pytest.fixture
def fw_dir(monkeypatch, tmp_path):
    """A firmware dir with one board, so nothing touches the real firmware/."""
    monkeypatch.setattr(hardware_guide, "FW_DIR", tmp_path)
    monkeypatch.setattr(hardware_guide, "UF2_FILES", {"test_board": "usbliter8.test.uf2"})
    monkeypatch.delenv(hardware_guide.SKIP_PIN_ENV, raising=False)
    return tmp_path


def _place_uf2(fw_dir: Path, payload: bytes | None = None) -> Path:
    path = fw_dir / hardware_guide.UF2_FILES["test_board"]
    path.write_bytes(payload if payload is not None else _uf2_bytes())
    return path


def _pin(path: Path, monkeypatch, board_id: str = "test_board") -> str:
    digest = hardware_guide.sha256_file(path)
    assert digest is not None
    monkeypatch.setitem(hardware_guide.UF2_SHA256, board_id, digest)
    return digest


def _fake_download(payload: bytes):
    """Stand in for urlretrieve: write the payload where the caller asked."""
    def fake(url, dst):
        Path(dst).write_bytes(payload)
        return None

    return fake


# ── hashing ─────────────────────────────────────────────────────────


def test_sha256_of_a_file_matches_hashlib(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"usbliter8")
    assert hardware_guide.sha256_file(path) == hashlib.sha256(b"usbliter8").hexdigest()


def test_sha256_of_a_missing_file_is_none(tmp_path):
    assert hardware_guide.sha256_file(tmp_path / "nope.bin") is None


# ── the pin verdict ─────────────────────────────────────────────────


def test_an_unpinned_board_says_it_cannot_vouch_for_the_bytes(fw_dir):
    path = _place_uf2(fw_dir)
    verdict, note = hardware_guide.check_uf2_hash("test_board", path)
    assert verdict is None
    assert "no pinned sha256" in note


def test_the_pinned_bytes_read_as_pinned(fw_dir, monkeypatch):
    path = _place_uf2(fw_dir)
    pinned = _pin(path, monkeypatch)
    verdict, note = hardware_guide.check_uf2_hash("test_board", path)
    assert verdict is True
    assert pinned in note


def test_a_swapped_build_is_a_mismatch(fw_dir, monkeypatch):
    path = _place_uf2(fw_dir)
    monkeypatch.setitem(hardware_guide.UF2_SHA256, "test_board", OTHER_HASH)
    verdict, note = hardware_guide.check_uf2_hash("test_board", path)
    assert verdict is False
    # both hashes are named: the note has to be actionable
    got = hardware_guide.sha256_file(path)
    assert OTHER_HASH in note
    assert got and got in note


def test_an_unreadable_file_is_never_vouched_for(fw_dir, monkeypatch):
    monkeypatch.setitem(hardware_guide.UF2_SHA256, "test_board", OTHER_HASH)
    verdict, note = hardware_guide.check_uf2_hash("test_board", fw_dir / "gone.uf2")
    assert verdict is False
    assert "could not read" in note


def test_the_override_skips_the_pin_on_purpose(fw_dir, monkeypatch):
    path = _place_uf2(fw_dir)
    monkeypatch.setitem(hardware_guide.UF2_SHA256, "test_board", OTHER_HASH)
    monkeypatch.setenv(hardware_guide.SKIP_PIN_ENV, "1")
    verdict, note = hardware_guide.check_uf2_hash("test_board", path)
    assert verdict is None
    assert hardware_guide.SKIP_PIN_ENV in note
    assert hardware_guide.check_firmware("test_board") is True
    # and the reports say the pin was skipped, not that the board is unpinned
    assert "pin skipped" in hardware_guide.firmware_status("test_board")[1]
    assert hardware_guide.firmware_state("test_board")[1] == "present, pin skipped"


# ── usable or not ───────────────────────────────────────────────────


def test_no_file_is_not_usable(fw_dir):
    usable, note = hardware_guide.firmware_status("test_board")
    assert usable is False
    assert "missing or not a UF2" in note
    assert hardware_guide.check_firmware("test_board") is False


def test_an_unknown_board_is_not_usable(fw_dir):
    assert hardware_guide.firmware_status("no_such_board")[0] is False
    assert hardware_guide.firmware_state("no_such_board")[1] == "no firmware for this board"


def test_a_magic_only_file_is_usable_but_labelled_unpinned(fw_dir):
    _place_uf2(fw_dir)
    usable, note = hardware_guide.firmware_status("test_board")
    assert usable is True
    assert "unpinned" in note
    assert hardware_guide.check_firmware("test_board") is True


def test_a_pinned_match_is_labelled_pinned(fw_dir, monkeypatch):
    path = _place_uf2(fw_dir)
    _pin(path, monkeypatch)
    usable, note = hardware_guide.firmware_status("test_board")
    assert usable is True
    assert "sha256 pinned" in note


def test_a_pinned_mismatch_is_not_usable(fw_dir, monkeypatch):
    _place_uf2(fw_dir)
    monkeypatch.setitem(hardware_guide.UF2_SHA256, "test_board", OTHER_HASH)
    usable, note = hardware_guide.firmware_status("test_board")
    assert usable is False
    assert "does not match the pinned" in note
    assert hardware_guide.check_firmware("test_board") is False


# ── what the health check / board line print ────────────────────────


def test_state_names_the_checked_thing(fw_dir, monkeypatch):
    assert hardware_guide.firmware_state("test_board")[1] == "missing"

    path = _place_uf2(fw_dir)
    assert hardware_guide.firmware_state("test_board")[1] == "present, UF2 magic only"

    _pin(path, monkeypatch)
    assert hardware_guide.firmware_state("test_board")[1] == "present, sha256 pinned"


def test_state_reports_a_pinned_mismatch_with_its_detail(fw_dir, monkeypatch):
    _place_uf2(fw_dir)
    monkeypatch.setitem(hardware_guide.UF2_SHA256, "test_board", OTHER_HASH)
    usable, state, detail = hardware_guide.firmware_state("test_board")
    assert usable is False
    assert state == "not the pinned build"
    assert OTHER_HASH in detail


# ── the download path ───────────────────────────────────────────────


def test_download_refuses_bytes_that_are_not_the_pin(fw_dir, monkeypatch):
    monkeypatch.setitem(hardware_guide.UF2_SHA256, "test_board", OTHER_HASH)
    monkeypatch.setattr(hardware_guide, "urlretrieve", _fake_download(_uf2_bytes()))
    part = fw_dir / "f.part"
    ok, msg = hardware_guide._download_once("https://example/f.uf2", part, "test_board")
    assert ok is False
    assert msg.startswith(hardware_guide.REFUSED_PREFIX)
    assert not part.exists()                     # a refused file is never left behind


def test_download_accepts_the_pinned_bytes(fw_dir, monkeypatch):
    payload = _uf2_bytes()
    monkeypatch.setitem(hardware_guide.UF2_SHA256, "test_board",
                        hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(hardware_guide, "urlretrieve", _fake_download(payload))
    part = fw_dir / "f.part"
    ok, msg = hardware_guide._download_once("https://example/f.uf2", part, "test_board")
    assert (ok, msg) == (True, "")
    assert part.exists()


def test_download_still_refuses_a_page_that_is_not_a_uf2(fw_dir, monkeypatch):
    monkeypatch.setattr(hardware_guide, "urlretrieve",
                        _fake_download(b"<html>404</html>" * 100))
    ok, msg = hardware_guide._download_once("https://example/f.uf2", fw_dir / "f.part",
                                            "test_board")
    assert ok is False
    assert "invalid UF2 magic" in msg


def test_download_without_a_board_only_checks_the_magic(fw_dir, monkeypatch):
    monkeypatch.setattr(hardware_guide, "urlretrieve", _fake_download(_uf2_bytes()))
    ok, _ = hardware_guide._download_once("https://example/f.uf2", fw_dir / "f.part")
    assert ok is True


def test_a_refusal_is_not_retried(fw_dir, monkeypatch, capsys):
    """The same bytes would come back: download_firmware has to stop and say why."""
    monkeypatch.setitem(hardware_guide.UF2_SHA256, "test_board", OTHER_HASH)
    attempts = []

    def counting(url, dst):
        attempts.append(url)
        Path(dst).write_bytes(_uf2_bytes())

    monkeypatch.setattr(hardware_guide, "urlretrieve", counting)
    assert hardware_guide.download_firmware("test_board", retries=3) is False
    assert len(attempts) == 1
    out = capsys.readouterr().out
    assert "does not match the pinned" in out
    assert "check your internet connection" not in out   # not a network failure


def test_a_pinned_download_says_verified(fw_dir, monkeypatch, capsys):
    payload = _uf2_bytes()
    monkeypatch.setitem(hardware_guide.UF2_SHA256, "test_board",
                        hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(hardware_guide, "urlretrieve", _fake_download(payload))
    assert hardware_guide.download_firmware("test_board", retries=1) is True
    out = capsys.readouterr().out
    assert "sha256 pinned" in out
    assert (fw_dir / hardware_guide.UF2_FILES["test_board"]).exists()


# ── the table itself ────────────────────────────────────────────────


def test_every_pin_is_for_a_known_board():
    assert set(hardware_guide.UF2_SHA256) <= set(hardware_guide.UF2_FILES)


def test_every_pin_is_a_sha256():
    for board_id, pinned in hardware_guide.UF2_SHA256.items():
        assert len(pinned) == 64, board_id
        assert pinned == pinned.lower(), board_id
        int(pinned, 16)                          # a non-hex pin raises here


def test_the_local_firmware_matches_its_pin(monkeypatch):
    monkeypatch.delenv(hardware_guide.SKIP_PIN_ENV, raising=False)
    if not REAL_UF2.exists():
        pytest.skip("firmware/ is gitignored: no local copy to compare against")
    assert hardware_guide.UF2_SHA256.get("waveshare_usb_a") == \
        hardware_guide.sha256_file(REAL_UF2)
