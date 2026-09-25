# Glossary

Terms this toolkit prints, and what they mean in the usbliter8 chain. Every
definition here is the one the code assumes, so the doc is a lookup table for
output you do not recognize rather than a general iOS security primer.

## A to D

**APTicket** - the signed ticket that authorizes a restore of one build for one
device. A profile records the tag it was built against (`t8020` for A12, `t8030`
for A13) and the restore asks the TSS proxy for a ticket with that tag.

**blockers** - a profile field for data that looks valid but must never be
applied, with a count, a reason and what is missing. Blocked entries count as
unresolved everywhere, are refused by `patch_kernel` even under `--force`, and
are printed in the build's patch manifest. The iPad 9 kernel section is the
current example: those offsets belong to a different kernelcache component.

**boot-args** - the kernel command line iBoot hands to XNU (`-v wdt=-1 rd=md0
-restore`). The iBSS/iBEC `boot_args_adrp` and `boot_args_add` sites redirect the
pointer, `boot_args_string` is the string itself.

**component** - one file inside an IPSW (`Firmware/dfu/iBSS.n104.RELEASE.im4p`,
the root-level `kernelcache.release.iphone12b`, and so on). Offsets are per
component, which is why the resolver in `components.py` exists: the file name is
the internal component name, not the board id, so an iPad 9 boots
`iBSS.ipad12p...` and the mini 5 and Air 3 share `iBSS.j210...`.

**DeviceTree (FDT)** - the flattened device tree describing the hardware. Patched
in-tree by `dt_patch.py` (content-protect removal, `no-effaceable-storage`,
`boot-ios-diagnostics`, ephemeral storage, optional `system_rw`).

**DFU** - Device Firmware Upgrade mode, the state a pwned device sits in. WTF and
restore mode are the other USB states `pwn_utils.py` distinguishes.

**evidence** - `offsets/evidence/<profile>.json`, the component sha256 plus the
original bytes `preflight --record` saw at each site. It is committed, which is
what makes a profile self-verifying: the same profile against another board's
component reports `changed` and blocks the build.

## E to K

**fingerprint (confidence)** - how a patch site is re-found in another build: the
AArch64 instruction with its immediates wildcarded, searched in the target
binary, verified with capstone. 0.95 unique hit plus disassembly class match,
0.90 unique string site, 0.60 ambiguous or class mismatch, 0.30 multi-hit or
delta-inferred. Nothing below 0.90 is written without review and delta inference
is LOW by design.

**iBSS** - the first iBoot stage, loaded by SecureROM in DFU mode. Once the device
is pwned it accepts an unsigned iBSS, and that is where the boot-args and
Image4-validation patches go.

**iBEC** - the second iBoot stage, loaded by iBSS. Patched for the same
boot-args and nonce handling. It can be byte-identical to iBSS on a board
(n104 on 27.0), so compare sha256 before filling one section from the other.

**iBoot** - Apple's bootloader. iBSS and iBEC are its two DFU stages, and both
run before the kernelcache is loaded.

**IM4P** - the DER container that holds one component payload together with its
fourcc (`iBSS`, `ibec`, `krnl`, `TXM`) and compression description.

**IMG4 / IM4M** - the file format around IM4P: the payload plus the IM4M
manifest, which carries the signed hashes. The payload is usually
lzfse-compressed (a 1.8 MB iBSS container holds a 2.8 MB payload), and profile
offsets index the **decompressed payload**, so anything reading a component
straight from an IPSW must unwrap it first. `img4wrap.py` is that codec;
comparing container bytes against offsets makes every site look wrong.

**kernelcache** - the prelinked XNU image and the component that decides whether
kernel offsets may be shared between boards. iPhone 11 boots
`kernelcache.release.iphone12b`, the 11 Pro and 11 Pro Max `...iphone12`, the
SE 2 `...iphone12c`, the iPad 9 `...ipad12p`. Kernel offsets are per component,
never per SoC. Kernelcaches ship encrypted, so preflight reports them `skipped`
until a wiki IV and key exist.

## O to R

**offset** - a byte offset into the decompressed payload of one component of one
build, with the bytes to write at it. An offset from another board is a brick
risk, not a starting point.

**patch site** - the address a patch writes to. String patches write ASCII,
instruction patches write an encoding. PC-relative sites (the adrp/add pairs that
redirect a pointer into the image) are not comparable across builds: the correct
immediate depends on the site's own address, which is why the audit rates them
REVIEW instead of MISMATCH.

**pending** - a per-entry profile flag meaning "not discovered yet". Pending
entries are skipped by validation and by the flash gate, never treated as real
offsets. `offsets/template.yaml` ships every entry pending so a new profile
cannot look flashable.

**preflight** - the check that verifies a profile against the real component
bytes before a build. Statuses: `match`, `plausible`, `already-patched`,
`changed`, `implausible`, `out-of-range`, `skipped`. Exit 2 means blocked.

**PWN DFU / pwned** - the state after the RP2350 has run the usbliter8 exploit
against SecureROM and the device accepts unsigned firmware. The exploit is
re-applied on every cold boot, so this is a tethered jailbreak: no persistent
install.

**restore ramdisk** - the ramdisk iBoot boots for a restore, and the home of the
ASR and FDR patches. On iOS 26/27 it is a bare root-level `.dmg` in the IPSW
whose offsets target binaries inside the mounted image, which this build path
cannot mount and re-sign, so the build reports the section as unappliable
instead of pretending. Classic `.dmg.im4p` layouts are still patched in place.

**RP2350** - the microcontroller board that runs the exploit. RP2040 is not
enough, A13 needs the RP2350. VBUS is 5V, never solder to 3V3, and firmware
lands on the board as a UF2 whose magic is validated before flashing.

## S to Z

**SEP** - Secure Enclave Processor. Panic handling for it is one of the kernel
patches.

**SSHRD** - the SSH ramdisk. Boot it for filesystem access (mount the rootfs,
copy over a bootstrap) before you do anything else on the device.

**TSS proxy** - a local service that answers the restore's TSS (Apple signing
server) requests with the apticket for the requested build. The toolchain starts
and stops it around a restore, reads its own config, and reports a dead proxy
instead of letting the restore fail anonymously.

**TXM** - the Trusted Execution Monitor, the component that enforces module and
constraint validation on these builds. The `txm` section of a profile carries its
`query_module0/1/2`, constraint-signature and secure-channel sites.

**UF2** - the firmware file format the RP2350 bootloader accepts (drag it onto
the `RPI-RP2` volume). Prebuilt boards come from Octopus1633/usbliter8-firmware.

**usbliter8** - the A12/A13 tethered jailbreak chain this repo automates: RP2350
against SecureROM, pwned DFU, patched boot chain. It is not checkm8, which covers
A5 to A11.

**verification** - the profile-level claim: `pending`, `imported`,
`partial (...)` or `verified`. `safety_check.py` fails the push when a profile
claims `verified` while it still has pending entries or blockers.
