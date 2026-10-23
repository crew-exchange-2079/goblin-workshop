#!/usr/bin/env python3
"""Per-tool release helper for goblin-workshop. Standard library only.

Every releasable tool is listed in tools.json next to this file. Each one has
its own semantic version (read from its source) and its own CHANGELOG.md, and
is released by pushing a tag ``<tool>-v<version>``. See RELEASING.md.

    release.py check [--base REF] [--skip-changelog]
        Version and changelog consistency for every tool. With --base, also
        require that every tool whose files changed since REF updated its
        CHANGELOG.md, and that no version went backwards.
    release.py resolve TAG [--github-output FILE]
        Map a release tag to its tool and refuse it unless the tool's source
        version and top changelog entry both match the tag.
    release.py notes TOOL VERSION
        Print the changelog entry for VERSION (the release notes).
    release.py build TOOL OUTDIR
        Build the tool's distributions into OUTDIR, smoke-test them, and
        write SHA256SUMS.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import venv
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
REGISTRY = os.path.join(HERE, "tools.json")

# semver.org 2.0.0, without build metadata (tags carry no "+build" part).
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?$")
TAG_RE = re.compile(r"^(?P<tool>[a-z0-9][a-z0-9-]*?)-v(?P<version>\d[0-9A-Za-z.-]*)$")
UNRELEASED_RE = re.compile(r"^## Unreleased\s*$")
# "## 1.2.3 - 2026-10-23" or "## 1.2.3 (2026-10-23)".
RELEASED_RE = re.compile(r"^## (?P<version>\S+) (?:- (?P<d1>\d{4}-\d{2}-\d{2})|\((?P<d2>\d{4}-\d{2}-\d{2})\))\s*$")


class ReleaseError(Exception):
    pass


# ------------------------------------------------------------------ semver ---

def parse_semver(text):
    m = SEMVER_RE.match(text)
    if not m:
        raise ReleaseError("not a semantic version (MAJOR.MINOR.PATCH[-PRERELEASE]): %r" % text)
    pre = m.group(4)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), tuple(pre.split(".")) if pre else ())


def _pre_key(pre):
    # A release sorts after all of its pre-releases; numeric identifiers sort
    # numerically and before alphanumeric ones (semver.org section 11).
    if not pre:
        return (1,)
    return (0,) + tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre)


def semver_key(text):
    major, minor, patch, pre = parse_semver(text)
    return (major, minor, patch, _pre_key(pre))


def is_prerelease(text):
    return bool(parse_semver(text)[3])


# ---------------------------------------------------------------- registry ---

def load_registry(path=REGISTRY):
    with open(path, encoding="utf-8") as fh:
        tools = json.load(fh)["tools"]
    for name, tool in tools.items():
        if not re.match(r"^[a-z0-9][a-z0-9-]*$", name):
            raise ReleaseError("bad tool name %r" % name)
        if tool["kind"] not in ("python", "source", "static"):
            raise ReleaseError("%s: unknown kind %r" % (name, tool["kind"]))
    return tools


def read_version(tool, root=REPO):
    spec = tool["version"]
    path = os.path.join(root, tool["path"], spec["file"])
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if "json" in spec:
        version = json.loads(text)[spec["json"]]
    else:
        found = re.findall(spec["regex"], text, re.M)
        if len(found) != 1:
            raise ReleaseError("%s: expected one version match, found %d" % (spec["file"], len(found)))
        version = found[0]
    parse_semver(version)
    return version


# --------------------------------------------------------------- changelog ---

def parse_changelog(text):
    """Return [(version or None for Unreleased, date or None, body)], top first."""
    entries = []
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        if not line.startswith("## "):
            if entries:
                entries[-1][2].append(line)
            continue
        if UNRELEASED_RE.match(line):
            if entries:
                raise ReleaseError("line %d: '## Unreleased' must be the first entry" % i)
            entries.append([None, None, []])
            continue
        m = RELEASED_RE.match(line)
        if not m:
            raise ReleaseError("line %d: entry heading must be '## Unreleased' or "
                               "'## X.Y.Z - YYYY-MM-DD', got %r" % (i, line))
        version = m.group("version")
        try:
            key = semver_key(version)
        except ReleaseError as exc:
            raise ReleaseError("line %d: %s" % (i, exc))
        prev = [e for e in entries if e[0] is not None]
        if prev and semver_key(prev[-1][0]) <= key:
            raise ReleaseError("line %d: %s must be lower than the entry above it (%s)"
                               % (i, version, prev[-1][0]))
        entries.append([version, m.group("d1") or m.group("d2"), []])
    if not entries:
        raise ReleaseError("no '## ' entries")
    return [(v, d, "\n".join(body).strip("\n")) for v, d, body in entries]


def read_changelog(tool, root=REPO):
    path = os.path.join(root, tool["path"], "CHANGELOG.md")
    if not os.path.isfile(path):
        raise ReleaseError("%s/CHANGELOG.md is missing" % tool["path"])
    with open(path, encoding="utf-8") as fh:
        return parse_changelog(fh.read())


def check_consistency(name, version, entries, tagged):
    """Problems with one tool's version against its changelog and its tags."""
    problems = []
    released = [v for v, _d, _b in entries if v is not None]
    top = entries[0][0]
    if top is not None and version != top:
        problems.append("source version %s but the top changelog entry is %s "
                        "(add '## Unreleased' above it, or date an entry for %s)" % (version, top, version))
    if top is None and released and semver_key(version) < semver_key(released[0]):
        problems.append("source version %s is lower than the last released entry %s" % (version, released[0]))
    for tag_version in tagged:
        if tag_version not in released:
            problems.append("tag for %s exists but CHANGELOG.md has no dated entry for it" % tag_version)
        if semver_key(version) < semver_key(tag_version):
            problems.append("source version %s is lower than tagged %s" % (version, tag_version))
    if top is None and not entries[0][2].strip():
        problems.append("'## Unreleased' entry is empty")
    return ["%s: %s" % (name, p) for p in problems]


