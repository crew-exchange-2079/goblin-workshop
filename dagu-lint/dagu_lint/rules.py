"""Policy rules for one Dagu DAG file.

These run on top of (not instead of) Dagu's own JSON schema: they assume the
file is roughly well-formed and check policy the schema cannot express. Where
a value has the wrong type the rule reports it rather than crashing, but it
does not try to repeat every schema check.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import difflib
import re

from . import cron, secrets
from .config import HANDLER_EVENTS, Config
from .loader import LDict, LList, LStr

ERROR, WARNING = "error", "warning"

# code -> (severity, short title). The README table is generated from this.
RULES = {
    "DL001": (ERROR, "YAML syntax error"),
    "DL002": (ERROR, "duplicate mapping key"),
    "DL003": (ERROR, "document is not a mapping"),
    "DL004": (ERROR, "file cannot be read"),
    "DL101": (ERROR, "queue not in allowlist"),
    "DL102": (ERROR, "queue missing"),
    "DL103": (ERROR, "queue is not a non-empty string"),
    "DL201": (ERROR, "schedule does not parse"),
    "DL202": (ERROR, "schedule never fires"),
    "DL203": (WARNING, "uneven */N step resets at the wrap"),
    "DL204": (WARNING, "day-of-month and day-of-week are ORed"),
    "DL205": (WARNING, "one-off schedule is in the past"),
    "DL301": (ERROR, "handler_on missing"),
    "DL302": (ERROR, "required handler missing"),
    "DL303": (ERROR, "handler_on malformed"),
    "DL401": (ERROR, "literal value under a secret-looking key"),
    "DL402": (ERROR, "known credential format"),
    "DL403": (ERROR, "high-entropy token"),
    "DL501": (ERROR, "DAG timeout_sec missing"),
    "DL502": (ERROR, "timeout_sec invalid or above the maximum"),
    "DL503": (WARNING, "step timeout longer than DAG timeout"),
    "DL601": (ERROR, "retry limit invalid or above the maximum"),
    "DL602": (WARNING, "retry without interval_sec"),
    "DL603": (WARNING, "retry backoff without max_interval_sec"),
    "DL604": (ERROR, "retry interval invalid or inconsistent"),
    "DL605": (ERROR, "retry on exit code 0"),
    "DL606": (WARNING, "retries cannot fit in the DAG timeout"),
    "DL701": (ERROR, "worker_selector below DAG level"),
    "DL801": (ERROR, "exec command is not a single string"),
    "DL802": (ERROR, "exec command contains arguments"),
    "DL803": (ERROR, "exec command contains shell syntax"),
}


@dataclasses.dataclass
class Finding:
    code: str
    message: str
    line: int = 0
    col: int = 0
    doc: int = 0

    @property
    def severity(self) -> str:
        return RULES[self.code][0]


@dataclasses.dataclass
class Info:
    """Non-finding output: the human-readable schedule summary."""

    where: str
    expression: str
    description: str
    next_runs: list[str]
    line: int = 0


def _pos(obj, parent=None, key=None):
    if isinstance(parent, LDict) and key in parent.key_pos:
        return parent.pos(key)
    return (getattr(obj, "line", 0), getattr(obj, "col", 0))


def _int(v):
    """An int from an int or an all-digit string (Dagu accepts both for limits)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"-?\d+", v.strip()):
        return int(v)
    return None


def iter_steps(dag: LDict):
    """Yield (step, label, is_handler) for every step mapping in the DAG,
    including parallel groups, map-form steps, foreach bodies and handlers."""

    def walk(steps, prefix):
        if isinstance(steps, LList):
            for i, item in enumerate(steps):
                label = f"{prefix}[{i}]"
                if isinstance(item, LList):
                    yield from walk(item, label)
                elif isinstance(item, LDict):
                    yield from one(item, label)
        elif isinstance(steps, LDict):
            for name, item in steps.items():
                if isinstance(item, LDict):
                    yield from one(item, f"{prefix}.{name}")

    def one(step, label):
        name = step.get("id") or step.get("name")
        yield step, (f"{label} ({name})" if isinstance(name, str) else label), False
        fe = step.get("foreach")
        if isinstance(fe, LDict):
            yield from walk(fe.get("steps"), f"{label}.foreach.steps")

    yield from walk(dag.get("steps"), "steps")
    handlers = dag.get("handler_on")
    if isinstance(handlers, LDict):
        for event, step in handlers.items():
            if isinstance(step, LDict):
                yield step, f"handler_on.{event}", True


