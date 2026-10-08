#!/usr/bin/env python3
"""
Deterministic strategy. No prediction model, no API key, no running cost, ever.

It produces proposals in the shape gates.py expects, and they go through the same gates as
every other strategy's. It decides with arithmetic instead of judgement, which means it
costs nothing to run, cannot be talked into anything by what it reads, and can be tested.

It is also the honest version. Trading on public news is late by construction, because by
the time news is public the price has moved. Scheduled investing and rebalancing make no
prediction at all, which is why they survive contact with reality.

Two rules, both needing only your cash and your positions. Neither needs a price feed, so
neither depends on which broker endpoints turn out to work.

  Scheduled buy   buy a fixed amount of a ticker every N days. Cost averaging.
  Rebalance       hold target weights, correct them when they drift past a tolerance.

    python3 rules.py selftest      run the checks
    python3 rules.py explain       print the active rules in English
    python3 rules.py preview       show what it would propose against a sample account
"""

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"


def load(path=DEFAULT_CONFIG):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def as_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def scheduled_proposals(config, state, today, history=None):
    """Buy a fixed amount every N days. The only state it needs is when you last bought.

    If price signals are switched on, they can only ever BLOCK a buy the schedule already
    wanted. They cannot invent one. That keeps the schedule as the thing driving this, with
    the signal as a brake, which is the right way round: the schedule is the part with
    evidence behind it.
    """
    import signals

    signal_settings = config.get("strategy", {}).get("signals", {})
    history = history if history is not None else {}

    out, held_back = [], []
    for rule in config.get("strategy", {}).get("scheduled_buys", []):
        if not rule.get("enabled", False):
            continue
        ticker = str(rule.get("ticker", "")).upper()
        every = int(rule.get("every_days", 7))
        last = as_date((state.get("last_trade") or {}).get(ticker))
        due = last is None or (today - last).days >= every
        if not due:
            continue

        allowed, why = signals.gate_buy(ticker, history, signal_settings)
        if not allowed:
            held_back.append("%s: %s" % (ticker, why))
            continue
        out.append({
            "ticker": ticker,
            "action": "buy",
            "value": float(rule.get("value", 0)),
            # Not a prediction, so not a probability. A schedule is either due or it is not.
            "confidence": 1.0,
            "rationale": ("scheduled buy, every %d days, last was %s"
                          % (every, last.isoformat() if last else "never")),
        })
    return out, held_back


def rebalance_proposals(config, state):
    """Correct drift back to target weights, but only past a tolerance.

    Rebalancing on every tiny wobble just pays the spread repeatedly, so a drift under the
    tolerance is left alone, and a correction smaller than min_trade_value is not worth
    placing at all.
    """
    settings = config.get("strategy", {}).get("rebalance", {})
    if not settings.get("enabled", False):
        return []

    targets = {str(k).upper(): float(v) for k, v in (settings.get("targets") or {}).items()}
    if not targets:
        return []

    positions = state.get("positions", {})
    total = sum(float(p.get("value", 0)) for p in positions.values())
    if settings.get("include_cash_in_total", False):
        total += float(state.get("cash", 0))
    if total <= 0:
        return []

    tolerance = float(settings.get("drift_tolerance", 0.05))
    minimum = float(settings.get("min_trade_value", 10.0))

    out = []
    for ticker, target in sorted(targets.items()):
        held = float(positions.get(ticker, {}).get("value", 0))
        drift = (held / total) - target
        if abs(drift) <= tolerance:
            continue
        amount = abs(drift) * total
        if amount < minimum:
            continue
        out.append({
            "ticker": ticker,
            "action": "sell" if drift > 0 else "buy",
            "value": round(amount, 2),
            "confidence": 1.0,
            "rationale": ("rebalance: %.1f%% held against a %.1f%% target, drift %.1f%%"
                          % (held / total * 100, target * 100, drift * 100)),
        })
    return out


