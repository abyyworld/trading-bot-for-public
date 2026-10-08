#!/usr/bin/env python3
"""
A paper account: Trading 212 emulated in a local file, so the whole bot runs end to end with
no broker account, no credentials and no real money. Standard library only.

    python3 brokers/paper.py show                 cash, holdings and recent orders
    python3 brokers/paper.py reset                start again from starting_cash
    python3 brokers/paper.py buy MWIXl_EQ 2000    buy by value, as you would by hand in the app
    python3 brokers/paper.py sell MWIXl_EQ 10     sell a number of shares, or "all"
    python3 brokers/paper.py selftest

With broker.adapter "paper" in config.json every module talks to this instead of Trading 212,
through broker.request, and it answers in Trading 212's shapes (brokers/__init__.py), so the
runner, the gates and the sizing run unchanged.

Why "buy" exists: the bot sizes an order from a price, and Trading 212 has no quotes
endpoint, so it cannot price an instrument it does not hold yet (sizing.price_for). On the
real broker the core fund is bought once by hand in the app; "buy" is that step here.

What it emulates, and what it does not:

  fills        a market order fills at once at the latest daily close: a US share from
               pricefeed.fetch in dollars, converted with momentum.fx_rate; a configured
               symbol (broker.paper.symbols, such as the core fund) from Yahoo's chart for
               its yahoo symbol, times price_unit (0.01 for a line quoted in pence, which
               Trading 212 lists as GBX). No spread, no slippage, no partial fills and no
               queue: an order sent while the market is shut fills too, so the runner's
               market-hours gate is what keeps orders inside the session, as it is on the
               real broker. A close more than 10 days old is no price.
  fees         fx_fee, Trading 212's 0.15% currency conversion fee, on any instrument whose
               currency is not the account's, buying and selling. No other costs.
  refusals     as Trading 212 refuses, 400 with its error types: more than 4 decimal places;
               selling more than is held (selling-equity-not-owned); a sell that leaves a
               remainder worth under 1.00 of the account currency (min-opened-position-
               exceeded: the real broker does not say which unit its 1.00 is in, and a whole
               exit that keeps 2 of the account currency behind is accepted there); a buy
               that with its fee costs more than (1 - hold) of the free cash
               (insufficient-free-for-stocks-buy: the real broker keeps back part of the
               free funds on a market buy); a ticker not spelled exactly as listed, since
               case matters (MWIXl_EQ, never MWIXL_EQ). With no price it refuses rather than
               fill at a guess. The precision and unknown-ticker error types are this
               file's own spelling; the other three are Trading 212's.
  hours        exchanges publishes one venue, the LSE: weekdays 08:00 to 16:30 London time
               for the next 14 days, read through zoneinfo so summer time is right, with no
               holidays. A US share carries a schedule id that has no published schedule,
               so its hours come from config execution.market_hours_fallback, exactly as on
               Trading 212, whose exchanges list has no US venue either.
  instruments  US shares as SYMBOL_US_EQ, type STOCK, from the S&P 500 members (universe.py,
               or broker.paper.indexes, else strategy.momentum.indexes) and SPUS's holdings
               file, cached in the state file for 7 days and kept when a refresh fails; any
               held share stays listed so it can always be sold; plus every configured
               symbol on its venue's schedule. A list read while an index list is down is
               only part of the universe, so it serves that call alone, merged with any
               earlier copy, and is never saved; a share missing from it is refused as
               unavailable (503), not as unknown. Dividends, interest, splits and other
               corporate actions are not modelled.

Settings, under broker.paper in config.json: state_file (relative to the project folder),
starting_cash, fx_fee, hold, symbols {TICKER: {yahoo, price_unit, currency, venue, name}},
and optionally currency and indexes.

The account currency is broker.paper.currency when set, else the account.currency of the
config file the run read (broker.load_broker_config records it as config_file), else
config.json's in the project folder, else GBP. It is fixed when the state file is created:
the state's cash and costs are in it, so a later change is refused (409) until a reset.

State is JSON at the state file, written whole to a temporary file beside it and renamed
into place, so a crash mid-write leaves the previous state, never half of one. A state file
that cannot be read is reported and left alone, never silently replaced.
"""

import argparse
import json
import math
import os
import re
import sys
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone
from datetime import time as wall
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NEEDS_CREDENTIALS = False
LABEL = "paper (simulated, no real money)"
REQUEST_DELAY_SECONDS = 0

DEFAULTS = {"state_file": "paper_account.json", "starting_cash": 10000.0, "fx_fee": 0.0015,
            "hold": 0.05, "symbols": {}}
# Trading 212's paths, used when the broker section carries no endpoints of its own.
ENDPOINTS = {
    "account_summary": "/equity/account/summary",
    "positions": "/equity/positions",
    "pending_orders": "/equity/orders",
    "instruments": "/equity/metadata/instruments",
    "exchanges": "/equity/metadata/exchanges",
    "history_orders": "/equity/history/orders",
    "history_dividends": "/equity/history/dividends",
    "history_transactions": "/equity/history/transactions",
    "place_market_order": "/equity/orders/market",
}
DECIMALS = 4                  # Trading 212 takes at most 4 decimal places in an order
APP_DECIMALS = 8              # a purchase by value in the app leaves up to 8
PRICE_TTL = 15 * 60           # seconds a price or exchange rate is kept in memory
LISTING_DAYS = 7              # days the US share list is kept in the state file
STALE_DAYS = 10               # a close older than this is no price
MIN_REMAINDER = 1.00          # a sell may not leave a holding worth less than this
SCHEDULE_DAYS = 14            # days of LSE sessions the exchanges endpoint publishes
US_SCHEDULE = 900             # a schedule id the exchanges list does not carry
UNLISTED_SCHEDULE = 999       # a configured symbol on a venue this file does not know
VENUES = {"LSE": {"id": 1, "schedule": 101, "name": "London Stock Exchange",
                  "timezone": "Europe/London", "open": "08:00", "close": "16:30"}}

# Injected by the selftest: fetch(ticker) -> (series, source) for a US share, getter(url,
# headers) -> text for every other web read, fx(currency) -> dollars per unit, clock() ->
# an aware UTC datetime. None means the real thing.
HOOKS = {"fetch": None, "getter": None, "fx": None, "clock": None}
_CACHE = {}


class PaperError(Exception):
    def __init__(self, status, detail, kind="paper-error"):
        Exception.__init__(self, detail)
        self.status = status
        self.body = refusal(kind, detail, status, "The paper account cannot be used")


def refusal(kind, detail, status=400, title="Error while placing the order"):
    """An error body in Trading 212's shape."""
    return {"type": "/api-errors/%s" % kind, "title": title, "status": status,
            "detail": detail}


def clear_cache():
    _CACHE.clear()


def whole_config(broker_config):
    """The config file this broker section came from, or {} if it cannot be read."""
    path = (broker_config or {}).get("config_file") or str(ROOT / "config.json")
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def fmt(quantity):
    """A quantity as the broker prints one: up to 8 places, no trailing zeros."""
    return ("%.8f" % quantity).rstrip("0").rstrip(".")


