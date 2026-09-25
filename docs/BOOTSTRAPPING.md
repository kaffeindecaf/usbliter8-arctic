# Bootstrapping offsets for a new device

How to get the **first** offset profile for a device and iOS build that has none,
without a second machine and without guessing. This is the procedure for the
devices `profile_gen.py coverage` lists as `no profile`: the A12 iPhones
(iPhone11,2 / 11,4 / 11,6 / 11,8), the A12 iPads (iPad11,1 to iPad11,7), the
bootloader sections of iPhone12,5 and iPhone12,8, and any iOS 26.x build, since
iOS 27 dropped the A12 iPhones and no 27.x IPSW exists for them.

Two rules come before everything else:

- A profile that does not match the component bytes it will patch is a brick
  risk, so nothing here ends without `preflight` verifying the sites against the
  real binary and `--record` storing the evidence.
- Never fill a gap with another device's data to make a report go away. If the
  data does not exist, the section gets a `blockers:` entry that says what is
  missing (see step 5).

## 0. What you need in front of you

- The device itself only if you intend to verify by flashing. Every command
  below is host-side.
- Python 3.9 or newer with `pyyaml` and `capstone`, plus `pyimg4` for the
  compressed 27.x components. `deps.py` offers the right install command per OS.
- The IPSW for the build you are contributing for (Apple still ships it, or you
  already have the file). Component names are resolved from the IPSW entry list,
  so the build has to be one whose files you can list.

## 1. Name the components correctly before anything else

The component file name is the internal component name, **not** the board id.
iPad 9 (j181ap) boots `iBSS.ipad12p...`, iPad 8 (j171ap) `iBSS.ipad11b...`, and
the mini 5 and Air 3 share `iBSS.j210...`. Deriving the name from the board is
how the iPad 9 failed every build once already.

```bash
# what the shipped IPSW actually contains for this device and build
python3 fetch_components.py --device <Model> --build <build> --list
```

Then make sure the device has a row in both places, or the resolver and the
builder disagree:

- `profile_gen.py` -> `DEVICE_DB`: `name`, `soc`, `board`, `apticket`,
  `ibss_component`, `kernel_component`.
- `offsets/sources.yaml` -> `kernel_components`: model to kernelcache entry name.

The A12 side of that table, verified from the shipped entry names:

| Model | Device | Board | Component stem | Kernelcache |
|---|---|---|---|---|
| iPhone11,2 | iPhone XS | d321ap | d321 | kernelcache.release.iphone11 |
| iPhone11,4 | iPhone XS Max (CN) | d331pap | d331p | kernelcache.release.iphone11 |
| iPhone11,6 | iPhone XS Max | d331ap | d331 | kernelcache.release.iphone11 |
| iPhone11,8 | iPhone XR | n841ap | n841 | kernelcache.release.iphone11b |
| iPad11,1 / 11,2 | iPad mini 5 | j211ap / j212ap | j210 | kernelcache.release.ipad11 |
| iPad11,3 / 11,4 | iPad Air 3 | j213ap / j214ap | j210 | kernelcache.release.ipad11 |
| iPad11,6 / 11,7 | iPad 8 | j171ap / j172ap | ipad11b | kernelcache.release.ipad11b |

A12 ticks are `t8020`. Get the stem wrong here and every later step works on the
wrong bytes.

## 2. Pull only the components you need

```bash
python3 fetch_components.py --device <Model> --build <build> --all --jobs 4
python3 fetch_components.py --device <Model> --build <build> --extract-payload
```

The default output is `research/extracted/<Model>_<ios>_<build>/`, which is
exactly where `preflight` auto-discovers components, and `provenance.json` lands
next to them with the entry name, size and sha256 of the file and of the payload.
Range fetching means a bootstrap costs a few MB, not a 6 GB download.

