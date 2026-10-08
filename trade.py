#!/usr/bin/env python3
"""
The runner: account state, then strategy, then gates, then sizing, then maybe an order.

    python3 trade.py                         propose only, place nothing
    python3 trade.py --execute               actually place what survives
    python3 trade.py --env demo --execute    the same, on Trading 212's demo environment
    python3 trade.py selftest                the whole runner against a fake broker

Which broker it talks to is config.json broker.adapter: "paper" (simulated, as shipped) or
"trading212", where --env (or execution.environment) picks demo or live.

Order of operations, none of it negotiable at runtime:

    broker.py   reads cash and positions          (read only, cannot place)
    rules.py    proposes, from arithmetic         (no model, no credentials, no network)
    momentum.py proposes the monthly momentum book (only in the window after each build)
    gates.py    throws most of it away            (no model, no network, tested)
    sizing.py   converts an amount to a quantity  (rounds down, re-checks the cap)
    here        places what is left, if allowed

Four independent things must all be true before an order goes out: --execute on the
command line, execute true in config.json, no HALT file, and the order not already being in
today's ledger. Any one missing means propose.

Two places this deliberately fails closed rather than guessing:

  If today's order count cannot be read from history, it refuses to execute. Assuming zero is
  how a daily cap silently becomes no cap.

  If no price can be found for an instrument, the order is refused rather than sized from a
  guess. Trading 212 has no quotes endpoint, so a price for something you do not hold has to
  come from prices.csv, or for a momentum pick from two outside sources that agree, or not
  at all.
"""

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import broker  # noqa: E402
import gates  # noqa: E402
import momentum  # noqa: E402
import sizing  # noqa: E402


def number(value):
    return float(value) if isinstance(value, (int, float)) else None


def dig(blob, *path):
    """Walk a nested dict, returning None rather than raising on a missing key."""
    current = blob
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def account_state(broker_config, key, secret, cooldown_hours=120, currency=None):
    """Build what gates.py and sizing.py need. Anything unreadable is reported, never assumed.

    currency is config account.currency. When the broker reports the account in another
    one, the summary counts as unread, so nothing is placed: the caps and every proposal
    value are read as that currency, and momentum converts a new pick's dollar price into
    it. A USD account run as GBP at 1.30 dollars to the pound buys each new pick 30% bigger
    than its share, and counts each buy that much short against the daily cap."""
    state = {"cash": 0.0, "positions": {}, "spent_today": 0.0, "orders_today": 0,
             "last_trade": {}, "unknown": []}
    endpoints = broker_config["endpoints"]

    status, summary = broker.request(broker_config, endpoints["account_summary"], key, secret)
    if status == 200 and isinstance(summary, dict):
        reported = str(summary.get("currency") or "").strip().upper()
        expected = str(currency or "").strip().upper()
        if reported and expected and reported != expected:
            state["unknown"].append(
                "account summary: the broker reports the account in %s, but config.json "
                "account.currency is %s; set account.currency to %s" % (reported, expected,
                                                                       reported))
        # The published shape nests cash under a cash object. Older guesses used flat fields,
        # so try the nested path first and fall back rather than silently reading zero.
        # First field that is PRESENT, not first that is truthy: chaining with `or` read a
        # real balance of 0.00 as missing and reported "could not read cash" on an empty
        # account.
        cash = next((value for value in (number(dig(summary, "cash", "availableToTrade")),
                                          number(dig(summary, "cash", "free")),
                                          number(summary.get("free")),
                                          number(summary.get("availableToTrade")))
                     if value is not None), None)
        if cash is None:
            state["unknown"].append("cash: no recognised field in %s" % sorted(summary)[:8])
        else:
            state["cash"] = cash
        # What the broker values the holdings at, to tell an empty positions list from an
        # account that really holds nothing.
        invested = number(dig(summary, "investments", "currentValue"))
    else:
        invested = None
        state["unknown"].append("account summary: %s" % broker.describe(status, summary))

    status, holdings = broker.request(broker_config, endpoints["positions"], key, secret)
    if status == 200 and isinstance(holdings, list) and not holdings and (invested or 0) > 25:
        # Read as holding nothing, every pick looks empty and the position cap sees nothing
        # held, so picks already at their share would be bought again.
        state["unknown"].append("positions: the list came back empty, but the account summary "
                                "values the holdings at %.2f" % invested)
    elif status == 200 and isinstance(holdings, list):
        for row in holdings:
            # The ticker is NESTED under instrument, not top level. Reading it flat silently
            # produced an EMPTY portfolio, which would have made every position cap and every
            # sell check evaluate against nothing held. Confirmed against a real response.
            spelled = str(dig(row, "instrument", "ticker") or row.get("ticker") or "").strip()
            ticker = spelled.upper()
            quantity = number(row.get("quantity")) or 0.0
            price = (number(row.get("currentPrice"))
                     or number(row.get("averagePricePaid"))
                     or number(row.get("averagePrice")) or 0.0)
            if not ticker:
                state["unknown"].append("a position row carried no ticker in any known field")
                continue
            wallet = number(dig(row, "walletImpact", "currentValue"))
            state["positions"][ticker] = {
                # Prefer the broker's own valuation over quantity times price: it already
                # accounts for the instrument's currency and any FX.
                "value": wallet or quantity * price,
                "quantity": quantity,
                # In ACCOUNT currency, like every value it is divided into. currentPrice is in
                # the instrument's currency, dollars for a US share in a pounds account.
                "current_price": sizing.account_price(quantity, price, wallet) or None,
                "currency": (dig(row, "instrument", "currency")
                             or dig(row, "walletImpact", "currency") or ""),
                # The broker's own spelling, for the order body. See sizing.broker_ticker.
                "broker_ticker": spelled,
            }
    else:
        state["unknown"].append("positions: %s" % broker.describe(status, holdings))

    # Paged back past the cooldown window: one default page held only 20 orders, so the
    # daily order cap and the cooldown could not see anything older. See broker.order_history.
    reach = max(float(cooldown_hours or 0), 24.0) + 24.0
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=reach)).isoformat()
    status, history, history_notes = broker.order_history(
        lambda path: broker.request(broker_config, path, key, secret),
        endpoints["history_orders"], cutoff)
    for note in history_notes:
        state["unknown"].append("order history: %s" % note)
    # The run's own UTC clock, not the machine's local date: it is the day the ledger and the
    # daily cap count in, and a second pass reads it again a minute later.
    today = datetime.now(timezone.utc).date().isoformat()
    if status == 200 and isinstance(history, (list, dict)):
        rows = history if isinstance(history, list) else (history.get("items") or [])
        placed = 0
        for row in rows:
            # Each item wraps the order: {"order": {...}, "fill": {...}}. The ticker sits at
            # order.ticker or order.instrument.ticker, and the timestamp is createdAt, NOT
            # dateCreated. Reading them flat returned None for both, which meant orders_today
            # was always zero and last_trade always empty. Two of the three safety counters
            # were inert: the daily cap never bound and the cooldown never fired, so a weekly
            # schedule would have bought on every single run. Confirmed against real history.
            order = row.get("order") if isinstance(row.get("order"), dict) else row
            when = (order.get("createdAt") or order.get("dateCreated")
                    or order.get("dateModified") or "")
            ticker = str(dig(order, "instrument", "ticker")
                         or order.get("ticker") or "").upper()

            # last_trade drives the cooldown, so it needs the most recent trade for each
            # instrument whenever it happened, not just today's. History is not ordered, so
            # compare rather than overwrite.
            if ticker and str(when) > str(state["last_trade"].get(ticker, "")):
                state["last_trade"][ticker] = when

            # orders_today drives the daily cap, so that one is today only.
            if str(when)[:10] == today:
                placed += 1
        state["orders_today"] = placed
    else:
        state["unknown"].append(
            "order history: %s. Today's order count is unknown, so executing is refused."
            % broker.describe(status, history))
        state["history_unavailable"] = True

    return state


