import json
import os

import pytest

from conftest import FIXTURES, NOW, POLICY

GOOD = os.path.join(FIXTURES, "good", "minimal.yaml")
WARN = os.path.join(FIXTURES, "bad", "misleading-step.yaml")
BAD = os.path.join(FIXTURES, "bad", "queue-typo.yaml")


def write(tmp_path, name, text, mode="w"):
    p = tmp_path / name
    if mode == "wb":
        p.write_bytes(text)
    else:
        p.write_text(text)
    return str(p)


def test_exit_codes(run_cli):
    assert run_cli("-c", POLICY, "--now", NOW, GOOD)[0] == 0
    assert run_cli("-c", POLICY, "--now", NOW, BAD)[0] == 1
    assert run_cli("-c", POLICY, "--now", NOW, WARN)[0] == 0
    assert run_cli("-c", POLICY, "--now", NOW, "--strict", WARN)[0] == 1


def test_text_output_shape(run_cli):
    _, out, err = run_cli("-c", POLICY, "--now", NOW, BAD)
    assert f"{BAD}:4:1: error DL101 queue 'reprots' is not in the allowlist; did you mean 'reports'?" in out
    assert "info schedule '0 2 * * *': at 02:00, every day (UTC); next: 2026-10-23T02:00:00+00:00" in out
    assert "1 file(s), 1 error(s), 0 warning(s)" in err


def test_json_output(run_cli):
    code, out, err = run_cli("-c", POLICY, "--format", "json", "--now", NOW, "--next-runs", "2", BAD)
    data = json.loads(out)
    f = data["files"][0]
    assert f["path"] == BAD
    assert f["findings"] == [{"code": "DL101", "severity": "error", "title": "queue not in allowlist",
                              "message": "queue 'reprots' is not in the allowlist; did you mean 'reports'?",
                              "line": 4, "column": 1, "document": 0}]
    assert f["schedules"][0]["next_runs"] == ["2026-10-23T02:00:00+00:00", "2026-10-24T02:00:00+00:00"]
    assert err == ""


def test_github_annotations(run_cli):
    _, out, _ = run_cli("-c", POLICY, "--format", "github", "--now", NOW, BAD, WARN)
    lines = out.splitlines()
    assert any(line.startswith(f"::error file={BAD},line=4,col=1,title=DL101 queue not in allowlist::") for line in lines)
    assert any(line.startswith("::warning ") and "DL203" in line for line in lines)
    assert any(line.startswith("::notice ") for line in lines)
    _, out, _ = run_cli("-c", POLICY, "--format", "github", "--strict", "--now", NOW, WARN)
    assert any(line.startswith("::error ") and "DL203" in line for line in out.splitlines())


def test_github_escaping(tmp_path, run_cli):
    p = write(tmp_path, "a,b:c.yaml", "name: x\nqueue: \"bad%queue\"\n")
    _, out, _ = run_cli("--format", "github", "-q", "default", "--disable", "DL301", "--disable", "DL501", p)
    assert "file=" + p.replace(":", "%3A").replace(",", "%2C") in out
    assert "bad%25queue" in out


def test_disable_flag_and_unknown_code(run_cli):
    assert run_cli("-c", POLICY, "--now", NOW, "--disable", "DL101", BAD)[0] == 0
    code, _, err = run_cli("-c", POLICY, "--disable", "DL999", BAD)
    assert code == 2 and "DL999" in err


def test_file_level_suppression(tmp_path, run_cli):
    with open(BAD) as fh:
        text = "# dagu-lint: disable-file=DL101\n" + fh.read()
    assert run_cli("-c", POLICY, "--now", NOW, write(tmp_path, "s.yaml", text))[0] == 0


def test_queue_flag_adds_to_config(run_cli):
    assert run_cli("-c", POLICY, "-q", "reprots", "--now", NOW, BAD)[0] == 0


def test_no_allowlist_warns_on_stderr(run_cli):
    code, out, err = run_cli("--now", NOW, BAD)
    assert code == 0 and "no queue allowlist configured" in err


