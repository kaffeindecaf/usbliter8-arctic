"""Hardware setup guide for usbliter8-arctic.

Guided Setup: wiring diagrams, RP2350 board selection, firmware download/flash,
status/health checks, and pre-flash verification.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import socket
import subprocess
import time
from importlib.util import find_spec
from pathlib import Path
from urllib.request import urlretrieve, URLError

import yaml

import toolchain
from colors import C, ok, err, warn, info, section, key_value, header, prompt
import log_utils
from log_utils import log_info, log_warn, log_error, log_step

FW_DIR = Path(__file__).parent / "firmware"
CONFIG_PATH = Path(__file__).parent / "config.yaml"

FW_REPO = "https://raw.githubusercontent.com/Octopus1633/usbliter8-firmware/main"
UF2_FILES = {
    "pico2":                 "usbliter8.pico2.uf2",
    "waveshare_usb_a":       "usbliter8.waveshare_rp2350_usb_a.uf2",
    "waveshare_usb_c":       "usbliter8.waveshare_rp2350_usb_c.uf2",
    "waveshare_usb_cm":      "usbliter8.waveshare_rp2350_usb_cm.uf2",
    "waveshare_zero":        "usbliter8.waveshare_rp2350_zero.uf2",
    "waveshare_pizero":      "usbliter8.waveshare_rp2350_pizero.uf2",
    "pimoroni_tiny2350":      "usbliter8.pimoroni_tiny2350.uf2",
}

# sha256 of the prebuilt UF2s as published in Octopus1633/usbliter8-firmware.
# The UF2 magic check below only proves a file is *a* UF2: any rebuilt or swapped
# firmware with the same header passes it, and the CFW this board sends to the
# device is what the whole chain trusts. A board listed here is pinned, so its
# file has to be byte-identical before it counts as ready; a board without an
# entry is reported as unpinned instead of "verified".
#
# The printed hash comes from the copy in firmware/ that this rig has flashed
# (verify it yourself: sha256sum firmware/<name>.uf2). To pin another board,
# download it and put its sha256 here.
UF2_SHA256 = {
    "waveshare_usb_a": "dc6f6f44e224359dc5ffef28bc11d5caf7e2a1bbd67ee7fb9b043a90295b7c29",
}

# the deliberate override, like UL8_FORCE_RESTORE: skip the pin and say so loudly
SKIP_PIN_ENV = "UL8_SKIP_UF2_PIN"

# a download message with this prefix means the bytes were rejected, so retrying
# cannot help (as opposed to a transfer that failed)
REFUSED_PREFIX = "refused:"


BOARDS = [
    {
        "id": "waveshare_usb_a",
        "name": "Waveshare RP2350-USB-A",
        "soldering": False,
        "icon": "★",
        "pins": {
            "D+": "Built-in USB-A host port (plug Lightning cable directly)",
            "D-": "Built-in USB-A host port",
            "VBUS": "USB-A provides 5V power",
            "GND": "GND via USB-A shield",
        },
        "note": "RECOMMENDED. No soldering — just plug your Lightning-to-USB-A cable into the board directly.",
    },
    {
        "id": "pico2",
        "name": "Raspberry Pi Pico 2",
        "soldering": True,
        "icon": " ",
        "pins": {"D+": "GP12", "D-": "GP13", "VBUS": "VBUS (NOT 3V3!)", "GND": "GND"},
        "note": "Most common. Requires cutting a Lightning-to-USB-A cable and soldering to GPIO pins.",
    },
    {
        "id": "waveshare_zero",
        "name": "Waveshare RP2350-Zero",
        "soldering": True,
        "icon": " ",
        "pins": {"D+": "GP12", "D-": "GP13", "VBUS": "VBUS", "GND": "GND"},
        "note": "Ultra-compact. Good for permanent rigs.",
    },
    {
        "id": "pimoroni_tiny2350",
        "name": "Pimoroni Tiny2350",
        "soldering": True,
        "icon": " ",
        "pins": {"D+": "GP12", "D-": "GP13", "VBUS": "VBUS", "GND": "GND"},
        "note": "Tiny footprint. Tested and working.",
    },
]


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)


UF2_MAGICS = {0x0A324655, 0x9E5D5157, 0x0AB16F30}


def validate_uf2(path: Path) -> bool:
    """Verify a UF2 file has a valid magic header (not a 404 HTML page)."""
    if not path.exists():
        return False
    try:
        if path.stat().st_size < 512:
            return False
        with open(path, "rb") as f:
            block = f.read(512)
        return len(block) >= 4 and int.from_bytes(block[:4], "little") in UF2_MAGICS
    except OSError:
        return False


def _pin_skipped() -> bool:
    return os.environ.get(SKIP_PIN_ENV, "").strip().lower() in ("1", "true", "yes")


def sha256_file(path: str | Path) -> str | None:
    """sha256 of a file, or None when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def check_uf2_hash(board_id: str, path: str | Path) -> tuple[bool | None, str]:
    """Compare a UF2 against the sha256 pinned for its board.

    Returns (verdict, note): None = this board has no pin (the bytes cannot be
    vouched for), True = the file is the pinned build, False = it is not (or
    could not be read). The magic check is a different question - it only says
    the file is a UF2 at all.
    """
    if _pin_skipped():
        return None, f"{SKIP_PIN_ENV} is set: the pinned sha256 is not checked"
    pinned = UF2_SHA256.get(board_id)
    if not pinned:
        return None, (f"no pinned sha256 for {board_id or 'this board'}: only the UF2 magic is "
                      f"checked, so a swapped build would not be caught")
    got = sha256_file(path)
    if got is None:
        return False, f"could not read {Path(path).name} to hash it"
    if got == pinned:
        return True, f"sha256 matches the pinned {pinned}"
    return False, (f"sha256 {got} does not match the pinned {pinned} for {board_id}: this is not "
                   f"the build the toolkit pins")