class Paper:
    """One paper account, read from and written back to its state file on every call."""

    def __init__(self, broker_config, fetch=None, getter=None, fx=None, clock=None):
        self.broker = broker_config or {}
        self.settings = dict(DEFAULTS, **(self.broker.get("paper") or {}))
        self.symbols = self.settings.get("symbols") or {}
        self.endpoints = dict(ENDPOINTS, **(self.broker.get("endpoints") or {}))
        self.fetch = fetch or HOOKS["fetch"]
        self.getter = getter or HOOKS["getter"]
        self.fx = fx or HOOKS["fx"]
        self.clock = clock or HOOKS["clock"]
        path = Path(str(self.settings.get("state_file") or DEFAULTS["state_file"]))
        self.path = path if path.is_absolute() else ROOT / path
        self._config, self._listing = None, None
        explicit = self.settings.get("currency")
        self.currency = str(explicit or (self.config().get("account") or {}).get("currency")
                            or "GBP").strip().upper()
        self.notes = []

    # ------------------------------------------------------------------ plumbing

    def config(self):
        if self._config is None:
            self._config = whole_config(self.broker)
        return self._config

    def now(self):
        return (self.clock or (lambda: datetime.now(timezone.utc)))()

    def stamp(self):
        return self.now().astimezone(timezone.utc).isoformat(timespec="milliseconds")

    def note(self, text):
        self.notes.append(text)
        print("paper: %s" % text, file=sys.stderr)

    def cached(self, key, make):
        """make()'s answer, kept in memory for PRICE_TTL seconds; failures are kept too, so
        a source that is down is not asked again on every read of the same run."""
        moment = self.now().timestamp()
        hit = _CACHE.get(key)
        if hit and hit[0] > moment:
            return hit[1]
        value = make()
        _CACHE[key] = (moment + PRICE_TTL, value)
        return value

    # ------------------------------------------------------------------ the state file

    def fresh(self):
        return {"version": 1, "currency": self.currency, "created": self.stamp(),
                "cash": round(float(self.settings.get("starting_cash", 0.0)), 2),
                "realized": 0.0, "next_id": 1, "positions": {}, "orders": [],
                "instruments": {}}

    def load(self):
        """The state, created with the starting cash on first use. Unreadable, or in another
        currency than the account's, is an error and the file is left as it is."""
        if not self.path.exists():
            state = self.fresh()
            self.save(state)
            return state
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(state, dict) or not isinstance(state.get("positions"), dict):
                raise ValueError("not a paper account")
        except (OSError, ValueError) as error:
            raise PaperError(500, "paper state file %s cannot be read (%s); fix or delete it, "
                             "or run brokers/paper.py reset" % (self.path, error))
        if str(state.get("currency", "")).upper() != self.currency:
            raise PaperError(409, "the paper account at %s is in %s but the account currency "
                             "is now %s; run brokers/paper.py reset to start again in %s"
                             % (self.path, state.get("currency"), self.currency,
                                self.currency), "paper-currency-changed")
        state.setdefault("orders", [])
        state.setdefault("instruments", {})
        state.setdefault("realized", 0.0)
        return state

    def save(self, state):
        """Atomic: the whole state to a temporary file in the same folder, then renamed."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(prefix=".%s." % self.path.name, suffix=".tmp",
                                        dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(state, out, indent=1, sort_keys=True)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, str(self.path))
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ instruments and prices

    def instrument(self, ticker, state):
        """What is known about a ticker spelled exactly so, or None if it is not listed."""
        spec = self.symbols.get(ticker)
        if isinstance(spec, dict):
            currency = str(spec.get("currency") or self.currency).upper()
            unit = float(spec.get("price_unit") or 1.0)
            pence = currency == "GBP" and abs(unit - 0.01) < 1e-12
            venue = str(spec.get("venue") or "").upper()
            return {"kind": "symbol", "currency": currency, "code": "GBX" if pence else currency,
                    "unit": unit, "yahoo": spec.get("yahoo"), "name": spec.get("name", ticker),
                    "isin": spec.get("isin", ""), "type": spec.get("type", "ETF"),
                    "schedule": VENUES[venue]["schedule"] if venue in VENUES
                    else UNLISTED_SCHEDULE}
        held = (state.get("positions") or {}).get(ticker)
        if held and isinstance(held.get("instrument"), dict):
            return held["instrument"]
        if not ticker.endswith("_US_EQ"):
            return None
        names = dict((s, n) for s, n in self.listing(state)[0])
        symbol = ticker[: -len("_US_EQ")]
        if symbol not in names or "%s_US_EQ" % symbol != ticker:
            return None
        return self.us(symbol, names[symbol])

    @staticmethod
    def us(symbol, name):
        return {"kind": "us", "currency": "USD", "code": "USD", "unit": 1.0, "yahoo": None,
                "name": name, "isin": "", "type": "STOCK", "schedule": US_SCHEDULE}

    def listing(self, state):
        """([[symbol, name]], note, whole): the US shares, from the state file's copy while it
        is under LISTING_DAYS old, else read again; a failed read keeps the old copy. whole
        is False when the list is known to be short (an index list could not be read), so a
        share missing from it may still exist."""
        if self._listing is None:
            self._listing = self.read_cached_listing(state)
        return self._listing

    def read_cached_listing(self, state):
        cache = state.get("instruments") or {}
        rows = cache.get("rows") or []
        try:
            age = self.now() - datetime.fromisoformat(str(cache.get("fetched")))
        except (TypeError, ValueError):
            age = None
        if rows and age is not None and age < timedelta(days=LISTING_DAYS):
            return rows, "", True
        fetched, problems, whole = self.read_listing()
        if fetched and whole:
            state["instruments"] = {"fetched": self.stamp(), "rows": fetched}
            self.save(state)
            return fetched, "", True
        if fetched:
            # An index list failed, so this is only SPUS's own names, a part of the universe.
            # Saved and stamped it would stand for LISTING_DAYS as if whole, refusing every
            # other member; so it serves this call alone, on top of any earlier copy, and the
            # next call reads the list again.
            names = dict((s, n) for s, n in rows)
            names.update((s, n) for s, n in fetched)
            merged = sorted([s, n] for s, n in names.items())
            return merged, ("the US share list is incomplete (%s): this call uses the %d "
                            "shares read now%s, saves nothing and reads the list again next "
                            "time" % ("; ".join(problems), len(fetched),
                                      " plus the copy from %s" % cache.get("fetched")
                                      if rows else "")), False
        if rows:
            return rows, ("could not refresh the US share list (%s), so the copy from %s is "
                          "used" % ("; ".join(problems), cache.get("fetched"))), True
        return [], "could not build the US share list: %s" % "; ".join(problems), False

    def read_listing(self):
        """([[symbol, name]], problems, whole). whole is False when an index list could not
        be read: SPUS's holdings file alone covers only its own names. A failed SPUS read is
        no matter, as the index list alone is enough."""
        import momentum
        import pricefeed
        import universe
        getter = self.getter or pricefeed.http_get
        strategy = (self.config().get("strategy") or {}).get("momentum") or {}
        indexes = self.settings.get("indexes") or strategy.get("indexes") or ["sp500"]
        names, problems = {}, []
        rows, notes = universe.load(indexes, getter)
        for row in rows:
            names.setdefault(row["symbol"], row["name"])
        index_failed = [n for n in notes if "fail" in n or "unknown" in n]
        if not rows and not index_failed:
            index_failed.append("the index list had no members")
        problems += index_failed
        try:
            text = getter(strategy.get("universe_url") or momentum.SPUS_HOLDINGS,
                          {"Accept": "text/csv,*/*"})
            for symbol, name, _ in momentum.parse_holdings(text):
                names.setdefault(symbol, name)
        except Exception as error:  # best effort: the index list alone is enough
            problems.append("SPUS holdings: %s" % error)
        good = sorted([s, n] for s, n in names.items() if re.fullmatch(r"[A-Z][A-Z0-9-]*", s))
        return good, problems or ["no rows"], not index_failed

    def dollars_per(self, currency):
        if currency == "USD":
            return 1.0

        def ask():
            if self.fx:
                return self.fx(currency)
            import momentum
            if hasattr(momentum, "fx_rate"):
                return momentum.fx_rate(currency, self.getter)
            return momentum.gbpusd(self.getter) if currency == "GBP" else None
        rate = self.cached(("fx", currency), ask)
        return rate if isinstance(rate, (int, float)) and rate > 0 else None

    def per_unit(self, currency):
        """Account currency per one unit of `currency`, or None."""
        if currency == self.currency:
            return 1.0
        mine, theirs = self.dollars_per(self.currency), self.dollars_per(currency)
        return theirs / mine if mine and theirs else None

    def closes(self, ticker, info):
        import pricefeed
        if info["kind"] == "us":
            if self.fetch:
                return self.fetch(ticker)
            if self.getter:
                return pricefeed.fetch(ticker, getter=self.getter)
            return pricefeed.fetch(ticker)
        symbol = info.get("yahoo")
        if not symbol:
            return [], "no yahoo symbol configured for %s" % ticker
        getter = self.getter or pricefeed.http_get
        problems = []
        for host in ("query2", "query1"):
            url = ("https://%s.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d"
                   % (host, urllib.parse.quote(str(symbol))))
            try:
                series = pricefeed.parse_yahoo(json.loads(getter(url, None)))
            except Exception as error:
                problems.append("yahoo %s: %s" % (host, error))
                continue
            if series:
                return series, "yahoo"
            problems.append("yahoo %s returned no closes" % host)
        return [], "; ".join(problems)

    def quote(self, ticker, info):
        """({quote, price, source}, "") or (None, reason). quote is the latest close in the
        instrument's own units (pence for GBX), price one share in the account currency."""
        def ask():
            try:
                series, source = self.closes(ticker, info)
            except Exception as error:  # a price source must never crash a run
                series, source = [], "lookup failed: %s" % error
            if not series:
                return None, "no price for %s: %s" % (ticker, source)
            when, close = series[-1]
            age = (self.now().date() - when).days
            if age > STALE_DAYS:
                return None, "the newest close for %s is from %s" % (ticker, when.isoformat())
            try:
                rate = self.per_unit(info["currency"])
            except Exception:  # as above: no rate is no price, never a crash
                rate = None
            if rate is None:
                return None, "no %s to %s exchange rate" % (info["currency"], self.currency)
            return {"quote": float(close), "price": float(close) * info["unit"] * rate,
                    "source": "%s close %s" % (source, when.isoformat())}, ""
        return self.cached(("quote", ticker, self.currency, info["unit"]), ask)

    def valued(self, state):
        """[(ticker, position, value)], every holding at its latest price, or at the last
        known one when no price can be had now. The state is saved if a price moved."""
        out, moved = [], False
        for ticker in sorted(state["positions"]):
            position = state["positions"][ticker]
            info = self.instrument(ticker, state) or position.get("instrument")
            found, _ = self.quote(ticker, info) if info else (None, "")
            if found and (found["quote"] != position.get("last_quote")
                          or found["price"] != position.get("last_price")):
                position.update(last_quote=found["quote"], last_price=found["price"],
                                priced_at=self.stamp())
                moved = True
            value = round(position["quantity"] * float(position.get("last_price") or 0.0), 2)
            out.append((ticker, position, value))
        if moved:
            self.save(state)
        return out

    # ------------------------------------------------------------------ endpoints

    def route(self, path):
        parts = urllib.parse.urlsplit(str(path))
        bare = parts.path
        if bare.startswith("/api/v0/"):
            bare = bare[len("/api/v0"):]
        query = dict(urllib.parse.parse_qsl(parts.query))
        for name, value in self.endpoints.items():
            if value == bare:
                return name, query
        return None, query

    def handle(self, path, method="GET", payload=None):
        name, query = self.route(path)
        method = str(method or "GET").upper()
        if name == "place_market_order":
            if method != "POST":
                return 405, refusal("method-not-allowed", "POST a market order", 405)
            return self.place(payload)
        if name is None:
            return 404, refusal("not-found", "the paper account has no endpoint %s" % path, 404)
        if method != "GET":
            return 405, refusal("method-not-allowed", "%s takes GET" % path, 405)
        if name == "exchanges":
            return 200, self.exchanges()
        state = self.load()
        if name == "account_summary":
            return 200, self.summary(state)
        if name == "positions":
            return 200, self.positions(state)
        if name == "history_orders":
            return self.history(state, query)
        if name == "pending_orders":
            return 200, []
        if name in ("history_dividends", "history_transactions"):
            return 200, {"items": [], "nextPagePath": None}
        if name == "instruments":
            return 200, self.instruments(state)
        return 404, refusal("not-found", "the paper account does not emulate %s" % name, 404)

    def summary(self, state):
        holdings = self.valued(state)
        invested = round(sum((value for _, _, value in holdings), 0.0), 2)
        cost = round(sum((float(p.get("cost") or 0.0) for _, p, _ in holdings), 0.0), 2)
        cash = round(float(state["cash"]), 2)
        return {"id": 0, "currency": self.currency, "totalValue": round(cash + invested, 2),
                "cash": {"availableToTrade": cash, "reservedForOrders": 0.0, "inPies": 0.0},
                "investments": {"currentValue": invested, "totalCost": cost,
                                "realizedProfitLoss": round(float(state["realized"]), 2),
                                "unrealizedProfitLoss": round(invested - cost, 2)}}

    def positions(self, state):
        rows = []
        for ticker, position, value in self.valued(state):
            info = position.get("instrument") or {}
            quantity = position["quantity"]
            rows.append({
                "instrument": {"ticker": ticker, "name": info.get("name", ticker),
                               "isin": info.get("isin", ""),
                               "currency": info.get("code") or info.get("currency")},
                "createdAt": position.get("opened"),
                "quantity": quantity,
                "quantityAvailableForTrading": quantity,
                "quantityInPies": 0,
                "currentPrice": position.get("last_quote"),
                "averagePricePaid": (round(position.get("paid", 0.0) / quantity, 6)
                                     if quantity else None),
                "walletImpact": {"currency": self.currency,
                                 "totalCost": round(float(position.get("cost") or 0.0), 2),
                                 "currentValue": value,
                                 "unrealizedProfitLoss": round(value - float(
                                     position.get("cost") or 0.0), 2),
                                 "fxImpact": None}})
        return rows

    def history(self, state, query):
        try:
            limit = int(query.get("limit", 20))
            cursor = int(query["cursor"]) if query.get("cursor") else None
        except ValueError:
            return 400, refusal("bad-request", "limit and cursor must be whole numbers")
        if not 1 <= limit <= 50:
            return 400, refusal("bad-request", "limit must be between 1 and 50")
        items = sorted(state["orders"], key=lambda row: row["order"]["id"], reverse=True)
        if query.get("ticker"):
            items = [row for row in items if row["order"]["ticker"] == query["ticker"]]
        if cursor is not None:
            items = [row for row in items if row["order"]["id"] < cursor]
        page, following = items[:limit], None
        if len(items) > limit:
            ask = {"limit": limit, "cursor": page[-1]["order"]["id"]}
            if query.get("ticker"):
                ask["ticker"] = query["ticker"]
            following = "/api/v0%s?%s" % (self.endpoints["history_orders"],
                                          urllib.parse.urlencode(ask))
        return 200, {"items": page, "nextPagePath": following}

    def exchanges(self):
        """The LSE's sessions for the next SCHEDULE_DAYS days, weekdays only, in UTC."""
        out = []
        for spec in VENUES.values():
            events = []
            try:
                from zoneinfo import ZoneInfo
                zone = ZoneInfo(spec["timezone"])
                opens = wall(*(int(x) for x in spec["open"].split(":")))
                closes = wall(*(int(x) for x in spec["close"].split(":")))
            except Exception as error:  # no tzdata on this machine: publish nothing, held
                self.note("no %s session times (%s); install the tzdata package"
                          % (spec["name"], error))
                zone = None
            first = self.now().astimezone(zone).date() if zone else None
            for offset in range(SCHEDULE_DAYS if zone else 0):
                day = first + timedelta(days=offset)
                if day.weekday() >= 5:
                    continue
                for moment, kind in ((opens, "OPEN"), (closes, "CLOSE")):
                    local = datetime.combine(day, moment).replace(tzinfo=zone)
                    events.append({"date": local.astimezone(timezone.utc).isoformat(),
                                   "type": kind})
            out.append({"id": spec["id"], "name": spec["name"],
                        "workingSchedules": [{"id": spec["schedule"], "timeEvents": events}]})
        return out

    def instruments(self, state):
        rows, note, _ = self.listing(state)
        if note:
            self.note(note)
        names = dict((s, n) for s, n in rows)
        for ticker, position in state["positions"].items():
            info = position.get("instrument") or {}
            if ticker.endswith("_US_EQ") and info.get("kind") == "us":
                names.setdefault(ticker[: -len("_US_EQ")], info.get("name", ticker))
        out = []
        for symbol in sorted(names):
            ticker = "%s_US_EQ" % symbol
            if ticker in self.symbols:
                continue
            out.append({"ticker": ticker, "type": "STOCK", "currencyCode": "USD",
                        "shortName": symbol, "name": names[symbol], "isin": "",
                        "workingScheduleId": US_SCHEDULE, "maxOpenQuantity": 1000000})
        for ticker in sorted(self.symbols):
            info = self.instrument(ticker, state)
            if info:
                out.append({"ticker": ticker, "type": info["type"], "currencyCode": info["code"],
                            "shortName": str(info.get("yahoo") or ticker).split(".")[0],
                            "name": info["name"], "isin": info["isin"],
                            "workingScheduleId": info["schedule"], "maxOpenQuantity": 1000000})
        return out

    # ------------------------------------------------------------------ orders

    def place(self, payload):
        """POST {ticker, quantity}: refused as Trading 212 refuses, else filled at once."""
        if not isinstance(payload, dict) or "ticker" not in payload or "quantity" not in payload:
            return 400, refusal("bad-request", "the body must be {ticker, quantity}")
        ticker, quantity = payload["ticker"], payload["quantity"]
        if (not isinstance(ticker, str) or isinstance(quantity, bool)
                or not isinstance(quantity, (int, float)) or not math.isfinite(quantity)
                or quantity == 0):
            return 400, refusal("bad-request", "ticker must be text and quantity a non-zero "
                                "number, positive to buy and negative to sell")
        if abs(quantity * 10 ** DECIMALS - round(quantity * 10 ** DECIMALS)) > 1e-6:
            return 400, refusal("invalid-quantity-precision",
                                "invalid quantity precision %d" % DECIMALS)
        state = self.load()
        return self.execute(state, ticker, float(quantity), "API", payload)

    def execute(self, state, ticker, quantity, origin, payload=None, value=None):
        info = self.instrument(ticker, state)
        if info is None:
            # Unknown is only a fact against a whole list: missing from a short one, the
            # share may exist, so it is unavailable for now rather than not found.
            whole = self.listing(state)[2] if ticker.endswith("_US_EQ") else True
            if not whole:
                return 503, refusal("instruments-unavailable", "%s cannot be checked: %s"
                                    % (ticker, self.listing(state)[1]), 503)
            return 400, refusal("instrument-not-found",
                                "no instrument %s; tickers are case-sensitive, list them with "
                                "python3 broker.py instruments" % ticker)
        held = float((state["positions"].get(ticker) or {}).get("quantity") or 0.0)
        if quantity < 0 and -quantity > held + 1e-9:
            return 400, refusal("selling-equity-not-owned",
                                "Selling more equities than owned, owned: %s" % fmt(held))
        found, why = self.quote(ticker, info)
        if not found:
            return 400, refusal("paper-no-price", "%s; refused rather than filled at a guess"
                                % why)
        price = found["price"]
        gross = round(abs(quantity) * price, 2)
        fee = (round(gross * float(self.settings.get("fx_fee", 0.0)), 2)
               if info["currency"] != self.currency else 0.0)
        cash = float(state["cash"])
        if quantity > 0:
            usable = (1.0 - float(self.settings.get("hold", 0.0))) * cash
            if gross + fee > usable + 1e-9:
                return 400, refusal("insufficient-free-for-stocks-buy", "Insufficient funds")
        else:
            left = held + quantity
            if left > 1e-9 and left * price < MIN_REMAINDER:
                return 400, refusal("min-opened-position-exceeded",
                                    "must have opened position at least %.2f" % MIN_REMAINDER)
        self.fill(state, ticker, quantity, info, found, gross, fee, origin, value)
        order = state["orders"][-1]["order"]
        reply = {key: order[key] for key in ("id", "strategy", "type", "ticker", "quantity",
                                             "side", "currency", "createdAt", "initiatedFrom")}
        reply.update(filledQuantity=0, status="NEW",
                     extendedHours=bool((payload or {}).get("extendedHours", False)))
        return 200, reply

    def fill(self, state, ticker, quantity, info, found, gross, fee, origin, value=None):
        stamp = self.stamp()
        identifier = int(state.get("next_id") or 1)
        state["next_id"] = identifier + 1
        position = state["positions"].get(ticker) or {
            "quantity": 0.0, "cost": 0.0, "paid": 0.0, "opened": stamp, "instrument": info}
        held = position["quantity"]
        if quantity > 0:
            state["cash"] = round(float(state["cash"]) - gross - fee, 2)
            position["cost"] = round(position["cost"] + gross, 2)
            position["paid"] = position["paid"] + quantity * found["quote"]
        else:
            share = -quantity / held
            cost = round(position["cost"] * share, 2)
            state["cash"] = round(float(state["cash"]) + gross - fee, 2)
            state["realized"] = round(float(state["realized"]) + gross - fee - cost, 2)
            position["cost"] = round(position["cost"] - cost, 2)
            position["paid"] = position["paid"] * (1.0 - share)
        position["quantity"] = round(held + quantity, APP_DECIMALS)
        position.update(last_quote=found["quote"], last_price=found["price"], priced_at=stamp)
        if position["quantity"] > 1e-9:
            state["positions"][ticker] = position
        else:
            state["positions"].pop(ticker, None)
        side = "BUY" if quantity > 0 else "SELL"
        order = {"id": identifier, "strategy": "VALUE" if value else "QUANTITY",
                 "type": "MARKET", "ticker": ticker, "quantity": quantity,
                 "filledQuantity": quantity, "status": "FILLED", "side": side,
                 "currency": info["code"], "createdAt": stamp, "initiatedFrom": origin,
                 "instrument": {"ticker": ticker, "name": info["name"],
                                "currency": info["code"]}}
        if value:
            order["value"] = value
        rate = found["price"] / (found["quote"] * info["unit"]) if found["quote"] else None
        fill = {"id": identifier, "quantity": quantity, "price": found["quote"],
                "type": "TRADE", "filledAt": stamp,
                "walletImpact": {"currency": self.currency,
                                 "netValue": round(-(gross + fee) if quantity > 0
                                                   else gross - fee, 2),
                                 "fxRate": rate,
                                 "taxes": ([{"name": "CURRENCY_CONVERSION_FEE",
                                             "quantity": fee, "currency": self.currency}]
                                           if fee else [])}}
        state["orders"].append({"order": order, "fill": fill})
        self.save(state)

    def buy_value(self, ticker, value):
        """Buy `value` of the account currency's worth, as the app does by hand: up to 8
        decimal places, which is why the bot later sells such a holding with care."""
        state = self.load()
        info = self.instrument(ticker, state)
        found, why = self.quote(ticker, info) if info else (None, "")
        if not found:
            return self.execute(state, ticker, 1.0, "PAPER_CLI")
        factor = 10 ** APP_DECIMALS
        quantity = math.floor(float(value) / found["price"] * factor) / factor
        if quantity <= 0:
            return 400, refusal("bad-request", "%.2f buys no %s" % (float(value), ticker))
        return self.execute(state, ticker, quantity, "PAPER_CLI", value=round(float(value), 2))

    def sell_quantity(self, ticker, quantity):
        """Sell shares by hand, at any precision; "all" empties the holding, as the app's
        sell-all does, leaving no remainder behind."""
        state = self.load()
        held = float((state["positions"].get(ticker) or {}).get("quantity") or 0.0)
        try:
            wanted = held if str(quantity).lower() == "all" else float(quantity)
        except ValueError:
            return 400, refusal("bad-request", 'sell takes a number of shares, or "all"')
        if not wanted > 0:
            return 400, refusal("bad-request", "nothing to sell")
        return self.execute(state, ticker, -wanted, "PAPER_CLI")


