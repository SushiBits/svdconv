# svdconv — CMSIS-SVD to C device header converter

`svdconv.py` converts a [CMSIS System View Description][svd] (SVD) file into the
C device header that CMSIS projects expect: peripheral structure typedefs,
register bit `_Pos`/`_Msk` macros and `_Enum` enumerations.

It is a from-scratch, dependency-free replacement for the header generator of
ARM's `SVDConv.exe` (V3.3.46) and reproduces its output layout faithfully, so
generated headers can replace the ones produced by

```console
SVDConv.exe <device>.svd -o <outdir> --generate=header --fields=macro --fields=enum
```

with a byte-for-byte identical result (see [Verification](#verification)).

## Requirements

* Python 3.6 or newer.
* No third-party packages — only the Python standard library is used, so the
  tool runs on Windows, Linux and macOS alike.

## Usage

```console
$ python3 svdconv.py [options] <device.svd>
```

The output file is named after the `<name>` element of the device description
(`<name>.h`) and is written to `--outdir` (default: the directory of the input
file).

### Command line

| Option | Description |
| --- | --- |
| `-o`, `--outdir DIR` | Output directory (default: directory of the SVD file). |
| `--generate KIND` | What to generate. Only `header` is supported (the default). `partition`/`SDF` are rejected. |
| `--fields TYPE` | Field generation; may be given several times. `macro` generates the Pos/Mask sections, `enum` the enumerated-value sections, `struct`/`struct-ansic` are accepted for compatibility (structures are always generated). Default: `macro` and `enum`. |
| `--license-file FILE` | Use `FILE` as the header comment of the generated file. |
| `--create-folder` | Create the output directory if it does not exist. |
| `--quiet` | Do not write progress messages to the console. |
| `-v`, `--verbose` | Print extra progress information. |
| `-b`, `--log FILE` | Write the diagnostics to `FILE` instead of stdout. |
| `--suppress-warnings` | Do not print warnings. |
| `--strict` | Accepted for command line compatibility; warnings are always printed. |
| `--version` | Print the program version and exit. |
| `-h`, `--help` | Print the help text and exit. |

The remaining SVDConv switches (`--suppress-path`, `--show-missingEnums`,
`--allow-suppress-error`, `--under-test`, `--nocleanup`, `-x/--diag-suppress`,
`-w`, `--debug` and `-n`) are accepted so that existing build rules keep
working, but they have no effect on the generated header.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | The header was generated. |
| `1` | A command line argument was wrong (see `--help`). |
| `2` | The SVD could not be read or generated, or errors were detected. |

## Example

```console
$ python3 svdconv.py LPC2103.svd -o include --create-folder
svdconv 1.0.0 - CMSIS-SVD device header generator

Generated: include/LPC2103.h
```

## What is generated

For each device the header contains, in this order:

1. **Interrupt Number Definition** — `IRQn_Type` with the architecturally
   defined interrupt numbers of the selected core followed by the
   device-specific interrupts sorted by number (0 … 239).
2. **Processor and Core Peripheral Section** — the `__CMx_REV`,
   `__NVIC_PRIO_BITS`, `__Vendor_SysTickConfig` and `__*_PRESENT` macros, the
   include of `core_<cpu>.h` and `system_<device>.h`, and the `__IM`/`__OM`/
   `__IOM` fallbacks.
3. **Device Specific Cluster Section** — one structure type per cluster
   (`<PERIPHERAL>_<CLUSTER>_Type`).
4. **Device Specific Peripheral Section** — one structure type per peripheral
   (`<PERIPHERAL>_Type`), including reserved gaps and anonymous unions for
   aliased registers.
5. **Peripheral Address Map** — `#define <PERIPHERAL>_BASE 0x........UL`.
6. **Peripheral declaration** — `#define <PERIPHERAL> ((<TYPE>*) <PERIPHERAL>_BASE)`.
7. **Pos/Mask sections** — `#define <P>_<REG>_<FIELD>_Pos` and `..._Msk`
   macros for every cluster and peripheral.
8. **Enumerated Values sections** — `typedef enum { ... } <P>_<REG>_<FIELD>_Enum;`
   for every field with `<enumeratedValues>`.

The copyright/license block at the top is taken from the `<licenseText>`
element of the SVD, or from `--license-file`; when neither is present the ARM
Apache-2.0 text used by SVDConv is emitted.

## Output format notes

The layout is a deliberate reproduction of SVDConv's formatting, including its
quirks, because existing projects diff their generated headers against it:

* Lines are 129 characters wide; trailing `/*!<` comments start at column 48
  (bit macros: 68; `#define ..._BASE` values end at column 48).
* Long comments are wrapped after 59 characters on the first line and after
  54 characters on continuation lines, with continuations indented to the
  comment text column (53).
* Section separators are `/* ====...  title  ...==== */` lines with 16 fill
  characters, or — for register-level titles — two blanks around the title and
  a title-dependent number of fill characters.
* Reserved gaps are filled with `RESERVED`, `RESERVED1`, … members using the
  largest naturally aligned integer type (`uint8_t`, `uint16_t`, `uint32_t`),
  and structures are padded to the alignment of their widest member.
* Registers that share an address are grouped into an anonymous `union`.
* `dim` registers named `NAME[%s]` become array members, `dim` registers named
  `NAME%s` are expanded into individual members, and field/register
  descriptions substitute `%s` with the element index.

## Supported SVD features

* device, peripheral, cluster, register and field `derivedFrom` resolution
* `dim`/`dimIncrement`/`dimIndex` expansion for registers, clusters and
  peripherals
* inherited `size`, `access`, `resetValue` and `resetMask` properties
* `<headerStructName>` for peripherals and clusters
* `<prependToName>`/`<appendToName>` for register members
* `<enumeratedValues>` including `<name>`, `<usage>`, `<headerEnumName>` and
  `<isDefault>`
* `<interrupt>` numbering of every peripheral
* alternate registers (aliases) and alternate groups
* `<licenseText>`, `<vendor>`, `<version>`, `<cpu>` and the
  `<...Present>`/`<vendorSystickConfig>` CPU properties
* the `<headerSystemFilename>` and `<headerDefinitionsPrefix>` elements

## Known differences to SVDConv

The following SVDConv behaviours are intentionally *not* reproduced because
they are bugs of the original tool or produce unusable C code:

* SVDConv emits broken C code (and occasionally runs for hours) for structures
  with unaligned registers, registers wider than 32 bits or nested cluster
  arrays. `svdconv.py` reports these constructs as warnings and skips the
  offending register instead.
* Negative interrupt numbers and negative enumerated values are reported and
  ignored (SVDConv prints a parse error and drops the value as well).
* Word wrapping of very long comments differs in rare cases from SVDConv.
* `--generate=partition` and `--generate=SDF` are not implemented.

## Verifying

The `check/` directory contains three devices together with the headers that
SVDConv V3.3.46 produced for them:

| Device | SVD | Reference header | Size |
| --- | --- | --- | --- |
| LPC2103 | `check/LPC2103.svd` | `check/LPC2103.h` | 1 959 lines |
| LKS32MC03x | `check/LKS32MC03x.svd` | `check/LKS32MC03x.h` | 4 169 lines |
| MSPM0L130X | `check/MSPM0L130X.svd` | `check/MSPM0L130X.h` | 27 470 lines |

The generated files are identical to the references except for the
`@file`/`@date`/`@note` lines, which contain the current date and the paths of
the input and output files. Run the test suite with:

```console
$ python3 -m unittest discover -s tests -v
```

`tests/test_svdconv.py` also contains unit tests for the structure layout
(gap filling, unions, alignment padding) and for the command line interface.

## Repository layout

```
svdconv.py             the converter (single file, standard library only)
tests/test_svdconv.py  unit tests and reference comparisons
check/                 SVD inputs and SVDConv reference headers
LICENSE.md             BSD 3-Clause license
README.md              this document
PROMPTS.md             development notes / prompt history
```

## License

BSD 3-Clause, see [LICENSE.md](LICENSE.md).

The header comment that `svdconv.py` writes into generated files is the one
provided by the SVD (`<licenseText>`), by `--license-file`, or the default ARM
Apache-2.0 text used by SVDConv; it is independent of the license of this
tool.

[svd]: https://www.keil.com/pack/doc/CMSIS/SVD/html/index.html
