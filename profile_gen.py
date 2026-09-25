#!/usr/bin/env python3
"""Offset profile generator for usbliter8-arctic.

Creates valid YAML offset files from a device model, iOS version,
and optionally merges kernel offsets from an existing DarkSword
offsets.m source file.

Usage:
  python3 profile_gen.py create iPhone12,1 27.0
  python3 profile_gen.py merge iPhone12,1_27.0b2.yaml kernel_offsets.txt
  python3 profile_gen.py diff base.yaml updated.yaml
"""

from __future__ import annotations

import copy
import json
import sys
import yaml
from pathlib import Path
from typing import Any

from colors import C, ok, err, warn, info, section
import log_utils

SCRIPT_DIR = Path(__file__).parent
OFFSETS_DIR = SCRIPT_DIR / "offsets"
TEMPLATE_PATH = OFFSETS_DIR / "template.yaml"
SENTINEL = 0xDEADBEEF

# Sections whose offsets are SoC-shared for a given build (kernelcache.release.<soc>,
# restore ramdisk, daemons and plist edits are device-independent) vs sections that
# are per-device bootloader binaries (iBSS/iBEC/TXM) requiring per-device discovery.
SHARED_SECTIONS = ("kernel", "daemons", "restoreramdisk", "devicetree", "userland")
DEVICE_SECTIONS = ("ibss", "ibec", "txm")

# Hard rule (OffsetMigrationChecklist.md): never trust anything below 0.90.
PENDING_CONFIDENCE = 0.90

# `ibss_component` is the name Apple uses inside `iBSS.<X>.RELEASE.im4p` /
# `iBEC.<X>.RELEASE.im4p`. It is NOT always the board id: every iPad ships a
# family name (iPad 9 = `ipad12p`, iPad 8 = `ipad11b`, iPad mini 5/Air 3 =
# `j210`), and the iPhone11,x IPSWs carry all three XS/XS Max images at once.
# Read them straight out of the shipped IPSWs (see fetch_components.py) instead
# of deriving them from `board`, and never let a glob pick for you.
DEVICE_DB = {
    "iPhone11,2": {"name": "iPhone XS",           "soc": "A12", "board": "d321ap",  "apticket": "t8020",
                   "ibss_component": "d321",   "kernel_component": "kernelcache.release.iphone11"},
    "iPhone11,4": {"name": "iPhone XS Max (CN)",   "soc": "A12", "board": "d331pap", "apticket": "t8020",
                   "ibss_component": "d331p",  "kernel_component": "kernelcache.release.iphone11"},
    "iPhone11,6": {"name": "iPhone XS Max",        "soc": "A12", "board": "d331ap",  "apticket": "t8020",
                   "ibss_component": "d331",   "kernel_component": "kernelcache.release.iphone11"},
    "iPhone11,8": {"name": "iPhone XR",            "soc": "A12", "board": "n841ap",  "apticket": "t8020",
                   "ibss_component": "n841",   "kernel_component": "kernelcache.release.iphone11b"},
    "iPhone12,1": {"name": "iPhone 11",            "soc": "A13", "board": "n104ap",  "apticket": "t8030",
                   "ibss_component": "n104",   "kernel_component": "kernelcache.release.iphone12b"},
    "iPhone12,3": {"name": "iPhone 11 Pro",        "soc": "A13", "board": "d421ap",  "apticket": "t8030",
                   "ibss_component": "d421",   "kernel_component": "kernelcache.release.iphone12"},
    "iPhone12,5": {"name": "iPhone 11 Pro Max",    "soc": "A13", "board": "d431ap",  "apticket": "t8030",
                   "ibss_component": "d421",   "kernel_component": "kernelcache.release.iphone12"},
    "iPhone12,8": {"name": "iPhone SE (2nd gen)",  "soc": "A13", "board": "d79ap",   "apticket": "t8030",
                   "ibss_component": "d79",    "kernel_component": "kernelcache.release.iphone12c"},
    "iPad11,1":   {"name": "iPad mini 5 (WiFi)",   "soc": "A12", "board": "j211ap",  "apticket": "t8020",
                   "ibss_component": "j210",   "kernel_component": "kernelcache.release.ipad11"},
    "iPad11,2":   {"name": "iPad mini 5 (Cell)",   "soc": "A12", "board": "j212ap",  "apticket": "t8020",
                   "ibss_component": "j210",   "kernel_component": "kernelcache.release.ipad11"},
    "iPad11,3":   {"name": "iPad Air 3 (WiFi)",    "soc": "A12", "board": "j213ap",  "apticket": "t8020",
                   "ibss_component": "j210",   "kernel_component": "kernelcache.release.ipad11"},
    "iPad11,4":   {"name": "iPad Air 3 (Cell)",    "soc": "A12", "board": "j214ap",  "apticket": "t8020",
                   "ibss_component": "j210",   "kernel_component": "kernelcache.release.ipad11"},
    "iPad11,6":   {"name": "iPad 8 (WiFi)",        "soc": "A12", "board": "j171ap",  "apticket": "t8020",
                   "ibss_component": "ipad11b", "kernel_component": "kernelcache.release.ipad11b"},
    "iPad11,7":   {"name": "iPad 8 (Cell)",        "soc": "A12", "board": "j172ap",  "apticket": "t8020",
                   "ibss_component": "ipad11b", "kernel_component": "kernelcache.release.ipad11b"},
    "iPad12,1":   {"name": "iPad 9 (WiFi)",        "soc": "A13", "board": "j181ap",  "apticket": "t8030",
                   "ibss_component": "ipad12p", "kernel_component": "kernelcache.release.ipad12p"},
    "iPad12,2":   {"name": "iPad 9 (Cell)",        "soc": "A13", "board": "j182ap",  "apticket": "t8030",
                   "ibss_component": "ipad12p", "kernel_component": "kernelcache.release.ipad12p"},
}


def generate_profile(model: str, ios_version: str, build: str = "unknown") -> dict:
    """Generate a new offset profile from template + device database."""
    dev = DEVICE_DB.get(model)
    if not dev:
        print(warn(f"Device {model} not in database — using defaults"))
        dev = {"name": model, "soc": "A12", "board": "unknown", "apticket": "t8020"}

    with open(TEMPLATE_PATH) as f:
        template = yaml.safe_load(f)

    template["device"] = dev["name"]
    template["model"] = model
    template["ios_version"] = ios_version
    template["build"] = build
    template["soc"] = dev["soc"]
    template["board"] = dev["board"]
    template["apticket"] = dev["apticket"]

    return template


