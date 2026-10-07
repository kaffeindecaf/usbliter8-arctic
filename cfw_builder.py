"""Custom firmware builder for usbliter8-arctic.

Takes an IPSW + device offset YAML, produces patched iBSS, iBEC,
DeviceTree, kernel, RestoreRamdisk, and userland binaries.

Supports --dry-run for validation without writing.
"""

from __future__ import annotations

import os
import shutil
import sys
import subprocess
import tempfile
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
# profile (the `txm` section today) is reported as unpatched instead of being
# dropped silently by the dry run.
DICT_SECTIONS = ("ibss", "ibec", "restoreramdisk")   # _apply_dict_patches
KERNEL_SECTION = "kernel"                            # appliable_kernel_entries
DAEMONS_SECTION = "daemons"                          # patch_userland
DEVICETREE_SECTION = "devicetree"                    # dt_patch flags, not offsets
# per-section outcome of this build, printed at the end so a user can see what
# was actually patched and what was skipped (and why)
MANIFEST: list[tuple[str, str, str]] = []


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
    fp.seek(offset)
    fp.write(data)
    fp.flush()


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
    """Apply all offset/value patches from a dict section. Returns count of applied patches."""
    count = 0
    for name, entry in appliable_dict_entries(patches):
        off = entry["offset"]
        val = entry["value"]
        try:
            data = device_offsets.hex_to_bytes(val)
        except ValueError:
            data = val  # string for boot-args
        _patch_at(fp, off, data)
        count += 1
        if VERBOSE:
            print(f"    {C.GRN}✓{C.NC} {section_name}.{name} @ 0x{off:X}")
    return count


def patch_ibss(ipsw_dir: str | Path, offsets: dict, work_dir: str | Path) -> bool:
    """Patch iBSS: resolve the device's iBSS, apply the ibss patches, rewrap."""
    print(stage("1/6", "Patching iBSS"))
    ibss_patches = offsets.get("patches", {}).get("ibss", {})

    src, candidates, reason = components.find_component(ipsw_dir, "ibss", offsets,
                                                       force=FORCE_COMPONENT)
    if src is None:
        print(err(f"iBSS not found for {offsets.get('model', '?')}: {reason}"))
        _note("ibss", "failed", reason)
        return False
    if reason == "forced":
        print(warn(f"iBSS: several candidates, using {src.name}"))
    print(f"    {C.DIM}component: {src.name} ({components.component_stem(offsets, 'ibss')}){C.NC}")

    raw = Path(work_dir) / "iBSS.raw"
    if not _extract_im4p_to_raw(src, raw):
        print(err("Failed to extract iBSS"))
        _note("ibss", "failed", "extract")
        return False

    with open(raw, "r+b") as fp:
        count = _apply_dict_patches(fp, ibss_patches, "ibss")
    _note("ibss", "patched", f"{count} entries → {src.name}")

    if DRY_RUN:
        return True

    dest = Path(ipsw_dir) / "Firmware" / "dfu" / src.name
    return _wrap_raw_to_im4p(raw, dest, "ibss")


def patch_ibec(ipsw_dir: str | Path, offsets: dict, work_dir: str | Path) -> bool:
    """Patch iBEC: resolve the device's iBEC, apply the ibec patches, rewrap."""
    print(stage("2/6", "Patching iBEC"))
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
        _note("ibec", "failed", reason)
        return False

    raw = Path(work_dir) / "iBEC.raw"
    if not _extract_im4p_to_raw(src, raw):
        print(err("Failed to extract iBEC"))
        _note("ibec", "failed", "extract")
        return False

    with open(raw, "r+b") as fp:
        count = _apply_dict_patches(fp, ibec_patches, "ibec")
    _note("ibec", "patched", f"{count} entries → {src.name}")

    if DRY_RUN:
        return True

    dest = cfw_dir / src.name
    cfw_dir.mkdir(parents=True, exist_ok=True)
    return _wrap_raw_to_im4p(raw, dest, "ibec")


