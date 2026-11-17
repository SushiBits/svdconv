# Development notes (train of thought)

This file records how `svdconv.py` came to be, what was learned from the
reference implementation, and the rules that were reverse-engineered from its
output. It is meant as an engineering log, not as user documentation — see
[README.md](README.md) for that.

## 1. The request

> Recreate the SVD header generator I use in embedded development:
> input is an XML file following `check/CMSIS-SVD.xsd`, output is a device
> header with all register and bit-field definitions. The reference
> implementation is `C:\Keil_v5\UV4\SVDConv.exe`; use it as an oracle. The
> program must be Python, cross-OS portable, freely distributable, and shipped
> with three SVD/header pairs under `check/` that were produced with
>
> ```
> $(SVDCONV) $< -o $(dir $@) --generate=header --fields=macro --fields=enum
> ```

The three reference pairs are LPC2103 (CPU "other"), LKS32MC03x (Cortex-M0) and
MSPM0L130X (Cortex-M0+). Together they cover clusters, unions, derived
peripherals, register arrays, mixed 8/16/32-bit register sizes, enumerated
values, and long wrapped descriptions.

## 2. Plan

1. Read the XSD to bound the feature space.
2. Characterise SVDConv's output format *precisely* (columns, blank lines,
   wrapping, padding rules) from the three reference headers.
3. Probe the oracle with synthetic SVDs for anything the references do not
   show (empty enums, unusual dims, CPU variants, error paths).
4. Write a single-file converter, then iterate with a diff harness until the
   generated files are byte-identical to the references.
5. Document, test, commit.

The key decision was to aim for **byte-identical output**. Anything less makes
it impossible to tell a formatting bug from an intentional difference, and the
reference headers are already in the repository as a golden baseline.

## 3. What the oracle told us

The oracle is happy to run on synthetic input, which made it possible to pin
down details that the three real devices do not exercise. The probes live in
`.probe/` while working (they are not part of the final tree); each probe is a
hand-written SVD plus the header SVDConv produced for it.

Useful invocations:

```console
C:\Keil_v5\UV4\SVDConv.exe PROBE.svd -o out --generate=header --fields=macro --fields=enum
```

Things that the oracle made obvious early on:

* The output file is named after the `<name>` of the device, not after the
  input file (`RENAMED.svd` still produced `PROBE.h`).
* The header banner contains the absolute path of the SVD, the absolute path
  of the generated file, the current date and the modification time of the
  SVD. These four lines are the only non-deterministic part of the output.
* `--generate=partition`/`SDF` produce completely different artifacts; only
  `header` matters here.
* Diagnostics are printed to stdout (with `-b` optionally mirrored to a file)
  and the exit code is 2 as soon as an error was detected, even though the
  header is still written.

## 4. Reverse-engineered layout rules

Every rule below was confirmed by measuring the reference headers and by
probing the oracle; several of them are surprising and cannot be guessed from
the CMSIS specification.

### Lines and columns

| Element | Rule |
| --- | --- |
| Line width | 129 characters, ` */` right-aligned at the end |
| Trailing comment | starts at column 48 |
| Bit macro | `#define <NAME>` (column 8), value at column 42, comment at column 68; if the name is too long the value starts one column after the name and the comment one column after the value |
| `#define <P>_BASE` | value right-aligned so that it ends at column 48; a single space when the name is too long |
| Peripheral declaration | name at column 8 padded to 36, cast `((<TYPE>*)` there, base symbol at column 63 (or cast end + 2, whichever is further right) |
| Enum member | name at column 2, `=` at column 39 (`name.ljust(37)`, one space minimum), `= <value>,` and the comment at column 48 |
| IRQ member | name at column 2, `=` at column 28, value `%4d`, comment at column 48 |
| Structure member | two blanks, access macro padded to 5, one blank, type padded to 10 (at least one blank), member name |
| Union member | four blanks, access padded to 5, one blank, *unpadded* type, one blank, member name |
| Structure close | `} <TYPE>;` padded to column 48 for the `Size = N (0xN)` comment |

### Comment wrapping

Comments are **not** wrapped at the line width. SVDConv appends words to the
current line until it has reached at least 59 characters (first line) or 54
characters (continuation lines), then breaks. The continuation lines are
indented to the comment text column (53) and only the *last* line is padded so
that ` */` ends at column 129. Lines that are still too long simply overflow
past column 129 — that is why the MSPM0 reference header contains lines of 132
characters.