def merge_kernel_offsets(profile: dict, kernel_file: Path) -> dict:
    """Merge kernel offsets from a DarkSword offsets.m style file into a profile."""
    if not kernel_file.exists():
        print(warn(f"Kernel offset file not found: {kernel_file}"))
        return profile

    with open(kernel_file) as f:
        content = f.read()

    patches = profile.get("patches", {}).get("kernel", [])
    merged = 0

    import re
    for pattern_name, regex, fmt in [
        ("usb_restricted", r"off_usb_restricted\s*=\s*(0x[0-9a-fA-F]+)", "value"),
        ("sandbox_mmap",  r"off_sandbox_mmap\s*=\s*(0x[0-9a-fA-F]+)", "value"),
        ("amfi_trust",    r"off_amfi_trust\s*=\s*(0x[0-9a-fA-F]+)", "value"),
    ]:
        m = re.search(regex, content)
        if m:
            for entry in patches:
                if isinstance(entry, dict) and pattern_name in entry.get("name", "").lower():
                    entry["offset"] = int(m.group(1), 16)
                    merged += 1
                    break

    if merged:
        print(ok(f"Merged {merged} kernel offsets from kernel offset file"))
    else:
        print(warn("No kernel offsets could be merged — offsets.m may not contain matching labels"))

    return profile


def diff_profiles(base: Path, updated: Path) -> bool:
    """Show differences between two offset profiles."""
    with open(base) as f:
        data_a = yaml.safe_load(f)
    with open(updated) as f:
        data_b = yaml.safe_load(f)

    print(section("Profile Diff"))
    print(f"  {C.DIM}A: {base.name}  —  B: {updated.name}{C.NC}")
    print()

    changes = 0
    for sec in ["ibss", "ibec", "restoreramdisk", "kernel", "daemons"]:
        patches_a = data_a.get("patches", {}).get(sec, {})
        patches_b = data_b.get("patches", {}).get(sec, {})

        if sec == "kernel":
            offsets_a = {e.get("name", "?"): e.get("offset", 0) for e in patches_a if isinstance(e, dict)}
            offsets_b = {e.get("name", "?"): e.get("offset", 0) for e in patches_b if isinstance(e, dict)}
        elif isinstance(patches_a, dict) and isinstance(patches_b, dict):
            offsets_a = {k: v.get("offset", 0) for k, v in patches_a.items() if isinstance(v, dict)}
            offsets_b = {k: v.get("offset", 0) for k, v in patches_b.items() if isinstance(v, dict)}
        else:
            continue

        all_keys = set(offsets_a) | set(offsets_b)
        for key in sorted(all_keys):
            off_a = offsets_a.get(key, 0)
            off_b = offsets_b.get(key, 0)
            if off_a != off_b:
                diff_val = off_b - off_a
                sign = "+" if diff_val > 0 else ""
                print(f"  {C.EYE}{sec}.{key}{C.NC}")
                print(f"    {C.DIM}{base.name}:{C.NC} 0x{off_a:X}")
                print(f"    {C.DIM}{updated.name}:{C.NC} 0x{off_b:X}  {C.AMB}({sign}{diff_val:+d}){C.NC}")
                changes += 1

    if changes == 0:
        print(ok("No differences found — profiles are identical"))
    else:
        print(f"\n  {C.AMB}{changes} changed offset(s){C.NC}")
    print()
    return changes > 0


def cmd_create(args: list[str]):
    model = args[0] if len(args) > 0 else ""
    ios = args[1] if len(args) > 1 else ""
    build = args[2] if len(args) > 2 else "unknown"
    kernel_file = Path(args[3]) if len(args) > 3 else None

    if not model or not ios:
        print(err("Usage: create <Model> <iOS_version> [build] [kernel_offsets_file]"))
        return

    fname = f"{model}_{ios}.yaml"
    out_path = OFFSETS_DIR / fname

    if out_path.exists():
        overwrite = log_utils.safe_input(f"  {C.AMB}{fname} already exists. Overwrite? [y/N]:{C.NC} ")
        if overwrite.lower() not in ("y", "yes"):
            print(info("Cancelled"))
            return

    profile = generate_profile(model, ios, build)
    if kernel_file:
        profile = merge_kernel_offsets(profile, kernel_file)

    from device_offsets import dump_profile_yaml
    dump_profile_yaml(profile, out_path)
    print(ok(f"Created: {fname}"))
    print(f"  {C.DIM}Fill in the 0x{SENTINEL:8X} sentinel values with real offsets.{C.NC}")
    print(f"  {C.DIM}Validate with: python3 device_offsets.py validate {fname}{C.NC}")


def cmd_diff(args: list[str]):
    if len(args) < 2:
        print(err("Usage: diff <base.yaml> <updated.yaml>"))
        return
    diff_profiles(Path(args[0]), Path(args[1]))


def _tag_from_path(path: Path) -> str:
    """iOS tag from a profile filename, e.g. 'iPhone12,3_27.0b2.yaml' -> '27.0b2'."""
    stem = path.stem
    return stem.split("_", 1)[1] if "_" in stem else stem


def _pending_section(section: Any) -> Any:
    """Deep-copy a patches section, zeroing every offset to the sentinel and
    marking each entry `pending: true` (offsets not yet discovered for this device)."""
    out = copy.deepcopy(section)
    if isinstance(out, dict):
        for entry in out.values():
            if isinstance(entry, dict) and "offset" in entry:
                entry["offset"] = SENTINEL
                entry["pending"] = True
            elif isinstance(entry, dict):
                for sub in entry.values():
                    if isinstance(sub, dict) and "offset" in sub:
                        sub["offset"] = SENTINEL
                        sub["pending"] = True
    elif isinstance(out, list):
        for entry in out:
            if isinstance(entry, dict) and "offset" in entry:
                entry["offset"] = SENTINEL
                entry["pending"] = True
    return out


def _find_pending_entry(section: Any, name: str) -> dict | None:
    """Locate an entry by name inside a (dict- or list-style) patches section."""
    if isinstance(section, dict):
        entry = section.get(name)
        return entry if isinstance(entry, dict) else None
    if isinstance(section, list):
        for entry in section:
            if isinstance(entry, dict) and entry.get("name") == name:
                return entry
    return None


