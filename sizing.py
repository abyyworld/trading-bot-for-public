#!/usr/bin/env python3
"""
Turning "buy 250 worth of the core fund" into something Trading 212 will accept, safely.

Trading 212's order body is {ticker, quantity}. There is no monetary value field, so an
amount of money has to become a fractional share quantity, which needs a price. The API will
not give you one for an instrument you do not already hold: there is no quotes endpoint,
and currentPrice only appears on existing positions.

So price comes from, in order:

  1. the position itself, if you already hold it, which is the freshest thing available
  2. the latest close in prices.csv, if it is not stale
  3. nothing, in which case the order is refused and says so

Two rules that exist because getting them wrong costs money:

  Round DOWN, never to nearest. Rounding up can push an order past max_order_value, which
  would mean the risk cap silently did not hold. After rounding, the value is recomputed and
  checked again against the cap, because the check that matters is on what will actually be
  sent, not on what was asked for.

  Keep a local ledger. Trading 212's own documentation warns the API is not idempotent and
  that duplicate requests may create duplicate orders. A timed-out request that actually
  succeeded, or a workflow that runs twice, would otherwise double your position. The daily
  cap alone does not save you, because it is reconstructed from an order history read that
  lags.

    python3 sizing.py selftest
"""

import argparse
import json
import math
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Trading 212's live account's ledger. Every other account has its own: see ledger_path.
DEFAULT_LEDGER = HERE / "orders_placed.json"

QUANTITY_DECIMALS = 4


def price_for(ticker, positions, history, today=None, max_staleness_days=5, outside=None):
    """Return (price, source) or (None, reason). Never guesses.

    outside is {TICKER: (price, source)} for stocks not yet held, from
    momentum.outside_prices: two sources that had to agree, already in the account currency.
    It is only consulted after the account's own position price, which is always preferred.
    """
    ticker = ticker.upper()
    today = today or date.today()

    held = (positions or {}).get(ticker) or {}
    live = held.get("current_price")
    if isinstance(live, (int, float)) and live > 0:
        return float(live), "current position price"

    quote = (outside or {}).get(ticker)
    if quote and isinstance(quote[0], (int, float)) and quote[0] > 0:
        return float(quote[0]), "outside price: %s" % quote[1]

    series = (history or {}).get(ticker) or []
    if series:
        when, close = series[-1]
        age = (today - when).days
        if age > max_staleness_days:
            return None, ("newest price for %s is %d days old, limit is %d"
                          % (ticker, age, max_staleness_days))
        if close > 0:
            return float(close), "prices.csv close from %s" % when.isoformat()

    return None, ("no price available for %s: not held, and no usable row in prices.csv"
                  % ticker)


def account_price(quantity, current_price, wallet_value):
    """Price per share in the ACCOUNT currency, which is what every value here is in.

    Trading 212 quotes currentPrice in the instrument's own currency, while cash, the caps and
    every proposal value are in the account currency. A GBP account holding US shares shows
    the gap: 250 pounds of a US stock sized at its dollar price is 250 DOLLARS of shares,
    about a quarter short. The broker's own valuation of the holding,
    walletImpact.currentValue, is already in account currency with FX applied, so dividing it
    by the quantity gives the price in the right currency. Without it, fall back to
    currentPrice, which is correct whenever the two currencies are the same.
    """
    if (isinstance(wallet_value, (int, float)) and isinstance(quantity, (int, float))
            and wallet_value > 0 and quantity > 0):
        return wallet_value / quantity
    return current_price


def broker_ticker(ticker, spellings):
    """The broker's own spelling of a ticker, for the order body and nothing else.

    Everything in this project compares tickers in upper case, but Trading 212's are
    case-sensitive: a London listing ends in a lower-case l, so the fund is MWIXl_EQ and
    MWIXL_EQ is not an instrument. A US ticker is upper case already, which is why sending
    the upper-cased form goes unnoticed until the first London order. spellings is every
    form seen verbatim, allowlist first so it wins; with no match the ticker is unchanged.
    """
    wanted = str(ticker or "").strip()
    for spelling in spellings:
        spelling = str(spelling or "").strip()
        if spelling and spelling.upper() == wanted.upper():
            return spelling
    return wanted


def quantity_for(value, price, decimals=QUANTITY_DECIMALS):
    """Shares for an amount of money, rounded DOWN so the cap can never be exceeded."""
    if price is None or price <= 0 or value <= 0:
        return 0.0
    factor = 10 ** decimals
    return math.floor((value / price) * factor) / factor