Remember which bytes offsets index: the **decompressed payload**, not the IMG4
container (see `docs/GLOSSARY.md`). `--extract-payload` gives you the raw payload
for the ones that are compressed.

## 3. Find a source of truth for the literal patch sites

In this order, best first. The first two carry real sites for a real build and
board, which is what a first profile needs; fingerprinting is the third option
because a brand new device has no same-device base profile to fingerprint from.

**a. An upstream script for the exact build and board.** The usbliter8-fun work
dirs (wh1te4ever, 34306) hold the literal sites in `make_cfw.py`. Create the
profile from the template, fill the entries by hand, then diff your work against
the script it came from:

```bash
python3 profile_gen.py create iPhone11,6 26.0 <build>
python3 source_audit.py script <usbliter8-fun>/work-<build>/make_cfw.py offsets/iPhone11,6_26.0.yaml
```

The audit prints `COVERED`, `PARTIAL`, `MISMATCH`, `REVIEW`, `MISSING` and
`PROFILE-ONLY` per entry. `MISSING` and `MISMATCH` have to be explained before
you move on; `REVIEW` on PC-relative sites (adrp/add redirects) is expected,
because their immediate depends on their own address and is not comparable
across builds.

**b. A Liter8 fixture set for that build and board.** Fixtures record offset,
original bytes, replacement bytes and the component sha256 per site:

```bash
python3 liter8_import.py --fixtures <Liter8>/fixtures --build <b> --board <board> \
    --model <Model> --ios <ios> --verify-components research/extracted/<dir>
```

Without `--write` it prints what it would write. With `--verify-components` it
re-checks every mapped site against the fetched binary first. Add `--write` only
once the dry run reads clean.

**c. Fingerprint from a nearby build of the same device.** When the device has a
profile for another build, `migrate` is the right tool and lands at 0.95 or 0.90
for most entries:

```bash
mkdir -p research/comp-dir/base research/comp-dir/target
# base/   = raw components of the base build
# target/ = raw components of the build you are bootstrapping
python3 profile_gen.py migrate offsets/<Model>_<old>.yaml offsets/<Model>_<new>.yaml \
    --comp-dir research/comp-dir --checkpoint research/work/ck.json --json > /tmp/m.json
echo "rc=$?"
```

The exit code is the verdict: 0 ready, 2 review required, 1 bad input. Read the
`review` and `not_written` arrays before `--auto`. `--checkpoint` writes each
section as it finishes so a killed run resumes instead of searching the kernel
again. For reference, the recorded b2 to b3 run for iPhone 11 Pro: 15 entries at
HIGH, 23 unresolved, exit 2.

Cross-device fingerprinting (an A13 base profile against A12 components) is a
different risk class: expect 0.60 and 0.30 for most entries, treat every hit as a
candidate, and never accept one because it looks plausible. If you have no
same-device base profile and no upstream source, the honest outcome is a
`blockers:` entry, not a filled section.

**d. Complete a profile you already have components for.** This is how the iPad 9
profiles got 15 bootloader offsets each at 0.95:

```bash
python3 profile_gen.py fill offsets/iPad12,1_27.0b2.yaml --from offsets/iPhone12,3_27.0b2.yaml \
    --comp-dir research/comp-dir --sections ibss,ibec,txm
```

`fill` only touches entries that are still `pending`, and it refuses to write
anything below the 0.90 floor.

## 4. Apply the confidence rules

- 0.95 unique masked hit plus disassembly class match.
- 0.90 unique string site.
- 0.60 ambiguous or class mismatch.
- 0.30 multi-hit or delta-inferred, always LOW, never auto-written.

Nothing below 0.90 is written automatically. A wrong offset rated HIGH is a brick
risk, which is why the gate is uniqueness **and** disasm class match rather than
one of the two. If a value disagrees with upstream, decode both encodings and
compare their targets before "fixing" the profile: a branch that should reach
`#+0x28` encodes as `0x1400000a`, and `0x14000028` jumps into unrelated code.

