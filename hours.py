#!/usr/bin/env python3
"""
Market hours. Do not send a market order into a closed exchange.

A market order placed outside trading hours does not fail. It queues, and fills at the next
open, at whatever price the market opens at. For a small weekly drip that barely matters. For
a one-off sell of a quarter of your portfolio it matters a great deal: an overnight gap of a
few percent on a large order is real money, decided by nothing you chose.

There is a second problem this catches, specific to a mixed portfolio. US equities and an
LSE-listed fund keep different hours. Selling a US stock at 20:00 UK time and buying the fund
with the proceeds cannot both happen: one queues overnight while the other does not, leaving
the account sitting in cash through a move it did not choose to be exposed to.

Trading 212 publishes the schedules. Each instrument carries a workingScheduleId, and
/equity/metadata/exchanges returns the schedule for each. So this is checkable rather than
guessable, which is the whole point.

Fails closed. If the schedule cannot be read, or the instrument's schedule is unknown, the
order is held rather than sent. An unknown market state is not an open one.

    python3 hours.py selftest
"""

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone


def iso_text(value):
    """An ISO 8601 time, rewritten so datetime.fromisoformat reads it on every Python here.

    Before 3.11, fromisoformat takes no "Z" and a fraction of exactly 3 or 6 digits, so
    ".12Z" or a nine-digit nanosecond stamp, which other brokers send, raised on 3.9 and
    parsed on 3.12: the same state could pass a cooldown on one Python and not the other.
    So a trailing Z becomes +00:00, and the seconds' fraction is padded or cut to 6 digits
    (cut, never rounded, so it cannot roll into the next second). Shared by
    gates.parse_time and parse_moment below, so both read a stamp the same way.
    """
    text = str(value).strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    return re.sub(r"(:\d{2})\.(\d+)",
                  lambda m: "%s.%s" % (m.group(1), (m.group(2) + "000000")[:6]), text, count=1)