def sell_quantity(wanted, holding, price, keep_value, decimals=QUANTITY_DECIMALS):
    """(shares to sell, how) or (None, reason), positive; the caller signs it.

    Two broker rules, both learned from rejected sells:

      At most 4 decimal places. A purchase by value in the app can leave 8, such as
      1.23456789 shares, and rounding that to nearest gives 1.2346, which is more than is
      owned: "Selling more equities than owned". Sending all 8 places is refused too, as
      "invalid quantity precision 4".
      No dust left behind. Rounding down instead leaves a sliver, and a sell that leaves a
      remainder under 1.00 is refused: "must have opened position at least 1.00". Whether
      that is the account currency, dollars or shares is not documented.

    So a holding that already fits in 4 places, which is every one the bot bought itself,
    is sold exactly. Any other whole exit keeps keep_value behind, rounded so the remainder
    only grows, and a partial sell that would leave less than keep_value becomes that exit.
    The small remainder left over is real money still in the account, worth too little to
    trade; the app's own sell-all clears it.
    """
    factor = 10 ** decimals
    if holding <= 0 or price is None or price <= 0:
        return None, "nothing held to sell"
    if 0 < wanted < holding and (holding - wanted) * price >= keep_value:
        return wanted, "part of the holding"
    if abs(holding * factor - round(holding * factor)) < 1e-6:
        return round(holding, decimals), "the whole holding"
    quantity = math.floor((holding - keep_value / price) * factor) / factor
    if quantity <= 0:
        return None, ("%.8f shares is worth under %.2f, too little to sell without leaving "
                      "a remainder the broker refuses" % (holding, keep_value))
    return quantity, ("all but %.8f shares, as %.8f has more than %d decimal places"
                      % (holding - quantity, holding, decimals))


def size(proposal, positions, history, limits, today=None, outside=None):
    """Attach a quantity to a proposal, or refuse it with a reason.

    Returns (sized proposal or None, reason). The returned proposal carries `quantity`
    signed the way Trading 212 wants it: positive buys, negative sells.
    """
    ticker = str(proposal.get("ticker", "")).upper()
    action = str(proposal.get("action", "")).lower()
    try:
        wanted = float(proposal.get("value", 0))
    except (TypeError, ValueError):
        return None, "value is not a number"
    if wanted <= 0:
        return None, "value must be positive"

    price, source = price_for(ticker, positions, history, today, outside=outside)
    if price is None:
        return None, source

    quantity = quantity_for(wanted, price)
    if quantity <= 0:
        return None, ("%.2f buys less than the smallest tradeable fraction of %s at %.4f"
                      % (wanted, ticker, price))

    # The value that will actually be sent, not the one that was asked for.
    actual = round(quantity * price, 2)

    # Buys only, and for the same reason gates.py applies it to buys only: the cap exists to
    # limit what can be SPENT. Applying it to a sell traps you in any position larger than the
    # cap, which is worst in the case that matters most, getting out of something. gates.py was
    # fixed for this first and this second copy of the check was missed, so a rebalance sell
    # several times the per-order cap was refused.
    if action == "buy":
        cap = limits.get("max_order_value")
        if cap is not None and actual > float(cap):
            return None, ("rounded to %.4f shares which is %.2f, over the %.2f cap"
                          % (quantity, actual, float(cap)))

    if action == "sell":
        holding = float((positions or {}).get(ticker, {}).get("quantity", 0))
        if holding <= 0:
            return None, "cannot sell %s, none held" % ticker
        keep = float(limits.get("min_position_remainder", 2.0))
        quantity, how = sell_quantity(quantity, holding, price, keep)
        if quantity is None:
            return None, how
        source = "%s, %s" % (source, how)
        actual = round(quantity * price, 2)
        quantity = -quantity

    sized = dict(proposal)
    sized["quantity"] = round(quantity, QUANTITY_DECIMALS)
    sized["price_used"] = price
    sized["price_source"] = source
    sized["value_actual"] = actual
    return sized, "sized at %.4f shares using %s" % (abs(quantity), source)


# ------------------------------------------------------------------- the ledger

class LedgerError(ValueError):
    """The ledger file exists but cannot be used. Read as empty, it would forget today's
    orders: the daily cap would start from nothing and an order already sent would look new."""

    def __init__(self, path, problem):
        ValueError.__init__(self, "order ledger %s cannot be read: %s" % (path, problem))
        self.path, self.problem = Path(path), problem


