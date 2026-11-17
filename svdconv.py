#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""svdconv - CMSIS-SVD to C device header converter.

This program converts a CMSIS System View Description (SVD) file into the
C device header that CMSIS projects expect: peripheral structure typedefs,
register bit ``_Pos`` / ``_Msk`` macros and ``_Enum`` enumerations.

It is a from-scratch re-implementation of the header generator of ARM's
``SVDConv`` utility (V3.3.46) and reproduces its output layout, so that
generated headers are drop-in replacements for the ones produced by::

    SVDConv.exe <device>.svd -o <outdir> --generate=header \\
                --fields=macro --fields=enum

Only the Python standard library is used, so the program runs on any
platform that has Python 3.6 or newer.

See README.md for the full documentation.

SPDX-License-Identifier: BSD-3-Clause
Copyright (c) 2026, HIKARU Infotech Shanghai Ltd.
"""

from __future__ import annotations

import argparse
import copy
import datetime
import os
import re
import sys
import xml.etree.ElementTree as ET

PROGRAM = "svdconv"
VERSION = "1.0.0"

#: Version of the SVDConv output format this program reproduces.
SVDCONV_VERSION = "3.3.46"

# ---------------------------------------------------------------------------
# Output layout constants (see README.md, "Output format")
# ---------------------------------------------------------------------------

LINE_WIDTH = 129          # total width of a generated line
COMMENT_COL = 48          # column of the trailing '/*!<' comment
DEFINE_VALUE_COL = 42     # column of the value of a bit macro
DEFINE_COMMENT_COL = 68   # column of '/*!<' on bit macro lines
WRAP_FIRST = 59           # first comment line: break after reaching this
WRAP_CONT = 54            # continuation lines: break after reaching this
CONT_INDENT = COMMENT_COL + 5
SECTION_FILL = 16         # fill characters of a major divider
BASE_VALUE_END = 48       # '#define <P>_BASE <value>UL' ends here
DECL_CAST_COL = 36        # column of the cast in a peripheral declaration
DECL_BASE_COL = 63        # column of '<P>_BASE)' in a peripheral declaration

#: Largest number of device interrupts (0 .. MAX_INTERRUPTS-1).
MAX_INTERRUPTS = 240


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_SCALE = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}


def parse_int(text):
    """Parse an SVD numeric string (decimal, ``0x``, ``#``, ``0b``, scaled)."""
    if text is None:
        return None
    value = text.strip()
    if not value:
        return None
    scale = 1
    if value[-1].lower() in _SCALE:
        scale = _SCALE[value[-1].lower()]
        value = value[:-1]
    if value.startswith("#"):
        result = int(value[1:], 16)
    elif value[:2].lower() == "0x":
        result = int(value[2:], 16)
    elif value[:2].lower() == "0b":
        result = int(value[2:], 2)
    else:
        result = int(value.lstrip("+"), 10)
    return result * scale


def parse_bool(text, default=False):
    """Parse an XML boolean."""
    if text is None:
        return default
    value = text.strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    return default


def asctime(timestamp):
    """Format a timestamp like SVDConv: ``Wed Nov 18 01:24:30 2026``."""
    stamp = timestamp.strftime("%a %b %d %H:%M:%S %Y")
    return re.sub(r" ([0-9]) ", r"  \1 ", stamp, count=1)


def file_mtime(path):
    """Return the modification time of *path* formatted like SVDConv."""
    try:
        stamp = datetime.datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:                                     # pragma: no cover
        stamp = datetime.datetime.now()
    return asctime(stamp)


def normalize_text(text):
    """Collapse the whitespace of an SVD description."""
    if not text:
        return ""
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Comment and divider rendering
# ---------------------------------------------------------------------------

def wrap_comment_text(text, prefix=""):
    """Split comment *text* into lines exactly like SVDConv does.

    SVDConv appends words until the line has reached a fixed length and
    then breaks; the limit is larger for the first line than for the
    continuation lines.  *prefix* is unbreakable text leading the first
    line (for example the padded interrupt number).
    """
    words = text.split()
    if not words:
        return [prefix] if prefix else [""]
    chunks = []
    current = prefix
    for word in words:
        current = word if not current else current + " " + word
        limit = WRAP_FIRST if not chunks else WRAP_CONT
        if len(current) >= limit:
            chunks.append(current)
            current = ""
    if current or not chunks:
        chunks.append(current)
    return chunks


def comment_lines(code, text, col=COMMENT_COL, sep=" ", lead=""):
    """Return the source lines for *code* with a trailing ``/*!<`` comment.

    ``code`` is the code part of the first line.  The comment starts at
    column *col*; when the code is longer, *sep* is used as separator.
    Continuation lines are indented to the comment text column.
    """
    if not text:
        return [code]
    if len(code) < col:
        prefix = code + " " * (col - len(code))
    else:
        prefix = code + sep
    chunks = wrap_comment_text(text, lead)
    lines = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        if index == 0:
            line = prefix + "/*!< " + chunk
        else:
            line = " " * CONT_INDENT + chunk
        if last:
            if len(line) <= LINE_WIDTH - 3:
                line += " " * (LINE_WIDTH - 3 - len(line)) + " */"
            else:
                line += " */"
        lines.append(line)
    return lines


def divider_major(title):
    """``/* ====...  title  ...==== */``, 16 fill characters on both sides."""
    inner = LINE_WIDTH - 6 - 2 * SECTION_FILL
    gap = inner - len(title)
    if gap < 0:
        return divider_minor(title)
    left = gap // 2
    right = gap - left
    return "/* %s%s%s%s%s */" % (
        "=" * SECTION_FILL, " " * left, title, " " * right,
        "=" * SECTION_FILL)


def divider_minor(title):
    """``/* ====...  title  ...==== */`` with two blanks around the title."""
    inner = LINE_WIDTH - 6 - 4
    gap = max(inner - len(title), 0)
    left = gap // 2
    right = gap - left
    return "/* %s  %s  %s */" % ("=" * left, title, "=" * right)


def full_divider():
    """A divider line without any title."""
    return "/* " + "=" * (LINE_WIDTH - 6) + " */"


# ---------------------------------------------------------------------------
# SVD object model
# ---------------------------------------------------------------------------

class SvdError(Exception):
    """Raised when an SVD file cannot be converted."""


class Element(object):
    """Base class wrapping an XML node of the SVD."""

    def __init__(self, node):
        self.node = node

    def text(self, tag):
        if self.node is None:
            return None
        child = self.node.find(tag)
        if child is None or child.text is None:
            return None
        value = child.text.strip()
        return value or None

    def integer(self, tag):
        return parse_int(self.text(tag))

    def boolean(self, tag, default=False):
        return parse_bool(self.text(tag), default)

    @property
    def name(self):
        return self.text("name") or ""

    @property
    def description(self):
        return normalize_text(self.text("description"))


class DimElement(Element):
    """Element supporting the ``dim`` expansion group."""

    @property
    def dim(self):
        return self.integer("dim")

    @property
    def dim_increment(self):
        return self.integer("dimIncrement")

    def indices(self):
        """Return the indices of a ``dim`` array as strings."""
        count = self.dim
        if not count:
            return []
        explicit = self.text("dimIndex")
        if explicit:
            parts = [part.strip() for part in explicit.split(",")]
            if len(parts) == 1 and "-" in parts[0]:
                first, last = parts[0].split("-", 1)
                try:
                    start, stop = int(first, 0), int(last, 0)
                except ValueError:
                    pass
                else:
                    return [str(i) for i in range(start, stop + 1)]
                if len(first) == 1 and len(last) == 1:
                    return [chr(c) for c in range(ord(first), ord(last) + 1)]
                return [parts[0]]
            if len(parts) >= count:
                return parts[:count]
            return parts + [str(i) for i in range(len(parts), count)]
        return [str(i) for i in range(count)]


class EnumeratedValue(Element):
    @property
    def value(self):
        return self.integer("value")

    @property
    def is_default(self):
        return self.boolean("isDefault")


class EnumeratedValues(Element):
    @property
    def usage(self):
        return self.text("usage")

    @property
    def header_enum_name(self):
        return self.text("headerEnumName")

    @property
    def values(self):
        return [EnumeratedValue(n)
                for n in self.node.findall("enumeratedValue")]


class Field(DimElement):
    @property
    def lsb(self):
        value = self.integer("lsb")
        if value is not None:
            return value
        offset = self.integer("bitOffset")
        if offset is not None:
            return offset
        bit_range = self.text("bitRange")
        if bit_range:
            return parse_int(bit_range.strip("[]").split(":")[1])
        return 0

    @property
    def msb(self):
        value = self.integer("msb")
        if value is not None:
            return value
        offset = self.integer("bitOffset")
        width = self.integer("bitWidth")
        if offset is not None and width is not None:
            return offset + width - 1
        bit_range = self.text("bitRange")
        if bit_range:
            return parse_int(bit_range.strip("[]").split(":")[0])
        return None

    @property
    def width(self):
        msb = self.msb
        if msb is None:
            return None
        return msb - self.lsb + 1

    @property
    def access(self):
        return self.text("access")

    @property
    def derived_from(self):
        return self.node.get("derivedFrom")

    @property
    def enumerated_values(self):
        return [EnumeratedValues(n)
                for n in self.node.findall("enumeratedValues")]


class Register(DimElement):
    @property
    def address_offset(self):
        return self.integer("addressOffset")

    @property
    def size(self):
        return self.integer("size")

    @property
    def access(self):
        return self.text("access")

    @property
    def derived_from(self):
        return self.node.get("derivedFrom")

    @property
    def fields(self):
        container = self.node.find("fields")
        if container is None:
            return []
        return [Field(n) for n in container.findall("field")]


class Cluster(DimElement):
    @property
    def address_offset(self):
        return self.integer("addressOffset")

    @property
    def header_struct_name(self):
        return self.text("headerStructName")

    @property
    def derived_from(self):
        return self.node.get("derivedFrom")

    @property
    def size(self):
        return self.integer("size")

    @property
    def access(self):
        return self.text("access")

    def children(self):
        items = []
        for node in self.node:
            if node.tag == "register":
                items.append(Register(node))
            elif node.tag == "cluster":
                items.append(Cluster(node))
        return items


class Interrupt(Element):
    @property
    def value(self):
        return self.integer("value")


class Peripheral(DimElement):
    @property
    def base_address(self):
        return self.integer("baseAddress")

    @property
    def header_struct_name(self):
        return self.text("headerStructName")

    @property
    def prepend_to_name(self):
        return self.text("prependToName") or ""

    @property
    def append_to_name(self):
        return self.text("appendToName") or ""

    @property
    def derived_from(self):
        return self.node.get("derivedFrom")

    @property
    def size(self):
        return self.integer("size")

    @property
    def access(self):
        return self.text("access")

    @property
    def interrupts(self):
        return [Interrupt(n) for n in self.node.findall("interrupt")]

    def children(self):
        container = self.node.find("registers")
        if container is None:
            return []
        items = []
        for node in container:
            if node.tag == "register":
                items.append(Register(node))
            elif node.tag == "cluster":
                items.append(Cluster(node))
        return items


class Cpu(Element):
    @property
    def revision(self):
        return self.text("revision") or "r0p0"

    @property
    def nvic_prio_bits(self):
        value = self.integer("nvicPrioBits")
        return 0 if value is None else value

    @property
    def vendor_systick_config(self):
        return self.boolean("vendorSystickConfig")

    def flag(self, tag):
        return self.boolean(tag)


class Device(Element):
    @property
    def version(self):
        return self.text("version") or ""

    @property
    def vendor(self):
        return self.text("vendor") or ""

    @property
    def license_text(self):
        return self.node.findtext("licenseText")

    @property
    def header_system_filename(self):
        return self.text("headerSystemFilename")

    @property
    def cpu(self):
        node = self.node.find("cpu")
        return Cpu(node) if node is not None else None

    @property
    def size(self):
        return self.integer("size")

    @property
    def access(self):
        return self.text("access")

    @property
    def peripherals(self):
        container = self.node.find("peripherals")
        if container is None:
            return []
        return [Peripheral(n) for n in container.findall("peripheral")]


def parse_svd(path):
    """Parse *path* and return the ``<device>`` element."""
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError) as exc:
        raise SvdError("cannot read '%s': %s" % (path, exc))
    root = tree.getroot()
    if root.tag != "device":
        raise SvdError("'%s' does not contain a CMSIS-SVD <device> element"
                       % path)
    return Device(root)


