import pytest

from quadlet_lint.timespan import INFINITY, format_seconds, parse_timespan


@pytest.mark.parametrize("text,seconds", [
    ("90", 90), ("90s", 90), ("1min 30s", 90), ("1min30s", 90), ("2min", 120),
    ("1h", 3600), ("500ms", 0.5), ("1.5s", 1.5), ("0", 0), (" 10 sec ", 10),
    ("infinity", INFINITY),
])
def test_valid(text, seconds):
    assert parse_timespan(text) == seconds


@pytest.mark.parametrize("text", ["", "abc", "30 parsecs", "s30", "1min x", "-5"])
def test_invalid(text):
    assert parse_timespan(text) is None


def test_format():
    assert format_seconds(90.0) == "90s"
    assert format_seconds(0.5) == "0.5s"
    assert format_seconds(INFINITY) == "infinity"