def firmware_status(board_id: str) -> tuple[bool, str]:
    """(usable, one-line note) for the guided setup, the health check and the menu.

    A board's file is usable when it is a UF2 and, if its board is pinned, when
    it is byte-identical to the pinned build.
    """
    fname = UF2_FILES.get(board_id)
    if not fname:
        return False, "no firmware file is known for this board"
    path = FW_DIR / fname
    if not validate_uf2(path):
        return False, f"{fname} is missing or not a UF2"
    size = path.stat().st_size
    verdict, note = check_uf2_hash(board_id, path)
    if verdict is None:
        state = "pin skipped" if _pin_skipped() else "unpinned"
        return True, f"{fname} ({size:,} bytes, UF2 magic ok, {state})"
    if verdict:
        return True, f"{fname} ({size:,} bytes, sha256 pinned and matching)"
    return False, note


def check_firmware(board_id: str) -> bool:
    """Check the board's firmware UF2 is usable (see firmware_status)."""
    return firmware_status(board_id)[0]


def firmware_state(board_id: str) -> tuple[bool, str, str]:
    """(usable, state, detail) for the health check and the board line.

    state names what was actually checked: "present, sha256 pinned",
    "present, UF2 magic only", "not the pinned build" or "missing". The first
    two are usable, and only the first one means the bytes were vouched for.
    """
    fname = UF2_FILES.get(board_id)
    if not fname:
        return False, "no firmware for this board", ""
    path = FW_DIR / fname
    verdict, note = check_uf2_hash(board_id, path) if path.is_file() else (None, "")
    usable = check_firmware(board_id)
    if usable and verdict:
        return True, "present, sha256 pinned", note
    if usable:
        return True, "present, pin skipped" if _pin_skipped() else "present, UF2 magic only", note
    if verdict is False:
        return False, "not the pinned build", note
    return False, "missing", note


def _download_once(url: str, dst: Path, board_id: str | None = None) -> tuple[bool, str]:
    """Single download attempt. Returns (success, error_message).

    A message starting with REFUSED_PREFIX means the bytes were rejected (the
    pin), not the transfer: the caller must not retry that one.
    """
    try:
        urlretrieve(url, dst)
        size = dst.stat().st_size
        if size <= 1024 or not validate_uf2(dst):
            dst.unlink(missing_ok=True)
            return False, f"bad file ({size} bytes, invalid UF2 magic)"
        if board_id:
            verdict, note = check_uf2_hash(board_id, dst)
            if verdict is False:
                dst.unlink(missing_ok=True)
                return False, f"{REFUSED_PREFIX} {note}"
        return True, ""
    except URLError as e:
        return False, f"network error — {e.reason}"
    except socket.timeout:
        return False, "timed out after 30s"
    except Exception as e:
        return False, str(e)