def _discover_section(pending_section: Any, base_raw: bytes, target_raw: bytes,
                      base_section: Any, sec: str) -> int:
    """Cross-device pattern search: locate each base patch site in the target
    device's binary via the AArch64 fingerprint engine. Accepts only hits at
    confidence >= 0.90 (hard rule); anything else stays pending."""
    from migrate import normalize_section
    from fingerprint import migrate_site

    base_entries = normalize_section({"patches": {sec: base_section}}, sec)
    filled = 0
    for name, bentry in base_entries.items():
        r = migrate_site(base_raw, target_raw, bentry["offset"], name=name)
        if r.target_offset is not None and r.confidence >= PENDING_CONFIDENCE:
            entry = _find_pending_entry(pending_section, name)
            if entry is not None:
                entry["offset"] = r.target_offset
                entry["pending"] = False
                entry["method"] = "cross-device"
                entry["confidence"] = round(r.confidence, 2)
                if r.suggested_value:
                    entry["value"] = r.suggested_value
                    entry["value_recomputed"] = True
                filled += 1
                print(f"    {C.GRN}✓{C.NC} {sec}.{name}: 0x{r.target_offset:X}  (conf {r.confidence:.2f})")
            else:
                print(warn(f"    {sec}.{name}: matched 0x{r.target_offset:X} but entry missing in profile"))
        else:
            why = f"conf {r.confidence:.2f}" if r.target_offset is not None else "no hit"
            print(warn(f"    {sec}.{name}: {why} — left pending"))
    return filled


def cmd_fill(args: list[str]):
    """fill <profile.yaml> --from <base.yaml> --comp-dir DIR [--sections ibss,ibec,txm]

    Completes the *pending* sections of an existing profile by fingerprinting a
    verified base profile's patch sites into this device's own components. Used
    for devices we have components for but no upstream offsets (e.g. iPad 9).
    """
    import migrate
    from migrate import SECTION_TO_COMPONENT

    if not args:
        print(err("Usage: profile_gen.py fill <profile.yaml> --from <base.yaml> "
                  "--comp-dir DIR [--sections ibss,ibec,txm] [--json]"))
        return 1

    profile_path = Path(args[0])
    base_path, comp_dir, only = None, None, []
    as_json = "--json" in args
    rest = [a for a in args[1:] if a != "--json"]
    i = 0
    while i < len(rest):
        flag, value = rest[i], (rest[i + 1] if i + 1 < len(rest) else "")
        if flag == "--from" and value:
            base_path = Path(value)
            i += 2
        elif flag == "--comp-dir" and value:
            comp_dir = Path(value)
            i += 2
        elif flag == "--sections" and value:
            only = [s.strip() for s in value.split(",") if s.strip()]
            i += 2
        else:
            i += 1

    if not profile_path.is_file():
        print(err(f"No such profile: {profile_path}"))
        return 1
    if base_path is None or comp_dir is None:
        print(err("--from and --comp-dir are required"))
        return 1

    profile = yaml.safe_load(profile_path.read_text())
    base = yaml.safe_load(base_path.read_text())
    model = profile.get("model", "?")

    # sections that are still all-pending in the profile
    pending_sections = []
    for sec, body in (profile.get("patches") or {}).items():
        if sec not in SECTION_TO_COMPONENT:
            continue
        entries = body if isinstance(body, list) else list(body.values())
        if entries and all(isinstance(e, dict) and e.get("pending") for e in entries):
            pending_sections.append(sec)
    targets = only or pending_sections

    section("Fill pending sections: %s" % model)
    print(info(f"profile: {profile_path.name}   from: {base_path.name}   "
               f"pending: {', '.join(pending_sections) or 'none'}"))

    comps = migrate.load_components(comp_dir)
    report: dict = {}
    for sec in targets:
        comp = SECTION_TO_COMPONENT.get(sec)
        base_raw, target_raw = comps.base.get(comp), comps.target.get(comp)
        if not isinstance(base_raw, bytes) or not isinstance(target_raw, bytes):
            print(warn(f"    {sec}: components missing (base={bool(base_raw)}, "
                       f"target={bool(target_raw)}) — skipped"))
            report[sec] = {"filled": 0, "skipped": "components missing"}
            continue
        base_section = (base.get("patches") or {}).get(sec)
        if base_section is None:
            print(warn(f"    {sec}: base profile has no {sec} section — skipped"))
            report[sec] = {"filled": 0, "skipped": "no base section"}
            continue
        print(f"    {C.DIM}{sec} ({comp}: base {len(base_raw)} B → target {len(target_raw)} B){C.NC}")
        filled = _discover_section(profile["patches"][sec], base_raw, target_raw,
                                   base_section, sec)
        report[sec] = {"filled": filled,
                       "total": len(base_section) if isinstance(base_section, (dict, list)) else 0}

    # provenance on the profile itself
    profile.setdefault("provenance", {})
    profile["provenance"]["filled_from"] = base_path.name
    profile["provenance"]["fill_method"] = "cross-device fingerprint (>=0.90)"
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False, width=100))

    total = sum(v.get("filled", 0) for v in report.values())
    print()
    print(ok(f"{total} offset(s) discovered and written to {profile_path}"))
    if as_json:
        print(json.dumps({"model": model, "profile": str(profile_path),
                          "sections": report, "filled": total}, indent=2))
    return 0


