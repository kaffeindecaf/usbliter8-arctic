# usbliter8-arctic

> Tethered iOS jailbreak toolkit for A12/A13 — a TUI hub that automates the whole usbliter8 chain: guided hardware setup, offset profiles, CFW building, restore, and post-exploit setup on RP2350 boards.

![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB) ![Tests](https://img.shields.io/badge/tests-35%20passing-2ea44f) ![Platform](https://img.shields.io/badge/platform-Linux-5272A8) ![Exploit](https://img.shields.io/badge/exploit-usbliter8_%E2%80%A2_RP2350-8B5CF6)

## Why usbliter8-arctic?

The original flow (rav000's RP2350 firmware + wh1te4ever's scripts) is raw scripts and hand-edited offsets. Arctic is the engineering around it:

| Arctic | Original usbliter8 |
|---|---|
| **Guided setup** — board picker, wiring diagrams, LED guide, troubleshooting, firmware download with UF2-magic validation + retries | "Solder D+/D- and figure it out" |
| **Offset profiles** — per-device/iOS YAML with validation (sentinel + type + hex), active-device config, online source lookup | Offsets hardcoded in `make_cfw.py` |
| **Beta-to-beta migration** — `profile_gen.py migrate` auto-finds every patch site in a new beta via AArch64 pattern fingerprinting (capstone-verified), confidence-scored, with delta fallback + canonical cross-check | Re-discover all offsets by hand every beta |
| **Hardware awareness** — RP2350 VID/PID detection, DFU/WTF/restore detection, PWN serial verification with polling | Blind runs |
| **CFW builder with dry-run** — board-aware paths, dry-run simulation, correct IMG4 type tags, temp cleanup | One device, one path, no preview |
| **Safe restore flow** — script pre-checks, PWN verification, TSS proxy lifecycle, explicit `YES`, post-write validation | Run `restore_cfw.sh` and pray |
| **Post-boot toolkit** — USB networking, VNC, SSH (password via `SSHPASS` env, never in `ps` output) | Manual setup |
| **Health check + deps installer** — one command verifies board/firmware/tools/USB; apt/pacman/dnf/brew/pip detection | Cryptic pip errors |
| **Engineered** — 25-test pytest suite with a b2→b3 ground-truth oracle, 21 audit bugs fixed (`foundbugs.md`), session logging, colored TUI | Hacked together |

## Supported devices

| Device | Chip | Board |
|---|---|---|
| iPhone XS / XS Max (incl. CN) | A12 | d321ap / d331ap / d331pap |
| iPhone XR | A12 | n841ap |
| iPhone 11 / 11 Pro / Pro Max | A13 | n104ap / d421ap / d431ap |
| iPhone SE (2nd gen) | A13 | d79ap |
| iPad mini 5 (WiFi / Cell) | A12 | j211ap / j212ap |
| iPad Air 3 (WiFi / Cell) | A12 | j213ap / j214ap |
| iPad 8 (WiFi / Cell) | A12 | j171ap / j172ap |
| iPad 9 (WiFi / Cell) | A13 | j181ap / j182ap |

> **Profile status** — verified offsets exist for iPhone 11 Pro (iPhone12,3) on 27.0b2/b3. A13 siblings (iPhone 11 / 11 Pro Max / SE 2 / iPad 9) have propagated kernel+daemon profiles with iBSS/iBEC/TXM offsets pending discovery; A12 devices need first-offset bootstrapping. Live status: `python3 profile_gen.py coverage`.

## How it works

1. **RP2350** exploits the A12/A13 SecureROM → device enters **PWND DFU** mode
2. **PWN DFU** grants unsigned firmware execution — iBoot, kernel and device-tree patches are applied
3. **Custom firmware** is built from an Apple IPSW with security bypasses patched in
4. **Tethered boot** — the exploit must be re-applied on every cold boot

## Diagrams

### Connection overview

![Connection Overview](ConnectionOverview.png)

### Soldered board wiring

For boards without a built-in USB-A host port (Pico 2, RP2350-Zero, Tiny2350), cut a Lightning-to-USB-A cable and solder the four internal wires to the GPIO pins:

```
                   SOLDERED BOARD WIRING
               Pico 2 · RP2350-Zero · Tiny2350

    Cut a Lightning-to-USB-A cable. Keep the Lightning
    end, discard the USB-A plug.

   Lightning cable                  Pico 2 board
   ┌─────────────────────┐      ┌───────────────────────────┐
   │  Lightning end ►    │      │                           │
   │      iPhone         │      │   ┌──────────────┐        │
   └──────────┬──────────┘      │   │ USB-C ──► PC │        │
              │                 │   └──────────────┘        │
              ├─ Red   (VBUS)───┤─► VBUS (pin 40) ⚠ 5V!     │
              ├─ White (D+)  ───┤─► GP12 (pin 16)           │
              ├─ Green (D-)  ───┤─► GP13 (pin 17)           │
              └─ Black (GND) ───┤─► GND  (pin 38)           │
                                │                           │
                                └───────────────────────────┘
```

> ⚠️ **Wire colors vary by brand** — verify continuity from the Lightning pin to each wire with a multimeter before soldering.
>
> ⚠️ **VBUS is 5V** — never solder it to the 3V3 pin or you will destroy the board.

## Prerequisites

- **RP2350 board** (NOT RP2040 — A13 requires RP2350): Waveshare RP2350-USB-A ★ (recommended, no soldering) · Raspberry Pi Pico 2 · Waveshare RP2350-Zero · Pimoroni Tiny2350
- Lightning-to-USB-A cable + a compatible A12/A13 device
- **Linux** (tested on Debian/Parrot) with Python 3.9+:

  ```bash
  sudo apt install python3-usb python3-yaml
  ```

- Binary tools in `tools/` are macOS Mach-O; the interactive menu guides tool handling

## Quick start

```bash
git clone https://github.com/kaffeindecaf/usbliter8-arctic.git
cd usbliter8-arctic
chmod +x main.py
sudo ./main.py
```

> **★ New to usbliter8? Start with `1` / `h` — Guided Setup.** It walks you through hardware, firmware flashing and your first PWN with built-in checks and retries.

| Key | Action |
|---|---|
| `1` `h` | **Guided Setup** ★ — wiring, flash firmware, verify PWN |
| `2` `c` | Configure device (model / iOS offset profile) |
| `3` `b` | Build custom firmware (IPSW + offsets) |
| `4` `f` | Flash CFW ⚠ erases all data |
| `5` | Boot SSHRD ramdisk for filesystem access |
| `6` | Normal boot with patches applied |
| `7` | Post-boot setup (USB network, VNC, SSH, Sileo) |
| `8` `p` | Check PWN/DFU status |
| `9` `x` | Health check |
| `i` | Install dependencies (pyusb, pyyaml, libusb) |
| `0` `e` | Explain capabilities |
| `q` | Quit |

## CLI usage

Standalone script interfaces:

```bash
sudo python3 ul8.py menu                                   # TUI hub

python3 pwn_utils.py scan | wait                           # USB detection / PWN wait

python3 device_offsets.py list                             # available offset profiles
python3 device_offsets.py validate offsets/iPhone12,3_27.0b2.yaml
python3 device_offsets.py find iPhone11,8                  # online offset sources

python3 profile_gen.py list                                # device database
python3 profile_gen.py create iPhone12,3 27.0              # new profile (sentinel offsets)
python3 profile_gen.py diff a.yaml b.yaml

# Same-SoC device propagation — carry kernel/daemon offsets to another device
python3 profile_gen.py propagate offsets/iPhone12,3_27.0b2.yaml iPhone12,1
python3 profile_gen.py propagate offsets/iPhone12,3_27.0b2.yaml iPhone12,1 \
    --comp-dir extracted/          # + auto-discover iBSS/iBEC/TXM via fingerprinting
python3 profile_gen.py coverage    # per-device profile status table

# Offset migration — carry patch offsets across beta builds
python3 profile_gen.py migrate offsets/iPhone12,3_27.0b2.yaml offsets/iPhone12,3_27.0b3.yaml \
    --comp-dir extracted/ --report migrate_report.md
python3 profile_gen.py migrate offsets/iPhone12,3_27.0b3.yaml 27.0b4 --auto   # bootstrap a new beta

python3 cfw_builder.py iPhone12,3_27.0b3.ipsw offsets/iPhone12,3_27.0b3.yaml --check-only
```

### Offset migration (`profile_gen.py migrate`)

Re-discovers patch offsets for a new beta automatically: AArch64 instructions are
fingerprinted with immediates wildcarded (`fingerprint.py`), searched in the target
binary, and verified with capstone disassembly.

- **Components** — `--comp-dir` with `base/` + `target/` raw files (`kernelcache.raw`, `iBSS.raw`, `iBEC.raw`, `RestoreRamdisk.raw`, `TXM.raw`), or `--fetch` to run the work dir's `get_fw.py`
- **Confidence** — 0.95 unique+class match · 0.90 unique string site · 0.60 ambiguous · 0.30 multi-hit / delta-inferred
- **Output** — `migrate_report.md` (per-entry table, site hexdump, `REVIEW REQUIRED` + `CANONICAL CONFLICTS` sections); `--auto` writes the target profile with `migrated:` metadata + post-write validation
- **Safety** — never trust anything below 0.90 without manual review; delta inference is LOW by design

Tests: `python3 -m pytest tests/ -q` (ground truth = b2 → b3 oracle in `OffsetMigrationChecklist.md`).

## Patch overview

The CFW builder applies hex patches at precise offsets for each component:

- **iBSS / iBEC** — Image4 validation bypass, boot-args injection, nonce preservation
- **Kernel** — USB restriction removal, sandbox bypasses, AMFI trust, APFS seal/SSV bypass, SEP panic bypass, launchd constraints, debugger unlock
- **Device Tree** — content-protection removal, ephemeral storage
- **Restore Ramdisk** — ASR signature bypass, FDR force-succeed
- **Daemons** — coreauthd, ctkd, mobileactivationd activation bypass

## Layout

```
usbliter8-arctic/
├── main.py / ul8.py      # TUI hub + standalone launcher
├── boot_chain.py         # boot / restore / SSH / post-boot utilities
├── cfw_builder.py        # CFW patching pipeline
├── pwn_utils.py          # USB detection + PWN verification
├── device_offsets.py     # YAML offset profile manager
├── profile_gen.py        # profile generator + migrate entrypoint
├── migrate.py            # migration orchestrator (delta, canonical check, report)
├── fingerprint.py        # AArch64 pattern fingerprint engine
├── hardware_guide.py     # guided setup, health check, firmware flashing
├── deps.py               # dependency checker & installer
├── log_utils.py · colors.py
├── offsets/              # device offset profiles (template, sources, iPhone12,3_*)
├── tools/                # binary utilities (img4, img4tool, usbliter8ctl, …)
└── firmware/             # downloaded UF2 firmware files
```

## Warnings

- **Tethered jailbreak** — the device will not boot without the RP2350 exploit applied on every cold start
- **Flashing CFW erases all data** — always keep a backup
- **Linux only** — not tested on macOS

## Credits

- [rav000/usbliter8](https://github.com/rav000/usbliter8) — RP2350 firmware and exploit
- [wh1te4ever/usbliter8-fun](https://github.com/wh1te4ever/usbliter8-fun) — CFW scripts and boot chain
- [W0lfSword](https://github.com/W0lfSword) — kernel offset research
- [Octopus1633/usbliter8-firmware](https://github.com/Octopus1633/usbliter8-firmware) — prebuilt UF2 binaries
