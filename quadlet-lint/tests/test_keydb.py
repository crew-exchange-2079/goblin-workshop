import importlib.util
import json
from pathlib import Path

import pytest

from quadlet_lint.keydb import KeyDB, VersionError, parse_version

from helpers import DB

TOOLS = Path(__file__).resolve().parent.parent / "tools" / "extract_keys.py"


def test_parse_version_forms():
    assert parse_version("5.2") == (5, 2, 0)
    assert parse_version("v5.2.1") == (5, 2, 1)
    assert parse_version("5.3.0-rc2") == (5, 3, 0)
    with pytest.raises(VersionError):
        parse_version("five")


def test_table_covers_quadlet_history():
    assert DB.oldest == (4, 4, 0)
    assert DB.newest >= (6, 1, 0)
    for section in ("Container", "Volume", "Network", "Pod", "Quadlet"):
        assert DB.keys(section), section


@pytest.mark.parametrize("section,key,added", [
    ("Container", "Image", "4.4"),
    ("Container", "HealthCmd", "4.5"),
    ("Container", "AutoUpdate", "4.6"),
    ("Container", "Pod", "5.0"),
    ("Container", "StopTimeout", "5.0"),
    ("Container", "Memory", "5.5"),
    ("Pod", "PodName", "5.0"),
])
def test_known_introductions(section, key, added):
    assert DB.keys(section)[key]["added"][:2] == parse_version(added)[:2]


def test_status_compares_major_minor_only():
    assert DB.status("Container", "Pod", (4, 9, 3))[0] == "too-new"
    assert DB.status("Container", "Pod", (5, 0, 0))[0] == "ok"
    assert DB.status("Container", "Pod", (5, 0, 7))[0] == "ok"
    assert DB.status("Container", "Nope", (5, 0, 0)) == ("unknown", None)


def test_removed_status():
    db = KeyDB({"versions": ["4.4.0", "5.0.0"], "sections": {"Container": {
        "Old": {"added": "4.4.0", "removed": "5.0.0", "deprecated": False}}}})
    assert db.status("Container", "Old", (4, 9, 0))[0] == "ok"
    assert db.status("Container", "Old", (5, 1, 0))[0] == "removed"


def test_deprecated_flags_come_from_upstream_comments():
    deprecated = {k for k, v in DB.keys("Container").items() if v["deprecated"]}
    assert {"RemapUsers", "RemapUid", "RemapGid", "RemapUidSize"} <= deprecated
    assert "Image" not in deprecated


OLD_LAYOUT = '''
const (
	ContainerGroup = "Container"
	KeyImage = "Image"
	KeyExec  = "Exec" // deprecated
)
var (
	supportedContainerKeys = map[string]bool{
		KeyImage: true,
		KeyExec:  true,
	}
)
'''
NEW_LAYOUT = '''
const (
	ContainerGroup = "Container"
	KeyImage = "Image"
	KeyPod   = "Pod"
)
var (
	groupsInfo = map[string]GroupInfo{
		ContainerGroup: {
			Extension: ".container",
			SupportedKeys: map[string]bool{
				KeyImage: true,
				KeyPod:   true,
			},
		},
	}
)
'''


def test_extractor_handles_both_upstream_layouts(tmp_path, capsys):
    spec = importlib.util.spec_from_file_location("extract_keys", TOOLS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    (tmp_path / "q-4.4.0.go").write_text(OLD_LAYOUT)
    (tmp_path / "q-5.6.0.go").write_text(NEW_LAYOUT)
    mod.main(["x", str(tmp_path)])
    data = json.loads(capsys.readouterr().out)
    keys = data["sections"]["Container"]
    assert keys["Image"] == {"added": "4.4.0", "removed": None, "deprecated": False}
    assert keys["Exec"] == {"added": "4.4.0", "removed": "5.6.0", "deprecated": False}
    assert keys["Pod"]["added"] == "5.6.0"
