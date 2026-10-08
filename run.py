#!/usr/bin/env python3
"""
The launcher: one command for every way of running the bot, on any computer.

    python3 run.py once [--execute]          one run of trade.py, then exit
    python3 run.py loop [--execute] [--now]  a run at each of config.json schedule.utc_times
    python3 run.py selftest [MODULE ...]     every module's selftest, each in its own process
    python3 run.py doctor                    Python, time zones, config, broker, credentials,
                                             and which order ledger the account uses

once, loop and doctor also take --config PATH, and --env demo|live for Trading 212 (it
overrides execution.environment). Standard library only; on Windows, pip install -r
requirements.txt adds the time zone data Python lacks there.

Without --execute nothing is placed: a run only proposes. With it, an order still needs
execute true in config.json, no HALT file, and every gate in gates.py. Which account the
orders reach is config.json broker.adapter: "paper" is simulated, no real money;
"trading212" with execution.environment "live" is real money.

loop survives a failed run: the error is printed and it waits for the next time. It reads
the schedule again after every run, so a schedule edit needs no restart (a code change
does). Ctrl+C stops it. To stop orders but keep it running, create an empty file named HALT.
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
DEFAULT_CONFIG = HERE / "config.json"

# Building blocks first, the runner last, then the paper broker.
MODULES = ["sizing", "signals", "rules", "gates", "hours", "pricefeed", "backtest", "momentum",
           "universe", "research", "broker", "trade", "brokers/paper"]
SECTIONS = ("broker", "compliance", "risk", "execution", "strategy")
TALLY = re.compile(r"(\d+) checks?, (\d+) failed")


def utcnow():
    return datetime.now(timezone.utc)


def say(message):
    print("%s  %s" % (utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"), message), flush=True)


def read_config(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def trade_argv(args):
    argv = ["--config", str(args.config)]
    if getattr(args, "env", None):
        argv += ["--env", args.env]
    if args.execute:
        argv.append("--execute")
    return argv


# ---------------------------------------------------------------------- the schedule

def schedule(config):
    """(sorted [(hour, minute)] in UTC, weekdays_only) from config.json's schedule.
    A bad entry is refused, not skipped: a loop quietly missing a run is worse than one
    that says why it cannot start."""
    block = config.get("schedule")
    times = block.get("utc_times") if isinstance(block, dict) else None
    if not isinstance(times, list) or not times:
        raise ValueError("config.json has no schedule.utc_times list")
    parsed = set()
    for text in times:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(text).strip())
        if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise ValueError("schedule.utc_times has %r, which is not HH:MM in 24-hour UTC"
                             % (text,))
        parsed.add((int(match.group(1)), int(match.group(2))))
    return sorted(parsed), bool(block.get("weekdays_only", True))


def next_run(now, times, weekdays_only):
    """The first scheduled moment strictly after now, an aware UTC datetime."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(8):
        day = midnight + timedelta(days=offset)
        if weekdays_only and day.weekday() >= 5:
            continue
        for hour, minute in times:
            when = day.replace(hour=hour, minute=minute)
            if when > now:
                return when
    raise ValueError("no scheduled time in the coming week")


def sleep_until(when, clock=utcnow, sleep=time.sleep):
    """Wait in steps of at most a minute, so Ctrl+C is prompt and a suspended laptop
    wakes to the right time rather than finishing a stale long sleep."""
    while True:
        left = (when - clock()).total_seconds()
        if left <= 0:
            return
        sleep(min(60.0, left))


def guarded_run(argv, runner=None):
    """One run of trade.main. Whatever goes wrong inside it is printed and the loop goes
    on; only Ctrl+C (KeyboardInterrupt) gets through."""
    try:
        if runner is None:
            import trade
            runner = trade.main
        code = runner(argv)
    except SystemExit as stop:
        # trade.py stops with a message on, for example, missing credentials.
        code = stop.code
        if not isinstance(code, int) and code is not None:
            print(code, file=sys.stderr)
            code = 1
    except Exception:
        traceback.print_exc()
        code = 1
    say("run finished, exit status %s" % (code or 0))
    return code or 0


