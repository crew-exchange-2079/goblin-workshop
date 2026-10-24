"""Keep docs/rules.md, the pre-commit hook and the Action in step with the code."""

import re
from pathlib import Path

from quadlet_lint.rules import RULES

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent


def test_every_rule_documented_with_matching_heading():
    doc = (ROOT / "docs" / "rules.md").read_text()
    headings = re.findall(r"^### (QL\d{3}) `([a-z0-9-]+)`", doc, re.M)
    assert [h[0] for h in headings] == list(RULES), "rules.md order or ids drifted"
    for rule_id, name in headings:
        assert RULES[rule_id].name == name, rule_id
        section = doc.split(f"### {rule_id} ", 1)[1].split("\n### ", 1)[0]
        assert f"Severity: **{RULES[rule_id].severity}**" in section, rule_id


def test_pre_commit_hook_points_at_wrapper():
    hooks = (REPO / ".pre-commit-hooks.yaml").read_text()
    assert "id: quadlet-lint" in hooks
    entry = re.search(r"entry:\s*(\S+)", hooks).group(1)
    assert (REPO / entry).is_file()
    for ext in ("container", "volume", "network", "pod"):
        assert ext in hooks


def test_action_runs_wrapper_with_env_inputs():
    action = (ROOT / "action.yml").read_text()
    assert "bin/quadlet-lint" in action
    # Inputs reach the shell through env vars, never ${{ }} inside run: scripts.
    for block in re.findall(r"run: \|\n((?:\s{8,}.*\n)+)", action):
        assert "${{" not in block
