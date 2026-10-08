#!/usr/bin/env python3
"""
Price signals. Arithmetic over a price history, no prediction model, no API key.

This is what "analysis" means here. Three numbers, each computed from a plain CSV of
closing prices:

  trend       today's price against its own moving average. A long moving average is the
              one timing signal with real published support, and what support it has is for
              avoiding deep drawdowns rather than for beating a buy-and-hold return.
  drawdown    how far below its recent high the price is, which is what a dip buy triggers on.
  momentum    total return over a window. Reported, not acted on, because acting on it well
              is much harder than computing it.

Read this before you rely on it: none of these predict anything. A trend filter reduces the
chance of buying into a long decline and, in exchange, makes you late to every recovery. It
is a risk control, not an edge. A plain scheduled buy in rules.py rests on firmer ground
than any of these, precisely because it claims nothing.

Fail closed. If a signal is enabled but there is not enough history to compute it, the buy is
skipped and the reason is reported. Silently treating "unknown" as "fine" is how a filter
stops being a filter.

    python3 signals.py selftest              run the checks
    python3 signals.py show --ticker SPUS    print the signals for one ticker
"""

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_HISTORY = HERE / "prices.csv"
FIELDS = ["date", "ticker", "close"]


def load_history(path=DEFAULT_HISTORY):
    """Return {TICKER: [(date, close), ...]} sorted oldest first, bad rows skipped."""
    history, problems = {}, []
    path = Path(path)
    if not path.exists():
        return history, ["no price history at %s" % path]

    with open(path, newline="", encoding="utf-8") as handle:
        for number, row in enumerate(csv.DictReader(handle), start=2):
            try:
                when = date.fromisoformat((row.get("date") or "").strip())
                close = float(row.get("close"))
                ticker = (row.get("ticker") or "").strip().upper()
                if not ticker or close <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                problems.append("row %d is unusable" % number)
                continue
            history.setdefault(ticker, []).append((when, close))

    for ticker in history:
        history[ticker].sort(key=lambda pair: pair[0])
    return history, problems


def closes(series):
    return [close for _, close in series]


def sma(series, window):
    """Simple moving average of the last `window` closes, or None if history is too short."""
    values = closes(series)
    if window <= 0 or len(values) < window:
        return None
    return sum(values[-window:]) / window


def drawdown(series, lookback):
    """How far below the highest close in the window we are now, as a fraction.

    0.0 means at the high. 0.2 means 20 percent below it. None if history is too short.
    """
    values = closes(series)
    if lookback <= 0 or len(values) < lookback:
        return None
    window = values[-lookback:]
    peak = max(window)
    if peak <= 0:
        return None
    return (peak - window[-1]) / peak


def momentum(series, lookback):
    """Total return over the window, as a fraction. None if history is too short."""
    values = closes(series)
    if lookback <= 0 or len(values) <= lookback:
        return None
    then, now = values[-lookback - 1], values[-1]
    if then <= 0:
        return None
    return (now - then) / then


def evaluate(series, settings):
    """Every signal for one ticker, plus whether each could be computed at all."""
    window = int(settings.get("trend_window_days", 200))
    dip_window = int(settings.get("drawdown_window_days", 250))
    mom_window = int(settings.get("momentum_window_days", 250))

    average = sma(series, window)
    last = closes(series)[-1] if series else None
    return {
        "observations": len(series),
        "last_close": last,
        "trend_window": window,
        "sma": average,
        "above_sma": None if (average is None or last is None) else last > average,
        "drawdown": drawdown(series, dip_window),
        "momentum": momentum(series, mom_window),
    }


def gate_buy(ticker, history, settings):
    """Should a scheduled buy of this ticker go ahead? Returns (bool, reason).

    Only ever blocks. It cannot create a buy that the schedule did not already want.
    """
    if not settings.get("enabled", False):
        return True, "signals off"

    series = history.get(ticker.upper(), [])
    if not series:
        return False, "no price history for %s, and signals are enabled" % ticker

    verdict = evaluate(series, settings)

    if settings.get("require_above_sma", False):
        if verdict["above_sma"] is None:
            return False, ("need %d days of history for the trend filter, have %d"
                           % (verdict["trend_window"], verdict["observations"]))
        if not verdict["above_sma"]:
            return False, ("price %.2f is below its %d day average %.2f"
                           % (verdict["last_close"], verdict["trend_window"], verdict["sma"]))

    floor = settings.get("min_drawdown_to_buy")
    if floor is not None:
        if verdict["drawdown"] is None:
            return False, "not enough history to measure drawdown"
        if verdict["drawdown"] < float(floor):
            return False, ("only %.1f%% below its high, waiting for %.1f%%"
                           % (verdict["drawdown"] * 100, float(floor) * 100))

    return True, "signals pass"