def ledger_path(broker_config, directory=HERE):
    """The ledger file for the account broker_config points at: one file per account.

    The ledger counts the day's spend and refuses a repeat, so an account must never see
    another's orders: a morning on the paper account would otherwise use up a real
    account's daily cap, and refuse its buys as already placed. Trading 212 live keeps the
    plain orders_placed.json; Trading 212 demo is orders_placed.demo.json, and any other
    adapter orders_placed.<adapter>.json, so paper is orders_placed.paper.json. A missing
    adapter is Trading 212, as in broker.adapter_name.
    """
    def safe(text):
        return re.sub(r"[^a-z0-9_]+", "_", str(text).strip().lower()) or "unknown"

    broker_config = broker_config or {}
    adapter = safe(broker_config.get("adapter") or "trading212")
    if adapter == "trading212":
        environment = safe(broker_config.get("environment") or "demo")
        name = ("orders_placed.json" if environment == "live"
                else "orders_placed.%s.json" % environment)
    else:
        name = "orders_placed.%s.json" % adapter
    return Path(directory) / name


def load_ledger(path=DEFAULT_LEDGER):
    """The ledger as a dict: {} only when the file does not exist yet. A file that exists
    but cannot be read, does not parse (cut off by a crash, or empty) or is not an object
    raises LedgerError, so the run can refuse to place rather than start the day from
    nothing."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LedgerError(path, error)
    if not isinstance(data, dict):
        raise LedgerError(path, "it holds a %s, not an object" % type(data).__name__)
    return data


def order_key(when, ticker, action):
    """One buy and one sell per instrument per day is the granularity we dedupe at."""
    return "%s|%s|%s" % (when.isoformat(), str(ticker).upper(), str(action).lower())


def already_placed(ledger, when, ticker, action):
    return order_key(when, ticker, action) in (ledger or {})


def record_order(ledger, when, ticker, action, quantity, response=None, value=None):
    ledger = dict(ledger or {})
    entry = {
        "quantity": quantity,
        "response": str(response)[:200] if response is not None else "",
    }
    if isinstance(value, (int, float)):
        entry["value"] = round(abs(float(value)), 2)
    ledger[order_key(when, ticker, action)] = entry
    return ledger


def spent_on(ledger, when, unknown_value):
    """Buying this bot sent on a day, in the account currency, for the daily spend cap.

    The cap has to see what EARLIER runs spent, not just this one: there are many runs a
    weekday, and each used to start from zero, so max_daily_spend only ever bound within a
    single run. The ledger is the source because it is written before every request and
    committed back after every run. A failed order still counts, which errs towards
    spending less. A buy with no recorded value counts as unknown_value, the per-order cap,
    rather than as nothing. Hand purchases are not in it: the cap limits the bot, not you.
    """
    prefix = "%s|" % when.isoformat()
    total = 0.0
    for key, entry in (ledger or {}).items():
        if not (key.startswith(prefix) and key.endswith("|buy")):
            continue
        value = entry.get("value") if isinstance(entry, dict) else None
        total += float(value) if isinstance(value, (int, float)) else float(unknown_value)
    return total


def save_ledger(ledger, path=DEFAULT_LEDGER, keep_days=90, today=None):
    """Persist, dropping entries older than keep_days so it cannot grow without bound.

    Atomic: written whole to a temporary file in the same folder, flushed to disk, then
    renamed over the ledger. Writing in place truncates first, so a run stopped mid-write
    (a docker stop, a full disk, a power cut) left a cut-off file; the old file stays as
    it was until the new one is complete, and the temporary file is removed on failure.
    """
    today = today or date.today()
    cutoff = (today - timedelta(days=keep_days)).isoformat()
    trimmed = {k: v for k, v in (ledger or {}).items() if k.split("|")[0] >= cutoff}
    path = Path(path)
    handle, temp = tempfile.mkstemp(prefix=".%s." % path.name, suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(trimmed, out, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, str(path))
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return trimmed


# ---------------------------------------------------------------------- selftest

def cmd_selftest(args):
    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    today = date(2026, 9, 12)
    limits = {"max_order_value": 25.0}
    positions = {"HELD": {"current_price": 10.0, "quantity": 3.0}}
    history = {"CSV": [(date(2026, 9, 11), 20.0)],
               "STALE": [(date(2026, 1, 1), 20.0)]}

    # Price sourcing.
    price, source = price_for("HELD", positions, history, today)
    expect("a held position supplies its own price", price == 10.0 and "position" in source)
    price, source = price_for("CSV", positions, history, today)
    expect("prices.csv supplies a price when not held", price == 20.0)
    price, reason = price_for("STALE", positions, history, today)
    expect("a stale price is refused, not used", price is None and "days old" in reason)
    price, reason = price_for("NOWHERE", positions, history, today)
    expect("an unknown ticker gets no price and a reason", price is None and "no price" in reason)

    # Rounding must go down, always.
    expect("quantity rounds down, never up", quantity_for(10.0, 3.0) == 3.3333)
    expect("an exact division is unaffected", quantity_for(10.0, 2.0) == 5.0)
    expect("a zero price yields nothing rather than dividing by zero",
           quantity_for(10.0, 0) == 0.0)
    expect("rounding down means value is never exceeded", 3.3333 * 3.0 <= 10.0)

    # Sizing, and the cap re-check after rounding.
    sized, why = size({"ticker": "CSV", "action": "buy", "value": 25.0}, positions, history,
                      limits, today)
    expect("a normal buy sizes", sized and sized["quantity"] == 1.25)
    expect("and records the price it used", sized["price_used"] == 20.0)
    expect("and the value actually being sent", sized["value_actual"] == 25.0)

    sized, why = size({"ticker": "CSV", "action": "buy", "value": 30.0}, positions, history,
                      limits, today)
    expect("a buy over the cap is refused after rounding", sized is None and "cap" in why)

    # The cap must not apply to sells, or you are trapped in anything bigger than it: a
    # rebalance sell many times the per-order cap must still go through.
    big = {"HELD": {"current_price": 10.0, "quantity": 200.0}}
    sized, why = size({"ticker": "HELD", "action": "sell", "value": 1500.0}, big, history,
                      limits, today)
    expect("a sell far over the order cap is still allowed", sized is not None)
    expect("and it is negative, sized to the holding", sized and sized["quantity"] < 0)

    dear = {"EXPENSIVE": [(date(2026, 9, 11), 1e9)]}
    sized, why = size({"ticker": "EXPENSIVE", "action": "buy", "value": 25.0}, {}, dear,
                      limits, today)
    expect("too small to buy a tradeable fraction is refused",
           sized is None and "smallest tradeable fraction" in why)

    sized, why = size({"ticker": "NOWHERE", "action": "buy", "value": 25.0}, positions,
                      history, limits, today)
    expect("no price means no order", sized is None)

    # Sells are negative, and cannot exceed the holding.
    sized, why = size({"ticker": "HELD", "action": "sell", "value": 20.0}, positions, history,
                      limits, today)
    expect("a sell is negative quantity", sized and sized["quantity"] < 0)
    expect("a sell is capped at what is actually held", abs(sized["quantity"]) <= 3.0)

    sized, why = size({"ticker": "CSV", "action": "sell", "value": 20.0}, positions, history,
                      limits, today)
    expect("selling something not held is refused", sized is None and "none held" in why)

    # Account currency. A GBP account holding a US stock: 2.5 shares, currentPrice 240.00
    # dollars, walletImpact.currentValue 480.00 pounds, so 192.00 pounds a share.
    gbp = account_price(2.5, 240.0, 480.0)
    expect("a GBP account prices a dollar stock in pounds", abs(gbp - 192.0) < 0.01)
    expect("no wallet value falls back to currentPrice", account_price(2.0, 50.0, None) == 50.0)
    expect("zero quantity falls back to currentPrice", account_price(0.0, 50.0, 100.0) == 50.0)
    abc = {"ABC_US_EQ": {"current_price": gbp, "quantity": 2.5, "value": 480.0}}
    sized, _ = size({"ticker": "ABC_US_EQ", "action": "sell", "value": 250.0}, abc, history,
                    limits, today)
    expect("250 pounds of a dollar stock sells 250 pounds of it, not 250 dollars",
           sized and 249.0 <= sized["value_actual"] <= 250.0)

    # Whole exits. A holding bought by value in the app has 8 decimal places, 1.23456789
    # here: sending 1.2346 sells more than is owned, and rounding down instead leaves a
    # sliver under 1.00, which the broker refuses too. Momentum asks for 1.02 times the
    # holding to mean all.
    odd = {"ODD": {"current_price": 150.0, "quantity": 1.23456789, "value": 185.19}}
    sized, why = size({"ticker": "ODD", "action": "sell", "value": 185.19 * 1.02}, odd,
                      history, limits, today)
    sold = abs(sized["quantity"]) if sized else 0.0
    expect("a whole exit never sells more than is owned", sized and sold <= 1.23456789)
    expect("and leaves at least 2.00 behind, not a sliver the broker refuses",
           sized and (1.23456789 - sold) * 150.0 >= 2.0)
    expect("and sells all the rest of it", sized and (1.23456789 - sold) * 150.0 < 2.1)
    expect("in at most 4 decimal places", sized and round(sold, 4) == sold)
    expect("and the value sent is what is actually sold",
           sized and sized["value_actual"] == round(sold * 150.0, 2))
    mine = {"EVEN": {"current_price": 500.0, "quantity": 0.8125, "value": 406.25}}
    sized, why = size({"ticker": "EVEN", "action": "sell", "value": 406.25 * 1.02}, mine,
                      history, limits, today)
    expect("a holding the bot bought in 4 places is sold exactly, nothing left",
           sized and sized["quantity"] == -0.8125)
    sized, _ = size({"ticker": "EVEN", "action": "sell", "value": 405.0}, mine, history,
                    limits, today)
    expect("a partial sell that would leave dust becomes the whole exit",
           sized and sized["quantity"] == -0.8125)
    crumb = {"BIT": {"current_price": 300.0, "quantity": 0.00512345, "value": 1.54}}
    sized, why = size({"ticker": "BIT", "action": "sell", "value": 5.0}, crumb, history,
                      limits, today)
    expect("a crumb too small to sell cleanly is refused, with the reason",
           sized is None and "too little" in why)
    expect("sell_quantity leaves a partial sell alone when the rest is worth keeping",
           sell_quantity(0.5, 1.23456789, 150.0, 2.0) == (0.5, "part of the holding"))

    # Malformed input.
    for bad in ({"ticker": "CSV", "action": "buy", "value": "lots"},
                {"ticker": "CSV", "action": "buy", "value": -5},
                {}):
        sized, _ = size(bad, positions, history, limits, today)
        expect("malformed proposal %r is refused" % (bad.get("value", "empty")), sized is None)

    # The ledger.
    ledger = {}
    expect("a fresh ledger has not placed anything",
           not already_placed(ledger, today, "CSV", "buy"))
    ledger = record_order(ledger, today, "CSV", "buy", 1.25, "ok")
    expect("after recording, the same order is a duplicate",
           already_placed(ledger, today, "CSV", "buy"))
    expect("a sell of the same ticker is not a duplicate of the buy",
           not already_placed(ledger, today, "CSV", "sell"))
    expect("tomorrow is not a duplicate",
           not already_placed(ledger, today + timedelta(days=1), "CSV", "buy"))
    expect("ticker case does not defeat the dedupe",
           already_placed(ledger, today, "csv", "BUY"))

    day = record_order({}, today, "AAA", "buy", 1.0, "sending", value=250.0)
    day = record_order(day, today, "AAA", "buy", 1.0, "{'id': 1}", value=250.0)
    expect("the response overwrites, keeping the value", day[order_key(today, "AAA", "buy")]
           == {"quantity": 1.0, "response": "{'id': 1}", "value": 250.0})
    day = record_order(day, today, "BBB", "sell", -2.0, "ok", value=-90.0)
    day = record_order(day, today - timedelta(days=1), "CCC", "buy", 1.0, "ok", value=250.0)
    expect("today's spend counts today's buys only, across runs",
           spent_on(day, today, 250.0) == 250.0)
    day[order_key(today, "DDD", "buy")] = {"quantity": 1.0, "response": "old"}
    expect("a buy with no recorded value counts as the per-order cap, not as zero",
           spent_on(day, today, 250.0) == 500.0)
    expect("no ledger is nothing spent", spent_on({}, today, 250.0) == 0.0)

    outside = {"NEW_US_EQ": (80.0, "yahoo 100.00 USD at GBPUSD 1.25")}
    price, source = price_for("NEW_US_EQ", {}, {}, today, outside=outside)
    expect("an outside price prices a stock not yet held", price == 80.0 and "outside" in source)
    held_too = {"NEW_US_EQ": {"current_price": 81.0, "quantity": 1.0}}
    expect("the account's own price beats an outside one",
           price_for("NEW_US_EQ", held_too, {}, today, outside=outside)[0] == 81.0)
    expect("a zero outside price is no price",
           price_for("NEW_US_EQ", {}, {}, today, outside={"NEW_US_EQ": (0.0, "x")})[0] is None)
    sized, why = size({"ticker": "NEW_US_EQ", "action": "buy", "value": 400.0}, {}, {},
                      {"max_order_value": 500}, today, outside=outside)
    expect("a new pick sizes from its outside price", sized and sized["quantity"] == 5.0)

    spellings = ["MSFT_US_EQ", "MWIXl_EQ"]
    expect("a London ticker goes out with its lower-case l",
           broker_ticker("MWIXL_EQ", spellings) == "MWIXl_EQ")
    expect("a US ticker is unchanged", broker_ticker("MSFT_US_EQ", spellings) == "MSFT_US_EQ")
    expect("an unknown ticker is sent as given", broker_ticker("ABCd_EQ", spellings) == "ABCd_EQ")
    expect("the first spelling, the allowlist's, wins",
           broker_ticker("ABCL_EQ", ["ABCl_EQ", "ABCL_EQ"]) == "ABCl_EQ")

    old = record_order({}, date(2020, 1, 1), "OLD", "buy", 1.0)
    kept = {k: v for k, v in {**old, **ledger}.items()
            if k.split("|")[0] >= (today - timedelta(days=90)).isoformat()}
    expect("entries older than the window are dropped", "OLD" not in str(kept))

    # One ledger per account, so a paper morning neither uses up a real account's daily cap
    # nor makes its buys look already placed.
    folder = Path("/ledgers")
    paths = [ledger_path({"adapter": "paper"}, folder),
             ledger_path({"adapter": "trading212", "environment": "demo"}, folder),
             ledger_path({"adapter": "trading212", "environment": "live"}, folder)]
    expect("paper, Trading 212 demo and Trading 212 live keep three different ledgers",
           [p.name for p in paths] == ["orders_placed.paper.json", "orders_placed.demo.json",
                                       "orders_placed.json"] and len(set(paths)) == 3)
    expect("Trading 212 live keeps the plain name, and a missing adapter is Trading 212",
           ledger_path({"environment": "live"}, folder) == folder / "orders_placed.json"
           and ledger_path({}, folder).name == "orders_placed.demo.json"
           and ledger_path({"adapter": "Paper"}).parent == HERE)
    expect("another adapter gets its own, and its name cannot leave the folder",
           ledger_path({"adapter": "alpaca"}, folder).name == "orders_placed.alpaca.json"
           and ledger_path({"adapter": "../x"}, folder).parent == folder)

    # The ledger on disk: written atomically, and a damaged one fails closed.
    import shutil
    scratch = Path(tempfile.mkdtemp(prefix="sizing-selftest-"))
    try:
        book = scratch / "orders_placed.paper.json"
        expect("a ledger that does not exist yet is empty", load_ledger(book) == {})
        day = record_order({}, today, "AAA", "buy", 1.5, "ok", value=300.0)
        save_ledger(day, book, today=today)
        expect("a saved ledger reads back as it was", load_ledger(book) == day)
        expect("and no temporary file is left behind",
               [p.name for p in scratch.iterdir()] == [book.name])
        try:
            save_ledger(dict(day, **{"%s|BAD|buy" % today.isoformat(): {"x": object()}}),
                        book, today=today)
            raised = False
        except TypeError:
            raised = True
        expect("a write that fails part way leaves the old ledger whole and no temporary file",
               raised and load_ledger(book) == day
               and [p.name for p in scratch.iterdir()] == [book.name])
        whole = book.read_text(encoding="utf-8")
        for name, text in (("cut off mid-write", whole[:len(whole) // 2]), ("empty", ""),
                           ("a list, not an object", "[]")):
            book.write_text(text, encoding="utf-8")
            try:
                load_ledger(book)
                problem = None
            except LedgerError as error:
                problem = error
            expect("a ledger that is %s raises, naming the file, rather than reading as empty"
                   % name, problem is not None and str(book) in str(problem))
        expect("LedgerError is a ValueError, for callers that catch that",
               issubclass(LedgerError, ValueError))
    finally:
        shutil.rmtree(str(scratch), ignore_errors=True)

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