def download_firmware(board_id: str, retries: int = 3) -> bool:
    """Download RP2350 firmware UF2 from community repo, with retries and validation."""
    with log_utils.timed('firmware', 'download_firmware'):
        fname = UF2_FILES.get(board_id)
        if not fname:
            print(err(f"Unknown board ID: {board_id}"))
            log_error(f"Unknown board ID for firmware: {board_id}")
            return False

        url = f"{FW_REPO}/{fname}"
        dst = FW_DIR / fname
        tmp = FW_DIR / f"{fname}.part"
        FW_DIR.mkdir(parents=True, exist_ok=True)

        print(info(f"Downloading {fname}..."))
        print(f"    {C.DIM}{url}{C.NC}")
        log_step(f"Downloading UF2: {url}")

        socket.setdefaulttimeout(30)
        try:
            for attempt in range(1, retries + 1):
                if attempt > 1:
                    delay = 1.5 * (attempt - 1)
                    print(info(f"Retry {attempt}/{retries} in {delay:.1f}s..."))
                    time.sleep(delay)

                success, msg = _download_once(url, tmp, board_id)
                if success:
                    tmp.replace(dst)
                    size = dst.stat().st_size
                    verdict, note = check_uf2_hash(board_id, dst)
                    if verdict:
                        print(ok(f"Downloaded + verified {fname} ({size:,} bytes, sha256 pinned)"))
                    else:
                        print(ok(f"Downloaded {fname} ({size:,} bytes, UF2 magic ok)"))
                        print(warn(note))
                    log_info(f"Firmware downloaded: {fname} ({size} bytes, "
                             f"{'pinned hash ok' if verdict else 'unpinned'})")
                    return True

                tmp.unlink(missing_ok=True)
                print(warn(f"Attempt {attempt}/{retries} failed: {msg}"))
                log_warn(f"UF2 download attempt {attempt}/{retries} failed: {msg}")
                if msg.startswith(REFUSED_PREFIX):
                    # the bytes are not the pinned build: retrying downloads the
                    # same bytes again, so stop and say what is wrong
                    print()
                    print(err(msg[len(REFUSED_PREFIX):].strip()))
                    print(f"  {C.DIM}The toolkit pins this board's firmware because the CFW built "
                          f"here gets flashed through it. If upstream published a new build, check "
                          f"it and update UF2_SHA256 in hardware_guide.py; to skip the pin on "
                          f"purpose, set {SKIP_PIN_ENV}=1.{C.NC}")
                    log_error(f"UF2 refused for {board_id}: {msg}", module="hardware_guide")
                    return False

            print(err(f"All {retries} download attempts failed — check your internet connection"))
            log_error(f"UF2 download failed after {retries} attempts: {url}")
            return False
        finally:
            socket.setdefaulttimeout(None)


def show_wiring(board: dict):
    """Display wiring diagram in ASCII art."""
    print(section(f"Wiring — {board['name']}"))
    print()

    pins = board.get("pins", {})

    if board.get("soldering", True):
        print(f"  {C.AMB}${C.NC} Cut a Lightning-to-USB-A cable. Verify wires with a multimeter!")
        print(f"  {C.AMB}${C.NC} Colors may vary — always test continuity before soldering.")
        print()
        print(f"  {C.SNOW}Lightning Cable  ──────→  {board['name']}{C.NC}")
        print()
        for signal, gpio in pins.items():
            color = {"D+": C.GRN, "D-": C.FROST, "VBUS": C.AMB, "GND": C.DIM}.get(signal, C.NC)
            print(f"    {color}{signal:<6}{C.NC} → {C.EYE}{gpio}{C.NC}")
        print()
        print(f"  {C.DIM}Tip: Use heat-shrink tubing on each solder joint for durability.{C.NC}")
    else:
        print(f"  {C.GRN}No soldering needed!{C.NC} Just plug your Lightning cable into the USB-A port.")
        print()


