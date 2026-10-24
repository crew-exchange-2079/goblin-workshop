import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tailnet_policy_tester.policy import parse_policy  # noqa: E402

EXAMPLE = ROOT / "examples" / "policy.hujson"
INVENTORY = ROOT / "examples" / "inventory.hujson"


def mk(**sections):
    """Build a policy from sections, with a default tagOwners for common tags."""
    data = {"tagOwners": {t: ["admin@example.com"] for t in
                          ("tag:web", "tag:db", "tag:ci", "tag:prod", "tag:router")}}
    data.update(sections)
    return parse_policy(json.loads(json.dumps(data)))


@pytest.fixture
def example_text():
    return EXAMPLE.read_text()
