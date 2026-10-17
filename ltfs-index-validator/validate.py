#!/usr/bin/env python3
"""Offline LTFS 2.4.0 index XML *schema* checker.

Schema success is NOT LTFS compliance: UID uniqueness, extent ordering/overlap/
coverage, payload checksums, tape layout and recovery are never checked.
"""
import argparse
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

try:  # a missing dependency is reported as ERROR, never as success
    from lxml import etree
except ImportError:  # pragma: no cover - exercised by tests via monkeypatch
    etree = None

TOOL_VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
SCHEMA_PATH = HERE / "schema" / "ltfs-index-2.4.0.xsd"
SCHEMA_SHA256 = "c9c72686e628834bcdd321ffa96f5d6a8debbf2388448b0ca114a567f5fa7b65"
SUPPORTED_VERSION = "2.4.0"

MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 64
MAX_NODES = 100000
MAX_REPORT_BYTES = 64 * 1024
MAX_DIAGNOSTICS = 50

SCHEMA_VALID_ONLY, INVALID, UNSUPPORTED, ERROR = ("SCHEMA_VALID_ONLY", "INVALID",
                                                  "UNSUPPORTED", "ERROR")
EXIT = {SCHEMA_VALID_ONLY: 0, INVALID: 1, UNSUPPORTED: 2, ERROR: 3}

LIMITATIONS = [
    "Schema validity only; this is not LTFS compliance or certification.",
    "Not checked: UID uniqueness, extent ordering/overlap/coverage, payload "
    "checksums, tape layout, recovery, interoperability.",
]
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class Refusal(Exception):
    """Fixed-code refusal; carries no input-derived text."""

    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


class ResolverDenied(Exception):
    pass


if etree is not None:
    class DenyResolver(etree.Resolver):
        """Records and refuses every external resolution attempt."""

        def __init__(self):
            super().__init__()
            self.calls = 0

        def resolve(self, url, id, context):
            self.calls += 1
            raise ResolverDenied("external resolution denied")


def build_parser(resolver=None):
    parser = etree.XMLParser(
        recover=False, huge_tree=False, load_dtd=False, dtd_validation=False,
        no_network=True, resolve_entities=False, attribute_defaults=False,
        collect_ids=False, remove_pis=True, remove_comments=True)
    parser.resolvers.add(resolver if resolver is not None else DenyResolver())
    return parser


_O_PATH = getattr(os, "O_PATH", None)  # Linux-only; absent => input is refused
_ENC_RE = re.compile(rb"^(?:\xef\xbb\xbf)?\s*<\?xml[^>]*?encoding\s*=\s*[\"']([^\"']*)[\"']")


def read_input(path):
    """Read <=4MiB from a regular file without ever data-opening an untyped inode.

    Linux only: pin the target with an O_PATH|O_NOFOLLOW descriptor (opens no
    device driver), decide regular-file from fstat on that descriptor, then
    reopen the pinned inode through /proc/self/fd/N and verify its identity.
    Missing O_PATH or /proc => refusal, never an unsafe fallback.
    """
    if not isinstance(path, str) or not path or "\0" in path:
        raise Refusal(ERROR, "INPUT_PATH_REJECTED")
    if _URL_RE.match(path) and "://" in path:
        raise Refusal(ERROR, "INPUT_URL_REFUSED")
    if _O_PATH is None or not os.path.isdir("/proc/self/fd"):
        raise Refusal(ERROR, "INPUT_PLATFORM_UNSUPPORTED")
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        real = os.path.realpath(path)  # resolves //dev, parent symlinks, cwd aliases
    except (OSError, ValueError):
        raise Refusal(ERROR, "INPUT_OPEN_FAILED")
    if real == "/dev" or real.startswith("/dev/"):
        raise Refusal(ERROR, "INPUT_DEVICE_REFUSED")
    try:
        pfd = os.open(path, _O_PATH | os.O_NOFOLLOW | cloexec)
    except OSError:
        raise Refusal(ERROR, "INPUT_OPEN_FAILED")
    fd = None
    try:
        pst = os.fstat(pfd)
        if not stat.S_ISREG(pst.st_mode):
            raise Refusal(ERROR, "INPUT_NOT_REGULAR_FILE")
        try:
            fd = os.open("/proc/self/fd/%d" % pfd, os.O_RDONLY | os.O_NONBLOCK | cloexec)
        except OSError:
            raise Refusal(ERROR, "INPUT_OPEN_FAILED")
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (pst.st_dev, pst.st_ino):
            raise Refusal(ERROR, "INPUT_IDENTITY_MISMATCH")
        chunks, total = [], 0
        while total <= MAX_INPUT_BYTES:
            try:
                chunk = os.read(fd, min(65536, MAX_INPUT_BYTES + 1 - total))
            except OSError:
                raise Refusal(ERROR, "INPUT_READ_FAILED")
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(pfd)
    if len(data) > MAX_INPUT_BYTES:
        raise Refusal(ERROR, "INPUT_TOO_LARGE")
    return data


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