def cmd_propagate(args: list[str]):
    """propagate <base.yaml> <model> [--ios V] [--build B] [--comp-dir DIR]
    [--overwrite] [--force]

    Generates a profile for another device of the same SoC from a verified base:
    SoC-shared sections (kernel, daemons, restoreramdisk, devicetree, userland)
    are copied; device-specific sections (iBSS/iBEC/TXM) become pending sentinels.
    With --comp-dir (base/ + target/ raw components), iBSS/iBEC/TXM offsets are
    auto-discovered via cross-device AArch64 fingerprinting (>= 0.90 confidence).
    """
    base_path = Path(args[0]) if args else None
    model = args[1] if len(args) > 1 else ""
    ios_tag, build, comp_dir = "", "", None
    overwrite = force = False
    i = 2
    while i < len(args):
        a = args[i]
        if a == "--ios" and i + 1 < len(args):
            ios_tag, i = args[i + 1], i + 2
        elif a == "--build" and i + 1 < len(args):
            build, i = args[i + 1], i + 2
        elif a == "--comp-dir" and i + 1 < len(args):
            comp_dir, i = Path(args[i + 1]), i + 2
        elif a == "--overwrite":
            overwrite, i = True, i + 1
        elif a == "--force":
            force, i = True, i + 1
        else:
            print(err(f"Unknown argument: {a}"))
            return

    if not base_path or not model:
        print(err("Usage: propagate <base.yaml> <model> [--ios V] [--build B] [--comp-dir DIR] [--overwrite] [--force]"))
        return
    if not base_path.exists():
        print(err(f"Base profile not found: {base_path}"))
        return
    try:
        with open(base_path) as f:
            base = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(err(f"Invalid base profile: {e}"))
        return
    if not isinstance(base, dict) or "patches" not in base:
        print(err("Base profile is missing a 'patches' section"))
        return

    if base.get("verification") == "pending" and not force:
        print(err(f"Base profile {base_path.name} is itself pending — refusing to propagate unverified data. Use --force to override."))
        return

    dev = DEVICE_DB.get(model)
    if not dev:
        print(warn(f"Device {model} not in database — using defaults (SoC unknown)"))
        dev = {"name": model, "soc": "?", "board": "unknown", "apticket": "?"}

    base_soc = base.get("soc", "?")
    if dev["soc"] != base_soc and not force:
        print(err(f"SoC mismatch: base is {base_soc}, target {model} is {dev['soc']} — "
                  f"kernel/daemon offsets are NOT shared across SoCs. Use --force to proceed anyway."))
        return

    base_kernel = DEVICE_DB.get(base.get("model", ""), {}).get("kernel_component")
    target_kernel = dev.get("kernel_component")
    if base_kernel and target_kernel and base_kernel != target_kernel and not force:
        print(err(f"kernel component mismatch: {base.get('model', '?')} ships {base_kernel}, "
                  f"{model} ships {target_kernel}."))
        print(f"  {C.DIM}Kernel partition offsets live in the kernelcache binary, which is a"
              f" different file per board ({base_kernel} != {target_kernel}) — copying them"
              f" would patch the wrong addresses. Discover them from the target's own"
              f" kernelcache (python3 fetch_components.py ... --entries kernelcache)"
              f" or pass --force.{C.NC}")
        return

    tag = ios_tag or _tag_from_path(base_path)
    out_path = OFFSETS_DIR / f"{model}_{tag}.yaml"
    if out_path.exists() and not overwrite:
        ans = log_utils.safe_input(f"  {C.AMB}{out_path.name} already exists. Overwrite? [y/N]:{C.NC} ") or "n"
        if ans.lower() not in ("y", "yes"):
            print(info("Cancelled"))
            return

    base_patches = base["patches"]
    patches: dict = {}

    # SoC-shared sections — copy verbatim from the verified base
    shared = []
    for sec in SHARED_SECTIONS:
        if sec in base_patches:
            patches[sec] = copy.deepcopy(base_patches[sec])
            shared.append(sec)

    # Device-specific sections — sentinel offsets, pending marker
    pending_sections = []
    for sec in DEVICE_SECTIONS:
        if sec in base_patches:
            patches[sec] = _pending_section(base_patches[sec])
            pending_sections.append(sec)

    profile = {
        "device": dev["name"],
        "model": model,
        "ios_version": tag,
        "build": build or base.get("build", "unknown"),
        "soc": dev["soc"],
        "board": dev["board"],
        "apticket": dev["apticket"],
        "verification": "pending",
        "propagated_from": base_path.name,
        "propagated": {
            "base": f"{base.get('device', '?')} ({base.get('model', '?')})",
            "soc": base_soc,
            "build": build or base.get("build", "unknown"),
            "shared_sections": shared,
            "pending_sections": pending_sections,
            "note": ("kernel/daemons/restoreramdisk offsets are SoC-shared for this build "
                     "(kernelcache.release.<soc>); iBSS/iBEC/TXM are device-specific "
                     "bootloader binaries — discover them before flashing."),
        },
        "patches": patches,
    }

    print(section(f"Propagate → {dev['name']} ({model})"))
    print(f"  base:  {C.SNOW}{base.get('device', '?')} ({base.get('model', '?')}){C.NC} — {base_soc} · {base.get('ios_version', '?')}")
    print(f"  copy:  {C.GRN}{', '.join(shared)}{C.NC}")
    print(f"  pend:  {C.AMB}{', '.join(pending_sections)}{C.NC} (sentinel offsets)")

    if comp_dir:
        try:
            from migrate import load_components
            comps = load_components(comp_dir)
        except SystemExit as e:
            print(warn(f"--comp-dir unusable: {e}"))
            comps = None
        if comps:
            print()
            print(section("Cross-device discovery (--comp-dir)"))
            for sec in DEVICE_SECTIONS:
                if sec not in patches:
                    continue
                base_raw = comps.base.get(sec)
                target_raw = comps.target.get(sec)
                if base_raw is None or target_raw is None:
                    print(warn(f"  {sec}: base/ or target/ component missing — skipping"))
                    continue
                print(f"  {C.EYE}{sec}:{C.NC} fingerprinting target binary…")
                filled = _discover_section(patches[sec], base_raw, target_raw, base_patches[sec], sec)
                if filled == 0:
                    print(warn(f"  {sec}: nothing matched ≥0.90 — all entries stay pending"))

    from device_offsets import dump_profile_yaml, validate_offsets, pending_entries
    dump_profile_yaml(profile, out_path)
    print()
    print(ok(f"Wrote {out_path.name} — verification: pending"))
    passed, failed, errors = validate_offsets(out_path)
    pend = pending_entries(out_path)
    print(info(f"Post-write validation: {passed} valid · {failed} failed · {pend} pending"))
    for e in errors:
        print(f"    {C.AMB}{e}{C.NC}")
    print()
    print(section("Next steps"))
    print(f"  {C.EYE}[1]{C.NC} Discover the pending iBSS/iBEC/TXM offsets:")
    print(f"      python3 profile_gen.py propagate {base_path.name} {model} --comp-dir extracted/")
    print(f"  {C.EYE}[2]{C.NC} Verify shared kernel offsets against the target kernelcache before flashing:")
    print(f"      research/extract.sh + diff (see research/README.md)")
    print(f"  {C.EYE}[3]{C.NC} The profile activates only once nothing is pending.")


GAP_SECTIONS = ("ibss", "ibec", "kernel", "txm", "restoreramdisk", "daemons")


def section_stats(profile: dict) -> dict[str, tuple[int, int]]:
    """section -> (filled, total) counting every entry the profile defines.

    A pending (sentinel) entry counts as unfilled, so the numbers describe how
    complete each profile is on its own terms.
    """
    import yaml as _yaml  # noqa: F401  (kept for symmetry with loaders)

    def walk(name: str, entry) -> tuple[int, int]:
        if not isinstance(entry, dict):
            return 0, 0
        if "offset" in entry and "value" in entry:
            filled = 0 if entry.get("pending") else 1
            return filled, 1
        filled = total = 0
        for sub_name, sub in entry.items():
            f, tt = walk(f"{name}.{sub_name}", sub)
            filled += f
            total += tt
        return filled, total

    stats: dict[str, tuple[int, int]] = {}
    for sec, data in (profile.get("patches") or {}).items():
        filled = total = 0
        if isinstance(data, list):
            for entry in data:
                f, tt = walk(sec, entry)
                filled += f
                total += tt
        elif isinstance(data, dict):
            for name, entry in data.items():
                f, tt = walk(name, entry)
                filled += f
                total += tt
        if total:
            stats[sec] = (filled, total)
    return stats