def check_market_hours(broker_config, key, secret, orders, now, config_fallbacks=None,
                       instruments=None, cache=None):
    """Return [(ticker, reason)] for orders whose exchange is not open right now.

    Fails closed throughout: if the schedules cannot be read, every order is held. An unknown
    market state is not an open one, and the cost of waiting is a day where the cost of
    guessing wrong is an overnight gap.

    cache, a dict, keeps the exchanges and instruments read here for the rest of the run. A
    second pass a minute later would otherwise ask again inside the API's limits (one
    instruments dump per 50 seconds) and be held for being refused.
    """
    import hours

    cache = {} if cache is None else cache
    endpoints = broker_config["endpoints"]
    exchanges = cache.get("exchanges")
    if exchanges is None:
        status, exchanges = broker.request(broker_config, endpoints["exchanges"], key, secret)
        if status != 200 or not isinstance(exchanges, list):
            why = "could not read exchange schedules (%s), holding everything" % (
                broker.describe(status, exchanges))
            return [(o["ticker"], why) for o in orders]
        cache["exchanges"] = exchanges

    status = 200
    if instruments is None:
        instruments = cache.get("instruments")
    if instruments is None:
        # One instruments dump per 50 seconds is the API's limit, so a list already fetched
        # this run (the momentum build needs it too) is passed in rather than asked for twice.
        status, instruments = broker.request(broker_config, endpoints["instruments"], key,
                                             secret, timeout=120)
    if status != 200 or not isinstance(instruments, list):
        why = "could not read instruments (%s), so no schedule is known, holding" % (
            broker.describe(status, instruments))
        return [(o["ticker"], why) for o in orders]
    cache["instruments"] = instruments

    schedule_of = {str(row.get("ticker", "")).upper(): row.get("workingScheduleId")
                   for row in instruments if row.get("ticker")}
    schedules = hours.schedules_by_id(exchanges)
    # UTC, not local: fallback_open compares against the configured open_utc/close_utc, and
    # the published windows are UTC-aware. Both branches of the old conditional were the
    # same expression, and both produced local time.
    moment = now.astimezone(timezone.utc)

    # Trading 212's exchanges endpoint returns European and Canadian venues but NOT NYSE or
    # NASDAQ, so a US holding resolves to no schedule at all. Without a fallback every US
    # order is held forever, which is safe and useless. Keyed by ticker suffix.
    fallbacks = config_fallbacks or {}

    def fallback_for(ticker):
        for suffix, spec in fallbacks.items():
            if suffix.startswith("_") and ticker.upper().endswith(suffix.upper()):
                return spec
        return None

    held = []
    for order in orders:
        open_now, why = hours.is_open(schedule_of.get(order["ticker"]), schedules, moment,
                                      fallback_for(order["ticker"]))
        if not open_now:
            held.append((order["ticker"], why))

    # A rebalance that sells on one exchange and buys on another needs both open at once, or
    # the legs happen hours apart with the account exposed in between.
    identifiers = {schedule_of.get(o["ticker"]) for o in orders}
    if len(identifiers) > 1 and not held:
        window, why = hours.overlap(sorted(i for i in identifiers if i is not None),
                                    schedules, moment)
        print("  spanning %d exchanges: %s" % (len(identifiers), why))
    return held


def strategy_proposals(config, state, today):
    """The rules engine: (proposals, lines to print).

    The schedule and the rebalance from rules.py, with the signals layer as a brake on buys.
    Rules proposals carry confidence 1.0. "rules" is the only strategy.mode: any other value
    is printed as a note and the rules run anyway, so an old config never stops a run and
    never reaches for a strategy that is not here.
    """
    import rules
    import signals
    lines = []
    mode = str(config.get("strategy", {}).get("mode") or "rules").strip().lower()
    if mode != "rules":
        lines.append("strategy note    mode %r is not supported, so the rules run instead"
                     % mode)
    history, gaps = signals.load_history()
    if config.get("strategy", {}).get("signals", {}).get("enabled") and gaps:
        for gap in gaps[:3]:
            lines.append("price history    %s" % gap)
    result = rules.propose(config, state, today, history)
    proposals = list(result.get("proposals", []))
    if result.get("notes"):
        lines.append("notes            %s" % result["notes"])
    lines.append("strategy         rules (%s)" % (result.get("model") or "deterministic"))
    return proposals, lines


def near_high_history(config, state, proposals):
    """The near-high rule (risk.near_high, off unless enabled) needs about a year of closes
    for each single stock that a BUY is proposed for. Fetched only then, only for those, and
    only into price_history, which only gates.near_high_reasons reads. Never into sizing:
    these are dollar closes."""
    near = config.get("risk", {}).get("near_high") or {}
    if not near.get("enabled"):
        return
    exempt = {str(t).strip().upper() for t in near.get("exempt", [])}
    wanted = {str(p.get("ticker", "")).strip().upper() for p in proposals
              if str(p.get("action", "")).strip().lower() == "buy"} - exempt - {""}
    if wanted:
        import pricefeed
        state["price_history"], notes = pricefeed.fetch_many(wanted)
        for ticker in sorted(wanted):
            print("52-week data     %s: %s" % (ticker, notes.get(ticker)))


