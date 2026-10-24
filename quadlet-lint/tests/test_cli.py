import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from quadlet_lint.cli import discover, main
from quadlet_lint.rules import RULES

from helpers import container

ROOT = Path(__file__).resolve().parent.parent
GOOD = ROOT / "tests" / "fixtures" / "good"
BAD = ROOT / "tests" / "fixtures" / "bad"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), out=out, err=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def unit(tmp_path):
    def write(text, name="app.container"):
        p = tmp_path / name
        p.write_text(text)
        return str(p)
    return write


def test_good_tree_exits_zero():
    code, out, err = run(str(GOOD))
    assert code == 0 and out == ""
    assert "5 file(s) checked" in err


def test_bad_tree_exits_one():
    assert run(str(BAD))[0] == 1


def test_text_format_line(unit):
    path = unit(container(image="quay.io/a/b:1"))
    code, out, _ = run(path)
    assert code == 1
    assert out == f"{path}:2: warning QL101 [image-not-pinned] " \
        "Image=quay.io/a/b:1 uses tag :1 and no @sha256: digest; the running image can change under you\n"


def test_json_format(unit):
    path = unit(container(image="quay.io/a/b:1"))
    code, out, _ = run("--format", "json", "--podman-version", "5.2", path)
    data = json.loads(out)
    assert data["version"] == 1 and data["podman_version"] == "5.2"
    assert data["files"] == [path]
    assert data["summary"] == {"error": 0, "warning": 1, "info": 0}
    assert data["findings"] == [{"rule": "QL101", "name": "image-not-pinned", "severity": "warning",
                                 "path": path, "line": 2, "message": data["findings"][0]["message"]}]


def test_github_format_escapes(unit):
    path = unit(container(image="quay.io/a/b:1"), name="a,b.container")
    _, out, _ = run("--format", "github", path)
    assert out.startswith("::warning file=")
    assert "a%2Cb.container,line=2,title=QL101 image-not-pinned::" in out


def test_github_format_info_is_notice(unit):
    path = unit(container().replace("ReadOnly=true\n", ""))
    _, out, _ = run("--format", "github", path)
    assert out.startswith("::notice ")


def test_fail_on_levels(unit):
    path = unit(container().replace("ReadOnly=true\n", ""))  # one info finding
    assert run(path)[0] == 0
    assert run("--fail-on", "info", path)[0] == 1
    bad = unit(container(image="nginx"), name="b.container")
    assert run("--fail-on", "error", bad)[0] == 0
    assert run("--fail-on", "never", str(BAD))[0] == 0


def test_min_severity_hides_findings(unit):
    path = unit(container().replace("ReadOnly=true\n", ""))
    code, out, _ = run("--min-severity", "warning", "--fail-on", "info", path)
    assert code == 0 and out == ""


def test_select_and_disable(unit):
    path = unit(container(image="nginx"))
    _, out, _ = run("--select", "QL102", path)
    assert "QL102" in out and "QL101" not in out
    _, out, _ = run("--disable", "ql101,QL102", path)
    assert out == ""


def test_unknown_rule_id_is_usage_error(unit):
    with pytest.raises(SystemExit) as e:
        run("--disable", "QL999", unit(container()))
    assert e.value.code == 2


@pytest.mark.parametrize("version", ["4.3", "abc"])
def test_bad_podman_version(unit, version):
    with pytest.raises(SystemExit) as e:
        run("--podman-version", version, unit(container()))
    assert e.value.code == 2


def test_future_podman_version_warns(unit):
    code, _, err = run("--podman-version", "99.0", unit(container()))
    assert code == 0 and "key table stops at Podman" in err


def test_missing_path_and_wrong_extension(tmp_path):
    code, _, err = run(str(tmp_path / "nope.container"))
    assert code == 2 and "no such file" in err
    other = tmp_path / "x.service"
    other.write_text("[Service]\n")
    code, _, err = run(str(other))
    assert code == 2 and "not a .container" in err


def test_no_paths_is_usage_error():
    with pytest.raises(SystemExit) as e:
        run()
    assert e.value.code == 2


def test_list_rules_and_explain():
    code, out, _ = run("--list-rules")
    assert code == 0 and len(out.splitlines()) == len(RULES)
    code, out, _ = run("--explain", "ql304")
    assert code == 0 and out.startswith("QL304 podman-socket-mount (error)")
    assert "#ql304-podman-socket-mount" in out


def test_discover_skips_dropins_and_hidden(tmp_path):
    for rel in ("a.container", "sub/b.pod", "a.container.d/10.conf", "x.container.d/y.container",
                ".git/c.volume", "notes.txt"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    files, errors = discover([str(tmp_path)])
    assert [os.path.relpath(f, tmp_path) for f in files] == ["a.container", os.path.join("sub", "b.pod")]
    assert errors == []


def test_undecodable_file_is_usage_error(tmp_path):
    p = tmp_path / "bin.container"
    p.write_bytes(b"\xff\xfe[Container]")
    assert run(str(p))[0] == 2


def test_bin_wrapper_runs_without_install(tmp_path):
    proc = subprocess.run([sys.executable, str(ROOT / "bin" / "quadlet-lint"), "--format", "json", str(GOOD)],
                          capture_output=True, text=True, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["findings"] == []


def test_python_dash_m(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "quadlet_lint", "--version"],
                          capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0 and proc.stdout.startswith("quadlet-lint ")
