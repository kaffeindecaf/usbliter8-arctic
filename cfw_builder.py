"""Custom firmware builder for usbliter8-arctic.

Takes an IPSW + device offset YAML, produces patched iBSS, iBEC,
DeviceTree, TXM, kernel, RestoreRamdisk, and userland binaries.

Supports --dry-run for validation without writing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import yaml

import components
import device_offsets
import dt_patch
import toolchain
from colors import C, ok, err, warn, info, stage, section
import log_utils

DRY_RUN = False
VERBOSE = True
FORCE = False          # --force: build despite failed preflight / pending entries
FORCE_COMPONENT = False  # --force-component: take the first component when several match
# which profile sections this build path writes, and how. Anything else in a
# profile is reported as unpatched instead of being dropped silently by the dry
# run. `txm` used to be that section: the profiles carry it, upstream patches
# it, and nothing here applied it (every CFW booted with module validation
# intact), so it has a patcher now.
TXM_SECTION = "txm"                                  # patch_txm
DICT_SECTIONS = ("ibss", "ibec", TXM_SECTION, "restoreramdisk")  # _apply_dict_patches
# the fourcc Apple wraps the TXM payload in (verified against iPhone12,3
# 24A5380h: Firmware/txm.iphoneos.release.im4p = fourcc "trxm", description "1")
TXM_FOURCC = "trxm"
KERNEL_SECTION = "kernel"                            # appliable_kernel_entries
DAEMONS_SECTION = "daemons"                          # patch_userland
DEVICETREE_SECTION = "devicetree"                    # dt_patch flags, not offsets
# per-section outcome of this build, printed at the end so a user can see what
# was actually patched and what was skipped (and why)
MANIFEST: list[tuple[str, str, str]] = []

# (section, path) of every component this build wrote and verified, in order:
# the build marker hashes exactly these, not whatever happens to share a name
PUBLISHED: list[tuple[str, Path]] = []

# entries the current section could not apply (offset past the end of the
# component, unreadable value) - a section with any of these is failed, never
# published as "patched"
PATCH_FAILURES: list[str] = []

# the pre-patch containers of everything this build overwrites, kept inside the
# IPSW tree so `--restore-originals` can undo a build (components.py never picks
# them up: they live under a .ul8-* path)
ORIGINALS_DIRNAME = ".ul8-originals"
# what this build wrote, by component: the fingerprint a restore can be checked
# against before it erases a device
BUILD_MARKER = ".ul8-build.json"


class PatchOutOfRange(ValueError):
    """An entry points past the end of the component it patches.

    Writing it would grow/corrupt the payload instead of patching a site, so the
    applier refuses it. A silently extended payload is a component the device
    rejects at best and a half-broken boot chain at worst.
    """


def _note(section: str, status: str, detail: str = "") -> None:
    MANIFEST.append((section, status, detail))


def _patch_at(fp, offset: int, data: bytes | str):
    """Write raw bytes or encoded string at file offset."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    if DRY_RUN:
        current = "??" * len(data)
        print(f"    {C.DIM}[dry-run] offset 0x{offset:X}: {current} → {data.hex()}{C.NC}")
        return
    fp.seek(0, os.SEEK_END)
    size = fp.tell()
    if offset < 0 or offset + len(data) > size:
        raise PatchOutOfRange(
            f"0x{offset:X} + {len(data)} B is past the end of the {size:,} B component")
    fp.seek(offset)
    fp.write(data)
    fp.flush()


def entry_bytes(value: object) -> bytes:
    """Patch payload for a profile value: hex, or ASCII for string patches.

    One conversion for the appliers and for the post-write check, so what a build
    believes it wrote cannot drift from what it verifies. Anything that is
    neither hex nor a string raises, and every caller counts that as a failed
    entry rather than letting it end the build.
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    try:
        return device_offsets.hex_to_bytes(value)          # type: ignore[arg-type]
    except (AttributeError, ValueError, TypeError):
        if isinstance(value, str):
            return value.encode("utf-8")
        raise TypeError(f"{value!r} is neither hex nor a string") from None


def section_sites(section_data: dict) -> list[tuple[int, bytes]]:
    """(offset, bytes) for every appliable entry of a dict section, in order.

    An entry whose value cannot be turned into bytes is left out: the applier
    already failed the section over it, and the post-write check must not trip on
    the same entry.
    """
    out: list[tuple[int, bytes]] = []
    for _name, entry in appliable_dict_entries(section_data):
        try:
            out.append((entry["offset"], entry_bytes(entry["value"])))
        except (TypeError, ValueError):
            continue
    return out


# ── write safety: never leave behind a component the build cannot vouch for ──

INDEX_NAME = "index.json"


def originals_dir(ipsw_dir: str | Path) -> Path:
    """Where this tree's pre-patch components are kept."""
    return Path(ipsw_dir) / ORIGINALS_DIRNAME


def _atomic_write(path: Path, data: bytes) -> None:
    """Write through a sibling temp file and rename, so no reader sees a partial file.

    A component half-written by a crash (or a full disk) is a brick risk: the
    device gets a container whose payload is truncated. os.replace is atomic on
    the same filesystem, so the destination is either the old bytes or all of the
    new ones.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.ul8tmp{os.getpid()}")
    try:
        with open(tmp, "wb") as fp:
            fp.write(data)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _read_index(store: Path) -> dict:
    path = store / INDEX_NAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _keep_original(dest: Path, original: bytes | None, ipsw_dir: str | Path) -> Path | None:
    """Stash the bytes that were at `dest` before this build overwrote them.

    The store keeps a name -> relative-path index, so a root-level component
    (the kernelcache) and a nested one (Firmware/dfu/iBSS...) both restore to
    exactly where they came from.
    """
    if not original:
        return None
    tree = Path(ipsw_dir)
    store = originals_dir(tree)
    store.mkdir(parents=True, exist_ok=True)
    absolute = Path(dest).resolve()
    # the CFW iBEC lives next to the tree, not in it, so the index stores the
    # real absolute path: a restore must put bytes back where they came from,
    # never guess from a filename
    try:
        rel = absolute.relative_to(tree.resolve())
        key = str(rel).replace(os.sep, "__")
    except ValueError:
        key = f"{hashlib.sha256(str(absolute).encode()).hexdigest()[:16]}__{absolute.name}"
    out = store / key
    if not out.exists():                    # first writer wins: never back up our own output
        _atomic_write(out, original)
        index = _read_index(store)
        index[key] = str(absolute)
        try:
            _atomic_write(store / INDEX_NAME, json.dumps(index, indent=2).encode())
        except OSError:                                          # noqa: BLE001
            pass
    return out


def _rollback(dest: Path, original: bytes | None) -> None:
    """Undo one component: put the original back, or remove what we created."""
    try:
        if original:
            _atomic_write(dest, original)
            print(info(f"    restored the original {dest.name}"))
        elif dest.exists():
            dest.unlink()
            print(info(f"    removed the unverified {dest.name}"))
    except OSError as exc:                                        # noqa: BLE001
        print(err(f"    rollback failed for {dest.name}: {exc}"))
        print(f"    {C.DIM}the pre-patch copy is in {dest.parent / ORIGINALS_DIRNAME}{C.NC}")


def verify_component(dest: Path, expected_payload: bytes,
                     sites: Sequence[tuple[int, bytes]] = (),
                     expect_fourcc: str = "", expect_description: str | None = None) -> str:
    """Re-read a written component and prove it is the patched payload.

    Returns "" when the file on disk is exactly the payload the build intended in
    the container shape it came from, else a one-line reason. This is the last
    guard before a user flashes the tree: the bytes we are about to hand to a
    restore must be the bytes the profile asked for.
    """
    try:
        import img4wrap

        result = img4wrap.unwrap_file(dest)
    except Exception as exc:                                      # noqa: BLE001
        return f"cannot read the written component back: {exc}"
    if result.encrypted:
        return "the written component reads back as encrypted"
    if len(result.payload) != len(expected_payload):
        return (f"payload length is {len(result.payload):,} B, expected "
                f"{len(expected_payload):,} B")
    if result.payload != expected_payload:
        return "payload is not the patched bytes"
    if expect_fourcc and result.fourcc and result.fourcc != expect_fourcc:
        return f"container fourcc changed to {result.fourcc!r}"
    if expect_description is not None and result.description != expect_description:
        return f"container description changed to {result.description!r}"
    for offset, want in sites:
        got = result.payload[offset:offset + len(want)]
        if got != want:
            return f"site 0x{offset:X} holds {got.hex()} instead of {want.hex()}"
    return ""


def verify_binary(path: Path, expected_size: int, sites: Sequence[tuple[int, bytes]] = ()) -> str:
    """Check a binary patched in place: same size, and every site holds its value.

    Used for the rootfs daemons, which are not containers and have no rewrap step
    to lean on.
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        return f"cannot read back: {exc}"
    if len(data) != expected_size:
        return f"size changed from {expected_size:,} B to {len(data):,} B"
    for offset, want in sites:
        got = data[offset:offset + len(want)]
        if got != want:
            return f"site 0x{offset:X} holds {got.hex()} instead of {want.hex()}"
    return ""


