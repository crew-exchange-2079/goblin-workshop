from quadlet_lint.cli import lint_text
from quadlet_lint.keydb import KeyDB, parse_version

DB = KeyDB.load()
DIGEST = "4f9c2b6e1a7d3c8b5e0f9a2d6c4b1e8f7a3d5c9b2e6f1a4d8c7b3e5f0a9d2c6b"
PINNED = f"docker.io/library/nginx@sha256:{DIGEST}"


def lint(text, kind="container", version=None, select=None):
    target = parse_version(version) if version else DB.newest
    return lint_text(text, f"unit.{kind}", target, DB, select)


def ids(text, kind="container", version=None):
    return sorted({f.rule for f in lint(text, kind, version)})


def container(extra="", service="Restart=on-failure\nRestartSec=5s", image=PINNED):
    """A unit that is clean apart from `extra` / `service`."""
    return (f"[Container]\nImage={image}\nReadOnly=true\nHealthCmd=/bin/true\n{extra}\n\n"
            f"[Service]\n{service}\n")