def vet(proposals, state, config, now, ledger, momentum_targets, broker_config, key, secret,
        cache):
    """Gates, then sizing, then the ledger, then market hours: (orders that may be sent,
    the price history sizing used)."""
    accepted, rejected = gates.decide(proposals, state, config, now)
    for item in rejected:
        p = item["proposal"]
        print("  REJECTED %s %s %s: %s" % (p.get("action"), p.get("ticker"), p.get("value"),
                                           "; ".join(item["reasons"])))

    # Sizing happens after the gates, because an amount of money is not an order until it has
    # a quantity, and the conversion can itself fail or breach the cap once rounded.
    price_history, _ = __import__("signals").load_history()
    outside = {}
    if momentum_targets:
        need = sorted({str(p["ticker"]).upper() for p in accepted
                       if p.get("source") == "momentum" and p.get("action") == "buy"
                       and str(p["ticker"]).upper() not in state["positions"]})
        if need:
            outside = momentum.outside_prices(need, momentum_targets, config)
            for ticker in need:
                print("outside price    %s: %s" % (
                    ticker, outside[ticker][1] if ticker in outside
                    else "none that two sources agree on, so no buy"))
    sized = []
    for proposal in accepted:
        order, why = sizing.size(proposal, state["positions"], price_history,
                                 config["risk"], now.date(), outside=outside)
        if order is None:
            print("  UNSIZED  %s %s: %s" % (proposal["action"], proposal["ticker"], why))
            continue
        if sizing.already_placed(ledger, now.date(), order["ticker"], order["action"]):
            print("  DUPLICATE %s %s: already placed today, skipping"
                  % (order["action"], order["ticker"]))
            continue
        sized.append(order)
        print("  ALLOWED  %s %s  %.4f shares at %.4f = %.2f  (%s)"
              % (order["action"], order["ticker"], abs(order["quantity"]),
                 order["price_used"], order["value_actual"], order["price_source"]))

    # Market hours, checked BEFORE anything is sent. A market order into a closed exchange
    # does not fail, it queues and fills at the next open at whatever price that is. On a
    # small weekly drip that is noise. On a rebalance selling a quarter of the account it is
    # an overnight gap nobody chose to take.
    if sized and config["execution"].get("require_market_open", True):
        # The clock is read again here: builds and price lookups can take minutes, and the
        # question is whether markets are open when the orders go out.
        closed = check_market_hours(broker_config, key, secret, sized,
                                    datetime.now(timezone.utc),
                                    config["execution"].get("market_hours_fallback"),
                                    cache=cache)
        for ticker, why in closed:
            print("  HELD     %s: %s" % (ticker, why))
        blocked = {t for t, _ in closed}
        sized = [o for o in sized if o["ticker"] not in blocked]
    return sized, price_history


def broker_label(broker_config):
    """What this run talks to, for printing: broker.adapter_label when broker.py offers it,
    else the environment name. Never raises, as a label is not worth a failed run."""
    describe = getattr(broker, "adapter_label", None)
    label = ""
    if callable(describe):
        try:
            label = str(describe(broker_config) or "")
        except Exception:
            label = ""
    return label or str(broker_config.get("environment") or "unknown")


def place(orders, config, broker_config, key, secret, state, ledger, today, note="",
          ledger_file=None):
    """Send each order, the ledger written before and after every request: (ledger, sent),
    sent holding (order, accepted) for each one. accepted is True or False when the broker
    answered, and None when no answer came back (a timeout or a network error): such an
    order may still have been placed. ledger_file is this account's ledger,
    sizing.ledger_path(broker_config) when not given."""
    ledger_file = ledger_file or sizing.ledger_path(broker_config)
    print("\nPLACING %d order(s) on %s%s" % (len(orders), broker_label(broker_config), note))
    # Tickers are compared upper-cased everywhere above, but the order body must carry the
    # broker's case-sensitive spelling. The allowlist comes first so it decides.
    spellings = ([entry.get("ticker") for entry in config["compliance"].get("allowlist", [])]
                 + [p.get("broker_ticker") for p in state["positions"].values()])
    sent = []
    for order in orders:
        # The ledger is written BEFORE the request, not after. A request that times out may
        # still have placed the order, and the API is not idempotent, so the dangerous
        # failure is recording too little rather than too much.
        ledger = sizing.record_order(ledger, today, order["ticker"], order["action"],
                                     order["quantity"], "sending", order["value_actual"])
        sizing.save_ledger(ledger, ledger_file, today=today)

        status, body = broker.request(
            broker_config, broker_config["endpoints"]["place_market_order"], key, secret,
            method="POST",
            payload={"ticker": sizing.broker_ticker(order["ticker"], spellings),
                     "quantity": order["quantity"]})

        ok = True if status in (200, 201) else (None if status is None else False)
        ledger = sizing.record_order(ledger, today, order["ticker"], order["action"],
                                     order["quantity"], body, order["value_actual"])
        sizing.save_ledger(ledger, ledger_file, today=today)
        print("  %s %s %s %.4f -> %s %s"
              % ("placed" if ok else "FAILED", order["action"], order["ticker"],
                 abs(order["quantity"]), status, "" if ok else str(body)[:200]))
        sent.append((order, ok))
    return ledger, sent


def settled(sent, before, holdings):
    """(True, []) once every order the broker accepted shows in the positions: a sale's
    shares gone, a buy's arrived. Otherwise (False, [tickers still waiting]). A partial fill
    counts as waiting, and an order the broker refused is not waited for."""
    now_held = {}
    for row in holdings:
        ticker = str(dig(row, "instrument", "ticker") or row.get("ticker") or "").strip().upper()
        if ticker:
            now_held[ticker] = number(row.get("quantity")) or 0.0
    waiting = []
    for order, ok in sent:
        if not ok:
            continue
        ticker = str(order["ticker"]).upper()
        was = float((before.get(ticker) or {}).get("quantity") or 0.0)
        moved = now_held.get(ticker, 0.0) - was
        want = float(order["quantity"])
        slack = max(1e-4, 0.01 * abs(want))
        if (want < 0 and moved > want + slack) or (want > 0 and moved < want - slack):
            waiting.append(ticker)
    return not waiting, waiting


