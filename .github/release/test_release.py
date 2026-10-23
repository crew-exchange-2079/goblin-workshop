"""Tests for release.py. Run from the repo root: python -m pytest -q .github/release"""

import importlib.util
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import release  # noqa: E402
from release import ReleaseError  # noqa: E402


class SemverTest(unittest.TestCase):
    def test_precedence(self):
        order = ["0.9.0", "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
                 "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.10.0", "2.0.0"]
        self.assertEqual(sorted(order, key=release.semver_key), order)

    def test_rejects(self):
        for bad in ("1.0", "01.0.0", "1.0.0+build", "v1.0.0", "1.0.0-", "1.0.0-01"):
            with self.assertRaises(ReleaseError, msg=bad):
                release.parse_semver(bad)

    def test_prerelease(self):
        self.assertTrue(release.is_prerelease("1.0.0-rc.1"))
        self.assertFalse(release.is_prerelease("1.0.0"))


class PythonVersionTest(unittest.TestCase):
    def test_mapping(self):
        cases = {"1.2.3": "1.2.3", "1.2.3-alpha.1": "1.2.3a1", "1.2.3-beta.2": "1.2.3b2",
                 "1.2.3-rc.10": "1.2.3rc10"}
        for semver, pep440 in cases.items():
            self.assertEqual(release.python_version(semver), pep440)

    def test_unmappable_prereleases_refused(self):
        for bad in ("1.0.0-rc", "1.0.0-rc.1.2", "1.0.0-preview.1", "1.0.0-alpha.beta", "1.0.0-dev.1"):
            with self.assertRaisesRegex(ReleaseError, "PEP 440", msg=bad):
                release.python_version(bad)

    def test_same_order_as_semver(self):
        order = ["1.0.0-alpha.1", "1.0.0-alpha.2", "1.0.0-beta.1", "1.0.0-rc.1", "1.0.0-rc.2", "1.0.0"]
        self.assertEqual(sorted(order, key=release.semver_key), order)
        # PEP 440 order of the mapped strings, by its pre-release segment rank.
        rank = {"a": 0, "b": 1, "rc": 2}
        def key(v):
            m = re.match(r"^1\.0\.0(?:(a|b|rc)(\d+))?$", release.python_version(v))
            return (1, 0, 0) if not m.group(1) else (0, rank[m.group(1)], int(m.group(2)))
        self.assertEqual(sorted(order, key=key), order)

    def test_python_tool_version_checked_on_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "t"))
            with open(os.path.join(tmp, "t", "v.py"), "w") as fh:
                fh.write('__version__ = "1.0.0-preview.1"\n')
            spec = {"file": "v.py", "regex": '^__version__ = "([^"]+)"$'}
            self.assertEqual(release.read_version({"path": "t", "kind": "source", "version": spec}, tmp),
                             "1.0.0-preview.1")
            with self.assertRaisesRegex(ReleaseError, "PEP 440"):
                release.read_version({"path": "t", "kind": "python", "version": spec}, tmp)


class TagTest(unittest.TestCase):
    def test_tool_names_containing_v(self):
        m = release.TAG_RE.match("ltfs-index-validator-v0.1.0")
        self.assertEqual((m.group("tool"), m.group("version")), ("ltfs-index-validator", "0.1.0"))
        m = release.TAG_RE.match("tape-exporter-v1.0.0-rc.1")
        self.assertEqual((m.group("tool"), m.group("version")), ("tape-exporter", "1.0.0-rc.1"))

    def test_legacy_and_junk_tags_do_not_match(self):
        for tag in ("v0.2.0", "tape-exporter", "tape-exporter-0.3.0", "-v1.0.0"):
            self.assertIsNone(release.TAG_RE.match(tag), tag)

    def test_unknown_tool(self):
        with self.assertRaisesRegex(ReleaseError, "unknown tool"):
            release.resolve("nope-v1.0.0")

    def test_unreleased_head_refuses_to_resolve(self):
        # Every tool is Unreleased on this branch, so no tag may resolve yet.
        for name, tool in release.load_registry().items():
            version = release.read_version(tool)
            with self.assertRaisesRegex(ReleaseError, "top entry"):
                release.resolve("%s-v%s" % (name, version))

    def test_version_mismatch(self):
        with self.assertRaisesRegex(ReleaseError, "tag says 99.0.0"):
            release.resolve("tapefec-v99.0.0")


