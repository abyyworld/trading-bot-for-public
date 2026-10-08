#!/usr/bin/env python3
"""
The momentum book: a core fund at a fixed share of the account, and the rest split evenly
across the N halal stocks with the strongest 12-month rise, refreshed once a month.

Why this design: in research.py's point-in-time backtest (each stock counted only from the
day it joined the index, 2007 to 2026), top-10 12-1 momentum beat owning the same list
equally, with a deeper worst fall. Delisted companies are still missing from that data, so
even its numbers are flattered: after the usual decay of published effects, expect the
momentum sleeve to beat the core fund by about 2.5 points a year (anywhere from 0 to 5), so
roughly 2 points a year on a 20/80 account, and a 2008-style fall of 45 to 55%. Quote
research.py, never backtest.py, whose absolute returns are inflated.

How it decides, all of it failing closed:

  halal list   universe 'not_haram': the members of `indexes`, the S&P 500 by default, minus
               haram businesses per compliance.screen (universe.py). Index labels and ratio
               screens do not decide it. If that list cannot be read, or the screen leaves
               under min_universe names, SPUS's own holdings file is used instead, and with
               neither no targets are built: nothing is bought. universe 'spus' uses SPUS's
               holdings directly. A pick is only buyable in the month its targets were built.
  ranking      12-1 momentum on Yahoo monthly closes, the same code the backtest ran
               (backtest.signals_at), on the last COMPLETE month. Stale data builds nothing.
  tickers      each pick must be in the broker's instrument list as a US share (Trading 212's
               ticker format), matched by symbol.
  timing       targets are built once a month and written to momentum_targets.json; trades
               happen only in the window_days after the build. Every other day it checks
               and does nothing, because swapping daily would hand the gain to fees.
  prices       for a stock not yet held, Yahoo's latest close in dollars converted to the
               account currency (config account.currency: GBP through GBPUSD, EUR through
               EURUSD, USD as is; any other currency gets no price, so no buy), and refused
               unless it agrees within tolerance with a reference taken at the build from an
               INDEPENDENT source: the SPUS file's price if SPUS holds it, else Nasdaq's
               close. A pick with neither is skipped for the next in line, never bought on
               one source.
  trades       sell whatever is neither the core nor a pick, bring each pick to its share and
               the core to its share, inside a band so drift alone does not churn. Every one
               then passes gates.py like any other proposal: caps, cash buffer, cooldown.

    python3 momentum.py selftest
"""

import argparse
import csv
import io
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPUS_HOLDINGS = "https://www.sp-funds.com/wp-content/uploads/data/TidalFG_Holdings_SPUS.csv"
FX_URL = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=1d"
# Dollars per one unit of the account currency: Yahoo's pair and the band a quote must fall
# inside to be believed. A broken quote must not size an order, so outside it means no price.
FX_PAIRS = {"GBP": ("GBPUSD=X", 1.0, 2.0), "EUR": ("EURUSD=X", 0.8, 1.6)}


def settings(config):
    return config.get("strategy", {}).get("momentum", {})


def targets_path(config, directory=HERE):
    return Path(directory) / settings(config).get("file", "momentum_targets.json")


# ---------------------------------------------------------------------- the halal list

def parse_holdings(text):
    """[(symbol, name, usd_price)] from the SP Funds holdings CSV; cash lines dropped."""
    out = []
    for row in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        symbol = (row.get("StockTicker") or "").strip().upper()
        name = (row.get("SecurityName") or "").strip()
        if not symbol or not symbol[0].isalpha() or "CASH" in symbol or "CASH" in name.upper():
            continue
        try:
            price = float(str(row.get("Price") or "").replace(",", ""))
        except ValueError:
            price = 0.0
        out.append((symbol.replace(".", "-").replace("/", "-"), name, price))
    return out


def t212_tickers(instruments):
    """{SYMBOL: Trading 212 ticker} for US shares, matched on the plain symbol."""
    out = {}
    for row in instruments or []:
        ticker = str(row.get("ticker") or "")
        short = str(row.get("shortName") or "").strip().upper().replace(".", "-")
        if ticker.endswith("_US_EQ") and short and str(row.get("type", "STOCK")) == "STOCK":
            out.setdefault(short, ticker)
    return out


# ---------------------------------------------------------------------- building targets

def previous_month(today):
    return "%04d-%02d" % ((today.year, today.month - 1) if today.month > 1
                          else (today.year - 1, 12))


def load_universe(config, getter):
    """([universe rows], {symbol: SPUS price}, notes). A row is {symbol, name, sector,
    sub_industry}. The not-haram list when it can be read AND the screen proves sound on
    it (universe.sound: rules present, canaries excluded); SPUS's holdings otherwise, with
    the named exclusions (compliance.screen.excluded_symbols) still applied; empty when
    neither can be had."""
    import universe as screening
    s = settings(config)
    floor = int(s.get("min_universe", 100))
    rules = screening.rules_from(config)
    notes, spus = [], []
    try:
        spus = parse_holdings(getter(s.get("universe_url", SPUS_HOLDINGS),
                                     {"Accept": "text/csv,*/*"}))
    except Exception as error:
        notes.append("could not read the SPUS holdings: %s" % error)
    prices = {symbol: price for symbol, _, price in spus if price > 0}
    if s.get("universe", "spus") == "not_haram":
        rows, more = screening.load(s.get("indexes") or ["sp500"], getter)
        notes += more
        kept, dropped = screening.screen(rows, rules)
        broken = screening.sound(rows, kept, rules) if rows else "no index rows"
        unreviewed = [r["symbol"] for r, why in dropped if why.startswith(("unreviewed", "no sub"))]
        if unreviewed:
            notes.append("not screened, so left out: %s" % " ".join(unreviewed[:20]))
        if broken:
            notes.append("the not-haram list is not trusted (%s), so SPUS is used" % broken)
        elif len(kept) < floor:
            notes.append("the screened index list had only %d names, so SPUS is used" % len(kept))
        else:
            notes.append("not haram: %d of %d index members kept, %d excluded"
                         % (len(kept), len(rows), len(dropped)))
            return kept, prices, notes
    # SPUS is screened by S&P, but the named exclusions still apply to it.
    named = {"excluded_symbols": rules.get("excluded_symbols") or []}
    rows = [{"symbol": symbol, "name": name, "sector": "", "sub_industry": ""}
            for symbol, name, _ in spus]
    kept, dropped = screening.screen(rows, named)
    if dropped:
        notes.append("named exclusions removed from SPUS: %s"
                     % " ".join(r["symbol"] for r, _ in dropped))
    if len(kept) >= floor:
        return kept, prices, notes
    notes.append("the SPUS holdings had only %d names" % len(kept))
    return [], prices, notes


def company(name):
    """One key per company across share classes: 'Alphabet Inc. (Class A)' and '(Class C)',
    'Fox Corporation (Class B)', 'NEWS CORP-CL A' all lose the class."""
    text = re.sub(r"\(?\b(class|cl)[\s.-]*[a-z]\b\)?", " ", str(name or "").lower())
    text = re.sub(r"-\s*cl\s*[a-z]\b", " ", text)
    return re.sub(r"[^a-z0-9]+", "", text)