def second_pass(config, broker_config, key, secret, first, sent, ledger, momentum_targets,
                cache, pause=time.sleep, ledger_file=None):
    """After a sale goes through, look again and buy what its cash now pays for.

    Without it a sale's cash waits for the next run, which a scheduler can delay or drop,
    and the momentum book has only its window to trade. On Trading 212 a filled sale's cash
    has been seen in cash.availableToTrade within a minute. It runs only when a sell was
    placed, only once every order the broker accepted shows in the positions, and it only
    BUYS. Everything is read again from the broker, and the same gates, sizing, ledger
    and market-hours check apply as on any run. What this run sent counts towards today's
    orders and spend and starts each instrument's cooldown, even if the broker's history has
    not caught up, so nothing it sent can be sent twice. It fails closed: an order whose fate
    is unknown, or any part of the fresh read missing, means no second pass.
    """
    settings = config["execution"].get("second_pass") or {}
    sold = [o for o, ok in sent if ok and o["action"] == "sell"]
    if not settings.get("enabled") or not sold:
        return ledger, []
    unknown = [o["ticker"] for o, ok in sent if ok is None]
    if unknown:
        print("\nSECOND PASS skipped: no answer came back for %s, so what the account holds "
              "is not known" % ", ".join(unknown))
        return ledger, []
    wait = float(settings.get("wait_seconds", 10))
    polls = int(settings.get("polls", 6))
    print("\nSECOND PASS: %d sale(s) placed; waiting up to %ds for every order to show"
          % (len(sold), wait * polls))
    endpoint = broker_config["endpoints"]["positions"]
    waiting = ["the positions could not be read"]
    for _ in range(polls):
        pause(wait)
        status, holdings = broker.request(broker_config, endpoint, key, secret)
        if status == 200 and isinstance(holdings, list):
            done, waiting = settled(sent, first["positions"], holdings)
            if done:
                break
    else:
        print("  not settled (%s), so no second pass; the next run buys instead"
              % ", ".join(waiting))
        return ledger, []

    # The settle poll just read the positions, and account_state reads them again: spaced as
    # the broker check spaces its probes, so the second read is not refused for the rate.
    pause(float(broker_config.get("request_delay_seconds", 5)))
    state = account_state(broker_config, key, secret,
                          config["risk"].get("cooldown_hours_per_instrument", 120),
                          momentum.account_currency(config))
    now = datetime.now(timezone.utc)
    # Fails closed on anything missing. An unread positions list reads as holding nothing,
    # which would buy picks already at their share past the position cap; an unread or short
    # history would forget cooldowns. Nothing is assumed: the next run buys instead.
    if state["unknown"]:
        for problem in state["unknown"]:
            print("  could not read %s" % problem)
        print("  the fresh read is incomplete, so no second pass; the next run buys instead")
        return ledger, []
    whole = {str(o["ticker"]).upper() for o, ok in sent if ok and o["action"] == "sell"
             and abs(float(o["quantity"])) >= float(
                 (first["positions"].get(str(o["ticker"]).upper()) or {}).get("quantity")
                 or 0.0) - 1e-6}
    missing = sorted(t for t, p in first["positions"].items()
                     if float(p.get("quantity") or 0.0) > 1e-9 and t not in whole
                     and t not in state["positions"])
    if missing:
        print("  %s missing from the fresh read though not sold, so no second pass"
              % ", ".join(missing))
        return ledger, []
    # The ledger, not the broker's history, is the record of what this run just sent.
    state["spent_today"] = sizing.spent_on(ledger, now.date(),
                                           config["risk"].get("max_order_value", 0.0))
    state["orders_today"] = max(state["orders_today"], first["orders_today"] + len(sent))
    # Cooldowns the first pass knew about are kept, and everything this run sent starts one.
    for ticker, when in (first.get("last_trade") or {}).items():
        if str(when) > str(state["last_trade"].get(ticker, "")):
            state["last_trade"][ticker] = when
    for order, _ in sent:
        state["last_trade"][str(order["ticker"]).upper()] = now.isoformat()
    state["extra_allowlist"] = first.get("extra_allowlist", [])
    print("  settled: cash %.2f, was %.2f" % (state["cash"], first["cash"]))

    proposals, _ = strategy_proposals(config, state, now.date())
    mom = momentum.settings(config)
    if (momentum_targets and mom.get("mode", "shadow") == "live"
            and momentum.in_window(momentum_targets, now.date(), mom.get("window_days", 7),
                                   broker.adapter_name(broker_config))):
        proposals += momentum.proposals(config, state, momentum_targets)
    buys = [p for p in proposals if str(p.get("action", "")).strip().lower() == "buy"]
    print("  %d buy(s) proposed; a second pass never sells" % len(buys))
    if not buys:
        return ledger, []
    near_high_history(config, state, buys)
    orders, _ = vet(buys, state, config, now, ledger, momentum_targets, broker_config, key,
                    secret, cache)
    if mom.get("mode", "shadow") != "live":
        orders = [o for o in orders if o.get("source") != "momentum"]
    if not orders:
        print("  nothing more survived")
        return ledger, []
    return place(orders, config, broker_config, key, secret, state, ledger, now.date(),
                 ", second pass", ledger_file)