_loaded = {}


def load_schema(schema_path=None):
    """Return (XMLSchema, known element names, sha256); hash-verified, local only."""
    if etree is None:
        raise Refusal(ERROR, "DEPENDENCY_MISSING")
    path = Path(schema_path) if schema_path is not None else SCHEMA_PATH
    try:
        raw = path.read_bytes()
    except OSError:
        raise Refusal(ERROR, "SCHEMA_MISSING")
    digest = _sha256(raw)
    if digest != SCHEMA_SHA256:
        raise Refusal(ERROR, "SCHEMA_HASH_MISMATCH")
    if digest not in _loaded:
        try:
            doc = etree.fromstring(raw, build_parser())
            names = frozenset(
                e.get("name") for e in doc.iter("{http://www.w3.org/2001/XMLSchema}element")
                if e.get("name"))
            _loaded[digest] = (etree.XMLSchema(doc), names)
        except (etree.LxmlError, ResolverDenied):
            raise Refusal(ERROR, "SCHEMA_COMPILE_FAILED")
    schema, names = _loaded[digest]
    return schema, names, digest


def _walk_limits(root):
    nodes, stack = 0, [(root, 1)]
    while stack:
        el, depth = stack.pop()
        nodes += 1
        if depth > MAX_DEPTH:
            raise Refusal(INVALID, "LIMIT_DEPTH_EXCEEDED")
        if nodes > MAX_NODES:
            raise Refusal(INVALID, "LIMIT_NODES_EXCEEDED")
        stack.extend((c, depth + 1) for c in el)


def _safe_path(raw, names):
    """Keep only schema-known element names; everything else becomes '?'."""
    out = []
    for seg in (raw or "").split("/"):
        if not seg:
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)(\[\d+\])?$", seg)
        out.append((m.group(1) if m and m.group(1) in names else "?") + (m.group(2) or "") if m else "?")
    return "/" + "/".join(out)


def _code_for(type_name):
    n = type_name or ""
    if "ELEMENT_CONTENT" in n:
        return "SCHEMA_ELEMENT_CONTENT"
    if "ATTRIBUTE" in n:
        return "SCHEMA_ATTRIBUTE"
    if any(k in n for k in ("DATATYPE", "FACET", "PATTERN", "ENUMERATION", "VALUE")):
        return "SCHEMA_VALUE"
    return "SCHEMA_OTHER"