def encode_component(raw_path: Path, dest: Path, tag: str) -> tuple[bytes | None, str]:
    """Container bytes for a patched payload, plus how they were produced.

    img4tool first (it keeps Apple's metadata by rewriting the existing file),
    then the in-tree pure-Python writer, which carries the original description
    over on a rewrap.
    """
    if toolchain.tool_available("img4tool"):
        work = Path(tempfile.mkdtemp(prefix="ul8_wrap_", dir=str(dest.parent if dest.parent.is_dir()
                                                                else Path(tempfile.gettempdir()))))
        staging = work / dest.name
        if dest.exists():
            shutil.copy2(dest, staging)          # img4tool updates an existing container
        cmd = ([toolchain.tool("img4tool"), "-c", str(staging), "-t", tag, str(raw_path)] if tag
               else [toolchain.tool("img4tool"), "-c", str(staging), str(raw_path)])
        try:
            if _run(cmd).returncode == 0 and staging.exists():
                return staging.read_bytes(), "img4tool"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    try:
        import img4wrap
    except ImportError as exc:                                    # noqa: BLE001
        return None, f"no IMG4 writer available: {exc}"
    description = ""
    fourcc = ""
    if dest.exists():                            # keep Apple's metadata on rewrap
        try:
            previous = img4wrap.unwrap_file(dest)
            description = previous.description
            fourcc = previous.fourcc
        except Exception:                                         # noqa: BLE001
            description = fourcc = ""
    if not tag and not fourcc:
        # never invent a fourcc: a real container carries the one the device and
        # the restore expect (kernelcache is "krnl", not "IM4P")
        return None, "no container tag given and no existing container to copy it from"
    try:
        # an existing container's own fourcc wins: it is what the device reads
        return img4wrap.wrap(raw_path.read_bytes(), fourcc or tag, description), "img4wrap"
    except Exception as exc:                                      # noqa: BLE001
        return None, str(exc)


def publish_component(dest: str | Path, raw_path: str | Path, tag: str, section: str,
                      sites: Sequence[tuple[int, bytes]] = (), *, ipsw_dir: str | Path,
                      original: bytes | None = None, expect_description: str | None = None) -> bool:
    """Publish a patched component: encode, write atomically, verify, roll back.

    The single place that puts patched bytes into a tree. If the file on disk is
    not provably the patched payload, the original goes back (or the file we
    created is removed) and the section fails, so a build can never hand a
    restore a tree it cannot vouch for.
    """
    dest = Path(dest)
    payload = Path(raw_path).read_bytes()
    # the shape the file has to keep: what was there before the build (fourcc and
    # description come from Apple's container, never from us)
    shape = _shape_of(original)
    data, how = encode_component(Path(raw_path), dest, tag)
    if data is None:
        print(err(f"cannot wrap {dest.name}: {how}"))
        _note(section, "failed", f"wrap: {how}")
        return False

    kept = _keep_original(dest, original, ipsw_dir)
    _atomic_write(dest, data)
    if VERBOSE:
        print(f"    {C.DIM}wrote {dest.name} ({len(data):,} B, {how}){C.NC}")

    problem = verify_component(dest, payload, sites,
                               expect_fourcc=shape.get("fourcc", ""),
                               expect_description=expect_description
                               if expect_description is not None else shape.get("description"))
    if problem:
        print(err(f"{section}: {dest.name} did not verify ({problem}) — rolling back"))
        _rollback(dest, original)
        _note(section, "failed", f"verify: {problem}")
        return False

    if kept is not None:
        try:
            kept_rel = str(kept.relative_to(Path(ipsw_dir)))
        except ValueError:
            kept_rel = str(kept)                # an out-of-tree component's backup
        _note(f"{section}.backup", "kept", kept_rel)
    PUBLISHED.append((section, dest))
    return True


def _shape_of(original: bytes | None) -> dict:
    """fourcc/description of the container this build is about to overwrite."""
    if not original:
        return {}
    try:
        import img4wrap

        if not img4wrap.looks_like_container(original):
            return {}
        unwrapped = img4wrap.unwrap(original)
        return {"fourcc": unwrapped.fourcc, "description": unwrapped.description}
    except Exception:                                             # noqa: BLE001
        return {}


def restore_originals(ipsw_dir: str | Path) -> int:
    """Undo a build: put every stashed pre-patch component back. Returns the count."""
    store = originals_dir(ipsw_dir)
    if not store.is_dir():
        print(warn(f"no {ORIGINALS_DIRNAME}/ in {ipsw_dir} — nothing to restore"))
        return 0
    restored = 0
    index = _read_index(store)
    for kept in sorted(store.iterdir()):
        if not kept.is_file() or kept.name == INDEX_NAME:
            continue
        if kept.name in index:
            target = Path(index[kept.name])
        else:                                # store from an older build: name only
            target = Path(ipsw_dir) / kept.name.replace("__", os.sep)
        if not target.parent.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, kept.read_bytes())
        restored += 1
        print(ok(f"restored {target}"))
    if restored:
        # the tree is no longer what the marker describes: drop it, so a later
        # --verify cannot pass a tree that has been rolled back
        marker_path(ipsw_dir).unlink(missing_ok=True)
        print(info("removed the build marker: this tree is back to its pre-build state"))
    return restored


# ── the build marker: what a tree is, so a restore can be gated on it ──

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def marker_path(ipsw_dir: str | Path) -> Path:
    return Path(ipsw_dir) / BUILD_MARKER


def write_build_marker(ipsw_dir: str | Path, offsets_path: Path, offsets: dict) -> Path | None:
    """Record what this build wrote, next to the tree it wrote it into.

    A restore that erases a device should be able to answer "is this the tree the
    profile produced?" without trusting a filename. The marker carries the
    profile's hash, every patched component's hash and the per-section outcome,
    so `--verify` can tell a good tree from a stale or hand-edited one.
    """
    ipsw_dir = Path(ipsw_dir)
    published = [m for m in MANIFEST if m[1] in ("patched", "partial")]
    components: list[dict] = []
    seen: set[str] = set()
    for pub_section, path in PUBLISHED:              # exact paths the build wrote
        if not path.is_file():
            continue
        try:
            rel = path.resolve().relative_to(ipsw_dir.resolve())
            record = {"path": str(rel), "outside_tree": False}
        except ValueError:
            # the CFW iBEC is written next to the tree; a restore uses it, so it
            # has to be in the record even though it is not under ipsw_dir
            record = {"path": str(path.resolve()), "outside_tree": True}
        if record["path"] in seen:
            continue
        seen.add(record["path"])
        components.append({
            "section": pub_section,
            **record,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
        })
    marker = {
        "tool": "usbliter8-arctic cfw_builder",
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile": str(Path(offsets_path).name),
        "profile_sha256": _sha256(Path(offsets_path)),
        "model": offsets.get("model", ""),
        "ios_version": offsets.get("ios_version", ""),
        "build": offsets.get("build", ""),
        "sections": [{"section": s, "status": st, "detail": d} for s, st, d in MANIFEST],
        "components": components,
        "fully_applied": not [m for m in MANIFEST if m[1] in ("failed", "mismatch", "skipped")],
        "patched_sections": len(published),
    }
    try:
        _atomic_write(marker_path(ipsw_dir), json.dumps(marker, indent=2).encode())
    except OSError as exc:                                        # noqa: BLE001
        print(warn(f"could not write {BUILD_MARKER}: {exc}"))
        return None
    return marker_path(ipsw_dir)