def main(argv=None, pause=time.sleep):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(HERE / "config.json"))
    parser.add_argument("--env", choices=("demo", "live"))
    parser.add_argument("--execute", action="store_true",
                        help="place the orders that survive every check")
    args = parser.parse_args(argv)

    config = gates.load(args.config)
    broker_config = broker.load_broker_config(args.config)
    broker_config["environment"] = (args.env or config["execution"].get("environment", "demo"))
    key, secret = broker.credentials(broker_config)
    # UTC and aware, deliberately. Every timestamp this compares against is UTC: the API's
    # order history carries an offset, and the market-hours fallback is configured in
    # open_utc/close_utc. A naive local clock agreed with all of that only because the
    # runner happens to be on UTC.
    now = datetime.now(timezone.utc)
    # Exchanges and instruments, read at most once a run: see check_market_hours.
    cache = {}

    print("broker           %s" % broker_label(broker_config))

    state = account_state(broker_config, key, secret,
                          config["risk"].get("cooldown_hours_per_instrument", 120),
                          momentum.account_currency(config))
    print("cash             %.2f" % state["cash"])
    print("positions        %d" % len(state["positions"]))
    print("orders today     %s" % ("unknown" if state.get("history_unavailable")
                                   else state["orders_today"]))
    for problem in state["unknown"]:
        print("could not read   %s" % problem)

    # What earlier runs spent today, so the daily cap spans all of a day's runs rather than
    # resetting in each. From the ledger, which the broker's history cannot replace: it
    # carries the value sent, in account currency, and it is written before the request.
    # One ledger per account (sizing.ledger_path), so paper orders never count against a
    # real account's cap. A ledger that exists but cannot be read stops placing: read as
    # empty it would start the day from nothing and resend what was already sent.
    ledger_file = sizing.ledger_path(broker_config)
    ledger_problem = None
    try:
        ledger = sizing.load_ledger(ledger_file)
    except sizing.LedgerError as error:
        ledger, ledger_problem = {}, error
        print("could not read   %s" % error)
    state["spent_today"] = sizing.spent_on(ledger, now.date(),
                                           config["risk"].get("max_order_value", 0.0))
    print("ledger           %s" % ledger_file.name)
    print("spent today      %.2f" % state["spent_today"])

    proposals, lines = strategy_proposals(config, state, now.date())
    for line in lines:
        print(line)

    # The momentum book, momentum.py. Targets are built once a month (network, minutes) and
    # saved; trades only happen in the window after the build. Its picks are buyable this
    # month only, and only because they passed the screen when the targets were built.
    mom = momentum.settings(config)
    momentum_targets, shadow_proposals = None, []
    # Targets carry the adapter whose instrument list built them, so a switch from paper to
    # Trading 212 rebuilds the month from Trading 212's own tickers: see momentum.current.
    adapter = broker.adapter_name(broker_config)
    if mom.get("enabled"):
        momentum_targets = momentum.load_targets(config)
        if not momentum.current(momentum_targets, now.date(), adapter):
            status, instruments = broker.request(
                broker_config, broker_config["endpoints"]["instruments"], key, secret,
                timeout=120)
            if status == 200 and isinstance(instruments, list):
                cache["instruments"] = instruments
                built, why = momentum.build_targets(config, now.date(), instruments)
            else:
                built, why = None, "could not read instruments (%s)" % (
                    broker.describe(status, instruments))
            if built:
                built["adapter"] = adapter
                momentum.save_targets(config, built)
                print("momentum         built %s targets from %d names (%s), signals to %s"
                      % (built["month"], built["universe"],
                         built.get("universe_rule", "spus"), built["signal_month"]))
                for note in built.get("universe_notes", []):
                    print("  universe       %s" % note)
                for missed in built.get("no_reference_price", [])[:5]:
                    print("  skipped        %s: no independent price to check Yahoo's against"
                          % missed)
            else:
                print("momentum         no targets this month: %s" % why)
            momentum_targets = built
        if momentum_targets:
            # Re-screened on every run against today's compliance.screen: an exclusion added
            # mid-month revokes a pick now, and proposals() sells it as a non-pick.
            allowed, revoked = momentum.screen_picks(config, momentum_targets)
            for ticker, why in revoked:
                print("momentum revoked %s: %s" % (ticker, why))
            momentum_targets = dict(momentum_targets, picks=allowed)
            picks = [p["ticker"] for p in momentum_targets["picks"]]
            state["extra_allowlist"] = picks
            print("momentum picks   %s" % " ".join(picks))
            if momentum.in_window(momentum_targets, now.date(), mom.get("window_days", 7),
                                  adapter):
                extra = momentum.proposals(config, state, momentum_targets)
                if mom.get("mode", "shadow") == "live":
                    proposals += extra
                else:
                    # Shadow: evaluated on its own further down, so it cannot use up the
                    # real day's caps or order count, and nothing it proposes is ever sent.
                    shadow_proposals = extra
                print("momentum         %d trade(s) proposed, %s mode"
                      % (len(extra), mom.get("mode", "shadow")))
            else:
                print("momentum         outside the rebalance window (built %s), holding"
                      % momentum_targets.get("built"))

    near_high_history(config, state, proposals)
    print("\n%d proposal(s)" % len(proposals))

    sized, price_history = vet(proposals, state, config, now, ledger, momentum_targets,
                               broker_config, key, secret, cache)

    # Shadow mode: the momentum book runs end to end against the account, through every
    # gate and sizing step, on its own, and nothing it produces is sent. Switched to live in
    # config only once a shadow run has been read and looked right.
    if shadow_proposals:
        print("\nSHADOW: momentum is not live; this is what it would do, and NONE of it is sent")
        ok, refused = gates.decide(shadow_proposals, state, config, now)
        for item in refused:
            p = item["proposal"]
            print("  would be refused %s %s %.2f: %s" % (p["action"], p["ticker"], p["value"],
                                                        "; ".join(item["reasons"])))
        need = sorted({str(p["ticker"]).upper() for p in ok if p["action"] == "buy"
                       and str(p["ticker"]).upper() not in state["positions"]})
        quotes = momentum.outside_prices(need, momentum_targets, config) if need else {}
        for ticker in need:
            print("  outside price  %s: %s" % (ticker, quotes[ticker][1] if ticker in quotes
                                               else "none that two sources agree on"))
        for p in ok:
            order, why = sizing.size(p, state["positions"], price_history, config["risk"],
                                     now.date(), outside=quotes)
            if order is None:
                print("  would not size %s %s: %s" % (p["action"], p["ticker"], why))
            else:
                print("  would %s %s %.4f shares = %.2f (%s)"
                      % (order["action"], order["ticker"], abs(order["quantity"]),
                         order["value_actual"], order["price_source"]))
    # Belt and braces: whatever happened above, a non-live momentum order is never sent.
    if mom.get("mode", "shadow") != "live":
        sized = [o for o in sized if o.get("source") != "momentum"]

    reasons_not_to = []
    if not args.execute:
        reasons_not_to.append("--execute was not passed")
    if not config["execution"].get("execute"):
        reasons_not_to.append("execute is false in config.json")
    if gates.killswitch_path(config).exists():
        reasons_not_to.append("the HALT file exists")
    if state.get("history_unavailable"):
        reasons_not_to.append("today's order count could not be read, so the daily cap "
                              "cannot be enforced")
    if ledger_problem is not None:
        reasons_not_to.append("the order ledger %s could not be read (%s), so the daily cap "
                              "and the guard against sending an order twice cannot be "
                              "enforced; restore it from git or a backup"
                              % (ledger_file, ledger_problem.problem))
    # Holdings or cash unread: read as nothing held, picks already at their share would be
    # bought again past the position cap. A short history read is printed but not blocking
    # here, as it was before; the second pass, which spends a sale's cash, refuses it too.
    unread = [p for p in state["unknown"]
              if p.split(":")[0] in ("positions", "account summary", "cash")
              or p.startswith("a position row")]
    if unread:
        reasons_not_to.append("the account could not be fully read (%s), so nothing is "
                              "sized against it" % "; ".join(unread))

    if reasons_not_to:
        print("\nPROPOSE ONLY, nothing was placed:")
        for reason in reasons_not_to:
            print("  %s" % reason)
        return 0

    if not sized:
        print("\nNothing survived. No orders placed.")
        return 0

    ledger, sent = place(sized, config, broker_config, key, secret, state, ledger, now.date(),
                         ledger_file=ledger_file)
    ledger, again = second_pass(config, broker_config, key, secret, state, sent, ledger,
                                momentum_targets, cache, pause, ledger_file)
    failures = sum(1 for _, ok in sent + again if not ok)
    return 1 if failures else 0


# ---------------------------------------------------------------------- selftest