class Linter:
    def __init__(self, cfg: Config, now: dt.datetime):
        self.cfg = cfg
        self.now = now
        # Every value a secret rule matched in this file, whether or not the
        # rule is disabled or suppressed; output is scrubbed with redact().
        self.sensitive: set[str] = set()

    def redact(self, text: str) -> str:
        return secrets.redact(text, self.sensitive, self.cfg.entropy_min_length, self.cfg.entropy_threshold)

    def lint_dag(self, dag, doc_index: int, is_main: bool):
        self.findings: list[Finding] = []
        self.infos: list[Info] = []
        self.doc = doc_index
        if not isinstance(dag, LDict):
            self.add("DL003", "a DAG document must be a YAML mapping", *_pos(dag))
            return self.findings, self.infos
        if is_main:
            self.check_queue(dag)
            self.check_schedule(dag)
            self.check_handlers(dag)
            self.check_dag_timeout(dag)
        if "retry_policy" in dag:
            self.check_retry(dag.get("retry_policy"), "retry_policy", dag, "retry_policy", None, dag_level=True)
        dag_timeout = _int(dag.get("timeout_sec"))
        for step, label, _ in iter_steps(dag):
            self.check_step(step, label, dag_timeout)
        self.check_secrets(dag)
        return self.findings, self.infos

    def add(self, code, message, line=0, col=0):
        self.findings.append(Finding(code, message, line, col, self.doc))

    # --- queue ---------------------------------------------------------
    def check_queue(self, dag):
        if "queue" not in dag:
            if self.cfg.require_queue:
                name = dag.get("name")
                hint = f" (Dagu would use the DAG name {name!r})" if isinstance(name, str) else " (Dagu would use the DAG name)"
                self.add("DL102", "no `queue` set" + hint + "; set one from the allowlist", dag.line, dag.col)
            return
        q = dag["queue"]
        line, col = dag.pos("queue")
        if not isinstance(q, str) or not q.strip():
            self.add("DL103", "`queue` must be a non-empty string", line, col)
            return
        if self.cfg.queues and q not in self.cfg.queues:
            close = difflib.get_close_matches(q, self.cfg.queues, n=1)
            hint = f"; did you mean {close[0]!r}?" if close else f"; allowed: {', '.join(self.cfg.queues)}"
            self.add("DL101", f"queue {q!r} is not in the allowlist{hint}", line, col)

    # --- schedule ------------------------------------------------------
    def check_schedule(self, dag):
        if "schedule" not in dag:
            return
        sched = dag["schedule"]
        line, col = dag.pos("schedule")
        if isinstance(sched, LDict):
            for kind, value in sched.items():
                kline, kcol = sched.pos(kind)
                if kind not in ("start", "stop", "restart"):
                    self.add("DL201", f"schedule.{kind}: unknown key (expected start, stop or restart)", kline, kcol)
                    continue
                self._schedule_entries(value, f"schedule.{kind}", kline, kcol, allow_at=(kind == "start"))
        else:
            self._schedule_entries(sched, "schedule", line, col, allow_at=True)

    def _schedule_entries(self, value, where, line, col, allow_at):
        items = value if isinstance(value, LList) else [value]
        for i, item in enumerate(items):
            label = f"{where}[{i}]" if isinstance(value, LList) else where
            iline, icol = _pos(item) if getattr(item, "line", 0) else (line, col)
            if isinstance(item, LDict):
                if "at" in item:
                    if not allow_at:
                        self.add("DL201", f"{label}: one-off `at` entries are only allowed for start", iline, icol)
                    else:
                        self._one_off(item["at"], label, *item.pos("at"))
                    continue
                expr = item.get("expression")
                if expr is None:
                    self.add("DL201", f"{label}: entry needs `expression` or `at`", iline, icol)
                    continue
                iline, icol = item.pos("expression")
                item = expr
            if not isinstance(item, str):
                self.add("DL201", f"{label}: expected a cron string, got {type(item).__name__}", iline, icol)
                continue
            try:
                s = cron.parse(item, self.cfg.timezone)
            except cron.CronError as e:
                self.add("DL201", f"{label}: {item.strip()!r}: {e}", iline, icol)
                continue
            for msg in cron.misleading_steps(s.expression):
                self.add("DL203", f"{label}: {msg}", iline, icol)
            if cron.day_fields_ored(s):
                self.add("DL204", f"{label}: {s.expression!r} fires on days matching day-of-month OR day-of-week, "
                                  "not both", iline, icol)
            runs = s.next_runs(self.now, max(self.cfg.next_runs, 1))
            if not runs:
                self.add("DL202", f"{label}: {s.expression!r} has no fire time in the next {cron.SEARCH_YEARS} years",
                         iline, icol)
                continue
            self.infos.append(Info(label, s.expression, s.describe(),
                                   [r.isoformat() for r in runs[: self.cfg.next_runs]], iline))

    def _one_off(self, at, label, line, col):
        if not isinstance(at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00(?:Z|[+-]\d{2}:\d{2})", at):
            self.add("DL201", f"{label}.at: must be RFC 3339 with minute precision and an explicit offset "
                              "(e.g. 2026-11-01T09:30:00Z)", line, col)
            return
        try:
            when = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            self.add("DL201", f"{label}.at: {at!r} is not a real date/time", line, col)
            return
        if when <= self.now:
            self.add("DL205", f"{label}.at: {at} is in the past; it will never fire", line, col)
            return
        self.infos.append(Info(label, at, "once", [when.isoformat()], line))

    # --- handlers ------------------------------------------------------
    def check_handlers(self, dag):
        required = self.cfg.handlers_required
        if "handler_on" not in dag:
            if required:
                self.add("DL301", f"no `handler_on`; required events: {', '.join(required)}", dag.line, dag.col)
            return
        h = dag["handler_on"]
        line, col = dag.pos("handler_on")
        if not isinstance(h, LDict):
            self.add("DL303", "`handler_on` must be a mapping of event -> step", line, col)
            return
        for event, step in h.items():
            eline, ecol = h.pos(event)
            if event not in HANDLER_EVENTS:
                close = difflib.get_close_matches(str(event), HANDLER_EVENTS, n=1)
                hint = f"; did you mean {close[0]!r}?" if close else ""
                self.add("DL303", f"handler_on.{event}: unknown event{hint}", eline, ecol)
            elif not isinstance(step, LDict) or not step:
                self.add("DL303", f"handler_on.{event}: must be a step mapping (e.g. `run: ./notify.sh`)", eline, ecol)
        for event in required:
            if event not in h:
                self.add("DL302", f"handler_on.{event} is required", line, col)

    # --- timeouts ------------------------------------------------------
    def check_dag_timeout(self, dag):
        if "timeout_sec" not in dag:
            if self.cfg.require_dag_timeout:
                self.add("DL501", f"no DAG-level `timeout_sec` (maximum {self.cfg.max_dag_timeout_sec})",
                         dag.line, dag.col)
            return
        self._timeout(dag["timeout_sec"], "timeout_sec", self.cfg.max_dag_timeout_sec, *dag.pos("timeout_sec"),
                      allow_zero=False)

    def _timeout(self, v, where, maximum, line, col, allow_zero):
        n = v if isinstance(v, int) and not isinstance(v, bool) else None
        if n is None or n < 0 or (n == 0 and not allow_zero):
            self.add("DL502", f"{where}: must be a positive integer number of seconds, got {v!r}", line, col)
            return None
        if n > maximum:
            self.add("DL502", f"{where}: {n}s is above the maximum {maximum}s", line, col)
        return n

    # --- steps ---------------------------------------------------------
    def check_step(self, step: LDict, label: str, dag_timeout):
        if "worker_selector" in step:
            self.add("DL701", f"{label}: worker_selector is only allowed at DAG level", *step.pos("worker_selector"))
        step_timeout = None
        if "timeout_sec" in step:
            step_timeout = self._timeout(step["timeout_sec"], f"{label}.timeout_sec", self.cfg.max_step_timeout_sec,
                                         *step.pos("timeout_sec"), allow_zero=True)
            if step_timeout and dag_timeout and dag_timeout > 0 and step_timeout > dag_timeout:
                self.add("DL503", f"{label}.timeout_sec ({step_timeout}s) is longer than the DAG timeout_sec "
                                  f"({dag_timeout}s); one of them is wrong", *step.pos("timeout_sec"))
        if "retry_policy" in step:
            self.check_retry(step["retry_policy"], f"{label}.retry_policy", step, "retry_policy",
                             (step_timeout, dag_timeout), dag_level=False)
        if step.get("action") == "exec":
            w = step.get("with")
            if not isinstance(w, LDict) or "command" not in w:
                line, col = step.pos("with") if "with" in step else step.pos("action")
                self.add("DL801", f"{label}: action exec needs `with.command` (a single executable name/path)",
                         line, col)
            else:
                self._exec_command(w["command"], f"{label}.with.command", *w.pos("command"))
        elif isinstance(step.get("exec"), LDict) and "command" in step["exec"]:
            e = step["exec"]
            self._exec_command(e["command"], f"{label}.exec.command", *e.pos("command"))

    def _exec_command(self, cmd, where, line, col):
        if not isinstance(cmd, str):
            kind = "a list" if isinstance(cmd, list) else type(cmd).__name__
            self.add("DL801", f"{where}: must be a single string (the executable), got {kind}; "
                              "put arguments in with.args", line, col)
            return
        if not cmd.strip():
            self.add("DL801", f"{where}: must not be empty", line, col)
            return
        if re.search(r"[|&;<>`*\n]|\$\(", cmd):
            self.add("DL803", f"{where}: exec runs the executable directly with no shell; pipes, redirects, "
                              "globs and `;`/`&&` will not work (use `run:` for shell commands)", line, col)
        elif re.search(r"\s", cmd.strip()) or cmd != cmd.strip():
            self.add("DL802", f"{where}: contains whitespace; exec does not split it, so arguments must go in "
                              "with.args", line, col)

    # --- retry ---------------------------------------------------------
    def check_retry(self, rp, where, parent, key, timeouts, dag_level):
        line, col = parent.pos(key)
        if not isinstance(rp, LDict):
            self.add("DL601", f"{where}: must be a mapping", line, col)
            return
        limit = _int(rp.get("limit"))
        if "limit" not in rp:
            if dag_level:
                self.add("DL601", f"{where}: `limit` is required", line, col)
            return
        lline, lcol = rp.pos("limit")
        if limit is None or limit < 0:
            self.add("DL601", f"{where}.limit: must be a non-negative integer, got {rp['limit']!r}", lline, lcol)
            return
        if limit > self.cfg.max_retry_limit:
            self.add("DL601", f"{where}.limit: {limit} is above the maximum {self.cfg.max_retry_limit}", lline, lcol)
        if limit == 0:
            return
        interval = None
        if "interval_sec" in rp:
            interval = _int(rp["interval_sec"])
            iline, icol = rp.pos("interval_sec")
            if interval is None or interval < 1:
                self.add("DL604", f"{where}.interval_sec: must be a positive integer, got {rp['interval_sec']!r}",
                         iline, icol)
                interval = None
            elif interval > self.cfg.max_retry_interval_sec:
                self.add("DL604", f"{where}.interval_sec: {interval}s is above the maximum "
                                  f"{self.cfg.max_retry_interval_sec}s", iline, icol)
        else:
            self.add("DL602", f"{where}: retries {limit}x with no interval_sec", line, col)
        backoff = rp.get("backoff")
        mult = 2.0 if backoff is True else (float(backoff) if isinstance(backoff, (int, float))
                                            and not isinstance(backoff, bool) else 1.0)
        if "backoff" in rp and backoff is not False and mult <= 1.0:
            self.add("DL604", f"{where}.backoff: must be true/false or a multiplier greater than 1",
                     *rp.pos("backoff"))
            mult = 1.0
        cap = None
        if "max_interval_sec" in rp:
            cap = _int(rp["max_interval_sec"])
            mline, mcol = rp.pos("max_interval_sec")
            if cap is None or cap < 1:
                self.add("DL604", f"{where}.max_interval_sec: must be a positive integer", mline, mcol)
                cap = None
            elif interval and cap < interval:
                self.add("DL604", f"{where}.max_interval_sec ({cap}s) is shorter than interval_sec ({interval}s)",
                         mline, mcol)
        elif mult > 1.0:
            self.add("DL603", f"{where}: backoff without max_interval_sec grows without bound", line, col)
        codes = rp.get("exit_code")
        if isinstance(codes, list) and any(_int(c) == 0 for c in codes):
            self.add("DL605", f"{where}.exit_code: includes 0, so a successful step is retried",
                     *rp.pos("exit_code"))
        if timeouts and interval:
            step_timeout, dag_timeout = timeouts
            if dag_timeout and dag_timeout > 0:
                wait, cur = 0.0, float(interval)
                for _ in range(limit):
                    wait += min(cur, cap) if cap else cur
                    cur *= mult
                attempts = (limit + 1) * step_timeout if step_timeout else 0
                if wait + attempts > dag_timeout:
                    extra = f" plus {limit + 1} x {step_timeout}s attempts" if step_timeout else ""
                    self.add("DL606", f"{where}: worst case is {int(wait)}s of retry waits{extra}, more than the "
                                      f"DAG timeout_sec ({dag_timeout}s)", line, col)

    # --- secrets -------------------------------------------------------
    def check_secrets(self, dag):
        for key, value, line, col, path in _scalars(dag, ""):
            if not value.strip():
                continue
            if path.startswith("secrets["):
                continue  # references to an external provider, by design
            hit = secrets.known_pattern_match(value)
            if hit:
                self.sensitive.update((hit[1], value.strip()))
                self.add("DL402", f"{path}: looks like a {hit[0]} (value redacted, {len(value)} chars)", line, col)
                continue
            if key is not None and secrets.secret_key(str(key)) and not secrets.is_reference(value):
                if not _is_benign(str(key), value):
                    self.sensitive.add(value.strip())
                    self.add("DL401", f"{path}: literal value for a secret-looking key (value redacted); "
                                      "use `secrets:` or an env reference", line, col)
                    continue
            hits = secrets.high_entropy_tokens(value, self.cfg.entropy_min_length, self.cfg.entropy_threshold,
                                               self.cfg.secret_allow)
            if hits:
                self.sensitive.update(tok for _, _, tok in hits)
                n, e, _ = hits[0]
                self.add("DL403", f"{path}: contains a {n}-char token with entropy {e} bits/char "
                                  "(value redacted)", line, col)


def _is_benign(key: str, value: str) -> bool:
    # Boolean-ish/feature flags, file paths and numeric settings under
    # secret-ish names (`auth: none`, `password_file: /run/secrets/db`,
    # `token_ttl: 3600`) are not secrets.
    v = value.strip().lower()
    if v in ("true", "false", "none", "null", "", "required", "optional", "disabled", "enabled"):
        return True
    if re.search(r"(?:_file|_path|_env|_var|_ref|_name|_type|_url|_ttl|_len|_length|_min|_max|_size|_count|"
                 r"_timeout|_expiry|_expires|_sec|_seconds|_minutes|_hours|_days|_retries|_attempts|_port)$",
                 key, re.IGNORECASE):
        return not secrets.known_pattern(value)
    return False


def _scalars(obj, path, key=None, line=0, col=0):
    """Yield (key, value-as-str, line, col, path) for every non-boolean, non-null
    scalar, including env list entries of the KEY=value form (split so the key
    is checked)."""
    if isinstance(obj, LDict):
        for k, v in obj.items():
            kl, kc = obj.pos(k)
            yield from _scalars(v, f"{path}.{k}" if path else str(k), k, kl, kc)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            sub = f"{path}[{i}]"
            if isinstance(v, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", v):
                k, _, rest = v.partition("=")
                yield k, LStr(rest), getattr(v, "line", line), getattr(v, "col", col), f"{sub} ({k})"
            else:
                yield from _scalars(v, sub, key, getattr(v, "line", line), getattr(v, "col", col))
    elif isinstance(obj, str):
        yield key, obj, getattr(obj, "line", line) or line, getattr(obj, "col", col) or col, path
    elif obj is not None and not isinstance(obj, bool):
        # Numbers, dates, ...: `DB_PASSWORD: 1234567890` is still a literal secret.
        yield key, str(obj), line, col, path