# ---------------------------------------------------------------------------
# Cortex-M interrupt vectors and core descriptions
# ---------------------------------------------------------------------------

IRQS_CORTEX_M0 = [
    ("Reset_IRQn", -15, "Reset Vector, invoked on Power up and warm reset"),
    ("NonMaskableInt_IRQn", -14,
     "Non maskable Interrupt, cannot be stopped or preempted"),
    ("HardFault_IRQn", -13, "Hard Fault, all classes of Fault"),
    ("SVCall_IRQn", -5, "System Service Call via SVC instruction"),
    ("PendSV_IRQn", -2, "Pendable request for system service"),
    ("SysTick_IRQn", -1, "System Tick Timer"),
]

IRQS_CORTEX_M = [
    ("Reset_IRQn", -15, "Reset Vector, invoked on Power up and warm reset"),
    ("NonMaskableInt_IRQn", -14,
     "Non maskable Interrupt, cannot be stopped or preempted"),
    ("HardFault_IRQn", -13, "Hard Fault, all classes of Fault"),
    ("MemoryManagement_IRQn", -12,
     "Memory Management, MPU mismatch, including Access Violation and No Match"),
    ("BusFault_IRQn", -11,
     "Bus Fault, Pre-Fetch-, Memory Access Fault, other address/memory related Fault"),
    ("UsageFault_IRQn", -10,
     "Usage Fault, i.e. Undef Instruction, Illegal State Transition"),
    ("SVCall_IRQn", -5, "System Service Call via SVC instruction"),
    ("DebugMonitor_IRQn", -4, "Debug Monitor"),
    ("PendSV_IRQn", -2, "Pendable request for system service"),
    ("SysTick_IRQn", -1, "System Tick Timer"),
]

IRQS_ARMV8_M = [
    ("NonMaskableInt_IRQn", -14,
     "Non maskable Interrupt, cannot be stopped or preempted"),
    ("HardFault_IRQn", -13, "Hard Fault, all classes of Fault"),
    ("MemoryManagement_IRQn", -12,
     "Memory Management, MPU mismatch, including Access Violation and No Match"),
    ("BusFault_IRQn", -11,
     "Bus Fault, Pre-Fetch-, Memory Access Fault, other address/memory related Fault"),
    ("UsageFault_IRQn", -10,
     "Usage Fault, i.e. Undef Instruction, Illegal State Transition"),
    ("SecureFault_IRQn", -9,
     "Secure Fault, e.g. access violation or invalid transition"),
    ("SVCall_IRQn", -5, "System Service Call via SVC instruction"),
    ("DebugMonitor_IRQn", -4, "Debug Monitor"),
    ("PendSV_IRQn", -2, "Pendable request for system service"),
    ("SysTick_IRQn", -1, "System Tick Timer"),
]

