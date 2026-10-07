"""Every write a build makes has to be provable, and undoable.

The requirement these pin: when something goes wrong during a build, nothing bad
happens to the user's device. Concretely, a tree a restore might flash must never
contain a component this build cannot vouch for, so:

- a component that does not read back as the patched payload is rolled back and
  its section fails (no half-patched firmware, no silently wrong fourcc)
- an entry whose offset is past the end of the component is refused, not written
- the bytes that were there before a build are kept, and can be put back
- the build records what it wrote (hashes), so a restore can check the tree first

No hardware, no network: synthetic payloads in fake IPSW trees.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import cfw_builder  # noqa: E402
import components  # noqa: E402
import img4wrap  # noqa: E402

W_BNE = bytes.fromhex("01070054")
W_NOP = bytes.fromhex("1f2003d5")
W_RET = bytes.fromhex("c0035fd6")

PAYLOAD = bytearray(0x2000)
PAYLOAD[0x100:0x104] = W_BNE
PAYLOAD[0x104:0x108] = W_RET


def _patched() -> bytes:
    """The payload after the NOP patch the profiles apply."""
    data = bytearray(PAYLOAD)
    data[0x100:0x104] = W_NOP
    return bytes(data)


def _tree(tmp_path: Path, name: str = "ipsw", *, ibss: bool = True) -> Path:
    tree = tmp_path / name
    (tree / "Firmware" / "dfu").mkdir(parents=True)
    if ibss:
        (tree / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p").write_bytes(
            img4wrap.wrap(bytes(PAYLOAD), "ibss", "mBoot-20457"))
    return tree


def _raw(tmp_path: Path, payload: bytes | None = None) -> Path:
    path = tmp_path / "iBSS.raw"
    path.write_bytes(payload if payload is not None else _patched())
    return path


@pytest.fixture(autouse=True)
def _clean_build_state():
    cfw_builder.MANIFEST.clear()
    cfw_builder.PUBLISHED.clear()
    cfw_builder.PATCH_FAILURES.clear()
    yield
    cfw_builder.MANIFEST.clear()
    cfw_builder.PUBLISHED.clear()
    cfw_builder.PATCH_FAILURES.clear()


# ── publishing: verify, or put it back ──────────────────────────────

def test_a_published_component_reads_back_as_the_patched_payload(tmp_path):
    tree = _tree(tmp_path, ibss=False)
    dest = tree / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    sites = [(0x100, W_NOP)]

    assert cfw_builder.publish_component(dest, _raw(tmp_path), "ibss", "ibss", sites,
                                         ipsw_dir=tree) is True
    assert dest in [p for _section, p in cfw_builder.PUBLISHED]
    written = img4wrap.unwrap_file(dest)
    assert written.fourcc == "ibss"
    assert written.payload == _patched()


def test_fourcc_and_description_of_apple_s_container_are_kept(tmp_path):
    """A rewrap must not invent its own shape (the device reads the fourcc)."""
    tree = _tree(tmp_path)
    dest = tree / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    original = dest.read_bytes()

    assert cfw_builder.publish_component(dest, _raw(tmp_path), "ibss", "ibss",
                                        [(0x100, W_NOP)], ipsw_dir=tree,
                                        original=original) is True
    written = img4wrap.unwrap_file(dest)
    assert written.fourcc == "ibss"
    assert written.description == "mBoot-20457"


def test_a_component_that_does_not_verify_is_rolled_back(tmp_path, monkeypatch):
    """The old bytes come back and the section fails: never a half-patched image."""
    tree = _tree(tmp_path)
    dest = tree / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    original = dest.read_bytes()
    # a writer that drops the patch: whatever it does, verification must catch it
    monkeypatch.setattr(cfw_builder, "encode_component",
                        lambda raw_path, dest, tag: (img4wrap.wrap(bytes(PAYLOAD), "ibss",
                                                                   "mBoot-20457"), "broken"))

    assert cfw_builder.publish_component(dest, _raw(tmp_path), "ibss", "ibss",
                                        [(0x100, W_NOP)], ipsw_dir=tree,
                                        original=original) is False
    assert dest.read_bytes() == original
    assert [m for m in cfw_builder.MANIFEST if m[0] == "ibss"][0][1] == "failed"
    assert cfw_builder.PUBLISHED == []
    # the pre-patch copy is still there for a manual recovery
    assert cfw_builder.originals_dir(tree).is_dir()


def test_a_component_this_build_created_is_removed_when_it_fails(tmp_path, monkeypatch):
    """No original to restore means the unverifiable file must not stay in the tree."""
    tree = _tree(tmp_path, ibss=False)
    dest = tree / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    monkeypatch.setattr(cfw_builder, "encode_component",
                        lambda raw_path, dest, tag: (img4wrap.wrap(bytes(PAYLOAD), "ibss"),
                                                     "broken"))

    assert cfw_builder.publish_component(dest, _raw(tmp_path), "ibss", "ibss",
                                        [(0x100, W_NOP)], ipsw_dir=tree) is False
    assert not dest.exists()


def test_a_wrong_fourcc_is_caught(tmp_path, monkeypatch):
    tree = _tree(tmp_path)
    dest = tree / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    original = dest.read_bytes()
    monkeypatch.setattr(cfw_builder, "encode_component",
                        lambda raw_path, dest, tag: (img4wrap.wrap(_patched(), "krnl",
                                                                   "mBoot-20457"), "broken"))

    assert cfw_builder.publish_component(dest, _raw(tmp_path), "ibss", "ibss",
                                        [(0x100, W_NOP)], ipsw_dir=tree,
                                        original=original) is False
    assert dest.read_bytes() == original


# ── a write that would corrupt the component is refused ─────────────

def test_patch_at_refuses_a_write_past_the_end(tmp_path):
    target = tmp_path / "payload.bin"
    target.write_bytes(bytes(PAYLOAD))

    with open(target, "r+b") as fp:
        with pytest.raises(cfw_builder.PatchOutOfRange):
            cfw_builder._patch_at(fp, len(PAYLOAD) - 2, W_NOP)
    assert target.read_bytes() == bytes(PAYLOAD)


def test_an_out_of_range_entry_fails_its_section_and_is_not_counted(tmp_path):
    target = tmp_path / "payload.bin"
    target.write_bytes(bytes(PAYLOAD))
    patches = {
        "good": {"offset": 0x100, "value": W_NOP.hex()},
        "past_the_end": {"offset": 0x1FFE, "value": W_NOP.hex()},
        "not_a_value": {"offset": 0x108, "value": {"nested": "dict"}},
    }

    with open(target, "r+b") as fp:
        count = cfw_builder._apply_dict_patches(fp, patches, "ibss")

    assert count == 1                        # only the entry that really landed
    assert len(cfw_builder.PATCH_FAILURES) == 2
    assert cfw_builder._section_failed("ibss") is True
    assert target.read_bytes()[0x100:0x104] == W_NOP


def test_an_ascii_value_is_still_written_verbatim(tmp_path):
    """String patches (boot-args) are not hex, and that path must keep working."""
    target = tmp_path / "payload.bin"
    target.write_bytes(bytes(PAYLOAD))
    value = "-v wdt=-1 rd=md0"

    with open(target, "r+b") as fp:
        count = cfw_builder._apply_dict_patches(
            fp, {"boot_args": {"offset": 0x200, "value": value}}, "ibss")

    assert count == 1
    assert cfw_builder.PATCH_FAILURES == []
    assert target.read_bytes()[0x200:0x200 + len(value)] == value.encode()


def test_a_section_with_a_failed_entry_never_publishes(tmp_path):
    """patch_ibss must not wrap a payload it could not patch completely."""
    tree = _tree(tmp_path)
    dest = tree / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    original = dest.read_bytes()
    profile = tmp_path / "p.yaml"
    profile.write_text(yaml.safe_dump({
        "model": "iPhone12,1", "board": "n104ap", "ios_version": "27.0", "build": "24A437",
        "patches": {"ibss": {"nop": {"offset": 0x100, "value": W_NOP.hex()},
                             "past_the_end": {"offset": 0x1FFE, "value": W_NOP.hex()}}},
    }))
    offsets = yaml.safe_load(profile.read_text())
    work = tmp_path / "work"
    work.mkdir()

    assert cfw_builder.patch_ibss(tree, offsets, work) is False
    assert dest.read_bytes() == original
    assert [m for m in cfw_builder.MANIFEST if m[0] == "ibss"][0][1] == "failed"


# ── the pre-patch copies ────────────────────────────────────────────

def test_restore_originals_puts_the_tree_back(tmp_path):
    tree = _tree(tmp_path)
    dest = tree / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    original = dest.read_bytes()

    assert cfw_builder.publish_component(dest, _raw(tmp_path), "ibss", "ibss",
                                        [(0x100, W_NOP)], ipsw_dir=tree,
                                        original=original) is True
    assert dest.read_bytes() != original

    assert cfw_builder.restore_originals(tree) == 1
    assert dest.read_bytes() == original


def test_restore_originals_reaches_a_component_outside_the_tree(tmp_path):
    """The CFW iBEC lives next to the tree: the index has to bring it back."""
    tree = _tree(tmp_path)
    cfw_dir = tmp_path / "CFW" / "Firmware" / "dfu"
    cfw_dir.mkdir(parents=True)
    dest = cfw_dir / "iBEC.d421.RELEASE.im4p"
    original = img4wrap.wrap(bytes(PAYLOAD), "ibec", "iBEC-1")
    dest.write_bytes(original)

    assert cfw_builder.publish_component(dest, _raw(tmp_path), "ibec", "ibec",
                                        [(0x100, W_NOP)], ipsw_dir=tree,
                                        original=original) is True
    assert cfw_builder.restore_originals(tree) == 1
    assert dest.read_bytes() == original


def test_restore_originals_drops_a_marker_it_invalidated(tmp_path):
    tree = _tree(tmp_path)
    dest = tree / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    original = dest.read_bytes()
    profile = tmp_path / "iPhone12,1_99.9.yaml"
    profile.write_text("model: iPhone12,1\npatches: {}\n")
    cfw_builder.publish_component(dest, _raw(tmp_path), "ibss", "ibss", [(0x100, W_NOP)],
                                 ipsw_dir=tree, original=original)
    cfw_builder.write_build_marker(tree, profile, {"model": "iPhone12,1"})
    assert cfw_builder.marker_path(tree).is_file()

    cfw_builder.restore_originals(tree)
    assert not cfw_builder.marker_path(tree).is_file()


# ── the build marker and the pre-flash check ────────────────────────

def _built_tree(tmp_path: Path) -> Path:
    tree = _tree(tmp_path)
    dest = tree / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    original = dest.read_bytes()
    cfw_builder.publish_component(dest, _raw(tmp_path), "ibss", "ibss", [(0x100, W_NOP)],
                                 ipsw_dir=tree, original=original)
    cfw_builder._note("ibss", "patched", "1 entries → iBSS.d421.RELEASE.im4p")
    profile = _tree_profile(tmp_path)
    cfw_builder.write_build_marker(tree, profile, yaml.safe_load(profile.read_text()))
    return tree


def _tree_profile(tmp_path: Path) -> Path:
    profile = tmp_path / "iPhone12,1_99.9.yaml"
    profile.write_text(yaml.safe_dump({
        "model": "iPhone12,1", "board": "n104ap", "ios_version": "27.0", "build": "24A437",
        "patches": {"ibss": {"image4_validate_nop": {"offset": 0x100, "value": W_NOP.hex()}}},
    }, sort_keys=False))
    return profile


def test_the_marker_hashes_exactly_what_the_build_wrote(tmp_path):
    tree = _built_tree(tmp_path)
    marker = cfw_builder.read_build_marker(tree)

    assert marker is not None
    assert marker["model"] == "iPhone12,1"
    assert [c["path"] for c in marker["components"]] == ["Firmware/dfu/iBSS.d421.RELEASE.im4p"]
    recorded = tree / marker["components"][0]["path"]
    assert marker["components"][0]["sha256"] == hashlib.sha256(recorded.read_bytes()).hexdigest()
    assert marker["fully_applied"] is True


def test_verify_tree_passes_a_tree_that_holds_its_patches(tmp_path):
    tree = _built_tree(tmp_path)
    ok, findings = cfw_builder.verify_tree(tree, _tree_profile(tmp_path))

    assert ok is True, findings
    assert not [f for f in findings if not f.startswith("unchecked:")]


def test_verify_tree_catches_a_component_changed_after_the_build(tmp_path):
    tree = _built_tree(tmp_path)
    dest = tree / "Firmware" / "dfu" / "iBSS.d421.RELEASE.im4p"
    payload = bytearray(img4wrap.unwrap_file(dest).payload)
    payload[0x100] ^= 0xFF
    dest.write_bytes(img4wrap.wrap(bytes(payload), "ibss", "mBoot-20457"))

    ok, findings = cfw_builder.verify_tree(tree, _tree_profile(tmp_path))
    assert ok is False
    assert any("changed since the build" in f for f in findings)
    assert any("0x100" in f for f in findings)


def test_verify_tree_reports_what_it_could_not_check(tmp_path):
    tree = _tree(tmp_path)                    # nothing built here, no marker
    ok, findings = cfw_builder.verify_tree(tree, _tree_profile(tmp_path))

    # the payload is Apple's, so the profile's site check fails: that is a
    # finding, and the missing marker has to be visible too
    assert ok is False
    assert any("no .ul8-build.json" in f for f in findings)


def test_verify_tree_flags_a_profile_that_changed_since_the_build(tmp_path):
    tree = _built_tree(tmp_path)
    profile = _tree_profile(tmp_path)
    profile.write_text(profile.read_text() + "\n# edited after the build\n")

    ok, findings = cfw_builder.verify_tree(tree, profile)
    assert ok is False
    assert any("profile has changed" in f for f in findings)


def test_a_failed_section_keeps_the_tree_out_of_a_flash(tmp_path):
    tree = _built_tree(tmp_path)
    cfw_builder.MANIFEST.clear()
    cfw_builder._note("kernel", "failed", "verify: payload is not the patched bytes")
    cfw_builder.write_build_marker(tree, _tree_profile(tmp_path), {"model": "iPhone12,1"})

    ok, findings = cfw_builder.verify_tree(tree, _tree_profile(tmp_path))
    assert ok is False
    assert any("section kernel was failed" in f for f in findings)


# ── daemons: patched in place, so they get their own guard ──────────

def test_a_daemon_that_fails_its_check_is_put_back(tmp_path, monkeypatch):
    tree = _tree(tmp_path)
    rootfs = tree / "rootfs"
    rootfs.mkdir()
    daemon = rootfs / "coreauthd"
    daemon.write_bytes(bytes(PAYLOAD))
    original = daemon.read_bytes()
    offsets = {"model": "iPhone12,1",
               "patches": {"daemons": {"coreauthd": {
                   "anti_sep_crash": {"offset": 0x100, "value": W_NOP.hex()}}}}}
    # a check that always disagrees: the binary must go back untouched
    monkeypatch.setattr(cfw_builder, "verify_binary", lambda *a, **k: "site 0x100 is wrong")

    assert cfw_builder.patch_userland(tree, offsets, tmp_path) is False
    assert daemon.read_bytes() == original


def test_verify_binary_checks_size_and_sites(tmp_path):
    target = tmp_path / "bin"
    target.write_bytes(_patched())

    assert cfw_builder.verify_binary(target, len(PAYLOAD), [(0x100, W_NOP)]) == ""
    assert "site 0x100" in cfw_builder.verify_binary(target, len(PAYLOAD), [(0x100, W_RET)])
    assert "size changed" in cfw_builder.verify_binary(target, len(PAYLOAD) + 1, [])


# ── the toolkit's own files never look like components ──────────────

def test_the_originals_store_and_marker_are_never_resolved_as_components(tmp_path):
    tree = _tree(tmp_path, ibss=False)
    store = cfw_builder.originals_dir(tree)
    store.mkdir()
    (store / "Firmware__dfu__iBSS.d421.RELEASE.im4p").write_bytes(
        img4wrap.wrap(bytes(PAYLOAD), "ibss"))
    (tree / cfw_builder.BUILD_MARKER).write_text("{}")
    offsets = {"model": "iPhone12,1", "board": "n104ap", "build": "24A437"}

    src, candidates, reason = components.find_component(tree, "ibss", offsets)
    assert src is None, f"resolved {src} ({reason})"
    assert candidates == []