The number of an interrupt comment is padded to an even number of characters
(`0  WDT`, `10 SPI`, `100  DMA`), which is the reason for the irregular
spacing in `IRQn_Type`.

### Section separators

Two kinds of separators exist:

* *Major* separators have exactly 16 `=` on both sides and the title centred
  in the remaining 91 characters (left-biased when odd).
* *Minor* separators keep two blanks around the title and distribute the rest
  of the 119 inner characters as `=` fillers.

Cluster and peripheral structure blocks use the major form; register and
field headings in the Pos/Mask and Enum sections use the minor form. The
configuration heading and the "anonymous unions" headings are also minor
separators, even though they look like major ones at first glance.

### Reserved members

Gaps between registers are filled so that the structure stays naturally
aligned: first pad to the alignment of the next register using 1- and 2-byte
reservations, then fill the rest with the largest type that is aligned at the
current offset. The resulting members are named `RESERVED`, `RESERVED1`, … and
are emitted without a comment. `compute_layout()` implements exactly this.

Structures are padded at the end to the alignment of their widest member
(e.g. `DSP_Type` in the LKS32 file ends with a 16-bit `RESERVED1`).

### Structures, typedefs and macro prefixes

* A peripheral structure type is called `<headerStructName>_Type` (or
  `<NAME>_Type` when there is no `headerStructName`). The heading uses the
  *peripheral name*, while the Pos/Mask heading uses the *macro prefix*.
* Cluster structure types are `<peripheral name>_<cluster>_Type` (note: the
  peripheral *name*, not `headerStructName`), or `<headerStructName>_Type`
  when the cluster defines one.
* Pos/Mask macros of a cluster use `<peripheral headerStructName>_<cluster>_…`.
* Enum identifiers use the peripheral *name* for peripherals and the cluster
  *type name* for clusters, so a peripheral with `<headerStructName>ADC12` and
  the name `ADC0` produces `ADC12_GPRCM_PWREN_ENABLE_Pos` macros but
  `ADC0_GPRCM_PWREN_KEY_Enum` enumerations.
* Derived peripherals reuse the base structure and do not emit new types,
  new clusters, new macros or new enumerations.
* Derived clusters only contribute a member (`__IOM DATETIME_Type ALARM;`).

### Cores

The interrupt table and the `__*_PRESENT` macros depend on `<cpu><name>`:

| Core | Interrupts | Presence macros |
| --- | --- | --- |
| `other` | none predefined | the ones declared in the SVD |
| `CM0` | Reset…SysTick | `__MPU_PRESENT`, `__FPU_PRESENT` |
| `CM0PLUS` | Reset…SysTick | adds `__VTOR_PRESENT` (always emitted) |
| `CM3` | Reset…SysTick | `__MPU_PRESENT`, `__FPU_PRESENT` |
| `CM4`/`CM7` | Reset…SysTick | adds `__FPU_DP`, `__DSP_PRESENT`, caches |

The `__xxx_PRESENT` macros are emitted when the corresponding element is
present in the SVD (except `__VTOR_PRESENT`, which Cortex-M0+ always gets).

`<revision>rApB</revision>` becomes `0x0A0BU`.

### Enumerated values

* Values that do not fit into the field are dropped with a warning; negative
  values are dropped as well.
* 32-bit values are printed as signed integers, so `0xFFFFFFFF` becomes `-1`.
* Every enumerator carries a trailing comma, including the last one.
* `<isDefault>true</isDefault>` entries are not emitted.
* `<headerEnumName>` replaces both the type name (`<name>_Enum`) and the
  enumerator prefix; `<usage>` only affects the type name (`_R`/`_W`/`_RW`).

## 5. Iteration log

The converter was developed against a diff harness that normalises the four
non-deterministic header lines and then compares line by line. Diff sizes over
time (LPC2103 / LKS32 / MSPM0):

```
4710 / 4122 / 18438   first working version (structure and sections in place)
1960 /   42 / 10528   after clusters, unions, reserved fill and derivations
   0 /    0 /  4122   after names, prefixes and enum formatting
   0 /    0 /   366   after signed enum values and padding rules
   0 /    0 /     0   final
```

Bug classes that the diff harness caught, in the order they were fixed:

1. `comment_lines()` returned lists that were appended instead of extended.
2. A local variable named `prefix` shadowed the new `prefix` parameter of
   `comment_lines()`, so every comment contained the whole source line.
