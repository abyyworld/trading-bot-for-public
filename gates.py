#!/usr/bin/env python3
"""
The gates every proposed trade has to pass.

Nothing here touches the network or reads a credential. It takes a list of proposals, the
current account state, and the config, and returns what is allowed and why everything else
was not. That makes it testable, and it means the safety of this system does not depend on
any strategy behaving.

The ordering matters. The strategies (rules.py, momentum.py) run first and produce proposals.
This runs second and throws away every one that fails a check. A proposal that reaches the
broker has passed every check below, so a strategy with a bug, or one fed bad data, still
cannot get an order out: it can only ever nominate a ticker that was already on the allowlist
(or among this month's screened momentum picks), at a size the risk block already permits.

    python3 gates.py selftest      run the checks on the checks
    python3 gates.py explain       print the active limits in English
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import hours  # pure arithmetic on times, no network: the shared ISO 8601 reader

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"

ACTIONS = ("buy", "sell")


def load(path=DEFAULT_CONFIG):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def killswitch_path(config, directory=HERE):
    return Path(directory) / config["risk"].get("killswitch_file", "HALT")


def allowlist(config):
    """The set of tickers that may be BOUGHT. Selling is deliberately not restricted."""
    return {entry["ticker"].strip().upper()
            for entry in config["compliance"].get("allowlist", [])
            if entry.get("ticker")}


def extra_allowlist(state, proposal):
    """Tickers made buyable for this run only, by trade.py, from the momentum targets: the
    current month's picks, each passed by the compliance screen when they were built. They
    admit MOMENTUM proposals only, so a scheduled buy or any other source can never ride in
    on them. Static entries still come only from config, and selling is never restricted."""
    if proposal.get("source") != "momentum":
        return set()
    return {str(t).strip().upper() for t in (state.get("extra_allowlist") or ()) if t}


def parse_time(value):
    """Parse to an AWARE UTC datetime, or None. Never returns a naive one.

    This normalising is the whole point. Trading 212 stamps order history with an offset
    ("2026-01-15T14:30:00+00:00"), while `now` and the selftests are naive, and subtracting
    one from the other raises TypeError. That only fires once an instrument HAS a previous
    trade to compare against, so a whole selftest suite can pass while a live run crashes
    after its first real order: the worst possible moment, because from then on every run
    dies in the same place and the bot silently stops trading.

    A naive value is read as UTC: every producer here is UTC already (the API sends an
    offset, the runner clock is UTC), so this attaches the truth rather than guessing.

    The text goes through hours.iso_text first, so a Z and a fraction of any length read
    the same on Python 3.9 as on 3.13. Unreadable is None, and check() then holds the
    instrument rather than treating it as never traded.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(hours.iso_text(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def position_cap_for(risk, ticker):
    """The position cap for one instrument: its own entry if it has one, else the global cap.

    One global cap cannot serve both a diversified core fund and single stocks. Sized for a
    stock it blocks every buy of the fund as soon as the fund is worth more than the cap;
    sized for the fund it lets thousands pile into one company. So the fund gets its own
    entry in max_position_value_by_ticker and everything else keeps the global cap.
    """
    overrides = {str(k).strip().upper(): v
                 for k, v in (risk.get("max_position_value_by_ticker") or {}).items()}
    return float(overrides.get(ticker, risk.get("max_position_value", float("inf"))))


def fraction(raw, default=0.0):
    """A hold as a fraction from 0 to 0.5, so it can never divide by zero. Missing is the
    default; anything unreadable, NaN included, is 0.5, the most cautious, because every
    check here fails closed."""
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.5
    if value != value:
        return 0.5
    return min(max(value, 0.0), 0.5)


def broker_hold(risk):
    """The share of free funds Trading 212 keeps back from each buy: risk.broker_hold."""
    return fraction((risk or {}).get("broker_hold"))


def near_high_reasons(ticker, state, config, now):
    """Reasons to refuse a BUY because the price is too close to its 52-week high.

    An optional rule, off by default (risk.near_high.enabled): do not buy a stock sitting
    near its high for the year. It is checked here rather than in signals.gate_buy because
    that only ever sees scheduled buys, and momentum is where single stocks get bought. Buys
    only; a sell is never held back by this.

    Diversified funds are exempt by name. On the evidence the rule costs an index fund money
    (indexes sit near their highs most of the time, so it mostly holds cash), and for single
    stocks backtest.py's mom10_rule row found it lost return in every window, which is why it
    ships off. The series is in the instrument's own quote currency, which is correct for a
    ratio and must never be mixed with the account currency.

    Fails closed: no history, too little, or stale history refuses the buy with the reason.
    """
    rule = config.get("risk", {}).get("near_high") or {}
    if not rule.get("enabled", False):
        return []
    exempt = {str(t).strip().upper() for t in rule.get("exempt", [])}
    if ticker in exempt:
        return []

    import signals  # pure arithmetic, no network; imported here to keep gates importable alone

    window = int(rule.get("window_days", 252))
    floor = float(rule.get("min_below_high", 0.05))
    series = (state.get("price_history") or {}).get(ticker) or []
    if not series:
        return ["no price history for %s, so the near-high rule cannot clear it" % ticker]
    if len(series) < window:
        return ["need %d daily closes for %s's 52-week high, have %d"
                % (window, ticker, len(series))]
    moment = parse_time(now)
    newest = series[-1][0]
    stale = int(rule.get("max_staleness_days", 5))
    if moment and (moment.date() - newest).days > stale:
        return ["%s's newest close is from %s, more than %d days old"
                % (ticker, newest.isoformat(), stale)]
    below = signals.drawdown(series, window)
    if below is None:
        return ["could not measure %s against its 52-week high" % ticker]
    if below < floor:
        return ["%s is only %.1f%% below its 52-week high, the rule wants at least %.1f%%"
                % (ticker, below * 100, floor * 100)]
    return []


def check(proposal, state, config, now, running=None):
    """Return the reasons this proposal must not be sent. Empty list means it may.

    Every check runs, none short circuits. A proposal that fails four ways should say so,
    because the first reason alone is usually the least interesting one.
    """
    risk = config["risk"]
    compliance = config["compliance"]
    running = running or {"spend": 0.0, "orders": 0}
    reasons = []

    ticker = str(proposal.get("ticker", "")).strip().upper()
    action = str(proposal.get("action", "")).strip().lower()
    try:
        value = float(proposal.get("value", 0))
    except (TypeError, ValueError):
        value = -1.0
    try:
        confidence = float(proposal.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = -1.0

    if killswitch_path(config).exists():
        reasons.append("halted: the killswitch file exists")

    if not ticker:
        reasons.append("no ticker")
    if action not in ACTIONS:
        reasons.append("action %r is not buy or sell" % proposal.get("action"))
    if value <= 0:
        reasons.append("order value must be a positive number")
    if not 0.0 <= confidence <= 1.0:
        reasons.append("confidence must be between 0 and 1")

    if action == "buy" and not risk.get("allow_buy", True):
        reasons.append("buying is switched off")
    if action == "sell" and not risk.get("allow_sell", True):
        reasons.append("selling is switched off")

    # The compliance screen applies to buying only. Selling a holding that is not on the
    # list is how you get out of one, so blocking that would be exactly backwards.
    if action == "buy" and compliance.get("allowlist_only", True):
        if ticker not in allowlist(config) | extra_allowlist(state, proposal):
            reasons.append("%s is not on the compliance allowlist" % (ticker or "?"))

    if confidence >= 0 and confidence < risk.get("min_confidence", 0.0):
        reasons.append("confidence %.2f is below the %.2f floor"
                       % (confidence, risk["min_confidence"]))

    positions = state.get("positions", {})
    held = float(positions.get(ticker, {}).get("value", 0.0))

    # Value limits apply to buying only. They exist to cap what can be SPENT, and applying
    # them to a sell would trap you in any position bigger than the cap, which matters most
    # in exactly the case that matters most: exiting a holding that fails the screen.
    if action == "buy":
        if value > risk.get("max_order_value", float("inf")):
            reasons.append("order of %.2f exceeds the %.2f per order cap"
                           % (value, risk["max_order_value"]))
        position_cap = position_cap_for(risk, ticker)
        if held + value > position_cap:
            reasons.append("would take %s to %.2f, over the %.2f position cap"
                           % (ticker, held + value, position_cap))
        reasons.extend(near_high_reasons(ticker, state, config, now))
        # Only THIS batch's buys come off the cash. running["spend"] also holds what earlier
        # runs spent today, for the daily cap, and the broker's cash already has that taken
        # off: subtracting it again counts an earlier run's buy twice and refuses buys the
        # account can pay for.
        # Each buy also ties up more than its value: Trading 212 takes an order only up to
        # about 95% of free funds, holding the rest against the price moving before the fill
        # (risk.broker_hold). Without it the gate can pass a buy that the broker then refuses
        # as "insufficient funds" while the gate's own sums still show cash to spare.
        hold = broker_hold(risk)
        cash_after = (float(state.get("cash", 0.0))
                      - (running.get("batch", 0.0) + value) / (1.0 - hold))
        if cash_after < risk.get("min_cash_buffer", 0.0):
            reasons.append("would leave %.2f in cash%s, under the %.2f buffer"
                           % (cash_after,
                              " after the broker's %g%% hold" % (hold * 100) if hold else "",
                              risk["min_cash_buffer"]))
        if running["spend"] + value > risk.get("max_daily_spend", float("inf")):
            reasons.append("would take today's spend to %.2f, over the %.2f daily cap"
                           % (running["spend"] + value, risk["max_daily_spend"]))

    if action == "sell" and held <= 0:
        reasons.append("cannot sell %s, none held" % (ticker or "?"))

    if running["orders"] >= risk.get("max_orders_per_day", float("inf")):
        reasons.append("already at the %d orders per day limit" % risk["max_orders_per_day"])

    cooldown = risk.get("cooldown_hours_per_instrument", 0)
    stamp = (state.get("last_trade") or {}).get(ticker)
    last = parse_time(stamp)
    # A last trade that is there but cannot be read is held, not waved through: read as
    # never traded, the cooldown would silently not apply to it.
    if cooldown and stamp and last is None:
        reasons.append("%s last traded at %r, which cannot be read as a time, so it is held "
                       "for the cooldown" % (ticker, stamp))
    # `now` goes through parse_time too, so this subtraction is total: it cannot raise no
    # matter which of the two sides arrives naive.
    moment = parse_time(now)
    if cooldown and last and moment and moment - last < timedelta(hours=cooldown):
        free_at = last + timedelta(hours=cooldown)
        reasons.append("%s traded %s, in cooldown until %s"
                       % (ticker, last.isoformat(timespec="minutes"),
                          free_at.isoformat(timespec="minutes")))

    return reasons


def decide(proposals, state, config, now):
    """Apply the gates to a whole batch, best first, accumulating the daily limits.

    Highest confidence goes first, so if a source ranks its proposals, a daily cap that bites
    spends what is left on the ones it ranked highest rather than whichever happened to be
    first. Every source here (the schedule, the rebalance, momentum) proposes at 1.0, and the
    sort is stable, so today a batch keeps the order its sources gave it.
    """
    running = {
        # Today's buying across all runs, for the daily cap.
        "spend": float(state.get("spent_today", 0.0)),
        # This batch's buying only, for the cash buffer: the cash read already reflects
        # every earlier run's orders.
        "batch": 0.0,
        "orders": int(state.get("orders_today", 0)),
    }
    ordered = sorted(proposals, key=lambda p: -float(p.get("confidence") or 0))

    # One order per instrument per direction per batch. The ledger stops a repeat across
    # runs, but it is written while placing, after this has already decided, so two buys of
    # the same ticker in ONE batch would both be sent. The rules engine alone can produce
    # that (a scheduled buy and a rebalance buy of the same fund), and momentum's core
    # top-up can name the same fund too. Best first, so the higher confidence proposal keeps
    # it, and between equals the first one proposed.
    taken = set()
    accepted, rejected = [], []
    for proposal in ordered:
        reasons = check(proposal, state, config, now, running)
        slot = (str(proposal.get("ticker", "")).strip().upper(),
                str(proposal.get("action", "")).strip().lower())
        if slot in taken:
            reasons.append("a %s of %s was already accepted in this batch" % (slot[1], slot[0]))
        if reasons:
            rejected.append({"proposal": proposal, "reasons": reasons})
            continue
        accepted.append(proposal)
        taken.add(slot)
        running["orders"] += 1
        if str(proposal.get("action", "")).lower() == "buy":
            running["spend"] += float(proposal["value"])
            running["batch"] += float(proposal["value"])
    return accepted, rejected


def account_label(config):
    """(adapter name, label) for the account config.json points at, as a run names it:
    broker.adapter_label, which says when it is simulated and when it is real money. A
    local import, so the gates themselves stay free of anything that talks to a network;
    the label only reads config and the adapter's module."""
    import broker
    section = dict(config.get("broker") or {},
                   environment=(config.get("execution") or {}).get("environment", "demo"))
    name = broker.adapter_name(section)
    try:
        return name, broker.adapter_label(section)
    except SystemExit as error:   # an adapter that does not exist: say so, do not stop
        return name, "unusable (%s)" % error


def cmd_explain(args, config):
    risk, compliance = config["risk"], config["compliance"]
    entries = compliance.get("allowlist", [])
    execution = config.get("execution", {})
    name, account = account_label(config)

    print("Execution")
    print("  mode          %s" % ("execute on: a run with --execute sends orders to %s" % account
                                  if execution.get("execute") else "propose only, nothing is sent"))
    print("  account       %s" % account)
    if name == "trading212":
        # Demo or live means something only to Trading 212; the paper account has neither.
        print("  environment   %s" % execution.get("environment", "demo"))
    print("  killswitch    %s (%s)" % (killswitch_path(config),
                                       "ACTIVE, everything halted"
                                       if killswitch_path(config).exists() else "not present"))
    print("\nCompliance")
    if not entries:
        print("  allowlist is EMPTY, so nothing can be bought at all. This is the safe default,")
        print("  not a bug. Add screened instruments before anything can trade.")
    else:
        for entry in entries:
            print("  %-8s %-34s %s, checked %s"
                  % (entry.get("ticker", "?"), entry.get("name", "")[:34],
                     entry.get("source", "no source recorded"),
                     entry.get("checked_on", "never")))
    print("  selling is never blocked by the allowlist, so a non compliant holding can be exited")

    print("\nRisk")
    for label, key, unit in (
        ("per order", "max_order_value", ""), ("per day", "max_daily_spend", ""),
        ("per position", "max_position_value", ""), ("cash buffer", "min_cash_buffer", ""),
        ("broker hold", "broker_hold", " of each buy, kept free by Trading 212"),
        ("orders per day", "max_orders_per_day", " orders"),
        ("cooldown", "cooldown_hours_per_instrument", " hours"),
        ("confidence floor", "min_confidence", ""),
    ):
        print("  %-17s %s%s" % (label, risk.get(key), unit))
    return 0


def cmd_selftest(args, config):
    now = datetime(2026, 9, 11, 12, 0)
    cfg = {
        "compliance": {"allowlist_only": True, "allowlist": [
            {"ticker": "SPUS"}, {"ticker": "HLAL"}]},
        "risk": {
            "max_order_value": 25.0, "max_daily_spend": 50.0, "max_orders_per_day": 2,
            "max_position_value": 200.0, "min_cash_buffer": 20.0,
            "cooldown_hours_per_instrument": 24, "min_confidence": 0.7,
            "allow_buy": True, "allow_sell": True, "killswitch_file": "HALT_NOT_PRESENT",
        },
        "execution": {"execute": False, "environment": "demo"},
    }
    base = {"cash": 500.0, "positions": {}, "spent_today": 0.0, "orders_today": 0,
            "last_trade": {}}

    def buy(ticker, value=10.0, confidence=0.9):
        return {"ticker": ticker, "action": "buy", "value": value, "confidence": confidence}

    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    def blocked_for(proposal, state=None, fragment=""):
        reasons = check(proposal, state or base, cfg, now)
        return any(fragment in r for r in reasons), reasons

    # The allowlist is the whole defence against a strategy proposing something unscreened.
    expect("an allowlisted buy passes", not check(buy("SPUS"), base, cfg, now))
    hit, _ = blocked_for(buy("MSFT"), fragment="not on the compliance allowlist")
    expect("a ticker not on the allowlist is refused", hit)
    hit, _ = blocked_for(buy("spus"), fragment="allowlist")
    expect("the allowlist is case insensitive", not hit)
    monthly = dict(base, extra_allowlist=["msft"])
    picked = dict(buy("MSFT"), source="momentum")
    hit, _ = blocked_for(picked, state=monthly, fragment="allowlist")
    expect("this month's momentum picks are buyable by momentum", not hit)
    hit, _ = blocked_for(buy("MSFT"), state=monthly, fragment="not on the compliance allowlist")
    expect("but not by any other source", hit)
    hit, _ = blocked_for(dict(buy("NFLX"), source="momentum"), state=monthly,
                         fragment="not on the compliance allowlist")
    expect("and momentum cannot buy outside its picks", hit)

    # Selling must not be gated by the allowlist, or you could never exit a holding.
    holding = dict(base, positions={"MSFT": {"value": 40.0}})
    sell = {"ticker": "MSFT", "action": "sell", "value": 40.0, "confidence": 0.9}
    expect("a non compliant holding can still be sold", not check(sell, holding, cfg, now))
    hit, _ = blocked_for({"ticker": "IBM", "action": "sell", "value": 5.0, "confidence": 0.9},
                         base, "none held")
    expect("selling something not held is refused", hit)

    # Sizing.
    hit, _ = blocked_for(buy("SPUS", value=26.0), fragment="per order cap")
    expect("an oversized buy is refused", hit)
    big = {"ticker": "MSFT", "action": "sell", "value": 500.0, "confidence": 0.9}
    expect("a sell larger than the order cap is still allowed",
           not check(big, dict(base, positions={"MSFT": {"value": 500.0}}), cfg, now))
    hit, _ = blocked_for(buy("SPUS", value=20.0),
                         dict(base, positions={"SPUS": {"value": 190.0}}), "position cap")
    expect("a buy breaching the position cap is refused", hit)
    hit, _ = blocked_for(buy("SPUS", value=20.0), dict(base, cash=30.0), "under the")
    expect("a buy breaching the cash buffer is refused", hit)

    # Confidence floor.
    hit, _ = blocked_for(buy("SPUS", confidence=0.5), fragment="below the")
    expect("a low confidence proposal is refused", hit)

    # Cooldown.
    recent = dict(base, last_trade={"SPUS": (now - timedelta(hours=2)).isoformat()})
    hit, _ = blocked_for(buy("SPUS"), recent, "cooldown")
    expect("an instrument traded 2 hours ago is in cooldown", hit)
    old = dict(base, last_trade={"SPUS": (now - timedelta(hours=25)).isoformat()})
    expect("cooldown expires after the window", not check(buy("SPUS"), old, cfg, now))

    # The real shape, and the one that actually broke. Trading 212 stamps createdAt with an
    # offset, so the STORED value is aware while `now` is naive. The two cases above build
    # both sides from the same naive clock, which is exactly why they agree with each other
    # and would miss this. Subtracting them raised TypeError instead of returning a verdict,
    # and only once an instrument had a previous trade, so a suite without this case passes
    # while a live run dies on the first propose after the first real order.
    stamped = dict(base, last_trade={"SPUS": "2026-09-11T10:00:00+00:00"})
    hit, _ = blocked_for(buy("SPUS"), stamped, "cooldown")
    expect("an offset-stamped last trade is a cooldown, not a crash", hit)
    stale = dict(base, last_trade={"SPUS": "2026-09-10T10:00:00+00:00"})
    expect("an offset-stamped last trade outside the window clears",
           not check(buy("SPUS"), stale, cfg, now))

    # The mirror image, which is the direction production now runs in: an aware `now` from
    # datetime.now(timezone.utc) against a naive stored timestamp.
    utc_now = now.replace(tzinfo=timezone.utc)
    naive_stored = dict(base, last_trade={"SPUS": (now - timedelta(hours=2)).isoformat()})
    expect("an aware now against a naive last trade still decides",
           any("cooldown" in r for r in check(buy("SPUS"), naive_stored, cfg, utc_now)))
    expect("an aware now still clears an expired cooldown",
           not check(buy("SPUS"), old, cfg, utc_now))

    # Stamps whose fraction is not 3 or 6 digits, which Python 3.9 and 3.10 refused to parse:
    # read as never traded, they waved a fresh trade straight through the cooldown on those
    # Pythons and not on newer ones. Each is two hours old here.
    for stamp in ("2026-09-11T10:00:00.12Z", "2026-09-11T10:00:00.1234567Z",
                  "2026-09-11T10:00:00.476614573Z", "2026-09-11T10:00:00.5+00:00"):
        fresh = dict(base, last_trade={"SPUS": stamp})
        hit, _ = blocked_for(buy("SPUS"), fresh, "in cooldown until")
        expect("a last trade stamped %s is a cooldown on any Python" % stamp[19:], hit)
    expect("a nine-digit stamp outside the window clears",
           not check(buy("SPUS"), dict(base, last_trade={"SPUS": "2026-09-10T10:00:00.476614573Z"}),
                     cfg, now))
    garbled = dict(base, last_trade={"SPUS": "not a time"})
    hit, reasons = blocked_for(buy("SPUS"), garbled, "cannot be read as a time")
    expect("a last trade that cannot be read holds the buy, it is not read as never traded", hit)
    expect("and a sell of it too, as the cooldown covers both",
           any("cannot be read" in r for r in check(
               {"ticker": "SPUS", "action": "sell", "value": 10.0, "confidence": 0.9},
               dict(garbled, positions={"SPUS": {"value": 40.0}}), cfg, now)))
    expect("with no cooldown configured an unreadable stamp holds nothing",
           not check(buy("SPUS"), garbled,
                     dict(cfg, risk=dict(cfg["risk"], cooldown_hours_per_instrument=0)), now))
    expect("an instrument with no last trade at all is not held",
           not check(buy("SPUS"), dict(base, last_trade={"SPUS": ""}), cfg, now))

    # Malformed input from any proposal source must be refused, not crash.
    for bad in ({"ticker": "SPUS", "action": "yolo", "value": 5, "confidence": 0.9},
                {"ticker": "SPUS", "action": "buy", "value": "lots", "confidence": 0.9},
                {"ticker": "SPUS", "action": "buy", "value": 5, "confidence": "very"},
                {}):
        expect("malformed proposal %r is refused" % (bad.get("action", "empty")),
               bool(check(bad, base, cfg, now)))

    # Batch limits accumulate, and the best proposal is funded first.
    batch = [buy("SPUS", 20.0, 0.75), buy("HLAL", 20.0, 0.95), buy("SPUS", 20.0, 0.85)]
    accepted, rejected = decide(batch, base, cfg, now)
    expect("the daily order limit caps the batch at 2", len(accepted) == 2)
    expect("highest confidence is funded first", accepted[0]["confidence"] == 0.95)
    expect("the rest are rejected with reasons", len(rejected) == 1 and rejected[0]["reasons"])

    # Two sources proposing the same trade must not become two orders. The more confident
    # proposal for the same fund (1.0 here) outranks the other, so it is the one that survives.
    roomy = dict(cfg, risk=dict(cfg["risk"], max_orders_per_day=6))
    twice = [buy("SPUS", 10.0, 0.8), buy("SPUS", 15.0, 1.0), buy("HLAL", 10.0, 0.9)]
    accepted, rejected = decide(twice, base, roomy, now)
    expect("one buy per ticker per batch, the most confident kept",
           [p["ticker"] for p in accepted].count("SPUS") == 1
           and next(p for p in accepted if p["ticker"] == "SPUS")["confidence"] == 1.0)
    expect("the duplicate is rejected and says why",
           any("already accepted" in r for item in rejected for r in item["reasons"]))
    expect("a different ticker in the same batch is unaffected",
           any(p["ticker"] == "HLAL" for p in accepted))
    held = dict(base, positions={"SPUS": {"value": 100.0}})
    both = [buy("SPUS", 10.0, 0.9),
            {"ticker": "SPUS", "action": "sell", "value": 10.0, "confidence": 0.9}]
    accepted, _ = decide(both, held, roomy, now)
    expect("a buy and a sell of one ticker are different slots", len(accepted) == 2)

    spendy = [buy("SPUS", 25.0, 0.95), buy("HLAL", 25.0, 0.9)]
    accepted, _ = decide(spendy, dict(base, spent_today=40.0), cfg, now)
    expect("the daily spend cap bites mid batch", len(accepted) == 0)

    # Earlier runs' spending is already out of the broker's cash, so it counts towards the
    # daily cap but must not come off the cash a second time. A run that bought 300.00
    # earlier today now reads 2,000.00 cash: a batch of 1,975.00 leaves 25.00, over the
    # 20.00 buffer, while counting the 300.00 again would call it -275.00.
    wide = dict(cfg, risk=dict(cfg["risk"], max_order_value=2000.0, max_daily_spend=5000.0,
                               max_position_value=10000.0, max_orders_per_day=50))
    later = dict(base, cash=2000.0, spent_today=300.0)
    accepted, rejected = decide([buy("SPUS", 300.0, 1.0), buy("HLAL", 1675.0, 0.95)],
                                later, wide, now)
    expect("earlier runs' buys are not taken off the cash twice", len(accepted) == 2)
    accepted, rejected = decide([buy("SPUS", 300.0, 1.0), buy("HLAL", 1685.0, 0.95)],
                                later, wide, now)
    expect("this batch's own buys still come off the cash",
           [p["ticker"] for p in accepted] == ["SPUS"]
           and any("buffer" in r for item in rejected for r in item["reasons"]))
    accepted, _ = decide([buy("SPUS", 300.0, 1.0)],
                         dict(later, spent_today=4800.0), wide, now)
    expect("and earlier runs' buys still count towards the daily cap", not accepted)

    # The broker's hold: an order may use at most about 95% of free funds. Nine buys of
    # 330.00 from 3,000.00 cash leave 30.00 on plain sums, over the 20.00 buffer, so a gate
    # without the hold passes all nine; the broker takes eight and refuses the ninth as
    # insufficient funds, and with the hold the gate refuses it first. A single buy that
    # fits inside the hold must still pass.
    held_back = dict(wide, risk=dict(wide["risk"], broker_hold=0.05))
    names = ["T%d" % i for i in range(9)]
    funded = dict(base, cash=3000.0, extra_allowlist=names)
    batch = [dict(buy(n, 330.0, 1.0), source="momentum") for n in names]
    accepted, rejected = decide(batch, funded, held_back, now)
    expect("the hold refuses the ninth buy the broker would refuse",
           [p["ticker"] for p in accepted] == names[:8]
           and [i["proposal"]["ticker"] for i in rejected] == ["T8"])
    expect("and says why", any("hold" in r for i in rejected for r in i["reasons"]))
    accepted, _ = decide(batch[:9], funded, wide, now)
    expect("without the hold the gate passes it", len(accepted) == 9)
    accepted, _ = decide([dict(buy("T0", 360.0, 1.0), source="momentum")],
                         dict(funded, cash=400.0), held_back, now)
    expect("the hold still lets through a buy that fits inside 95% of free cash", accepted)
    expect("a hold of 1 or more cannot divide by zero", broker_hold({"broker_hold": 1.0}) == 0.5)
    expect("an unreadable or NaN hold fails closed to the most cautious",
           broker_hold({"broker_hold": "x"}) == 0.5
           and broker_hold({"broker_hold": float("nan")}) == 0.5
           and broker_hold({"broker_hold": "nan"}) == 0.5)
    expect("a missing hold is none, as before", broker_hold({}) == 0.0 and broker_hold(None) == 0)

    # The killswitch overrides everything.
    halted = dict(cfg, risk=dict(cfg["risk"], killswitch_file="gates.py"))
    expect("the killswitch stops an otherwise perfect order",
           any("halted" in r for r in check(buy("SPUS"), base, halted, now)))

    # A position cap of its own for the core fund. The global 200 still binds everything else.
    fund = dict(cfg, risk=dict(cfg["risk"], max_position_value_by_ticker={"hlal": 5000.0}))
    big_fund = dict(base, positions={"HLAL": {"value": 3000.0}, "SPUS": {"value": 190.0}})
    expect("the fund's own cap lets it grow past the global cap",
           not any("position cap" in r for r in check(buy("HLAL", 20.0), big_fund, fund, now)))
    expect("the fund's own cap is still a cap",
           any("position cap" in r for r in check(
               buy("HLAL", 20.0), dict(base, positions={"HLAL": {"value": 4990.0}}), fund, now)))
    expect("every other ticker keeps the global cap",
           any("position cap" in r for r in check(buy("SPUS", 20.0), big_fund, fund, now)))

    # The optional near-high rule: do not buy a single stock within 5% of its 52-week high.
    def closes(*levels, days=260, end=date(2026, 9, 11)):
        """260 daily closes rising to 100, then the given closing levels at the end."""
        values = [80.0 + 20.0 * i / (days - 1) for i in range(days)] + list(levels)
        start = end - timedelta(days=len(values) - 1)
        return [(start + timedelta(days=i), v) for i, v in enumerate(values)]

    rule = {"enabled": True, "min_below_high": 0.05, "window_days": 252,
            "max_staleness_days": 5, "exempt": ["hlal"]}
    peaky = dict(cfg, risk=dict(cfg["risk"], near_high=rule))

    def near(ticker, history, conf=peaky):
        return [r for r in check(buy(ticker), dict(base, price_history=history), conf, now)
                if "52-week" in r or "price history" in r or "close" in r]

    expect("a stock at its 52-week high is not bought", near("SPUS", {"SPUS": closes(100.0)}))
    expect("a stock 3% under its high is not bought", near("SPUS", {"SPUS": closes(97.0)}))
    expect("a stock 6% under its high can be bought", not near("SPUS", {"SPUS": closes(94.0)}))
    expect("no history fails closed", near("SPUS", {}))
    expect("too little history fails closed", near("SPUS", {"SPUS": closes(days=100)}))
    expect("stale history fails closed",
           near("SPUS", {"SPUS": closes(94.0, end=date(2026, 9, 1))}))
    expect("the exempt fund is bought with no history at all", not near("HLAL", {}))
    expect("the rule is off unless enabled", not near("SPUS", {}, cfg))
    held = dict(base, positions={"SPUS": {"value": 100.0}}, price_history={"SPUS": closes(100.0)})
    expect("a sell is never held back by the near-high rule",
           not any("52-week" in r for r in check(
               {"ticker": "SPUS", "action": "sell", "value": 10.0, "confidence": 0.9},
               held, peaky, now)))

    # explain names the account as a run does. On the shipped paper adapter it must not say
    # orders are real, and the demo or live environment is shown for Trading 212 only.
    import contextlib
    import io

    def explained(broker_section, execute=True, environment="demo"):
        out = io.StringIO()
        shown = dict(cfg, broker=broker_section,
                     execution={"execute": execute, "environment": environment})
        with contextlib.redirect_stdout(out):
            cmd_explain(None, shown)
        return out.getvalue()

    text = explained({"adapter": "paper"})
    expect("explain on paper says simulated, never that orders are real, and shows no "
           "environment", "simulated" in text and "orders are real" not in text
           and "environment" not in text)
    text = explained({"adapter": "trading212"}, environment="live")
    expect("explain on Trading 212 live says real money and shows the environment",
           "trading212 live (real money)" in text and "environment   live" in text)
    text = explained({"adapter": "trading212"}, execute=False)
    expect("explain with execute off says nothing is sent",
           "propose only, nothing is sent" in text and "trading212 demo" in text)
    text = explained({"adapter": "nosuchbroker"})
    expect("explain on a missing adapter says so rather than stopping",
           "unusable" in text and "nosuchbroker" in text)

    width = max(len(name) for name, _ in checks)
    for name, passed in checks:
        print("%s  %s" % ("pass" if passed else "FAIL", name.ljust(width)))
    failed = [name for name, passed in checks if not passed]
    print("\n%d checks, %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("selftest", help="run the checks on the checks").set_defaults(func=cmd_selftest)
    sub.add_parser("explain", help="print the active limits").set_defaults(func=cmd_explain)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args, load(args.config))


if __name__ == "__main__":
    sys.exit(main())