def stop_on_sigterm():
    """docker stop sends SIGTERM, which Python as a container's first process would ignore.
    Treat it as Ctrl+C. An order cut off mid-run is safe: the ledger is written first."""
    def handler(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, handler)
    except (AttributeError, ValueError, OSError):
        pass


# ---------------------------------------------------------------------- commands

def cmd_once(args):
    import trade
    return trade.main(trade_argv(args))


def cmd_loop(args):
    argv = trade_argv(args)
    stop_on_sigterm()
    try:
        say("loop started, %s. Ctrl+C stops it."
            % ("--execute: orders are placed when config.json and every gate allow"
               if args.execute
               else "no --execute: it proposes and places nothing"))
        if args.now:
            say("running now (--now)")
            guarded_run(argv)
        while True:
            try:
                times, weekdays_only = schedule(read_config(args.config))
            except (OSError, ValueError) as problem:
                say("cannot read the schedule (%s); trying again in 5 minutes" % problem)
                sleep_until(utcnow() + timedelta(minutes=5))
                continue
            now = utcnow()
            when = next_run(now, times, weekdays_only)
            wait = int((when - now).total_seconds())
            say("next run %s UTC, in %dh %02dm" % (when.strftime("%a %Y-%m-%d %H:%M"),
                                                  wait // 3600, wait % 3600 // 60))
            sleep_until(when)
            say("running")
            guarded_run(argv)
    except KeyboardInterrupt:
        say("stopped")
        return 0


def module_path(name):
    """("brokers/paper", its file) for "brokers/paper", "brokers/paper.py" or brokers\\paper."""
    name = name.replace("\\", "/")
    name = name[:-3] if name.endswith(".py") else name
    return name, HERE.joinpath(*(name + ".py").split("/"))


def own_checks():
    """run.py's own checks: the schedule, the waiting, and a failed run not ending the loop."""
    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    def at(day, hour, minute, second=0):
        # October 2026: the 5th is a Monday, the 9th a Friday, the 10th a Saturday.
        return datetime(2026, 10, day, hour, minute, second, tzinfo=timezone.utc)

    times = [(8, 17), (13, 47), (20, 47)]
    expect("Monday before the first time: that day's first time",
           next_run(at(5, 7, 0), times, True) == at(5, 8, 17))
    expect("exactly at a time: the next one, never the same one again",
           next_run(at(5, 8, 17), times, True) == at(5, 13, 47))
    expect("Friday after the last time, weekdays only: Monday's first",
           next_run(at(9, 21, 0), times, True) == at(12, 8, 17))
    expect("Saturday, weekdays only: Monday's first",
           next_run(at(10, 10, 0), times, True) == at(12, 8, 17))
    expect("Friday after the last time, every day: Saturday's first",
           next_run(at(9, 21, 0), times, False) == at(10, 8, 17))
    expect("schedule reads, sorts and de-duplicates HH:MM, weekdays only by default",
           schedule({"schedule": {"utc_times": ["13:47", "8:17", "08:17"]}})
           == ([(8, 17), (13, 47)], True))
    for bad in (["24:00"], ["08:60"], ["nine"], [], "08:17", None):
        try:
            schedule({"schedule": {"utc_times": bad}})
            refused = False
        except ValueError:
            refused = True
        expect("schedule refuses %r" % (bad,), refused)

    clock = [at(5, 8, 0)]
    naps = []

    def nap(seconds):
        naps.append(seconds)
        clock[0] += timedelta(seconds=seconds)

    sleep_until(at(5, 8, 17, 30), clock=lambda: clock[0], sleep=nap)
    expect("waits in steps of at most 60 s and stops on time",
           naps and max(naps) <= 60 and clock[0] == at(5, 8, 17, 30))

    def boom(argv):
        raise RuntimeError("broken run")

    def stop(argv):
        raise SystemExit("T212_API_KEY not set")

    def interrupted(argv):
        raise KeyboardInterrupt

    devnull = open(os.devnull, "w")
    saved, sys.stderr, sys.stdout = (sys.stderr, sys.stdout), devnull, devnull
    try:
        expect("a run that raises is reported, status 1, and the loop goes on",
               guarded_run([], boom) == 1)
        expect("a run that stops with a message is status 1, and the loop goes on",
               guarded_run([], stop) == 1)
        expect("a run's own exit status is passed back", guarded_run([], lambda a: 3) == 3)
        try:
            guarded_run([], interrupted)
            passed_through = False
        except KeyboardInterrupt:
            passed_through = True
        expect("Ctrl+C during a run stops the loop", passed_through)
    finally:
        sys.stderr, sys.stdout = saved
        devnull.close()

    args = argparse.Namespace(config="c.json", env="demo", execute=True)
    expect("once and loop pass --config, --env and --execute to trade.py",
           trade_argv(args) == ["--config", "c.json", "--env", "demo", "--execute"])
    args = argparse.Namespace(config="c.json", env=None, execute=False)
    expect("without --execute, trade.py is not asked to place anything",
           trade_argv(args) == ["--config", "c.json"])

    # The doctor's ledger line: the configured account's own file, and a damaged one warned
    # about rather than read as empty.
    try:
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            paper = ledger_check({"adapter": "paper"}, "live", folder)
            demo = ledger_check({"adapter": "trading212"}, "demo", folder)
            live = ledger_check({"adapter": "trading212"}, "live", folder)
            expect("doctor names each account's own ledger, none written yet",
                   paper[0] == demo[0] == live[0] == "ok"
                   and "orders_placed.paper.json" in paper[1]
                   and "orders_placed.demo.json" in demo[1]
                   and "orders_placed.json" in live[1] and "not written yet" in live[1])
            (folder / "orders_placed.paper.json").write_text(
                '{"2026-10-05|AAA_US_EQ|buy": {"quantity": 1.0, "value": 100.0}}',
                encoding="utf-8")
            level, text = ledger_check({"adapter": "paper"}, "demo", folder)
            expect("a ledger that reads is ok, with its entry count",
                   level == "ok" and "1 entry" in text)
            (folder / "orders_placed.demo.json").write_text('{"2026-10-05|AAA', encoding="utf-8")
            level, text = ledger_check({"adapter": "trading212"}, "demo", folder)
            expect("a cut-off ledger is a warning that names it, not an empty ledger",
                   level == "warn" and "orders_placed.demo.json" in text)
            (folder / "orders_placed.json").write_text("", encoding="utf-8")
            level, text = ledger_check({"adapter": "trading212"}, "live", folder)
            expect("an empty ledger file is a warning too", level == "warn")
            expect("and another account's damaged ledger does not touch this one's line",
                   ledger_check({"adapter": "paper"}, "live", folder)[0] == "ok")
    except Exception as error:
        expect("the doctor's ledger check runs (%s: %s)" % (type(error).__name__, error), False)
    return checks


def selftest_module(name, env):
    """(checks, failed, lines worth showing) for one module's selftest, in its own process.
    A module that crashes, times out or never prints its count is one failed check."""
    name, path = module_path(name)
    if not path.exists():
        return 0, 1, ["no such file: %s" % path]
    try:
        done = subprocess.run([sys.executable, str(path), "selftest"], cwd=str(HERE), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              encoding="utf-8", errors="replace", timeout=900)
    except subprocess.TimeoutExpired:
        return 0, 1, ["timed out after 900 s"]
    lines = done.stdout.rstrip().splitlines()
    tallies = TALLY.findall(done.stdout)
    count, bad = (int(tallies[-1][0]), int(tallies[-1][1])) if tallies else (0, 0)
    if done.returncode == 0 and tallies and not bad:
        return count, 0, []
    notes = [line for line in lines if line.startswith("FAIL")] or lines[-15:]
    if not tallies:
        notes.append("printed no 'N checks, M failed' line")
    if done.returncode != 0:
        notes.append("exit status %s" % done.returncode)
    return count, max(bad, 1), notes


def cmd_selftest(args):
    names = args.modules or MODULES
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    own = own_checks()
    rows = [("run.py", len(own), sum(1 for _, p in own if not p),
             ["FAIL  %s" % n for n, p in own if not p], 0.0)]
    print_row(rows[0])
    for name in names:
        started = time.time()
        rows.append((module_path(name)[0],) + selftest_module(name, env)
                    + (time.time() - started,))
        print_row(rows[-1])
    broken = sum(1 for row in rows if row[2])
    print("\n%d of %d passed" % (len(rows) - broken, len(rows)))
    print("%d checks, %d failed" % (sum(row[1] for row in rows), sum(row[2] for row in rows)))
    return 1 if broken else 0


def print_row(row):
    name, count, bad, notes, seconds = row
    print("%-16s %-4s  %d checks, %d failed  (%.1f s)"
          % (name, "FAIL" if bad else "pass", count, bad, seconds), flush=True)
    for line in notes:
        print("    %s" % line)


def ledger_check(broker_section, environment, directory=HERE):
    """(level, text) for the doctor: which order ledger the configured account uses, and
    whether it reads. One file per account (sizing.ledger_path), so a paper morning never
    counts against a real account's daily cap. A file that exists but does not read is a
    warning, not a quiet empty ledger: sizing.load_ledger refuses it, and so a run places
    nothing until it is restored."""
    import sizing
    path = sizing.ledger_path(dict(broker_section or {}, environment=environment), directory)
    if not path.exists():
        return "ok", ("order ledger %s, not written yet (the first order sent creates it)"
                      % path.name)
    try:
        entries = len(sizing.load_ledger(path))
    except ValueError as error:
        return "warn", ("%s. No run places an order until it reads again: restore it from git "
                        "or a backup, and never edit it to make something pass" % error)
    return "ok", "order ledger %s reads, %d entr%s" % (path.name, entries,
                                                      "y" if entries == 1 else "ies")


def cmd_doctor(args):
    """Everything a run needs that can be checked without the network."""
    problems = []

    def report(level, text):
        print("%-7s %s" % (level, text))
        if level == "PROBLEM":
            problems.append(text)

    version = "%d.%d.%d" % sys.version_info[:3]
    if sys.version_info >= (3, 9):
        report("ok", "Python %s" % version)
    else:
        report("PROBLEM", "Python %s is too old: 3.9 or newer is needed" % version)

    try:
        from zoneinfo import ZoneInfo
        for zone in ("America/New_York", "Europe/London"):
            ZoneInfo(zone)
        report("ok", "time zone data loads (America/New_York, Europe/London)")
    except Exception as error:
        report("PROBLEM", "no time zone data (%s: %s). Run: pip install tzdata (Windows needs "
               "it; pip install -r requirements.txt does the same)"
               % (type(error).__name__, error))

    config = None
    path = Path(args.config)
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            report("PROBLEM", "%s starts with a byte order mark: save it as UTF-8 without one"
                   % path.name)
        else:
            config = json.loads(raw.decode("utf-8"))
            missing = [s for s in SECTIONS if not isinstance(config.get(s), dict)]
            if missing:
                report("PROBLEM", "%s has no %s section" % (path.name, ", ".join(missing)))
                config = None
            else:
                report("ok", "%s parses" % path)
    except (OSError, ValueError) as error:
        report("PROBLEM", "cannot read %s: %s" % (path, error))

    modules = None
    try:
        import broker
        import gates
        import trade  # noqa: F401  imports momentum, sizing and the rest of a run
        modules = (broker, gates)
        report("ok", "the bot's modules import")
    except Exception as error:
        report("PROBLEM", "the bot's modules do not import: %s: %s"
               % (type(error).__name__, error))

    if config is not None and modules is not None:
        broker, gates = modules
        section = config["broker"]
        name = broker.adapter_name(section)
        environment = (getattr(args, "env", None)
                       or config["execution"].get("environment", "demo"))
        loaded, plugin = False, None
        try:
            plugin, loaded = broker.adapter(section), True
            report("ok", "broker adapter %s (%s)" % (
                name, "built in" if plugin is None else plugin.__file__))
        except (Exception, SystemExit) as error:
            report("PROBLEM", "broker adapter %s does not load: %s" % (name, error))

        if loaded:
            if plugin is None or getattr(plugin, "NEEDS_CREDENTIALS", True):
                auth = section.get("auth", {})
                for var in (auth.get("key_env_var", "T212_API_KEY"),
                            auth.get("secret_env_var", "T212_API_SECRET")):
                    value = broker.secret_value(var)
                    if value.strip():
                        report("ok", "%s is set (in the %s; the value is not shown)"
                               % (var, "environment" if os.environ.get(var) else ".env file"))
                        for note in broker.key_warnings(var, value):
                            report("warn", note)
                    else:
                        report("PROBLEM", "%s is not set: export it, or put it in a .env file "
                               "here (see .env.example)" % var)
            else:
                report("ok", "no credentials needed by the %s adapter" % name)

            if plugin is None and environment == "live":
                report("note", "account: Trading 212 LIVE, REAL MONEY")
            elif plugin is None:
                report("note", "account: Trading 212 %s, practice money" % environment)
            elif name == "paper":
                report("note", "account: paper, simulated, no real money")
            else:
                report("note", "account: %s; whether that is real money is up to brokers/%s.py"
                       % (getattr(plugin, "LABEL", name), name))

        try:
            report(*ledger_check(section, environment))
        except Exception as error:
            report("warn", "cannot check the order ledger: %s: %s"
                   % (type(error).__name__, error))

        if config["execution"].get("execute"):
            report("note", "execute is true: a run started with --execute places the orders "
                   "that pass every gate; without --execute it only proposes")
        else:
            report("note", "execute is false in config.json: every run only proposes, even "
                   "with --execute")
        momentum = config["strategy"].get("momentum", {})
        if momentum.get("enabled"):
            report("note", "momentum book %s" % (
                "live: its orders are sent" if momentum.get("mode") == "live"
                else "in shadow mode: worked out and printed, never sent"))

        halt = gates.killswitch_path(config)
        if halt.exists():
            report("warn", "%s is present: no run places anything until it is deleted"
                   % halt.name)
        else:
            report("ok", "no %s file (create one, empty, to stop all orders)" % halt.name)

        try:
            times, weekdays_only = schedule(config)
            report("ok", "schedule: %d times a %s, UTC, next %s" % (
                len(times), "weekday" if weekdays_only else "day",
                next_run(utcnow(), times, weekdays_only).strftime("%a %H:%M")))
        except ValueError as error:
            report("PROBLEM", "run.py loop cannot run: %s" % error)

    try:
        with tempfile.NamedTemporaryFile(dir=str(HERE), prefix=".doctor-", suffix=".tmp") as f:
            f.write(b"ok")
        report("ok", "the project folder is writable (the order ledger is kept there)")
    except OSError as error:
        report("PROBLEM", "the project folder is not writable, and the order ledger and "
               "paper account are kept there: %s" % error)

    print("\n%s" % ("nothing blocks a run" if not problems
                    else "%d problem(s) block a run" % len(problems)))
    return 1 if problems else 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        # A Windows console cannot print every character: replace those, never crash.
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    for name, func, text in (("once", cmd_once, "one run of trade.py"),
                             ("loop", cmd_loop, "a run at each scheduled time, forever")):
        entry = sub.add_parser(name, help=text)
        entry.add_argument("--config", default=str(DEFAULT_CONFIG))
        entry.add_argument("--env", choices=("demo", "live"),
                           help="Trading 212 only: override execution.environment")
        entry.add_argument("--execute", action="store_true",
                           help="place the orders that survive every check")
        if name == "loop":
            entry.add_argument("--now", action="store_true", help="run once at start too")
        entry.set_defaults(func=func)
    entry = sub.add_parser("selftest", help="every module's selftest")
    entry.add_argument("modules", nargs="*", help="only these, e.g. gates trade brokers/paper")
    entry.set_defaults(func=cmd_selftest)
    entry = sub.add_parser("doctor", help="environment and config checks, no network")
    entry.add_argument("--config", default=str(DEFAULT_CONFIG))
    entry.add_argument("--env", choices=("demo", "live"),
                       help="Trading 212 only: check as if execution.environment were this")
    entry.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
