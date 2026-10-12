"""Check built distributions: the wheel installs only lto_exporter (no
offline parser, no tests), has the console entry point and the right
version; the sdist carries what's needed to run the test suite.

Usage: python3 ci/check_package.py dist/
"""

import glob
import os
import re
import sys
import tarfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def version():
    with open(os.path.join(ROOT, "lto_exporter", "__init__.py")) as fh:
        return re.search(r'__version__ = "([^"]+)"', fh.read()).group(1)


def main(dist):
    errors = []
    ver = version()
    wheels = glob.glob(os.path.join(dist, f"lto_exporter-{ver}-py3-none-any.whl"))
    sdists = glob.glob(os.path.join(dist, f"lto_exporter-{ver}.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        return print(f"expected one wheel and one sdist for {ver}, got {wheels} {sdists}") or 1
    with zipfile.ZipFile(wheels[0]) as z:
        names = z.namelist()
        top = {n.split("/")[0] for n in names}
        if top != {"lto_exporter", f"lto_exporter-{ver}.dist-info"}:
            errors.append(f"wheel top-level entries: {sorted(top)}")
        eps = z.read(f"lto_exporter-{ver}.dist-info/entry_points.txt").decode()
        if "lto-exporter = lto_exporter.exporter:main" not in eps:
            errors.append(f"console script missing: {eps!r}")
        meta = z.read(f"lto_exporter-{ver}.dist-info/METADATA").decode()
        if f"\nVersion: {ver}\n" not in meta or "Requires-Python: >=3.10" not in meta:
            errors.append("METADATA version/requires-python mismatch")
        if re.search(r"^Requires-Dist:", meta, re.M):
            errors.append("unexpected runtime dependencies")
    with tarfile.open(sdists[0]) as t:
        names = {n.split("/", 1)[1] for n in t.getnames() if "/" in n}
        for need in ("tests/test_exporter.py", "tests/fixtures/sg_logs_write_ok.txt",
                     "tests/fixtures/sg3_utils_1.46/write_page.txt", "offline/tapeinfo_text.py",
                     "ci/check_monitoring.py", "monitoring/prometheus/lto-exporter-alerts.yml",
                     "monitoring/grafana/lto-exporter-dashboard.json",
                     "contrib/systemd/lto-exporter.service", "LICENSE", "SECURITY.md",
                     "CHANGELOG.md", "example-config.json"):
            if need not in names:
                errors.append(f"sdist missing {need}")
        if any("__pycache__" in n or n.endswith(".pyc") for n in names):
            errors.append("sdist contains bytecode")
    for e in errors:
        print(f"ERROR: {e}")
    print(f"package check {ver}: {'FAIL' if errors else 'OK'}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "dist"))