def show_led_guide(board: dict):
    """Display LED indicator meanings."""
    print(section("LED Indicators"))
    print()
    print(f"  {C.SNOW}RGB LED boards:{C.NC}")
    print(f"    {C.AMB}Blinking orange{C.NC}  → booting (~2s)")
    print(f"    {C.AMB}Steady orange{C.NC}    → idle, ready")
    print(f"    {C.EYE}Blue{C.NC}              → exploit in progress")
    print(f"    {C.GRN}Green{C.NC}             → exploit SUCCEEDED (device is now PWND)")
    print(f"    {C.RED}Red{C.NC}               → exploit FAILED (reset board, try again)")
    print()
    print(f"  {C.SNOW}Single-color LED boards:{C.NC}")
    print(f"    {C.DIM}Slow blink (200ms){C.NC} → booting")
    print(f"    {C.DIM}Breathing{C.NC}          → idle, ready")
    print(f"    {C.EYE}Rapid blink (100ms){C.NC} → exploit in progress")
    print(f"    {C.GRN}Steady ON{C.NC}          → exploit SUCCEEDED")
    print(f"    {C.RED}OFF{C.NC}                → exploit FAILED")


def show_troubleshooting():
    """Common issues and fixes."""
    print(section("Troubleshooting"))
    print()
    issues = [
        ("LED turns off after exploit", "Reboot RP2350 (reset button or power cycle), re-enter DFU on iPhone, try again."),
        ("DFU but no PWND:[...] serial", "Check D+/D- aren't swapped. Check cable is USB-A (not USB-C). Verify UF2 matches your board."),
        ("RP2350 not detected by PC", "Hold BOOTSEL while plugging in, drag UF2 to RPI-RP2 drive. After flash, unplug and reconnect."),
        ("Device in DFU mode?", "Volume Down + Power for 3s, then release Power while holding Volume Down for 10s. Screen should be black."),
        ("Exploit works but fails intermittently", "RP2350 timing is tight. Try a shorter Lightning cable (<30cm). Remove USB hubs."),
        ("A13 device (iPhone 11/SE2) fails", "RP2040 does NOT work with A13. You MUST use an RP2350 board."),
    ]
    for problem, solution in issues:
        print(f"  {C.AMB}?{C.NC} {C.SNOW}{problem}{C.NC}")
        print(f"    {C.DIM}→ {solution}{C.NC}")
        print()


class QuitSetup(Exception):
    """Raised when the user asks to exit the guided setup."""


def _ask(prompt_text: str, default: str = "", valid: tuple | None = None,
         retries: int = 3) -> str:
    """Input with validation and retries. 'quit' or Ctrl-C exits the setup."""
    for attempt in range(retries):
        try:
            ans = log_utils.safe_input(prompt(prompt_text) or default).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            raise QuitSetup
        if ans in ("quit", "q"):
            print()
            raise QuitSetup
        if valid is None or ans in valid or ans == default:
            return ans
        print(warn(f"Invalid choice '{ans}' — try again ({retries - attempt - 1} left)"))
    return default


def _confirm(prompt_text: str, default: str = "y") -> bool:
    return _ask(prompt_text, default, valid=("y", "n", "yes", "no", "")) in ("y", "yes", "")


def _pick_board() -> dict:
    """Board selection with retry loop. Returns a BOARDS entry."""
    print(section("Step 1 — Select Your Board"))
    print()
    for i, b in enumerate(BOARDS):
        icon = "★" if not b["soldering"] else " "
        soldering_note = f"  {C.GRN}{icon} no soldering{C.NC}" if not b["soldering"] else f"  {C.AMB}{icon} requires soldering{C.NC}"
        print(f"  {C.EYE}[{i + 1}]{C.NC} {C.SNOW}{b['name']}{C.NC}  {soldering_note}")
        print(f"     {C.DIM}{b['note']}{C.NC}")
        print()
    print(f"  {C.EYE}[m]{C.NC} More info on all supported boards")
    print()

    while True:
        choice = _ask("Choose board [1]: ", "1")
        if choice == "m":
            print(section("All Supported Boards"))
            for b in BOARDS:
                print(f"  {C.EYE}{b['name']}{C.NC}")
                print(f"    {C.DIM}{b['note']}{C.NC}")
                print()
            continue
        try:
            idx = int(choice) - 1
        except ValueError:
            print(warn("Please enter a number between 1 and %d" % len(BOARDS)))
            continue
        if 0 <= idx < len(BOARDS):
            return BOARDS[idx]
        print(warn(f"Board {idx + 1} does not exist — pick 1-{len(BOARDS)}"))