def coverage_data() -> dict:
    """Machine-readable coverage: devices, profiles, per-section gaps."""
    from device_offsets import pending_entries, validate_offsets

    devices = []
    for model, dev_info in sorted(DEVICE_DB.items()):
        files = sorted(OFFSETS_DIR.glob(f"{model}_*.yaml"))
        profiles = []
        for f in files:
            passed, failed, _ = validate_offsets(f)
            pend = pending_entries(f)
            with open(f) as fh:
                data = yaml.safe_load(fh) or {}
            stats = section_stats(data)
            status = "incomplete" if failed else ("pending" if pend else "ready")
            profiles.append({
                "file": f.name,
                "tag": _tag_from_path(f),
                "ios_version": data.get("ios_version", ""),
                "build": data.get("build", ""),
                "status": status,
                "patches_ok": passed,
                "pending": pend,
                "kernel_component": dev_info.get("kernel_component", ""),
                "sections": {sec: {"filled": filled, "total": total}
                             for sec, (filled, total) in stats.items()},
            })
        devices.append({
            "model": model,
            "name": dev_info["name"],
            "soc": dev_info["soc"],
            "board": dev_info["board"],
            "kernel_component": dev_info.get("kernel_component", ""),
            "profiles": profiles,
        })
    ready = sum(1 for d in devices for p in d["profiles"] if p["status"] == "ready")
    total = sum(1 for d in devices for p in d["profiles"])
    return {
        "devices": devices,
        "summary": {
            "devices_known": len(devices),
            "devices_with_profiles": sum(1 for d in devices if d["profiles"]),
            "profiles": total,
            "profiles_ready": ready,
        },
        "kernel_components": {m: i.get("kernel_component", "")
                              for m, i in DEVICE_DB.items() if i.get("kernel_component")},
    }


def kernel_component_mismatch(profile_file: Path) -> tuple[str, str]:
    """(own, source) kernel components when a profile's kernel was propagated
    from a board that ships a different kernelcache binary, else ("", "")."""
    with open(profile_file) as fh:
        data = yaml.safe_load(fh) or {}
    own = DEVICE_DB.get(str(data.get("model", "")), {}).get("kernel_component", "")
    source_file = str(data.get("propagated_from", ""))
    if not (own and source_file):
        return "", ""
    source_path = OFFSETS_DIR / source_file
    if not source_path.exists():
        return own, ""
    with open(source_path) as fh:
        source = yaml.safe_load(fh) or {}
    source_comp = DEVICE_DB.get(str(source.get("model", "")), {}).get("kernel_component", "")
    if source_comp and source_comp != own:
        return own, source_comp
    return "", ""


def cmd_gaps(json_out: bool = False):
    """Per-profile, per-section gap matrix (which parts still need offsets)."""
    data = coverage_data()
    flagged = []
    for dev in data["devices"]:
        for prof in dev["profiles"]:
            own, source = kernel_component_mismatch(OFFSETS_DIR / prof["file"])
            if own:
                prof["kernel_component_mismatch"] = {"own": own, "source": source}
                flagged.append((prof["file"], own, source))
    if json_out:
        print(json.dumps({"profiles": [dict(p, model=d["model"], device=d["name"])
                                       for d in data["devices"] for p in d["profiles"]],
                          "kernel_component_mismatches": [
                              {"file": f, "own": o, "source": s} for f, o, s in flagged]},
                         indent=2))
        return

    print(section("Offset Gaps by Section"))
    print()
    header = f"  {'Profile':<26}" + "".join(f"{s[:6]:>8}" for s in GAP_SECTIONS)
    print(f"{C.EYE}{header}{C.NC}")
    print(f"  {'─' * (26 + 8 * len(GAP_SECTIONS))}")
    for dev in data["devices"]:
        for prof in dev["profiles"]:
            cells = []
            for sec in GAP_SECTIONS:
                stat = prof["sections"].get(sec)
                if not stat:
                    cells.append(f"{'—':>8}")
                    continue
                text = f"{stat['filled']}/{stat['total']}"
                color = C.GRN if stat["filled"] == stat["total"] else C.AMB
                cells.append(f"{color}{text:>8}{C.NC}")
            label = f"{dev['model']} {prof['tag']}"
            print(f"  {C.SNOW}{label:<26}{C.NC}" + "".join(cells))
    if flagged:
        print()
        print(f"  {C.AMB}kernel sections copied from another board's kernelcache "
              f"({len(flagged)} profile(s)):{C.NC}")
        for file, own, source in flagged:
            print(f"    {C.DIM}{file}{C.NC} {C.AMB}{source or '?'} -> needs {own}{C.NC}")
        print(f"  {C.DIM}kernel patch offsets live in the board's own kernelcache binary; "
              f"re-discover them from that component{C.NC}")
    print()
    print(f"  {C.DIM}filled/total entries per profile section · a pending entry counts as "
          f"unfilled{C.NC}")
    print()


def cmd_coverage(json_out: bool = False):
    """Coverage table: every known device × profile status."""
    from device_offsets import validate_offsets, pending_entries

    if json_out:
        print(json.dumps(coverage_data(), indent=2))
        return

    print(section("Offset Profile Coverage"))
    print()
    print(f"  {C.EYE}{'Device':<24}{'Model':<12}{'SoC':<5}{'Profiles':<16}Status{C.NC}")
    print(f"  {'─' * 62}")
    n_devices = n_with_profiles = n_ready = 0
    for model, dev_info in sorted(DEVICE_DB.items()):
        n_devices += 1
        files = sorted(OFFSETS_DIR.glob(f"{model}_*.yaml"))
        if not files:
            print(f"  {dev_info['name']:<24}{model:<12}{dev_info['soc']:<5}{'—':<16}{C.DIM}no profile{C.NC}")
            continue
        n_with_profiles += 1
        tags, statuses = [], set()
        for f in files:
            passed, failed, _ = validate_offsets(f)
            pend = pending_entries(f)
            tags.append(_tag_from_path(f))
            if failed > 0:
                statuses.add("incomplete")
            elif pend > 0:
                statuses.add("pending")
            else:
                statuses.add("ready")
                n_ready += 1
        label = ", ".join(tags)
        if statuses == {"ready"}:
            icon, col = "✓ ready", C.GRN
        elif "pending" in statuses:
            icon, col = "⚠ pending", C.AMB
        else:
            icon, col = "✗ incomplete", C.RED
        print(f"  {dev_info['name']:<24}{model:<12}{dev_info['soc']:<5}{label:<16}{col}{icon}{C.NC}")
    print(f"  {'─' * 62}")
    print(f"  {C.DIM}{n_with_profiles}/{n_devices} devices have profiles · {n_ready} ready to flash{C.NC}")