3. Missing descriptions: SVDConv falls back to the element name (and to
   `[0..N]` for `dim` elements).
4. Cluster member sizes were not known, which corrupted all following
   reserved gaps.
5. Enum identifiers used the wrong prefix (see above).
6. `0x%08XUL` formatting of base addresses; long names need exactly one blank.
7. The license text of an SVD uses literal `\n` sequences and is indented.

## 6. Deliberate deviations

* SVDConv emits invalid C (and sometimes loops for a very long time) on
  unaligned registers, registers wider than 32 bits and nested cluster
  arrays. `svdconv.py` emits an `#error` line, skips the register and
  continues.
* A word that is longer than the wrap limit is not split, so such a comment line can overflow the 129 character width exactly like in SVDConv.
* The interrupt-number limit of 240 (0…239) is enforced for every core;
  SVDConv applies smaller limits depending on the CPU and its own heuristics.

## 7. Makefile safety (second iteration)

The first version still exited with 2 for "model errors" such as a
`derivedFrom` that points at a peripheral which does not exist. Vendor SVD
files contain those, and a make recipe then dies. Probing the oracle made the
problem obvious:

```
$ SVDConv.exe WARNONLY2.svd -o out --generate=header --fields=macro --fields=enum
*** WARNING M335: WARNONLY2.svd (Line 37)
  Value 'TOO_BIG:99' does not fit into field 'MODE' [1..0:2].
Found 0 Error(s) and 1 Warning(s).
$ echo $?
1
```

A *pure warning* therefore aborts the build even though the header is
complete and correct. The redesign:

* Two severities only. ``ERROR`` means "no header was produced" (unreadable
  SVD, missing `<device><name>`, unwritable output, bad license file). Every
  defect in the description itself is a ``WARNING``: unknown or circular
  `derivedFrom`, values that do not fit, interrupts out of range, impossible
  register sizes.
* `Reporter` owns the counters and the exit code: errors always fail,
  warnings only fail when `-Werror` (alias `--warnings-as-errors`),
  `--strict` or `SVDCONV_WARNINGS_AS_ERRORS=1` ask for it.
* A closing `Found N Error(s) and M Warning(s).` line (as SVDConv prints it)
  always tells the reader what happened, and is repeated on stderr when
  `--quiet` would otherwise swallow a failing run.
* `running_under_make()` looks at `MAKELEVEL`/`MAKEFLAGS`/`MFLAGS`/
  `GNUMAKEFLAGS`; when the tool detects make it states in the banner that
  warnings will not fail the build. The decision does not depend on the
  environment though — the default is safe everywhere, so a CI run and a
  local `make` behave identically.
* Verified end to end with a real GNU make recipe in WSL2:

  ```
  === default (must succeed even though DEFECTIVE.svd has defects)
  make exit: 0
  === strict (must fail on the defective SVD)
  make exit: 2
  === warnings-as-errors from the environment
  make exit: 2
  ```

## 8. Arbitrary CPU names (second iteration)

The oracle rejects anything it does not know:

```
*** ERROR M202: CPUARM7TDMIS.svd (Line 10)
  Parse error: <name> = 'ARM7TDMI-S'
*** ERROR M329: CPU Type is not set, using default (Cortex-M3)
```

and silently maps unknown-but-valid names (`RV32IMAC`, `RISCV`) to Cortex-M3,
which produces a completely wrong interrupt table for a RISC-V device.

`resolve_core()` therefore works in two steps: a case-insensitive lookup in
`CORE_INDEX` first, and a fallback that copies the `other` profile but keeps
the SVD name for `#include "core_<name>.h"` and for the section headings. The
`__OTHER_REV` macro and the presence-macro policy stay those of `other`, so a
hand written `core_<name>.h` only has to provide the CMSIS intrinsics, not a
new set of configuration macros. For the LPC2103 SVD (currently
`<cpu><name>other</name>`) changing the name to `arm7tdmi-s` now yields:

```c
/* ============================  Configuration of the arm7tdmi-s Processor and Core Peripherals  ============================= */
#define __OTHER_REV                 0x0000U     /*!< other Core Revision */
...
#include "core_arm7tdmi-s.h"                    /*!< arm7tdmi-s processor and core peripherals */
```

This is a warning-free downgrade: an unknown name is *informational*, not a
warning, because describing a non-CMSIS core is a valid use of the format.

## 9. What I would do next

* Add a `--check` mode that only validates the SVD and reports diagnostics
  with the same message identifiers SVDConv uses.