def _wait_rp2350(attempts: int = 3, timeout: float = 15.0) -> bool:
    """Poll for RP2350 USB device; let the user retry between attempts."""
    from pwn_utils import detect_rp2350
    for attempt in range(1, attempts + 1):
        print(info(f"Scanning for RP2350 (attempt {attempt}/{attempts}, {timeout:.0f}s)..."))
        log_step(f"RP2350 scan attempt {attempt}/{attempts}")
        start = time.time()
        found = None
        while time.time() - start < timeout:
            found = detect_rp2350()
            if found:
                break
            time.sleep(0.5)
        if found:
            print(ok(f"RP2350 detected: bus {found['bus']}, addr {found['address']}, serial={found.get('serial', '?')[:40]}"))
            log_info(f"RP2350 detected: bus={found['bus']} addr={found['address']}")
            return True
        print(warn("No RP2350 board detected — check USB cable and power"))
        if attempt < attempts:
            ans = _ask("Reconnect the board and press Enter to retry, [s]kip: ", "r", valid=("", "r", "s", "skip"))
            if ans in ("s", "skip"):
                break
    return False


def _test_pwn(attempts: int = 3, timeout: int = 60) -> bool:
    """Live PWN test with retries and troubleshooting hints."""
    from pwn_utils import wait_for_pwn
    for attempt in range(1, attempts + 1):
        print(info(f"PWN test attempt {attempt}/{attempts} — plug iPhone into RP2350 and enter DFU"))
        if wait_for_pwn(timeout=timeout):
            return True
        if attempt < attempts:
            print(warn("PWN not detected. Common fixes:"))
            print(f"    {C.DIM}→ Hold Volume Down + Power for 3s, release Power, keep Volume Down 10s{C.NC}")
            print(f"    {C.DIM}→ Check LED: orange=idle, blue=exploiting, green=success, red=failed (reset board){C.NC}")
            print(f"    {C.DIM}→ Use a short cable (<30cm), remove USB hubs{C.NC}")
            ans = _ask("Press Enter to retry, [s]kip: ", "r", valid=("", "r", "s", "skip"))
            if ans in ("s", "skip"):
                break
    return False


def guided_setup():
    """Guided, beginner-friendly setup walkthrough with checks and retries."""
    try:
        _guided_setup_impl()
    except QuitSetup:
        print(info("Setup cancelled — returning to menu"))
    except KeyboardInterrupt:
        print()
        print(info("Setup cancelled (Ctrl+C) — returning to menu"))
    finally:
        print()