def screen_picks(config, targets):
    """(picks still allowed, [(ticker, reason)]). Picks are re-checked against today's
    compliance.screen on every run, so an exclusion added mid-month revokes a pick at
    once, and proposals() then sells it as no longer a pick. Older targets carry no
    sub-industry, so only the named exclusions can be applied to them."""
    import universe as screening
    rules = screening.rules_from(config)
    kept, dropped = [], []
    for pick in (targets or {}).get("picks", []):
        row = {"symbol": screening.yahoo_symbol(pick.get("symbol") or ""),
               "name": "", "sector": pick.get("sector", ""),
               "sub_industry": pick.get("sub_industry", "")}
        applied = rules if row["sub_industry"] else {
            "excluded_symbols": rules.get("excluded_symbols") or []}
        ok, why = screening.screen([row], applied)
        if ok:
            kept.append(pick)
        else:
            dropped.append((pick.get("ticker"), why[0][1]))
    return kept, dropped


def build_targets(config, today, instruments, getter=None, fetch_monthly=None,
                  sleep=time.sleep, reference=None):
    """(targets, None) or (None, why not). Network, so only ever called once a month.

    reference(symbol) -> (dollar price, source) or (None, why) is the independent price for
    a pick SPUS does not hold; Nasdaq by default. Never Yahoo, which sizes the buy."""
    import backtest
    import pricefeed
    s = settings(config)
    getter = getter or pricefeed.http_get
    fetch_monthly = fetch_monthly or backtest.fetch_monthly
    reference = reference or (lambda symbol: pricefeed.nasdaq_close(symbol, sleep=sleep))
    universe, spus_prices, notes = load_universe(config, getter)
    if not universe:
        return None, "; ".join(notes) or "no universe"
    about = {r["symbol"]: r for r in universe}

    data = {}
    for number, symbol in enumerate(r["symbol"] for r in universe):
        if number:
            sleep(0.3)
        bars = fetch_monthly(symbol, span="2y")
        if bars:
            data[symbol] = bars
    signal_month = previous_month(today)
    months = sorted({m for bars in data.values() for m in bars if m <= signal_month})
    if not months or months[-1] != signal_month:
        return None, "no monthly close for %s yet" % signal_month
    signals = backtest.signals_at(data, months, len(months) - 1)
    # 90%, not half: Yahoo can return quarterly bars for long-listed names, which drops them
    # from the ranking, and a floor of half once let a build that ranked barely half its list
    # through unnoticed.
    if len(signals) < 0.9 * len(universe):
        return None, "only %d of %d names had 13 months of closes" % (len(signals), len(universe))

    mapping = t212_tickers(instruments)
    wanted = int(s.get("picks", 10))
    picks, skipped, unpriced, companies = [], [], [], set()
    for symbol, (mom, _) in sorted(signals.items(), key=lambda kv: (-kv[1][0], kv[0])):
        ticker = mapping.get(symbol)
        if not ticker:
            skipped.append(symbol)
            continue
        row = about.get(symbol, {})
        # One slot per company: GOOGL and GOOG, FOX and FOXA rank side by side.
        key = company(row.get("name")) or symbol
        if key in companies:
            continue
        if symbol in spus_prices:
            price, source = spus_prices[symbol], "SPUS file"
        else:
            try:
                price, source = reference(symbol)
            except Exception as error:   # one bad reply must not stop the whole build
                price, source = None, "reference failed: %s" % error
            if not price:
                unpriced.append("%s (%s)" % (symbol, source))
                continue
        companies.add(key)
        picks.append({"ticker": ticker, "symbol": symbol, "momentum": round(mom, 4),
                      "usd_price": price, "price_source": source,
                      "labelled_halal": symbol in spus_prices,
                      "sector": row.get("sector", ""),
                      "sub_industry": row.get("sub_industry", "")})
        if len(picks) == wanted:
            break
    if len(picks) < wanted:
        return None, ("only %d of %d picks are in the broker's instruments with an independent "
                      "price%s"
                      % (len(picks), wanted,
                         ": no price for %s" % ", ".join(unpriced[:5]) if unpriced else ""))
    return {"month": today.strftime("%Y-%m"), "built": today.isoformat(),
            "signal_month": signal_month, "universe": len(universe),
            "universe_rule": s.get("universe", "spus"), "universe_notes": notes,
            "ranked": len(signals), "not_on_t212": skipped, "no_reference_price": unpriced,
            "picks": picks}, None


def load_targets(config, directory=HERE):
    path = targets_path(config, directory)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("picks") else None


def save_targets(config, targets, directory=HERE):
    targets_path(config, directory).write_text(json.dumps(targets, indent=2) + "\n",
                                               encoding="utf-8")


def current(targets, today, adapter=None):
    """True when the targets were built this calendar month, and, when adapter is given,
    from that adapter's instrument list.

    trade.py stamps the adapter into the targets it builds. Picks are mapped to the
    broker's own tickers, and the paper account names every US share SYMBOL_US_EQ while
    Trading 212 sometimes does not (SanDisk is SNDK1_US_EQ there): a month built on paper
    and then traded on Trading 212 would hold a pick with no Trading 212 schedule all
    month. So a switch of adapter rebuilds the month. Targets with no adapter, from an
    older version, count as matching."""
    if not targets or targets.get("month") != today.strftime("%Y-%m"):
        return False
    built_for = str(targets.get("adapter") or "").strip().lower()
    wanted = str(adapter or "").strip().lower()
    return not (built_for and wanted and built_for != wanted)


def in_window(targets, today, window_days, adapter=None):
    if not current(targets, today, adapter):
        return False
    try:
        built = date.fromisoformat(str(targets.get("built")))
    except ValueError:
        return False
    return 0 <= (today - built).days <= int(window_days)


# ---------------------------------------------------------------------- the trades