def cmd_list_templates(json_out: bool = False):
    """List all known device models in the database."""
    if json_out:
        print(json.dumps({
            "devices": [{"model": m, "name": i["name"], "soc": i["soc"],
                         "board": i["board"], "apticket": i["apticket"],
                         "kernel_component": i.get("kernel_component", ""),
                         "profiles": [f.name for f in sorted(OFFSETS_DIR.glob(f"{m}_*.yaml"))]}
                        for m, i in sorted(DEVICE_DB.items())],
        }, indent=2))
        return
    print(section("Device Database"))
    print()
    for model, dev_info in sorted(DEVICE_DB.items()):
        has_profile = list(OFFSETS_DIR.glob(f"{model}_*.yaml"))
        status = C.GRN + "✓" if has_profile else C.AMB + "⚠"
        kernel = dev_info.get("kernel_component", "kernelcache.release.?")
        print(f"  {status}{C.NC} {C.EYE}{dev_info['name']:<22}{C.NC} {C.DIM}{model:<12}{C.NC} "
              f"[{dev_info['soc']}]  {dev_info['board']:<9} {C.DIM}{kernel}{C.NC}")


# ── bootstrap: the run sheet for a device that has no offsets yet ───
# (checklist O1.3) A brand new device has no same-device base profile, so the
# fingerprint engine has nothing to work from and the honest deliverable is a
# procedure, not a number. Every fact in the sheet is read out of the repo
# (DEVICE_DB, offsets/, research/extracted/), never restated from
# docs/BOOTSTRAPPING.md: a sheet that names a component the board does not ship
# is exactly how a profile ends up pointing at the wrong binary.

CONFIDENCE_TIERS = (
    ("0.95", "unique masked hit AND the disassembly class matches"),
    ("0.90", "unique string site"),
    ("0.60", "ambiguous, or the instruction class does not match"),
    ("0.30", "multi-hit or delta-inferred - always LOW, never auto-written"),
)


def _list_phrase(names: list[str]) -> str:
    """'A ships' / 'A and B ship': one sibling must not read as a plural list."""
    if not names:
        return ""
    if len(names) == 1:
        return f"{names[0]} ships"
    return ", ".join(names[:-1]) + f" and {names[-1]} ship"


def _repo_relative(path: Path) -> str:
    """Path relative to the repo when it is inside it, else the path itself.

    Components can live outside the checkout (a fixture dir, an external
    research volume), and relative_to() raises on those: a run sheet is not
    allowed to fall over on a path it was handed.
    """
    try:
        return str(path.relative_to(SCRIPT_DIR))
    except ValueError:
        return str(path)


def component_dir_for(model: str, ios: str, build: str) -> Path | None:
    """research/extracted/<Model>_<ios>_<build>/ when those components are
    already on disk, else None.

    Reuses preflight's lookup so the run sheet names the directory the verifier
    will actually read. preflight imports this module for DEVICE_DB, so the
    import has to stay inside the function.
    """
    import preflight
    return preflight.discovered_component_dir(
        {"model": model, "ios_version": ios, "build": build})


def profile_rows(model: str) -> list[dict]:
    """This model's existing profiles, oldest tag first, with their status."""
    from device_offsets import pending_entries, validate_offsets

    rows = []
    for path in sorted(OFFSETS_DIR.glob(f"{model}_*.yaml")):
        passed, failed, _ = validate_offsets(path)
        pend = pending_entries(path)
        rows.append({
            "file": path.name,
            "tag": _tag_from_path(path),
            "status": "incomplete" if failed else ("pending" if pend else "ready"),
            "patches_ok": passed,
            "pending": pend,
        })
    return rows


def board_siblings(model: str, key: str) -> list[str]:
    """Other models sharing one component with this board.

    `key` is "kernel_component" (boards shipping the same kernelcache binary may
    share a kernel section) or "ibss_component" (boards sharing the bootloader
    image, e.g. the four j21x iPad boards that all boot iBSS.j210).
    """
    own = DEVICE_DB[model].get(key, "")
    if not own:
        return []
    return sorted(m for m, info in DEVICE_DB.items()
                  if m != model and info.get(key) == own)


def newest_ready_sibling_profile(model: str) -> dict | None:
    """Newest 'ready' profile of a board that shares this board's bootloader
    image, for the `fill` path (that is how iPad12,1/12,2 got iBSS/iBEC/TXM)."""
    candidates = []
    for sibling in board_siblings(model, "ibss_component") + board_siblings(model, "kernel_component"):
        for row in profile_rows(sibling):
            if row["status"] == "ready":
                candidates.append(dict(row, model=sibling))
    if not candidates:
        return None
    return max(candidates, key=lambda row: (row["tag"], row["model"]))


