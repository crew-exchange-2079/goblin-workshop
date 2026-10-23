import datetime as dt

import pytest

from conftest import NOW_DT
from dagu_lint import cron

UTC = dt.timezone.utc


def runs(expr, n=3, after=NOW_DT, tz="UTC"):
    return [t.isoformat() for t in cron.parse(expr, tz).next_runs(after, n)]


def test_descriptors_match_dagu_canonical_forms():
    assert runs("@daily") == runs("0 0 * * *")
    assert runs("@midnight") == runs("0 0 * * *")
    assert runs("@hourly", 2) == ["2026-10-23T01:00:00+00:00", "2026-10-23T02:00:00+00:00"]
    assert runs("@weekly", 1) == ["2026-10-25T00:00:00+00:00"]  # 2026-10-25 is a Sunday


def test_next_run_is_strictly_after_now():
    assert runs("0 0 * * *", 1) == ["2026-10-24T00:00:00+00:00"]


def test_names_are_case_insensitive():
    assert runs("0 9 * jan,Jul mon-FRI", 1) == runs("0 9 * 1,7 1-5", 1)


@pytest.mark.parametrize("expr", ["0 0 * * 7", "60 * * * *", "0 24 * * *", "0 0 0 * *", "0 0 * 13 *",
                                  "0 0 1 * * *", "0 0 L * *", "0 0 * * 1#2", "5-1 * * * *", "*/0 * * * *",
                                  "0 0 * * mon-", "@reboot", "", "a b c d e", "1,,2 * * * *"])
def test_rejected(expr):
    with pytest.raises(cron.CronError):
        cron.parse(expr)


def test_value_slash_step_runs_to_max():
    s = cron.parse("10/20 * * * *")
    assert sorted(s.minutes) == [10, 30, 50]


def test_day_fields_and_when_one_is_star():
    # robfig: if either day field is * (or ?), both must match.
    s = cron.parse("0 9 1 * *")
    assert not cron.day_fields_ored(s)
    assert runs("0 9 1 * *", 1) == ["2026-11-01T09:00:00+00:00"]


def test_day_fields_or_when_both_restricted():
    s = cron.parse("0 9 1-7 * MON")
    assert cron.day_fields_ored(s)
    # 2026-10-26 is a Monday outside 1-7, and it still fires.
    assert runs("0 9 1-7 * MON", 1) == ["2026-10-26T09:00:00+00:00"]


def test_step_clears_star_bit():
    # `*/2` in day-of-month is not a star for robfig, so the fields are ORed.
    s = cron.parse("0 0 */2 * MON")
    assert not s.dom_star and cron.day_fields_ored(s)
    s = cron.parse("0 0 */1 * MON")
    assert s.dom_star and not cron.day_fields_ored(s)


def test_never_fires():
    assert cron.parse("0 0 30 2 *").next_runs(NOW_DT, 1) == []
    assert cron.parse("0 0 31 4,6,9,11 *").next_runs(NOW_DT, 1) == []


def test_leap_day_found_within_search_window():
    assert runs("0 0 29 2 *", 1) == ["2028-02-29T00:00:00+00:00"]


def test_timezone_prefix():
    assert runs("CRON_TZ=Asia/Tokyo 0 10 * * *", 1) == ["2026-10-23T10:00:00+09:00"]
    assert runs("TZ=Asia/Tokyo 0 10 * * *", 1) == ["2026-10-23T10:00:00+09:00"]
    with pytest.raises(cron.CronError, match="unknown timezone"):
        cron.parse("CRON_TZ=Nowhere/Land 0 9 * * *")


def test_default_timezone_applies_without_prefix():
    assert runs("0 9 * * *", 1, tz="Europe/Berlin") == ["2026-10-23T09:00:00+02:00"]


def test_dst_gap_is_skipped():
    # 2027-03-14 02:30 does not exist in New York.
    after = dt.datetime(2027, 3, 13, 12, tzinfo=UTC)
    got = runs("CRON_TZ=America/New_York 30 2 * * *", 2, after=after)
    assert got == ["2027-03-15T02:30:00-04:00", "2027-03-16T02:30:00-04:00"]


def test_every_is_epoch_aligned():
    after = dt.datetime(2026, 10, 23, 0, 7, 30, tzinfo=UTC)
    assert runs("@every 15m", 2, after=after) == ["2026-10-23T00:15:00+00:00", "2026-10-23T00:30:00+00:00"]


@pytest.mark.parametrize("expr,msg", [("@every 30s", "whole number"), ("@every 90s", "whole number"),
                                      ("@every 0m", "whole number"), ("@every soon", "invalid duration"),
                                      ("CRON_TZ=UTC @every 1h", "timezone")])
def test_every_rejected(expr, msg):
    with pytest.raises(cron.CronError, match=msg):
        cron.parse(expr)


def test_go_durations():
    assert cron.parse_go_duration("1h30m") == 5400
    assert cron.parse_go_duration("1.5h") == 5400
    assert cron.parse_go_duration("90m") == 5400


def test_misleading_steps_match_dagu():
    assert cron.misleading_steps("*/7 * * * *")
    assert cron.misleading_steps("0 */5 * * *")
    assert cron.misleading_steps("CRON_TZ=UTC */7 * * * *")
    for ok in ("*/15 * * * *", "0 */6 * * *", "*/1 * * * *", "5 * * * *"):
        assert not cron.misleading_steps(ok)


@pytest.mark.parametrize("expr,text", [
    ("0 0 * * *", "at 00:00, every day (UTC)"),
    ("30 2 * * 1-5", "at 02:30, on Monday–Friday (UTC)"),
    ("*/15 8-18 * * MON-FRI", "every 15 minutes, during hour(s) 08–18, on Monday–Friday (UTC)"),
    ("5 * * * *", "at minute 5 of every hour, every day (UTC)"),
    ("0 0 1 1,7 *", "at 00:00, on day 1 of the month, in January and July (UTC)"),
    ("0 9 1-7 * MON", "at 09:00, on day 1–7 of the month or on Monday (UTC)"),
    ("0 9 1-31 * MON", "at 09:00, every day (UTC)"),
    ("*/7 * * * *", "at minutes 0, 7, 14, 21, 28, 35, 42, 49 and 56, every day (UTC)"),
    ("@every 90m", "every 90 minutes (on a grid aligned to the Unix epoch)"),
])
def test_describe(expr, text):
    assert cron.parse(expr).describe() == text