# -------------------------------------------------------------------------- commands

def cmd_show(args, _):
    history, problems = load_history(args.history)
    for problem in problems[:5]:
        print("note: %s" % problem)
    if not history:
        print("\nNo price history yet. Nothing can be computed, and with signals enabled")
        print("every buy would be blocked. Fill prices.csv with date,ticker,close rows.")
        return 1
    tickers = [args.ticker.upper()] if args.ticker else sorted(history)
    for ticker in tickers:
        series = history.get(ticker, [])
        if not series:
            print("\n%s: no history" % ticker)
            continue
        verdict = evaluate(series, {})
        print("\n%s  %d observations, %s to %s"
              % (ticker, verdict["observations"], series[0][0], series[-1][0]))
        print("  last close   %.4f" % verdict["last_close"])
        for label, key, fmt in (("%d day average" % verdict["trend_window"], "sma", "%.4f"),
                                ("drawdown from high", "drawdown", "%.2f"),
                                ("momentum", "momentum", "%.2f")):
            value = verdict[key]
            print("  %-20s %s" % (label, "not enough history" if value is None else fmt % value))
        print("  above average        %s" % ("unknown" if verdict["above_sma"] is None
                                             else verdict["above_sma"]))
    return 0


def cmd_selftest(args, _):
    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    def series(values, start=date(2026, 1, 1)):
        return [(date.fromordinal(start.toordinal() + i), v) for i, v in enumerate(values)]

    rising = series([10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
    falling = series([19, 18, 17, 16, 15, 14, 13, 12, 11, 10])

    expect("sma over the last 5 of a rising series", abs(sma(rising, 5) - 17.0) < 1e-9)
    expect("sma is None when history is too short", sma(rising, 50) is None)
    expect("sma of zero window is None", sma(rising, 0) is None)

    expect("a rising series is above its average", closes(rising)[-1] > sma(rising, 5))
    expect("a falling series is below its average", closes(falling)[-1] < sma(falling, 5))

    expect("at the high, drawdown is zero", abs(drawdown(rising, 10) - 0.0) < 1e-9)
    expect("after falling from 19 to 10, drawdown is about 47%",
           abs(drawdown(falling, 10) - (9 / 19)) < 1e-9)
    expect("drawdown is None when history is too short", drawdown(rising, 99) is None)

    expect("momentum over 9 days of a rising series is 90%",
           abs(momentum(rising, 9) - 0.9) < 1e-9)
    expect("momentum is None when history is too short", momentum(rising, 10) is None)

    history = {"UP": rising, "DOWN": falling}

    # Disabled means never interferes.
    ok, _ = gate_buy("UP", {}, {"enabled": False})
    expect("with signals off, a buy passes even with no history at all", ok)

    # Fail closed on missing data. This is the one that matters.
    ok, reason = gate_buy("MISSING", history, {"enabled": True})
    expect("a ticker with no history is blocked, not waved through", not ok)
    expect("and it says why", "no price history" in reason)

    ok, reason = gate_buy("UP", history, {"enabled": True, "require_above_sma": True,
                                          "trend_window_days": 200})
    expect("too little history for the trend filter blocks the buy", not ok)
    expect("and names the shortfall", "need 200 days" in reason)

    # Trend filter.
    settings = {"enabled": True, "require_above_sma": True, "trend_window_days": 5}
    ok, _ = gate_buy("UP", history, settings)
    expect("an uptrend passes the trend filter", ok)
    ok, reason = gate_buy("DOWN", history, settings)
    expect("a downtrend is blocked", not ok)
    expect("and reports the average it failed against", "below its 5 day average" in reason)

    # Dip buying.
    dip = {"enabled": True, "min_drawdown_to_buy": 0.20, "drawdown_window_days": 10}
    ok, reason = gate_buy("UP", history, dip)
    expect("at its high, a dip buy waits", not ok and "waiting for" in reason)
    ok, _ = gate_buy("DOWN", history, dip)
    expect("47% below its high, a dip buy fires", ok)

    # Parsing.
    expect("an empty history parses to nothing rather than crashing",
           load_history("/nonexistent/prices.csv") == ({}, ["no price history at "
                                                            "/nonexistent/prices.csv"]))

    width = max(len(n) for n, _ in checks)
    for name, passed in checks:
        print("%s  %s" % ("pass" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in checks if not p]
    print("\n%d checks, %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--history", default=str(DEFAULT_HISTORY))
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    show = sub.add_parser("show")
    show.add_argument("--ticker")
    show.set_defaults(func=cmd_show)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args, None)


if __name__ == "__main__":
    sys.exit(main())