def propose(config, state, today, history=None):
    """Everything the deterministic strategy wants to do, in gates.py's proposal shape.

    A buy the signals held back is reported in notes rather than dropped silently. A filter
    you cannot see working is indistinguishable from one that is broken.
    """
    scheduled, held_back = scheduled_proposals(config, state, today, history)
    return {
        "proposals": scheduled + rebalance_proposals(config, state),
        "notes": "held back by signals: " + "; ".join(held_back) if held_back else "",
        "model": "none, deterministic rules",
    }


# -------------------------------------------------------------------------- commands

def cmd_explain(args, config):
    strategy = config.get("strategy", {})
    print("Strategy mode: %s" % strategy.get("mode", "rules"))
    print("Cost to run:   nothing. It is arithmetic, with no outside service.\n")

    scheduled = [r for r in strategy.get("scheduled_buys", [])]
    print("Scheduled buys")
    if not any(r.get("enabled") for r in scheduled):
        print("  none enabled")
    for rule in scheduled:
        print("  %s %s  %.2f every %d days"
              % ("[on] " if rule.get("enabled") else "[off]", rule.get("ticker"),
                 float(rule.get("value", 0)), int(rule.get("every_days", 0))))

    rebalance = strategy.get("rebalance", {})
    print("\nRebalance  %s" % ("[on]" if rebalance.get("enabled") else "[off]"))
    targets = rebalance.get("targets") or {}
    if not targets:
        print("  no targets set")
    for ticker, weight in sorted(targets.items()):
        print("  %-8s %.0f%%" % (ticker, float(weight) * 100))
    if targets:
        total = sum(float(v) for v in targets.values())
        print("  total %.0f%%%s" % (total * 100,
                                    "" if abs(total - 1.0) < 1e-9 else "  <-- does not sum to 100"))
        print("  tolerance %.0f%%, minimum trade %.2f"
              % (float(rebalance.get("drift_tolerance", 0)) * 100,
                 float(rebalance.get("min_trade_value", 0))))
    return 0


def cmd_preview(args, config):
    state = {"cash": 200.0, "positions": {"SPUS": {"value": 300.0}, "HLAL": {"value": 100.0}},
             "last_trade": {}}
    print("Against a sample account: 200.00 cash, SPUS 300.00, HLAL 100.00, nothing traded yet\n")
    result = propose(config, state, date.today())
    if not result["proposals"]:
        print("nothing proposed. Enable a rule in config.json under strategy.")
    for p in result["proposals"]:
        print("  %-4s %-8s %8.2f   %s" % (p["action"], p["ticker"], p["value"], p["rationale"]))
    return 0


