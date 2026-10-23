"""Good fixtures must also be valid against Dagu's own JSON schema, so they
are real DAGs and not just inputs this linter happens to accept.

Dagu is GPL-3.0, so its schema is not vendored here. CI downloads it at a
pinned commit (see .github/workflows/ci.yml) and points DAGU_SCHEMA at it.
"""

import json
import os
import re

import pytest

from conftest import ROOT, fixture_files

SCHEMA = os.environ.get("DAGU_SCHEMA")
REQUIRED = os.environ.get("DAGU_LINT_REQUIRE_SCHEMA") == "1"


def _need(ok, reason):
    if not ok:
        if REQUIRED:
            pytest.fail(reason + " (DAGU_LINT_REQUIRE_SCHEMA=1)")
        pytest.skip(reason)


@pytest.mark.parametrize("path", fixture_files("good"), ids=lambda p: os.path.relpath(p, ROOT))
def test_good_fixture_is_schema_valid(path):
    _need(SCHEMA and os.path.isfile(SCHEMA), "DAGU_SCHEMA not set or missing")
    try:
        import jsonschema
    except ImportError:
        jsonschema = None
    _need(jsonschema, "jsonschema not installed")
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    # YAML 1.2 booleans, as Dagu (goccy/go-yaml) reads them.
    Loader.yaml_implicit_resolvers = {
        k: [(t, r) for t, r in v if t != "tag:yaml.org,2002:bool"]
        for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    Loader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$"), list("tf"))

    with open(SCHEMA, encoding="utf-8") as fh:
        schema = json.load(fh)
    validator = jsonschema.validators.validator_for(schema)(schema)
    with open(path, encoding="utf-8") as fh:
        docs = [d for d in yaml.load_all(fh, Loader=Loader) if d is not None]
    for doc in docs:
        errors = [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:200]}"
                  for e in validator.iter_errors(doc)]
        assert not errors, errors