def _guided_setup_impl():
    print(header("Guided Setup — usbliter8-arctic"))
    print()
    print(f"  {C.AMB}★ Recommended for beginners{C.NC} — hardware, firmware and first PWN, step by step")
    print(f"  {C.DIM}Resumable: re-run anytime. Progress is saved to config.yaml{C.NC}")
    print(f"  {C.DIM}Type 'quit' at any prompt to exit safely.{C.NC}")
    print()

    # Step 0 — dependencies
    print(section("Step 0 — Dependencies"))
    print()
    from deps import check_dependencies, install_dependencies
    results = check_dependencies()
    for pkg in ("pyusb", "pyyaml"):
        key = f"pkg_{pkg}"
        print(key_value(f"py {pkg}", f"{C.GRN}installed{C.NC}" if results[key] else f"{C.RED}missing{C.NC}"))
    print(key_value("libusb-1.0", f"{C.GRN}found{C.NC}" if results["libusb"] else f"{C.RED}missing{C.NC}"))
    if not all(results.values()):
        print()
        if _confirm("Some dependencies are missing. Install them now? [Y/n]: ", "y"):
            install_dependencies()
        else:
            print(warn("Continuing without dependencies — USB detection will not work"))
    print()

    # Step 1 — board selection
    board = _pick_board()
    print()
    print(ok(f"Selected: {board['name']}"))
    _save_config({"selected_board": board["id"]})
    log_info(f"Guided setup: board selected {board['id']}")
    print()

    # Step 2 — wiring
    print(section("Step 2 — Wiring"))
    print()
    show_wiring(board)

    # Step 3 — firmware
    print(section("Step 3 — Firmware"))
    print()
    board_id = board["id"]
    fname = UF2_FILES[board_id]
    fw_path = FW_DIR / fname

    fw_ready, fw_note = firmware_status(board_id)
    if fw_ready:
        print(ok(f"Firmware ready: {fw_note}"))
        if UF2_SHA256.get(board_id) is None and not _pin_skipped():
            print(warn("Only the UF2 magic was checked: this board has no pinned sha256, so a "
                       "swapped build would look exactly the same."))
            print(f"  {C.DIM}To pin it, hash the file and add it to UF2_SHA256 in "
                  f"hardware_guide.py.{C.NC}")
    else:
        verdict, note = check_uf2_hash(board_id, fw_path) if fw_path.exists() else (None, "")
        if verdict is False:
            print(err(f"Existing {fname} is not the build this toolkit pins"))
            print(f"  {C.DIM}{note}{C.NC}")
            print(f"  {C.DIM}Delete it and re-download, or update UF2_SHA256 if upstream "
                  f"published a new build (README, firmware section).{C.NC}")
        elif fw_path.exists():
            print(warn(f"Existing {fname} is corrupt/incomplete — will re-download"))
        print(info(f"No valid firmware for {board['name']}"))
        if _confirm("Download firmware from Octopus1633/usbliter8-firmware? [Y/n]: ", "y"):
            if download_firmware(board_id, retries=3):
                print()
                print(f"  {C.SNOW}To flash:{C.NC}")
                print(f"    1. Hold {C.EYE}BOOTSEL{C.NC} button on {board['name']}")
                print(f"    2. Plug USB into your computer (RPI-RP2 drive should appear)")
                print(f"    3. Drag {C.FROST}{fname}{C.NC} onto the RPI-RP2 drive")
                print(f"    4. Board will reboot — LED should start blinking")
                print()
            else:
                print(err("Firmware download failed — see the reason above, then re-run Guided Setup"))
        else:
            print(info("Skipping firmware download. Re-run Guided Setup anytime."))
    print()

    # Step 4 — connection & PWN test
    print(section("Step 4 — Connect & Verify"))
    print()
    from pwn_utils import check_pyusb_installed
    if check_pyusb_installed():
        if _confirm("Board plugged in? Check USB detection now? [Y/n]: ", "y"):
            _wait_rp2350(attempts=3)
        print()
        if _confirm("Test the exploit now (PWN DFU)? [Y/n]: ", "y"):
            _test_pwn(attempts=3)
    else:
        print(warn("pyusb not installed — USB verification skipped (install via Step 0)"))
    print()

    # Step 5 — final checklist
    print(section("Step 5 — Ready to Exploit"))
    print()
    checklist = [
        f"Flash the UF2 firmware to your {board['name']}",
        "Connect Lightning cable to iPhone/iPad",
        "Put iPhone in DFU mode (Vol Down + Power → release Power)",
        "Connect RP2350 to your computer via USB",
        "Wait for LED: steady green (RGB) or steady ON (single-color)",
        "Run [8] Check PWN Status from the main menu to verify",
    ]
    for i, item in enumerate(checklist, 1):
        print(f"  {C.GRN}{i}.{C.NC} {item}")
    print()

    show_led_guide(board)
    print()
    show_troubleshooting()

    # Step 6 - the one time this device's data is worth a question
    _offer_contribution("guided")

    try:
        log_utils.safe_input(prompt("Press Enter to return to menu..."))
    except (EOFError, KeyboardInterrupt):
        print()


