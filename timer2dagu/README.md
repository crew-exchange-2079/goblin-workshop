# timer2dagu

Convert a systemd `.timer` + `.service` pair into a [Dagu](https://github.com/dagu-org/dagu)
DAG, and say plainly what did not survive the trip.
Python 3.10+ standard library only; one file, `timer2dagu.py`.

```console
$ python3 timer2dagu.py ~/.config/systemd/user/restic.timer -o dags/restic.yaml
```

Every run prints a report (to stderr, or `--report FILE`) that sorts each
directive which was not carried over exactly into one of three buckets:

| bucket | meaning |
|---|---|
| **untranslated** | no Dagu equivalent; the directive was dropped (specifiers: left verbatim). Behaviour **will** differ. |
| **approximated** | translated, but Dagu's behaviour is close rather than equal; the entry says how. |
| **ignored** | has no effect for a timer-started job under Dagu (e.g. `[Install]`); nothing is lost. |

`--strict` makes the exit status 2 when anything is untranslated, so a batch
migration can stop for review.

> **Status: 0.1.0, untested against a running Dagu.** The cron output was
> checked against `systemd-analyze calendar` (systemd 255) for every
> translatable expression in the unit tests, and every golden DAG validates
> against Dagu's published JSON schema (`internal/cmn/schema/dag.schema.json`
> on dagu-org/dagu `main`, fetched 2026-10-23). It has **not** been loaded
> into a Dagu scheduler; the points marked *unverified* below are Dagu
> behaviours taken from its schema and docs, not observed. Older Dagu
> releases that use camelCase keys (`timeoutSec`, `handlerOn`) will not
> accept this output.

## What is translated

| systemd | Dagu |
|---|---|
| `OnCalendar=` (each line; `OnCalendar=` empty resets) | `schedule` (a string, or a list for several lines) |
| shorthands `minutely` … `semiannually`, `annually` | the equivalent cron line |
| `OnCalendar=… Europe/Berlin` | `CRON_TZ=Europe/Berlin …` prefix (*unverified*) |
| `Persistent=true` | `catchup_window` (default `7d`, `--catchup-window`) + `overlap_policy: latest` |
| `RandomizedDelaySec=` | a first step `randomized-delay` that sleeps 0…N s |
| `OnUnitActiveSec=` / `OnUnitInactiveSec=` (≥ 1 min) | `@every …` (approximated) |
| `Environment=` | a companion `.env` file named in `dotenv` — values never enter the DAG |
| `EnvironmentFile=` | `dotenv` entry with the same path — the file is never read |
| `WorkingDirectory=` | `working_dir` |
| `ExecStartPre=`, `ExecStart=`, `ExecStartPost=` | steps in that order, chained with `depends` |
| `ExecStopPost=` | `handler_on.exit` (runs after success or failure) |
| `-` command prefix | `continue_on: {failure: true, mark_success: true}`; `\|\| true` in the exit handler |
| `TimeoutStartSec=` / `TimeoutSec=` | `Type=oneshot`: DAG `timeout_sec`. Otherwise it only bounds start-up, so it goes on the `ExecStartPre` steps |
| `RuntimeMaxSec=` | DAG `timeout_sec` |
| `%h %u %t %S %C %L %E %n %N %p %P %i %I %j %J %f %T %V %%` | resolved (see below) |
| `Description=` | `description` (the service's, else the timer's) |

### Calendar expressions

Cron has five fields, so an `OnCalendar=` line is translated only when cron
can say the same thing. These are **untranslated** (the line is dropped and
reported; other lines still translate):

* seconds that repeat (`*:*:0/30`) — cron fires at most once a minute. A
  single non-zero second (`03:00:30`) is *approximated* to the whole minute;
* a weekday **and** a day of the month together (`Mon *-*-1..7`): systemd
  requires both, cron fires when either matches;
* `~` (days counted from the end of the month), a specific year, `@epoch`.

### Commands

systemd does not run `Exec*=` lines through a shell; Dagu `run` does. So each
word is re-quoted: literal text is single-quoted, `${VAR}` becomes `"${VAR}"`
(one word, as in systemd) and a whole-word `$VAR` stays unquoted (split at
whitespace, as in systemd). `$$` is a literal `$`. The `:` prefix turns off
variable expansion. `tests/test_units.py::test_shell_round_trip` runs the
quoted words through `sh` and checks the argv.

A literal `$` that reaches the YAML is reported: Dagu interpolates variables
in `run` itself before the shell sees it (*unverified* for single quotes).

### Environment

`Environment=` values often hold tokens, so they are not inlined. With `-o
dags/job.yaml` they go to `dags/job.env` (mode 0600, override with
`--env-out`) as single-quoted dotenv lines, and the DAG lists that file
first in `dotenv` (by absolute path, so nothing in `working_dir` can shadow
it), followed by every `EnvironmentFile=` path. That keeps
systemd's precedence (`EnvironmentFile=` beats `Environment=`) because later
dotenv files override earlier ones. `--inline-env` copies values into `env`
instead and says so in the report.

Note that Dagu also loads `.env` from the working directory when present.

### User units and specifiers

Units are user units with `--user`, or when the timer's path contains
`/systemd/user/`. For system units, `%h` is `/root` and `%u` is `root`, as
in systemd. For user units:

* `--home /home/alice` and `--user-name alice` bake in `%h`/`~` and `%u`;
* without them, `%h` becomes `${HOME}` and `%u` `${USER}`, read from the
  Dagu process environment at run time (reported);
* `%t` becomes `${XDG_RUNTIME_DIR}`; `%S %C %L %E` use systemd's
  `~/.local/state`, `~/.cache`, `~/.local/state/log`, `~/.config` defaults
  (reported: `$XDG_*` overrides are not consulted).

Specifiers that depend on the host (`%H %m %b %v %a %U` in user units, …)
are left verbatim and reported as untranslated.

## What is not translated

Reported, never silently dropped: `OnBootSec`, `OnStartupSec`,
`OnActiveSec`, `WakeSystem`, `OnClockChange`, `OnTimezoneChange`,
`FixedRandomDelay` (the delay is drawn afresh each run); `User=`, `Group=`,
sandboxing and resource directives (Dagu runs steps as its own user);
`Type=forking|notify|dbus`; `ExecCondition=`; `[Unit]` dependencies
(`After=`, `Wants=`, `OnFailure=` …); the `+ ! !!` privilege prefixes and
`@` argv[0] override; `$SERVICE_RESULT`/`$EXIT_CODE` in `ExecStopPost=`;
drop-in directories (`*.service.d/`) — merge them into the unit first, e.g.
with `systemctl cat`.

## Options

```
timer2dagu TIMER [SERVICE] [-o DAG.yaml] [--report FILE] [--env-out FILE]
           [--inline-env] [--user] [--home DIR] [--user-name NAME]
           [--catchup-window 7d] [--strict]
```

`SERVICE` defaults to the timer's `Unit=`, else the same name with
`.service`, next to the timer. Exit status: 0 converted, 1 could not convert
(no `ExecStart=`, unreadable file, syntax error), 2 `--strict` and something
was untranslated.

## Tests

```sh
python -m pytest -q tests
```

`tests/golden/` holds 76 timer + service pairs, each with the expected DAG,
report and (when there is `Environment=`) `.env`; an optional `options.json`
passes keyword arguments to `convert()`. After an intended change,
regenerate with `TIMER2DAGU_REGEN=1 python -m pytest -q tests/test_golden.py`
and review the diff.

MIT licensed.
