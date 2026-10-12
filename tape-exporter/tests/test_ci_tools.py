"""Tests for the mhvtl integration verdict logic, on synthetic strace text.

These do not replace the integration run; they make sure the checker fails
closed on the things it is meant to catch.
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ci", "mhvtl"))
import check_evidence as ce  # noqa: E402

SG = "/dev/sg5"
SG_LOGS = "/usr/bin/sg_logs"


def h(s):
    return "".join(f"\\x{b:02x}" for b in (s.encode() if isinstance(s, str) else s))


def execve(path, argv, ret="0"):
    args = ", ".join(f'"{h(a)}"' for a in argv)
    return f'101  execve("{h(path)}", [{args}], 0x7ffd /* 2 vars */) = {ret}'


def openat(path, flags, fd=3):
    return f'101  openat(AT_FDCWD<{h("/")}>, "{h(path)}", {flags}) = {fd}<{h(path)}>'


def sg_io(cdb, path=SG):
    return (f'101  ioctl(3<{h(path)}>, SG_IO, {{interface_id=\'S\', dxfer_direction=SG_DXFER_FROM_DEV, '
            f'cmd_len={len(cdb)}, cmdp="{h(cdb)}", mx_sb_len=64}}) = 0')


INQ = bytes([0x12, 0, 0, 0, 0x24, 0])


def log_sense(page, b1=0):
    return bytes([0x4D, b1, 0x40 | page, 0, 0, 0, 0, 0x10, 0, 0])


def good_trace():
    lines = [execve("/opt/venv/bin/lto-exporter", ["/opt/venv/bin/lto-exporter", "--config", "c"])]
    for page in (2, 3):
        lines += [execve(SG_LOGS, [SG_LOGS, "--readonly", f"--page=0x{page}", SG]),
                  openat("/proc/devices", "O_RDONLY"),
                  openat(SG, "O_RDONLY|O_NONBLOCK"),
                  sg_io(INQ), sg_io(log_sense(page))]
    return lines


class CheckEvidenceTests(unittest.TestCase):
    def verdict(self, lines):
        return ce.analyse_strace("\n".join(lines) + "\n", SG, SG_LOGS)

    def test_good_trace_passes(self):
        v = self.verdict(good_trace())
        self.assertEqual(v.failed, [], v.checks)

    def test_catches_forbidden_cdbs(self):
        for cdb in (bytes([0x4C, 0x02, 0x40, 0, 0, 0, 0, 0, 0, 0]),   # LOG SELECT (reset)
                    log_sense(0x2E),                                   # TapeAlert page
                    log_sense(2, b1=0x01),                             # SP=1 (save)
                    bytes([0x01, 0, 0, 0, 0, 0]),                      # REWIND
                    bytes([0x1B, 0, 0, 0, 0x01, 0]),                   # LOAD/UNLOAD
                    bytes([0x0A, 0, 0, 0, 1, 0]),                      # WRITE(6)
                    bytes([0x10, 0, 0, 0, 1, 0]),                      # WRITE FILEMARKS
                    bytes([0x15, 0x10, 0, 0, 0, 0])):                  # MODE SELECT
            v = self.verdict(good_trace() + [sg_io(cdb)])
            self.assertIn("cdb: only INQUIRY / LOG SENSE(SP=0,PPC=0,page 0x02|0x03)",
                          [c["check"] for c in v.failed], cdb.hex())

    def test_catches_rdwr_open_other_nodes_and_execs(self):
        cases = [
            [openat(SG, "O_RDWR|O_NONBLOCK")],
            [openat("/dev/sg0", "O_RDONLY")],
            [openat("/dev/nst0", "O_RDONLY")],
            [execve("/usr/sbin/tapeinfo", ["/usr/sbin/tapeinfo", "-f", SG])],
            [execve(SG_LOGS, [SG_LOGS, "--readonly", "--page=0x2", "/dev/sg0"])],
            [execve(SG_LOGS, [SG_LOGS, "--reset", "--page=0x2", SG])],
            [f'101  write(3<{h(SG)}>, "{h(b"x")}", 1) = 1'],
            [f'101  ioctl(3<{h(SG)}>, SG_SET_RESERVED_SIZE, [65536]) = 0'],
            [sg_io(INQ, path="/dev/sg0")],
        ]
        for extra in cases:
            self.assertTrue(self.verdict(good_trace() + extra).failed, extra)

    def test_undecoded_or_missing_evidence_fails_closed(self):
        self.assertTrue(self.verdict([]).failed)
        truncated = good_trace() + [f'101  ioctl(3<{h(SG)}>, SG_IO, {{interface_id=\'S\', cmdp="{h(INQ)}"...}}) = 0']
        self.assertTrue(self.verdict(truncated).failed)
        only_write_page = [l for l in good_trace() if h(log_sense(3)) not in l]
        self.assertTrue(self.verdict(only_write_page).failed)

    def test_metrics_parsing(self):
        rows = ce.parse_metrics('a_b{device="d",status="ok"} 1\n# HELP x\nc 0\n')
        self.assertEqual(rows, [("a_b", {"device": "d", "status": "ok"}, "1"), ("c", {}, "0")])


if __name__ == "__main__":
    unittest.main()
