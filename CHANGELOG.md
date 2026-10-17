# changelog

Newest first. One section per release; while the toolkit has no tagged release
the sections are dated work rounds and the number is the one in
`version.py` (`VERSION`). Dates are the day the work landed on `main`. The top
`## <version> - <date>` heading must name the current `VERSION`, and every date
heading must run newest first: `tests/test_changelog.py` fails when either
drifts. Security-relevant fixes name the report they came from (`foundbugs.md`).

## 0.2.0-beta - 2026-10-15

### 2026-10-17 - doc counts

- `safety_check.py` gained a `doc counts` warning: the README's prose numbers
  must match the repo. The suite count in prose is compared with
  `pytest --collect-only`, "N lines of Python in M modules" with the root
  modules, and "N audit bugs" with the rows in `foundbugs.md`.
- the badge check never looked past its own URL, so the comparison table still
  advertised 462 tests and 22 audit bugs while the suite had 561 and
  `foundbugs.md` recorded 33. Both lines are corrected, badge included.
- 6 tests in `tests/test_safety_check.py` pin the check: stale vs matching
  count, a per-file claim is not a suite claim, module/line drift, audit bug
  drift, and warning-only (not failing) through `main()`.

### 2026-10-15 - firmware pins (H1.1)

- the prebuilt UF2s are pinned by sha256, not only magic-checked:
  `hardware_guide.UF2_SHA256`, `sha256_file()`, `check_uf2_hash()`,
  `firmware_status()`/`firmware_state()`, and `_download_once()` refuses a
  mismatched download instead of retrying the same bytes.
- the guided setup, health check and board line now say `sha256 pinned` vs
  `UF2 magic only` vs `pin skipped`, so a magic check is never printed as
  "verified"; `UL8_SKIP_UF2_PIN=1` is the deliberate override.
- only `waveshare_usb_a` is pinned (the one UF2 in the tree). The other six
  boards are reported as unpinned until a download is pinned.
- 23 tests in `tests/test_firmware_pin.py`, one of them checking the local file
  against its pin.

### 2026-10-08 - repo hygiene

- dead-code pass (vulture, AST scan, repo-wide grep): `colors.STEEL`/`D`,
  `cfw_builder._board_config`, an unused loop target in `hardware_guide` and a
  computed-and-never-printed variable in `contribute.cmd_pr` are gone.
- `tests/test_contribute.py` (16 tests) pins the contribute flow: exit codes,
  unknown-command output, the PR refusals and `cmd_new` writing only through
  `dump_profile_yaml`. It found a real defect: a profile that fails validation
  was printed as an unidentifiable `? (?) iOS ?` row, which now names the file
  and says `(invalid profile)`.

### 2026-10-07 - the write path