def bootstrap_data(model: str, ios: str, build: str = "") -> dict | None:
    """The staged first-offset run sheet, or None for a model DEVICE_DB does not
    know (printing a component name for an unknown board is the failure mode
    this command exists to prevent)."""
    dev = DEVICE_DB.get(model)
    if dev is None:
        return None

    stem = dev.get("ibss_component", "")
    kernel = dev.get("kernel_component", "")
    profiles = profile_rows(model)
    newest = profiles[-1] if profiles else None
    profile_name = f"{model}_{ios}.yaml"
    target = OFFSETS_DIR / profile_name
    comp_dir = component_dir_for(model, ios, build)
    kc_siblings = board_siblings(model, "kernel_component")
    stem_siblings = board_siblings(model, "ibss_component")
    fill_base = newest_ready_sibling_profile(model)
    build_arg = build or "<build>"

    notes = []
    if dev["soc"] == "A12":
        notes.append("iOS 27 dropped the A12 iPhones: the XS/XS Max/XR and the A12 iPads "
                     "have no 27.x IPSW, so this profile is a 26.x build.")
    if newest:
        notes.append(f"{model} already has {len(profiles)} profile(s) "
                     f"({', '.join(p['tag'] for p in profiles)}): carry the sites over "
                     f"with `migrate` from {newest['file']} instead of discovering them again.")
    else:
        notes.append(f"{model} has no profile at all, so there is no same-device base to "
                     f"fingerprint from: an upstream script, Liter8 fixtures, or a sibling "
                     f"board are the only sources.")
    if not kc_siblings:
        notes.append(f"no other board in the device database ships {kernel or 'this kernelcache'}, "
                     f"so the kernel section can only come from this board's own component.")
    if comp_dir:
        notes.append(f"components already on disk: {_repo_relative(comp_dir)}")

    # ── the site sources, best first for THIS device ──
    sources = []
    if newest:
        sources.append({
            "label": f"carry the sites over from the same device ({newest['file']})",
            "commands": [
                "mkdir -p research/comp-dir/base research/comp-dir/target",
                f"# base/   = raw components of {newest['tag']}",
                f"# target/ = raw components of {ios} {build or ''}".rstrip(),
                f"python3 profile_gen.py migrate offsets/{newest['file']} "
                f"offsets/{profile_name} --comp-dir research/comp-dir "
                f"--checkpoint research/work/ck.json --json > /tmp/m.json",
            ],
            "expected": "0.95 for a unique hit whose disasm class matches, 0.90 for a unique "
                        "string site. The recorded b2->b3 run for iPhone12,3 came out 15 HIGH "
                        "and 23 unresolved (exit 2), so read `review` and `not_written` "
                        "before --auto",
        })
    if fill_base:
        sources.append({
            "label": f"fingerprint a sibling's bootloader sites "
                     f"({fill_base['model']} {fill_base['tag']} shares iBSS.{stem})",
            "commands": [
                f"python3 profile_gen.py fill offsets/{profile_name} "
                f"--from offsets/{fill_base['file']} --comp-dir research/comp-dir "
                f"--sections ibss,ibec,txm",
            ],
            "expected": "0.95 per unique hit with a matching disasm class; that is how "
                        "iPad12,1/12,2 got 15 bootloader offsets each. Entries it cannot "
                        "place stay pending, and nothing below 0.90 is written",
        })
    sources.append({
        "label": "an upstream script or Liter8 fixtures for this exact build and board",
        "commands": [
            f"python3 source_audit.py script <usbliter8-fun>/work-{build or '<build>'}/make_cfw.py "
            f"offsets/{profile_name}",
            f"python3 liter8_import.py --fixtures <Liter8>/fixtures --build {build_arg} "
            f"--board {dev['board']} --model {model} --ios {ios} "
            f"--verify-components research/extracted/<dir>",
        ],
        "expected": "COVERED / PARTIAL / MISMATCH / REVIEW / MISSING / PROFILE-ONLY per entry. "
                    "MISMATCH and MISSING have to be explained before moving on; REVIEW on "
                    "PC-relative sites (adrp/add redirects) is expected, because the immediate "
                    "depends on the site's own address. liter8_import prints a dry run until "
                    "--write, so add it only once --verify-components reads clean",
    })
    if not newest:
        sources.append({
            "label": "cross-device fingerprinting (last resort)",
            "commands": [
                "python3 profile_gen.py fill offsets/<Model>_<base>.yaml --from "
                "offsets/<sibling>.yaml --comp-dir research/comp-dir --sections ibss,ibec,txm",
            ],
            "expected": "expect 0.60 and 0.30 for most entries across SoC families: every hit "
                        "is a candidate a human has to confirm, and the honest outcome with no "
                        "same-device source at all is a `blockers:` entry, not a filled section",
        })
    for index, source in enumerate(sources):
        source["recommended"] = index == 0
    source_lines = {f"{i + 1}": s["label"] for i, s in enumerate(sources)}

    stages = [
        {
            "id": 1,
            "title": "name the components before anything else",
            "detail": f"the entry name is the component stem, never the board id: "
                      f"{dev['board']} boots iBSS.{stem}.RELEASE.im4p",
            "commands": [
                f"python3 fetch_components.py --device {model} --build {build_arg} --list",
            ],
            "expected": f"iBSS.{stem}.RELEASE.im4p and {kernel} in the listing. If either is "
                        f"missing, add the DEVICE_DB row (name, soc, board, apticket, "
                        f"ibss_component, kernel_component) and the offsets/sources.yaml "
                        f"kernel_components row first",
        },
        {
            "id": 2,
            "title": "create the profile from the template",
            "commands": [
                f"python3 profile_gen.py create {model} {ios} {build or 'unknown'}",
                f"$EDITOR offsets/{profile_name}",
            ],
            "expected": "every entry comes up pending: true, so a half-filled profile cannot "
                        "look flashable. `validate` exits 0 for pending entries and 1 for "
                        "invalid ones",
        },
        {
            "id": 3,
            "title": "pull only the components you need",
            "commands": [
                (f"# already fetched: {_repo_relative(comp_dir)} (add --refresh to re-download)"
                 if comp_dir else
                 f"python3 fetch_components.py --device {model} --ios {ios} --build {build_arg} "
                 f"--all --jobs 4 --extract-payload"),
            ],
            "expected": "a few MB of HTTP range requests, not a 6 GB IPSW. provenance.json "
                        "records the sha256 of the file AND of the payload: offsets index the "
                        "decompressed payload, so an evidence comparison uses that hash, not "
                        "the container's (compare containers and everything always differs)",
        },
        {
            "id": 4,
            "title": f"get the literal sites for this build and board ({len(sources)} source(s))",
            "detail": "; ".join(f"{k}) {v}" for k, v in source_lines.items()),
            "commands": [cmd for source in sources for cmd in
                         [f"# {i + 1}) {s['label']}" for i, s in enumerate(sources)
                          if s is source] + source["commands"]],
            "expected": " | ".join(f"{k}) {s['expected']}" for k, s in zip(source_lines, sources)),
            "sources": sources,
        },
        {
            "id": 5,
            "title": "kernel section: this board's own component, or a blocker",
            "commands": [
                f"python3 fetch_components.py --device {model} --build {build_arg} "
                f"--entries kernel --list",
                "# no published IV+key for that SoC and build -> record it, never invent values:",
                "blockers:",
                "  kernel:",
                "    entries: <n>",
                f"    reason: kernelcache offsets are per component: this board boots {kernel}, "
                f"and no verified offsets exist for it",
                "    needs: a contributor with this device, or a published key for that kernelcache",
            ],
            "expected": (f"{kernel} only; "
                         f"{_list_phrase(kc_siblings)} the same component and may share the "
                         f"section" if kc_siblings else
                         f"nothing may inherit this section, because no other board ships "
                         f"{kernel}") +
                        ". Blocked entries count as unresolved everywhere and patch_kernel "
                        "refuses them even under --force (the build manifest reports them)",
        },
        {
            "id": 6,
            "title": "the verification gate: nothing counts before this",
            "commands": [
                f"python3 device_offsets.py validate offsets/{profile_name}",
                f"python3 preflight.py offsets/{profile_name} --quiet",
                f"python3 preflight.py offsets/{profile_name} --record",
                "python3 profile_gen.py gaps",
                "python3 safety_check.py",
                "python3 -m pytest tests/ -q",
            ],
            "expected": "preflight records the component sha256 plus the original bytes at every "
                        "verified site into offsets/evidence/<profile>.json, which is committed "
                        "with the profile and makes it self-verifying. Flip verification: to "
                        "verified only with no pending entries and no blockers left: "
                        "safety_check.py fails the push on that claim otherwise",
        },
        {
            "id": 7,
            "title": "send it in",
            "commands": [
                "python3 contribute.py status",
                f"python3 contribute.py pr offsets/{profile_name}",
            ],
            "expected": "a branch and a PR against main (direct pushes are refused). The "
                        "required `tests` job runs safety_check.py, every profile through "
                        "device_offsets.py validate and the suite on 3.13 and 3.9",
        },
    ]

    return {
        "model": model,
        "device": dev["name"],
        "soc": dev["soc"],
        "board": dev["board"],
        "apticket": dev["apticket"],
        "ios": ios,
        "build": build,
        "component_stem": stem,
        "ibss_component": f"iBSS.{stem}.RELEASE.im4p" if stem else "",
        "kernel_component": kernel,
        "profile_path": str(Path("offsets") / profile_name),
        "profile_exists": target.exists(),
        "component_dir": _repo_relative(comp_dir) if comp_dir else "",
        "existing_profiles": profiles,
        "kernel_siblings": kc_siblings,
        "stem_siblings": stem_siblings,
        "fill_base": fill_base["file"] if fill_base else "",
        "notes": notes,
        "confidence_tiers": [{"confidence": c, "means": m} for c, m in CONFIDENCE_TIERS],
        "stages": stages,
    }


