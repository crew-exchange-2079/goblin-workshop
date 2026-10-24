"""Regenerate quadlet_lint/keydb.json from Podman's quadlet source.

Usage: python tools/extract_keys.py SRC_DIR > quadlet_lint/keydb.json

SRC_DIR holds one file per release named q-<version>.go, each a copy of
pkg/systemd/quadlet/quadlet.go at tag v<version> of containers/podman, e.g.

    curl -fsSo q-5.2.0.go \
      https://raw.githubusercontent.com/containers/podman/v5.2.0/pkg/systemd/quadlet/quadlet.go

The script reads the supported-key tables quadlet itself uses to reject
unknown keys, so the output is exactly what each release accepts. Two layouts
exist upstream: `supportedContainerKeys = map[string]bool{...}` (4.4-5.5)
and `groupsInfo = {ContainerGroup: {SupportedKeys: map[string]bool{...}}}`
(5.6+); both are handled. Not part of the installed package.
"""

import json
import re
import sys
from pathlib import Path

CONST_RE = re.compile(r'^\s*(Key\w+|\w+Group)\s*=\s*"([^"]+)"', re.M)
DEPRECATED_RE = re.compile(r'^\s*Key\w+\s*=\s*"([^"]+)"\s*//\s*deprecated', re.M | re.I)
OLD_BLOCK_RE = re.compile(r"supported(\w+?)Keys\s*=\s*map\[string\]bool\{(.*?)\n\t\}", re.S)
NEW_BLOCK_RE = re.compile(r"\t\t(\w+Group):\s*\{.*?SupportedKeys:\s*map\[string\]bool\{(.*?)\n\t\t\t\}", re.S)
ENTRY_RE = re.compile(r"(Key\w+)\s*:\s*true")


def version_key(v):
    return tuple(int(p) for p in v.split("."))


def extract(text):
    consts = dict(CONST_RE.findall(text))
    sections = {}
    for name, body in OLD_BLOCK_RE.findall(text):
        sections[name] = body
    for group, body in NEW_BLOCK_RE.findall(text):
        sections[consts[group]] = body
    out = {}
    for section, body in sections.items():
        out[section] = sorted(consts[k] for k in ENTRY_RE.findall(body))
    return out


def main(argv):
    src = Path(argv[1])
    files = sorted(src.glob("q-*.go"), key=lambda p: version_key(p.stem[2:]))
    versions = [p.stem[2:] for p in files]
    per_version = {v: extract(p.read_text()) for v, p in zip(versions, files)}

    # Deprecation is only marked by a comment on the constant; take the newest release's.
    deprecated = set(DEPRECATED_RE.findall(files[-1].read_text()))
    db = {}
    for section in sorted({s for t in per_version.values() for s in t}):
        keys = {}
        for key in sorted({k for t in per_version.values() for k in t.get(section, [])}):
            present = [v for v in versions if key in per_version[v].get(section, [])]
            first = present[0]
            # A key that disappears and stays gone gets a "removed" version.
            last = present[-1]
            removed = versions[versions.index(last) + 1] if last != versions[-1] else None
            gaps = [v for v in versions[versions.index(first):versions.index(last) + 1] if v not in present]
            if gaps:
                raise SystemExit(f"{section}.{key}: missing in {gaps}, not handled")
            keys[key] = {"added": first, "removed": removed, "deprecated": key in deprecated}
        db[section] = keys
    json.dump({"source": "containers/podman pkg/systemd/quadlet/quadlet.go",
               "versions": versions, "sections": db}, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv)