class ChangelogTest(unittest.TestCase):
    def test_parse(self):
        entries = release.parse_changelog(
            "# Changelog\n\n## Unreleased\n\n* x\n\n## 1.1.0 - 2026-10-01\n\n* y\n\n### Fixed\n\n* z\n\n"
            "## 1.0.0 (2026-09-01)\n\n* w\n")
        self.assertEqual([(v, d) for v, d, _ in entries],
                         [(None, None), ("1.1.0", "2026-10-01"), ("1.0.0", "2026-09-01")])
        self.assertEqual(entries[1][2], "* y\n\n### Fixed\n\n* z")

    def test_errors(self):
        cases = {
            "## 1.0.0\n": "heading must be",
            "## 1.0.0 (not yet tagged)\n": "heading must be",
            "## 1.0.0 - 2026-01-01\n## Unreleased\n": "must be the first",
            "## 1.0.0 - 2026-01-01\n## 1.1.0 - 2026-02-01\n": "must be lower",
            "## 1.0.0 - 2026-01-01\n## 1.0.0 - 2026-01-01\n": "must be lower",
            "## 1.0 - 2026-01-01\n": "semantic version",
            "# Changelog\n": "no '## ' entries",
        }
        for text, msg in cases.items():
            with self.assertRaisesRegex(ReleaseError, msg, msg=text):
                release.parse_changelog(text)

    def test_consistency(self):
        released = [("1.1.0", "2026-10-01", "* a"), ("1.0.0", "2026-09-01", "* b")]
        unreleased = [(None, None, "* c")] + released
        ok = release.check_consistency
        self.assertEqual(ok("t", "1.1.0", released, ["1.0.0", "1.1.0"]), [])
        self.assertEqual(ok("t", "1.1.0", unreleased, []), [])  # not bumped yet
        self.assertEqual(ok("t", "1.2.0", unreleased, []), [])  # bumped ahead
        self.assertRegex(ok("t", "1.2.0", released, [])[0], "top changelog entry is 1.1.0")
        self.assertRegex(ok("t", "1.0.0", unreleased, [])[0], "lower than the last released")
        self.assertRegex(ok("t", "1.1.0", released, ["1.3.0"])[0], "no dated entry for it")
        self.assertRegex(ok("t", "1.1.0", [(None, None, "")] + released, [])[0], "empty")


class RepoTest(unittest.TestCase):
    def test_registry_matches_repo(self):
        for name, tool in release.load_registry().items():
            self.assertTrue(os.path.isfile(os.path.join(release.REPO, tool["path"], "CHANGELOG.md")), name)
            release.read_version(tool)
            if tool["kind"] == "python":
                self.assertTrue(os.path.isfile(os.path.join(release.REPO, tool["path"], "pyproject.toml")))

    def test_check_passes(self):
        self.assertEqual(release.main(["check"]), 0)


def _git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_DATE="2026-10-01T00:00:00Z",
                            GIT_COMMITTER_DATE="2026-10-01T00:00:00Z", GIT_AUTHOR_NAME="t",
                            GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