def cmd_selftest(args, config):
    cfg = {"strategy": {
        "scheduled_buys": [
            {"ticker": "SPUS", "value": 25.0, "every_days": 7, "enabled": True},
            {"ticker": "HLAL", "value": 25.0, "every_days": 7, "enabled": False},
        ],
        "rebalance": {"enabled": True, "targets": {"SPUS": 0.6, "HLAL": 0.4},
                      "drift_tolerance": 0.05, "min_trade_value": 10.0},
    }}
    today = date(2026, 9, 11)
    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    def sched(conf, state, history=None):
        return scheduled_proposals(conf, state, today, history)[0]

    def held(conf, state, history=None):
        return scheduled_proposals(conf, state, today, history)[1]

    # Scheduled buys.
    got = sched(cfg, {"last_trade": {}})
    expect("a never-bought ticker is due immediately", len(got) == 1 and got[0]["ticker"] == "SPUS")
    expect("a disabled rule stays silent", all(p["ticker"] != "HLAL" for p in got))
    expect("a schedule is not a prediction, so confidence is 1", got[0]["confidence"] == 1.0)

    recent = {"last_trade": {"SPUS": "2026-09-08"}}
    expect("3 days after a buy it is not due", not sched(cfg, recent))
    due = {"last_trade": {"SPUS": "2026-09-04"}}
    expect("7 days after a buy it is due again", len(sched(cfg, due)) == 1)
    expect("a malformed last_trade is treated as never bought",
           len(sched(cfg, {"last_trade": {"SPUS": "not a date"}})) == 1)

    # Signals can only brake a scheduled buy, never create one.
    from datetime import date as _date
    downtrend = {"SPUS": [(_date.fromordinal(_date(2026, 1, 1).toordinal() + i), v)
                          for i, v in enumerate([19, 18, 17, 16, 15, 14, 13, 12, 11, 10])]}
    gated = dict(cfg["strategy"], signals={"enabled": True, "require_above_sma": True,
                                           "trend_window_days": 5})
    gated_cfg = {"strategy": gated}
    expect("signals can block a due buy", not sched(gated_cfg, {"last_trade": {}}, downtrend))
    expect("and the reason is reported, not swallowed",
           any("below its 5 day average" in h for h in held(gated_cfg, {"last_trade": {}},
                                                           downtrend)))
    expect("signals cannot create a buy that is not due",
           not sched(gated_cfg, recent, downtrend))
    expect("missing history blocks rather than waves through",
           not sched(gated_cfg, {"last_trade": {}}, {}))

    # Rebalance.
    balanced = {"positions": {"SPUS": {"value": 60.0}, "HLAL": {"value": 40.0}}, "cash": 0}
    expect("a portfolio already on target proposes nothing",
           not rebalance_proposals(cfg, balanced))

    drifted = {"positions": {"SPUS": {"value": 80.0}, "HLAL": {"value": 20.0}}, "cash": 0}
    got = rebalance_proposals(cfg, drifted)
    by_ticker = {p["ticker"]: p for p in got}
    expect("an overweight holding is sold", by_ticker.get("SPUS", {}).get("action") == "sell")
    expect("an underweight holding is bought", by_ticker.get("HLAL", {}).get("action") == "buy")
    expect("the correction is the size of the drift",
           abs(by_ticker["SPUS"]["value"] - 20.0) < 0.01)

    small = {"positions": {"SPUS": {"value": 62.0}, "HLAL": {"value": 38.0}}, "cash": 0}
    expect("drift inside the tolerance is left alone", not rebalance_proposals(cfg, small))

    tiny = {"positions": {"SPUS": {"value": 8.0}, "HLAL": {"value": 1.0}}, "cash": 0}
    expect("a correction below the minimum trade is skipped",
           not any(p["value"] < 10.0 for p in rebalance_proposals(cfg, tiny)))

    expect("an empty portfolio proposes no rebalance",
           not rebalance_proposals(cfg, {"positions": {}, "cash": 0}))

    off = {"strategy": {"rebalance": dict(cfg["strategy"]["rebalance"], enabled=False)}}
    expect("a disabled rebalance stays silent", not rebalance_proposals(off, drifted))
    expect("no targets means no rebalance",
           not rebalance_proposals({"strategy": {"rebalance": {"enabled": True, "targets": {}}}},
                                   drifted))

    # The whole thing together, and the shape gates.py expects.
    combined = propose(cfg, dict(drifted, last_trade={}), today)
    expect("scheduled and rebalance proposals combine", len(combined["proposals"]) == 3)
    expect("every proposal has the fields gates.py requires",
           all(set(p) >= {"ticker", "action", "value", "confidence", "rationale"}
               for p in combined["proposals"]))
    expect("no model is named", combined["model"] == "none, deterministic rules")

    width = max(len(n) for n, _ in checks)
    for name, passed in checks:
        print("%s  %s" % ("pass" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in checks if not p]
    print("\n%d checks, %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    sub.add_parser("explain").set_defaults(func=cmd_explain)
    sub.add_parser("preview").set_defaults(func=cmd_preview)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args, load(args.config))


if __name__ == "__main__":
    sys.exit(main())