# --------------------------------------------------------------------- git ---

def git(*args, root=REPO):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def tagged_versions(name, tool, root=REPO):
    versions = []
    prefixes = [name + "-v"] + ([tool["legacy_tag_prefix"]] if tool.get("legacy_tag_prefix") else [])
    for tag in git("tag", "--list", root=root).split():
        for prefix in prefixes:
            if tag.startswith(prefix) and SEMVER_RE.match(tag[len(prefix):]):
                versions.append(tag[len(prefix):])
    return versions


def changed_files(base, root=REPO):
    return git("diff", "--name-only", "-z", "%s...HEAD" % base, root=root).split("\0")


def file_at(ref, path, root=REPO):
    try:
        return git("show", "%s:%s" % (ref, path), root=root)
    except subprocess.CalledProcessError:
        return None


# ------------------------------------------------------------------ check ---

def cmd_check(args, root=REPO):
    tools = load_registry()
    problems = []
    for name, tool in tools.items():
        try:
            version = read_version(tool, root)
            entries = read_changelog(tool, root)
        except (ReleaseError, OSError, ValueError, KeyError) as exc:
            problems.append("%s: %s" % (name, exc))
            continue
        tagged = tagged_versions(name, tool, root)
        problems += check_consistency(name, version, entries, tagged)
        if args.base:
            problems += check_against_base(name, tool, version, entries, tagged, args, root)
        print("%-24s %-12s %s" % (name, version, "Unreleased" if entries[0][0] is None
                                  else "released %s (%s)" % (entries[0][0], entries[0][1])))
    for p in problems:
        print("ERROR: " + p, file=sys.stderr)
    print("release check: %s" % ("FAIL" if problems else "OK"))
    return 1 if problems else 0


def check_against_base(name, tool, version, entries, tagged, args, root):
    problems = []
    prefix = tool["path"].rstrip("/") + "/"
    changed = [f for f in changed_files(args.base, root) if f.startswith(prefix)]
    if not changed:
        return problems
    base_text = file_at(args.base, prefix + tool["version"]["file"], root)
    if base_text is not None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, tool["path"], tool["version"]["file"])
            os.makedirs(os.path.dirname(dest))
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(base_text)
            try:
                base_version = read_version(tool, tmp)
            except (ReleaseError, ValueError, KeyError):
                base_version = None
        if base_version and semver_key(version) < semver_key(base_version):
            problems.append("version went backwards: %s -> %s" % (base_version, version))
    if args.skip_changelog:
        return problems
    if prefix + "CHANGELOG.md" not in changed:
        problems.append("%d file(s) changed under %s but %sCHANGELOG.md did not; add a line under "
                        "'## Unreleased' (or apply the skip-changelog label)" % (len(changed), prefix, prefix))
    elif entries[0][0] is not None and entries[0][0] in tagged:
        problems.append("the top changelog entry %s is already tagged; put new changes under "
                        "'## Unreleased'" % entries[0][0])
    return problems


# ---------------------------------------------------------- resolve/notes ---