class ArchiveTest(unittest.TestCase):
    """Bundles built from a scratch repository."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "tool", "sub"))
        for rel, body in (("a.txt", "a\n"), ("sub/b.sh", "#!/bin/sh\n"), ("untracked.txt", "x")):
            with open(os.path.join(self.root, "tool", rel), "w") as fh:
                fh.write(body)
        os.chmod(os.path.join(self.root, "tool", "sub", "b.sh"), 0o755)
        _git(self.root, "init", "-q")
        _git(self.root, "add", "tool/a.txt", "tool/sub/b.sh")
        _git(self.root, "commit", "-qm", "x")
        self.addCleanup(self.tmp.cleanup)
        os.environ.pop("SOURCE_DATE_EPOCH", None)

    def build(self, tool):
        out = tempfile.mkdtemp(dir=self.root)
        paths = release.write_archives("tool", "1.0.0", tool, out, self.root)
        return paths, [open(p, "rb").read() for p in paths]

    def test_source_bundle_is_tracked_files_and_reproducible(self):
        tool = {"path": "tool", "kind": "source"}
        (tar_path, zip_path), first = self.build(tool)
        _paths, second = self.build(tool)
        self.assertEqual(first, second)
        with tarfile.open(tar_path) as t:
            members = {m.name: m for m in t.getmembers()}
        self.assertEqual(sorted(members), ["tool-1.0.0/a.txt", "tool-1.0.0/sub/b.sh"])
        self.assertEqual(members["tool-1.0.0/sub/b.sh"].mode, 0o755)
        self.assertEqual(members["tool-1.0.0/a.txt"].mtime, 1790812800)  # 2026-10-01T00:00:00Z, the commit time
        with zipfile.ZipFile(zip_path) as z:
            self.assertEqual(z.namelist(), ["tool-1.0.0/a.txt", "tool-1.0.0/sub/b.sh"])

    def test_static_bundle_lists_only_named_files(self):
        (tar_path, _zip), _ = self.build({"path": "tool", "kind": "static", "files": ["a.txt"]})
        with tarfile.open(tar_path) as t:
            self.assertEqual(t.getnames(), ["tool-1.0.0/a.txt"])
        with self.assertRaisesRegex(ReleaseError, "not tracked"):
            self.build({"path": "tool", "kind": "static", "files": ["untracked.txt"]})

    def test_dirty_tree_refused(self):
        with open(os.path.join(self.root, "tool", "a.txt"), "w") as fh:
            fh.write("changed\n")
        with self.assertRaisesRegex(ReleaseError, "uncommitted"):
            self.build({"path": "tool", "kind": "source"})


@unittest.skipUnless(importlib.util.find_spec("build") and importlib.util.find_spec("setuptools"),
                     "needs the release toolchain (.github/release/requirements.txt)")
class PythonPrereleaseBuildTest(unittest.TestCase):
    """A real sdist + wheel build of a SemVer pre-release."""

    def test_rc_builds_with_pep440_names_and_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            tool_dir = os.path.join(root, "pkg")
            os.makedirs(tool_dir)
            files = {
                "pyproject.toml": '[build-system]\nrequires = ["setuptools>=77"]\n'
                                  'build-backend = "setuptools.build_meta"\n\n[project]\n'
                                  'name = "scratch-pkg"\ndynamic = ["version"]\n\n[tool.setuptools]\n'
                                  'py-modules = ["scratch"]\n\n[tool.setuptools.dynamic]\n'
                                  'version = {attr = "scratch.__version__"}\n',
                "scratch.py": '__version__ = "1.0.0-rc.1"\n',
            }
            for rel, body in files.items():
                with open(os.path.join(tool_dir, rel), "w") as fh:
                    fh.write(body)
            _git(root, "init", "-q")
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "x")
            tool = {"path": "pkg", "kind": "python",
                    "version": {"file": "scratch.py", "regex": '^__version__ = "([^"]+)"$'},
                    "smoke": [["python", "-c", "import scratch, importlib.metadata as m; "
                                         "assert m.version('scratch-pkg') == '1.0.0rc1'"]]}
            version = release.read_version(tool, root)
            out = os.path.join(root, "out")
            os.makedirs(out)
            paths = release.build_python("scratch-pkg", version, tool, out, root)
            self.assertEqual(sorted(os.path.basename(p) for p in paths),
                             ["scratch_pkg-1.0.0rc1-py3-none-any.whl", "scratch_pkg-1.0.0rc1.tar.gz"])


if __name__ == "__main__":
    unittest.main()
