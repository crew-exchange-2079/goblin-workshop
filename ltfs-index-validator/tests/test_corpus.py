"""The committed corpus: every case through the real CLI, plus a drift check."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "corpus"
MANIFEST = json.loads((CORPUS / "manifest.json").read_text())["cases"]


def cli(*args):
    p = subprocess.run([sys.executable, str(ROOT / "validate.py"), *args],
                       capture_output=True, text=True, timeout=30)
    return p.returncode, json.loads(p.stdout)


class Corpus(unittest.TestCase):
    def test_corpus_matches_generator(self):
        p = subprocess.run([sys.executable, str(ROOT / "tests" / "make_corpus.py"), "--check"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_every_case_through_cli(self):
        self.assertGreaterEqual(len(MANIFEST), 30)
        exits = {"VALID": 0, "INVALID": 1}
        for case in MANIFEST:
            with self.subTest(case["file"]):
                args = ["--input", str(CORPUS / case["file"])]
                if case["previous"]:
                    args += ["--previous", str(CORPUS / case["previous"])]
                rc, rep = cli(*args)
                self.assertEqual((rc, rep["status"]), (exits[case["status"]], case["status"]),
                                 rep["diagnostics"])
                got = {(d["code"], d["severity"]) for d in rep["diagnostics"]}
                if not case["codes"]:
                    self.assertEqual(got, set())
                for code in case["codes"]:
                    self.assertIn((code, case["severity"]), got)
                if "strict_status" in case:
                    rc, rep = cli(*args, "--strict")
                    self.assertEqual((rc, rep["status"]), (1, case["strict_status"]))

    def test_good_cases_with_volume_facts(self):
        for case in MANIFEST:
            if case["status"] == "VALID" and not case["codes"]:
                with self.subTest(case["file"]):
                    rc, rep = cli("--input", str(CORPUS / case["file"]), "--strict",
                                  "--index-partition", "a", "--data-partition", "b",
                                  "--blocksize", "524288")
                    self.assertEqual((rc, rep["status"]), (0, "VALID"), rep["diagnostics"])


if __name__ == "__main__":
    unittest.main()