def resolve(tag, root=REPO):
    m = TAG_RE.match(tag)
    if not m:
        raise ReleaseError("tag %r is not <tool>-v<semver>" % tag)
    tools = load_registry()
    name, version = m.group("tool"), m.group("version")
    if name not in tools:
        raise ReleaseError("tag %r names unknown tool %r (known: %s)" % (tag, name, ", ".join(tools)))
    parse_semver(version)
    tool = tools[name]
    source = read_version(tool, root)
    if source != version:
        raise ReleaseError("tag says %s but %s/%s says %s" % (version, tool["path"], tool["version"]["file"], source))
    entries = read_changelog(tool, root)
    if entries[0][0] != version:
        raise ReleaseError("the top entry of %s/CHANGELOG.md must be '## %s - YYYY-MM-DD', got %s"
                           % (tool["path"], version, entries[0][0] or "'## Unreleased'"))
    if not entries[0][2].strip():
        raise ReleaseError("changelog entry for %s is empty" % version)
    return {"tool": name, "version": version, "kind": tool["kind"], "path": tool["path"],
            "prerelease": "true" if is_prerelease(version) else "false"}


def cmd_resolve(args):
    info = resolve(args.tag)
    for k, v in info.items():
        print("%s=%s" % (k, v))
    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            for k, v in info.items():
                fh.write("%s=%s\n" % (k, v))
    return 0


def release_notes(name, version, root=REPO):
    tools = load_registry()
    for v, date, body in read_changelog(tools[name], root):
        if v == version:
            return "Released %s. Changelog: `%s/CHANGELOG.md`.\n\n%s\n" % (date, tools[name]["path"], body)
    raise ReleaseError("%s: no changelog entry for %s" % (name, version))


def cmd_notes(args):
    sys.stdout.write(release_notes(args.tool, args.version))
    return 0


# ------------------------------------------------------------------ build ---

def source_date_epoch(root=REPO):
    if os.environ.get("SOURCE_DATE_EPOCH"):
        return int(os.environ["SOURCE_DATE_EPOCH"])
    return int(git("log", "-1", "--format=%ct", root=root).strip())


def tracked_files(tool, root=REPO):
    """Files of the tool tracked by git at HEAD, relative to the tool directory.

    Refuses a dirty tool directory: a release bundle must equal the tagged tree.
    """
    path = tool["path"]
    dirty = git("status", "--porcelain", "--untracked-files=no", "--", path, root=root)
    if dirty.strip():
        raise ReleaseError("uncommitted changes under %s/:\n%s" % (path, dirty))
    files = [f[len(path) + 1:] for f in git("ls-files", "-z", "--", path, root=root).split("\0") if f]
    if tool["kind"] == "static":
        missing = sorted(set(tool["files"]) - set(files))
        if missing:
            raise ReleaseError("static files not tracked: %s" % ", ".join(missing))
        files = list(tool["files"])
    return sorted(files)


def write_archives(name, version, tool, outdir, root=REPO):
    """Deterministic .tar.gz and .zip: sorted entries, fixed owner, mode and mtime."""
    files = tracked_files(tool, root)
    epoch = source_date_epoch(root)
    stem = "%s-%s" % (name, version)
    tar_path = os.path.join(outdir, stem + ".tar.gz")
    zip_path = os.path.join(outdir, stem + ".zip")
    date_time = time.gmtime(max(epoch, 315532800))[:6]  # zip cannot store dates before 1980
    with open(tar_path, "wb") as raw, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch, compresslevel=9) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar, \
            zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in files:
            src = os.path.join(root, tool["path"], rel)
            if os.path.islink(src) or not os.path.isfile(src):
                raise ReleaseError("%s/%s is not a regular file" % (tool["path"], rel))
            with open(src, "rb") as fh:
                data = fh.read()
            mode = 0o755 if os.stat(src).st_mode & 0o111 else 0o644
            info = tarfile.TarInfo("%s/%s" % (stem, rel))
            info.size, info.mode, info.mtime = len(data), mode, epoch
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(data))
            zinfo = zipfile.ZipInfo("%s/%s" % (stem, rel), date_time)
            zinfo.external_attr = (0o100000 | mode) << 16
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(zinfo, data)
    return [tar_path, zip_path]


