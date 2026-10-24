"""YAML loading with source positions.

Dagu reads DAG files with goccy/go-yaml, which follows YAML 1.2: only
true/false are booleans, so `on`, `yes` and `no` stay strings. PyYAML's
SafeLoader implements YAML 1.1, so the bool resolver is narrowed here to keep
values the way Dagu sees them. Duplicate mapping keys are reported instead of
silently keeping the last one (a duplicated `env` key can hide a value).
"""

from __future__ import annotations

import re

import yaml


class Node:
    """Mixin carrying the 1-based line/column of a constructed value."""

    line: int = 0
    col: int = 0


class LDict(dict, Node):
    """A mapping that also remembers where each key was written."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.key_pos: dict = {}

    def pos(self, key):
        return self.key_pos.get(key, (self.line, self.col))


class LList(list, Node):
    pass


class LStr(str, Node):
    pass


class DuplicateKey(Exception):
    def __init__(self, key, line, col, first_line):
        super().__init__(f"duplicate key {key!r} (first defined on line {first_line})")
        self.key = key
        self.line = line
        self.col = col
        self.first_line = first_line


def _at(obj, node):
    obj.line = node.start_mark.line + 1
    obj.col = node.start_mark.column + 1
    return obj


class Loader(yaml.SafeLoader):
    pass


# Replace the YAML 1.1 bool resolver with the YAML 1.2 core one.
Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in rs if tag != "tag:yaml.org,2002:bool"]
    for ch, rs in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


def _construct_mapping(loader, node):
    loader.flatten_mapping(node)  # resolves `<<` merge keys
    out = _at(LDict(), node)
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if isinstance(key, list):
            key = tuple(key)
        if key in out.key_pos and not getattr(key_node, "_merged", False):
            raise DuplicateKey(
                key, key_node.start_mark.line + 1, key_node.start_mark.column + 1, out.key_pos[key][0]
            )
        out[key] = loader.construct_object(value_node, deep=True)
        out.key_pos[key] = (key_node.start_mark.line + 1, key_node.start_mark.column + 1)
    return out


def _construct_seq(loader, node):
    out = _at(LList(), node)
    out.extend(loader.construct_object(child, deep=True) for child in node.value)
    return out


def _construct_str(loader, node):
    return _at(LStr(loader.construct_scalar(node)), node)


Loader.add_constructor("tag:yaml.org,2002:map", _construct_mapping)
Loader.add_constructor("tag:yaml.org,2002:seq", _construct_seq)
Loader.add_constructor("tag:yaml.org,2002:str", _construct_str)


_orig_flatten = Loader.flatten_mapping


def _flatten(self, node):
    # Keys pulled in through `<<` may legitimately be overridden by explicit
    # keys, so they are marked and never count as duplicates.
    explicit = [k for k, _ in node.value if k.tag != "tag:yaml.org,2002:merge"]
    _orig_flatten(self, node)
    explicit_ids = {id(k) for k in explicit}
    for k, _ in node.value:
        if id(k) not in explicit_ids:
            k._merged = True
    # Explicit keys override merged ones; drop merged keys that are shadowed so
    # the duplicate check only sees genuinely repeated explicit keys.
    seen = set()
    kept = []
    for k, v in reversed(node.value):
        ident = (k.tag, k.value) if isinstance(k, yaml.ScalarNode) else id(k)
        if getattr(k, "_merged", False) and ident in seen:
            continue
        seen.add(ident)
        kept.append((k, v))
    node.value = list(reversed(kept))


Loader.flatten_mapping = _flatten


def load_documents(text: str) -> list:
    """Parse every YAML document in `text`. Raises yaml.YAMLError or DuplicateKey."""
    return list(yaml.load_all(text, Loader=Loader))  # noqa: S506 - SafeLoader subclass
