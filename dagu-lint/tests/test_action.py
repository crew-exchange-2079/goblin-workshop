"""Static checks on action.yml; CI also runs the action itself (ci.yml, dagu-lint-action)."""

import os
import re
import shutil
import subprocess

import pytest
import yaml

from conftest import FIXTURES, POLICY, ROOT

with open(os.path.join(ROOT, "action.yml"), encoding="utf-8") as fh:
    ACTION = yaml.safe_load(fh)
STEPS = ACTION["runs"]["steps"]


def test_composite_with_pinned_uses():
    assert ACTION["runs"]["using"] == "composite"
    for step in STEPS:
        if "uses" in step:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"]), step["uses"]


def test_inputs_never_expanded_inside_run():
    for step in STEPS:
        if "run" in step:
            assert "${{" not in step["run"], step.get("name")


def test_python_does_not_leak_into_caller_env():
    setup = next(s for s in STEPS if "setup-python" in s.get("uses", ""))
    assert setup["with"]["update-environment"] is False


def test_requirements_are_hash_pinned():
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as fh:
        text = fh.read()
    assert "--hash=sha256:" in text and "==" in text


@pytest.mark.skipif(not shutil.which("bash"), reason="needs bash")
def test_run_script_argument_handling(tmp_path):
    """Execute the action's run script with a stub interpreter and check the argv it builds."""
    script = next(s for s in STEPS if s.get("name") == "Run dagu-lint")["run"]
    venv_bin = tmp_path / "dagu-lint-venv" / "bin"
    venv_bin.mkdir(parents=True)
    stub = venv_bin / "python"
    stub.write_text('#!/bin/sh\nfor a in "$@"; do printf "<%s>" "$a"; done\n')
    stub.chmod(0o755)
    env = dict(os.environ, RUNNER_TEMP=str(tmp_path), GITHUB_ACTION_PATH=ROOT,
               DL_PATHS="  dags/a b.yaml\n\n$(touch pwned)\n", DL_CONFIG=POLICY,
               DL_QUEUES="etl, reports\nml-gpu", DL_FORMAT="github", DL_STRICT="true")
    out = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    assert out.stdout == ("<-m><dagu_lint><--format><github><--strict><--config><" + POLICY + ">"
                          "<--queue><etl><--queue><reports><--queue><ml-gpu><--><dags/a b.yaml><$(touch pwned)>")
    assert not (tmp_path / "pwned").exists()
    env["DL_STRICT"] = "yes"
    assert subprocess.run(["bash", "-c", script], env=env, capture_output=True, cwd=tmp_path).returncode == 2
    env["DL_STRICT"], env["DL_PATHS"] = "false", " \n"
    assert subprocess.run(["bash", "-c", script], env=env, capture_output=True, cwd=tmp_path).returncode == 2


def test_fixture_policy_exists():
    assert os.path.isfile(os.path.join(FIXTURES, "dagu-lint.toml"))