def patch_devicetree(ipsw_dir: str | Path, offsets: dict, work_dir: str | Path) -> bool:
    """Patch DeviceTree with the native FDT patcher (dt_patch.py)."""
    print(stage("3/6", "Patching DeviceTree"))
    dt_flags = offsets.get("patches", {}).get("devicetree", {}) or {}

    src, _candidates, reason = components.find_component(ipsw_dir, "devicetree", offsets,
                                                        force=FORCE_COMPONENT)
    if src is None:
        print(err(f"DeviceTree not found for {offsets.get('model', '?')}: {reason}"))
        _note("devicetree", "failed", reason)
        return False

    raw = Path(work_dir) / "DeviceTree.raw"
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
    _note("devicetree", "patched" if not failed else "partial",
          ", ".join(f"{op}: {status}" for op, status in report) or "no flags set")
    if failed:
        print(warn(f"DeviceTree: {', '.join(failed)}"))

    if DRY_RUN:
        return True

    dest = Path(ipsw_dir) / "Firmware" / "all_flash" / src.name
    return _wrap_raw_to_im4p(raw, dest, "dtre")


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
    print(stage("4/6", "Patching Kernel"))
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
    with open(raw, "r+b") as fp:
        for entry in kernel_patches:
            if isinstance(entry, dict) and entry.get("invalid_component"):
                continue
            if isinstance(entry, dict) and "offset" in entry and "value" in entry:
                off = entry["offset"]
                val = entry["value"]
                name = entry.get("name", "kernel")
                try:
                    try:
                        data = device_offsets.hex_to_bytes(val)
                    except ValueError:
                        # ASCII payloads (e.g. the kernel identity string
                        # "/PATCHED_ARM64_T8030") are written verbatim
                        data = val.encode() if isinstance(val, str) else bytes(val)
                    _patch_at(fp, off, data)
                    count += 1
                    if VERBOSE:
                        print(f"    {C.GRN}✓{C.NC} {name} @ 0x{off:X}")
                except ValueError:
                    print(err(f"Invalid hex for {name}: {val}"))

    print(ok(f"Kernel: {count} patches applied"))
    _note("kernel", "patched", f"{count} entries → {src.name}")

    if DRY_RUN:
        return True

    return _wrap_raw_to_im4p(raw, src)


def patch_restoreramdisk(ipsw_dir: str | Path, offsets: dict, work_dir: str | Path) -> bool:
    """Patch RestoreRamdisk components.

    On iOS 26/27 IPSWs the restore ramdisk is a plain root-level `<build>.dmg`
    and the offsets in a profile target `restored_external` and `asr` *inside*
    the mounted image. That needs mounting + re-signing (hdiutil/ldid), which
    this pure-Python path does not do, so the section is reported as skipped
    instead of silently "succeeding".
    """
    print(stage("5/6", "Patching RestoreRamdisk"))
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
    if not _extract_im4p_to_raw(src, raw):
        print(err("Failed to extract RestoreRamdisk — cannot patch im4p at raw offsets"))
        _note("restoreramdisk", "failed", "extract")
        return False

    with open(raw, "r+b") as fp:
        applied = _apply_dict_patches(fp, rd_patches, "restoreramdisk")

    if DRY_RUN:
        _note("restoreramdisk", "patched", f"{applied} entries → {src.name}")
        return True

    if not _wrap_raw_to_im4p(raw, src, "rdsk"):
        print(err("Failed to rewrap RestoreRamdisk"))
        _note("restoreramdisk", "failed", "rewrap")
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
    print(stage("6/6", "Patching Userland Daemons"))
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

        with open(binary_path, "r+b") as fp:
            for name, entry in appliable_dict_entries(patches):
                off = entry["offset"]
                val = entry["value"]
                try:
                    data = device_offsets.hex_to_bytes(val)
                    _patch_at(fp, off, data)
                    count += 1
                    if VERBOSE:
                        print(f"    {C.GRN}✓{C.NC} {daemon_name}.{name} @ 0x{off:X}")
                except ValueError:
                    print(err(f"Invalid hex for {daemon_name}.{name}: {val}"))

    if missing:
        print(err(f"Daemon binaries not found in extracted IPSW: {', '.join(missing)}"))
        print(info("Userland binaries live inside the rootfs DMG. Extract the "
                   "rootfs first (work-dir make_cfw.py toolchain or 7z), or "
                   "remove the 'daemons' section if these patches are applied "
                   "elsewhere."))
        return False

    print(ok(f"Userland: {count} daemon patches applied"))
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
    silent about a section the build path does not apply at all (the profile's
    `txm` section today: the dry run used to end with "all patches validated"
    while those entries were missing from the build).

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
                for kind in ("ibss", "ibec", "devicetree", "kernelcache", "restoreramdisk"):
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

        # Extract IPSW (it's a ZIP)
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
            print()
            print(f"  {C.GRN}{'═' * 56}{C.NC}")
            if blocked:
                print(f"  {C.AMB}  Custom firmware built, {len(blocked)} section(s) NOT fully applied{C.NC}")
            else:
                print(f"  {C.GRN}  Custom firmware built successfully!{C.NC}")
            print(f"  {C.GRN}  Patched IPSW at: {ipsw_dir}{C.NC}")
            print(f"  {C.GRN}{'═' * 56}{C.NC}")
            if blocked:
                print()
                print(warn("Do not restore this build blindly: the sections above were skipped "
                           "or mismatched."))
            print()
            return True
        else:
            print()
            print(err("CFW build had errors — check output above"))
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

    if len(args) < 2:
        print(f"  Usage: {C.FROST}python3 cfw_builder.py <ipsw_path> <offsets.yaml> [--dry-run|--check-only] [--quiet]{C.NC}")
        print(f"  Flags: --check-only    Validate every patch site without extracting the IPSW")
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