## 5. Kernel section: per component, or blocked

Kernel offsets live inside one specific kernelcache binary, so an A12 profile may
not inherit an A13 kernel section, and only boards that ship the same component
may share one (iPhone12,3 and iPhone12,5 do, nothing else does). If the
kernelcache cannot be decrypted (no published IV and key for that SoC and build),
record that instead of inventing values:

```yaml
blockers:
  kernel:
    entries: 18
    reason: >-
      kernelcache offsets are per component: this board boots
      kernelcache.release.iphone11, while these values came from another board.
      No verified iphone11 offsets exist yet and no wiki key is published for
      this build, so they cannot be derived offline.
    needs: a contributor with this device, or a published key for that kernelcache
```

Blocked entries count as unresolved everywhere, are refused by `patch_kernel`
even under `--force`, and show up in the per-build patch manifest. `profile_gen.py
gaps` is the check that keeps this honest: it lists every profile whose kernel
section came from another board's component (8 profiles as of 2026-09-25).

## 6. Verify before it counts as done

```bash
python3 device_offsets.py validate offsets/<profile>.yaml        # exit 1 while entries are invalid
python3 preflight.py offsets/<profile>.yaml --quiet              # auto-finds research/extracted/<dir>/
python3 preflight.py offsets/<profile>.yaml --components research/extracted/<dir> --record
python3 profile_gen.py gaps                                      # per-section filled/total matrix
python3 safety_check.py                                          # leaks, names, evidence drift
python3 -m pytest tests/ -q
```

`--record` writes `offsets/evidence/<profile>.json` (component sha256 plus the
original bytes at every verified site). That file is committed with the profile,
and it is what makes the profile self-verifying: pointed at another board's
component it reports 0 verified and N bad, and the build gate blocks.

Flip `verification:` from `pending` or `partial (...)` to `verified` **only**
once there are no pending entries and no blockers left, because
`safety_check.py` fails the push on a `verified` claim that still has either. A
"verified" claim with a skipped section is a lie the CI catches, not a style
choice.

## 7. Send it in

The change is the profile, its evidence file, and the `DEVICE_DB` plus
`sources.yaml` rows if the device was new:

```bash
./usbliter8 contribute status
./usbliter8 contribute pr offsets/<profile>.yaml     # PR description + git commands
```

`main` is protected: push a branch and open a PR. The required `tests` job has to
be green before it merges, and it runs `safety_check.py`, every profile through
`device_offsets.py validate`, and the suite on Python 3.13 and 3.9.

## One-sitting run sheet

Print the sheet for your device first: `python3 profile_gen.py bootstrap <Model> <iOS>
[build]` (`./usbliter8 bootstrap <Model> <iOS>` through the wrapper, `--json` for a
machine-readable copy) names that board's actual component, the profiles that already
exist, the components already on disk, which boards may share its kernel section, and
the sources available for that build with the confidence each one lands at. The steps
below are the same procedure in prose.

1. `fetch_components.py --device <Model> --build <build> --list`, confirm the
   component names, add the `DEVICE_DB` and `kernel_components` rows.
2. `profile_gen.py create <Model> <iOS> <build>`, open the profile in `$EDITOR`.
   Leave `pending: true` on everything you have not found yet.
3. Fetch the components with `--all --jobs 4 --extract-payload`.
4. Get the sites: upstream script, Liter8 fixtures, or `migrate`/`fill` from a
   base profile. Audit or verify each path.
5. Kernel blocked? Write the `blockers:` entry with a reason and a `needs`.
6. `device_offsets.py validate` -> `preflight --record` -> `profile_gen.py gaps`
   -> `safety_check.py` -> `pytest tests/ -q`.
7. Only now set `verification:` and open the PR with the evidence file.

Steps 1 to 4 are the whole cost of a new device. Once the bootloader sections
exist for one build, the next beta is a `migrate` run.