#: Description of the cores SVDConv knows about.  ``irqs`` lists the
#: architecturally defined interrupt numbers, the remaining keys tell which
#: ``__xxx_PRESENT`` macros are emitted.
CORES = {
    "other": dict(display="other", rev="OTHER", rev_text="other",
                  core="other", irqs=[], vtor=True, mpu=False, fpu=True,
                  cache=True, tcm=True, dsp=False, fpu_dp=False),
    "CM0": dict(display="ARM Cortex-M0", rev="CM0", core="cm0",
                irqs=IRQS_CORTEX_M0, vtor=False, mpu=True, fpu=True,
                cache=False, tcm=False, dsp=False, fpu_dp=False),
    "CM0PLUS": dict(display="ARM Cortex-M0+", rev="CM0PLUS", core="cm0plus",
                    irqs=IRQS_CORTEX_M0, vtor=True, mpu=True, fpu=True,
                    cache=False, tcm=False, dsp=False, fpu_dp=False),
    "CM1": dict(display="ARM Cortex-M1", rev="CM1", core="cm1",
                irqs=IRQS_CORTEX_M, vtor=True, mpu=True, fpu=True,
                cache=False, tcm=False, dsp=False, fpu_dp=False),
    "CM3": dict(display="ARM Cortex-M3", rev="CM3", core="cm3",
                irqs=IRQS_CORTEX_M, vtor=False, mpu=True, fpu=True,
                cache=False, tcm=False, dsp=False, fpu_dp=False),
    "CM4": dict(display="ARM Cortex-M4", rev="CM4", core="cm4",
                irqs=IRQS_CORTEX_M, vtor=False, mpu=True, fpu=True,
                cache=True, tcm=True, dsp=True, fpu_dp=True),
    "CM7": dict(display="ARM Cortex-M7", rev="CM7", core="cm7",
                irqs=IRQS_CORTEX_M, vtor=False, mpu=True, fpu=True,
                cache=True, tcm=True, dsp=True, fpu_dp=True),
    "CM23": dict(display="ARM Cortex-M23", rev="CM23", core="cm23",
                 irqs=IRQS_ARMV8_M, vtor=True, mpu=True, fpu=False,
                 cache=False, tcm=False, dsp=False, fpu_dp=False),
    "CM33": dict(display="ARM Cortex-M33", rev="CM33", core="cm33",
                 irqs=IRQS_ARMV8_M, vtor=True, mpu=True, fpu=True,
                 cache=True, tcm=True, dsp=True, fpu_dp=True),
    "CM35P": dict(display="ARM Cortex-M35P", rev="CM35P", core="cm35p",
                  irqs=IRQS_ARMV8_M, vtor=True, mpu=True, fpu=True,
                  cache=True, tcm=True, dsp=True, fpu_dp=True),
    "CM52": dict(display="ARM Cortex-M52", rev="CM52", core="cm52",
                 irqs=IRQS_ARMV8_M, vtor=True, mpu=True, fpu=True,
                 cache=True, tcm=True, dsp=True, fpu_dp=True),
    "CM55": dict(display="ARM Cortex-M55", rev="CM55", core="cm55",
                 irqs=IRQS_ARMV8_M, vtor=True, mpu=True, fpu=True,
                 cache=True, tcm=True, dsp=True, fpu_dp=True),
    "CM85": dict(display="ARM Cortex-M85", rev="CM85", core="cm85",
                 irqs=IRQS_ARMV8_M, vtor=True, mpu=True, fpu=True,
                 cache=True, tcm=True, dsp=True, fpu_dp=True),
    "SC000": dict(display="ARM SecureCore SC000", rev="SC000", core="sc000",
                  irqs=IRQS_CORTEX_M0, vtor=False, mpu=True, fpu=False,
                  cache=False, tcm=False, dsp=False, fpu_dp=False),
    "SC300": dict(display="ARM SecureCore SC300", rev="SC300", core="sc300",
                  irqs=IRQS_CORTEX_M, vtor=False, mpu=True, fpu=True,
                  cache=False, tcm=False, dsp=False, fpu_dp=False),
    "ARMV8MML": dict(display="ARMv8-M Machine Learning", rev="ARMV8MML",
                     core="armv8mml", irqs=IRQS_ARMV8_M, vtor=True,
                     mpu=True, fpu=True, cache=True, tcm=True, dsp=True,
                     fpu_dp=True),
    "ARMV8MBL": dict(display="ARMv8-M Baseline", rev="ARMV8MBL",
                     core="armv8mbl", irqs=IRQS_CORTEX_M0, vtor=True,
                     mpu=True, fpu=False, cache=False, tcm=False, dsp=False,
                     fpu_dp=False),
    "CA5": dict(display="ARM Cortex-A5", rev="CA5", core="ca5", irqs=[],
                vtor=False, mpu=False, fpu=False, cache=False, tcm=False,
                dsp=False, fpu_dp=False),
    "CA7": dict(display="ARM Cortex-A7", rev="CA7", core="ca7", irqs=[],
                vtor=False, mpu=False, fpu=False, cache=False, tcm=False,
                dsp=False, fpu_dp=False),
    "CA9": dict(display="ARM Cortex-A9", rev="CA9", core="ca9", irqs=[],
                vtor=False, mpu=False, fpu=False, cache=False, tcm=False,
                dsp=False, fpu_dp=False),
    "CA15": dict(display="ARM Cortex-A15", rev="CA15", core="ca15", irqs=[],
                 vtor=False, mpu=False, fpu=False, cache=False, tcm=False,
                 dsp=False, fpu_dp=False),
}


# ---------------------------------------------------------------------------
# Intermediate model
# ---------------------------------------------------------------------------

class FieldInfo(object):
    """A bit field of a register."""

    def __init__(self, name, description, lsb, width, access, enums):
        self.name = name
        self.description = description
        self.lsb = lsb
        self.width = width
        self.access = access
        self.enums = enums

    @property
    def msb(self):
        return self.lsb + self.width - 1

    @property
    def mask(self):
        return ((1 << self.width) - 1) << self.lsb


class EnumInfo(object):
    """One ``enumeratedValues`` block of a field."""

    def __init__(self, base_name, type_name, values, usage):
        self.base_name = base_name
        self.type_name = type_name
        self.values = values
        self.usage = usage

    @property
    def member_prefix(self):
        """Prefix of the enumerator names (without the usage suffix)."""
        return self.base_name


class RegisterInfo(object):
    """A register instance (after ``dim`` expansion)."""

    is_cluster = False

    def __init__(self, name, description, offset, size, access, fields,
                 array=0, indices=None):
        self.name = name
        self.description = description
        self.offset = offset
        self.size = size
        self.access = access
        self.fields = fields
        self.array = array
        self.indices = indices or []

    @property
    def byte_size(self):
        return (self.size + 7) // 8

    @property
    def total_size(self):
        return self.byte_size * (self.array or 1)


class BlockInfo(object):
    """A structure type: a peripheral or a cluster."""

    is_cluster = False

    @property
    def macro_title(self):
        """Title used in the Pos/Mask section headers."""
        return self.title if self.is_cluster else self.member_prefix

    @property
    def section_title(self):
        """Title used in the Enum section headers."""
        return self.title

    def __init__(self, type_name, member_prefix, title, description,
                 offset, registers=(), derived=False):
        self.type_name = type_name
        self.member_prefix = member_prefix
        self.title = title
        self.description = description
        self.offset = offset
        self.registers = list(registers)
        self.derived = derived
        self.array = 0
        self.indices = []
        self.size = 0


class PeripheralInfo(BlockInfo):
    def __init__(self, type_name, member_prefix, title, description, base,
                 registers=(), derived=False):
        BlockInfo.__init__(self, type_name, member_prefix, title,
                           description, base, registers, derived)
        self.base = base
        self.interrupts = []


# ---------------------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------------------

ACCESS_TYPES = {
    "read-only": "__IM",
    "write-only": "__OM",
    "writeOnce": "__OM",
    "read-write": "__IOM",
    "read-writeOnce": "__IOM",
}


def access_macro(access):
    return ACCESS_TYPES.get(access, "__IOM")


def c_type(size):
    """Return the C integer type used for a register of *size* bits."""
    if size <= 8:
        return "uint8_t"
    if size <= 16:
        return "uint16_t"
    return "uint32_t"


def alignment_of(registers):
    """Return the natural alignment (in bytes) of a register group."""
    size = max(r.byte_size for r in registers) if registers else 1
    if size >= 4:
        return 4
    return size if size > 0 else 1