* Support `--generate=partition`/`SDF`.
* Generate the `_Pos`/`_Msk` macros for CMSIS "struct" style outputs
  (`--fields=struct`).
* Optionally avoid rewriting the header when only the `@date` line would
  change, so that make does not rebuild everything downstream on every run.

## Appendix: the prompts this work was driven by

1. The initial request (verbatim):

   > This folder is called svdconv. I want you to recreate the following tool
   > I use very frequently in embedded development:
   >
   > * Input: an XML file adhering to the XSD in check/CMSIS-SVD.xsd
   > * Output: a device header including all devices register definitions and
   >   bitfield definitions.
   >
   > The reference implementation of the converter is installed at
   > C:\Keil_v5\UV4\SVDConv.exe. Feel free to poke it as an oracle. Try to
   > emulate its command line switches too but this is not a hard requirement.
   >
   > The resulting program need to be in Python. It can have dependencies. If
   > so, please include it as submodules here. Try to make the code cross OS
   > portable. Use my WSL2 instance for verification if you so desires.
   >
   > I have provided three sets of SVD files and outputs under check folder,
   > all of which outputs of SVDConv with this command line (taken out of
   > project Makefile):
   > -$(SVDCONV) $< -o $(dir $@) --generate=header --fields=macro --fields=enum
   >
   > Make sure the resulting code can be freely distributed. I am dropping in
   > my LICENSE.md file here.
   >
   > Deliverables:
   > * svdconv.py - the tool itself
   > * README.md - Documentation of the tool
   > * PROMPTS.md - A dump of your train of thought.
   > * Properly cadenced Git commits with proper messages.

2. Follow-up question and answer about licensing (no LICENSE.md existed in the
   repository):

   * Question: which license should the project use?
   * Answer: BSD-3-Clause, copyright "2026 HIKARU Infotech Shanghai Ltd."

3. The result was verified three ways: the reference headers regenerate
   byte-identically on Windows and in WSL2 Ubuntu, the unit tests pass in both
   environments, and the tool was additionally exercised against a dozen
   synthetic SVD files that were fed to SVDConv as an oracle.

4. Second iteration request (verbatim):

   > * Make sure this code is Makefile-safe - that is, SVDConv emits a non-zero
   >   return code on warnings as well as errors, while vendor SVD often
   >   contained warnings, and that trips GNU make. If you are not
   >   Makefile-safe, figure out a way to detect if you are being called within
   >   make and adjust behavior accordingly.
   > * A quality of life patch for myself here: allow any string in `<cpu>`. If
   >   it is not a recognized Cortex series processor, treat it as an alias of
   >   "other" but when emitting the core_<cpu>.h header use the name I put in
   >   `<cpu>`. (I am abusing SVD for non Cortex-M CPUs. LPC2103 is already an
   >   ARM7TDMI-S while a few RISC-V RV32IMAC are in the pipeline.)

   Responses: sections 7 and 8 of this document describe what was changed and
   the oracle evidence that motivated it.

5. Third iteration request (verbatim):

   > Two more fixes:
   >
   > * --warnings-as-errors ehh too long. This one looks better: -Werror.
   > * Pylance is screaming at me. Silence it.

   What was done:

   * The switch is now `-Werror` (gcc style); the long spelling stays as an
     alias and `--strict`/`SVDCONV_WARNINGS_AS_ERRORS` keep working.
   * Pylance (analysis mode "standard") reported six problems in
     `svdconv.py`, all fixed in the code rather than silenced (commit
     "chore: fix the problems Pylance reports"):
       - `Field.width` subtracted `self.lsb` whose type was `int | None`;
         `lsb` now returns `int` and both properties are annotated.
       - `RegisterInfo.cluster_block`/`cluster_type_name` and
         `PeripheralInfo.svd_name`/`base_address` were created on the fly,
         so the inferred types were `None`/unknown.  They are now declared
         in the constructors (the two peripheral attributes are constructor
         arguments), which also removed three duplicate assignments in
         `Model._build`.
       - two hints: an unused `entries` local and an unused `metavar` loop
         variable.
   * `from __future__ import annotations` was not needed (the few
     annotations are plain or quoted strings), so it was dropped; the
     documented minimum is now Python 3.7, which the test suite already
     required for `subprocess.run(capture_output=...)`.
   * `workspace/diagnostic` reports zero items for the whole workspace, the
     31 tests pass and the three reference headers are still byte-identical.