def request(broker_config, path, method="GET", payload=None, timeout=30):
    """The adapter entry point broker.request calls: (status, body) in Trading 212's shapes."""
    try:
        return Paper(broker_config).handle(path, method, payload)
    except PaperError as error:
        return error.status, error.body
    except OSError as error:  # the state file could not be written: nothing changed
        return 500, refusal("paper-state-unwritable", "paper state not saved: %s" % error, 500)


# ---------------------------------------------------------------------- the command line

def cmd_show(paper, args):
    status, summary = paper.handle(paper.endpoints["account_summary"])
    if status != 200:
        print(summary.get("detail") if isinstance(summary, dict) else summary)
        return 1
    _, rows = paper.handle(paper.endpoints["positions"])
    _, history = paper.handle("%s?limit=10" % paper.endpoints["history_orders"])
    print("%s, %s, state in %s" % (LABEL, paper.currency, paper.path))
    print("cash        %12.2f" % summary["cash"]["availableToTrade"])
    print("invested    %12.2f" % summary["investments"]["currentValue"])
    print("total       %12.2f\n" % summary["totalValue"])
    print("%-14s %16s %14s %12s" % ("TICKER", "QUANTITY", "PRICE", "VALUE"))
    for row in rows:
        print("%-14s %16s %14.4f %12.2f"
              % (row["instrument"]["ticker"], fmt(row["quantity"]),
                 row["currentPrice"] or 0.0, row["walletImpact"]["currentValue"]))
    if not rows:
        print("(nothing held)")
    print("\nlatest orders")
    for item in history.get("items", []):
        order, fill = item["order"], item.get("fill") or {}
        print("  %s  %-4s %-14s %14s at %s  %s"
              % (order["createdAt"][:16], order["side"], order["ticker"],
                 fmt(abs(order["quantity"])), fill.get("price"), order["initiatedFrom"]))
    return 0