def read_build_marker(ipsw_dir: str | Path) -> dict | None:
    path = marker_path(ipsw_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def check_sites(dest: Path, sites: Sequence[tuple[int, bytes]]) -> str:
    """Are every one of the profile's sites present in this component? "" if yes."""
    try:
        import img4wrap

        payload = img4wrap.payload_of(dest)
    except Exception as exc:                                      # noqa: BLE001
        return f"cannot read {dest.name}: {exc}"
    for offset, want in sites:
        got = payload[offset:offset + len(want)]
        if got != want:
            return f"{dest.name}: site 0x{offset:X} holds {got.hex()} instead of {want.hex()}"
    return ""


def verify_tree(ipsw_dir: str | Path, profile_path: Path | None = None) -> tuple[bool, list[str]]:
    """Check a built tree before a restore: marker hashes, then the profile's sites.

    Returns (ok, problems). Anything this cannot check (encrypted components, a
    component that is not in the tree) is reported as an unchecked section, never
    as a pass: a flash decision has to know what was left unverified.
    """
    ipsw_dir = Path(ipsw_dir)
    problems: list[str] = []
    unchecked: list[str] = []

    marker = read_build_marker(ipsw_dir)
    if marker is None:
        unchecked.append(f"no {BUILD_MARKER} in {ipsw_dir}: the tree was not built here "
                         f"(or the marker was removed), so only the profile is checked")
    else:
        for component in marker.get("components", []):
            if component.get("outside_tree"):
                path = Path(str(component.get("path", "")))
            else:
                path = ipsw_dir / str(component.get("path", ""))
            if not path.is_file():
                problems.append(f"missing component {component.get('path')} "
                                f"(section {component.get('section')})")
                continue
            if _sha256(path) != component.get("sha256"):
                problems.append(f"{component.get('path')} changed since the build "
                                f"(section {component.get('section')})")
        for entry in marker.get("sections", []):
            if entry.get("status") in ("failed", "mismatch"):
                problems.append(f"section {entry.get('section')} was {entry.get('status')}: "
                                f"{str(entry.get('detail', ''))[:100]}")
        if marker.get("profile_sha256") and profile_path and Path(profile_path).is_file():
            if _sha256(Path(profile_path)) != marker["profile_sha256"]:
                problems.append(f"the profile has changed since this tree was built "
                                f"({Path(profile_path).name})")

    if profile_path is None and marker:
        recorded = marker.get("profile", "")
        candidate = Path(recorded)
        if candidate.is_file():
            profile_path = candidate
    if profile_path is None or not Path(profile_path).is_file():
        if not problems:
            unchecked.append("no profile given: only the marker hashes were checked")
        return (not problems, problems + [f"unchecked: {u}" for u in unchecked])

    offsets = yaml.safe_load(Path(profile_path).read_text()) or {}
    written: dict[str, list[Path]] = {}
    if marker:
        for component in marker.get("components", []):
            path = (Path(str(component.get("path", ""))) if component.get("outside_tree")
                    else ipsw_dir / str(component.get("path", "")))
            written.setdefault(str(component.get("section")), []).append(path)
    for sec_name, data in (offsets.get("patches") or {}).items():
        if sec_name == KERNEL_SECTION:
            entries = [e for e in data if isinstance(e, dict) and "offset" in e and "value" in e]
            sites = []
            for entry in entries:
                try:
                    sites.append((entry["offset"], entry_bytes(entry["value"])))
                except (TypeError, ValueError):
                    continue
        elif sec_name == DAEMONS_SECTION:
            continue                       # rootfs binaries, not in the tree
        elif isinstance(data, dict) and sec_name in DICT_SECTIONS:
            sites = section_sites(data)
        else:
            continue
        if not sites:
            continue
        kind = "kernelcache" if sec_name == KERNEL_SECTION else sec_name
        src, _cands, reason = components.find_component(ipsw_dir, kind, offsets)
        if src is None:
            unchecked.append(f"{sec_name}: {reason}")
            continue
        known = [p for p in written.get(sec_name, []) if p.resolve() == Path(src).resolve()]
        if written.get(sec_name) and not known:
            problems.append(f"{sec_name}: this tree resolves to {src}, but the build patched "
                            f"{', '.join(str(p) for p in written[sec_name])} — a restore "
                            f"would use bytes the build did not verify")
            continue
        problem = check_sites(src, sites)
        if problem:
            problems.append(problem)

    return (not problems, problems + [f"unchecked: {u}" for u in unchecked])


def _board_config(offsets: dict) -> str:
    """Full board config id (e.g. d421ap) from the profile."""
    return offsets.get("board", "d421ap")


def _run(cmd: list[str], cwd: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
    """Run a command (see toolchain.run); output is shown when VERBOSE."""
    if DRY_RUN:
        print(f"    {C.DIM}[dry-run] {' '.join(str(part) for part in cmd)}{C.NC}")
        return subprocess.CompletedProcess(cmd, 0)
    return toolchain.run(cmd, cwd=cwd, echo=VERBOSE, tail=5 if VERBOSE else 0)


def _extract_im4p_to_raw(im4p_path: str | Path, output_path: str | Path) -> bool:
    """Unwrap an im4p container to its raw payload.

    tools/img4 first (the long-standing macOS path), then the in-tree pure-Python
    decoder: the bundled binaries are Mach-O, so a Windows or Linux user has no
    way to extract a component otherwise (issue #4).
    """
    if toolchain.tool_available("img4"):
        r = _run([toolchain.tool("img4"), "-i", str(im4p_path), "-o", str(output_path)])
        if r.returncode == 0 and Path(output_path).exists():
            return True
        if VERBOSE:
            print(warn(f"img4 could not unwrap {Path(im4p_path).name}, trying the "
                       f"built-in decoder"))

    try:
        import img4wrap
        payload = img4wrap.payload_of(im4p_path)
    except Exception as exc:                                  # noqa: BLE001
        print(err(f"cannot unwrap {Path(im4p_path).name}: {exc}"))
        return False
    Path(output_path).write_bytes(payload)
    if VERBOSE:
        print(f"    {C.DIM}{Path(im4p_path).name} -> {len(payload):,} B payload{C.NC}")
    return True


def _wrap_raw_to_im4p(raw_path: str | Path, im4p_path: str | Path, tag: str = "") -> bool:
    """Wrap a raw binary back into an im4p container.

    img4tool first, then the built-in writer. The container carries no signature
    either way: the device is pwned, which is the whole point of the chain.
    """
    if toolchain.tool_available("img4tool"):
        cmd = [toolchain.tool("img4tool"), "-c", str(im4p_path), "-t", tag, str(raw_path)] if tag else \
              [toolchain.tool("img4tool"), "-c", str(im4p_path), str(raw_path)]
        if _run(cmd).returncode == 0:
            return True
        if VERBOSE:
            print(warn(f"img4tool failed for {Path(im4p_path).name}, using the "
                       f"built-in writer"))

    try:
        import img4wrap
        description = ""
        destination = Path(im4p_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():                 # keep Apple's metadata on rewrap
            try:
                description = img4wrap.unwrap_file(destination).description
            except Exception:                                 # noqa: BLE001
                description = ""
        container = img4wrap.wrap(Path(raw_path).read_bytes(), tag or "IM4P", description)
    except Exception as exc:                                  # noqa: BLE001
        print(err(f"cannot wrap {Path(im4p_path).name}: {exc}"))
        return False
    Path(im4p_path).write_bytes(container)
    return True


def appliable_dict_entries(section_data: dict) -> list[tuple[str, dict]]:
    """(name, entry) pairs a build writes from a dict-style section.

    The one filter shared by the applier (`_apply_dict_patches`), the dry-run
    plan and the tests: an entry counts only when it carries both an offset and
    a value, so nothing can advertise a site a build would not write (an
    offset-only entry used to be printed as patchable while the applier skipped
    it) or hide one it would. `pending` sentinels are NOT filtered here: the
    profile gate refuses them, and with `--force` the applier writes them.
    """
    if not isinstance(section_data, dict):
        return []
    return [(str(name), entry) for name, entry in section_data.items()
            if isinstance(entry, dict) and "offset" in entry and "value" in entry]


def _apply_dict_patches(fp, patches: dict, section_name: str) -> int:
    """Apply all offset/value patches from a dict section. Returns count of applied patches.

    An entry whose value cannot be read or whose site is past the end of the
    component is recorded in PATCH_FAILURES and skipped: the caller fails the
    section instead of publishing a partially patched component.
    """
    count = 0
    for name, entry in appliable_dict_entries(patches):
        off = entry["offset"]
        val = entry["value"]
        try:
            data = entry_bytes(val)
        except (TypeError, ValueError):
            # a value the profile carries but nothing can turn into bytes: count
            # it as a failure, never let it take the whole build down
            PATCH_FAILURES.append(f"{section_name}.{name}: value {val!r} is not writable")
            print(err(f"    {section_name}.{name}: value {val!r} is not writable"))
            continue
        try:
            _patch_at(fp, off, data)
        except PatchOutOfRange as exc:
            PATCH_FAILURES.append(f"{section_name}.{name}: {exc}")
            print(err(f"    {section_name}.{name}: {exc} — NOT patched"))
            continue
        except ValueError:
            PATCH_FAILURES.append(f"{section_name}.{name}: invalid value {val!r}")
            print(err(f"    {section_name}.{name}: invalid value {val!r}"))
            continue
        count += 1
        if VERBOSE:
            print(f"    {C.GRN}✓{C.NC} {section_name}.{name} @ 0x{off:X}")
    return count


def _section_failed(section: str) -> bool:
    """True when this section hit an entry it could not apply."""
    if not PATCH_FAILURES:
        return False
    print(err(f"{section}: {len(PATCH_FAILURES)} entry/entries NOT applied — "
              f"refusing to write a partially patched component"))
    for line in PATCH_FAILURES[:8]:
        print(f"    {C.RED}{line}{C.NC}")
    _note(section, "failed", PATCH_FAILURES[0])
    PATCH_FAILURES.clear()
    return True


def patch_ibss(ipsw_dir: str | Path, offsets: dict, work_dir: str | Path) -> bool:
    """Patch iBSS: resolve the device's iBSS, apply the ibss patches, rewrap."""
    print(stage("1/7", "Patching iBSS"))
    ibss_patches = offsets.get("patches", {}).get("ibss", {})

    src, candidates, reason = components.find_component(ipsw_dir, "ibss", offsets,
                                                       force=FORCE_COMPONENT)
    if src is None:
        print(err(f"iBSS not found for {offsets.get('model', '?')}: {reason}"))
        _note("ibss", "skipped", reason)
        return True
    if reason == "forced":
        print(warn(f"iBSS: several candidates, using {src.name}"))
    print(f"    {C.DIM}component: {src.name} ({components.component_stem(offsets, 'ibss')}){C.NC}")

    raw = Path(work_dir) / "iBSS.raw"
    original = src.read_bytes()          # the bytes at the destination before this build
    if not _extract_im4p_to_raw(src, raw):
        print(err("Failed to extract iBSS"))
        _note("ibss", "failed", "extract")
        return False

    PATCH_FAILURES.clear()
    with open(raw, "r+b") as fp:
        count = _apply_dict_patches(fp, ibss_patches, "ibss")
    if _section_failed("ibss"):
        return False

    if DRY_RUN:
        _note("ibss", "patched", f"{count} entries → {src.name}")
        return True

    dest = Path(ipsw_dir) / "Firmware" / "dfu" / src.name
    published = publish_component(dest, raw, "ibss", "ibss", section_sites(ibss_patches),
                                  ipsw_dir=ipsw_dir, original=original)
    if published:
        _note("ibss", "patched", f"{count} entries → {src.name}")
    return published


def patch_ibec(ipsw_dir: str | Path, offsets: dict, work_dir: str | Path) -> bool:
    """Patch iBEC: resolve the device's iBEC, apply the ibec patches, rewrap."""
    print(stage("2/7", "Patching iBEC"))
    ibec_patches = offsets.get("patches", {}).get("ibec", {})

    cfw_dir = Path(ipsw_dir).parent / "CFW" / "Firmware" / "dfu"
    search_roots = [Path(ipsw_dir)] + ([Path(ipsw_dir).parent / "CFW"] if cfw_dir.exists() else [])
    src = None
    reason = "no iBEC component found"
    for root in search_roots:
        src, _candidates, reason = components.find_component(root, "ibec", offsets,
                                                            force=FORCE_COMPONENT)
        if src is not None:
            break
    if src is None:
        print(err(f"iBEC not found for {offsets.get('model', '?')}: {reason}"))
        _note("ibec", "skipped", reason)
        return True

    raw = Path(work_dir) / "iBEC.raw"
    # the CFW iBEC is ours: no original bytes to keep or roll back
    if not _extract_im4p_to_raw(src, raw):
        print(err("Failed to extract iBEC"))
        _note("ibec", "failed", "extract")
        return False

    PATCH_FAILURES.clear()
    with open(raw, "r+b") as fp:
        count = _apply_dict_patches(fp, ibec_patches, "ibec")
    if _section_failed("ibec"):
        return False

    if DRY_RUN:
        _note("ibec", "patched", f"{count} entries → {src.name}")
        return True

    dest = cfw_dir / src.name
    cfw_dir.mkdir(parents=True, exist_ok=True)
    # the CFW dir is a copy of the tree's iBEC, so the original is what is there
    # now (nothing, on a first build): no rollback bytes, the file we create is
    # removed again if it does not verify
    published = publish_component(dest, raw, "ibec", "ibec", section_sites(ibec_patches),
                                  ipsw_dir=ipsw_dir, original=None)
    if published:
        _note("ibec", "patched", f"{count} entries → {src.name}")
    return published


def patch_devicetree(ipsw_dir: str | Path, offsets: dict, work_dir: str | Path) -> bool:
    """Patch DeviceTree with the native FDT patcher (dt_patch.py)."""
    print(stage("3/7", "Patching DeviceTree"))
    dt_flags = offsets.get("patches", {}).get("devicetree", {}) or {}

    src, _candidates, reason = components.find_component(ipsw_dir, "devicetree", offsets,
                                                        force=FORCE_COMPONENT)
    if src is None:
        print(err(f"DeviceTree not found for {offsets.get('model', '?')}: {reason}"))
        _note("devicetree", "skipped", reason)
        return True

    raw = Path(work_dir) / "DeviceTree.raw"
    original = src.read_bytes()
    if not _extract_im4p_to_raw(src, raw):
        print(err("Failed to extract DeviceTree"))
        _note("devicetree", "failed", "extract")
        return False

    data = Path(raw).read_bytes()
    patched, report = dt_patch.apply_profile_flags(data, dt_flags)
    for op, status in report:
        color = C.GRN if status in ("removed", "updated", "added", "unchanged") else C.AMB
        print(f"    {color}{status:<10}{C.NC} {op}")
    Path(raw).write_bytes(patched)
    failed = [f"{op}={status}" for op, status in report
              if status.startswith("node-not-found")]
    detail = ", ".join(f"{op}: {status}" for op, status in report) or "no flags set"
    if failed:
        print(warn(f"DeviceTree: {', '.join(failed)}"))

    if DRY_RUN:
        _note("devicetree", "patched" if not failed else "partial", detail)
        return True

    dest = Path(ipsw_dir) / "Firmware" / "all_flash" / src.name
    published = publish_component(dest, raw, "dtre", "devicetree", (),
                                  ipsw_dir=ipsw_dir, original=original)
    if published:
        _note("devicetree", "patched" if not failed else "partial", detail)
    return published


def blocked_entry(offsets: dict, section: str) -> dict | None:
    """The profile's `blockers:` entry for one section, if it has one.

    Blocked data looks valid but must never be applied (kernel offsets from
    another board's kernelcache): the section is refused even under --force.
    """
    for entry in device_offsets.blocked_sections_of(offsets):
        if str(entry.get("section")) == section:
            return entry
    return None


def patch_txm(ipsw_dir: str | Path, offsets: dict, work_dir: str | Path) -> bool:
    """Patch TXM: resolve the device's txm component, apply the txm patches, rewrap.

    The profiles always carried a `txm` section (query_module0/1/2,
    constraint-signature no-ops, allowed_before_secure_channel) and no code path
    applied it, so every CFW booted with Apple's module validation intact.
    Upstream's make_cfw.py extracts, patches and rewraps
    Firmware/txm.iphoneos.release.im4p; this is that step.
    """
    print(stage("4/7", "Patching TXM"))
    txm_patches = offsets.get("patches", {}).get(TXM_SECTION, {})
    if not isinstance(txm_patches, dict) or not txm_patches:
        print(warn("No txm patches defined — skipping"))
        _note(TXM_SECTION, "skipped", "no txm section in this profile")
        return True

    blocker = blocked_entry(offsets, TXM_SECTION)
    if blocker:
        print(err(f"txm section is BLOCKED for {offsets.get('model', '?')} — refusing to patch"))
        print(f"    {C.DIM}{str(blocker.get('reason', '')).strip()}{C.NC}")
        _note(TXM_SECTION, "skipped", f"blocked: {str(blocker.get('reason', '')).strip()[:120]}")
        return True

    src, _candidates, reason = components.find_component(ipsw_dir, TXM_SECTION, offsets,
                                                         force=FORCE_COMPONENT)
    if src is None:
        print(warn(f"TXM not found for {offsets.get('model', '?')}: {reason}"))
        _note(TXM_SECTION, "skipped", reason)
        return True
    print(f"    {C.DIM}component: {src.name}{C.NC}")

    raw = Path(work_dir) / "TXM.raw"
    original = src.read_bytes()
    if not _extract_im4p_to_raw(src, raw):
        print(err("Failed to extract TXM"))
        _note(TXM_SECTION, "failed", "extract")
        return False

    PATCH_FAILURES.clear()
    with open(raw, "r+b") as fp:
        count = _apply_dict_patches(fp, txm_patches, TXM_SECTION)
    if _section_failed(TXM_SECTION):
        return False

    if DRY_RUN:
        _note(TXM_SECTION, "patched", f"{count} entries → {src.name}")
        return True

    dest = Path(ipsw_dir) / "Firmware" / src.name
    published = publish_component(dest, raw, TXM_FOURCC, TXM_SECTION,
                                  section_sites(txm_patches),
                                  ipsw_dir=ipsw_dir, original=original)
    if published:
        _note(TXM_SECTION, "patched", f"{count} entries → {src.name}")
    return published


def appliable_kernel_entries(kernel_patches: list) -> tuple[list, list]:
    """Split kernel entries into (appliable, refused).

    Entries marked `invalid_component` were derived from another device's
    kernelcache (e.g. iPhone offsets in an iPad profile); applying them would
    patch unrelated code, so they are refused even with --force.
    """
    ok, invalid = [], []
    for entry in kernel_patches or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("invalid_component"):
            invalid.append(entry)
        elif "offset" in entry and "value" in entry:
            ok.append(entry)
        # entries without offset/value cannot be applied; validate_offsets reports them
    return ok, invalid


def patch_kernel(ipsw_dir: str | Path, offsets: dict, work_dir: str | Path) -> bool:
    """Patch kernelcache: resolve the device's own kernelcache, apply patches."""
    print(stage("5/7", "Patching Kernel"))
    kernel_patches = offsets.get("patches", {}).get("kernel", [])

    src, candidates, reason = components.find_component(ipsw_dir, "kernelcache", offsets,
                                                       force=FORCE_COMPONENT)
    if src is None:
        print(warn(f"Kernelcache not found for {offsets.get('model', '?')}: {reason}"))
        _note("kernel", "skipped", reason)
        return True
    expected = components.component_stem(offsets, "kernelcache")
    if reason == "forced" and expected:
        print(warn(f"kernelcache: using {src.name}, profile expects {expected}"))
    if expected and expected not in src.name:
        print(warn(f"kernelcache {src.name} does not match this device's component "
                   f"({expected}) — kernel offsets are per component"))
        _note("kernel", "mismatch", f"{src.name} != {expected}")
    print(f"    {C.DIM}component: {src.name}{C.NC}")

    raw = Path(work_dir) / "kernelcache.raw"
    original = src.read_bytes()
    if not _extract_im4p_to_raw(src, raw):
        print(err("Failed to extract kernelcache (decryption needs the wiki IV+key)"))
        _note("kernel", "failed", "extract")
        return False

    blocked = device_offsets.blocked_sections_of(offsets)
    if blocked:
        print(err(f"kernel section is BLOCKED for {offsets.get('model', '?')} — refusing to patch"))
        for entry in blocked:
            print(f"    {C.DIM}{entry.get('reason', '').strip()}{C.NC}")
        _note("kernel", "skipped", f"blocked: {blocked[0].get('reason', '').strip()[:120]}")
        return True

    count = 0
    _appliable, invalid = appliable_kernel_entries(kernel_patches)
    if invalid:
        print(err(f"kernel: {len(invalid)} entry/entries belong to a different "
                  f"kernelcache component — refusing to apply them"))
        print(f"    {C.DIM}{invalid[0].get('reason', '')}{C.NC}")
        _note("kernel", "skipped",
              f"{len(invalid)} invalid-component entries (not applied)")
    PATCH_FAILURES.clear()
    sites: list[tuple[int, bytes]] = []
    with open(raw, "r+b") as fp:
        for entry in _appliable:
            off = entry["offset"]
            name = entry.get("name", "kernel")
            try:
                data = entry_bytes(entry["value"])
            except (TypeError, ValueError):
                PATCH_FAILURES.append(f"kernel.{name}: value {entry['value']!r} is not writable")
                print(err(f"    kernel.{name}: value {entry['value']!r} is not writable"))
                continue
            try:
                _patch_at(fp, off, data)
            except PatchOutOfRange as exc:
                PATCH_FAILURES.append(f"kernel.{name}: {exc}")
                print(err(f"    kernel.{name}: {exc} — NOT patched"))
                continue
            except ValueError:
                PATCH_FAILURES.append(f"kernel.{name}: invalid value {entry['value']!r}")
                print(err(f"    kernel.{name}: invalid value {entry['value']!r}"))
                continue
            count += 1
            sites.append((off, data))
            if VERBOSE:
                print(f"    {C.GRN}✓{C.NC} {name} @ 0x{off:X}")

    if _section_failed("kernel"):
        return False

    print(ok(f"Kernel: {count} patches applied"))

    if DRY_RUN:
        _note("kernel", "patched", f"{count} entries → {src.name}")
        return True

    # in place: the kernelcache the restore picks up must be the patched one
    published = publish_component(src, raw, "krnl", "kernel", sites,
                                  ipsw_dir=ipsw_dir, original=original)
    if published:
        _note("kernel", "patched", f"{count} entries → {src.name}")
    return published


def patch_restoreramdisk(ipsw_dir: str | Path, offsets: dict, work_dir: str | Path) -> bool:
    """Patch RestoreRamdisk components.

    On iOS 26/27 IPSWs the restore ramdisk is a plain root-level `<build>.dmg`
    and the offsets in a profile target `restored_external` and `asr` *inside*
    the mounted image. That needs mounting + re-signing (hdiutil/ldid), which
    this pure-Python path does not do, so the section is reported as skipped
    instead of silently "succeeding".
    """
    print(stage("6/7", "Patching RestoreRamdisk"))
    rd_patches = offsets.get("patches", {}).get("restoreramdisk", {})

    if not rd_patches:
        print(warn("No restoreramdisk patches defined — skipping"))
        _note("restoreramdisk", "skipped", "no patches in profile")
        return True

    src, candidates, reason = components.find_component(ipsw_dir, "restoreramdisk", offsets)
    if src is None:
        print(warn(f"RestoreRamdisk not found ({reason}) — skipping"))
        _note("restoreramdisk", "skipped", reason)
        return True

    if reason == "modern-dmg-layout":
        print(warn(f"RestoreRamdisk is {src.name} (a plain dmg, not an im4p)"))
        print(f"    {C.DIM}the profile targets restored_external/asr inside the mounted image;"
              f" this build path cannot mount + re-sign it{C.NC}")
        print(f"    {C.DIM}the CFW will be built WITHOUT {len(rd_patches)} ramdisk patch(es) —"
              f" expect asr/FDR checks to fail on restore{C.NC}")
        _note("restoreramdisk", "skipped",
              f"{src.name}: needs mount+resign ({len(rd_patches)} patches not applied)")
        return True

    raw = Path(work_dir) / "RestoreRamdisk.raw"
    original = src.read_bytes()
    if not _extract_im4p_to_raw(src, raw):
        print(err("Failed to extract RestoreRamdisk — cannot patch im4p at raw offsets"))
        _note("restoreramdisk", "failed", "extract")
        return False

    PATCH_FAILURES.clear()
    with open(raw, "r+b") as fp:
        applied = _apply_dict_patches(fp, rd_patches, "restoreramdisk")
    if _section_failed("restoreramdisk"):
        return False

    if DRY_RUN:
        _note("restoreramdisk", "patched", f"{applied} entries → {src.name}")
        return True

    if not publish_component(src, raw, "rdsk", "restoreramdisk", section_sites(rd_patches),
                             ipsw_dir=ipsw_dir, original=original):
        print(err("Failed to rewrap RestoreRamdisk"))
        return False

    print(ok(f"RestoreRamdisk: {applied} patches applied and re-wrapped into IPSW"))
    _note("restoreramdisk", "patched", f"{applied} entries → {src.name}")
    return True


def _daemon_binaries(ipsw_dir: str | Path, names) -> dict[str, Path]:
    """Locate daemon binaries by file name in an IPSW tree (one walk).

    patch_userland and the dry-run plan share this, so both agree on which daemon
    patches are applicable: the binaries live inside the rootfs dmg, so a tree
    without an extracted rootfs has none of them.
    """
    wanted = {str(name) for name in names}
    found: dict[str, Path] = {}
    if not wanted:
        return found
    for root, _dirs, files in os.walk(ipsw_dir):
        for file_name in files:
            if file_name in wanted and file_name not in found:
                found[file_name] = Path(root) / file_name
    return found


def patch_userland(ipsw_dir: str | Path, offsets: dict, work_dir: str | Path) -> bool:
    """Patch userland daemons (coreauthd, ctkd, mobileactivationd)."""
    print(stage("7/7", "Patching Userland Daemons"))
    daemon_patches = offsets.get("patches", {}).get("daemons", {})

    if not daemon_patches:
        print(warn("No daemon patches defined — skipping"))
        return True

    daemon_patches = {name: entry for name, entry in daemon_patches.items()
                      if isinstance(entry, dict)}
    count = 0
    missing = []
    binaries = _daemon_binaries(ipsw_dir, daemon_patches)
    for daemon_name, patches in daemon_patches.items():
        binary_path = binaries.get(daemon_name)
        if binary_path is None:
            missing.append(daemon_name)
            continue

        original = binary_path.read_bytes()      # in place: keep the pre-patch binary
        sites: list[tuple[int, bytes]] = []
        with open(binary_path, "r+b") as fp:
            for name, entry in appliable_dict_entries(patches):
                off = entry["offset"]
                data = entry_bytes(entry["value"])
                try:
                    _patch_at(fp, off, data)
                except PatchOutOfRange as exc:
                    PATCH_FAILURES.append(f"{daemon_name}.{name}: {exc}")
                    print(err(f"    {daemon_name}.{name}: {exc} — NOT patched"))
                    continue
                except ValueError:
                    PATCH_FAILURES.append(f"{daemon_name}.{name}: invalid value "
                                          f"{entry['value']!r}")
                    print(err(f"    {daemon_name}.{name}: invalid value {entry['value']!r}"))
                    continue
                count += 1
                sites.append((off, data))
                if VERBOSE:
                    print(f"    {C.GRN}✓{C.NC} {daemon_name}.{name} @ 0x{off:X}")

        problem = verify_binary(binary_path, len(original), sites)
        if problem:
            print(err(f"{daemon_name}: {problem} — restoring the original binary"))
            _atomic_write(binary_path, original)
            PATCH_FAILURES.append(f"{daemon_name}: {problem}")
            continue

    if PATCH_FAILURES:
        print(err(f"daemons: {len(PATCH_FAILURES)} entry/entries NOT applied"))
        for line in PATCH_FAILURES[:8]:
            print(f"    {C.RED}{line}{C.NC}")
        _note("daemons", "failed", PATCH_FAILURES[0])
        PATCH_FAILURES.clear()
        return False

    if missing:
        print(err(f"Daemon binaries not found in extracted IPSW: {', '.join(missing)}"))
        print(info("Userland binaries live inside the rootfs DMG. Extract the "
                   "rootfs first (work-dir make_cfw.py toolchain or 7z), or "
                   "remove the 'daemons' section if these patches are applied "
                   "elsewhere."))
        # not a build failure: this tree simply has no rootfs to patch. The
        # manifest says so and the pre-flash check lists it as not applied.
        _note("daemons", "skipped",
              f"binaries not in this tree: {', '.join(missing)}")
        return True

    print(ok(f"Userland: {count} daemon patches applied"))
    _note("daemons", "patched", f"{count} entries in {len(binaries)} binaries")
    return True


def _profile_gate(offsets_path: Path, source: Path | None = None) -> bool:
    """Refuse to patch with an invalid, pending or unverified profile.

    This is the C1.3 gate: the patch manifest is compared against the profile
    and, when components are available, every site is byte-checked first
    (preflight.py). Skipped only with --force.
    """
    from device_offsets import pending_entries, validate_offsets  # noqa: F811 (kept local for readability)

    passed, failed, errors = validate_offsets(offsets_path)
    profile_data = yaml.safe_load(offsets_path.read_text()) or {}
    blocked_list = device_offsets.blocked_sections_of(profile_data)
    pend = pending_entries(offsets_path) + sum(int(b.get("entries", 0) or 0) for b in blocked_list)

    blocked = False
    if failed:
        print(err(f"profile has {failed} invalid entry/entries — refusing to patch"))
        for e in errors[:8]:
            print(f"    {C.RED}{e}{C.NC}")
        blocked = True
    if pend:
        print(warn(f"profile has {pend} unresolved entry/entries (pending sentinels or "
                   f"blocked sections) — not flashable"))
        blocked = True
    for blocker in blocked_list:
        print(warn(f"{blocker.get('section', '?')} section blocked: "
                   f"{str(blocker.get('reason', '')).strip()[:160]}"))

    try:
        import preflight
        # verify against the exact bytes being built from, when we have them
        src = Path(source) if source else None
        report = preflight.run_preflight(
            offsets_path,
            ipsw=src if src and src.is_file() else None,
            components=src if src and src.is_dir() else None)
    except Exception as exc:                                  # noqa: BLE001
        print(info(f"preflight unavailable ({exc}) — offsets not verified"))
        return not blocked or FORCE

    if report.verdict == "blocked":
        print(err("preflight blocked this build: the profile does not match the component"))
        for site in report.sites:
            if site.severity == "fail":
                print(f"    {C.RED}{site.section}.{site.entry}: {site.detail}{C.NC}")
        blocked = True
    elif report.components:
        print(ok(f"preflight: {report.count('match', 'plausible')} site(s) checked against "
                 f"{len(report.components)} component(s), "
                 f"{report.count('match')} matched recorded evidence"))
    else:
        print(warn("preflight: no components found — pass --components/--fetch to verify "
                   "offsets before flashing"))

    if blocked and not FORCE:
        print(info("fix the profile or re-run with --force to build anyway"))
        return False
    return True


def _print_manifest() -> None:
    """Per-section patch manifest: what this build actually wrote."""
    if not MANIFEST:
        return
    try:
        for sec, status, detail in MANIFEST:
            log_utils.log("WARN" if status in ("skipped", "mismatch", "failed", "partial")
                          else "INFO", f"manifest {sec}: {status} {detail}".strip(),
                          module="cfw_builder")
    except Exception:                                        # noqa: BLE001
        pass
    print()
    print(section("Patch manifest"))
    for sec_name, status, detail in MANIFEST:
        color = {"patched": C.GRN, "skipped": C.AMB, "mismatch": C.RED,
                 "failed": C.RED, "partial": C.AMB}.get(status, C.DIM)
        print(f"  {color}{status:<9}{C.NC} {sec_name:<15} {C.DIM}{detail}{C.NC}")
    print()


def _site_entries(section_data) -> list[tuple[str, dict]]:
    """(name, entry) pairs of offset/value entries in a list or dict section.

    Container shape only, `pending` sentinels included: used to report a section
    the build path cannot apply, so even a placeholder-only section shows up.
    """
    if isinstance(section_data, dict):
        return appliable_dict_entries(section_data)
    if isinstance(section_data, list):
        return [(str(entry.get("name", f"[{i}]")), entry) for i, entry in enumerate(section_data)
                if isinstance(entry, dict) and "offset" in entry and "value" in entry]
    return []


def dry_run_plan(ipsw_dir: str | Path, offsets: dict) -> dict:
    """What a build would write, per profile section, without writing anything.

    `--check-only` reads this. Every entry lands in exactly one bucket, so a dry
    run can neither advertise a site a build would not write (an offset-only
    entry used to be printed as patchable while the applier skipped it) nor stay
    silent about a section the build path does not apply at all. That last
    bucket is empty for the sections DICT_SECTIONS names, including `txm` (it
    had no patcher until the TXM step was added, so the dry run used to end with
    "all patches validated" while six verified sites were missing from a build).

      patchable  entries the build path writes
      skipped    the section is applied, this entry is not: pending sentinel,
                 blocked section, another board's kernelcache component, a daemon
                 whose binary is in the rootfs dmg
      ops        devicetree flags (dt_patch edits nodes, not offsets)
      unpatched  sections no code path applies, with the entry count
    """
    patches = offsets.get("patches", {}) or {}
    blocked = {str(b.get("section")): (str(b.get("reason", "")).strip() or "blocked")
               for b in device_offsets.blocked_sections_of(offsets)}
    root = Path(ipsw_dir)

    rd_reason = ""
    if root.is_dir():
        _rd, _cands, rd_reason = components.find_component(root, "restoreramdisk", offsets)

    daemons_data = patches.get(DAEMONS_SECTION)
    daemons = daemons_data if isinstance(daemons_data, dict) else {}
    binaries = (_daemon_binaries(root, [n for n, e in daemons.items() if isinstance(e, dict)])
                if root.is_dir() else {})

    plan: dict = {"patchable": [], "skipped": [], "ops": [], "unpatched": {},
                  "blocked": sorted(name for name in blocked if name), "rd_reason": rd_reason}

    def patchable(sec_name: str, name: str, entry: dict) -> None:
        plan["patchable"].append({"section": sec_name, "entry": name,
                                  "offset": entry["offset"], "value": entry["value"]})

    def skipped(sec_name: str, name: str, reason: str) -> None:
        plan["skipped"].append({"section": sec_name, "entry": name, "reason": reason})

    for sec_name in DICT_SECTIONS:
        data = patches.get(sec_name)
        if not isinstance(data, dict):
            continue
        for name, entry in data.items():
            if not isinstance(entry, dict):
                continue                       # devicetree-style flags, not sites
            if "offset" not in entry or "value" not in entry:
                skipped(sec_name, name, "no offset+value in the profile")
            elif entry.get("pending"):
                skipped(sec_name, name, "pending sentinel (no offsets for this device yet)")
            elif sec_name in blocked:
                skipped(sec_name, name, f"section blocked: {blocked[sec_name]}")
            elif sec_name == "restoreramdisk" and rd_reason == "modern-dmg-layout":
                skipped(sec_name, name, "needs mount + re-sign (bare dmg)")
            else:
                patchable(sec_name, name, entry)

    kernel = patches.get(KERNEL_SECTION)
    if isinstance(kernel, list):
        for index, entry in enumerate(kernel):
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name", f"kernel[{index}]"))
            if "offset" not in entry or "value" not in entry:
                skipped(KERNEL_SECTION, name, "no offset+value in the profile")
            elif entry.get("invalid_component"):
                skipped(KERNEL_SECTION, name, "derived from another kernelcache component")
            elif KERNEL_SECTION in blocked:
                skipped(KERNEL_SECTION, name, f"section blocked: {blocked[KERNEL_SECTION]}")
            elif entry.get("pending"):
                skipped(KERNEL_SECTION, name, "pending sentinel (no offsets for this device yet)")
            else:
                patchable(KERNEL_SECTION, name, entry)

    for daemon_name, entries in daemons.items():
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            if not isinstance(entry, dict) or "offset" not in entry or "value" not in entry:
                continue
            label = f"{daemon_name}.{name}"
            if entry.get("pending"):
                skipped(DAEMONS_SECTION, label, "pending sentinel (no offsets for this device yet)")
            elif daemon_name not in binaries:
                skipped(DAEMONS_SECTION, label,
                        "daemon binary is in the rootfs dmg, not in this tree")
            else:
                patchable(DAEMONS_SECTION, label, entry)

    dt_flags = patches.get(DEVICETREE_SECTION)
    if isinstance(dt_flags, dict):
        plan["ops"] = [str(name) for name, flag in dt_flags.items() if flag]

    known = set(DICT_SECTIONS) | {KERNEL_SECTION, DAEMONS_SECTION, DEVICETREE_SECTION}
    for sec_name, data in patches.items():
        if sec_name in known:
            continue
        entries = _site_entries(data)
        if entries:
            plan["unpatched"][sec_name] = {
                "entries": len(entries),
                "reason": "the build path has no patcher for this section"}

    reasons: dict[str, int] = {}
    for item in plan["skipped"]:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    plan["skip_reasons"] = reasons
    plan["counts"] = {"patchable": len(plan["patchable"]), "skipped": len(plan["skipped"]),
                      "ops": len(plan["ops"]),
                      "unpatched": sum(v["entries"] for v in plan["unpatched"].values())}
    return plan


def build_cfw(ipsw_path: Path, offsets_path: Path) -> bool:
    """Full CFW build pipeline."""
    with log_utils.timed('build', 'build_cfw'):
        if DRY_RUN:
            print()
            print(f"  {C.AMB}{'=' * 56}{C.NC}")
            print(f"  {C.AMB}DRY RUN — no files will be modified{C.NC}")
            print(f"  {C.AMB}{'=' * 56}{C.NC}")
            print()

        MANIFEST.clear()
        PUBLISHED.clear()
        with open(offsets_path) as f:
            offsets = yaml.safe_load(f)

        model = offsets.get("model", "unknown")
        try:
            log_utils.install()
            log_utils.log_info(f"build start: {model} iOS {offsets.get('ios_version', '?')} "
                               f"({offsets.get('build', '?')}) ipsw={ipsw_path} dry_run={DRY_RUN}",
                               module="cfw_builder")
        except Exception:                                        # noqa: BLE001
            pass
        ios = offsets.get("ios_version", "unknown")
        device = offsets.get("device", "unknown")

        print(section(f"Target: {device} ({model}) — iOS {ios}"))
        print()

        if not _profile_gate(offsets_path, ipsw_path):
            return False

        if not ipsw_path.exists():
            print(err(f"IPSW not found: {ipsw_path}"))
            return False

        # Create working directory
        work_dir = Path(tempfile.mkdtemp(prefix="usbliter8_cfw_"))
        print(info(f"Work directory: {work_dir}"))

        if DRY_RUN:
            print()
            print(f"  {C.SNOW}Would extract IPSW to:{C.NC} {work_dir}")
            print()

            # Simulate all patch steps
            print(section("Patch Simulation"))
            if ipsw_path.is_dir():
                for kind in ("ibss", "ibec", "devicetree", "txm", "kernelcache", "restoreramdisk"):
                    path, _cands, _reason = components.find_component(ipsw_path, kind, offsets)
                    stem = components.component_stem(offsets, kind)
                    label = f"{C.DIM}{kind}{C.NC}"
                    if path is not None:
                        print(f"    {C.GRN}✓{C.NC} {label}: {path.name}")
                    elif stem:
                        print(f"    {C.AMB}—{C.NC} {label}: not found (expected {stem})")
            plan = dry_run_plan(ipsw_path, offsets)
            counts = plan["counts"]
            for item in plan["patchable"]:
                if item["section"] == KERNEL_SECTION:
                    print(f"    {C.DIM}[dry-run]{C.NC} {item['entry']} @ 0x{item['offset']:X} "
                          f"→ {item['value']}")
                else:
                    print(f"    {C.DIM}[dry-run]{C.NC} {item['section']}.{item['entry']} "
                          f"@ 0x{item['offset']:X}")
            for op in plan["ops"]:
                print(f"    {C.DIM}[dry-run]{C.NC} {DEVICETREE_SECTION}.{op}")
            print()
            if counts["skipped"]:
                print(warn(f"{counts['skipped']} entry/entries would be SKIPPED (pending, invalid for "
                           f"this device's component, or not appliable by this build path)"))
                for reason, number in sorted(plan["skip_reasons"].items(), key=lambda kv: -kv[1]):
                    print(f"    {C.DIM}{number}x {reason}{C.NC}")
            for section_name, details in sorted(plan["unpatched"].items()):
                print(warn(f"{details['entries']} {section_name} entry/entries are NOT applied: "
                           f"{details['reason']}"))
            if plan["blocked"]:
                print(warn(f"blocked section(s): {', '.join(plan['blocked'])} "
                           f"(see the profile's blockers:)"))
            if plan["rd_reason"] == "modern-dmg-layout":
                print(warn("restore ramdisk is a bare .dmg: those offsets target "
                           "restored_external/asr inside the mounted image"))
            skipped_text = f", {counts['skipped']} skipped" if counts["skipped"] else ""
            print(ok(f"Dry-run complete — {counts['patchable']} patch site(s) would be written"
                     f"{skipped_text}"))
            log_utils.log("WARN" if (counts["skipped"] or plan["unpatched"]) else "INFO",
                          f"dry run {offsets.get('model', '?')}: {counts['patchable']} patchable, "
                          f"{counts['skipped']} skipped, "
                          f"{counts['unpatched']} in unapplied sections "
                          f"({', '.join(sorted(plan['unpatched'])) or 'none'})",
                          module="cfw_builder")
            shutil.rmtree(work_dir)
            return True

        # Extract IPSW (it's a ZIP) — or patch an already-extracted tree in place
        if ipsw_path.is_dir():
            ipsw_dir = ipsw_path
            print(info(f"IPSW already extracted — patching {ipsw_dir} in place"))
        else:
            ipsw_dir = Path(tempfile.mkdtemp(prefix="usbliter8_ipsw_"))
            print(info(f"Extracting IPSW to {ipsw_dir}..."))
            import zipfile
            with zipfile.ZipFile(ipsw_path) as zf:
                zf.extractall(ipsw_dir)
            print(ok("IPSW extracted"))

        # Run patches
        try:
            ok_patch = all([
                patch_ibss(ipsw_dir, offsets, work_dir),
                patch_ibec(ipsw_dir, offsets, work_dir),
                patch_devicetree(ipsw_dir, offsets, work_dir),
                patch_txm(ipsw_dir, offsets, work_dir),
                patch_kernel(ipsw_dir, offsets, work_dir),
                patch_restoreramdisk(ipsw_dir, offsets, work_dir),
                patch_userland(ipsw_dir, offsets, work_dir),
            ])
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        _print_manifest()

        try:
            log_utils.log("INFO" if ok_patch else "ERROR",
                          f"build {'finished' if ok_patch else 'FAILED'}: {model} -> {ipsw_dir}",
                          module="cfw_builder")
        except Exception:                                        # noqa: BLE001
            pass

        if ok_patch:
            blocked = [m for m in MANIFEST if m[1] in ("skipped", "mismatch", "failed")]
            marker = write_build_marker(ipsw_dir, offsets_path, offsets) if not DRY_RUN else None
            print()
            print(f"  {C.GRN}{'═' * 56}{C.NC}")
            if blocked:
                print(f"  {C.AMB}  Custom firmware built, {len(blocked)} section(s) NOT fully applied{C.NC}")
            else:
                print(f"  {C.GRN}  Custom firmware built successfully!{C.NC}")
            print(f"  {C.GRN}  Patched IPSW at: {ipsw_dir}{C.NC}")
            print(f"  {C.GRN}{'═' * 56}{C.NC}")
            if marker is not None:
                print(f"  {C.DIM}  every patched component was re-read and verified; "
                      f"{marker.name} records the hashes{C.NC}")
                print(f"  {C.DIM}  check before flashing: python3 cfw_builder.py {ipsw_dir} "
                      f"{offsets_path} --verify{C.NC}")
                print(f"  {C.DIM}  undo this build: python3 cfw_builder.py {ipsw_dir} "
                      f"--restore-originals{C.NC}")
            if blocked:
                print()
                print(warn("Do not restore this build blindly: the sections above were skipped "
                           "or mismatched."))
            print()
            return True
        else:
            print()
            print(err("CFW build had errors — check output above"))
            print(f"  {C.DIM}any component that failed its post-write check was rolled back; "
                  f"the pre-patch copies are in {ORIGINALS_DIRNAME}/{C.NC}")
            return False


# ── CLI ──

def _cli() -> int:
    """Command line entry point: whatever it raises, guard() turns into an exit code."""
    # the flags live at module level: without `global` these assignments would
    # only rebind locals and --force/--dry-run/--quiet would silently do nothing
    global FORCE, FORCE_COMPONENT, DRY_RUN, VERBOSE
    log_utils.install()          # usbliter8.log + unhandled-exception logging
    args = sys.argv[1:]

    if "--force" in args:
        FORCE = True
        args = [a for a in args if a != "--force"]

    if "--force-component" in args:
        FORCE_COMPONENT = True
        args = [a for a in args if a != "--force-component"]

    if "--dry-run" in args or "--check" in args or "--check-only" in args:
        DRY_RUN = True
        args = [a for a in args if a not in ("--dry-run", "--check", "--check-only")]

    if "--quiet" in args or "-q" in args:
        VERBOSE = False
        args = [a for a in args if a not in ("--quiet", "-q")]

    # verification / undo modes: they never patch anything
    verify_mode = "--verify" in args
    restore_mode = "--restore-originals" in args
    args = [a for a in args if a not in ("--verify", "--restore-originals")]

    if restore_mode:
        if not args:
            print(err("usage: cfw_builder.py <ipsw_dir> --restore-originals"))
            return log_utils.EXIT_ERROR
        target = Path(args[0])
        if not target.is_dir():
            print(err(f"{target} is not a directory (an extracted IPSW tree is needed)"))
            return log_utils.EXIT_ERROR
        restored = restore_originals(target)
        return log_utils.EXIT_OK if restored else log_utils.EXIT_ERROR

    if verify_mode:
        if not args:
            print(err("usage: cfw_builder.py <ipsw_dir> [profile.yaml] --verify"))
            return log_utils.EXIT_ERROR
        target = Path(args[0])
        profile = Path(args[1]) if len(args) > 1 else None
        print(section("Verify a built tree"))
        ok_verify, findings = verify_tree(target, profile)
        for line in findings:
            if line.startswith("unchecked:"):
                print(warn(f"  ? {line[len('unchecked:'):].strip()}"))
            else:
                print(err(f"  ✗ {line}"))
        if ok_verify:
            unchecked = [line for line in findings if line.startswith("unchecked:")]
            print(ok(f"  verified: {target} holds the patches this profile asks for"))
            if unchecked:
                print(warn(f"  {len(unchecked)} section(s) could not be checked "
                           f"(listed above) — weigh those before flashing"))
        else:
            print(err("  do NOT flash this tree: the checks above failed"))
        return log_utils.EXIT_OK if ok_verify else log_utils.EXIT_BLOCKED

    if len(args) < 2:
        print(f"  Usage: {C.FROST}python3 cfw_builder.py <ipsw_path> <offsets.yaml> [--dry-run|--check-only] [--quiet]{C.NC}")
        print(f"  Flags: --check-only    Validate every patch site without extracting the IPSW")
        print(f"         --verify        Check a built tree against its build marker + profile "
              f"(no patching)")
        print(f"         --restore-originals  Put the pre-patch components of {ORIGINALS_DIRNAME}/ back")
        print(f"         --quiet         Suppress per-patch output")
        print(f"         --force         Build even when preflight fails (NOT recommended)")
        print(f"         --force-component  Use the first component when several match")
        sys.exit(1)

    ipsw = Path(args[0])
    offsets = Path(args[1])

    if not offsets.exists():
        print(err(f"Offset file not found: {offsets}"))
        sys.exit(1)

    built = build_cfw(ipsw, offsets)
    # the exit code is the verdict, so a refused or failed run cannot look like a
    # success to a caller that gates on it (`--check-only` before a restore)
    return log_utils.EXIT_OK if built else log_utils.EXIT_ERROR


if __name__ == "__main__":

    log_utils.install()          # usbliter8.log + clean exits

    import deps
    deps.ensure(("profiles", "build"))          # offer to install what is missing
    sys.exit(log_utils.guard(_cli))
