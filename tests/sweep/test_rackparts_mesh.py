import pytest

from rackparts import mesh

_TETRA_FACES = [(0, 2, 1), (0, 1, 3), (1, 2, 3), (0, 3, 2)]


def _ascii_tetra(z):
    v = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, z)]
    s = "solid t\n"
    for f in _TETRA_FACES:
        s += "facet normal 0 0 0\nouter loop\n"
        s += "".join("vertex %s %s %s\n" % tuple(map(str, v[i])) for i in f)
        s += "endloop\nendfacet\n"
    return s + "endsolid t\n"


def test_sane_tetra_is_ok(tmp_path):  # control: the fixture itself is valid
    p = tmp_path / "t.stl"
    p.write_text(_ascii_tetra("1"))
    assert mesh.analyse(mesh.read_stl(str(p)))["ok"]


def test_infinite_coordinate_is_not_ok(tmp_path):
    p = tmp_path / "t.stl"
    p.write_text(_ascii_tetra("inf"))
    try:
        rep = mesh.analyse(mesh.read_stl(str(p)))
    except mesh.MeshError:
        return
    assert not rep["ok"]