def parse_moment(value):
    """Trading 212 returns ISO 8601 with an offset. Return an aware datetime, or None."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(iso_text(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def schedules_by_id(exchanges):
    """Flatten the exchanges response into {workingScheduleId: [(open, close), ...]}.

    The shape is exchanges -> workingSchedules -> timeEvents, and the field names vary
    slightly across the documented examples, so several spellings are accepted rather than
    assuming one and silently finding nothing.
    """
    out = {}
    for exchange in exchanges or []:
        if not isinstance(exchange, dict):
            continue
        for schedule in (exchange.get("workingSchedules") or []):
            identifier = schedule.get("id")
            if identifier is None:
                continue
            windows, opened = [], None
            for event in (schedule.get("timeEvents") or []):
                moment = parse_moment(event.get("date") or event.get("time"))
                kind = str(event.get("type") or "").upper()
                if moment is None:
                    continue
                if kind == "OPEN":
                    opened = moment
                elif kind == "CLOSE" and opened is not None:
                    windows.append((opened, moment))
                    opened = None
            if windows:
                out.setdefault(identifier, []).extend(windows)
    return out


def _minutes(text):
    hour, minute = (int(x) for x in str(text).split(":"))
    return hour * 60 + minute


def local_open(now, spec):
    """The fallback for a spec in the exchange's OWN clock: timezone, open_local, close_local,
    and optionally holidays, early_close and holidays_known_through.

    A fixed UTC window is right for half the year only. New York moves its clocks on dates
    London does not, so 13:30 to 20:00 UTC, the summer hours, would call Wall Street open an
    hour before its winter open and shut for its last hour, every day from the first Sunday
    of November to the second Sunday of March. In New York's own time the session is always
    09:30 to 16:00, so this asks the question there. Malformed or unknown, it answers None,
    which is_open turns into closed.
    """
    try:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo(spec["timezone"]))
        start, end = _minutes(spec["open_local"]), _minutes(spec["close_local"])
        early = {str(k): _minutes(v) for k, v in (spec.get("early_close") or {}).items()}
    except Exception:  # unknown zone, no tzdata, missing or malformed times
        return None, ""
    name, day = spec.get("name", "?"), local.date().isoformat()
    if local.weekday() >= 5:
        return False, "weekend in %s, per the configured fallback for %s" % (spec["timezone"],
                                                                            name)
    if day in {str(d) for d in spec.get("holidays") or []}:
        return False, "%s is a market holiday for %s" % (day, name)
    end = early.get(day, end)
    minutes = local.hour * 60 + local.minute
    window = "%02d:%02d to %02d:%02d %s time" % (start // 60, start % 60, end // 60, end % 60,
                                                  spec["timezone"])
    if not start <= minutes < end:
        return False, "closed per the configured fallback for %s, which trades %s" % (name,
                                                                                    window)
    known = str(spec.get("holidays_known_through") or "")
    caveat = ("" if known and day <= known else
              ", and its holiday list ends %s, so it does NOT know this year's holidays"
              % (known or "nowhere"))
    return True, "open per the configured fallback for %s, %s%s" % (name, window, caveat)


def fallback_open(now, spec):
    """A configured weekday window, for exchanges the API does not publish.

    Trading 212's exchanges endpoint returns European and Canadian venues but NOT NYSE or
    NASDAQ, so a US holding has a workingScheduleId that resolves to nothing. Without this,
    every US order is held forever, which is safe but useless.

    A spec with a timezone is read in the exchange's own clock, with its holidays; see
    local_open, which is what the default config uses for US shares. A spec with only
    open_utc and close_utc is the blunter instrument: weekday hours in UTC, no daylight
    saving and no holidays, so on Thanksgiving it says open. A market order on a holiday
    queues to the next open, which is the failure this whole module exists to avoid.
    """
    if not spec:
        return None, ""
    if spec.get("timezone"):
        return local_open(now, spec)
    if now.weekday() >= 5:
        return False, "weekend, per the configured fallback for %s" % spec.get("name", "?")
    try:
        start_h, start_m = (int(x) for x in str(spec["open_utc"]).split(":"))
        end_h, end_m = (int(x) for x in str(spec["close_utc"]).split(":"))
    except (KeyError, ValueError):
        return None, ""
    minutes = now.hour * 60 + now.minute
    if start_h * 60 + start_m <= minutes <= end_h * 60 + end_m:
        return True, ("open per the configured fallback for %s, %s to %s UTC, which does NOT "
                      "know holidays" % (spec.get("name", "?"), spec["open_utc"],
                                         spec["close_utc"]))
    return False, ("closed per the configured fallback for %s, which trades %s to %s UTC"
                   % (spec.get("name", "?"), spec["open_utc"], spec["close_utc"]))


def is_open(schedule_id, schedules, now, fallback=None):
    """(open?, detail). Unknown schedule means closed, unless a fallback is configured."""
    if schedule_id is None:
        return False, "the instrument carries no working schedule id"
    windows = schedules.get(schedule_id)
    if not windows:
        verdict, why = fallback_open(now, fallback)
        if verdict is not None:
            return verdict, why
        return False, "no published schedule for working schedule id %s" % schedule_id

    for start, end in sorted(windows):
        if start <= now <= end:
            return True, "open until %s" % end.isoformat(timespec="minutes")

    upcoming = sorted(start for start, _ in windows if start > now)
    if upcoming:
        wait = upcoming[0] - now
        return False, ("closed, next open %s, in %d hours"
                       % (upcoming[0].isoformat(timespec="minutes"),
                          int(wait.total_seconds() // 3600)))
    return False, "closed, and no future open is published in the returned window"


def overlap(schedule_ids, schedules, now, within_hours=24):
    """When are ALL these instruments tradeable at once?

    A rebalance that sells on one exchange and buys on another needs a window where both are
    open, or the two legs happen at different times and the account is exposed in between.
    """
    per_instrument = []
    for identifier in schedule_ids:
        windows = schedules.get(identifier)
        if not windows:
            return None, "no schedule for %s, so no overlap can be computed" % identifier
        per_instrument.append(sorted(windows))

    horizon = now + timedelta(hours=within_hours)
    best = None
    for start, end in per_instrument[0]:
        if end < now or start > horizon:
            continue
        low, high = max(start, now), end
        for other in per_instrument[1:]:
            hit = None
            for o_start, o_end in other:
                if o_start <= high and o_end >= low:
                    hit = (max(low, o_start), min(high, o_end))
                    break
            if hit is None:
                low = None
                break
            low, high = hit
        if low is not None and high > low:
            if best is None or low < best[0]:
                best = (low, high)
    if best is None:
        return None, "no window in the next %d hours has every exchange open at once" % within_hours
    return best, "all open %s to %s" % (best[0].isoformat(timespec="minutes"),
                                        best[1].isoformat(timespec="minutes"))


def cmd_selftest(args):
    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    def at(hour, day=13):
        return datetime(2026, 9, day, hour, 0, tzinfo=timezone.utc)

    exchanges = [{
        "name": "NASDAQ",
        "workingSchedules": [{
            "id": 1,
            "timeEvents": [
                {"date": "2026-09-13T13:30:00Z", "type": "OPEN"},
                {"date": "2026-09-13T20:00:00Z", "type": "CLOSE"},
                {"date": "2026-09-14T13:30:00Z", "type": "OPEN"},
                {"date": "2026-09-14T20:00:00Z", "type": "CLOSE"},
            ],
        }],
    }, {
        "name": "LSE",
        "workingSchedules": [{
            "id": 2,
            "timeEvents": [
                {"date": "2026-09-13T07:00:00Z", "type": "OPEN"},
                {"date": "2026-09-13T15:30:00Z", "type": "CLOSE"},
            ],
        }],
    }]

    schedules = schedules_by_id(exchanges)
    expect("both exchanges parse", set(schedules) == {1, 2})
    expect("NASDAQ has two sessions", len(schedules[1]) == 2)

    ok, why = is_open(1, schedules, at(15))
    expect("NASDAQ is open at 15:00 UTC", ok)
    ok, why = is_open(1, schedules, at(9))
    expect("NASDAQ is closed at 09:00 UTC", not ok and "next open" in why)
    expect("and says how long until it opens", "in 4 hours" in why)

    ok, why = is_open(2, schedules, at(9))
    expect("LSE is open at 09:00 UTC", ok)
    ok, _ = is_open(2, schedules, at(19))
    expect("LSE is closed at 19:00 UTC", not ok)

    # Fail closed, in every direction.
    ok, why = is_open(None, schedules, at(15))
    expect("no schedule id means closed", not ok and "no working schedule" in why)
    ok, why = is_open(99, schedules, at(15))
    expect("an unknown schedule id means closed", not ok and "no published schedule" in why)
    ok, _ = is_open(1, {}, at(15))
    expect("an empty schedule table means closed", not ok)
    expect("an unparseable exchanges payload yields nothing rather than crashing",
           schedules_by_id([{"workingSchedules": [{"id": 3, "timeEvents": [{"bad": 1}]}]}]) == {})

    # The overlap, which is the point for a mixed portfolio.
    window, why = overlap([1, 2], schedules, at(6))
    expect("US and LSE overlap exists", window is not None)
    expect("the overlap starts when the later one opens",
           window and window[0] == datetime(2026, 9, 13, 13, 30, tzinfo=timezone.utc))
    expect("and ends when the earlier one closes",
           window and window[1] == datetime(2026, 9, 13, 15, 30, tzinfo=timezone.utc))
    expect("which is only two hours",
           window and (window[1] - window[0]) == timedelta(hours=2))

    window, why = overlap([1, 99], schedules, at(6))
    expect("an unknown schedule kills the overlap", window is None and "no schedule" in why)

    lonely = schedules_by_id([exchanges[0]])
    window, why = overlap([1], lonely, at(6))
    expect("a single exchange overlaps with itself", window is not None)

    # The fallback, for exchanges Trading 212 does not publish. 13 September 2026 is a Sunday
    # and the 14th is a Monday, which is what makes these two cases meaningful.
    us = {"name": "US equities", "open_utc": "13:30", "close_utc": "20:00"}
    ok, why = is_open(71, schedules, at(15, 14), us)
    expect("an unpublished schedule falls back to configured hours", ok)
    expect("and the reply admits it does not know holidays", "does NOT" in why)
    ok, why = is_open(71, schedules, at(9, 14), us)
    expect("the fallback is closed before the open", not ok and "closed per" in why)
    ok, why = is_open(71, schedules, datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc), us)
    expect("the fallback is closed at the weekend", not ok and "weekend" in why)
    ok, why = is_open(71, schedules, at(15), None)
    expect("with no fallback configured it still fails closed", not ok)
    ok, _ = is_open(71, schedules, at(15), {"name": "broken", "open_utc": "nonsense"})
    expect("a malformed fallback fails closed rather than crashing", not ok)

    # In New York's own clock. US clocks go back on 1 November 2026, London's on 25 October,
    # so a fixed UTC window is wrong for half the year; these dates are the ones that matter.
    ny = {"name": "US equities", "timezone": "America/New_York", "open_local": "09:30",
          "close_local": "16:00", "holidays": ["2026-11-26"],
          "early_close": {"2026-11-27": "13:00"}, "holidays_known_through": "2027-12-31"}

    def utc(month, day, hour, minute=0, year=2026):
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)

    expect("summer: open at 13:45 UTC, which is 09:45 in New York",
           is_open(71, schedules, utc(10, 7, 13, 45), ny)[0])
    expect("summer: closed at 13:15 UTC, before the open",
           not is_open(71, schedules, utc(10, 7, 13, 15), ny)[0])
    ok, why = is_open(71, schedules, utc(11, 2, 14, 0), ny)
    expect("winter: closed at 14:00 UTC, 09:00 in New York", not ok and "closed per" in why)
    expect("which a fixed UTC window wrongly calls open",
           is_open(71, schedules, utc(11, 2, 14, 0), us)[0])
    expect("winter: open at 14:45 UTC", is_open(71, schedules, utc(11, 2, 14, 45), ny)[0])
    expect("winter: still open at 20:30 UTC, its last hour",
           is_open(71, schedules, utc(11, 2, 20, 30), ny)[0])
    expect("closed at 16:00 New York time exactly",
           not is_open(71, schedules, utc(11, 2, 21, 0), ny)[0])
    ok, why = is_open(71, schedules, utc(11, 26, 16, 0), ny)
    expect("Thanksgiving is a holiday, not a trading day", not ok and "holiday" in why)
    expect("the day after closes early, at 13:00 New York time",
           is_open(71, schedules, utc(11, 27, 17, 30), ny)[0]
           and not is_open(71, schedules, utc(11, 27, 18, 30), ny)[0])
    ok, why = is_open(71, schedules, utc(3, 1, 15, 0, 2028), ny)
    expect("past the end of its holiday list it trades but says it does not know them",
           ok and "does NOT know" in why)
    ok, why = is_open(71, schedules, utc(10, 10, 15, 0), ny)
    expect("a Saturday in New York is the weekend", not ok and "weekend" in why)
    expect("an unknown timezone fails closed",
           not is_open(71, schedules, utc(10, 7, 15, 0), dict(ny, timezone="Mars/Olympus"))[0])
    expect("missing local hours fail closed",
           not is_open(71, schedules, utc(10, 7, 15, 0), {"timezone": "America/New_York"})[0])

    # Stamps as brokers send them. Before Python 3.11 fromisoformat refused a Z and any
    # fraction that is not 3 or 6 digits, so these must parse the same on 3.9 as on 3.13.
    base = datetime(2026, 1, 15, 12, 34, 56, tzinfo=timezone.utc)
    for stamp, micro in (("2026-01-15T12:34:56Z", 0), ("2026-01-15T12:34:56.12Z", 120000),
                         ("2026-01-15T12:34:56.123Z", 123000),
                         ("2026-01-15T12:34:56.1234567Z", 123456),
                         ("2026-01-15T12:34:56.476614573Z", 476614),
                         ("2026-01-15T12:34:56.999999999+00:00", 999999),
                         ("2026-01-15T14:34:56.1+02:00", 100000),
                         ("2026-01-15T12:34:56.5z", 500000)):
        expect("%s parses, to the microsecond, on any Python" % stamp,
               parse_moment(stamp) == base.replace(microsecond=micro))
    expect("a stamp with no offset is read as UTC",
           parse_moment("2026-01-15T12:34:56.12") == base.replace(microsecond=120000))
    expect("a bare date parses to its midnight",
           parse_moment("2026-01-15") == datetime(2026, 1, 15, tzinfo=timezone.utc))
    expect("nonsense is None, not an exception",
           parse_moment("not a time") is None and parse_moment("") is None
           and parse_moment(None) is None)
    expect("iso_text leaves a 6-digit stamp as it was",
           iso_text("2026-01-15T12:34:56.123456+00:00") == "2026-01-15T12:34:56.123456+00:00")

    width = max(len(n) for n, _ in checks)
    for name, passed in checks:
        print("%s  %s" % ("pass" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in checks if not p]
    print("\n%d checks, %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
