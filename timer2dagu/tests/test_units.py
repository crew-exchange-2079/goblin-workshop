import os
import shutil
import stat
import subprocess

import pytest

import timer2dagu as t


# Cron strings below were checked against `systemd-analyze calendar --iterations=12`
# (systemd 255) by comparing the next elapse times with a cron evaluator.
@pytest.mark.parametrize("expr, cron", [
    ("minutely", "* * * * *"),
    ("hourly", "0 * * * *"),
    ("daily", "0 0 * * *"),
    ("weekly", "0 0 * * 1"),
    ("monthly", "0 0 1 * *"),
    ("quarterly", "0 0 1 1,4,7,10 *"),
    ("semiannually", "0 0 1 1,7 *"),
    ("yearly", "0 0 1 1 *"),
    ("annually", "0 0 1 1 *"),
    ("02:30", "30 2 * * *"),
    ("*-*-* 02:30:00", "30 2 * * *"),
    ("*:0/15", "0/15 * * * *"),
    ("*:00/20", "0/20 * * * *"),
    ("Mon..Fri 09:00", "0 9 * * 1-5"),
    ("Sat,Sun 10:00", "0 10 * * 0,6"),
    ("Sun 23:59", "59 23 * * 0"),
    ("Monday 07:00", "0 7 * * 1"),
    ("Mon..Sun 07:00", "0 7 * * *"),
    ("Thu..Sun 07:00", "0 7 * * 0,4-6"),
    ("*-*-01 03:00", "0 3 1 * *"),
    ("*-*-1..7 04:00", "0 4 1-7 * *"),
    ("*-1,4,7,10-15 01:00", "0 1 15 1,4,7,10 *"),
    ("12-25 08:00", "0 8 25 12 *"),
    ("0/2:00", "0 0/2 * * *"),
    ("8..18/2:05", "5 8-18/2 * * *"),
    ("*-*-* 06,18:00:00", "0 6,18 * * *"),
    ("  daily  ", "0 0 * * *"),
    ("daily UTC", "CRON_TZ=UTC 0 0 * * *"),
    ("Mon 07:00 America/New_York", "CRON_TZ=America/New_York 0 7 * * 1"),
])
def test_calendar_exact(expr, cron):
    assert t.calendar_to_cron(expr) == (cron, [])


def test_calendar_seconds_approximated():
    cron, notes = t.calendar_to_cron("*-*-* 03:00:30")
    assert cron == "0 3 * * *"
    assert [k for k, _ in notes] == [t.APPROXIMATED]


@pytest.mark.parametrize("expr, why", [
    ("*:*:0/30", "more than once a minute"),
    ("*-*-* *:*:*", "more than once a minute"),
    ("Mon *-*-1..7 03:00", "both weekday and day-of-month"),
    ("*-*~01 23:00", "end of the month"),
    ("2027-01-01 00:00", "specific year"),
    ("@1700000000", "UNIX-timestamp"),
    ("Fri..Mon 12:00", "runs backwards"),
    ("Funday 12:00", "unknown weekday"),
    ("25:00", "out of range"),
    ("*-13-01", "out of range"),
    ("*:0/0", "repetition of 0"),
    ("", "empty"),
])
def test_calendar_untranslatable(expr, why):
    with pytest.raises(t.CalendarError) as e:
        t.calendar_to_cron(expr)
    assert why in str(e.value)


@pytest.mark.parametrize("text, seconds", [
    ("90", 90), ("5min", 300), ("1h 30min", 5400), ("2h", 7200), ("1.5h", 5400),
    ("1d", 86400), ("1w", 604800), ("500ms", 0.5), ("10s", 10), ("3 min 4 s", 184),
])
def test_timespan(text, seconds):
    assert t.parse_timespan(text) == pytest.approx(seconds)


def test_timespan_special():
    assert t.parse_timespan("infinity") == float("inf")
    assert t.parse_timespan("5 parsecs") is None
    assert t.parse_timespan("") is None


def _ctx(user=False, home=None):
    return t.Context("job.service", user, home, None, t.Report())


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")
@pytest.mark.parametrize("value, env, argv", [
    ('/bin/echo "a b" c', {}, ["a b", "c"]),
    ("/bin/echo 'it''s' \"x\\\"y\"", {}, ["its", 'x"y']),
    ('/bin/echo "%%p\\n" *.tmp', {}, ["%p\n", "*.tmp"]),
    ("/bin/echo $OPTS", {"OPTS": "-a  -b"}, ["-a", "-b"]),
    ('/bin/echo "$OPTS"', {"OPTS": "-a -b"}, ["-a", "-b"]),
    ("/bin/echo ${OPTS}", {"OPTS": "-a -b"}, ["-a -b"]),
    ("/bin/echo pre${D}/x", {"D": "my dir"}, ["premy dir/x"]),
    ("/bin/echo pre$D", {"D": "x"}, ["pre$D"]),
    ("/bin/echo $$HOME", {"HOME": "/nope"}, ["$HOME"]),
    ("/bin/echo \\x41\\101", {}, ["AA"]),
    ("/bin/echo ''", {}, [""]),
])
def test_shell_round_trip(value, env, argv):
    """The emitted shell words give the argv systemd would have built."""
    words = t.split_words(value, _ctx(), "test", expand_env=True)
    script = " ".join(t.sh_word(w) for w in words[1:])
    out = subprocess.run(["sh", "-c", "printf '%s\\0' " + script],
                         env=dict(env, PATH=os.environ.get("PATH", "/usr/bin:/bin")),
                         capture_output=True, text=True, check=True).stdout
    assert out.split("\0")[:-1] == argv