def fill_gap(start, target, align):
    """Return the list of (bits, count) reserved fields for a gap."""
    fills = []
    current = start
    pad = (-current) % align
    if pad:
        if pad % 2 == 1:
            fills.append((8, 1))
            current += 1
            pad -= 1
        while pad > 0:
            fills.append((16, 1))
            current += 2
            pad -= 2
    remainder = target - current
    if remainder > 0:
        unit = 1
        for candidate in (4, 2, 1):
            if current % candidate == 0 and remainder >= candidate:
                unit = candidate
                break
        if remainder % unit:
            unit = 1
        fills.append((unit * 8, remainder // unit))
    return fills


def compute_layout(registers):
    """Lay out *registers* as struct members.

    Returns ``(entries, size)`` where each entry is one of
    ``("register", RegisterInfo)``, ``("union", [RegisterInfo, ...])`` or
    ``("reserved", bits, count)``.
    """
    entries = []
    groups = []
    for reg in registers:
        if getattr(reg, "unsupported", False):
            groups.append(("error",
                           "  #error \"Register '%s' is wider than 32 bits\""
                           % reg.name))
        elif groups and groups[-1][0] == "group" and \
                groups[-1][1][0].offset == reg.offset:
            groups[-1][1].append(reg)
        else:
            groups.append(("group", [reg]))
    current = 0
    for group in groups:
        if group[0] == "error":
            entries.append(group)
            continue
        group = group[1]
        offset = group[0].offset
        size = max(r.total_size for r in group)
        if offset > current:
            for bits, count in fill_gap(current, offset,
                                        alignment_of(group)):
                entries.append(("reserved", bits, count))
            current = offset
        if offset < current:
            continue
        if len(group) == 1:
            entries.append(("register", group[0]))
        else:
            entries.append(("union", group))
        current = offset + size
    if entries:
        align = 1
        for reg in registers:
            align = max(align, min(reg.byte_size, 4))
        pad = (-current) % align
        while pad > 0:
            if pad % 2 == 1:
                entries.append(("reserved", 8, 1))
                current += 1
                pad -= 1
            else:
                entries.append(("reserved", 16, 1))
                current += 2
                pad -= 2
    return entries, current


class Model(object):
    """Resolves the SVD tree into the intermediate representation.

    The model performs the following steps:

    * resolve ``derivedFrom`` references (peripherals, registers, clusters
      and fields),
    * expand ``dim`` arrays,
    * inherit ``size``/``access`` from the enclosing element,
    * collect the structure types (peripherals and clusters) in the order
      in which they have to be emitted.
    """

    def __init__(self, device, log=None):
        self.device = device
        self.log = log or (lambda *args: None)
        self.warnings = []
        self.errors = []
        self.peripheral_nodes = {}
        for per in device.peripherals:
            self.peripheral_nodes.setdefault(per.name, per)
        self.peripherals = []       # PeripheralInfo in SVD order
        self.blocks = []            # BlockInfo in emission order
        self.block_by_type = {}
        self.cluster_blocks = []
        self.peripheral_blocks = []
        self._build()

    # -- diagnostics ------------------------------------------------------

    def warn(self, message):
        self.warnings.append(message)
        self.log("WARNING: " + message)

    def error(self, message):
        self.errors.append(message)
        self.log("ERROR: " + message)

    # -- resolution helpers ------------------------------------------------

    @staticmethod
    def _set_text(parent, tag, text):
        node = parent.find(tag)
        if node is None:
            node = ET.SubElement(parent, tag)
        node.text = text

    def _resolve_peripheral(self, per, seen=None):
        """Return a dict with the effective attributes of *per*."""
        if seen is None:
            seen = set()
        if per.name in seen:
            raise SvdError("circular derivedFrom chain at peripheral '%s'"
                           % per.name)
        seen.add(per.name)
        data = {
            "name": per.name,
            "node": per,
            "description": per.description,
            "base": per.base_address,
            "size": per.size,
            "access": per.access,
            "header_struct_name": per.header_struct_name,
            "prepend": per.prepend_to_name,
            "append": per.append_to_name,
            "children": per.children(),
            "interrupts": per.interrupts,
            "derived": per.derived_from,
        }
        if per.derived_from:
            base_node = self.peripheral_nodes.get(per.derived_from)
            if base_node is None:
                self.error("peripheral '%s' derives from unknown peripheral "
                           "'%s'" % (per.name, per.derived_from))
            else:
                base = self._resolve_peripheral(base_node, seen)
                for key in ("description", "size", "access",
                            "header_struct_name", "prepend", "append"):
                    if not data[key]:
                        data[key] = base[key]
                if not data["children"]:
                    data["children"] = base["children"]
        if data["size"] is None:
            data["size"] = self.device.size or 32
        if data["access"] is None:
            data["access"] = self.device.access or "read-write"
        if not data["header_struct_name"]:
            data["header_struct_name"] = data["name"]
        return data

    @staticmethod
    def _find_by_name(children, name, kind):
        for child in children:
            if isinstance(child, kind) and child.name == name:
                return child
        return None

    def _merge_derived_register(self, reg, siblings):
        """Return *reg* with the attributes of its ``derivedFrom`` parent."""
        node = reg
        seen = set()
        while node.derived_from and node.derived_from not in seen:
            seen.add(node.derived_from)
            base = self._find_by_name(siblings, node.derived_from, Register)
            if base is None:
                self.warn("register '%s' derives from unknown register '%s'"
                          % (node.name, node.derived_from))
                break
            merged = copy.deepcopy(node.node)
            if not node.description and base.description:
                self._set_text(merged, "description", base.description)
            if node.size is None and base.size is not None:
                self._set_text(merged, "size", str(base.size))
            if not node.access and base.access:
                self._set_text(merged, "access", base.access)
            if not node.fields:
                container = merged.find("fields")
                if container is None:
                    container = ET.SubElement(merged, "fields")
                for field in base.fields:
                    container.append(copy.deepcopy(field.node))
            node = Register(merged)
        return node

    def _register_infos(self, reg, defaults, block_prefix, siblings):
        """Expand *reg* into one or more :class:`RegisterInfo` objects."""
        node = self._merge_derived_register(reg, siblings)
        size = node.size if node.size is not None else defaults["size"]
        access = node.access or defaults["access"]
        name = node.name
        prepend = block_prefix.get("prepend") or ""
        append = block_prefix.get("append") or ""
        if prepend and name.startswith(prepend):
            name = name[len(prepend):]
        if append and name.endswith(append):
            name = name[:-len(append)]
        description = node.description
        unsupported = size > 32
        if not description:
            if node.dim:
                indices = node.indices()
                description = "[%s..%s]" % (indices[0], indices[-1])
            else:
                description = name
        fields = self._field_infos(
            node.fields, re.sub(r"(\[%s\]|%s)", "", name), block_prefix)
        offset = node.address_offset or 0
        count = node.dim
        if count and not unsupported:
            indices = node.indices()
            if "[%s]" in name:
                array_name = re.sub(r"\[%s\]", "", name)
                if "%s" in description:
                    description = description.replace(
                        "%s", "[%s..%s]" % (indices[0], indices[-1]))
                info = RegisterInfo(array_name, description, offset, size,
                                    access, fields, count, indices)
                info.unsupported = unsupported
                return [info]
            if "%s" in name:
                increment = node.dim_increment or (size // 8)
                infos = []
                for position, index in enumerate(indices):
                    expanded = name.replace("%s", index)
                    text = description
                    if "%s" in text:
                        text = text.replace("%s", index)
                    info = RegisterInfo(
                        expanded, text, offset + position * increment, size,
                        access, fields)
                    info.unsupported = unsupported
                    infos.append(info)
                return infos
            self.warn("register array '%s' has no %%s in its name" % name)
        info = RegisterInfo(name, description, offset, size, access, fields)
        info.unsupported = unsupported
        return [info]

    def _field_infos(self, fields, register_name, block_prefix):
        """Resolve the fields of a register."""
        result = []
        by_name = {}
        for field in fields:
            by_name.setdefault(field.name, field)
        for field in fields:
            source = field
            seen = set()
            while source.derived_from and source.derived_from not in seen:
                seen.add(source.derived_from)
                base = by_name.get(source.derived_from)
                if base is None:
                    self.warn("field '%s' of register '%s' derives from "
                              "unknown field '%s'"
                              % (field.name, register_name,
                                 source.derived_from))
                    break
                source = base
            width = field.width
            if not width or width <= 0:
                self.warn("field '%s' of register '%s' has no bit width"
                          % (field.name, register_name))
                continue
            description = field.description or source.description
            enums = []
            for enum in (field.enumerated_values or source.enumerated_values):
                base = "%s_%s_%s" % (
                    block_prefix.get("enum") or block_prefix["macro"],
                    register_name, field.name.replace("%s", ""))
                if enum.name:
                    base = base + "_" + enum.name
                type_base = base
                usage = enum.usage
                if enum.header_enum_name:
                    type_base = enum.header_enum_name
                elif usage == "read":
                    type_base = base + "_R"
                elif usage == "write":
                    type_base = base + "_W"
                elif usage == "read-write":
                    type_base = base + "_RW"
                values = self._enum_values(enum, field, width)
                if values:
                    enums.append(EnumInfo(base, type_base, values, usage))
            result.append(FieldInfo(
                field.name.replace("%s", ""), description, field.lsb,
                width, field.access or source.access, enums))
        return result

    def _enum_values(self, enum, field, width):
        """Return the usable ``(name, value, description)`` triples."""
        limit = (1 << width) - 1
        values = []
        for value in enum.values:
            if value.is_default:
                continue
            number = value.value
            if number is None:
                continue
            if number < 0 or number > limit:
                self.warn("value '%s:%d' of field '%s' does not fit into "
                          "the field and is ignored"
                          % (value.name, number, field.name))
                continue
            values.append((value.name, number, value.description))
        return values

    def _block_registers(self, children, defaults, block_prefix, path):
        """Resolve the registers and clusters of a peripheral or cluster."""
        registers = []
        for child in children:
            if isinstance(child, Register):
                registers.extend(self._register_infos(
                    child, defaults, block_prefix, children))
            else:
                cluster = self._cluster_block(child, defaults, block_prefix,
                                              path)
                if cluster is not None:
                    registers.append(cluster)
        registers.sort(key=lambda reg: reg.offset)
        return registers

    def _cluster_block(self, cluster, defaults, block_prefix, path):
        """Resolve a cluster and register its structure type."""
        base_name = re.sub(r"(\[%s\]|%s)", "", cluster.name)
        if cluster.derived_from:
            return self._derived_cluster(cluster, block_prefix, path,
                                         base_name)
        if cluster.dim:
            self.warn("cluster array '%s' is only expanded as an array"
                      % cluster.name)
        prefix = block_prefix.get("cluster_prefix") or block_prefix["macro"]
        if cluster.header_struct_name:
            type_name = cluster.header_struct_name
            macro_prefix = cluster.header_struct_name
        else:
            type_name = "_".join([prefix] + path + [base_name])
            macro_prefix = "_".join([block_prefix["macro"]] + path +
                                    [base_name])
        inner_defaults = dict(defaults)
        if cluster.size is not None:
            inner_defaults["size"] = cluster.size
        if cluster.access is not None:
            inner_defaults["access"] = cluster.access
        inner_prefix = dict(block_prefix)
        inner_prefix.update(macro=macro_prefix, enum=type_name,
                            children=cluster.children())
        registers = self._block_registers(
            cluster.children(), inner_defaults, inner_prefix,
            path + [base_name])
        description = cluster.description
        if not description and cluster.dim:
            indices = cluster.indices()
            description = "[%s..%s]" % (indices[0], indices[-1])
        block = BlockInfo(type_name, macro_prefix, base_name,
                          description or base_name,
                          cluster.address_offset or 0, registers)
        block.is_cluster = True
        self._register_block(block)
        entries, size = compute_layout(block.registers)
        block.size = size
        info = RegisterInfo(base_name, block.description,
                            cluster.address_offset or 0, size * 8,
                            cluster.access or inner_defaults["access"], [],
                            cluster.dim or 0, cluster.indices())
        info.cluster_block = block
        info.is_cluster = True
        return info

    def _derived_cluster(self, cluster, block_prefix, path, base_name):
        """A cluster that only reuses the structure of another cluster."""
        base = self._find_by_name(block_prefix.get("children", []),
                                  cluster.derived_from, Cluster)
        if base is None:
            self.warn("cluster '%s' derives from unknown cluster '%s'"
                      % (cluster.name, cluster.derived_from))
            return None
        base_name = re.sub(r"(\[%s\]|%s)", "", base.name)
        prefix = block_prefix.get("cluster_prefix") or block_prefix["macro"]
        type_name = base.header_struct_name or "_".join(
            [prefix] + path + [base_name])
        base_block = self.block_by_type.get(type_name)
        size = base_block.size if base_block is not None else 0
        info = RegisterInfo(
            re.sub(r"(\[%s\]|%s)", "", cluster.name),
            cluster.description or cluster.name,
            cluster.address_offset or 0, size * 8, cluster.access, [],
            cluster.dim or 0, cluster.indices())
        info.cluster_type_name = type_name
        info.is_cluster = True
        return info

    def _register_block(self, block):
        if block.type_name in self.block_by_type:
            return
        self.block_by_type[block.type_name] = block
        self.blocks.append(block)

    # -- model building ---------------------------------------------------

    def _build(self):
        for per in self.device.peripherals:
            data = self._resolve_peripheral(per)
            prefix = {
                "macro": data["header_struct_name"].upper(),
                "enum": data["name"],
                "cluster_prefix": data["name"],
                "prepend": data["prepend"],
                "append": data["append"],
                "children": data["children"],
            }
            registers = []
            if not data["derived"]:
                registers = self._block_registers(
                    data["children"],
                    {"size": data["size"], "access": data["access"]},
                    prefix, [])
            info = PeripheralInfo(
                data["header_struct_name"], data["header_struct_name"].upper(),
                data["name"], data["description"] or data["name"],
                data["base"] or 0, registers, bool(data["derived"]))
            info.svd_name = data["name"]
            info.base_address = data["base"] or 0
            info.interrupts = data["interrupts"]
            self.peripherals.append(info)
            if not data["derived"]:
                self._register_block(info)
        self.cluster_blocks = [b for b in self.blocks if b.is_cluster]
        self.peripheral_blocks = [b for b in self.blocks if not b.is_cluster]


# ---------------------------------------------------------------------------
# Header file generation
# ---------------------------------------------------------------------------

RECOMMENDED_LICENSE = """\
Copyright (c) 2009-2019 ARM Limited. All rights reserved.

SPDX-License-Identifier: Apache-2.0

Licensed under the Apache License, Version 2.0 (the License); you may
not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an AS IS BASIS, WITHOUT
WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License."""


class Generator(object):
    """Renders the device header."""

    def __init__(self, model, svd_path, out_name, license_text=None,
                 log=None, fields=("macro", "enum")):
        self.model = model
        self.fields = set(fields)
        self.device = model.device
        self.svd_path = svd_path
        self.out_name = out_name
        self.license_text = license_text or self.device.license_text \
            or RECOMMENDED_LICENSE
        self.log = log or (lambda *args: None)
        self.lines = []
        self.cpu_name = (self.device.cpu.name if self.device.cpu else "other")
        self.core = CORES.get(self.cpu_name.upper(), CORES["other"])
        self.has_unions = self._detect_unions()

    # -- helpers ----------------------------------------------------------

    def emit(self, line=""):
        self.lines.append(line)

    def emit_all(self, lines):
        """Append one or more pre-rendered lines."""
        if isinstance(lines, str):
            self.lines.append(lines)
        else:
            self.lines.extend(lines)

    def _detect_unions(self):
        for block in self.model.blocks:
            offsets = [r.offset for r in block.registers]
            if len(offsets) != len(set(offsets)):
                return True
        return False

    # -- top level --------------------------------------------------------

    def render(self):
        self.render_header()
        self.render_open_groups()
        self.render_guard()
        self.render_interrupts()
        self.render_core_config()
        self.render_anonymous_unions_begin()
        self.render_cluster_section()
        self.render_peripheral_section()
        self.render_address_map()
        self.render_declarations()
        self.render_anonymous_unions_end()
        if "macro" in self.fields:
            self.render_pos_mask_clusters()
            self.render_pos_mask_peripherals()
        if "enum" in self.fields:
            self.render_enum_clusters()
            self.render_enum_peripherals()
        self.render_footer()
        return "\n".join(self.lines) + "\n"

    # -- header ------------------------------------------------------------

    def license_lines(self):
        """Return the lines of the license comment block.

        SVD license texts use a literal ``\n`` as line separator and are
        usually indented; both are normalised here.
        """
        raw = self.license_text
        if "\\n" in raw:
            pieces = raw.split("\\n")
        else:
            pieces = raw.splitlines()
        lines = [" ".join(piece.split()) for piece in pieces]
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        return lines

    def render_header(self):
        lic = self.license_lines()
        self.emit("/*")
        for line in lic:
            if line:
                self.emit(" * " + line)
            else:
                self.emit(" * ")
        self.emit(" *")
        self.emit(" * @file     %s" % self.out_name)
        self.emit(" * @brief    CMSIS HeaderFile")
        self.emit(" * @version  %s" % self.device.version)
        self.emit(" * @date     %s" % asctime(datetime.datetime.now()))
        self.emit(" * @note     Generated by SVDConv V%s" % SVDCONV_VERSION)
        self.emit(" *           from File '%s'," % self.svd_path)
        self.emit(" *           last modified on %s" % file_mtime(self.svd_path))
        self.emit(" */")
        self.emit()
        self.emit()

    def render_open_groups(self):
        self.emit("/** @addtogroup %s" % self.device.vendor)
        self.emit("  * @{")
        self.emit("  */")
        self.emit()
        self.emit()
        self.emit("/** @addtogroup %s" % self.device.name)
        self.emit("  * @{")
        self.emit("  */")
        self.emit()
        self.emit()

    def guard_name(self):
        return self.device.name.upper() + "_H"

    def render_guard(self):
        self.emit("#ifndef %s" % self.guard_name())
        self.emit("#define %s" % self.guard_name())
        self.emit()
        self.emit("#ifdef __cplusplus")
        self.emit('extern "C" {')
        self.emit("#endif")
        self.emit()
        self.emit()
        self.emit("/** @addtogroup Configuration_of_CMSIS")
        self.emit("  * @{")
        self.emit("  */")
        self.emit()
        self.emit()
        self.emit()

    # -- configuration -----------------------------------------------------

    def interrupt_entries(self):
        """Return the (name, value, description) tuples of the IRQ enum."""
        entries = []
        seen = set()
        for name, value, description in self.core["irqs"]:
            entries.append((name, value, description))
            seen.add(name)
        device_entries = []
        used_values = set()
        for per in self.model.peripherals:
            for irq in per.interrupts:
                name = irq.name
                value = irq.value
                if value is None:
                    continue
                if value < 0:
                    continue
                if value >= MAX_INTERRUPTS:
                    self.log("WARNING: interrupt '%s' (%d) exceeds the "
                             "maximum interrupt number" % (name, value))
                    continue
                full = name + "_IRQn"
                if full in seen or value in used_values:
                    continue
                seen.add(full)
                used_values.add(value)
                device_entries.append(
                    (full, value, irq.description or name))
        device_entries.sort(key=lambda item: (item[1], item[0]))
        return entries, device_entries

    def render_interrupts(self):
        core_irqs, device_irqs = self.interrupt_entries()
        self.emit(full_divider())
        self.emit(divider_major("Interrupt Number Definition"))
        self.emit(full_divider())
        self.emit()
        self.emit("typedef enum {")
        self.emit(divider_minor("%s Specific Interrupt Numbers"
                                % self.core["display"]))
        for name, value, description in core_irqs:
            self.emit_all(self.irq_line(name, value, description))
        self.emit(divider_minor("%s Specific Interrupt Numbers"
                                % self.device.name))
        total = len(device_irqs)
        for index, (name, value, description) in enumerate(device_irqs):
            self.emit_all(self.irq_line(name, value, description,
                                        comma=index + 1 < total))
        self.emit("} IRQn_Type;")
        self.emit()
        self.emit()
        self.emit()

    def irq_line(self, name, value, description, comma=True):
        code = "  %-26s=%4d%s" % (name, value, "," if comma else "")
        number = str(value)
        prefix = number + (" " if len(number) % 2 else "")
        return comment_lines(code, description, lead=prefix)

    def core_revision(self):
        revision = self.device.cpu.revision if self.device.cpu else "r0p0"
        match = re.match(r"[rR](\d+)[pP](\d+)", revision)
        if match:
            major = int(match.group(1))
            minor = int(match.group(2))
            return "0x%04XU" % ((major << 8) | minor)
        return "0x0000U"

    def render_core_config(self):
        self.emit(full_divider())
        self.emit(divider_major("Processor and Core Peripheral Section"))
        self.emit(full_divider())
        self.emit()
        self.emit(divider_minor("Configuration of the %s Processor and Core "
                                "Peripherals" % self.core["display"]))
        cpu = self.device.cpu
        rev_name = "__%s_REV" % self.core["rev"]
        rev_line = "#define %s" % rev_name
        rev_line += " " * 17 + self.core_revision()
        self.emit_all(comment_lines(
            rev_line,
            "%s Core Revision" % self.core.get("rev_text", self.core["rev"])))
        prio = cpu.nvic_prio_bits if cpu is not None else 0
        self.emit_all(self.config_value_line(
            "__NVIC_PRIO_BITS", str(prio),
            "Number of Bits used for Priority Levels"))
        systick = 1 if (cpu is not None and cpu.vendor_systick_config) else 0
        self.emit_all(self.config_value_line(
            "__Vendor_SysTickConfig", str(systick),
            "Set to 1 if different SysTick Config is used"))
        present = [
            ("vtor", "__VTOR_PRESENT", "Set to 1 if CPU supports Vector Table "
                                       "Offset Register"),
            ("mpu", "__MPU_PRESENT", "MPU present"),
            ("fpu", "__FPU_PRESENT", "FPU present"),
            ("fpu_dp", "__FPU_DP", "Double Precision FPU"),
            ("dsp", "__DSP_PRESENT", "DSP extension present"),
            ("icache", "__ICACHE_PRESENT", "Instruction Cache present"),
            ("dcache", "__DCACHE_PRESENT", "Data Cache present"),
            ("itcm", "__ITCM_PRESENT", "Instruction TCM present"),
            ("dtcm", "__DTCM_PRESENT", "Data TCM present"),
        ]
        flags = {
            "vtor": "vtorPresent",
            "mpu": "mpuPresent",
            "fpu": "fpuPresent",
            "fpu_dp": "fpuDP",
            "dsp": "dspPresent",
            "icache": "icachePresent",
            "dcache": "dcachePresent",
            "itcm": "itcmPresent",
            "dtcm": "dtcmPresent",
        }
        enabled = {
            "vtor": self.core.get("vtor"),
            "mpu": self.core.get("mpu"),
            "fpu": self.core.get("fpu"),
            "fpu_dp": self.core.get("fpu_dp"),
            "dsp": self.core.get("dsp"),
            "icache": self.core.get("cache"),
            "dcache": self.core.get("cache"),
            "itcm": self.core.get("tcm"),
            "dtcm": self.core.get("tcm"),
        }
        for key, macro, text in present:
            if not enabled.get(key):
                continue
            if cpu is None:
                continue
            if cpu.node.find(flags[key]) is None:
                if not (key == "vtor" and self.core["core"] not in
                        ("other", "cm0", "cm1", "sc000", "ca5", "ca7", "ca9",
                         "ca15")):
                    continue
            value = 1 if cpu.flag(flags[key]) else 0
            self.emit_all(self.config_value_line(macro, str(value), text))
        self.emit()
        self.emit()
        self.emit("/** @} */ /* End of group Configuration_of_CMSIS */")
        self.emit()
        core_header = "core_%s.h" % self.core["core"]
        self.emit_all(comment_lines('#include "%s"' % core_header,
                                "%s processor and core peripherals"
                                % self.core["display"]))
        system_header = self.device.header_system_filename or \
            ("system_%s" % self.device.name)
        self.emit_all(comment_lines('#include "%s.h"' % system_header,
                                "%s System" % self.device.name))
        self.emit()
        self.emit_all(comment_lines("#ifndef __IM",
                                "Fallback for older CMSIS versions"))
        self.emit("  #define __IM   __I")
        self.emit("#endif")
        self.emit_all(comment_lines("#ifndef __OM",
                                "Fallback for older CMSIS versions"))
        self.emit("  #define __OM   __O")
        self.emit("#endif")
        self.emit_all(comment_lines("#ifndef __IOM",
                                "Fallback for older CMSIS versions"))
        self.emit("  #define __IOM  __IO")
        self.emit("#endif")
        self.emit()
        self.emit()

    def config_value_line(self, name, value, text):
        code = "#define %s" % name
        padding = 40 - len(code) - len(value)
        return comment_lines(code + " " * max(padding, 1) + value, text)

    # -- anonymous unions --------------------------------------------------

    def render_anonymous_unions_begin(self):
        if not self.has_unions:
            return
        self.emit(divider_minor("Start of section using anonymous unions"))
        self.emit("#if defined (__CC_ARM)")
        self.emit("  #pragma push")
        self.emit("  #pragma anon_unions")
        self.emit("#elif defined (__ICCARM__)")
        self.emit("  #pragma language=extended")
        self.emit("#elif defined(__ARMCC_VERSION) && (__ARMCC_VERSION >= 6010050)")
        self.emit("  #pragma clang diagnostic push")
        self.emit('  #pragma clang diagnostic ignored "-Wc11-extensions"')
        self.emit('  #pragma clang diagnostic ignored "-Wreserved-id-macro"')
        self.emit('  #pragma clang diagnostic ignored "-Wgnu-anonymous-struct"')
        self.emit('  #pragma clang diagnostic ignored "-Wnested-anon-types"')
        self.emit("#elif defined (__GNUC__)")
        self.emit("  /* anonymous unions are enabled by default */")
        self.emit("#elif defined (__TMS470__)")
        self.emit("  /* anonymous unions are enabled by default */")
        self.emit("#elif defined (__TASKING__)")
        self.emit("  #pragma warning 586")
        self.emit("#elif defined (__CSMC__)")
        self.emit("  /* anonymous unions are enabled by default */")
        self.emit("#else")
        self.emit("  #warning Not supported compiler type")
        self.emit("#endif")
        self.emit()
        self.emit()

    def render_anonymous_unions_end(self):
        if not self.has_unions:
            return
        self.emit(divider_minor("End of section using anonymous unions"))
        self.emit("#if defined (__CC_ARM)")
        self.emit("  #pragma pop")
        self.emit("#elif defined (__ICCARM__)")
        self.emit("  /* leave anonymous unions enabled */")
        self.emit("#elif defined(__ARMCC_VERSION) && (__ARMCC_VERSION >= 6010050)")
        self.emit("  #pragma clang diagnostic pop")
        self.emit("#elif defined (__GNUC__)")
        self.emit("  /* anonymous unions are enabled by default */")
        self.emit("#elif defined (__TMS470__)")
        self.emit("  /* anonymous unions are enabled by default */")
        self.emit("#elif defined (__TASKING__)")
        self.emit("  #pragma warning restore")
        self.emit("#elif defined (__CSMC__)")
        self.emit("  /* anonymous unions are enabled by default */")
        self.emit("#endif")
        self.emit()
        self.emit()

    # -- structure sections ------------------------------------------------

    def render_cluster_section(self):
        if not self.model.cluster_blocks:
            return
        self.emit(full_divider())
        self.emit(divider_major("Device Specific Cluster Section"))
        self.emit(full_divider())
        self.emit()
        self.emit()
        self.emit("/** @addtogroup Device_Peripheral_clusters")
        self.emit("  * @{")
        self.emit("  */")
        self.emit()
        self.emit()
        for block in self.model.cluster_blocks:
            self.render_cluster_struct(block)
            self.emit()
            self.emit()
        self.emit("/** @} */ /* End of group Device_Peripheral_clusters */")
        self.emit()
        self.emit()

    def render_peripheral_section(self):
        self.emit(full_divider())
        self.emit(divider_major("Device Specific Peripheral Section"))
        self.emit(full_divider())
        self.emit()
        self.emit()
        self.emit("/** @addtogroup Device_Peripheral_peripherals")
        self.emit("  * @{")
        self.emit("  */")
        self.emit()
        for block in self.model.peripheral_blocks:
            self.emit()
            self.emit()
            self.emit(full_divider())
            self.emit(divider_major(block.title))
            self.emit(full_divider())
            self.emit()
            self.emit()
            self.render_peripheral_struct(block)
            self.emit()
        self.emit()
        self.emit("/** @} */ /* End of group Device_Peripheral_peripherals */")
        self.emit()
        self.emit()

    def block_layout(self, block):
        """Return the struct member lines and the size of *block*."""
        lines = []
        layout, size = self.layout(block.registers)
        for entry in layout:
            if entry[0] == "error":
                lines.append(entry[1])
            elif entry[0] == "register":
                lines.extend(self.member_line(entry[1]))
            elif entry[0] == "union":
                lines.append("  ")
                lines.append("  union {")
                for member in entry[1]:
                    lines.extend(self.member_line(member, indent=4,
                                                  align=False))
                lines.append("  };")
            else:
                lines.append(entry[1])
        return lines, size

    def render_cluster_struct(self, block):
        members, size = self.block_layout(block)
        self.emit("/**")
        self.emit("  * @brief %s [%s] (%s)" % (block.type_name, block.title,
                                               block.description))
        self.emit("  */")
        self.emit("typedef struct {")
        for line in members:
            self.emit(line)
        self.emit_all(comment_lines("} %s;" % (block.type_name + "_Type"),
                                    "Size = %d (0x%x)" % (size, size)))

    def render_peripheral_struct(self, block):
        members, size = self.block_layout(block)
        self.emit("/**")
        self.emit("  * @brief %s (%s)" % (block.description, block.title))
        self.emit("  */")
        self.emit()
        self.emit_all(comment_lines("typedef struct {",
                                    "%s Structure" % block.title))
        for line in members:
            self.emit(line)
        self.emit_all(comment_lines("} %s;" % (block.type_name + "_Type"),
                                    "Size = %d (0x%x)" % (size, size)))

    def member_line(self, member, indent=2, align=True):
        access = access_macro(member.access)
        if getattr(member, "cluster_block", None) is not None:
            type_name = member.cluster_block.type_name + "_Type"
        elif getattr(member, "cluster_type_name", None):
            type_name = member.cluster_type_name + "_Type"
        else:
            type_name = c_type(member.size)
        name = member.name
        if member.array:
            name = "%s[%d]" % (name, member.array)
        if align and len(type_name) < 10:
            type_name += " " * (10 - len(type_name))
        else:
            type_name += " "
        code = "%s%-5s %s%s;" % (" " * indent, access, type_name, name)
        return comment_lines(code, member.description)

    def layout(self, registers):
        """Return the struct member layout of *registers* and the size."""
        entries = []
        reserved = 0
        for entry in compute_layout(registers)[0]:
            if entry[0] != "reserved":
                entries.append(entry)
                continue
            _, bits, count = entry
            name = "RESERVED" if reserved == 0 else "RESERVED%d" % reserved
            reserved += 1
            count_text = "" if count == 1 else "[%d]" % count
            entries.append(("reserved",
                            "  __IM  %-10s%s%s;" % (c_type(bits), name,
                                                    count_text)))
        return entries, compute_layout(registers)[1]

    # -- address map and declarations --------------------------------------

    def render_address_map(self):
        self.emit(full_divider())
        self.emit(divider_major("Device Specific Peripheral Address Map"))
        self.emit(full_divider())
        self.emit()
        self.emit()
        self.emit("/** @addtogroup Device_Peripheral_peripheralAddr")
        self.emit("  * @{")
        self.emit("  */")
        self.emit()
        for per in self.model.peripherals:
            code = "#define %s_BASE" % per.svd_name
            value = "0x%08XUL" % per.base_address
            column = max(BASE_VALUE_END - len(value), len(code) + 1)
            self.emit(code + " " * (column - len(code)) + value)
        self.emit()
        self.emit("/** @} */ /* End of group Device_Peripheral_peripheralAddr */")
        self.emit()
        self.emit()

    def render_declarations(self):
        self.emit(full_divider())
        self.emit(divider_major("Peripheral declaration"))
        self.emit(full_divider())
        self.emit()
        self.emit()
        self.emit("/** @addtogroup Device_Peripheral_declaration")
        self.emit("  * @{")
        self.emit("  */")
        self.emit()
        for per in self.model.peripherals:
            type_name = per.type_name + "_Type"
            code = "#define %s" % per.svd_name
            cast = "((%s*)" % type_name
            line = code + " " * max(DECL_CAST_COL - len(code), 1) + cast
            column = max(DECL_BASE_COL, len(line) + 2)
            line = line + " " * (column - len(line))
            line += "%s_BASE)" % per.svd_name
            self.emit(line)
        self.emit()
        self.emit("/** @} */ /* End of group Device_Peripheral_declaration */")
        self.emit()
        self.emit()

    # -- pos/mask sections -------------------------------------------------

    def render_pos_mask_clusters(self):
        if not self.model.cluster_blocks:
            return
        self.render_pos_mask_section(
            self.model.cluster_blocks, "Pos/Mask Cluster Section",
            "PosMask_clusters")

    def render_pos_mask_peripherals(self):
        self.render_pos_mask_section(
            self.model.peripheral_blocks, "Pos/Mask Peripheral Section",
            "PosMask_peripherals")

    def render_pos_mask_section(self, blocks, title, group):
        self.emit(full_divider())
        self.emit(divider_major(title))
        self.emit(full_divider())
        self.emit()
        self.emit()
        self.emit("/** @addtogroup %s" % group)
        self.emit("  * @{")
        self.emit("  */")
        self.emit()
        self.emit()
        if blocks:
            for block in blocks:
                self.emit()
                self.emit(full_divider())
                self.emit(divider_major(block.macro_title))
                self.emit(full_divider())
                self.emit()
                self.render_pos_mask_block(block)
                self.emit()
        self.emit("/** @} */ /* End of group %s */" % group)
        self.emit()
        self.emit()

    def render_pos_mask_block(self, block):
        for reg in block.registers:
            if reg.is_cluster:
                continue
            self.emit(divider_minor(reg.name))
            prefix = "%s_%s" % (block.member_prefix, reg.name)
            for field in reg.fields:
                self.emit_all(comment_lines(
                    self.define_line("%s_%s_Pos" % (prefix, field.name),
                                     "(%dUL)" % field.lsb),
                    "%s (Bit %d)" % (field.name, field.lsb),
                    DEFINE_COMMENT_COL))
                mask_text = "%s (Bitfield-Mask: 0x%02x)" % (
                    field.name, field.mask >> field.lsb)
                self.emit_all(comment_lines(
                    self.define_line("%s_%s_Msk" % (prefix, field.name),
                                     "(0x%xUL)" % field.mask),
                    mask_text, DEFINE_COMMENT_COL))

    def define_line(self, name, value):
        """Return ``#define <name> <value>`` aligned for the value column."""
        code = "#define %s" % name
        if len(code) < DEFINE_VALUE_COL:
            return code + " " * (DEFINE_VALUE_COL - len(code)) + value
        return code + " " + value

    # -- enum sections -----------------------------------------------------

    def render_enum_clusters(self):
        if not self.model.cluster_blocks:
            return
        self.render_enum_section(
            self.model.cluster_blocks, "Enumerated Values Cluster Section",
            "EnumValue_clusters")

    def render_enum_peripherals(self):
        self.render_enum_section(
            self.model.peripheral_blocks,
            "Enumerated Values Peripheral Section", "EnumValue_peripherals")

    def render_enum_section(self, blocks, title, group):
        self.emit(full_divider())
        self.emit(divider_major(title))
        self.emit(full_divider())
        self.emit()
        self.emit()
        self.emit("/** @addtogroup %s" % group)
        self.emit("  * @{")
        self.emit("  */")
        self.emit()
        self.emit()
        if blocks:
            for block in blocks:
                self.emit()
                self.emit(full_divider())
                self.emit(divider_major(block.section_title))
                self.emit(full_divider())
                self.emit()
                self.render_enum_block(block)
                self.emit()
        self.emit("/** @} */ /* End of group %s */" % group)
        self.emit()
        self.emit()

    def render_enum_block(self, block):
        for reg in block.registers:
            if reg.is_cluster:
                continue
            self.emit(divider_minor(reg.name))
            for field in reg.fields:
                if not field.enums:
                    continue
                self.emit(divider_minor("%s %s %s [%d..%d]" % (
                    block.section_title, reg.name, field.name,
                    field.lsb, field.msb)))
                for enum in field.enums:
                    self.emit_all(comment_lines(
                        "typedef enum {", enum.base_name))
                    signed = field.width >= 32
                    for name, value, description in enum.values:
                        full = enum.member_prefix + "_" + name
                        if len(full) < 37:
                            full = full.ljust(37)
                        else:
                            full += " "
                        number = value
                        if signed and number >= 0x80000000:
                            number -= 0x100000000
                        code = "  %s= %d," % (full, number)
                        text = "%s : %s" % (name, description or name)
                        self.emit_all(comment_lines(code, text, sep=""))
                    self.emit("} %s_Enum;" % enum.type_name)
                    self.emit()

    # -- footer ------------------------------------------------------------

    def render_footer(self):
        self.emit("#ifdef __cplusplus")
        self.emit("}")
        self.emit("#endif")
        self.emit()
        self.emit("#endif /* %s */" % self.guard_name())
        self.emit()
        self.emit()
        self.emit("/** @} */ /* End of group %s */" % self.device.name)
        self.emit()
        self.emit("/** @} */ /* End of group %s */" % self.device.vendor)


# ---------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------

FIELD_TYPES = ("enum", "macro", "struct", "struct-ansic")


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="CMSIS-SVD to C device header converter",
        epilog="Example: %s MyDevice.svd -o include "
               "--generate=header --fields=macro --fields=enum" % PROGRAM)
    parser.add_argument("input", nargs="+", metavar="SVD",
                        help="CMSIS-SVD input file")
    parser.add_argument("-o", "--outdir", metavar="DIR",
                        help="output directory (default: input directory)")
    parser.add_argument("--generate", metavar="KIND", default="header",
                        help="what to generate: header (default)")
    parser.add_argument("--fields", metavar="FIELD", action="append",
                        choices=FIELD_TYPES,
                        help="field generation: %s (default: macro, enum)"
                             % ", ".join(FIELD_TYPES))
    parser.add_argument("--license-file", metavar="FILE",
                        help="file holding the header comment (default: "
                             "<licenseText> from the SVD)")
    parser.add_argument("--create-folder", action="store_true",
                        help="create the output directory if needed")
    parser.add_argument("--quiet", action="store_true",
                        help="no output on the console")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="verbose output")
    parser.add_argument("-b", "--log", metavar="FILE",
                        help="write diagnostics to FILE")
    parser.add_argument("--suppress-warnings", action="store_true",
                        help="suppress all warnings")
    parser.add_argument("--strict", action="store_true",
                        help="strict error checking")
    # SVDConv compatibility switches that do not affect header generation.
    for flag, metavar in (("--suppress-path", None),
                          ("--show-missingEnums", None),
                          ("--allow-suppress-error", None),
                          ("--under-test", None),
                          ("--nocleanup", None)):
        parser.add_argument(flag, action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("-x", "--diag-suppress", metavar="MSG",
                        action="append", default=[],
                        help="suppress a message id (accepted, ignored)")
    parser.add_argument("-w", metavar="LEVEL", type=int, default=0,
                        help="warning level (accepted, ignored)")
    parser.add_argument("--debug", metavar="KIND", action="append", default=[],
                        help="debug output kind (accepted, ignored)")
    parser.add_argument("-n", metavar="FILE", default=None,
                        help="SFD output file name (accepted, ignored)")
    parser.add_argument("--version", action="version",
                        version="%s %s (CMSIS-SVD header generator, "
                                "SVDConv %s compatible)"
                                % (PROGRAM, VERSION, SVDCONV_VERSION))
    return parser


def convert(path, args, log):
    """Convert a single SVD file; returns the process exit code."""
    try:
        device = parse_svd(path)
    except SvdError as exc:
        log("ERROR: %s" % exc)
        return 2
    outdir = args.outdir or os.path.dirname(os.path.abspath(path))
    if args.create_folder and not os.path.isdir(outdir):
        os.makedirs(outdir, exist_ok=True)
    if not os.path.isdir(outdir):
        log("ERROR: output directory '%s' does not exist" % outdir)
        return 2
    out_name = device.name + ".h"
    out_path = os.path.join(outdir, out_name)
    license_text = None
    if args.license_file:
        try:
            with open(args.license_file, "r", encoding="utf-8") as handle:
                license_text = handle.read()
        except OSError as exc:
            log("ERROR: cannot read license file: %s" % exc)
            return 1
    if args.verbose:
        log("Input:      %s" % os.path.abspath(path))
        log("Device:     %s %s" % (device.name, device.version))
        log("Output:     %s" % os.path.abspath(out_path))
    model = Model(device, log)
    generator = Generator(model, path, out_name, license_text, log,
                          args.fields)
    if args.verbose:
        log("Peripherals: %d (%d structure types)"
            % (len(model.peripherals), len(model.peripheral_blocks)))
        log("Clusters:    %d" % len(model.cluster_blocks))
    text = generator.render()
    try:
        with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    except OSError as exc:
        log("ERROR: cannot write '%s': %s" % (out_path, exc))
        return 2
    if not args.quiet:
        log("Generated: %s" % out_path)
    if model.errors:
        log("Found %d error(s)." % len(model.errors))
        return 2
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.generate.lower() not in ("header", "h"):
        parser.error("only '--generate=header' is supported")
    if not args.fields:
        args.fields = ["macro", "enum"]

    log_handle = None
    if args.log:
        try:
            log_handle = open(args.log, "w", encoding="utf-8")
        except OSError as exc:                          # pragma: no cover
            sys.stderr.write("ERROR: cannot open log file: %s\n" % exc)
            return 1

    def log(message):
        """Write a diagnostic message; errors are never suppressed."""
        is_error = message.startswith("ERROR")
        if args.suppress_warnings and message.startswith("WARNING"):
            return
        if log_handle is not None:
            log_handle.write(message + "\n")
            log_handle.flush()
        if is_error:
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
        elif not args.quiet:
            sys.stdout.write(message + "\n")
            sys.stdout.flush()

    if not args.quiet:
        sys.stdout.write("%s %s - CMSIS-SVD device header generator\n\n"
                         % (PROGRAM, VERSION))

    status = 0
    try:
        for path in args.input:
            result = convert(path, args, log)
            status = max(status, result)
            if result != 0 and len(args.input) > 1:
                continue
    finally:
        if log_handle is not None:
            log_handle.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
