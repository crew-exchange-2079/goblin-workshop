import pytest
import yaml

from dagu_lint.loader import DuplicateKey, load_documents


def test_positions():
    doc = load_documents("a: 1\nb:\n  c: x\n")[0]
    assert doc.pos("b") == (2, 1)
    assert doc["b"].pos("c") == (3, 3)
    assert doc["b"]["c"].line == 3


def test_yaml12_booleans():
    doc = load_documents("a: on\nb: yes\nc: no\nd: true\ne: False\n")[0]
    assert doc == {"a": "on", "b": "yes", "c": "no", "d": True, "e": False}


def test_duplicate_keys_rejected():
    with pytest.raises(DuplicateKey) as e:
        load_documents("a: 1\nb: 2\na: 3\n")
    assert e.value.line == 3


def test_merge_keys_may_be_overridden():
    doc = load_documents("base: &b {x: 1, y: 2}\nother:\n  <<: *b\n  y: 3\n")[0]
    assert doc["other"] == {"x": 1, "y": 3}


def test_duplicate_after_merge_still_rejected():
    with pytest.raises(DuplicateKey):
        load_documents("base: &b {x: 1}\nother:\n  <<: *b\n  y: 3\n  y: 4\n")


def test_multi_document():
    assert len(load_documents("a: 1\n---\nb: 2\n")) == 2


def test_unsafe_tags_refused():
    with pytest.raises(yaml.YAMLError):
        load_documents("a: !!python/object/apply:os.system ['true']\n")
