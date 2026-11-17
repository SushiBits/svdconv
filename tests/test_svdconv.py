"""Tests for svdconv.

Run with::

    python -m unittest discover -s tests -v
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK = os.path.join(ROOT, "check")
TOOL = os.path.join(ROOT, "svdconv.py")

METADATA = re.compile(
    r"^ \* @file|^ \* @date|^ \*           from File|^ \*           last modified")


def strip_metadata(text):
    """Drop the lines that depend on the build time and on file paths."""
    return [line for line in text.splitlines() if not METADATA.match(line)]


def run_tool(argv):
    return subprocess.run([sys.executable, TOOL] + argv,
                          capture_output=True, text=True)


class ReferenceHeaderTest(unittest.TestCase):
    """The generated headers must match the SVDConv reference output."""

    def check_device(self, device):
        svd = os.path.join(CHECK, device + ".svd")
        reference = os.path.join(CHECK, device + ".h")
        if not (os.path.exists(svd) and os.path.exists(reference)):
            self.skipTest("%s reference files are not available" % device)
        with tempfile.TemporaryDirectory() as outdir:
            result = run_tool([svd, "-o", outdir, "--quiet"])
            self.assertEqual(result.returncode, 0, result.stderr)
            generated = os.path.join(outdir, device + ".h")
            self.assertTrue(os.path.exists(generated))
            with open(generated, encoding="utf-8", newline="") as handle:
                got = strip_metadata(handle.read())
            with open(reference, encoding="utf-8", newline="") as handle:
                expected = strip_metadata(handle.read())
        self.assertEqual(expected, got)

    def test_lpc2103(self):
        self.check_device("LPC2103")

    def test_lks32mc03x(self):
        self.check_device("LKS32MC03x")

    def test_mspm0l130x(self):
        self.check_device("MSPM0L130X")


MINIMAL_SVD = """\
<?xml version="1.1" encoding="UTF-8"?>
<device schemaVersion="1.1">
  <name>{name}</name>
  <version>1.0</version>
  <description>{name} device</description>
  <cpu>
    <name>CM0</name>
    <revision>r0p0</revision>
    <endian>little</endian>
    <fpuPresent>false</fpuPresent>
    <mpuPresent>false</mpuPresent>
    <nvicPrioBits>2</nvicPrioBits>
    <vendorSystickConfig>false</vendorSystickConfig>
  </cpu>
  <addressUnitBits>8</addressUnitBits>
  <width>32</width>
  <size>32</size>
  <access>read-write</access>
  <peripherals>
    <peripheral>
      <name>PER</name>
      <description>Peripheral</description>
      <baseAddress>0x40000000</baseAddress>
      <registers>
{registers}
      </registers>
    </peripheral>
  </peripherals>
</device>
"""


def register(name, offset, size=32, fields="", extra=""):
    return ("        <register><name>%s</name><description>%s</description>"
            "<addressOffset>%s</addressOffset><size>%d</size>%s%s</register>"
            % (name, name, offset, size, extra, fields))


class LayoutTest(unittest.TestCase):
    """Struct layout rules: reservations, unions and end padding."""

    def convert(self, registers, name="TEST"):
        svd = MINIMAL_SVD.format(name=name, registers=registers)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, name + ".svd")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(svd)
            result = run_tool([path, "-o", tmp, "--quiet"])
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(os.path.join(tmp, name + ".h"), encoding="utf-8") as f:
                return f.read()

    def test_gap_filling(self):
        # An 8 bit register at 0 followed by a 32 bit register at 8: the
        # gap is filled with 1 + 2 + 4 bytes to keep the alignment.
        header = self.convert(register("A", "0x0", 8) +
                              register("B", "0x8", 32))
        self.assertIn("  __IM  uint8_t   RESERVED;", header)
        self.assertIn("  __IM  uint16_t  RESERVED1;", header)
        self.assertIn("  __IM  uint32_t  RESERVED2;", header)
        self.assertIn("} PER_Type;", header)

    def test_byte_array_gap(self):
        # Two byte registers four bytes apart are joined into one array.
        header = self.convert(register("A", "0x0", 8) +
                              register("B", "0x4", 8))
        self.assertIn("  __IM  uint8_t   RESERVED[3];", header)

    def test_aliased_registers_form_a_union(self):
        header = self.convert(register("A", "0x0") + register("B", "0x0"))
        self.assertIn("  union {", header)
        self.assertIn("    __IOM uint32_t A;", header)
        self.assertIn("    __IOM uint32_t B;", header)

    def test_structure_alignment_padding(self):
        # 16 bit register at 0, 32 bit register at 4: two reserved bytes
        # are needed to align the 32 bit member.
        header = self.convert(register("A", "0x0", 16) +
                              register("B", "0x4", 32))
        self.assertIn("  __IM  uint16_t  RESERVED;", header)
        self.assertIn("Size = 8 (0x8)", header)

    def test_bit_macros_and_enumerations(self):
        fields = ("<fields><field><name>MODE</name>"
                  "<description>Mode</description><bitRange>[3:2]</bitRange>"
                  "<enumeratedValues>"
                  "<enumeratedValue><name>OFF</name><description>off</description>"
                  "<value>0</value></enumeratedValue>"
                  "<enumeratedValue><name>ON</name><description>on</description>"
                  "<value>1</value></enumeratedValue>"
                  "</enumeratedValues></field></fields>")
        header = self.convert(register("CTRL", "0x0", fields=fields))
        self.assertIn("(2UL)", header)
        self.assertIn("#define PER_CTRL_MODE_Msk", header)
        self.assertIn("(0xcUL)", header)
        self.assertIn("} PER_CTRL_MODE_Enum;", header)
        self.assertIn("PER_CTRL_MODE_ON", header)


class CommandLineTest(unittest.TestCase):
    def test_version(self):
        result = run_tool(["--version"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("svdconv", result.stdout)

    def test_missing_input_file(self):
        result = run_tool([os.path.join(ROOT, "does-not-exist.svd")])
        self.assertEqual(result.returncode, 2)

    def test_unsupported_generate_option(self):
        result = run_tool(["x.svd", "--generate=partition"])
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