def cmd_bootstrap(args: list[str]) -> int:
    """bootstrap <Model> <iOS> [build] [--json] - print the run sheet."""
    argv = [a for a in args if a != "--json"]
    json_out = "--json" in args
    model = argv[0] if argv else ""
    ios = argv[1] if len(argv) > 1 else ""
    build = argv[2] if len(argv) > 2 else ""
    if not model or not ios:
        print(err("Usage: bootstrap <Model> <iOS> [build] [--json]"))
        print(f"  {C.DIM}e.g. python3 profile_gen.py bootstrap iPhone11,6 26.0 24A100{C.NC}")
        return 1

    data = bootstrap_data(model, ios, build)
    if data is None:
        print(err(f"Unknown device: {model}"))
        print(info("known models: " + ", ".join(sorted(DEVICE_DB))))
        return 1

    if json_out:
        print(json.dumps(data, indent=2))
        return log_utils.EXIT_OK

    import textwrap
    print(section(f"Bootstrap: {data['device']} on iOS {ios}"))
    print()
    print(f"  {C.EYE}{data['device']}{C.NC} {C.SNOW}{model}{C.NC}  "
          f"{C.SNOW}iOS {ios}{C.NC}" + (f" {C.SNOW}build {build}{C.NC}" if build else ""))
    print(f"  {C.DIM}{data['soc']} · {data['board']} · {data['apticket']} · "
          f"{data['ibss_component']} · {data['kernel_component']}{C.NC}")
    state = "exists" if data["profile_exists"] else "does not exist yet"
    print(f"  {C.DIM}profile target: {data['profile_path']} ({state}){C.NC}")
    for note in data["notes"]:
        for index, line in enumerate(textwrap.wrap(note, 74)):
            bullet = f"{C.AMB}!{C.NC} " if index == 0 else "  "
            print(f"  {bullet}{C.DIM}{line}{C.NC}")

    for stage in data["stages"]:
        print()
        print(f"  {C.FROST}{C.B}{stage['id']}. {stage['title']}{C.NC}")
        if stage.get("detail"):
            for line in textwrap.wrap(stage["detail"], 70):
                print(f"      {C.DIM}{line}{C.NC}")
        commands = stage.get("commands", [])
        for command in commands:
            colour = C.DIM if command.startswith("#") else C.SNOW
            print(f"      {colour}{command}{C.NC}")
        if stage.get("expected"):
            for line in textwrap.wrap("expected: " + stage["expected"], 68):
                print(f"      {C.DIM}{line}{C.NC}")

    print()
    print(f"  {C.DIM}confidence floor: nothing below 0.90 may be written automatically{C.NC}")
    for tier, means in CONFIDENCE_TIERS:
        print(f"    {C.EYE}{tier}{C.NC} {C.DIM}{means}{C.NC}")
    print()
    print(f"  {C.DIM}same sheet as JSON: --json (one object on stdout, machine readable){C.NC}")
    print()
    return log_utils.EXIT_OK


def _cli() -> int:
    """Command line entry point: whatever it raises, guard() turns into an exit code."""
    import log_utils
    log_utils.install()          # usbliter8.log + unhandled-exception logging
    if len(sys.argv) < 2:
        print(f"\n  {C.FROST}usbliter8 profile generator{C.NC}\n")
        print(f"  Commands:")
        print(f"    {C.EYE}create{C.NC}    <Model> <iOS> [build] [kernel_file]")
        print(f"    {C.EYE}propagate{C.NC} <base.yaml> <Model> [--ios V] [--build B] [--comp-dir DIR] [--overwrite] [--force]")
        print(f"    {C.EYE}diff{C.NC}      <base.yaml> <updated.yaml>")
        print(f"    {C.EYE}migrate{C.NC}   <base.yaml> <target.yaml|iOS> [--comp-dir DIR] [--auto] [--report FILE]")
        print(f"                [--json] [--checkpoint [FILE]] [--resume [FILE]]  (2 = review required)")
        print(f"    {C.EYE}coverage{C.NC}  Show per-device profile status [--json]")
        print(f"    {C.EYE}gaps{C.NC}      Per-profile, per-section gap matrix [--json]")
        print(f"    {C.EYE}bootstrap{C.NC} <Model> <iOS> [build] - the first-offset run sheet [--json]")
        print(f"    {C.EYE}list{C.NC}      Show all known devices [--json]")
        print()
        sys.exit(0)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "create":
        cmd_create(args)
    elif cmd == "fill":
        sys.exit(cmd_fill(args))
    elif cmd == "propagate":
        cmd_propagate(args)
    elif cmd == "diff":
        cmd_diff(args)
    elif cmd == "migrate":
        from migrate import cli_main
        # the exit code is the contract (2 = REVIEW REQUIRED), so it must
        # survive: dropping it made every run look like a clean success
        return cli_main(args)
    elif cmd == "coverage":
        cmd_coverage(json_out="--json" in args)
    elif cmd == "gaps":
        cmd_gaps(json_out="--json" in args)
    elif cmd == "bootstrap":
        sys.exit(cmd_bootstrap(args))
    elif cmd == "list":
        cmd_list_templates(json_out="--json" in args)
    else:
        print(err(f"Unknown command: {cmd}"))
        sys.exit(1)
    return log_utils.EXIT_OK


if __name__ == "__main__":
    import log_utils

    log_utils.install()          # usbliter8.log + clean exits

    import deps
    deps.ensure(("profiles",))          # offer to install what is missing
    sys.exit(log_utils.guard(_cli))
