"""Synthetic SSC cases; no device or subprocess access."""

from pathlib import Path
import unittest

from lto_exporter.parsers import (SequentialAccessPage,
                                  parse_sg_logs_error_counters,
                                  parse_sg_logs_sequential_access)


FIX = Path(__file__).parent / "fixtures"
HEADER = "Sequential access device log page (ssc-3)"
LABELS = (
    ("Data bytes received with WRITE operations", "bytes_received_write"),
    ("Data bytes written to media by WRITE operations", "bytes_written_media"),
    ("Data bytes read from media by READ operations", "bytes_read_media"),
    ("Data bytes transferred by READ operations", "bytes_transferred_read"),
)
REQUIRED = "  Cleaning action required (or in progress)"
NOT_REQUIRED = "  Cleaning action not required (or completed)"


def counter(raw, index=0):
    return f"  {LABELS[index][0]}: {raw}"


def page(*lines):
    return parse_sg_logs_sequential_access("\n".join((HEADER,) + lines))


class SequentialAccessTests(unittest.TestCase):
    def fixture(self, name):
        return (FIX / f"ssc0c-{name}.txt").read_text(encoding="utf-8")

    def test_normal_literal_synthetic_fixture(self):
        result = parse_sg_logs_sequential_access(self.fixture("normal"))
        self.assertIsInstance(result, SequentialAccessPage)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.values, {
            "bytes_received_write": 2500000,
            "bytes_written_media": 1250000,
            "bytes_read_media": 3100000,
            "bytes_transferred_read": 6200000,
            "cleaning_action_required": 1,
        })

    def test_zero_literal_fixture_is_partial(self):
        result = parse_sg_logs_sequential_access(self.fixture("zero"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.values, {
            "bytes_received_write": 0, "cleaning_action_required": 0,
        })

    def test_all_four_explicit_zeros(self):
        result = page(*(counter("0 GB [0 bytes]", i) for i in range(4)))
        self.assertEqual(result.values, {key: 0 for _, key in LABELS})

    def test_malformed_literal_fixture(self):
        result = parse_sg_logs_sequential_access(self.fixture("malformed"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.values, {})

    def test_nonverbose_precision_unknown_including_zero(self):
        for gb in ("0", "1", "18446744073"):
            with self.subTest(gb=gb):
                self.assertEqual(page(counter(f"{gb} GB")).values, {})
        self.assertEqual(page(counter("0 GB [1 bytes]")).values,
                         {"bytes_received_write": 1})

    def test_uint64_and_decimal_consistency_for_each_counter(self):
        for i, (_, key) in enumerate(LABELS):
            for exact in (0, 999999999, 1000000000, 2**64 - 1):
                with self.subTest(key=key, exact=exact):
                    raw = f"{exact // 1000000000} GB [{exact} bytes]"
                    self.assertEqual(page(counter(raw, i)).values, {key: exact})
            self.assertEqual(page(counter("18446744073 GB [18446744073709551616 bytes]", i)).values, {})

    def test_invalid_numeric_shapes_are_unknown(self):
        for raw in (
            "-1 GB [-1 bytes]", "0 GB [-1 bytes]", "+0 GB [+1 bytes]",
            "0 GB [oops bytes]", "0 GB [0x10 bytes]", "0 GB [1.0 bytes]",
            "0 GB [1e3 bytes]", "0 GB [1_000 bytes]", "0 GB [١ bytes]",
            "0 GB [1 bytes", "0 GB [1 bytes] trailing", "0 GiB [1 bytes]",
            "1 GB [999999999 bytes]", "0 GB [1000000000 bytes]",
            "999999999999999999999999999999 GB [1 bytes]",
            "0 GB [999999999999999999999999999999 bytes]",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(page(counter(raw)).values, {})

    def test_missing_wrong_and_commented_headers(self):
        for heading in (
            "", "Read error counter log page [0x3]",
            "Logical block provisioning log page [0x0c]",
            "Sequential access device log page [0x0c]",
            "Sequential access device log page (ssc-2)",
            "# " + HEADER, "  " + HEADER,
        ):
            with self.subTest(heading=heading):
                result = parse_sg_logs_sequential_access(
                    heading + "\n" + counter("0 GB [1 bytes]") + "\n" + REQUIRED)
                self.assertEqual(result.status, "parse_error")
                self.assertEqual(result.values, {})

    def test_different_page_boundary_blocks_bytes_and_cleaning(self):
        for boundary in (
            "Read error counter page [0x3]",
            "Device statistics log page (ssc-3)",
            "Logical block provisioning log page [0x0c]",
            "# " + HEADER,
        ):
            with self.subTest(boundary=boundary):
                result = page(counter("0 GB [7 bytes]"), boundary,
                              counter("0 GB [8 bytes]", 1), REQUIRED)
                self.assertEqual(result.status, "ok")
                self.assertEqual(result.values, {"bytes_received_write": 7})

    def test_empty_partial_and_unknown_lines(self):
        self.assertEqual(page().status, "ok")
        self.assertEqual(page().values, {})
        self.assertEqual(page(
            "  Vendor error count: 99", "  Parameter code = 0xff: 12",
            "  Native capacity from BOP to EOD: 123 MB",
            "    parameter control byte: 0x00", "    cleaning value: 2",
            "  Cleaning action required (or in progress) extra",
        ).values, {})
        self.assertEqual(page(REQUIRED, "    cleaning value: 9").values,
                         {"cleaning_action_required": 1})

    def test_conflicting_cleaning_is_permanently_unknown(self):
        for narratives in ((REQUIRED, NOT_REQUIRED), (NOT_REQUIRED, REQUIRED),
                           (REQUIRED, NOT_REQUIRED, REQUIRED)):
            self.assertEqual(page(*narratives).values, {})
        self.assertEqual(page(NOT_REQUIRED, NOT_REQUIRED).values,
                         {"cleaning_action_required": 0})

    def test_numeric_duplicates_never_sum_or_recover_conflicts(self):
        good = counter("0 GB [5 bytes]")
        self.assertEqual(page(good, good).values, {"bytes_received_write": 5})
        for lines in (
            (good, counter("0 GB [6 bytes]"), good),
            (counter("0 GB [oops bytes]"), good),
            (good, counter("0 GB [oops bytes]")),
            (counter("0 GB"), good), (good, counter("0 GB")),
        ):
            with self.subTest(lines=lines):
                self.assertEqual(page(*lines).values, {})

    def test_repeated_sections_preserve_conflicts(self):
        result = page(counter("0 GB [5 bytes]"), REQUIRED, HEADER,
                      counter("0 GB [6 bytes]"), NOT_REQUIRED,
                      counter("0 GB [7 bytes]", 1))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.values, {"bytes_written_media": 7})

    def test_error_page_parser_still_rejects_ssc(self):
        for direction in ("read", "write"):
            result = parse_sg_logs_error_counters(self.fixture("normal"), direction)
            self.assertEqual(result.status, "parse_error")
            self.assertEqual(result.values, {})


class RealSgLogs146Tests(unittest.TestCase):
    """Golden output of the real sg_logs 1.46 binary (synthetic page bytes)."""

    def golden(self, name):
        return (FIX / "sg3_utils_1.46" / name).read_text(encoding="utf-8")

    def test_full_page(self):
        result = parse_sg_logs_sequential_access(self.golden("seqaccess_page.txt"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.values, {
            "bytes_received_write": 2199023255553,
            "bytes_written_media": 1500000000123,
            "bytes_read_media": 734003200000,
            "bytes_transferred_read": 1468006400000,
            "cleaning_action_required": 1,
        })

    def test_partial_page_cleaning_not_required(self):
        result = parse_sg_logs_sequential_access(self.golden("seqaccess_clean_page.txt"))
        self.assertEqual(result.values, {
            "bytes_received_write": 999999999,
            "bytes_written_media": 0,
            "cleaning_action_required": 0,
        })

    def test_nonverbose_output_gives_no_bytes(self):
        result = parse_sg_logs_sequential_access(self.golden("seqaccess_page_nonverbose.txt"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.values, {"cleaning_action_required": 1})

    def test_page_0xc_decoded_for_a_disk_is_rejected(self):
        result = parse_sg_logs_sequential_access(self.golden("seqaccess_page_as_disk.txt"))
        self.assertEqual(result.status, "parse_error")
        self.assertEqual(result.values, {})

    def test_inquiry_line_before_header_is_ignored(self):
        text = "    IBM       ULT3580-TD8       0105\n" + self.golden("seqaccess_page.txt")
        self.assertEqual(len(parse_sg_logs_sequential_access(text).values), 5)

    def test_mixed_spellings_share_conflict_tracking(self):
        result = parse_sg_logs_sequential_access("\n".join((
            "Sequential access device page (ssc-3)",
            "  Data bytes received with WRITE commands: 0 GB [5 bytes]",
            "  Cleaning action required",
            HEADER,
            counter("0 GB [6 bytes]"),
            NOT_REQUIRED,
        )))
        self.assertEqual(result.values, {})
