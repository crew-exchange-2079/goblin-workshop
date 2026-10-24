"""Static checks for examples/ (no Docker). The live run is run_walkthrough.py.

Keeps the walkthroughs honest about what they chain: the compose stack uses
the same pinned images as the exporter's Grafana smoke test, mounts the repo's
dashboard and alert rules rather than copies, and the committed recording
shows the commands walkthrough.sh actually runs, in order.
"""

import ast
import json
import os
import re

import pytest

EXAMPLES = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(EXAMPLES)
E2G = os.path.join(EXAMPLES, "exporter-to-grafana")
SMOKE = os.path.join(ROOT, "tape-exporter", "ci", "grafana", "smoke.py")
DASHBOARD = os.path.join(ROOT, "tape-exporter", "monitoring", "grafana",
                         "lto-exporter-dashboard.json")
LIVE = ["exporter-to-grafana"]


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def smoke_images():
    """IMAGES from smoke.py, read with ast so nothing there is executed."""
    for node in ast.parse(read(SMOKE)).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "IMAGES":
            return ast.literal_eval(node.value)
    raise AssertionError("IMAGES not found in smoke.py")


def test_compose_images_match_the_grafana_smoke_pins():
    images = re.findall(r"^\s+image:\s*(\S+)$", read(E2G, "compose.yaml"), re.M)
    assert sorted(images) == sorted(smoke_images().values())
    assert all("@sha256:" in i for i in images)


def test_compose_mounts_repo_files_read_only_and_they_exist():
    mounts = re.findall(r"^\s+- (\.\.?/\S+):(\S+):ro$", read(E2G, "compose.yaml"), re.M)
    sources = {src for src, _ in mounts}
    assert "../../tape-exporter/monitoring/grafana" in sources
    assert "../../tape-exporter/monitoring/prometheus/lto-exporter-alerts.yml" in sources
    assert "../../tape-exporter/lto_exporter" in sources
    for src, _ in mounts:
        assert os.path.exists(os.path.normpath(os.path.join(E2G, src))), src
    # every bind mount is read-only
    volumes = re.findall(r"^\s+- (\.\.?/\S+)$", read(E2G, "compose.yaml"), re.M)
    assert volumes and all(v.endswith(":ro") for v in volumes)


def test_ports_are_published_on_loopback_only():
    ports = re.findall(r'^\s+- "([^"]+:\d+)"', read(E2G, "compose.yaml"), re.M)
    assert len(ports) == 3
    assert all(p.startswith("127.0.0.1:") for p in ports)


def test_peek_targets_exist_in_the_dashboard():
    dash = json.loads(read(DASHBOARD))
    src = read(E2G, "peek.py")
    uid = re.search(r'^DASHBOARD_UID = "([^"]+)"', src, re.M).group(1)
    panel = re.search(r'^PANEL = "([^"]+)"', src, re.M).group(1)
    assert dash["uid"] == uid
    assert panel in {p["title"] for p in dash["panels"]}
    ds_uid = re.search(r"uid: (\S+)", read(E2G, "grafana", "provisioning", "datasources",
                                            "prometheus.yml")).group(1)
    assert f'"uid": "{ds_uid}"' in src


@pytest.mark.parametrize("name", LIVE)
def test_recording_is_asciicast_v2_of_the_current_script(name):
    lines = read(EXAMPLES, name, "walkthrough.cast").splitlines()
    header = json.loads(lines[0])
    assert header["version"] == 2 and header["width"] > 0 and header["height"] > 0
    events = [json.loads(l) for l in lines[1:]]
    times = [e[0] for e in events]
    assert times == sorted(times) and all(e[1] == "o" for e in events)
    output = "".join(e[2] for e in events)
    shown = re.findall(r"\x1b\[1;36m\$ (.*?)\x1b\[0m", output)
    steps = [l.strip()[len("step "):] for l in read(EXAMPLES, name, "walkthrough.sh").splitlines()
             if l.strip().startswith("step ")]
    # `step` prints "$*", so shell quoting is gone from the recording
    assert shown == [s.replace("'", "") for s in steps]
    assert os.path.getsize(os.path.join(EXAMPLES, name, "walkthrough.gif")) > 0


def test_gallery_lists_every_walkthrough_and_marks_slots():
    readme = read(EXAMPLES, "README.md")
    for name in LIVE:
        assert f"({name}/)" in readme
    for slot in ("validator-to-viewer", "label-gen-to-capacity-planner"):
        assert slot in readme
        assert not os.path.exists(os.path.join(EXAMPLES, slot))


def test_run_walkthrough_knows_every_live_walkthrough():
    src = read(EXAMPLES, "run_walkthrough.py")
    expect = ast.literal_eval(re.search(r"^EXPECT = (\{.*?^\})", src, re.M | re.S).group(1))
    assert sorted(expect) == sorted(LIVE)