class FakeBroker:
    """Trading 212 for the selftest: holdings, cash and fills, no network. An accepted order
    fills on the settle_after-th read of the positions after it was sent."""

    def __init__(self, cash, held, prices, history=(), settle_after=0, refuse=(), lag=False,
                 when="2026-10-14T15:00:00+00:00", timeout=(), positions_fail_at=None,
                 positions_empty_at=None, history_fail_at=None, currency="GBP",
                 summary_reply=None):
        self.cash, self.held, self.prices = cash, dict(held), dict(prices)
        # currency: what the summary reports the account in. summary_reply: (status, body)
        # the summary answers instead, as an adapter's refusal.
        self.currency, self.summary_reply = currency, summary_reply
        self.history, self.settle_after = list(history), settle_after
        self.refuse, self.lag, self.when = set(refuse), lag, when
        # timeout: orders placed but answered with nothing, as a timed-out request can be.
        # positions_*_at: the n-th read of the positions is refused, or comes back empty.
        self.timeout = set(timeout)
        self.positions_fail_at, self.positions_empty_at = positions_fail_at, positions_empty_at
        self.history_fail_at, self.history_reads = history_fail_at, 0
        self.pending, self.posts, self.calls, self.position_reads = [], [], [], 0

    def request(self, broker_config, path, key, secret, method="GET", payload=None,
                timeout=30):
        self.calls.append(path)
        if method == "POST":
            ticker, quantity = payload["ticker"].upper(), payload["quantity"]
            self.posts.append((ticker, quantity))
            if ticker in self.refuse:
                return 400, {"type": "/api-errors/insufficient-free-for-stocks-buy"}
            self.pending.append([ticker, quantity, self.settle_after])
            if ticker in self.timeout:
                return None, "network error: timed out"
            return 200, {"id": len(self.posts), "status": "NEW", "filledQuantity": 0}
        if "summary" in path:
            if self.summary_reply:
                return self.summary_reply
            invested = sum(q * self.prices[t] for t, q in self.held.items() if q > 1e-9)
            return 200, dict({"cash": {"availableToTrade": round(self.cash, 2)},
                              "investments": {"currentValue": round(invested, 2)}},
                             **({"currency": self.currency} if self.currency else {}))
        if "positions" in path:
            self.position_reads += 1
            if self.position_reads == self.positions_fail_at:
                return 429, "rate limited"
            self.fill()
            if self.position_reads == self.positions_empty_at:
                return 200, []
            return 200, [{"instrument": {"ticker": t, "currency": "USD"}, "quantity": q,
                          "currentPrice": self.prices[t] * 1.32,
                          "walletImpact": {"currentValue": round(q * self.prices[t], 2)}}
                         for t, q in sorted(self.held.items()) if q > 1e-9]
        if "history" in path:
            self.history_reads += 1
            if self.history_reads == self.history_fail_at:
                return 429, "rate limited"
            return 200, {"items": list(self.history), "nextPagePath": None}
        if "exchanges" in path:
            return 200, []
        if "instruments" in path:
            # As on Trading 212: a US instrument names a schedule the exchanges list does not
            # carry, so its hours come from the configured New York fallback.
            return 200, [{"ticker": t, "workingScheduleId": 999} for t in self.prices]
        raise AssertionError("unexpected request %s" % path)

    def fill(self):
        waiting = []
        for item in self.pending:
            if item[2] > 0:
                item[2] -= 1
                waiting.append(item)
                continue
            ticker, quantity = item[0], item[1]
            self.held[ticker] = self.held.get(ticker, 0.0) + quantity
            self.cash -= quantity * self.prices[ticker]
            if not self.lag:
                self.history.append({"order": {"ticker": ticker, "createdAt": self.when}})
        self.pending = waiting