def test_directory_discovery_and_exclude(tmp_path, run_cli):
    d = tmp_path / "dags"
    (d / "sub").mkdir(parents=True)
    (d / ".hidden").mkdir()
    with open(GOOD) as fh:
        good = fh.read()
    (d / "a.yaml").write_text(good)
    (d / "sub" / "b.yml").write_text(good)
    (d / "base.yaml").write_text("env: [A=1]\n")  # Dagu base config, not a DAG
    (d / ".hidden" / "c.yaml").write_text("broken: [")
    (d / "notes.txt").write_text("broken: [")
    code, out, err = run_cli("-c", POLICY, "--now", NOW, "--exclude", "base.yaml", str(d))
    assert code == 0 and "2 file(s)" in err


def test_no_files(tmp_path, run_cli):
    code, _, err = run_cli(str(tmp_path))
    assert code == 2 and "no DAG files" in err


def test_unreadable_inputs(tmp_path, run_cli):
    big = write(tmp_path, "big.yaml", "a: " + "x" * (1 << 20) + "\n")
    latin = write(tmp_path, "latin.yaml", b"name: caf\xe9\n", mode="wb")
    missing = str(tmp_path / "missing.yaml")
    code, out, _ = run_cli("--format", "json", big, latin, missing)
    files = json.loads(out)["files"]
    assert code == 1
    assert [f["findings"][0]["code"] for f in files] == ["DL004"] * 3


def test_empty_document(tmp_path, run_cli):
    code, out, _ = run_cli("--format", "json", write(tmp_path, "e.yaml", "# nothing\n"))
    assert [f["code"] for f in json.loads(out)["files"][0]["findings"]] == ["DL003"]


@pytest.mark.parametrize("toml,msg", [
    ("[queues]\nallow = 'default'\n", "list of strings"),
    ("[nope]\n", "unknown config section"),
    ("[queues]\nbogus = 1\n", "unknown config key"),
    ("[handler_on]\nrequire = ['failed']\n", "unknown events"),
    ("[secrets]\nallow = ['(']\n", "invalid regex"),
    ("[timeouts]\nmax_dag_sec = 0\n", "must be positive"),
    ("[timeouts]\nmax_dag_sec = true\n", "must be int"),
    ("not toml", "invalid TOML"),
])
def test_config_errors(tmp_path, run_cli, toml, msg):
    code, _, err = run_cli("-c", write(tmp_path, "c.toml", toml), GOOD)
    assert code == 2 and msg in err


def test_missing_config_file(run_cli):
    code, _, err = run_cli("-c", "/nonexistent/dagu-lint.toml", GOOD)
    assert code == 2 and "cannot read config" in err


def test_default_config_in_cwd(tmp_path, monkeypatch, run_cli):
    with open(POLICY) as fh:
        (tmp_path / "dagu-lint.toml").write_text(fh.read())
    monkeypatch.chdir(tmp_path)
    code, _, err = run_cli("--now", NOW, BAD)
    assert code == 1 and "no queue allowlist" not in err


def test_config_overrides(tmp_path, run_cli):
    cfg = write(tmp_path, "c.toml", '[queues]\nallow = ["default"]\n[handler_on]\nrequire = []\n'
                                   '[timeouts]\nrequire_dag = false\n')
    p = write(tmp_path, "d.yaml", "name: x\nqueue: default\nsteps:\n  - run: echo\n")
    assert run_cli("-c", cfg, p)[0] == 0


def test_bad_now_and_tz(run_cli):
    with pytest.raises(SystemExit):
        run_cli("--now", "2026-10-23T00:00:00", GOOD)
    code, _, err = run_cli("--tz", "Nowhere/Land", GOOD)
    assert code == 2 and "unknown timezone" in err


def test_tz_flag_changes_next_runs(run_cli):
    _, out, _ = run_cli("-c", POLICY, "--now", NOW, "--tz", "Asia/Tokyo", GOOD)
    assert "next: 2026-10-24T02:00:00+09:00" in out


def test_version(run_cli, capsys):
    from dagu_lint import __version__

    with pytest.raises(SystemExit):
        run_cli("--version")
    assert __version__ in capsys.readouterr().out


def test_readme_documents_every_rule():
    from conftest import ROOT
    from dagu_lint.rules import RULES

    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        readme = fh.read()
    for code, (severity, _) in RULES.items():
        assert f"| `{code}` | {severity} |" in readme, code