- `patch_txm()` applies the profile's `txm` section for real as stage 4/7,
  rewrapped into Apple's own container (fourcc `trxm`, description kept). It was
  migrated, validated, preflighted and fetched before, but nothing wrote it
  (foundbugs #24).
- site-dependent entries (`boot_args_adrp`, `boot_args_add`, `keep_nonce_b`) are
  never filled with the value read out of the target site: the fill engine,
  `propagate` and `migrate` keep them pending, `preflight` blocks a site copy as
  the failing `no-op` status, and the iPad12,1/12,2 27.0b3 profiles are
  corrected (foundbugs #25).
- every write goes through `publish_component()`: encode, atomic write, then
  re-open and prove the file unwraps to exactly the patched payload with
  Apple's fourcc and description. A mismatch restores the original, marks the
  section `failed` and records nothing (foundbugs #28). Rootfs daemons get
  `verify_binary()`.
- `_patch_at` refuses a write past the end of the component instead of growing
  the payload (`PatchOutOfRange`, foundbugs #27); a malformed profile value is a
  clean failure, not a traceback (foundbugs #31); an absent component is
  `skipped` with a reason, not `failed` (foundbugs #32).
- a restore now checks the tree it builds the CFW from: `.ul8-build.json` plus
  `cfw_builder.py <tree> <profile> --verify` gate `restore_device()` before
  stage 1, with `UL8_FORCE_RESTORE=1` as the override (foundbugs #29).
  `components._is_payload()` skips `.ul8*` paths so a rebuild cannot resolve its
  own backup as the component (foundbugs #30), and `--restore-originals` puts a
  tree back byte-identically.
- an SSHRD boot that replaced the Ramdisk is recorded (`Ramdisk_pre_sshrd` plus
  a `.ul8-sshrd` note) and the erase prompt warns first (foundbugs #33).
- the dry run reads `dry_run_plan()`: the site count it prints is the count a
  build writes, every skipped entry is listed with its reason, and a section no
  code path applies is reported. `cfw_builder.py <ipsw> <profile> --check-only`
  exits 1 when the gate refuses instead of always 0.
- 21 tests in `tests/test_write_safety.py`, 7 in `tests/test_restore_gate.py`.

### 2026-10-01 - exit paths

- `guard()` follows CPython for a `SystemExit` carrying a message: a string is
  printed and exits 1, `None` is 0. It used to run `int()` over the message and
  show `ValueError: invalid literal for int()` instead of the reason, and
  `migrate --json --comp-dir <bad>` printed no JSON at all (foundbugs #22).
- `migrate.load_components()` raises `ValueError` (a library does not exit) and
  `cli_main` turns it into the documented JSON error object; `fill` and
  `propagate` catch it.

### 2026-09-30 - python 3.9 and health

- `hardware_guide.py` and `contribute.py` got `from __future__ import
  annotations`: a `tuple | None` annotation in a module no test imported turned
  the CI 3.9 job red and nothing caught it. `tests/test_py39_annotations.py` is
  the static guard for every root and test module now.
- `ul8 health` runs the repo suite and reports the pass count, so the TUI and
  CI agree. It never recurses (`PYTEST_CURRENT_TEST`), skips on
  `UL8_NO_TESTS=1` or a missing `tests/`, and reports a suite that cannot run as
  a note, not a pass.

### 2026-09-29 - fingerprint

- the mask decoder zeroed every immediate bit it claims to (M1.5): `0xFF0303FF`
  kept the imm12 bits 6-7 of an `add`/`sub`, `0xFFE0001F` kept the bit-select
  bits 23-21 of `tbz`/`tbnz`. Both families plus cbz/cbnz, ldr-literal,
  ldr/str-imm, movk/movn, adr/adrp and passthrough now have a unit test, and a
  real b2 to b3 run is byte-identical before and after the fix.

### 2026-09-28 - menu keys

- every menu key resolves to its own row: `menu_opt C "Coverage"` and
  `menu_opt P "Propagate"` (uppercase is the research variant, like `B`
  bootstrap vs `b` build), with the case patterns matching the printed keys.
  `tests/test_menu_keys.py` copies the case block into a probe and lets bash
  resolve every advertised key, so a row an earlier pattern claims fails.

### 2026-09-25 - bootstrapping docs

- `profile_gen.py bootstrap <Model> <iOS> [build]` prints the first-offset run
  sheet for a device with no profile: component names from `DEVICE_DB` (never
  the board id), existing profiles, components already on disk, which boards may
  share its kernel or bootloader component, the sources for that build with the
  confidence each lands at, the `blockers:` shape, and the verification gate.
  Read-only, `--json`, exit 1 on an unknown model. 17 tests in
  `tests/test_bootstrap.py`, one of them asserting it writes nothing.
- `docs/BOOTSTRAPPING.md` is the procedure end to end and `docs/GLOSSARY.md`
  defines the terms the output prints (PWND DFU, IMG4, IM4P, TXM, TSS proxy,
  confidence tiers).

### 2026-09-22 - migration automation

- `migrate --json` is the automation interface: one JSON object on stdout (per
  section entries with decimal offsets, counts, `review`, `conflicts`, `wrote`,
  `validation`), no prompt, no ANSI, errors as a JSON object too. The exit code
  is the verdict: 0 ready, 2 review required, 1 bad input.
- `--checkpoint [FILE]`/`--resume [FILE]` write each section as it finishes and
  reuse it (the kernel search is the slow part). The checkpoint stores the path
  and sha256 of both profiles, and a checkpoint from another pair raises
  `CheckpointMismatch` (exit 2) rather than reusing offsets from another build.
  Real b2 to b3 run: 1.59s full, 0.14s resumed, identical results.
- MIT `LICENSE` added.

### 2026-09-21 - fail safely, one dispatcher

- nothing hands the user a traceback: `log_utils.guard()` wraps every entry
  point with 0 ok / 1 clean failure / 2 verification refusal / 3 unexpected
  exception (traceback to the log, one line on screen) / 130 interrupt, and
  `safe_input()` replaces all 32 `input()` calls so a piped stdin stops cleanly
  instead of raising `EOFError`. `CleanExit` is a `BaseException`, so a broad
  `except Exception` cannot swallow an "input ended" stop.
- `cli.py` holds the verb table (description, handler, dependency features) and
  is the only dispatcher; `ul8.py` is a shim and `main.py` is the TUI. The
  duplication is what let `ul8.py version` and `main.py build` open the menu
  instead of dispatching, and help text, the unknown-subcommand list and
  `deps.COMMAND_FEATURES` all read the table now.
- `deps.py` decides from the verb: `PACKAGES`/`FEATURES`/`COMMAND_FEATURES`,
  per-OS install commands (apt/pacman/dnf/brew), PEP 668 detected up front,
  `uv pip install --python` when a venv has no pip, and an explicit `ensurepip`
  hint when there is no installer. Nothing installs without a yes: a pipe or CI
  is report-only, EOF declines, `UL8_AUTO_INSTALL=1` installs unattended and
  `UL8_NO_DEPS=1` skips. A typo'd `--feature` is rejected instead of matching
  no package and reporting "all installed".
- USB and IMG4 fixes for issues #2 and #4: `_get_usb()` returns the `usb.core`
  module but a caller did `usb.core.find(...)` inside a bare `except`, so DFU,
  WTF and PWN detection silently never worked on any platform; and preflight
  compared profile offsets against the compressed IMG4 container instead of the
  payload, which turned a correct local IPSW into a wall of "does not match".
  `img4wrap.py` reads and writes IMG4/IM4P (lzfse via `pyimg4`, bzip2 in
  stdlib) and the macOS-only `tools/img4`/`img4tool` steps fall back to it.
- logging: errors and warnings on screen are mirrored to `usbliter8.log` through
  the `colors` hooks (no call-site edits), with `sys.excepthook`,
  `threading.excepthook`, a session header, 2 MB rotation, and `ul8.py logs
  [--tail/--level/--since/--grep/--clear/--json]`. Long steps are timed and every
  run ends with a forced summary line; a pytest run never appends to the
  developer's log.
- `safety_check.py` + `.github/workflows/tests.yml` + the `protect-main`
  ruleset: the pre-push gate fails on secrets/UDIDs, machine-local home paths,
  tracked generated files, invalid or misnamed profiles, a "verified" claim with
  pending entries and evidence that no longer matches its profile; CI runs
  `compileall`, the gate, every profile through `device_offsets.py validate` and
  the suite on 3.13 and 3.9. `main` takes PRs only.
- `share.py`: ask before sending anything back (profile, component hashes,
  environment, recent warnings), scrub UDID/serial/ECID/home paths/hostnames/LAN
  IPs, print exactly what would go, then send a PR when the run left files in
  `offsets/` (built in a temporary worktree from `origin/main`, the caller's
  checkout untouched) or an issue when it did not. `never` is remembered,
  `UL8_NO_SHARE=1` skips a run, and without a terminal it prints the command
  instead of prompting.

### 2026-09-20 - verification

- `preflight.py` verifies a profile against the real component bytes before
  anything is flashed, with `offsets/evidence/*.json` recording the component
  sha256 and the original bytes, so a profile carried to another board's
  component reports 0 verified and blocks. Statuses: `match`, `plausible`,
  `already-patched`, `changed`, `implausible`, `out-of-range`, `no-op`,
  `skipped`.
- `profile_gen.py coverage`/`gaps` (per-section filled/total matrix, `--json`),
  an in-repo `offsets/canonical.yaml` snapshot, the 0.90 confidence floor for
  anything that writes, and a verdict line at the top of a report.
- `fetch_components.py` resolves IPSW urls itself (ipsw.me for releases, the
  `ipsw.dev` build page for betas) and pulls single components out of a
  multi-GB IPSW over HTTP range (`kczip.py`, ported from W0lfSword) instead of
  downloading the whole file.
- `liter8_import.py` imports an upstream fixture set as a profile and re-checks
  every mapped site against the fetched binary with `--verify-components`, and
  the kernel section is refused when the base and target ship different
  `kernelcache.release.*` components.
- evidence for iPhone12,1 27.0 and iPhone12,3 27.0b2/b3 re-recorded against the
  real components.

## 0.1.0 - 2026-08-12

- 2026-08-12: first beta of the toolkit: guided hardware setup for the RP2350
  (Pico 2 and Cell variants), the CFW build pipeline over an extracted IPSW
  tree, the restore and boot chain, per-device offset profiles with a template
  and validator, and the Python TUI hub.
- 2026-08-12: 21 audit bugs fixed across the CFW pipeline, boot chain, offset
  validator and USB handling.
- 2026-08-12: the beta-to-beta migration engine: AArch64 fingerprint matching
  with capstone verification, confidence tiers (0.95 unique and disasm-class
  match, 0.90 unique string, 0.60 ambiguous, 0.30 multi-hit or delta-inferred),
  and the b2 to b3 oracle table as pytest fixtures.
- 2026-08-12: the wiring diagrams, the connection overview and the comparison
  against the original usbliter8 in the README.
- 2026-08-21: guided setup quit handling polished, Cell device variants added,
  README compacted; cross-device propagation plus `coverage`, and 10 A13
  profiles for 27.0b2/b3.
- 2026-08-29: the `./usbliter8` terminal script and the offset contribution flow.
- 2026-09-04: the wolf cmatrix scramble at menu start (port of W0lfSword's
  `scramble_wolf`).