def cmd_reset(paper, args):
    kept = {}
    if paper.path.exists():
        try:
            kept = json.loads(paper.path.read_text(encoding="utf-8")).get("instruments") or {}
        except (OSError, ValueError, AttributeError):
            kept = {}
    state = paper.fresh()
    state["instruments"] = kept
    paper.save(state)
    print("paper account reset: %.2f %s in cash, nothing held, no orders (%s)"
          % (state["cash"], paper.currency, paper.path))
    import sizing
    ledger = sizing.ledger_path(dict(paper.broker, adapter="paper"))
    print("The bot's order ledger for this paper account, %s, still records today's orders, "
          "so a run today will not resend them and still counts them against the daily "
          "spending cap. It holds paper orders only (Trading 212 keeps a ledger of its own), "
          "so to start the paper ledger afresh as well, delete %s." % (ledger.name, ledger))
    return 0


def cmd_trade(paper, args):
    if args.command == "buy":
        status, body = paper.buy_value(args.ticker, args.amount)
    else:
        status, body = paper.sell_quantity(args.ticker, args.amount)
    if status != 200:
        print("refused (%s): %s" % (status, body.get("detail") if isinstance(body, dict)
                                    else body))
        return 1
    print("filled %s %s %s" % (body["side"].lower(), fmt(abs(body["quantity"])), body["ticker"]))
    return cmd_show(paper, args)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("show", help="cash, holdings and recent orders")
    sub.add_parser("reset", help="start again from starting_cash")
    buy = sub.add_parser("buy", help="buy by value, as you would by hand in the app")
    buy.add_argument("ticker", help="exactly as listed, e.g. MWIXl_EQ")
    buy.add_argument("amount", type=float, help="how much of the account currency to spend")
    sell = sub.add_parser("sell", help="sell a number of shares, or all")
    sell.add_argument("ticker")
    sell.add_argument("amount", help='shares, or "all"')
    sub.add_parser("selftest", help="offline checks")
    args = parser.parse_args(argv)
    if args.command == "selftest":
        return cmd_selftest()
    if not args.command:
        parser.print_help()
        return 2

    import broker
    broker_config = broker.load_broker_config(args.config)
    if broker.adapter_name(broker_config) != "paper":
        print("note: broker.adapter in %s is %r, so the bot is not using this paper account"
              % (args.config, broker.adapter_name(broker_config)), file=sys.stderr)
    paper = Paper(broker_config)
    try:
        if args.command == "show":
            return cmd_show(paper, args)
        if args.command == "reset":
            return cmd_reset(paper, args)
        return cmd_trade(paper, args)
    except PaperError as error:
        print(error.body["detail"])
        return 1