def proposals(config, state, targets):
    """The trades that bring the account to core_weight in the core and the rest evenly in
    the picks. Sells first. Values in account currency; gates.py and sizing.py still decide.
    """
    s = settings(config)
    core = str(s.get("core_ticker", "")).strip().upper()
    weight = float(s.get("core_weight", 0.2))
    band = float(s.get("band", 0.10))
    floor = float(s.get("min_trade", 25.0))
    cap = float(config.get("risk", {}).get("max_order_value", float("inf")))
    positions = state.get("positions", {})
    picks = [str(p["ticker"]).strip().upper() for p in targets.get("picks", [])]
    if not picks or not core:
        return []
    total = float(state.get("cash", 0.0)) + sum(float(p.get("value", 0.0))
                                                for p in positions.values())
    per_pick = (1.0 - weight) * total / len(picks)
    out = []

    def add(ticker, action, value, why):
        if value >= floor:
            out.append({"ticker": ticker, "action": action, "value": round(value, 2),
                        "confidence": 1.0, "source": "momentum", "rationale": why})

    for ticker, pos in sorted(positions.items()):
        held = float(pos.get("value", 0.0))
        if ticker != core and ticker not in picks and held > 0:
            # A little over the holding, so sizing sells it all rather than leaving dust.
            add(ticker, "sell", held * 1.02, "momentum: no longer a top-%d pick" % len(picks))

    # What buys can still tie up: cash above the buffer, less what the buys already proposed
    # tie up. Counted the way gates.check counts it, broker's hold included, so a buy cut to
    # fit here passes there. A sale in the same batch does not count, as its cash lands only
    # once it fills.
    from gates import broker_hold, fraction, position_cap_for
    risk = config.get("risk", {})
    hold = broker_hold(risk)
    # A buy this code sizes to the cash is sized to survive a bigger hold than the gate
    # assumes (strategy.momentum.cut_hold): Trading 212 does not document its real hold, real
    # orders only bound it loosely, up to nearly 10%, and a refused buy blocks a retry for the
    # whole day.
    cut_hold = max(hold, fraction(s.get("cut_hold"), hold))
    spendable = [float(state.get("cash", 0.0)) - float(risk.get("min_cash_buffer", 0.0))]
    # Today's room under the daily spend cap, booked the same way. A buy the cap refuses
    # must not hold cash back from the next pick either: that pick would then be cut short
    # and start its 120-hour cooldown below its share.
    room = [float(risk.get("max_daily_spend", float("inf")))
            - float(state.get("spent_today", 0.0))]

    def held_of(ticker):
        return float(positions.get(ticker, {}).get("value", 0.0))

    goals = [(t, per_pick, "pick") for t in picks] + [(core, weight * total, "core")]
    # Whether this batch also sells, so more cash is on its way once the sale fills.
    coming = bool(out) or any(held_of(t) > g * (1.0 + band) and held_of(t) - g >= floor
                              for t, g, _ in goals)

    def toward(ticker, held, target, label):
        if held > target * (1.0 + band):
            add(ticker, "sell", held - target, "momentum: trim %s to %.0f" % (label, target))
            return
        # Never asked for past the single-stock cap: the gate refuses a buy over it whole,
        # which left the slot in cash for the month once a pick's share passed the cap.
        goal = min(target, position_cap_for(risk, ticker))
        if held < goal * (1.0 - band):
            # Down to the penny before any bookkeeping, as add() and the gate both use it.
            want = int(min(goal - held, cap) * 100 + 1e-6) / 100.0
            safe = int(max(spendable[0] * (1.0 - cut_hold) - 0.01, 0.0) * 100) / 100.0
            if want > safe and (held + safe >= goal * (1.0 - band) or not coming):
                # With every pick at its share, the core's gap IS the cash, so asking for it
                # whole always fell a little short of the buffer and the gate refused it on
                # every run. Buy what the cash surely covers when that lands inside the
                # band, or when no sale in this batch is bringing more: a pick swapped for
                # one whose sale fell short of its share left the whole slot in cash for the
                # month. While a sale is still to fill, ask for the whole share and let the
                # gate wait for its cash: a part-buy now would start the 120-hour cooldown
                # with the holding short, and the second pass buys it whole instead.
                want = safe
            # Booked exactly as the gate will judge it, on the cash and on the daily cap,
            # so a buy it passes is never forgotten here and one it refuses never holds
            # cash back from the next.
            if floor <= want and want / (1.0 - hold) <= spendable[0] and want <= room[0]:
                spendable[0] -= want / (1.0 - hold)
                room[0] -= want
            add(ticker, "buy", want, "momentum: %s to %.0f" % (label, goal))

    for ticker, goal, label in goals:
        toward(ticker, held_of(ticker), goal, label)
    return out


# ---------------------------------------------------------------------- prices

def account_currency(config):
    """The account's own currency, config account.currency, upper-cased: GBP when unset."""
    return str(((config or {}).get("account") or {}).get("currency") or "GBP").strip().upper()


def fx_rate(currency, getter=None):
    """Dollars per one unit of the account currency, or None.

    USD is 1.0 and asks nothing. GBP and EUR come from Yahoo's GBPUSD=X and EURUSD=X, each
    sanity-bounded (FX_PAIRS): a broken quote must not size an order. Any other currency is
    None, which means no outside price and so no buy: fails closed rather than guessing."""
    currency = str(currency or "").strip().upper()
    if currency == "USD":
        return 1.0
    if currency not in FX_PAIRS:
        return None
    pair, low, high = FX_PAIRS[currency]
    import pricefeed
    getter = getter or pricefeed.http_get
    try:
        series = pricefeed.parse_yahoo(json.loads(getter(FX_URL % pair, None)))
    except Exception:
        return None
    rate = series[-1][1] if series else None
    return rate if rate and low < rate < high else None


def gbpusd(getter=None):
    """Dollars per pound, or None. Kept for callers of the pounds-only version."""
    return fx_rate("GBP", getter)


def outside_prices(tickers, targets, config, fetch=None, rate=None, getter=None):
    """{TICKER: (account_price, source)} for picks not yet held, or nothing for a ticker.

    Yahoo's latest dollar close converted to the account currency (fx_rate), accepted only
    if it is within tolerance of the price in the SPUS file the targets were built from. Two
    independent sources have to agree before an amount becomes a share count. rate, when
    given, is dollars per unit of the account currency and skips the lookup; getter is the
    HTTP getter fx_rate uses.
    """
    import pricefeed
    fetch = fetch or pricefeed.fetch
    tolerance = float(settings(config).get("price_check_tolerance", 0.25))
    currency = account_currency(config)
    reference = {str(p["ticker"]).upper(): float(p.get("usd_price") or 0)
                 for p in (targets or {}).get("picks", [])}
    # Looked up by the exchange symbol the pick was ranked under, NOT by the Trading 212
    # ticker: SanDisk is SNDK1_US_EQ there, and stripping the suffix asks Yahoo for "SNDK1".
    symbols = {str(p["ticker"]).upper(): str(p.get("symbol") or "").upper()
               for p in (targets or {}).get("picks", [])}
    origins = {str(p["ticker"]).upper(): str(p.get("price_source") or "").split(" ")[0].lower()
               for p in (targets or {}).get("picks", [])}
    rate = rate if rate is not None else fx_rate(currency, getter)
    out = {}
    if not rate:
        return out
    if currency == "USD":
        how = "no conversion, a USD account"
    else:
        how = "at %sUSD %.4f" % (currency, rate)
    for ticker in tickers:
        ticker = str(ticker).upper()
        expected = reference.get(ticker)
        if not expected:
            continue
        symbol = symbols.get(ticker)
        if not symbol:
            continue
        series, source = fetch("%s_US_EQ" % symbol)
        if not series:
            continue
        # Two sources must agree. If the reference came from Nasdaq and Yahoo was refused,
        # pricefeed.fetch falls back to Nasdaq too, and the check would be one source twice.
        if origins.get(ticker) and str(source).lower().startswith(origins[ticker]):
            continue
        dollars = series[-1][1]
        if abs(dollars - expected) / expected > tolerance:
            continue
        out[ticker] = (dollars / rate, "%s %.2f USD %s" % (source, dollars, how))
    return out


# ---------------------------------------------------------------------- selftest

