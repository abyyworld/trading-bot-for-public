#!/usr/bin/env python3
"""
A skeleton for another broker, such as Alpaca or Interactive Brokers. Copy it to
brokers/<name>.py, set broker.adapter to "<name>" in config.json, and fill in each function
below until `python3 broker.py check` reports every endpoint working. Every call it does
not answer raises NotImplementedError, so a half-finished adapter stops a run loudly
instead of trading on a guess.

The rest of the project speaks Trading 212's dialect (brokers/__init__.py has the shapes;
brokers/paper.py is a complete, tested example). An adapter's whole job is translation:
take a Trading 212 path, ask the other broker, answer in Trading 212's shape. The things
that matter most, because the gates and the sizing rely on them:

  money       cash.availableToTrade and walletImpact.currentValue in the ACCOUNT currency;
              currentPrice in the instrument's own units.
  tickers     the runner compares tickers upper-cased but sends the broker's own spelling;
              pick one spelling per instrument (Trading 212's SYMBOL_US_EQ is easiest, as
              momentum.t212_tickers and pricefeed.base_symbol already understand it) and
              translate it to and from the other broker's symbol here.
  history     every order, newest first, with createdAt (ISO 8601 with an offset) and the
              ticker under order.ticker: the daily order cap and the per-instrument
              cooldown are counted from it. A history that cannot be read must be an error
              status, never an empty list, or both counters fail open.
  orders      quantity signed (positive buys, negative sells), at most 4 decimal places.
              A timeout is status None, since the order may have been placed: the runner's
              ledger then blocks a resend. Return 200 only when the broker accepted it.
  hours       exchanges may return [] for venues the config's market_hours_fallback covers
              (keyed by ticker suffix); an instrument with no schedule and no fallback is
              never traded, which is the safe failure.

Credentials: NEEDS_CREDENTIALS is True, so broker.credentials() demands the key and secret
named in broker.auth. Read your own names with broker.secret_value("ALPACA_API_KEY"),
which looks in the environment and then in the project's .env file. Never print them.
"""

import urllib.parse

NEEDS_CREDENTIALS = True
LABEL = "template (not implemented, places nothing)"

# How each Trading 212 endpoint might map, for orientation only: check every one against the
# other broker's current documentation before relying on it.
#
#   endpoint              Alpaca (paper-api / api.alpaca.markets)    IBKR Client Portal API
#   account_summary       GET /v2/account: cash, equity               /portfolio/{id}/summary
#   positions             GET /v2/positions: qty, current_price,      /portfolio/{id}/positions/0
#                         market_value (USD account)
#   history_orders        GET /v2/orders?status=all&direction=desc    /iserver/account/orders
#   pending_orders        GET /v2/orders?status=open                  /iserver/account/orders
#   instruments           GET /v2/assets?status=active                /trsrv/stocks?symbols=...
#   exchanges             GET /v2/calendar, or [] and the fallback    [] and the fallback
#   place_market_order    POST /v2/orders {symbol, qty, side,         /iserver/account/{id}/orders
#                         type: market, time_in_force: day}
#   dividends, transactions  GET /v2/account/activities               /pa/transactions


def endpoint_of(broker_config, path):
    """(endpoint name, {query}) for a Trading 212 path, or (None, query)."""
    parts = urllib.parse.urlsplit(str(path))
    bare = parts.path[len("/api/v0"):] if parts.path.startswith("/api/v0/") else parts.path
    query = dict(urllib.parse.parse_qsl(parts.query))
    for name, value in (broker_config.get("endpoints") or {}).items():
        if value == bare:
            return name, query
    return None, query


def account_summary(broker_config):
    """-> (200, {"currency": "USD", "cash": {"availableToTrade": 0.0},
                 "investments": {"currentValue": 0.0}})"""
    raise NotImplementedError("account_summary: map the broker's cash and holdings value")


def positions(broker_config):
    """-> (200, [{"instrument": {"ticker", "currency"}, "quantity", "currentPrice",
                  "averagePricePaid", "walletImpact": {"currentValue", "currency"}}])"""
    raise NotImplementedError("positions: one row per holding, ticker under instrument")


def history_orders(broker_config, query):
    """-> (200, {"items": [{"order": {"id", "ticker", "quantity", "filledQuantity",
                                      "status", "createdAt", "type"}, "fill": {}}],
                 "nextPagePath": None or "/equity/history/orders?limit=50&cursor=..."})
    Newest first; honour query["limit"] (up to 50) and query["cursor"]."""
    raise NotImplementedError("history_orders: every order, newest first, paged")


def pending_orders(broker_config):
    """-> (200, [orders not yet filled, in the history's order shape])"""
    raise NotImplementedError("pending_orders")


def instruments(broker_config):
    """-> (200, [{"ticker", "shortName", "name", "type": "STOCK", "currencyCode",
                  "workingScheduleId"}])"""
    raise NotImplementedError("instruments: every tradeable share, US shares as SYMBOL_US_EQ")


def exchanges(broker_config):
    """-> (200, [{"id", "name", "workingSchedules": [{"id", "timeEvents":
                  [{"date", "type": "OPEN" or "CLOSE"}]}]}]), or (200, []) to leave the
    hours to config execution.market_hours_fallback."""
    raise NotImplementedError("exchanges: published sessions, or [] for the fallback")


def place_market_order(broker_config, payload):
    """payload {"ticker", "quantity"}: quantity > 0 buys, < 0 sells, at most 4 decimals.
    -> (200, {"id", "ticker", "quantity", "status"}) when accepted, (400, {"type", "detail"})
    when refused, (None, "network error: ...") when no answer came back."""
    raise NotImplementedError("place_market_order: a market order, signed quantity")


def request(broker_config, path, method="GET", payload=None, timeout=30):
    """The adapter entry point broker.request calls. Returns (status, body)."""
    name, query = endpoint_of(broker_config, path)
    method = str(method or "GET").upper()
    if name == "place_market_order" and method == "POST":
        return place_market_order(broker_config, payload)
    if name in ("history_dividends", "history_transactions"):
        return 200, {"items": [], "nextPagePath": None}
    handlers = {"account_summary": account_summary, "positions": positions,
                "pending_orders": pending_orders, "instruments": instruments,
                "exchanges": exchanges}
    if name == "history_orders":
        return history_orders(broker_config, query)
    if name in handlers and method == "GET":
        return handlers[name](broker_config)
    return 404, {"type": "/api-errors/not-found",
                 "detail": "the template adapter has no endpoint %s %s" % (method, path)}


if __name__ == "__main__":
    print(__doc__)