# ---------------------------------------------------------------------- selftest

def cmd_selftest():
    """Offline: prices, exchange rates, web pages and the clock are injected, and every
    state file is in a temporary folder. HOOKS and the cache are put back afterwards."""
    import contextlib
    import io
    import shutil

    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    folder = Path(tempfile.mkdtemp(prefix="paper-selftest-"))
    saved_hooks = dict(HOOKS)
    clock = [datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)]   # a Wednesday, in BST
    dollars = {"AAA_US_EQ": 100.0, "BBB_US_EQ": 50.0, "DDD_US_EQ": 88.0}
    pence = {"MWIX.L": 500.0}
    calls = {"fetch": 0, "wiki": 0, "spus": 0, "chart": 0}
    down = set()

    def fetch(ticker):
        calls["fetch"] += 1
        today = clock[0].date()
        if ticker == "OLD_US_EQ":
            return [(today - timedelta(days=30), 20.0)], "yahoo"
        if ticker == "BOOM_US_EQ":
            raise RuntimeError("source broke")
        if ticker in down or ticker not in dollars:
            return [], "yahoo returned no closes"
        return [(today - timedelta(days=1), dollars[ticker])], "yahoo"

    wiki = ("<table class='wikitable'><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th>"
            "<th>GICS Sub-Industry</th><th>Date added</th></tr>"
            + "".join("<tr><td>%s</td><td>%s</td><td>Information Technology</td>"
                      "<td>Semiconductors</td><td>2001-01-01</td></tr>" % (s, n)
                      for s, n in (("AAA", "Alpha Corp"), ("BBB", "Beta Inc"),
                                   ("BRK.B", "Berkshire Class B"), ("DDD", "Delta Ltd"),
                                   ("NOPE", "No Price Inc"), ("OLD", "Old Close Co"),
                                   ("BOOM", "Broken Source Co")))
            + "</table>")
    spus = "StockTicker,SecurityName,Price\nCCC,Gamma Inc,30.00\nAAA,Alpha Corp,100.00\n"

    def getter(url, headers=None):
        if "wikipedia" in url:
            calls["wiki"] += 1
            if "wiki" in down:
                raise OSError("offline")
            return wiki
        if "Holdings_SPUS" in url:
            calls["spus"] += 1
            if "spus" in down:
                raise OSError("offline")
            return spus
        if "/chart/" in url:
            calls["chart"] += 1
            symbol = urllib.parse.unquote(url.split("/chart/")[1].split("?")[0])
            if symbol not in pence or "chart" in down:
                raise OSError("offline")
            stamp = int(datetime.combine(clock[0].date() - timedelta(days=1), wall(16, 0),
                                         tzinfo=timezone.utc).timestamp())
            return json.dumps({"chart": {"result": [{"timestamp": [stamp], "indicators": {
                "quote": [{"close": [pence[symbol]]}]}}]}})
        raise OSError("unexpected url %s" % url)

    rates = {"GBP": 1.25, "EUR": 1.10, "USD": 1.0}
    HOOKS.update(fetch=fetch, getter=getter, fx=lambda c: rates.get(c), clock=lambda: clock[0])
    clear_cache()

    def settings(name, currency="GBP", **extra):
        paper = {"state_file": str(folder / name), "starting_cash": 10000.0,
                 "fx_fee": 0.0015, "hold": 0.05, "currency": currency, "indexes": ["sp500"],
                 "symbols": {"MWIXl_EQ": {"yahoo": "MWIX.L", "price_unit": 0.01,
                                          "currency": "GBP", "venue": "LSE",
                                          "name": "Core fund (test)"}}}
        paper.update(extra)
        return {"adapter": "paper", "environment": "demo", "endpoints": dict(ENDPOINTS),
                "paper": paper}

    def call(config, name, method="GET", payload=None, query=""):
        return request(config, ENDPOINTS[name] + query, method=method, payload=payload)

    def buy(config, ticker, quantity):
        return call(config, "place_market_order", "POST", {"ticker": ticker,
                                                           "quantity": quantity})

    def kind(body):
        return str((body or {}).get("type", "")) if isinstance(body, dict) else ""

    quiet = io.StringIO()
    try:
        with contextlib.redirect_stderr(quiet):
            # ---------------------------------------------------------- shapes and fills
            gbp = settings("gbp.json")
            expect("no state file before first use", not (folder / "gbp.json").exists())
            status, summary = call(gbp, "account_summary")
            expect("summary: 200 with the starting cash in the account currency",
                   status == 200 and summary["cash"]["availableToTrade"] == 10000.0
                   and summary["currency"] == "GBP"
                   and summary["investments"]["currentValue"] == 0.0)
            expect("the state file is created on first use, as utf-8 JSON",
                   json.loads((folder / "gbp.json").read_text(encoding="utf-8"))["cash"]
                   == 10000.0)
            status, rows = call(gbp, "positions")
            expect("positions: an empty list to start", status == 200 and rows == [])
            status, body = call(gbp, "history_orders")
            expect("history: items and nextPagePath",
                   status == 200 and body == {"items": [], "nextPagePath": None})
            expect("pending orders: an empty list", call(gbp, "pending_orders") == (200, []))
            expect("dividends and transactions: empty pages",
                   call(gbp, "history_dividends")[1] == {"items": [], "nextPagePath": None}
                   and call(gbp, "history_transactions")[1]["items"] == [])

            status, body = buy(gbp, "AAA_US_EQ", 2.5)
            expect("a buy answers 200 as Trading 212 does: NEW, nothing filled yet",
                   status == 200 and body["status"] == "NEW" and body["filledQuantity"] == 0
                   and body["strategy"] == "QUANTITY" and body["type"] == "MARKET"
                   and body["ticker"] == "AAA_US_EQ" and body["quantity"] == 2.5)
            status, summary = call(gbp, "account_summary")
            # 2.5 x 100 dollars at 1.25 dollars a pound is 200.00, plus the 0.15% FX fee.
            expect("the buy filled at the close in pounds, with the FX fee",
                   summary["cash"]["availableToTrade"] == 9799.70
                   and summary["investments"]["currentValue"] == 200.0)
            status, rows = call(gbp, "positions")
            row = rows[0] if rows else {}
            expect("positions: the ticker nested under instrument, with its currency",
                   row.get("instrument", {}).get("ticker") == "AAA_US_EQ"
                   and row["instrument"]["currency"] == "USD")
            expect("positions: currentPrice in dollars, walletImpact in pounds",
                   row.get("currentPrice") == 100.0 and row.get("averagePricePaid") == 100.0
                   and row["walletImpact"]["currentValue"] == 200.0
                   and row["walletImpact"]["currency"] == "GBP" and row["quantity"] == 2.5)
            import sizing
            expect("so sizing.account_price reads 80.00 pounds a share",
                   abs(sizing.account_price(row["quantity"], row["currentPrice"],
                                            row["walletImpact"]["currentValue"]) - 80.0) < 1e-9)
            status, body = call(gbp, "history_orders")
            item = body["items"][0] if body["items"] else {}
            expect("history: the order FILLED under order, with createdAt and a fill",
                   item.get("order", {}).get("status") == "FILLED"
                   and item["order"]["ticker"] == "AAA_US_EQ"
                   and item["order"]["createdAt"].startswith("2026-10-14T10:00")
                   and item["fill"]["walletImpact"]["taxes"][0]["quantity"] == 0.30)

            status, body = buy(gbp, "MWIXl_EQ", 100)
            status, summary = call(gbp, "account_summary")
            expect("a pence line: 500p times price_unit is 5.00 a share, no FX fee",
                   status == 200 and summary["cash"]["availableToTrade"] == 9299.70)
            fund = [r for r in call(gbp, "positions")[1]
                    if r["instrument"]["ticker"] == "MWIXl_EQ"]
            expect("and its currentPrice stays in pence, as GBX, its value in pounds",
                   fund and fund[0]["currentPrice"] == 500.0
                   and fund[0]["instrument"]["currency"] == "GBX"
                   and fund[0]["walletImpact"]["currentValue"] == 500.0
                   and sizing.account_price(100, 500.0, 500.0) == 5.0)

            # ---------------------------------------------------------- refusals
            before = json.loads((folder / "gbp.json").read_text(encoding="utf-8"))
            status, body = buy(gbp, "MWIXL_EQ", 1)
            expect("a ticker in the wrong case is refused, as on Trading 212",
                   status == 400 and kind(body).endswith("instrument-not-found"))
            status, body = buy(gbp, "aaa_US_EQ", 1)
            expect("lower-case US tickers are refused too", status == 400)
            status, body = buy(gbp, "ZZZ_US_EQ", 1)
            expect("a ticker not listed at all is refused", status == 400)
            status, body = buy(gbp, "BBB_US_EQ", 1.23456)
            expect("more than 4 decimal places: invalid quantity precision",
                   status == 400 and "precision 4" in body["detail"])
            status, body = buy(gbp, "AAA_US_EQ", -3)
            expect("selling more than held: selling-equity-not-owned, naming what is owned",
                   status == 400 and kind(body).endswith("selling-equity-not-owned")
                   and "owned: 2.5" in body["detail"])
            status, body = buy(gbp, "BBB_US_EQ", -1)
            expect("selling what is not held at all: owned 0",
                   status == 400 and "owned: 0" in body["detail"])
            status, body = buy(gbp, "AAA_US_EQ", -2.49)
            expect("a sell leaving a sliver under 1.00 is refused: min-opened-position",
                   status == 400 and kind(body).endswith("min-opened-position-exceeded"))
            status, body = buy(gbp, "NOPE_US_EQ", 1)
            expect("no price: refused, not filled at a guess",
                   status == 400 and "no price" in body["detail"])
            status, body = buy(gbp, "OLD_US_EQ", 1)
            expect("a close 30 days old is no price", status == 400
                   and "newest close" in body["detail"])
            status, body = buy(gbp, "BOOM_US_EQ", 1)
            expect("a price source that raises is no price, not a crash",
                   status == 400 and "source broke" in body["detail"])
            expect("missing quantity, zero or text are refused",
                   call(gbp, "place_market_order", "POST", {"ticker": "AAA_US_EQ"})[0] == 400
                   and buy(gbp, "AAA_US_EQ", 0)[0] == 400
                   and buy(gbp, "AAA_US_EQ", "1")[0] == 400)
            expect("a GET on the order endpoint is 405, an unknown path 404",
                   call(gbp, "place_market_order")[0] == 405
                   and request(gbp, "/equity/nothing")[0] == 404)
            after = json.loads((folder / "gbp.json").read_text(encoding="utf-8"))
            expect("no refusal changed the account",
                   before["cash"] == after["cash"] and before["positions"] == after["positions"]
                   and len(before["orders"]) == len(after["orders"]))

            # ---------------------------------------------------------- sells
            status, body = buy(gbp, "AAA_US_EQ", -1)
            summary = call(gbp, "account_summary")[1]
            expect("a sell pays the close less the FX fee: 80.00 less 0.12",
                   status == 200 and summary["cash"]["availableToTrade"] == 9379.58)
            row = [r for r in call(gbp, "positions")[1]
                   if r["instrument"]["ticker"] == "AAA_US_EQ"][0]
            expect("and the holding and its cost shrink in proportion",
                   row["quantity"] == 1.5 and row["walletImpact"]["totalCost"] == 120.0)
            status, body = buy(gbp, "AAA_US_EQ", -1.5)
            expect("a whole exit leaves nothing behind and no position row",
                   status == 200 and all(r["instrument"]["ticker"] != "AAA_US_EQ"
                                         for r in call(gbp, "positions")[1]))

            # ---------------------------------------------------------- the hold
            # Synthetic: cash 1,000.00 and a 5% hold leave 950.00 a buy may use. 23 shares
            # of BBB at 40.00 pounds is 920.00 + 1.38 fee and fills; 24 is 960.00 + 1.44,
            # under the cash but over 950.00, so a check against cash alone passes a buy the
            # broker refuses, which is why the gate counts every buy as value / (1 - hold).
            hold = settings("hold.json", starting_cash=1000.0)
            status, body = buy(hold, "BBB_US_EQ", 24)
            expect("a buy within the cash but over (1 - hold) of it is refused",
                   status == 400 and kind(body).endswith("insufficient-free-for-stocks-buy")
                   and body["detail"] == "Insufficient funds")
            status, body = buy(hold, "BBB_US_EQ", 23)
            expect("and one inside (1 - hold), fee included, fills",
                   status == 200 and call(hold, "account_summary")[1]["cash"][
                       "availableToTrade"] == 78.62)

            # ---------------------------------------------------------- currencies
            eur = settings("eur.json", currency="EUR")
            status, body = buy(eur, "MWIXl_EQ", 10)
            summary = call(eur, "account_summary")[1]
            # 5.00 pounds at 1.25 / 1.10 euros a pound is 5.6818 euros: 56.82 for 10, and
            # a GBP line in a EUR account pays the FX fee, 0.09.
            expect("a EUR account: a pounds line is converted through dollars, and pays fee",
                   status == 200 and summary["currency"] == "EUR"
                   and summary["cash"]["availableToTrade"] == 9943.09)
            usd = settings("usd.json", currency="USD")
            buy(usd, "AAA_US_EQ", 1)
            expect("a USD account: a US share at its dollar close, no fee",
                   call(usd, "account_summary")[1]["cash"]["availableToTrade"] == 9900.0)
            status, body = call(settings("eur.json", currency="GBP"), "account_summary")
            expect("an account currency changed under an existing state is refused (409)",
                   status == 409 and "reset" in body["detail"])

            # ---------------------------------------------------------- revaluation, cache
            clear_cache()
            calls["fetch"] = 0
            buy(hold, "BBB_US_EQ", -23)
            buy(hold, "BBB_US_EQ", 10)
            first = call(hold, "positions")[1][0]["walletImpact"]["currentValue"]
            dollars["BBB_US_EQ"] = 60.0
            again = call(hold, "positions")[1][0]["walletImpact"]["currentValue"]
            expect("a price is kept in memory for 15 minutes",
                   first == again == 400.0 and calls["fetch"] == 1)
            clock[0] += timedelta(minutes=16)
            moved = call(hold, "positions")[1][0]["walletImpact"]["currentValue"]
            expect("then read again, and the holding revalued", moved == 480.0
                   and calls["fetch"] == 2)
            clock[0] += timedelta(minutes=16)
            down.add("BBB_US_EQ")
            status, rows = call(hold, "positions")
            expect("no price now: the last known one values it, still 200",
                   status == 200 and rows[0]["walletImpact"]["currentValue"] == 480.0)
            status, body = buy(hold, "BBB_US_EQ", 1)
            expect("but no order fills without a price now", status == 400)
            down.discard("BBB_US_EQ")

            # ---------------------------------------------------------- persistence, writes
            clear_cache()
            reloaded = Paper(gbp).load()
            expect("persistence: a fresh read of the file sees the same account",
                   reloaded["cash"] == 9499.40 and set(reloaded["positions"]) == {"MWIXl_EQ"}
                   and len(reloaded["orders"]) == 4)
            leftovers = [p.name for p in folder.iterdir() if p.name.endswith(".tmp")]
            expect("atomic writes leave no temporary files behind", not leftovers)
            real_replace = os.replace
            text = (folder / "gbp.json").read_text(encoding="utf-8")

            def broken(*args, **kwargs):
                raise OSError("disk full")
            os.replace = broken
            try:
                status, body = buy(gbp, "MWIXl_EQ", 1)
            finally:
                os.replace = real_replace
            expect("a write that fails mid-way is an error and leaves the old state whole",
                   status == 500 and (folder / "gbp.json").read_text(encoding="utf-8") == text
                   and not [p for p in folder.iterdir() if p.name.endswith(".tmp")])
            (folder / "bad.json").write_text("{not json", encoding="utf-8")
            status, body = call(settings("bad.json"), "account_summary")
            expect("an unreadable state file is reported and never replaced",
                   status == 500 and (folder / "bad.json").read_text(encoding="utf-8")
                   == "{not json")

            # ---------------------------------------------------------- paging
            paged = settings("paged.json", starting_cash=100000.0)
            for _ in range(55):
                buy(paged, "MWIXl_EQ", 1)
            status, body = call(paged, "history_orders", query="?limit=3")
            ids = [i["order"]["id"] for i in body["items"]]
            expect("history pages newest first, honouring limit",
                   status == 200 and ids == [55, 54, 53]
                   and body["nextPagePath"].startswith("/api/v0/equity/history/orders?"))
            status, body = request(paged, body["nextPagePath"])
            expect("nextPagePath, /api/v0 prefix and all, is the next page",
                   [i["order"]["id"] for i in body["items"]] == [52, 51, 50])
            expect("the default page is 20 and a limit over 50 is refused",
                   len(call(paged, "history_orders")[1]["items"]) == 20
                   and call(paged, "history_orders", query="?limit=51")[0] == 400)
            import broker
            waits = []
            status, rows, notes = broker.order_history(
                lambda path: broker.request(paged, path, "", ""),
                ENDPOINTS["history_orders"], "2000-01-01T00:00:00+00:00", sleep=waits.append)
            expect("broker.order_history reads every page of it: 55 orders in 2 requests",
                   status == 200 and len(rows) == 55 and len(waits) == 1 and not notes)

            # ---------------------------------------------------------- exchanges, hours
            import hours
            status, body = call(gbp, "exchanges")
            schedules = hours.schedules_by_id(body)
            lse = VENUES["LSE"]["schedule"]
            windows = sorted(schedules.get(lse, []))
            expect("exchanges: the LSE only, 10 weekday sessions in the next 14 days",
                   status == 200 and set(schedules) == {lse} and len(windows) == 10)
            expect("in summer time it opens 07:00 and closes 15:30 UTC",
                   windows and windows[0][0].strftime("%H:%M") == "07:00"
                   and windows[0][1].strftime("%H:%M") == "15:30")
            clock[0] = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
            winter = sorted(hours.schedules_by_id(call(gbp, "exchanges")[1])[lse])
            expect("in winter 08:00 to 16:30 UTC", winter[0][0].strftime("%H:%M") == "08:00"
                   and winter[0][1].strftime("%H:%M") == "16:30")
            expect("and the US has no published schedule, as on Trading 212",
                   US_SCHEDULE not in schedules)

            # ---------------------------------------------------------- instruments
            clock[0] = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
            listed = settings("listed.json")
            calls.update(wiki=0, spus=0)
            status, body = call(listed, "instruments")
            by = {r["ticker"]: r for r in body}
            expect("instruments: US shares as SYMBOL_US_EQ, STOCK, USD, unpublished schedule",
                   status == 200 and by.get("AAA_US_EQ", {}).get("type") == "STOCK"
                   and by["AAA_US_EQ"]["currencyCode"] == "USD"
                   and by["AAA_US_EQ"]["workingScheduleId"] == US_SCHEDULE)
            expect("from the index list and the SPUS file together",
                   "CCC_US_EQ" in by and "BRK-B_US_EQ" in by and "DDD_US_EQ" in by)
            expect("plus the configured symbols on their venue's schedule",
                   by.get("MWIXl_EQ", {}).get("workingScheduleId") == lse
                   and by["MWIXl_EQ"]["currencyCode"] == "GBX")
            import momentum
            mapped = momentum.t212_tickers(body)
            expect("momentum.t212_tickers maps them by symbol",
                   mapped.get("AAA") == "AAA_US_EQ" and mapped.get("BRK-B") == "BRK-B_US_EQ"
                   and "MWIXl_EQ" not in mapped.values())
            call(listed, "instruments")
            expect("the list is cached in the state file, not read on every call",
                   calls["wiki"] == 1 and calls["spus"] == 1
                   and json.loads((folder / "listed.json").read_text(encoding="utf-8"))[
                       "instruments"]["rows"])
            buy(listed, "DDD_US_EQ", 1)
            clock[0] += timedelta(days=8)
            down.update({"wiki", "spus"})
            wiki = wiki.replace("<td>DDD</td>", "<td>EEE</td>")
            status, body = call(listed, "instruments")
            expect("a week on it is read again, and a failed read keeps the old list",
                   status == 200 and calls["wiki"] == 2 and "AAA_US_EQ" in
                   {r["ticker"] for r in body})
            down.difference_update({"wiki", "spus"})
            clear_cache()
            clock[0] += timedelta(days=8)
            body = call(listed, "instruments")[1]
            expect("a held share that left the index stays listed, so it can be sold",
                   "DDD_US_EQ" in {r["ticker"] for r in body} and calls["wiki"] == 3)
            expect("and its sale goes through", buy(listed, "DDD_US_EQ", -1)[0] == 200)
            wiki = wiki.replace("<td>EEE</td>", "<td>DDD</td>")

            # ---------------------------------------------------------- a partial list
            # The index list fails and only SPUS's file is read: that is a part of the
            # universe (DDD is an index member SPUS does not hold). Cached for 7 days it
            # would refuse DDD all week and drop it from momentum's ticker map, so it serves
            # the call alone, and the next call reads the index again.
            clock[0] = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
            clear_cache()
            short = settings("partial.json")
            calls.update(wiki=0, spus=0)
            down.add("wiki")
            status, body = call(short, "instruments")
            seen = {r["ticker"] for r in body} if status == 200 else set()
            expect("index down: SPUS's shares are listed for this call, DDD is not",
                   status == 200 and {"AAA_US_EQ", "CCC_US_EQ", "MWIXl_EQ"} <= seen
                   and "DDD_US_EQ" not in seen)
            expect("but the short list is neither saved nor stamped",
                   json.loads((folder / "partial.json").read_text(encoding="utf-8"))[
                       "instruments"] == {})
            status, body = buy(short, "DDD_US_EQ", 1)
            expect("a share missing from a short list is unavailable (503), not unknown",
                   status == 503 and kind(body).endswith("instruments-unavailable")
                   and "incomplete" in body["detail"]
                   and not Paper(short).load()["positions"])
            status, body = buy(short, "AAA_US_EQ", 1)
            expect("while a share on it still trades", status == 200)
            clock[0] += timedelta(days=1)
            down.discard("wiki")
            status, body = call(short, "instruments")
            seen = {r["ticker"] for r in body} if status == 200 else set()
            cache = json.loads((folder / "partial.json").read_text(encoding="utf-8"))[
                "instruments"]
            expect("a day later the index is read again, not a week later",
                   calls["wiki"] == 4 and "DDD_US_EQ" in seen)
            expect("and the whole list is saved and stamped",
                   ["DDD", "Delta Ltd"] in cache.get("rows", [])
                   and cache.get("fetched", "").startswith("2026-10-15T10:00"))
            expect("so momentum.t212_tickers maps DDD again",
                   momentum.t212_tickers(body).get("DDD") == "DDD_US_EQ")
            expect("and DDD can be bought", buy(short, "DDD_US_EQ", 1)[0] == 200)
            # A stale whole copy and a short read: the call gets both, the copy stays as it
            # was, stamp and all, so the next call tries the index again.
            clock[0] += timedelta(days=8)
            clear_cache()
            down.add("wiki")
            saved_spus = spus
            spus += "FFF,Foxtrot Inc,10.00\n"
            calls["wiki"] = 0
            body = call(short, "instruments")[1]
            seen = {r["ticker"] for r in body}
            after = json.loads((folder / "partial.json").read_text(encoding="utf-8"))[
                "instruments"]
            expect("index down on a stale copy: the copy and the new read, merged",
                   {"DDD_US_EQ", "BBB_US_EQ", "FFF_US_EQ"} <= seen)
            expect("and the copy is left as it was, unstamped by the short read",
                   after == cache)
            call(short, "instruments")
            expect("so the next call reads the index again", calls["wiki"] == 2)
            spus = saved_spus
            down.discard("wiki")
            shown = io.StringIO()
            with contextlib.redirect_stdout(shown):
                cmd_reset(Paper(short), None)
            fresh = Paper(short).load()
            expect("reset: starting cash, nothing held, the whole list kept",
                   fresh["cash"] == 10000.0 and not fresh["positions"]
                   and fresh["instruments"] == cache)
            expect("and it names the paper account's own ledger, orders_placed.paper.json",
                   "orders_placed.paper.json" in shown.getvalue())

            # ---------------------------------------------------------- by hand, 8 places
            clock[0] = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
            clear_cache()
            hand = settings("hand.json")
            paper = Paper(hand)
            status, body = paper.buy_value("DDD_US_EQ", 1003)
            held = Paper(hand).load()["positions"]["DDD_US_EQ"]["quantity"]
            # 88 dollars at 1.25 is 70.40 pounds a share: 1003 buys 14.24715909 shares.
            expect("buy by value, as by hand in the app: up to 8 decimal places",
                   status == 200 and held == 14.24715909)
            status, body = buy(hand, "DDD_US_EQ", -round(held, 4))
            expect("rounding that up to 4 places sells more than is owned",
                   status == 400 and kind(body).endswith("selling-equity-not-owned"))
            status, body = buy(hand, "DDD_US_EQ", -held)
            expect("sending all 8 places is invalid quantity precision", status == 400
                   and "precision" in body["detail"])
            quantity, how = sizing.sell_quantity(held, held, 70.4, 2.0)
            status, body = buy(hand, "DDD_US_EQ", -quantity)
            expect("sizing.sell_quantity's exit, which keeps 2.00 behind, is accepted",
                   status == 200 and "all but" in how)
            status, body = Paper(hand).sell_quantity("DDD_US_EQ", "all")
            expect("and the by-hand sell-all clears the remainder",
                   status == 200 and "DDD_US_EQ" not in Paper(hand).load()["positions"])

            # ---------------------------------------------------------- the runner on it
            import trade
            clock[0] = datetime.now(timezone.utc).replace(microsecond=0)
            clear_cache()
            live = settings("runner.json")
            expect("broker.credentials needs nothing for paper",
                   broker.credentials(live) == ("", ""))
            expect("broker.adapter_label is the paper label", broker.adapter_label(live)
                   == LABEL)
            buy(live, "AAA_US_EQ", 2)
            buy(live, "MWIXl_EQ", 10)
            state = trade.account_state(live, "", "")
            expect("trade.account_state reads it all, with nothing unknown",
                   state["unknown"] == [] and state["cash"] == 9789.76
                   and set(state["positions"]) == {"AAA_US_EQ", "MWIXL_EQ"})
            expect("prices in the account currency, the broker's spelling kept",
                   abs(state["positions"]["AAA_US_EQ"]["current_price"] - 80.0) < 1e-9
                   and abs(state["positions"]["MWIXL_EQ"]["current_price"] - 5.0) < 1e-9
                   and state["positions"]["MWIXL_EQ"]["broker_ticker"] == "MWIXl_EQ")
            expect("orders today and the cooldown clock come from the history",
                   state["orders_today"] == 2 and "AAA_US_EQ" in state["last_trade"])
            order = {"ticker": "AAA_US_EQ", "action": "sell", "quantity": -2.0}
            status, _ = broker.request(live, ENDPOINTS["place_market_order"], "", "",
                                       method="POST", payload={"ticker": "AAA_US_EQ",
                                                               "quantity": -2.0})
            holdings = broker.request(live, ENDPOINTS["positions"], "", "")[1]
            expect("trade.settled sees the sale at once",
                   status == 200 and trade.settled([(order, True)], state["positions"],
                                                   holdings) == (True, []))
            expect("and is not fooled before it", trade.settled(
                [({"ticker": "MWIXl_EQ", "quantity": 5.0}, True)], state["positions"],
                holdings) == (False, ["MWIXL_EQ"]))
            new_york = {"_US_EQ": {"name": "US", "timezone": "America/New_York",
                                   "open_local": "09:30", "close_local": "16:00",
                                   "holidays_known_through": "2099-12-31"}}
            clock[0] = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
            orders = [{"ticker": "MWIXL_EQ"}, {"ticker": "AAA_US_EQ"}]
            with contextlib.redirect_stdout(io.StringIO()):
                morning = trade.check_market_hours(live, "", "", orders, clock[0], new_york)
                afternoon = trade.check_market_hours(
                    live, "", "", orders, clock[0].replace(hour=14), new_york)
            expect("market hours: the LSE line open at 10:00 UTC, the US share held",
                   [t for t, _ in morning] == ["AAA_US_EQ"])
            expect("and both open at 14:00 UTC, the US by the configured fallback",
                   afternoon == [])
    finally:
        HOOKS.clear()
        HOOKS.update(saved_hooks)
        clear_cache()
        shutil.rmtree(str(folder), ignore_errors=True)

    width = max(len(n) for n, _ in checks)
    for name, passed in checks:
        print("%s  %s" % ("pass" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in checks if not p]
    print("\n%d checks, %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    # Run as brokers.paper, not __main__, so broker.request (which imports brokers.paper)
    # and this command share one module: one set of HOOKS, one price cache.
    from brokers import paper as module
    sys.exit(module.main())
