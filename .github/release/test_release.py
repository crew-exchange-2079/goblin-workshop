"""Tests for release.py. Run from the repo root: python -m pytest -q .github/release"""

import os
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


if __name__ == "__main__":
    unittest.main()