def cmd_selftest():
    """The second pass, end to end through main() against a fake broker: no network, no
    files touched. Every patched module attribute is put back afterwards."""
    import contextlib
    import io
    import os
    import shutil
    import tempfile

    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    real = gates.load(str(HERE / "config.json"))
    picks = ["P1_US_EQ", "P2_US_EQ", "P3_US_EQ", "P4_US_EQ"]
    targets = {"month": "2026-10", "built": "2026-10-12", "signal_month": "2026-09",
               "universe": 500, "picks": [{"ticker": t, "symbol": t[:2]} for t in picks]}
    prices = dict({t: 100.0 for t in picks}, CORE_US_EQ=5.0, OLD_US_EQ=200.0)

    def settings_for(second=True, mode="rules"):
        config = json.loads(json.dumps(real))
        # The fake speaks Trading 212's dialect, so the run is told it is talking to one.
        config.setdefault("broker", {})["adapter"] = "trading212"
        config["account"] = {"currency": "GBP"}
        config.setdefault("execution", {}).update({
            "execute": True, "environment": "demo", "require_market_open": True,
            "second_pass": {"enabled": second, "wait_seconds": 10, "polls": 3}})
        strategy = config.setdefault("strategy", {})
        strategy["mode"] = mode
        strategy["scheduled_buys"] = []
        strategy["rebalance"] = dict(strategy.get("rebalance", {}), enabled=False)
        strategy["signals"] = dict(strategy.get("signals", {}), enabled=False)
        strategy["momentum"] = dict(strategy.get("momentum", {}), enabled=True, mode="live",
                                    core_ticker="CORE_US_EQ", core_weight=0.2, band=0.1,
                                    window_days=7, min_trade=25.0, cut_hold=0.10)
        config["risk"] = {"max_order_value": 2500.0, "max_daily_spend": 7000.0,
                          "max_orders_per_day": 60, "max_position_value": 1500.0,
                          "max_position_value_by_ticker": {"CORE_US_EQ": 12000.0},
                          "min_cash_buffer": 20.0, "broker_hold": 0.05,
                          "min_position_remainder": 2.0, "cooldown_hours_per_instrument": 120,
                          "min_confidence": 0.7, "allow_buy": True, "allow_sell": True,
                          "near_high": {"enabled": False}, "killswitch_file": "HALT"}
        config["compliance"] = dict(config.get("compliance", {}), allowlist_only=True,
                                    allowlist=[{"ticker": "CORE_US_EQ"}])
        return config

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)

    import pricefeed
    saved = {(broker, "request"): broker.request, (sizing, "load_ledger"): sizing.load_ledger,
             (sizing, "save_ledger"): sizing.save_ledger,
             (sizing, "ledger_path"): sizing.ledger_path,
             (momentum, "load_targets"): momentum.load_targets,
             (momentum, "save_targets"): momentum.save_targets,
             (momentum, "build_targets"): momentum.build_targets,
             (momentum, "outside_prices"): momentum.outside_prices,
             (pricefeed, "fetch_many"): pricefeed.fetch_many,
             (gates, "killswitch_path"): gates.killswitch_path,
             (sys.modules[__name__], "datetime"): datetime}
    missing = object()
    label_was = getattr(broker, "adapter_label", missing)
    env = {k: os.environ.get(k) for k in ("T212_API_KEY", "T212_API_SECRET")}
    scratch = tempfile.mkdtemp(prefix="trade-selftest-")

    def run(fake, config, execute=True):
        """(exit status, output, pauses) of one main() run against fake."""
        path = os.path.join(scratch, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        broker.request = fake.request
        pauses, out = [], io.StringIO()
        argv = ["--config", path, "--env", "demo"] + (["--execute"] if execute else [])
        with contextlib.redirect_stdout(out):
            status = main(argv, pause=pauses.append)
        return status, out.getvalue(), pauses

    def account(**extra):
        held = {"CORE_US_EQ": 400.0, "OLD_US_EQ": 10.0}
        base = dict(cash=100.0, held=held, prices=prices,
                    history=[{"order": {"ticker": "CORE_US_EQ",
                                        "createdAt": "2026-10-01T10:00:00+00:00"}}])
        base.update(extra)
        return FakeBroker(**base)

    try:
        os.environ["T212_API_KEY"], os.environ["T212_API_SECRET"] = "test", "test"
        sys.modules[__name__].datetime = Clock
        # The ledger is never touched on disk, but every path main() reads and writes it
        # through is recorded, to check it is this account's own.
        ledger_paths = []

        def no_ledger(*a, **k):
            ledger_paths.append(a[0] if a else k.get("path"))
            return {}

        def keep_ledger(ledger, *a, **k):
            ledger_paths.append(a[0] if a else k.get("path"))
            return ledger
        sizing.load_ledger, sizing.save_ledger = no_ledger, keep_ledger
        momentum.load_targets = lambda *a, **k: json.loads(json.dumps(targets))
        momentum.save_targets = lambda *a, **k: None

        def no_build(*a, **k):
            raise AssertionError("the selftest must never build targets")
        momentum.build_targets = no_build
        momentum.outside_prices = lambda need, *a, **k: {t: (prices[t], "test")
                                                         for t in need if t in prices}
        pricefeed.fetch_many = lambda *a, **k: ({}, {})
        gates.killswitch_path = lambda config, directory=None: Path(scratch) / "HALT"

        # A month's rotation: the old holding and part of the core are sold, and the picks
        # can only be bought with that cash. 100 cash, OLD 2,000, core 2,000, four picks.
        fake = account()
        status, out, pauses = run(fake, settings_for())
        first = [p for p in fake.posts[:2]]
        later = fake.posts[2:]
        expect("the first pass sells and cannot afford the picks",
               sorted(t for t, q in first) == ["CORE_US_EQ", "OLD_US_EQ"]
               and all(q < 0 for t, q in first) and "SECOND PASS" in out)
        expect("the second pass buys picks with the sale's cash, in the same run",
               len(later) >= 3 and all(q > 0 for t, q in later)
               and {t for t, q in later} <= set(picks))
        expect("it never sells", all(q > 0 for t, q in later))
        expect("it buys a pick only once", len(later) == len({t for t, q in later}))
        expect("it waited for the sales to show before buying", 1 <= len(pauses) <= 3)
        expect("and the run succeeded", status == 0)
        expect("exchanges and instruments are read once, not again in the second pass",
               sum("exchanges" in c for c in fake.calls) == 1
               and sum("instruments" in c for c in fake.calls) == 1)
        expect("each pass announces its orders, so a watcher of the output sees both",
               out.count("\nPLACING ") == 2 and "second pass" in out)

        fake = account(settle_after=99)
        status, out, pauses = run(fake, settings_for())
        expect("sales that never show mean no second pass and no buys",
               all(q < 0 for t, q in fake.posts) and "not settled" in out and len(pauses) == 3)

        # The same faults on the first read of a run: nothing may be sized against an account
        # read as holding nothing. An account that really holds nothing still trades.
        for name, fault in (("refused", {"positions_fail_at": 1}),
                            ("empty", {"positions_empty_at": 1})):
            fake = account(**fault)
            status, out, pauses = run(fake, settings_for())
            expect("a %s positions read in the first pass means nothing is placed" % name,
                   not fake.posts and "could not be fully read" in out)
        fake = account(cash=4000.0, held={})
        status, out, pauses = run(fake, settings_for())
        expect("an account that really holds nothing still trades",
               fake.posts and all(q > 0 for t, q in fake.posts))

        # The fresh read after the sales settle (the third read of the positions) refused or
        # empty: read as holding nothing, it would buy picks already full past the cap.
        for name, fault in (("refused", {"positions_fail_at": 3}),
                            ("empty", {"positions_empty_at": 3})):
            fake = account(**fault)
            status, out, pauses = run(fake, settings_for())
            expect("a %s positions read in the second pass means no buys" % name,
                   fake.posts and all(q < 0 for t, q in fake.posts)
                   and "no second pass" in out)
        # The second pass's history asks for 50 a page, is refused, and falls back to one
        # short default page: read whole, it could forget a cooldown, so nothing is bought.
        fake = account(history_fail_at=2)
        status, out, pauses = run(fake, settings_for())
        expect("a short history read in the second pass means no buys",
               fake.posts and all(q < 0 for t, q in fake.posts)
               and "the fresh read is incomplete" in out)
        fake = account()
        status, out, pauses = run(fake, settings_for())
        expect("the fresh read waits out the rate limit after the settle poll",
               pauses[-1:] == [float(real.get("broker", {}).get("request_delay_seconds", 5))])

        fake = account(timeout={"CORE_US_EQ"})
        status, out, pauses = run(fake, settings_for())
        expect("an order with no answer means no second pass, and the run fails",
               all(q < 0 for t, q in fake.posts) and "no answer came back" in out
               and not pauses and status == 1)

        # Today is the run's UTC day, not the machine's local date.
        fake = account(history=[{"order": {"ticker": "CORE_US_EQ",
                                           "createdAt": "2026-10-14T09:00:00+00:00"}}])
        status, out, pauses = run(fake, settings_for(), execute=False)
        expect("orders are counted for the run's own UTC day", "orders today     1" in out)

        # The broker refuses the old holding's sale and the one pick the first pass could
        # afford, and its history lags, so only the ledger and this run's own record stand
        # between the second pass and sending them again.
        fake = account(cash=1300.0, refuse={"OLD_US_EQ", "P1_US_EQ"}, lag=True)
        status, out, pauses = run(fake, settings_for())
        sent_first = [t for t, q in fake.posts[:3]]
        again = fake.posts[3:]
        expect("a refused buy is not sent again by the second pass, even with history lagging",
               "P1_US_EQ" in sent_first and "P1_US_EQ" not in [t for t, q in again])
        expect("nor is a refused sell, as a second pass only buys",
               all(q > 0 for t, q in again) and again)
        expect("a refused order makes the run fail, so it is noticed", status == 1)

        fake = account()
        status, out, pauses = run(fake, settings_for(), execute=False)
        expect("propose places nothing and does not wait", not fake.posts and not pauses)

        fake = account()
        status, out, pauses = run(fake, settings_for(second=False))
        expect("switched off, there is no second pass",
               all(q < 0 for t, q in fake.posts) and not pauses and "SECOND PASS" not in out)

        # A strategy mode this version does not have is noted, and the rules run instead.
        fake = account()
        status, out, pauses = run(fake, settings_for(mode="combined"), execute=False)
        expect("a strategy mode other than rules is noted and the rules run instead",
               "mode 'combined' is not supported" in out and "strategy         rules" in out
               and status == 0)

        # The broker is named by broker.adapter_label, read defensively.
        broker.adapter_label = lambda config: "fake broker (%s)" % config.get("environment")
        fake = account()
        status, out, pauses = run(fake, settings_for())
        expect("the header and each placing line name the broker by broker.adapter_label",
               "broker           fake broker (demo)" in out
               and out.count("order(s) on fake broker (demo)") == 2)

        def broken(config):
            raise ValueError("no label")
        broker.adapter_label = broken
        failed_label = broker_label({"environment": "demo"})
        broker.adapter_label = None
        expect("a label that fails or is missing falls back to the environment name",
               failed_label == "demo" and broker_label({"environment": "live"}) == "live")

        # Core inside its band and cash for three picks: buys only, nothing sold.
        fake = account(cash=4000.0, held={"CORE_US_EQ": 200.0})
        status, out, pauses = run(fake, settings_for())
        expect("a run that only buys has no second pass", fake.posts and not pauses
               and all(q > 0 for t, q in fake.posts))

        # The second pass on its own, against an account where a fresh look would trim a
        # pick that nothing has traded today: it must still only buy.
        fake = FakeBroker(cash=3280.0, held={"CORE_US_EQ": 164.0, "P2_US_EQ": 30.0},
                          prices=prices)
        broker.request = fake.request
        config = settings_for()
        broker_config = dict(config["broker"], environment="demo")
        first = {"positions": {"CORE_US_EQ": {"quantity": 400.0}}, "orders_today": 0,
                 "cash": 100.0, "extra_allowlist": picks}
        sent = [({"ticker": "CORE_US_EQ", "action": "sell", "quantity": -236.0,
                  "value_actual": 1180.0}, True)]
        with contextlib.redirect_stdout(io.StringIO()):
            ledger, again = second_pass(config, broker_config, "k", "s", first, sent, {},
                                        json.loads(json.dumps(targets)), {},
                                        pause=lambda s: None)
        expect("a second pass never sells, even when a fresh look would",
               again and all(q > 0 for t, q in fake.posts))

        # One ledger per account: on Trading 212 demo it is orders_placed.demo.json, read
        # and written through sizing.ledger_path, never the live account's file.
        del ledger_paths[:]
        fake = account()
        status, out, pauses = run(fake, settings_for())
        demo_ledger = saved[(sizing, "ledger_path")]({"adapter": "trading212",
                                                      "environment": "demo"})
        expect("main reads and writes the ledger of the account it trades, and says which",
               fake.posts and len(ledger_paths) >= 3
               and all(Path(p) == demo_ledger for p in ledger_paths)
               and demo_ledger.name == "orders_placed.demo.json"
               and "ledger           orders_placed.demo.json" in out)

        # A ledger that exists but cannot be read stops placing, and says which file.
        def unreadable(*a, **k):
            raise sizing.LedgerError(demo_ledger, "Expecting value: line 1 column 1 (char 0)")
        sizing.load_ledger = unreadable
        fake = account()
        status, out, pauses = run(fake, settings_for())
        sizing.load_ledger = no_ledger
        expect("an unreadable ledger means propose only, naming the file",
               not fake.posts and status == 0 and "PROPOSE ONLY" in out
               and "the order ledger %s could not be read" % demo_ledger in out)

        # The same on disk, through the real ledger functions in a scratch folder: a run's
        # orders land in its own file, and that file cut off mid-write stops the next run.
        sizing.load_ledger = saved[(sizing, "load_ledger")]
        sizing.save_ledger = saved[(sizing, "save_ledger")]
        sizing.ledger_path = lambda config, directory=None: saved[(sizing, "ledger_path")](
            config, Path(scratch))
        try:
            fake = account()
            status, out, pauses = run(fake, settings_for())
            on_disk = Path(scratch) / "orders_placed.demo.json"
            book = json.loads(on_disk.read_text(encoding="utf-8"))
            expect("the orders sent are in the account's own ledger file, and nothing else is",
                   fake.posts and len(book) == len(fake.posts)
                   and sorted(p.name for p in Path(scratch).iterdir())
                   == ["config.json", "orders_placed.demo.json"])
            whole = on_disk.read_text(encoding="utf-8")
            on_disk.write_text(whole[:len(whole) // 2], encoding="utf-8")
            fake = account()
            status, out, pauses = run(fake, settings_for())
            expect("a ledger cut off mid-write is not read as empty: nothing is placed",
                   not fake.posts and "could not be read" in out and str(on_disk) in out)
        finally:
            sizing.load_ledger, sizing.save_ledger = no_ledger, keep_ledger
            sizing.ledger_path = saved[(sizing, "ledger_path")]

        # The account's currency is checked against config account.currency: run as GBP, a
        # USD account would size every new pick and count every buy in the wrong currency.
        fake = account(currency="USD")
        status, out, pauses = run(fake, settings_for())
        expect("a broker reporting another currency than account.currency places nothing",
               not fake.posts and "could not be fully read" in out
               and "reports the account in USD" in out and "account.currency is GBP" in out)
        fake = account(currency="gbp")
        status, out, pauses = run(fake, settings_for())
        expect("the same currency in any case trades", fake.posts and status == 0)
        fake = account(currency=None)
        status, out, pauses = run(fake, settings_for())
        expect("a summary that names no currency is not refused for it", fake.posts)

        # An adapter's refusal reaches the output with its own reason, not just a status.
        fake = account(summary_reply=(409, {
            "type": "paper-currency-changed",
            "detail": "the paper account is in GBP but the account currency is now EUR; "
                      "run brokers/paper.py reset to start again in EUR"}))
        status, out, pauses = run(fake, settings_for())
        expect("a refused summary prints the broker's own detail and places nothing",
               not fake.posts and "(409): the paper account is in GBP" in out
               and "run brokers/paper.py reset" in out)

        # Targets built from another adapter's instruments are rebuilt from this one's, and
        # what is built carries the adapter. The same adapter's targets are kept.
        builds, kept = [], []

        def build(config, today, instruments, *a, **k):
            builds.append(len(instruments))
            return dict(json.loads(json.dumps(targets)), built="2026-10-14"), None
        momentum.build_targets = build
        momentum.save_targets = lambda config, made, *a, **k: kept.append(made)
        momentum.load_targets = lambda *a, **k: dict(json.loads(json.dumps(targets)),
                                                     adapter="paper")
        fake = account()
        status, out, pauses = run(fake, settings_for(), execute=False)
        expect("targets built on paper are rebuilt on Trading 212, and stamped with it",
               builds and kept and kept[-1].get("adapter") == "trading212"
               and "momentum         built 2026-10" in out)
        del builds[:]
        momentum.load_targets = lambda *a, **k: dict(json.loads(json.dumps(targets)),
                                                     adapter="trading212")
        fake = account()
        status, out, pauses = run(fake, settings_for(), execute=False)
        expect("targets built on the same adapter are kept for the month", not builds)
        momentum.build_targets = no_build
        momentum.save_targets = lambda *a, **k: None
        momentum.load_targets = lambda *a, **k: json.loads(json.dumps(targets))

        held = {"AAA": {"quantity": 2.0}}
        done, _ = settled([({"ticker": "AAA", "quantity": -2.0}, True)], held, [])
        expect("a whole sale is settled once the holding is gone", done)
        done, waiting = settled([({"ticker": "AAA", "quantity": -2.0}, True)], held,
                                [{"instrument": {"ticker": "AAA"}, "quantity": 1.0}])
        expect("half a sale is still waiting", not done and waiting == ["AAA"])
        done, _ = settled([({"ticker": "BBB", "quantity": 1.5}, False)], held, [])
        expect("an order the broker refused is not waited for", done)
    finally:
        for (module, name), value in saved.items():
            setattr(module, name, value)
        if label_was is missing:
            if hasattr(broker, "adapter_label"):
                delattr(broker, "adapter_label")
        else:
            broker.adapter_label = label_was
        shutil.rmtree(scratch, ignore_errors=True)
        for name, value in env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    width = max(len(n) for n, _ in checks)
    for name, passed in checks:
        print("%s  %s" % ("pass" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in checks if not p]
    print("\n%d checks, %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(cmd_selftest() if sys.argv[1:2] == ["selftest"] else main())
