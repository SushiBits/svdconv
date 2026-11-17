"""Tests for svdconv.

Run with::

    python -m unittest discover -s tests -v
"""

import io
import os
import re
import shutil
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


def run_tool(argv, env=None):
    """Run the converter and return the completed process."""
    environ = os.environ.copy()
    environ.pop("MAKELEVEL", None)
    environ.pop("MAKEFLAGS", None)
    environ.pop("SVDCONV_WARNINGS_AS_ERRORS", None)
    if env:
        environ.update(env)
    return subprocess.run([sys.executable, TOOL] + argv,
                          capture_output=True, text=True, env=environ)


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
    <name>{cpu}</name>
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

    def convert(self, registers, name="TEST", cpu="CM0", argv=()):
        svd = MINIMAL_SVD.format(name=name, registers=registers, cpu=cpu)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, name + ".svd")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(svd)
            result = run_tool([path, "-o", tmp, "--quiet"] + list(argv))
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

    def test_werror_is_documented_in_help(self):
        result = run_tool(["--help"])
        self.assertIn("-Werror", result.stdout)

    def test_device_without_name_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "noname.svd")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write('<?xml version="1.1"?>\n<device '
                             'schemaVersion="1.1"><peripherals/></device>\n')
            result = run_tool([path, "-o", tmp, "--quiet"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("no <device><name>", result.stderr)


class CpuNameTest(unittest.TestCase):
    """Any string in <cpu> must be accepted."""

    def header_for(self, cpu):
        svd = MINIMAL_SVD.format(name="CPU", cpu=cpu,
                                 registers=register("R", "0x0"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "CPU.svd")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(svd)
            result = run_tool([path, "-o", tmp, "--quiet"])
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(os.path.join(tmp, "CPU.h"), encoding="utf-8") as handle:
                return handle.read()

    def test_known_core_is_unchanged(self):
        header = self.header_for("CM3")
        self.assertIn('#include "core_cm3.h"', header)
        self.assertIn("#define __CM3_REV", header)
        self.assertIn("Configuration of the ARM Cortex-M3 Processor", header)

    def test_core_lookup_ignores_case(self):
        header = self.header_for("cm0plus")
        self.assertIn('#include "core_cm0plus.h"', header)
        self.assertIn("#define __CM0PLUS_REV", header)

    def test_unknown_core_uses_its_name_for_the_include(self):
        # A hand written core_arm7tdmi-s.h is picked up by name, while the
        # device keeps the "other" core profile.
        header = self.header_for("arm7tdmi-s")
        self.assertIn('#include "core_arm7tdmi-s.h"', header)
        self.assertIn("Configuration of the arm7tdmi-s Processor", header)
        self.assertIn("#define __OTHER_REV", header)
        self.assertNotIn("#define __CM3_REV", header)
        self.assertNotIn("Reset_IRQn", header)

    def test_riscv_core_name(self):
        header = self.header_for("RV32IMAC")
        self.assertIn('#include "core_RV32IMAC.h"', header)
        self.assertIn("#define __OTHER_REV", header)

    def test_unknown_core_does_not_warn(self):
        svd = MINIMAL_SVD.format(name="CPU", cpu="RV32IMAC",
                                 registers=register("R", "0x0"))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "CPU.svd")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(svd)
            result = run_tool([path, "-o", tmp])
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("WARNING", result.stdout)
        self.assertIn("RV32IMAC", result.stdout)


DEFECTIVE_SVD = """\
<?xml version="1.1" encoding="UTF-8"?>
<device schemaVersion="1.1">
  <name>DEFECTIVE</name>
  <version>1.0</version>
  <description>Device with recoverable defects</description>
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
      <name>GOOD</name>
      <description>Good peripheral</description>
      <baseAddress>0x40000000</baseAddress>
      <interrupt><name>OK</name><description>fine</description><value>3</value></interrupt>
      <interrupt><name>FAR</name><description>too large</description><value>400</value></interrupt>
      <registers>
        <register>
          <name>CTRL</name>
          <description>Control</description>
          <addressOffset>0x0</addressOffset>
          <fields>
            <field>
              <name>MODE</name>
              <description>Mode</description>
              <bitRange>[1:0]</bitRange>
              <enumeratedValues>
                <enumeratedValue><name>OFF</name><description>off</description><value>0</value></enumeratedValue>
                <enumeratedValue><name>TOO_BIG</name><description>no</description><value>99</value></enumeratedValue>
              </enumeratedValues>
            </field>
          </fields>
        </register>
      </registers>
    </peripheral>
    <peripheral derivedFrom="NO_SUCH_PERIPHERAL">
      <name>BROKEN</name>
      <description>Broken derivation</description>
      <baseAddress>0x40001000</baseAddress>
    </peripheral>
  </peripherals>
</device>
"""


class MakeSafetyTest(unittest.TestCase):
    """Warnings must never fail the build unless the user opts in."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.svd = os.path.join(self.tmp, "DEFECTIVE.svd")
        with open(self.svd, "w", encoding="utf-8") as handle:
            handle.write(DEFECTIVE_SVD)

    def test_warnings_are_generated_and_non_fatal(self):
        result = run_tool([self.svd, "-o", self.tmp])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING: interrupt 'FAR'", result.stdout)
        self.assertIn("WARNING: peripheral 'BROKEN' derives from unknown",
                      result.stdout)
        self.assertIn("Found 0 Error(s) and 3 Warning(s).", result.stdout)
        self.assertTrue(
            os.path.exists(os.path.join(self.tmp, "DEFECTIVE.h")))

    def test_strict_turns_warnings_into_errors(self):
        result = run_tool([self.svd, "-o", self.tmp, "--strict"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("WARNING", result.stdout)

    def test_werror_turns_warnings_into_errors(self):
        result = run_tool([self.svd, "-o", self.tmp, "-Werror"])
        self.assertEqual(result.returncode, 2)

    def test_warnings_as_errors_spelling_is_accepted(self):
        result = run_tool([self.svd, "-o", self.tmp,
                           "--warnings-as-errors"])
        self.assertEqual(result.returncode, 2)

    def test_warnings_as_errors_environment(self):
        result = run_tool([self.svd, "-o", self.tmp, "--quiet"],
                          env={"SVDCONV_WARNINGS_AS_ERRORS": "1"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("Found 0 Error(s) and 3 Warning(s).", result.stderr)

    def test_environment_off_is_not_an_opt_in(self):
        result = run_tool([self.svd, "-o", self.tmp, "--quiet"],
                          env={"SVDCONV_WARNINGS_AS_ERRORS": "0"})
        self.assertEqual(result.returncode, 0)

    def test_make_environment_is_reported(self):
        result = run_tool([self.svd, "-o", self.tmp],
                          env={"MAKELEVEL": "0"})
        self.assertEqual(result.returncode, 0)
        self.assertIn("Running under make", result.stdout)

    def test_missing_input_still_fails_under_make(self):
        result = run_tool([os.path.join(self.tmp, "nope.svd"),
                           "-o", self.tmp],
                          env={"MAKELEVEL": "0"})
        self.assertEqual(result.returncode, 2)

    def test_quiet_still_explains_a_failure(self):
        result = run_tool([self.svd, "-o", self.tmp, "--quiet", "--strict"])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("Warning(s)", result.stderr)

    @unittest.skipUnless(shutil.which("make"), "make is not available")
    def test_gnu_make_recipe(self):
        makefile = os.path.join(self.tmp, "Makefile")
        recipe = "\t" + sys.executable + " " + TOOL + " $< -o $(OUT) --quiet\n"
        with open(makefile, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("OUT := out\n"
                         "all: $(OUT)/DEFECTIVE.h\n"
                         "$(OUT)/%.h: %.svd\n"
                         "\t@mkdir -p $(OUT)\n" + recipe)
        result = subprocess.run(["make", "-C", self.tmp],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(
            os.path.exists(os.path.join(self.tmp, "out", "DEFECTIVE.h")))


class DiagnosticsUnitTest(unittest.TestCase):
    """The reporter and the make detection are unit tested as well."""

    def load_module(self):
        sys.path.insert(0, ROOT)
        import svdconv
        return svdconv

    def test_running_under_make(self):
        module = self.load_module()
        self.assertFalse(module.running_under_make({}))
        self.assertTrue(module.running_under_make({"MAKELEVEL": "0"}))
        self.assertTrue(module.running_under_make({"MAKEFLAGS": "w"}))
        self.assertFalse(module.running_under_make({"PATH": "/bin"}))

    def test_reporter_exit_codes(self):
        module = self.load_module()
        reporter = module.Reporter(stream=io.StringIO(),
                                   error_stream=io.StringIO())
        self.assertEqual(reporter.exit_code, 0)
        reporter.warn("bad")
        self.assertEqual(reporter.exit_code, 0)
        reporter.warnings_as_errors = True
        self.assertEqual(reporter.exit_code, 2)
        reporter.error("worse")
        reporter.warnings_as_errors = False
        self.assertEqual(reporter.exit_code, 2)
        self.assertEqual(reporter.summary(),
                         "Found 1 Error(s) and 1 Warning(s).")

    def test_environment_flag(self):
        module = self.load_module()
        self.assertFalse(module.environment_flag("X", {}))
        for value in ("1", "true", "YES", "on"):
            self.assertTrue(module.environment_flag("X", {"X": value}))
        for value in ("0", "false", "no", "off", ""):
            self.assertFalse(module.environment_flag("X", {"X": value}))


if __name__ == "__main__":
    unittest.main()