def cmd_selftest(args):
    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    csv_text = ("Date,Account,StockTicker,CUSIP,SecurityName,Shares,Price\n"
                "10/06/2026,SPUS,NVDA,1,NVIDIA Corp,1,238.9\n"
                "10/06/2026,SPUS,BRK.B,1,B,1,\"1,000.5\"\n"
                "10/06/2026,SPUS,,1,Cash & Other,1,1\n")
    rows = parse_holdings(csv_text)
    expect("holdings parse with prices, cash dropped",
           rows == [("NVDA", "NVIDIA Corp", 238.9), ("BRK-B", "B", 1000.5)])

    instruments = [{"ticker": "NVDA_US_EQ", "shortName": "NVDA", "type": "STOCK"},
                   {"ticker": "NVDd_EQ", "shortName": "NVDA", "type": "STOCK"},
                   {"ticker": "SPY_US_EQ", "shortName": "SPY", "type": "ETF"}]
    expect("only US shares map to tickers", t212_tickers(instruments) == {"NVDA": "NVDA_US_EQ"})

    expect("previous month wraps the year", previous_month(date(2027, 1, 4)) == "2026-12")
    targets = {"month": "2026-10", "built": "2026-10-06", "picks": []}
    expect("inside the window", in_window(targets, date(2026, 10, 13), 7))
    expect("outside the window after a week", not in_window(targets, date(2026, 10, 14), 7))
    expect("last month's targets are not current", not current(targets, date(2026, 11, 2)))

    # Build: 3 names, fake monthly bars, ranked and mapped.
    months = ["2025-%02d" % m for m in range(9, 13)] + ["2026-%02d" % m for m in range(1, 10)]

    def bars(rate):
        out, price = {}, 100.0
        for m in months:
            price *= 1 + rate
            out[m] = (price, price)
        return out

    universe_csv = ("StockTicker,SecurityName,Price\nAAA,A,10\nBBB,B,20\nCCC,C,30\n")
    fake = {"AAA": bars(0.05), "BBB": bars(0.01), "CCC": bars(-0.02)}
    config = {"strategy": {"momentum": {"picks": 2, "min_universe": 3, "universe_url": "u"}}}
    maps = [{"ticker": "%s_US_EQ" % s, "shortName": s, "type": "STOCK"} for s in fake]
    built, why = build_targets(config, date(2026, 10, 6), maps,
                               getter=lambda url, h: universe_csv,
                               fetch_monthly=lambda s, span=None: fake[s],
                               sleep=lambda x: None)
    expect("build ranks by momentum and maps tickers",
           built and [p["ticker"] for p in built["picks"]] == ["AAA_US_EQ", "BBB_US_EQ"])
    expect("build records the SPUS price for the cross-check",
           built and built["picks"][0]["usd_price"] == 10.0)
    stale, why = build_targets(config, date(2026, 11, 2), maps,
                               getter=lambda url, h: universe_csv,
                               fetch_monthly=lambda s, span=None: fake[s],
                               sleep=lambda x: None)
    expect("no build without last month's close", stale is None and "2026-10" in why)
    short, why = build_targets(config, date(2026, 10, 6), maps[:1],
                               getter=lambda url, h: universe_csv,
                               fetch_monthly=lambda s, span=None: fake[s],
                               sleep=lambda x: None)
    expect("no build when picks are missing on Trading 212", short is None)
    thin, why = build_targets(dict(config, strategy={"momentum": {"min_universe": 100}}),
                              date(2026, 10, 6), maps, getter=lambda url, h: universe_csv,
                              fetch_monthly=lambda s, span=None: fake[s],
                              sleep=lambda x: None)
    expect("no build from a short halal list", thin is None and "only 3" in why)
    gappy = dict(fake, CCC={m: v for i, (m, v) in enumerate(sorted(fake["CCC"].items()))
                           if i % 3 == 0})
    holed, why = build_targets(config, date(2026, 10, 6), maps,
                               getter=lambda url, h: universe_csv,
                               fetch_monthly=lambda s, span=None: gappy[s],
                               sleep=lambda x: None)
    expect("quarterly bars for a name stop the build, not silently drop it",
           holed is None and "13 months" in why)

    # The not-haram universe: an index's members minus haram businesses, with a pick SPUS
    # does not hold priced from Nasdaq, never on Yahoo alone.
    import universe as screening
    wiki = ('<table id="constituents"><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th>'
            '<th>GICS Sub-Industry</th></tr>'
            '<tr><td>AAA</td><td>A</td><td>IT</td><td>Semiconductors</td></tr>'
            '<tr><td>BBB</td><td>B</td><td>Financials</td><td>Diversified Banks</td></tr>'
            '<tr><td>CCC</td><td>C</td><td>IT</td><td>Application Software</td></tr>'
            '<tr><td>DDD</td><td>D</td><td>Staples</td><td>Packaged Foods &amp; Meats</td></tr>'
            '</table>')
    ranked = {"AAA": bars(0.06), "BBB": bars(0.05), "CCC": bars(0.04), "DDD": bars(0.03)}
    spus_csv = "StockTicker,SecurityName,Price\nAAA,A,10\n"
    halal = {"strategy": {"momentum": {"picks": 2, "min_universe": 2, "universe": "not_haram",
                                       "indexes": ["sp500"], "universe_url": "u"}},
             "compliance": {"screen": {
                 "excluded_sub_industries": [{"name": "Diversified Banks", "activity": "interest"}],
                 "permitted_sub_industries": ["Semiconductors", "Application Software",
                                              "Packaged Foods & Meats"],
                 "canaries": ["BBB"],
                 "excluded_symbols": [{"symbol": "DDD", "activity": "pork", "reason": "x"}]}}}
    halal_maps = [{"ticker": "%s_US_EQ" % s, "shortName": s, "type": "STOCK"} for s in ranked]

    def pages(spus=spus_csv, wiki_page=wiki):
        def get(url, headers):
            if url == screening.INDEX_URLS["sp500"]:
                if wiki_page is None:
                    raise OSError("blocked")
                return wiki_page
            return spus
        return get

    asked = []
    nasdaq = lambda symbol: asked.append(symbol) or ((30.5, "nasdaq 2026-10-05")
                                                     if symbol == "CCC" else (None, "no data"))
    built, why = build_targets(halal, date(2026, 10, 6), halal_maps, getter=pages(),
                               fetch_monthly=lambda s, span=None: ranked[s],
                               sleep=lambda x: None, reference=nasdaq)
    got = {p["symbol"]: p for p in (built or {}).get("picks", [])}
    expect("not haram: a bank and a named pork producer are never picked, however strong",
           built and set(got) == {"AAA", "CCC"})
    expect("a stock SPUS has not labelled can be picked", built and not got["CCC"]["labelled_halal"])
    expect("a labelled one keeps the SPUS file's price", built and got["AAA"]["usd_price"] == 10.0
           and got["AAA"]["price_source"] == "SPUS file")
    expect("an unlabelled one is priced from Nasdaq, independently of Yahoo",
           built and got["CCC"]["usd_price"] == 30.5 and asked == ["CCC"])
    expect("the build records which rule made it", built and built["universe_rule"] == "not_haram")
    priceless = lambda symbol: (None, "nasdaq: blocked")
    short, why = build_targets(halal, date(2026, 10, 6), halal_maps, getter=pages(),
                               fetch_monthly=lambda s, span=None: ranked[s],
                               sleep=lambda x: None, reference=priceless)
    expect("no independent price means no pick, and no build when too few remain",
           short is None and "no price for CCC" in why)
    fallback, why = build_targets(halal, date(2026, 10, 6), halal_maps,
                                  getter=pages(spus="StockTicker,SecurityName,Price\nAAA,A,10\n"
                                               "CCC,C,30\n", wiki_page=None),
                                  fetch_monthly=lambda s, span=None: ranked[s],
                                  sleep=lambda x: None, reference=priceless)
    expect("an unreadable index list falls back to SPUS, not to an unscreened list",
           fallback and {p["symbol"] for p in fallback["picks"]} == {"AAA", "CCC"}
           and any("blocked" in n for n in fallback["universe_notes"]))
    neither, why = build_targets(halal, date(2026, 10, 6), halal_maps,
                                 getter=pages(spus="", wiki_page=None),
                                 fetch_monthly=lambda s, span=None: ranked[s],
                                 sleep=lambda x: None, reference=nasdaq)
    expect("with neither list nothing is built", neither is None)

    expect("each pick records its sub-industry, so it can be re-screened later",
           built and got["CCC"]["sub_industry"] == "Application Software")
    bare = dict(halal, compliance={"screen": {}})
    unscreened, why = build_targets(bare, date(2026, 10, 6), halal_maps,
                                    getter=pages(spus="StockTicker,SecurityName,Price\nAAA,A,10\n"
                                                 "CCC,C,30\n"),
                                    fetch_monthly=lambda s, span=None: ranked[s],
                                    sleep=lambda x: None, reference=nasdaq)
    expect("a missing screen never yields the unscreened index: SPUS is used instead",
           unscreened and "BBB" not in {p["symbol"] for p in unscreened["picks"]}
           and any("not trusted" in n for n in unscreened["universe_notes"]))
    # Two mistakes at once: banks dropped from the exclusions AND added to the permitted list.
    leaky = dict(halal, compliance={"screen": dict(
        halal["compliance"]["screen"], excluded_sub_industries=[{"name": "Nothing"}],
        permitted_sub_industries=halal["compliance"]["screen"]["permitted_sub_industries"]
        + ["Diversified Banks"])})
    leaked, why = build_targets(leaky, date(2026, 10, 6), halal_maps,
                                getter=pages(spus="StockTicker,SecurityName,Price\nAAA,A,10\n"
                                             "CCC,C,30\nBBB,B,20\n"),
                                fetch_monthly=lambda s, span=None: ranked[s],
                                sleep=lambda x: None, reference=nasdaq)
    expect("a screen that keeps a canary is refused, even when both lists are wrong",
           leaked and any("kept BBB" in n for n in leaked["universe_notes"]))
    named_spus = build_targets(dict(halal, compliance={"screen": {"excluded_symbols": [
        {"symbol": "CCC", "activity": "x", "reason": "y"}]}}), date(2026, 10, 6), halal_maps,
        getter=pages(spus="StockTicker,SecurityName,Price\nAAA,A,10\nCCC,C,30\nBBB,B,20\n",
                     wiki_page=None),
        fetch_monthly=lambda s, span=None: ranked[s], sleep=lambda x: None,
        reference=nasdaq)[0]
    expect("the SPUS fallback still applies the named exclusions",
           named_spus and "CCC" not in {p["symbol"] for p in named_spus["picks"]})
    def explode(symbol):
        raise ValueError("cut off")
    survived, why = build_targets(halal, date(2026, 10, 6), halal_maps, getter=pages(),
                                  fetch_monthly=lambda s, span=None: ranked[s],
                                  sleep=lambda x: None, reference=explode)
    expect("a reference that raises costs that pick, not the whole build",
           survived is None and "reference failed" in why)
    expect("share classes collapse to one company",
           company("Alphabet Inc. (Class A)") == company("Alphabet Inc. (Class C)")
           == company("ALPHABET INC-CL A") and company("Fox Corporation") != company("News Corp"))
    twins = wiki.replace('<tr><td>CCC</td><td>C</td>',
                         '<tr><td>CCC</td><td>Twin Co (Class A)</td>').replace(
        '<tr><td>DDD</td><td>D</td><td>Staples</td><td>Packaged Foods &amp; Meats</td>',
        '<tr><td>DDD</td><td>Twin Co (Class B)</td><td>IT</td><td>Application Software</td>')
    halal_twins = dict(halal, compliance={"screen": dict(halal["compliance"]["screen"],
                                                         excluded_symbols=[])}, strategy={
        "momentum": dict(halal["strategy"]["momentum"], picks=3)})
    paired, why = build_targets(halal_twins, date(2026, 10, 6), halal_maps,
                                getter=pages(wiki_page=twins),
                                fetch_monthly=lambda s, span=None: ranked[s],
                                sleep=lambda x: None, reference=lambda s: (30.0, "nasdaq x"))
    expect("two share classes of one company never take two slots",
           paired is None or not {"CCC", "DDD"} <= {p["symbol"] for p in paired["picks"]})
    revoked, why_not = screen_picks(halal, built)
    expect("re-screening today's picks keeps what is still allowed",
           [p["symbol"] for p in revoked] == ["AAA", "CCC"])
    tighter = dict(halal, compliance={"screen": dict(halal["compliance"]["screen"],
                                                     excluded_symbols=[{"symbol": "CCC",
                                                                        "activity": "x",
                                                                        "reason": "found out"}])})
    revoked, why_not = screen_picks(tighter, built)
    expect("an exclusion added mid-month revokes a pick at once",
           [p["symbol"] for p in revoked] == ["AAA"] and why_not[0][0] == "CCC_US_EQ")
    old_targets = {"picks": [{"ticker": "OLD_US_EQ", "symbol": "OLD"}]}
    expect("older targets without a sub-industry are checked against names only",
           screen_picks(halal, old_targets)[0] == old_targets["picks"])
    ref_targets = {"picks": [{"ticker": "CCC_US_EQ", "symbol": "CCC", "usd_price": 30.0,
                              "price_source": "nasdaq 2026-10-05"}]}
    one_source = outside_prices(["CCC_US_EQ"], ref_targets, {}, rate=1.25,
                                fetch=lambda t: ([(date(2026, 10, 6), 30.5)], "nasdaq"))
    expect("a pick checked against Nasdaq is never sized from Nasdaq too", one_source == {})
    two_source = outside_prices(["CCC_US_EQ"], ref_targets, {}, rate=1.25,
                                fetch=lambda t: ([(date(2026, 10, 6), 30.5)], "yahoo"))
    expect("but is sized from Yahoo when the two agree", "CCC_US_EQ" in two_source)

    # Trades. A synthetic account: XXX and YYY are held but no longer picks, CCC is a pick
    # already held below its share.
    cfg = {"strategy": {"momentum": {"core_ticker": "MWIXl_EQ", "core_weight": 0.6,
                                     "band": 0.1, "min_trade": 25}},
           "risk": {"max_order_value": 2500}}
    state = {"cash": 3900.0, "positions": {
        "MWIXL_EQ": {"value": 3800.0}, "XXX_US_EQ": {"value": 1300.0},
        "CCC_US_EQ": {"value": 1050.0}, "YYY_US_EQ": {"value": 350.0}}}
    tg = {"picks": [{"ticker": "AAA_US_EQ"}, {"ticker": "BBB_US_EQ"}, {"ticker": "CCC_US_EQ"}]}
    trades = proposals(cfg, state, tg)
    by = {(t["ticker"], t["action"]): t["value"] for t in trades}
    total = 3900 + 3800 + 1300 + 1050 + 350
    expect("holdings that are not picks are sold whole",
           by.get(("XXX_US_EQ", "sell")) == round(1300 * 1.02, 2)
           and ("YYY_US_EQ", "sell") in by)
    expect("sells come before buys", trades[0]["action"] == "sell")
    expect("a held pick below its share is topped up, not sold",
           abs(by.get(("CCC_US_EQ", "buy"), 0) - (0.4 * total / 3 - 1050)) < 0.01
           and ("CCC_US_EQ", "sell") not in by)
    heavy = dict(state, positions=dict(state["positions"], CCC_US_EQ={"value": 3000.0}))
    heavy_total = total - 1050 + 3000
    trimmed = {(t["ticker"], t["action"]): t["value"] for t in proposals(cfg, heavy, tg)}
    expect("a held pick above its share is trimmed, not sold whole",
           abs(trimmed.get(("CCC_US_EQ", "sell"), 0) - (3000 - 0.4 * heavy_total / 3)) < 0.01)
    expect("new picks are bought to their share",
           abs(by.get(("AAA_US_EQ", "buy"), 0) - 0.4 * total / 3) < 0.01)
    expect("the core is topped up to its share",
           abs(by.get(("MWIXL_EQ", "buy"), 0) - (0.6 * total - 3800)) < 0.01)
    capped = proposals(dict(cfg, risk={"max_order_value": 500}), state, tg)
    expect("no single buy exceeds the per-order cap",
           all(t["value"] <= 500 for t in capped if t["action"] == "buy"))
    settled = {"cash": 20.0, "positions": {"MWIXL_EQ": {"value": 6000.0},
                                           "AAA_US_EQ": {"value": 1350.0},
                                           "BBB_US_EQ": {"value": 1300.0},
                                           "CCC_US_EQ": {"value": 1330.0}}}
    expect("inside the band nothing trades", proposals(cfg, settled, tg) == [])
    unset = {"strategy": {"momentum": {"core_ticker": "MWIXl_EQ", "band": 0.1,
                                       "min_trade": 25}},
             "risk": {"max_order_value": 10000}}
    fresh = {(t["ticker"], t["action"]): t["value"]
             for t in proposals(unset, {"cash": 10000.0, "positions": {}}, tg)}
    expect("with core_weight unset the core takes 20% and the picks the rest",
           abs(fresh.get(("MWIXL_EQ", "buy"), 0) - 2000.0) < 0.02
           and abs(fresh.get(("AAA_US_EQ", "buy"), 0) - 8000.0 / 3) < 0.01)

    # The core's gap equals the cash once every pick is at its share, so asked for whole it
    # always missed the 20 buffer by a little.
    buffered = dict(cfg, risk={"max_order_value": 2500, "min_cash_buffer": 20.0})
    full = {"cash": 2470.0, "positions": {"MWIXL_EQ": {"value": 3760.0},
                                          "AAA_US_EQ": {"value": 1390.0},
                                          "BBB_US_EQ": {"value": 1390.0},
                                          "CCC_US_EQ": {"value": 1390.0}}}
    topup = proposals(buffered, full, tg)
    expect("the core takes what the cash covers above the buffer when that reaches its band",
           len(topup) == 1 and topup[0]["ticker"] == "MWIXL_EQ"
           and 2400.0 <= topup[0]["value"] <= 2470.0 - 20.0)
    short = {"cash": 460.0, "positions": {"MWIXL_EQ": {"value": 3760.0},
                                          "CCC_US_EQ": {"value": 1390.0}}}
    whole = {(t["ticker"], t["action"]): t["value"] for t in proposals(buffered, short, tg)}
    short_total = 460.0 + 3760.0 + 1390.0
    expect("a pick the cash cannot reach is asked for whole, not part-bought",
           abs(whole.get(("AAA_US_EQ", "buy"), 0) - 0.4 * short_total / 3) < 0.01
           and abs(whole.get(("BBB_US_EQ", "buy"), 0) - 0.4 * short_total / 3) < 0.01)
    two = {"cash": 1500.0, "positions": {"MWIXL_EQ": {"value": 3760.0},
                                         "AAA_US_EQ": {"value": 900.0},
                                         "BBB_US_EQ": {"value": 1390.0},
                                         "CCC_US_EQ": {"value": 1390.0}}}
    pair = {(t["ticker"], t["action"]): t["value"] for t in proposals(buffered, two, tg)}
    expect("a pick's buy comes off what the core can use",
           ("AAA_US_EQ", "buy") in pair and pair.get(("MWIXL_EQ", "buy"), 0)
           <= 1500.0 - 20.0 - pair[("AAA_US_EQ", "buy")])

    # Through the real gate, with a 5% broker hold and the default caps: what proposals()
    # cuts to fit must pass gates.decide, or the cut was for nothing.
    import gates
    live_risk = {"max_order_value": 2500.0, "max_daily_spend": 7000.0,
                 "max_orders_per_day": 60, "max_position_value": 1500.0,
                 "max_position_value_by_ticker": {"MWIXl_EQ": 12000.0},
                 "min_cash_buffer": 20.0, "broker_hold": 0.05,
                 "cooldown_hours_per_instrument": 120, "min_confidence": 0.7,
                 "killswitch_file": "HALT_NOT_PRESENT"}
    gated = dict(cfg, risk=live_risk, compliance={"allowlist_only": True,
                                                  "allowlist": [{"ticker": "MWIXl_EQ"}]})
    when = datetime(2026, 10, 12, 9, 17, tzinfo=timezone.utc)

    def through_gate(state, targets):
        state = dict(state, extra_allowlist=[p["ticker"] for p in targets["picks"]],
                     spent_today=0.0, orders_today=0, last_trade={})
        made = proposals(gated, state, targets)
        ok, refused = gates.decide(made, state, gated, when)
        return made, ok, refused

    made, ok, refused = through_gate(full, tg)
    expect("the core's cut top-up allows for the broker's hold and passes the gate",
           [p["ticker"] for p in ok] == ["MWIXL_EQ"] and not refused
           and ok[0]["value"] <= (2470.0 - 20.0) * 0.95)
    # With cut_hold at 0.10: a buy sized to the cash survives a 10% hold, since a refusal
    # would cost the day. It still has to land the core inside its band.
    cautious = dict(gated, strategy={"momentum": dict(cfg["strategy"]["momentum"],
                                                      cut_hold=0.10)})
    state = dict(full, extra_allowlist=[p["ticker"] for p in tg["picks"]],
                 spent_today=0.0, orders_today=0, last_trade={})
    made = proposals(cautious, state, tg)
    ok, refused = gates.decide(made, state, cautious, when)
    mtotal = 2470.0 + 3760.0 + 3 * 1390.0
    expect("with cut_hold 0.10 the core's top-up survives a 10% hold and still reaches the band",
           [p["ticker"] for p in ok] == ["MWIXL_EQ"] and not refused
           and ok[0]["value"] <= (2470.0 - 20.0) * 0.90
           and 3760.0 + ok[0]["value"] >= 0.6 * mtotal * 0.9)
    garbled = dict(cautious, strategy={"momentum": dict(cfg["strategy"]["momentum"],
                                                        cut_hold="x")})
    made = proposals(garbled, state, tg)
    expect("an unreadable cut_hold fails closed: a cut takes at most half the free cash",
           made and all(p["value"] <= (2470.0 - 20.0) * 0.5 for p in made))
    # A pick the cash cannot reach is asked for whole and refused; it must not hold back the
    # cash the core's cut buy needs. The first-run-of-a-month shape, with a sale pending.
    ab = {"picks": [{"ticker": "AAA_US_EQ"}, {"ticker": "BBB_US_EQ"}]}
    rotate = {"cash": 1000.0, "positions": {"MWIXL_EQ": {"value": 5000.0},
                                            "BBB_US_EQ": {"value": 2000.0},
                                            "OLD_US_EQ": {"value": 2000.0}},
              "extra_allowlist": ["AAA_US_EQ", "BBB_US_EQ"], "spent_today": 0.0,
              "orders_today": 0, "last_trade": {}}
    made = proposals(cautious, rotate, ab)
    ok, refused = gates.decide(made, rotate, cautious, when)
    expect("a whole share the cash cannot reach leaves its cash for the core's cut buy",
           sorted((p["ticker"], p["action"]) for p in ok)
           == [("MWIXL_EQ", "buy"), ("OLD_US_EQ", "sell")]
           and [i["proposal"]["ticker"] for i in refused] == ["AAA_US_EQ"])
    ten = {"picks": [{"ticker": "P%d_US_EQ" % i} for i in range(10)]}
    # A case found in review: pick top-ups whose pennies, rounded up, used to push the
    # core's part-buy under the buffer by a fraction of a penny.
    values = [267.28, 396.14, 316.89, 432.2, 363.45, 448.41, 285.46, 365.33, 432.85, 267.73]
    pennies = {"cash": 2429.22, "positions": dict(
        {"MWIXL_EQ": {"value": 3283.67}},
        **{"P%d_US_EQ" % i: {"value": v} for i, v in enumerate(values)})}
    made, ok, refused = through_gate(pennies, ten)
    expect("rounded pick top-ups no longer push the core's part-buy under the buffer",
           ("MWIXL_EQ") in [p["ticker"] for p in ok] and not refused)
    seed, cut_refused, cuts = 12345, [], 0
    for _ in range(400):
        draws = []
        for _ in range(12):
            seed = (seed * 1103515245 + 12345) % (2 ** 31)
            draws.append(seed / float(2 ** 31))
        state = {"cash": round(500 + 3000 * draws[0], 2), "positions": dict(
            {"MWIXL_EQ": {"value": round(2500 + 2500 * draws[1], 2)}},
            **{"P%d_US_EQ" % i: {"value": round(200 + 300 * draws[2 + i], 2)}
               for i in range(10)})}
        made, ok, refused = through_gate(state, ten)
        total = state["cash"] + sum(p["value"] for p in state["positions"].values())
        for item in refused:
            p = item["proposal"]
            held = state["positions"].get(p["ticker"], {}).get("value", 0.0)
            share = 0.6 * total if p["ticker"] == "MWIXL_EQ" else 0.4 * total / 10
            # A whole share comes out floored to the penny, so a cut is a cent or more short.
            if p["value"] < min(share - held, 2500.0) - 0.011:
                cut_refused.append((p["ticker"], p["value"], item["reasons"]))
        cuts += sum(1 for p in made if p["action"] == "buy" and p["ticker"] == "MWIXL_EQ"
                    and p["value"] < min(0.6 * total - state["positions"]
                                         ["MWIXL_EQ"]["value"], 2500.0) - 0.011)
    expect("across 400 accounts, no buy cut to fit the cash is refused by the gate",
           cuts > 50 and not cut_refused)

    # No sale in the batch, so no more cash is coming: a new pick takes what the cash covers
    # rather than leaving its whole slot idle for the month. A rotation whose outgoing pick
    # is already sold, its cash just under the new pick's share.
    rest = {"P%d_US_EQ" % i: {"value": 840.0} for i in range(1, 10)}
    rotated = {"cash": 815.0, "positions": dict(rest, MWIXL_EQ={"value": 2100.0}),
               "extra_allowlist": [p["ticker"] for p in ten["picks"]], "spent_today": 0.0,
               "orders_today": 0, "last_trade": {}}
    split = dict(cautious, strategy={"momentum": dict(cautious["strategy"]["momentum"],
                                                      core_weight=0.2)})
    made = proposals(split, rotated, ten)
    ok, refused = gates.decide(made, rotated, split, when)
    expect("with no sale coming, a new pick takes what the cash covers and the gate passes it",
           [p["ticker"] for p in ok] == ["P0_US_EQ"] and not refused
           and 700.0 <= ok[0]["value"] <= (815.0 - 20.0) * 0.90)
    # A pick's share past the single-stock cap: bought up to the cap, not refused whole.
    big = {"cash": 3000.0, "positions": dict(
        {"MWIXL_EQ": {"value": 3900.0}},
        **{"P%d_US_EQ" % i: {"value": 1500.0} for i in range(1, 10)}),
        "extra_allowlist": [p["ticker"] for p in ten["picks"]], "spent_today": 0.0,
        "orders_today": 0, "last_trade": {}}
    made = proposals(split, big, ten)
    ok, refused = gates.decide(made, big, split, when)
    expect("a pick's buy stops at the 1,500 single-stock cap instead of being refused",
           [(p["ticker"], p["value"]) for p in ok] == [("P0_US_EQ", 1500.0)]
           and not any("position cap" in r for i in refused for r in i["reasons"]))
    # A month's first run with only the core held: ten picks of 800.00 from 8,000.00 cash,
    # with a 7,000.00 daily cap. The cash covers nine of them, the cap eight. A ninth pick
    # booked against the cash although the cap refuses it used to cut the tenth to the
    # remainder, which the gate then passed: a part-buy that started its 120-hour cooldown
    # at under half its share. Each pick must be asked for whole, and the cap must refuse
    # whole buys only, to be bought whole on a later day.
    first = {"cash": 8000.0, "positions": {"MWIXL_EQ": {"value": 2000.0}},
             "extra_allowlist": [p["ticker"] for p in ten["picks"]], "spent_today": 0.0,
             "orders_today": 0, "last_trade": {}}
    made = proposals(split, first, ten)
    ok, refused = gates.decide(made, first, split, when)
    expect("a buy the daily cap refuses holds no cash back: every pick is asked for whole",
           len(made) == 10 and all(p["action"] == "buy" and p["value"] == 800.0 for p in made))
    expect("and the gate buys eight whole and refuses two whole for the cap, cutting none",
           [p["value"] for p in ok] == [800.0] * 8 and len(refused) == 2
           and all(any("daily cap" in r for r in i["reasons"]) for i in refused))
    later = dict(first, spent_today=6400.0, cash=1600.0)
    later["positions"] = dict(first["positions"],
                              **{"P%d_US_EQ" % i: {"value": 800.0} for i in range(8)})
    made = proposals(split, later, ten)
    expect("a later run the same day, 600.00 under the cap, still asks for whole shares",
           [(p["ticker"], p["value"]) for p in made]
           == [("P8_US_EQ", 800.0), ("P9_US_EQ", 800.0)])

    expect("every trade is tagged as momentum",
           all(t["source"] == "momentum" and t["confidence"] == 1.0 for t in trades))

    # Targets are for the adapter whose instruments they were built from: a switch from
    # paper to Trading 212 rebuilds the month from Trading 212's own tickers.
    stamped = {"month": "2026-10", "built": "2026-10-06", "adapter": "paper",
               "picks": [{"ticker": "AAA_US_EQ"}]}
    october = date(2026, 10, 7)
    expect("targets built on paper are current for paper",
           current(stamped, october, "paper") and current(stamped, october, "Paper"))
    expect("but not for Trading 212, so its first run rebuilds them",
           not current(stamped, october, "trading212")
           and not in_window(stamped, october, 7, "trading212"))
    expect("targets with no adapter, from an older version, count for any adapter",
           current(dict(stamped, adapter=None), october, "trading212")
           and current({k: v for k, v in stamped.items() if k != "adapter"}, october,
                       "trading212"))
    expect("asked without an adapter, only the month decides, as before",
           current(stamped, october) and in_window(stamped, october, 7)
           and not current(stamped, date(2026, 11, 2), "paper"))

    # Prices
    tg2 = {"picks": [{"ticker": "AAA_US_EQ", "symbol": "AAA", "usd_price": 100.0},
                     {"ticker": "BBB_US_EQ", "symbol": "BBB", "usd_price": 100.0},
                     {"ticker": "SNDK1_US_EQ", "symbol": "SNDK", "usd_price": 1500.0}]}
    feed = {"AAA_US_EQ": ([(date(2026, 10, 6), 105.0)], "yahoo"),
            "BBB_US_EQ": ([(date(2026, 10, 6), 160.0)], "yahoo"),
            "SNDK_US_EQ": ([(date(2026, 10, 6), 1520.0)], "yahoo")}
    got = outside_prices(["AAA_US_EQ", "BBB_US_EQ", "ZZZ_US_EQ"], tg2, cfg,
                         fetch=lambda t: feed[t], rate=1.25)
    expect("dollars are divided by GBPUSD, not multiplied",
           abs(got["AAA_US_EQ"][0] - 84.0) < 1e-9)
    expect("a price that disagrees with the SPUS file is refused", "BBB_US_EQ" not in got)
    expect("a ticker that is not a pick gets no price", "ZZZ_US_EQ" not in got)
    renamed = outside_prices(["SNDK1_US_EQ"], tg2, cfg, fetch=lambda t: feed[t], rate=1.25)
    expect("a pick whose Trading 212 ticker differs is priced by its symbol",
           abs(renamed["SNDK1_US_EQ"][0] - 1216.0) < 1e-9)
    expect("no exchange rate means no prices",
           outside_prices(["AAA_US_EQ"], tg2, cfg, fetch=lambda t: feed[t], rate=0) == {})
    fx = {"chart": {"result": [{"timestamp": [1759708800],
                                "indicators": {"quote": [{"close": [1.34]}]}}]}}
    expect("GBPUSD parses", gbpusd(lambda url, h: json.dumps(fx)) == 1.34)
    bad = {"chart": {"result": [{"timestamp": [1759708800],
                                 "indicators": {"quote": [{"close": [134.0]}]}}]}}
    expect("an absurd exchange rate is refused", gbpusd(lambda url, h: json.dumps(bad)) is None)

    # Account currencies: dollars per one unit of each, from its own pair and inside its own
    # bounds; USD needs none; anything else gets no rate and so no buy.
    def quote(close, asked):
        def get(url, headers):
            asked.append(url)
            return json.dumps({"chart": {"result": [{"timestamp": [1759708800],
                                                     "indicators": {"quote": [{"close":
                                                                               [close]}]}}]}})
        return get

    asked = []
    expect("EURUSD is read from its own pair",
           fx_rate("EUR", quote(1.08, asked)) == 1.08 and "EURUSD=X" in asked[0])
    expect("a EUR rate outside 0.8 to 1.6 is refused",
           fx_rate("EUR", quote(1.7, [])) is None and fx_rate("EUR", quote(0.75, [])) is None)
    expect("a GBP rate outside 1.0 to 2.0 is refused",
           fx_rate("GBP", quote(0.95, [])) is None and fx_rate("GBP", quote(2.1, [])) is None)
    asked = []
    expect("a USD account needs no rate and asks for none",
           fx_rate("USD", quote(1.3, asked)) == 1.0 and not asked)
    expect("an unknown currency gets no rate, without asking",
           fx_rate("JPY", quote(150.0, asked)) is None and fx_rate("", quote(1.3, asked)) is None
           and not asked)
    expect("a currency code is read in any case", fx_rate(" eur", quote(1.08, [])) == 1.08)
    asked = []
    euro = outside_prices(["AAA_US_EQ"], tg2, dict(cfg, account={"currency": "EUR"}),
                          fetch=lambda t: feed[t], getter=quote(1.05, asked))
    expect("a euro account converts through EURUSD and says so",
           abs(euro["AAA_US_EQ"][0] - 100.0) < 1e-9 and "EURUSD 1.0500" in euro["AAA_US_EQ"][1]
           and "EURUSD=X" in asked[0])
    asked = []
    dollar = outside_prices(["AAA_US_EQ"], tg2, dict(cfg, account={"currency": "USD"}),
                            fetch=lambda t: feed[t], getter=quote(1.3, asked))
    expect("a dollar account takes the dollar close as it is",
           dollar["AAA_US_EQ"][0] == 105.0 and "no conversion" in dollar["AAA_US_EQ"][1]
           and not asked)
    expect("an account currency with no known rate gets no outside price, so no buy",
           outside_prices(["AAA_US_EQ"], tg2, dict(cfg, account={"currency": "JPY"}),
                          fetch=lambda t: feed[t], getter=quote(150.0, [])) == {})
    asked = []
    pounds = outside_prices(["AAA_US_EQ"], tg2, cfg, fetch=lambda t: feed[t],
                            getter=quote(1.25, asked))
    expect("with no account currency set it is GBP, through GBPUSD",
           abs(pounds["AAA_US_EQ"][0] - 84.0) < 1e-9 and "GBPUSD 1.2500" in pounds["AAA_US_EQ"][1]
           and "GBPUSD=X" in asked[0])
    expect("a rate outside its bounds means no outside prices",
           outside_prices(["AAA_US_EQ"], tg2, dict(cfg, account={"currency": "EUR"}),
                          fetch=lambda t: feed[t], getter=quote(2.5, [])) == {})
    expect("account_currency defaults to GBP and reads the config",
           account_currency({}) == "GBP" and account_currency({"account": None}) == "GBP"
           and account_currency({"account": {"currency": "usd"}}) == "USD")

    width = max(len(n) for n, _ in checks)
    for name, passed in checks:
        print("%s  %s" % ("pass" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in checks if not p]
    print("\n%d checks, %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
