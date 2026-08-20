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

import copy
import sys
import yaml
from pathlib import Path
from typing import Any

from colors import C, ok, err, warn, info, section, header

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

DEVICE_DB = {
    "iPhone11,2": {"name": "iPhone XS",           "soc": "A12", "board": "d321ap",  "apticket": "t8020"},
    "iPhone11,4": {"name": "iPhone XS Max (CN)",   "soc": "A12", "board": "d331pap", "apticket": "t8020"},
    "iPhone11,6": {"name": "iPhone XS Max",        "soc": "A12", "board": "d331ap",  "apticket": "t8020"},
    "iPhone11,8": {"name": "iPhone XR",            "soc": "A12", "board": "n841ap",  "apticket": "t8020"},
    "iPhone12,1": {"name": "iPhone 11",            "soc": "A13", "board": "n104ap",  "apticket": "t8030"},
    "iPhone12,3": {"name": "iPhone 11 Pro",        "soc": "A13", "board": "d421ap",  "apticket": "t8030"},
    "iPhone12,5": {"name": "iPhone 11 Pro Max",    "soc": "A13", "board": "d431ap",  "apticket": "t8030"},
    "iPhone12,8": {"name": "iPhone SE (2nd gen)",  "soc": "A13", "board": "d79ap",   "apticket": "t8030"},
    "iPad11,1":   {"name": "iPad mini 5 (WiFi)",   "soc": "A12", "board": "j211ap",  "apticket": "t8020"},
    "iPad11,2":   {"name": "iPad mini 5 (Cell)",   "soc": "A12", "board": "j212ap",  "apticket": "t8020"},
    "iPad11,3":   {"name": "iPad Air 3 (WiFi)",    "soc": "A12", "board": "j213ap",  "apticket": "t8020"},
    "iPad11,4":   {"name": "iPad Air 3 (Cell)",    "soc": "A12", "board": "j214ap",  "apticket": "t8020"},
    "iPad11,6":   {"name": "iPad 8 (WiFi)",        "soc": "A12", "board": "j171ap",  "apticket": "t8020"},
    "iPad11,7":   {"name": "iPad 8 (Cell)",        "soc": "A12", "board": "j172ap",  "apticket": "t8020"},
    "iPad12,1":   {"name": "iPad 9 (WiFi)",        "soc": "A13", "board": "j181ap",  "apticket": "t8030"},
    "iPad12,2":   {"name": "iPad 9 (Cell)",        "soc": "A13", "board": "j182ap",  "apticket": "t8030"},
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
        overwrite = input(f"  {C.AMB}{fname} already exists. Overwrite? [y/N]:{C.NC} ")
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

    tag = ios_tag or _tag_from_path(base_path)
    out_path = OFFSETS_DIR / f"{model}_{tag}.yaml"
    if out_path.exists() and not overwrite:
        ans = input(f"  {C.AMB}{out_path.name} already exists. Overwrite? [y/N]:{C.NC} ") or "n"
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


def cmd_coverage():
    """Coverage table: every known device × profile status."""
    from device_offsets import validate_offsets, pending_entries

    print(section("Offset Profile Coverage"))
    print()
    print(f"  {C.EYE}{'Device':<24}{'Model':<12}{'SoC':<5}{'Profiles':<16}Status{C.NC}")
    print(f"  {'─' * 62}")
    n_devices = n_with_profiles = n_ready = 0
    for model, info in sorted(DEVICE_DB.items()):
        n_devices += 1
        files = sorted(OFFSETS_DIR.glob(f"{model}_*.yaml"))
        if not files:
            print(f"  {info['name']:<24}{model:<12}{info['soc']:<5}{'—':<16}{C.DIM}no profile{C.NC}")
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
        print(f"  {info['name']:<24}{model:<12}{info['soc']:<5}{label:<16}{col}{icon}{C.NC}")
    print(f"  {'─' * 62}")
    print(f"  {C.DIM}{n_with_profiles}/{n_devices} devices have profiles · {n_ready} ready to flash{C.NC}")


def cmd_list_templates():
    """List all known device models in the database."""
    print(section("Device Database"))
    print()
    for model, info in sorted(DEVICE_DB.items()):
        has_profile = list(OFFSETS_DIR.glob(f"{model}_*.yaml"))
        status = C.GRN + "✓" if has_profile else C.AMB + "⚠"
        print(f"  {status}{C.NC} {C.EYE}{info['name']:<22}{C.NC} {C.DIM}{model:<12}{C.NC} [{info['soc']}]  {info['board']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"\n  {C.FROST}usbliter8 profile generator{C.NC}\n")
        print(f"  Commands:")
        print(f"    {C.EYE}create{C.NC}    <Model> <iOS> [build] [kernel_file]")
        print(f"    {C.EYE}propagate{C.NC} <base.yaml> <Model> [--ios V] [--build B] [--comp-dir DIR] [--overwrite] [--force]")
        print(f"    {C.EYE}diff{C.NC}      <base.yaml> <updated.yaml>")
        print(f"    {C.EYE}migrate{C.NC}   <base.yaml> <target.yaml|iOS> [--comp-dir DIR] [--auto] [--report FILE]")
        print(f"    {C.EYE}coverage{C.NC}  Show per-device profile status")
        print(f"    {C.EYE}list{C.NC}      Show all known devices")
        print()
        sys.exit(0)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "create":
        cmd_create(args)
    elif cmd == "propagate":
        cmd_propagate(args)
    elif cmd == "diff":
        cmd_diff(args)
    elif cmd == "migrate":
        from migrate import cli_main
        cli_main(args)
    elif cmd == "coverage":
        cmd_coverage()
    elif cmd == "list":
        cmd_list_templates()
    else:
        print(err(f"Unknown command: {cmd}"))
        sys.exit(1)