def test_specifiers_user_home_given():
    words = t.split_words("%h/bin %u", _ctx(user=True, home="/home/a"), "w", expand_env=True)
    assert words == [[("lit", "/home/a/bin")], [("var", "USER")]]


def test_unescape_unit():
    assert t.unescape_unit("srv-data\\x2dold") == "srv/data-old"


def test_yaml_emitter_round_trip():
    yaml = pytest.importorskip("yaml")
    doc = {"name": "x", "description": "a: b # c", "n": 3, "yes": "yes", "ON": "1", "empty": "",
           "steps": [{"name": "s", "run": "line one\nline 'two'", "continue_on":
                      {"failure": True}}, {"run": "trailing space \nx"}],
           "lst": ["0 0 * * *", "CRON_TZ=UTC 0 1 * * *", "on", "1.5", "été"]}
    assert yaml.safe_load(t.to_yaml(doc)) == doc


# ---- CLI --------------------------------------------------------------------

TIMER = "[Timer]\nOnCalendar=daily\n"


def _units(tmp_path, service, timer=TIMER, stem="job", sub=""):
    d = tmp_path / sub if sub else tmp_path
    d.mkdir(parents=True, exist_ok=True)
    (d / (stem + ".timer")).write_text(timer)
    (d / (stem + ".service")).write_text(service)
    return d / (stem + ".timer")


def test_cli_stdout_and_report(tmp_path, capsys):
    timer = _units(tmp_path, "[Service]\nExecStart=/bin/true\n")
    assert t.main([str(timer)]) == 0
    out, err = capsys.readouterr()
    assert 'schedule: "0 0 * * *"' in out
    assert "nothing to report" in err


def test_cli_follows_unit_directive(tmp_path, capsys):
    timer = _units(tmp_path, "[Service]\nExecStart=/bin/true\n",
                   timer="[Timer]\nOnCalendar=daily\nUnit=other.service\n")
    (tmp_path / "other.service").write_text("[Service]\nExecStart=/bin/other\n")
    assert t.main([str(timer)]) == 0
    assert "run: /bin/other" in capsys.readouterr()[0]


def test_cli_environment_needs_destination(tmp_path, capsys):
    timer = _units(tmp_path, "[Service]\nEnvironment=SECRET=x\nExecStart=/bin/true\n")
    assert t.main([str(timer)]) == 1
    assert "--env-out" in capsys.readouterr()[1]


def test_cli_environment_written_privately(tmp_path):
    timer = _units(tmp_path, "[Service]\nEnvironment=SECRET=hunter2\nExecStart=/bin/true\n")
    out = tmp_path / "dags" / "job.yaml"
    out.parent.mkdir()
    assert t.main([str(timer), "-o", str(out), "--report", str(tmp_path / "r.txt")]) == 0
    dag = out.read_text()
    assert "hunter2" not in dag
    assert "- %s" % (tmp_path / "dags" / "job.env") in dag
    env = tmp_path / "dags" / "job.env"
    assert "SECRET='hunter2'" in env.read_text()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_cli_strict(tmp_path):
    timer = _units(tmp_path, "[Service]\nUser=nobody\nExecStart=/bin/true\n")
    report = str(tmp_path / "r.txt")
    assert t.main([str(timer), "-o", str(tmp_path / "o.yaml"), "--report", report]) == 0
    assert t.main([str(timer), "-o", str(tmp_path / "o.yaml"), "--report", report,
                   "--strict"]) == 2


def test_cli_user_path_detected(tmp_path, capsys):
    timer = _units(tmp_path, "[Service]\nExecStart=%h/bin/job\n", sub=".config/systemd/user")
    assert t.main([str(timer), "--home", "/home/z"]) == 0
    assert "run: /home/z/bin/job" in capsys.readouterr()[0]


def test_cli_errors(tmp_path, capsys):
    timer = _units(tmp_path, "[Service]\nType=oneshot\n")
    assert t.main([str(timer)]) == 1
    assert "no ExecStart=" in capsys.readouterr()[1]
    assert t.main([str(tmp_path / "missing.timer")]) == 1
    (tmp_path / "bad.timer").write_text("OnCalendar=daily\n")
    (tmp_path / "bad.service").write_text("[Service]\nExecStart=/bin/true\n")
    assert t.main([str(tmp_path / "bad.timer")]) == 1
    assert "outside any [Section]" in capsys.readouterr()[1]