def _offer_contribution(trigger: str) -> None:
    """Ask whether to send this device's data back. Silent when there is nothing."""
    try:
        import share
        share.offer(trigger)
    except Exception as exc:                                  # noqa: BLE001
        log_utils.log_debug(f"share prompt unavailable: {exc}", module="hardware_guide")


# Backward-compatible alias
interactive_hardware_setup = guided_setup


# ── test suite row (ties the health check to the CI gate) ────────────

TESTS_DIR = Path(__file__).resolve().parent / "tests"
PYTEST_TIMEOUT = 900


def pytest_available() -> bool:
    """True when this interpreter can run the suite at all."""
    return find_spec("pytest") is not None


def _run_pytest(argv: list[str]) -> subprocess.CompletedProcess:
    """Run pytest from the repo root. Kept as its own call so tests can stub it."""
    return subprocess.run(argv, cwd=str(TESTS_DIR.parent), capture_output=True,
                          text=True, timeout=PYTEST_TIMEOUT, check=False)


def parse_pytest_counts(output: str) -> dict[str, int]:
    """Pull {"passed": 388, "failed": 1, ...} out of pytest's summary line.

    The summary line is the last one pytest prints, and a word can appear more
    than once (a test quoting "3 failed" in an assertion message), so the LAST
    match per word is the one that counts.
    """
    counts: dict[str, int] = {}
    for number, word in re.findall(
            r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed|deselected)\b", output):
        counts[word] = int(number)
    return counts


# count words in the order they are worth reading, singular and plural of
# "error" both present because pytest prints the form that fits the number
SUMMARY_WORDS = ("passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed", "deselected")