def build_python(name, version, tool, outdir, root=REPO):
    src = os.path.join(root, tool["path"])
    files = tracked_files(tool, root)
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(source_date_epoch(root)), PYTHONHASHSEED="0")
    with tempfile.TemporaryDirectory() as tmp:
        # Build from a copy of the tracked files only, so untracked local files
        # never reach a distribution and build/ never lands in the checkout.
        work = os.path.join(tmp, "src")
        for rel in files:
            os.makedirs(os.path.dirname(os.path.join(work, rel)), exist_ok=True)
            shutil.copy2(os.path.join(src, rel), os.path.join(work, rel))
        out = os.path.join(tmp, "dist")
        subprocess.run([sys.executable, "-m", "build", "--no-isolation", "--sdist", "--wheel",
                        "--outdir", out, work], check=True, env=env, stdout=sys.stderr)
        built = sorted(os.listdir(out))
        norm = re.sub(r"[-_.]+", "_", project_name(work)).lower()
        want = ["%s-%s-py3-none-any.whl" % (norm, version), "%s-%s.tar.gz" % (norm, version)]
        if sorted(built) != sorted(want):
            raise ReleaseError("expected %s, built %s" % (want, built))
        paths = []
        for f in built:
            dest = os.path.join(outdir, f)
            if f.endswith(".tar.gz"):
                normalize_tar_gz(os.path.join(out, f), dest, int(env["SOURCE_DATE_EPOCH"]))
            else:
                shutil.copy2(os.path.join(out, f), dest)
            paths.append(dest)
    smoke_python(tool, [p for p in paths if p.endswith(".whl")][0])
    return paths


def normalize_tar_gz(src, dest, epoch):
    """Repack a setuptools sdist with sorted entries and fixed mtime/owner.

    setuptools honours SOURCE_DATE_EPOCH for wheels but not for the sdist's
    member mtimes or gzip header, so the sdist is rewritten (same members,
    same bytes) to make it reproducible as well.
    """
    with tarfile.open(src, "r:gz") as tin:
        members = sorted(tin.getmembers(), key=lambda m: m.name)
        with open(dest, "wb") as raw, \
                gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch, compresslevel=9) as gz, \
                tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tout:
            for m in members:
                if not (m.isfile() or m.isdir()):
                    raise ReleaseError("%s: unexpected sdist member %s" % (src, m.name))
                m.mtime, m.uid, m.gid, m.uname, m.gname = epoch, 0, 0, "", ""
                m.pax_headers = {}
                tout.addfile(m, tin.extractfile(m) if m.isfile() else None)


def project_name(src):
    with open(os.path.join(src, "pyproject.toml"), encoding="utf-8") as fh:
        m = re.search(r'^name = "([^"]+)"$', fh.read(), re.M)
    if not m:
        raise ReleaseError("no project name in pyproject.toml")
    return m.group(1)


def smoke_python(tool, wheel):
    with tempfile.TemporaryDirectory() as tmp:
        venv.EnvBuilder(with_pip=True).create(tmp)
        bindir = os.path.join(tmp, "bin")
        subprocess.run([os.path.join(bindir, "python"), "-m", "pip", "install", "-q",
                        "--disable-pip-version-check", "--no-deps", "--no-index", wheel],
                       check=True, stdout=sys.stderr)
        for argv in tool.get("smoke", []):
            subprocess.run([os.path.join(bindir, argv[0])] + argv[1:], check=True, cwd=tmp, stdout=sys.stderr)


def sha256sums(outdir):
    lines = []
    for f in sorted(os.listdir(outdir)):
        if f == "SHA256SUMS":
            continue
        with open(os.path.join(outdir, f), "rb") as fh:
            lines.append("%s  %s\n" % (hashlib.sha256(fh.read()).hexdigest(), f))
    with open(os.path.join(outdir, "SHA256SUMS"), "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    return "".join(lines)


def cmd_build(args, root=REPO):
    tools = load_registry()
    if args.tool not in tools:
        raise ReleaseError("unknown tool %r" % args.tool)
    tool = tools[args.tool]
    version = read_version(tool, root)
    os.makedirs(args.outdir, exist_ok=True)
    if os.listdir(args.outdir):
        raise ReleaseError("%s is not empty" % args.outdir)
    if tool["kind"] == "python":
        build_python(args.tool, version, tool, args.outdir, root)
    else:
        write_archives(args.tool, version, tool, args.outdir, root)
    sys.stdout.write(sha256sums(args.outdir))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--base", help="git ref the change is compared against (PR base)")
    c.add_argument("--skip-changelog", action="store_true", help="do not require CHANGELOG.md updates")
    r = sub.add_parser("resolve")
    r.add_argument("tag")
    r.add_argument("--github-output")
    n = sub.add_parser("notes")
    n.add_argument("tool")
    n.add_argument("version")
    b = sub.add_parser("build")
    b.add_argument("tool")
    b.add_argument("outdir")
    args = p.parse_args(argv)
    try:
        return {"check": cmd_check, "resolve": cmd_resolve, "notes": cmd_notes, "build": cmd_build}[args.cmd](args)
    except ReleaseError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