def check_document(data, schema_path=None, resolver=None):
    """Return (status, established_version, diagnostics, schema_sha256)."""
    schema, names, digest = load_schema(schema_path)
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refusal(UNSUPPORTED, "ENCODING_NOT_UTF8")
    if b"\0" in data:
        raise Refusal(UNSUPPORTED, "ENCODING_NOT_UTF8")
    m = _ENC_RE.match(data)
    if m and m.group(1).decode("ascii", "replace").strip().upper().replace("_", "-") not in ("UTF-8", "UTF8"):
        raise Refusal(UNSUPPORTED, "ENCODING_NOT_UTF8")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise Refusal(INVALID, "DTD_FORBIDDEN")
    try:
        root = etree.fromstring(data, build_parser(resolver))
    except ResolverDenied:
        raise Refusal(INVALID, "EXTERNAL_RESOLUTION_DENIED")
    except etree.XMLSyntaxError:
        raise Refusal(INVALID, "XML_MALFORMED")
    except etree.LxmlError:
        raise Refusal(INVALID, "XML_PARSE_FAILED")
    doc = root.getroottree()
    if (doc.docinfo.encoding or "UTF-8").upper() not in ("UTF-8", "UTF8"):
        raise Refusal(UNSUPPORTED, "ENCODING_NOT_UTF8")
    if doc.docinfo.doctype or doc.docinfo.internalDTD is not None:
        raise Refusal(INVALID, "DTD_FORBIDDEN")
    _walk_limits(root)
    for el in root.iter():
        if not isinstance(el.tag, str):  # entity reference or similar node
            raise Refusal(INVALID, "ENTITY_REFERENCE_FORBIDDEN")
        if el.tag.startswith("{"):
            raise Refusal(UNSUPPORTED, "NAMESPACE_UNSUPPORTED")
        if any(k.startswith("{") for k in el.attrib):
            raise Refusal(UNSUPPORTED, "NAMESPACED_ATTRIBUTE_UNSUPPORTED")
    if root.tag != "ltfsindex":
        raise Refusal(UNSUPPORTED, "ROOT_NOT_FULL_INDEX")
    version = root.get("version")
    if version is None:
        raise Refusal(INVALID, "VERSION_MISSING")
    if not _VERSION_RE.match(version):
        raise Refusal(INVALID, "VERSION_MALFORMED")
    if version != SUPPORTED_VERSION:
        raise Refusal(UNSUPPORTED, "VERSION_UNSUPPORTED")
    if schema.validate(root):
        return SCHEMA_VALID_ONLY, SUPPORTED_VERSION, [], digest
    diags = [{"code": _code_for(e.type_name), "line": e.line,
              "path": _safe_path(e.path, names)} for e in schema.error_log]
    if not diags:
        diags = [{"code": "SCHEMA_OTHER", "line": 0, "path": "/"}]
    return INVALID, SUPPORTED_VERSION, diags, digest


def build_report(status, code=None, data=None, version=None, diags=None, schema_sha=None):
    diags = list(diags or [])
    if code:
        diags = [{"code": code, "line": 0, "path": "/"}] + diags
    truncated = len(diags) > MAX_DIAGNOSTICS
    report = {
        "tool": "ltfs-index-validator", "tool_version": TOOL_VERSION,
        "status": status, "exit_code": EXIT[status],
        "input": {"sha256": _sha256(data) if data is not None else None,
                  "bytes": len(data) if data is not None else None},
        "schema": {"sha256": schema_sha, "pinned_sha256": SCHEMA_SHA256,
                   "ltfs_version": SUPPORTED_VERSION},
        "established_version": version,
        "semantic_checks": "NOT_PERFORMED",
        "limitations": LIMITATIONS,
        "diagnostics": diags[:MAX_DIAGNOSTICS], "diagnostics_truncated": truncated,
    }
    while len(json.dumps(report).encode()) > MAX_REPORT_BYTES and report["diagnostics"]:
        report["diagnostics"] = report["diagnostics"][: len(report["diagnostics"]) // 2]
        report["diagnostics_truncated"] = True
    return report


def validate_path(path, schema_path=None, resolver=None):
    data = None
    try:
        data = read_input(path)
        status, version, diags, digest = check_document(data, schema_path, resolver)
        return build_report(status, None, data, version, diags, digest)
    except Refusal as r:
        return build_report(r.status, r.code, data)
    except Exception:  # never leak raw exception text; never look like success
        return build_report(ERROR, "INTERNAL_ERROR", data)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refusal(ERROR, "USAGE_ERROR")


def main(argv=None):
    parser = _Parser(prog="validate.py", description=__doc__)
    parser.add_argument("--input", required=True, help="path to a regular index XML file")
    try:
        args = parser.parse_args(argv)
        report = validate_path(args.input)
    except Refusal as r:
        report = build_report(r.status, r.code)
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