def inside_pytest() -> bool:
    """True when this process is a pytest run.

    pytest sets PYTEST_CURRENT_TEST for the duration of every test, which is
    the honest signal that a suite is already running.
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


def run_test_suite() -> dict:
    """Run the repo suite, return {"ran": bool, "ok": bool, "detail": str}.

    Never raises and never runs from inside the suite itself: a health check
    under pytest would recurse into another pytest. A missing pytest or a
    missing tests/ dir is a note, not a failed check.
    """
    if inside_pytest():
        return {"ran": False, "ok": False, "detail": "skipped (already inside pytest)"}
    if os.environ.get("UL8_NO_TESTS") == "1":
        return {"ran": False, "ok": False, "detail": "skipped (UL8_NO_TESTS=1)"}
    if not TESTS_DIR.is_dir():
        return {"ran": False, "ok": False, "detail": "no tests/ in this tree"}
    if not pytest_available():
        return {"ran": False, "ok": False, "detail": "pytest not installed"}

    argv = [sys.executable, "-m", "pytest", str(TESTS_DIR), "-q", "-p", "no:cacheprovider"]
    try:
        proc = _run_pytest(argv)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ran": False, "ok": False, "detail": f"could not run pytest: {exc}"}

    output = f"{proc.stdout}\n{proc.stderr}"
    counts = parse_pytest_counts(output)
    summary = ", ".join(f"{counts[key]} {key}"
                        for key in SUMMARY_WORDS if counts.get(key))
    if counts:
        duration = re.search(r"in ([\d.]+)s", output)
        detail = summary + (f" in {duration.group(1)}s" if duration else "")
    else:
        detail = f"pytest exited {proc.returncode} with no summary"
    bad = counts.get("failed") or counts.get("error") or counts.get("errors")
    ok = proc.returncode == 0 and bool(counts) and not bad
    return {"ran": True, "ok": ok, "detail": detail}


def run_health_check() -> dict[str, bool]:
    """Run comprehensive hardware readiness check."""
    print(header("Hardware Health Check"))
    print()

    results = {}

    # 1. Board config
    cfg = _load_config()
    board_id = cfg.get("selected_board", "unknown")
    board_name = {b["id"]: b["name"] for b in BOARDS}.get(board_id, board_id)
    print(key_value("Board", board_name))
    results["board_configured"] = board_id != "unknown"
    if not results["board_configured"]:
        print(warn("No board selected — run [1] Guided Setup first"))

    # 2. Firmware: present, and are its bytes the build this board is pinned to
    fw_ok, fw_state, fw_note = firmware_state(board_id)
    fw_name = UF2_FILES.get(board_id, "N/A")
    color = C.GRN if fw_state == "present, sha256 pinned" else (C.AMB if fw_ok else C.RED)
    print(key_value("Firmware", f"{fw_name} ({color}{fw_state}{C.NC})"))
    if not fw_ok and fw_note:
        print(f"    {C.DIM}{fw_note}{C.NC}")
    results["firmware_present"] = fw_ok

    # 3. Tools
    required = ["usbliter8ctl"]
    # a bundled macOS binary "exists" on Linux but cannot run: ask toolchain
    tools = {t: toolchain.tool_available(t) for t in required}
    for t, ok_val in tools.items():
        color = C.GRN if ok_val else C.RED
        print(key_value(f"Tool: {t}", f"{color}{'found' if ok_val else 'NOT FOUND'}{C.NC}"))
    results["tools_ready"] = all(tools.values())

    # 4. RP2350
    from pwn_utils import USB_HELP, check_pyusb_installed, detect_rp2350, usb_status
    pyusb_ok = check_pyusb_installed()
    print(key_value("pyusb", f"{C.GRN}installed{C.NC}" if pyusb_ok else f"{C.AMB}not installed{C.NC}"))
    results["pyusb_available"] = pyusb_ok

    status = usb_status()
    if status["ready"]:
        print(key_value("usb backend", f"{C.GRN}{status['backend']}{C.NC}"))
    else:
        print(key_value("usb backend", f"{C.RED}unavailable{C.NC}"))
        for line in (status["problem"] or USB_HELP).splitlines():
            print(f"    {C.DIM}{line}{C.NC}")
    results["usb_backend_ready"] = status["ready"]

    try:
        import img4wrap
        decoder_ok, decoder_note = img4wrap.decoder_available()
        print(key_value("IMG4 codec", f"{C.GRN}{decoder_note}{C.NC}" if decoder_ok
                        else f"{C.AMB}{decoder_note}{C.NC}"))
        results["img4_decoder"] = decoder_ok
    except Exception:                                          # noqa: BLE001
        results["img4_decoder"] = False

    if pyusb_ok:
        rp = detect_rp2350()
        if rp:
            print(ok(f"RP2350 connected: bus {rp['bus']}, addr {rp['address']}"))
            results["rp2350_detected"] = True
        else:
            print(warn("No RP2350 detected — connect board to USB"))
            results["rp2350_detected"] = False
    else:
        results["rp2350_detected"] = False

    # 5. Tools directory check
    tools_ok = toolchain.TOOLS_DIR.exists() and list(toolchain.TOOLS_DIR.glob("*"))
    print(key_value("Tool dir", f"{C.GRN}ready ({len(list(toolchain.TOOLS_DIR.glob('*')))}){C.NC}" if tools_ok else f"{C.RED}empty/missing{C.NC}"))
    results["tool_dir_ready"] = tools_ok

    # 6. Test suite (the CI gate, run here so the TUI can see it)
    suite = run_test_suite()
    if suite["ran"]:
        suite_color = C.GRN if suite["ok"] else C.RED
        print(key_value("Test suite", f"{suite_color}{suite['detail']}{C.NC}"))
        results["test_suite"] = suite["ok"]
        if not suite["ok"]:
            log_warn(f"Health check: test suite {suite['detail']}")
    else:
        print(key_value("Test suite", f"{C.AMB}{suite['detail']}{C.NC}"))

    # Summary
    print()
    all_ok = all(results.values())
    if all_ok:
        print(ok("All checks passed — ready to exploit"))
    else:
        failed = [k for k, v in results.items() if not v]
        print(warn(f"{len(failed)} check(s) need attention: {', '.join(failed)}"))

    log_info(f"Health check: {'ALL OK' if all_ok else f'FAILURES: {failed}'}")
    return results



if __name__ == "__main__":
    import log_utils
    log_utils.install()
    sys.exit(log_utils.guard(interactive_hardware_setup))
