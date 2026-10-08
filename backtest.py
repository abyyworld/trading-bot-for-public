#!/usr/bin/env python3
"""
Backtest: which way of picking halal stocks has actually paid, after Trading 212's costs.

Its absolute returns are inflated by survivorship and pre-inclusion bias (a stock counts for
years before it joined the index, so one that ran up and was then added looks like a winner
nobody could have bought); research.py, which counts each stock only from its join date, is
the trustworthy source. Compare rows here, never quote their levels.

Built to answer with data rather than opinion, including about the bot's own optional rules.
It replays monthly history for a halal universe and compares:

  ew_all        every stock in the universe, equal weight, rebalanced monthly (the baseline)
  mom5/10/20    the 5, 10 or 20 with the best 12-month return, skipping the last month
                (momentum, the best documented stock-picking effect in the literature)
  mom10_rule    mom10 under the near-high rule (risk.near_high): nothing within 5% of its
                52-week high
  mom10_buffer  mom10, but a holding is kept while it stays in the top 20, so fewer trades
                (Novy-Marx and Velikov 2016: a buffer cuts momentum's costs a lot)
  mom10_trend   mom10 while the universe's own equal-weight index is above its 10-month
                average, cash otherwise (Faber 2007; momentum crashes come after bear
                markets, Daniel and Moskowitz 2016). Cash earns nothing here, as halal cash
  book, book_trend  60% ew_all + 40% mom10 (or mom10_trend), a PROXY for the momentum book
                at core_weight 0.6: ew_all stands in for the core fund, which has too short
                a history
  high10        the 10 closest to their 52-week high (George and Hwang 2004)
  dip10         the 10 furthest below their 52-week high ("buy the dip")
  SPY, ...      plain buy and hold of reference funds, for scale

Universe: the holdings of SPUS, the S&P 500 Shariah ETF, from the manager's own daily file
(about 218 stocks screened by S&P's Shariah methodology, business AND ratio screens). This is
the list the momentum book uses under universe 'spus', and its fallback under 'not_haram'.
Fallback here: today's S&P 500, minus the businesses every Shariah index excludes
(financials, real estate, alcohol, tobacco, gambling, media and entertainment, hotels and
cruise lines), read from Wikipedia's constituent table. That is the business screen behind
SPUS; the debt and cash ratio screens cannot be applied historically, so this is a proxy for
testing RULES, not a list of stocks to buy. Buys still come only from the allowlist and the
momentum book's screened picks. Override with tickers on the command line. (A fund's top
holdings were tried first and rejected: the biggest holdings are by construction the past
winners, and every strategy showed 38% a year.)

Prices: Yahoo monthly bars. Returns use the dividend-adjusted close (what a holder earned);
the 52-week-high tests use the plain close, like the near-high gate. Monthly closes stand in for
daily highs, so "near the high" here is approximate.

Costs: 0.20% of every amount traded, each way: Trading 212's 0.15% FX fee on US shares plus
an allowance for the bid-ask spread. No commission, no stamp duty on US shares.

READ THE BIASES BEFORE THE RESULTS. The universe is TODAY's halal list, so every stock in it
survived and stayed compliant; failures and delistings are missing, which flatters every
strategy, momentum included, and halal status is applied with hindsight. Treat the
differences between strategies as the finding, not the absolute returns, and treat any edge
smaller than a few percent a year as noise.

    python3 backtest.py run                  fetch, simulate, print the table
    python3 backtest.py run AAPL MSFT NVDA   a custom universe of Yahoo symbols
    python3 backtest.py selftest
"""

import argparse
import csv
import io
import json
import math
import sys
import time
from datetime import datetime, timezone

import pricefeed

# iShares UK puts an investor-type page in front of everything; siteEntryPassthrough skips it.
# Tried in order; the first that parses to a non-empty list wins.
ISHARES_US_ISLAMIC = tuple(
    "https://www.ishares.com/uk/%s/en/products/251393/ishares-msci-usa-islamic-ucits-etf/"
    "1506575576011.ajax?fileType=csv&fileName=ISUS_holdings&dataType=fund"
    "&siteEntryPassthrough=true" % audience for audience in ("individual", "professional"))
SPUS_HOLDINGS = "https://www.sp-funds.com/wp-content/uploads/data/TidalFG_Holdings_SPUS.csv"
SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
EXCLUDED_SECTORS = {"financials", "real estate"}
EXCLUDED_SUB_INDUSTRIES = {
    "brewers", "distillers & vintners", "tobacco", "casinos & gaming",
    "movies & entertainment", "broadcasting", "cable & satellite", "advertising",
    "publishing", "hotels, resorts & cruise lines"}
REFERENCES = ("SPY", "SPUS", "HLAL")
MIN_NAMES = 40   # a month counts only when at least this many stocks have a signal
SPAN = "20y"     # see fetch_monthly; checked to stay monthly with backtest.py gaps
COST_PER_SIDE = 0.002
LOOKBACK = 12   # months of momentum
SKIP = 1        # the most recent month is skipped, the standard 12-1 construction
NEAR = 0.05     # the near-high rule's band below the 52-week high


# ---------------------------------------------------------------------- data

def parse_ishares(text):
    """[(yahoo_symbol, name, sector)] for the equity rows of an iShares holdings CSV."""
    lines = text.lstrip("\ufeff").splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.strip().strip('"').startswith("Ticker")), None)
    if start is None:
        return []
    rows = list(csv.reader(io.StringIO("\n".join(lines[start:]))))
    header = [h.strip() for h in rows[0]]
    out = []
    for row in rows[1:]:
        if len(row) < len(header):
            break
        item = dict(zip(header, (cell.strip() for cell in row)))
        if "equity" not in item.get("Asset Class", "").lower():
            continue
        ticker = item.get("Ticker", "").replace(".", "-").replace("/", "-").replace(" ", "-")
        if ticker and ticker != "-":
            out.append((ticker, item.get("Name", ""), item.get("Sector", "")))
    return out


def parse_spus(text):
    """[(yahoo_symbol, name, '')] from the SP Funds daily holdings CSV; cash lines dropped."""
    out = []
    for row in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        ticker = (row.get("StockTicker") or "").strip().upper()
        name = (row.get("SecurityName") or "").strip()
        if not ticker or not ticker[0].isalpha() or "CASH" in ticker or "CASH" in name.upper():
            continue
        out.append((ticker.replace(".", "-").replace("/", "-").replace(" ", "-"), name, ""))
    return out


def parse_sp500(html):
    """[(yahoo_symbol, name, sector)] from Wikipedia's S&P 500 table, Shariah business screen."""
    import html as entities
    import re
    table = html.split('id="constituents"', 1)[-1].split("</table>", 1)[0]
    out = []
    for row in table.split("<tr")[1:]:
        cells = [entities.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 4 or not cells[0]:
            continue
        symbol, name, sector, sub = cells[0], cells[1], cells[2], cells[3]
        if sector.lower() in EXCLUDED_SECTORS or sub.lower() in EXCLUDED_SUB_INDUSTRIES:
            continue
        out.append((symbol.replace(".", "-"), name, sector))
    return out


def fetch_universe(getter=None):
    """(universe, notes): SPUS holdings, else the screened S&P 500, else iShares."""
    getter = getter or pricefeed.http_get
    notes = []
    try:
        rows = parse_spus(getter(SPUS_HOLDINGS, {"Accept": "text/csv,*/*"}))
        notes.append("SPUS holdings (S&P 500 Shariah): %d stocks" % len(rows))
        if len(rows) >= 100:
            return rows, notes
    except Exception as error:
        notes.append("SPUS holdings failed: %s" % error)
    try:
        rows = parse_sp500(getter(SP500, {"Accept": "text/html,*/*"}))
        notes.append("S&P 500 after the Shariah business screen: %d stocks" % len(rows))
        if len(rows) >= 100:
            return rows, notes
    except Exception as error:
        notes.append("S&P 500 list failed: %s" % error)
    for url in ISHARES_US_ISLAMIC:
        try:
            text = getter(url, {"Accept": "text/csv,*/*"})
        except Exception as error:
            notes.append("ishares failed: %s" % error)
            continue
        rows = parse_ishares(text)
        if rows:
            return rows, notes + ["ishares: %d stocks" % len(rows)]
        notes.append("ishares gave no equities; it began %r" % text[:80])
    return [], notes


def parse_monthly(payload):
    """{'YYYY-MM': (close, adjusted_close)} from a Yahoo monthly chart response."""
    chart = (payload or {}).get("chart") or {}
    result = (chart.get("result") or [None])[0] or {}
    stamps = result.get("timestamp") or []
    indicators = result.get("indicators") or {}
    raw = ((indicators.get("quote") or [{}])[0] or {}).get("close") or []
    adj = ((indicators.get("adjclose") or [{}])[0] or {}).get("adjclose") or raw
    out = {}
    for stamp, close, adjusted in zip(stamps, raw, adj):
        if isinstance(close, (int, float)) and isinstance(adjusted, (int, float)) \
                and close > 0 and adjusted > 0:
            month = datetime.fromtimestamp(int(stamp), tz=timezone.utc).strftime("%Y-%m")
            out[month] = (float(close), float(adjusted))
    return out


def fetch_monthly(symbol, getter=pricefeed.http_get, sleep=time.sleep, span=None):
    # NOT range=max: for long-listed companies Yahoo answers range=max with QUARTERLY bars
    # even when asked for 1mo, which silently dropped 97 of 215 SPUS names (every one with a
    # long history) from the ranking. A bounded span keeps the bars monthly.
    url = ("https://query2.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1mo"
           "&includeAdjustedClose=true" % (symbol, span or SPAN))
    try:
        return parse_monthly(json.loads(pricefeed._retrying(getter, url, None, sleep)))
    except Exception as error:  # one bad symbol must not sink the whole run
        print("  %-8s skipped: %s" % (symbol, error), file=sys.stderr)
        return {}


# ---------------------------------------------------------------------- simulation

def signals_at(data, months, t):
    """{symbol: (momentum, distance_below_high)} using only months[..t]. None if too short."""
    out = {}
    for symbol, bars in data.items():
        window = [bars.get(m) for m in months[max(0, t - LOOKBACK): t + 1]]
        if len(window) < LOOKBACK + 1 or any(b is None for b in window):
            continue
        past, recent = window[0][1], window[-1 - SKIP][1]
        closes = [b[0] for b in window[1:]]          # the last 12 monthly closes
        out[symbol] = (recent / past - 1.0, 1.0 - closes[-1] / max(closes))
    return out


def pick(strategy, signals):
    ranked = sorted(signals.items(), key=lambda kv: kv[0])   # deterministic ties
    if strategy == "ew_all":
        return [s for s, _ in ranked]
    if strategy == "mom5":
        return [s for s, _ in sorted(ranked, key=lambda kv: -kv[1][0])[:5]]
    if strategy == "mom10":
        return [s for s, _ in sorted(ranked, key=lambda kv: -kv[1][0])[:10]]
    if strategy == "mom20":
        return [s for s, _ in sorted(ranked, key=lambda kv: -kv[1][0])[:20]]
    if strategy == "mom10_rule":
        allowed = [kv for kv in ranked if kv[1][1] >= NEAR]
        return [s for s, _ in sorted(allowed, key=lambda kv: -kv[1][0])[:10]]
    if strategy == "high10":
        return [s for s, _ in sorted(ranked, key=lambda kv: kv[1][1])[:10]]
    if strategy == "dip10":
        return [s for s, _ in sorted(ranked, key=lambda kv: -kv[1][1])[:10]]
    raise ValueError(strategy)


def buffered(signals, held, size=10, keep_within=20):
    """Top `size` by momentum, except a name already held stays while it ranks inside
    `keep_within`. Swapping the 10th for the 11th every month pays costs for nothing."""
    ranked = [s for s, _ in sorted(signals.items(), key=lambda kv: (-kv[1][0], kv[0]))]
    rank = {s: i for i, s in enumerate(ranked)}
    keep = [s for s in held if rank.get(s, keep_within) < keep_within][:size]
    return keep + [s for s in ranked if s not in keep][:size - len(keep)]


def ew_index(data, months):
    """Level of the universe's own equal-weight index at each month, from 1.0."""
    level, out = 1.0, [1.0]
    for a, b in zip(months, months[1:]):
        moves = [bars[b][1] / bars[a][1] - 1.0 for bars in data.values()
                 if bars.get(a) and bars.get(b)]
        level *= 1.0 + (sum(moves) / len(moves) if moves else 0.0)
        out.append(level)
    return out


def trend_up(levels, t, window=10):
    """The index above its own average of the last `window` month ends, inclusive."""
    recent = levels[max(0, t - window + 1): t + 1]
    return len(recent) == window and levels[t] > sum(recent) / window


def simulate(strategy, data, months, cost=COST_PER_SIDE, min_names=MIN_NAMES):
    """[(month, net_return)] holding the strategy's picks from each month end to the next."""
    weights, out, held = {}, [], []
    levels = ew_index(data, months) if strategy == "mom10_trend" else None
    for t in range(LOOKBACK, len(months) - 1):
        signals = signals_at(data, months, t)
        if len(signals) < min_names:
            continue
        if strategy == "mom10_buffer":
            chosen = buffered(signals, held)
        elif strategy == "mom10_trend":
            chosen = pick("mom10", signals) if trend_up(levels, t) else []
        else:
            chosen = pick(strategy, signals)
        held = chosen
        target = {s: 1.0 / len(chosen) for s in chosen}
        traded = sum(abs(target.get(s, 0.0) - weights.get(s, 0.0))
                     for s in set(target) | set(weights))
        gross, drifted = 0.0, {}
        for s, w in target.items():
            now, nxt = data[s].get(months[t]), data[s].get(months[t + 1])
            r = (nxt[1] / now[1] - 1.0) if now and nxt else 0.0
            gross += w * r
            drifted[s] = w * (1.0 + r)
        total = sum(drifted.values()) or 1.0
        weights = {s: v / total for s, v in drifted.items()}
        out.append((months[t + 1], gross - traded * cost))
    return out


def blend(parts):
    """[(month, return)] of fixed weights over several return series, rebalanced monthly,
    over the months they all share. parts is [(weight, series)]."""
    maps = [(w, dict(series)) for w, series in parts]
    shared = sorted(set.intersection(*(set(m) for _, m in maps))) if maps else []
    return [(month, sum(w * m[month] for w, m in maps)) for month in shared]


def buy_and_hold(bars, months):
    out = []
    for a, b in zip(months, months[1:]):
        if bars.get(a) and bars.get(b):
            out.append((b, bars[b][1] / bars[a][1] - 1.0))
    return out


def metrics(returns):
    """CAGR, annual volatility, Sharpe (no risk-free rate), max drawdown, worst 12 months."""
    r = [x for _, x in returns]
    if len(r) < 12:
        return None
    growth, peak, worst_dd, curve = 1.0, 1.0, 0.0, []
    for x in r:
        growth *= 1.0 + x
        curve.append(growth)
        peak = max(peak, growth)
        worst_dd = min(worst_dd, growth / peak - 1.0)
    years = len(r) / 12.0
    mean = sum(r) / len(r)
    vol = math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(12)
    rolling = [curve[i] / (curve[i - 12] if i >= 12 else 1.0) - 1.0
               for i in range(11, len(curve))]
    return {"cagr": growth ** (1.0 / years) - 1.0, "vol": vol,
            "sharpe": (mean * 12) / vol if vol else 0.0, "max_dd": worst_dd,
            "worst_12m": min(rolling), "months": len(r)}


def table(results, since=None):
    lines = ["%-12s %7s %7s %7s %8s %9s %6s" % ("strategy", "CAGR", "vol", "sharpe",
                                                 "max_dd", "worst12m", "months")]
    for name, returns in results:
        sliced = [x for x in returns if since is None or x[0] > since]
        m = metrics(sliced)
        if not m:
            lines.append("%-12s %s" % (name, "too little data"))
            continue
        lines.append("%-12s %6.1f%% %6.1f%% %7.2f %7.1f%% %8.1f%% %6d"
                     % (name, m["cagr"] * 100, m["vol"] * 100, m["sharpe"], m["max_dd"] * 100,
                        m["worst_12m"] * 100, m["months"]))
    return "\n".join(lines)


# ---------------------------------------------------------------------- commands

def cmd_run(args):
    if args.symbols:
        universe = [(s.upper(), "", "") for s in args.symbols]
        print("universe: %d symbols from the command line" % len(universe))
    else:
        universe, notes = fetch_universe()
        for note in notes:
            print("  %s" % note)
        # A research command that cannot get its data is not a bot failure, so it exits 0
        # and says why: a non-zero exit would read as one to whatever scheduled it.
        if len(universe) < 30:
            print("could not get a halal universe; give tickers on the command line instead")
            return 0
        print("universe: %d stocks" % len(universe))
        print("note: SPUS has existed only since 2019; earlier years use today's list, so read"
              " the long window as the rule's behaviour, not as returns you could have had")
    data = {}
    for number, (symbol, _, _) in enumerate(universe):
        if number:
            time.sleep(0.5)
        bars = fetch_monthly(symbol)
        if bars:
            data[symbol] = bars
    references = {s: fetch_monthly(s) for s in REFERENCES}
    print("price history for %d of %d" % (len(data), len(universe)))
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    months = sorted({m for bars in data.values() for m in bars if m < this_month})
    if len(months) < LOOKBACK + 13:
        print("too little history to test anything")
        return 0
    print("months %s to %s, costs %.2f%% per side\n" % (months[0], months[-1],
                                                      COST_PER_SIDE * 100))
    results = [(name, simulate(name, data, months))
               for name in ("ew_all", "mom5", "mom10", "mom20", "mom10_rule", "mom10_buffer",
                            "mom10_trend", "high10", "dip10")]
    series = dict(results)
    results += [("book", blend([(0.6, series["ew_all"]), (0.4, series["mom10"])])),
                ("book_trend", blend([(0.6, series["ew_all"]), (0.4, series["mom10_trend"])]))]
    results += [(s, buy_and_hold(bars, months)) for s, bars in references.items() if bars]
    start = min((r[0][0] for _, r in results if r), default=months[0])
    print("FULL PERIOD from %s\n%s\n" % (start, table(results)))
    for label, years in (("LAST 10 YEARS", 10), ("LAST 5 YEARS", 5), ("LAST 3 YEARS", 3)):
        cut = "%04d-%s" % (int(months[-1][:4]) - years, months[-1][5:])
        print("%s (after %s)\n%s\n" % (label, cut, table(results, since=cut)))
    latest = signals_at(data, months, len(months) - 1)
    print("TODAY'S PICKS (month end %s)" % months[-1])
    for name in ("mom5", "mom10", "mom20"):
        print("  %-11s %s" % (name, " ".join(pick(name, latest))))
    return 0


def cmd_selftest(args):
    checks = []

    def expect(name, condition):
        checks.append((name, bool(condition)))

    months = ["2020-%02d" % m for m in range(1, 13)] + ["2021-%02d" % m for m in range(1, 13)]

    def path(rate, jump_at=None, jump=0.0):
        bars, price = {}, 100.0
        for i, m in enumerate(months):
            price *= 1.0 + rate + (jump if i == jump_at else 0.0)
            bars[m] = (price, price)
        return bars

    data = {"UP": path(0.03), "FLAT": path(0.0), "DOWN": path(-0.02)}
    sig = signals_at(data, months, 12)
    expect("momentum ranks the riser first", pick("mom10", sig)[0] == "UP")
    expect("mom5 holds at most five", len(pick("mom5", dict((("S%d" % i, (i, 0.1)) for i in range(9))))) == 5)
    expect("a riser sits at its high and a faller below it",
           sig["UP"][1] == 0.0 and sig["DOWN"][1] > 0.1)
    expect("the near-high rule removes the stock at its high",
           "UP" not in pick("mom10_rule", sig))
    expect("dip buying picks the faller first", pick("dip10", sig)[0] == "DOWN")
    expect("too short a history gives no signal", signals_at(data, months, 5) == {})

    late = dict(data, LATE=path(0.0, jump_at=13, jump=5.0))
    expect("no look-ahead: a jump next month is invisible today",
           pick("mom10", signals_at(late, months, 12))[0] == "UP")
    skipped = {"S": path(0.0, jump_at=12, jump=1.0), "UP": path(0.03)}
    expect("the most recent month is skipped in momentum",
           pick("mom10", signals_at(skipped, months, 12))[0] == "UP")

    many = {"S%02d" % i: (float(i), 0.0) for i in range(30)}   # S29 strongest
    expect("the buffer keeps a holding that slipped to 15th",
           "S15" in buffered(many, ["S15"], 10, 20) and len(buffered(many, ["S15"], 10, 20)) == 10)
    expect("but drops one that fell out of the top 20", "S05" not in buffered(many, ["S05"]))
    expect("with nothing held it is plain top 10",
           buffered(many, []) == ["S%02d" % i for i in range(29, 19, -1)])
    rising = {"A": path(0.02), "B": path(0.01)}
    falling = {"A": path(-0.02), "B": path(-0.03)}
    expect("a rising index is in an uptrend", trend_up(ew_index(rising, months), 12))
    expect("a falling one is not", not trend_up(ew_index(falling, months), 12))
    expect("too few months is not an uptrend", not trend_up(ew_index(rising, months), 5))
    crashing = {k: path(-0.03) for k in ("A", "B", "C")}
    held_cash = simulate("mom10_trend", crashing, months, cost=0.0, min_names=1)
    expect("in a downtrend the trend book holds cash and loses nothing",
           held_cash and all(r == 0.0 for _, r in held_cash))
    expect("in an uptrend it holds mom10",
           simulate("mom10_trend", data, months, cost=0.0, min_names=1)[-1][1]
           == simulate("mom10", data, months, cost=0.0, min_names=1)[-1][1])
    mixed = blend([(0.6, [("a", 0.10), ("b", 0.0)]), (0.4, [("a", -0.10), ("c", 1.0)])])
    expect("the blend weights shared months and drops the rest",
           len(mixed) == 1 and abs(mixed[0][1] - 0.02) < 1e-12)

    free = simulate("mom10", data, months, cost=0.0, min_names=1)
    paid = simulate("mom10", data, months, cost=0.01, min_names=1)
    expect("costs reduce returns", sum(r for _, r in paid) < sum(r for _, r in free))
    expect("one return per month held", len(free) == len(months) - 1 - LOOKBACK)
    expect("a month with too few stocks is not traded",
           simulate("mom10", data, months) == [])

    steady = [("m%d" % i, 0.01) for i in range(24)]
    m = metrics(steady)
    expect("1% a month compounds to 12.68% a year", abs(m["cagr"] - 0.126825) < 1e-4)
    crash = [("a", 0.10), ("b", -0.50), ("c", 0.20)] + [("d%d" % i, 0.0) for i in range(12)]
    expect("max drawdown is peak to trough", abs(metrics(crash)["max_dd"] + 0.5) < 1e-9)

    sample = ('Fund Holdings as of,"Oct 03, 2026"\n\n'
              'Ticker,Name,Sector,Asset Class,Market Value,Weight (%)\n'
              'NVDA,NVIDIA CORP,Information Technology,Equity,"1,000",10.5\n'
              'BRK.B,BERKSHIRE,Financials,Equity,"500",5.0\n'
              'USD,USD CASH,Cash and/or Derivatives,Cash,"10",0.1\n'
              '\n"The content contained herein"\n')
    parsed = parse_ishares(sample)
    expect("ishares rows parse, cash dropped", [p[0] for p in parsed] == ["NVDA", "BRK-B"])
    expect("an html page is no universe", parse_ishares("<!DOCTYPE html><html>") == [])
    page = ('<table id="constituents"><tr><th>Symbol</th></tr>'
            '<tr><td><a href="x">MMM</a></td><td>3M</td><td>Industrials</td>'
            '<td>Industrial Conglomerates</td></tr>'
            '<tr><td>JPM</td><td>JPMorgan</td><td>Financials</td><td>Diversified Banks</td></tr>'
            '<tr><td>BF.B</td><td>Brown-Forman</td><td>Consumer Staples</td>'
            '<td>Distillers &amp; Vintners</td></tr>'
            '<tr><td>BRK.B</td><td>Berkshire</td><td>Financials</td><td>Insurance</td></tr>'
            '<tr><td>DIS</td><td>Disney</td><td>Communication Services</td>'
            '<td>Movies &amp; Entertainment</td></tr></table><table><tr><td>ZZZ</td></tr>')
    kept = [r[0] for r in parse_sp500(page)]
    expect("the business screen drops banks, insurers, alcohol and entertainment",
           kept == ["MMM"])
    holdings = ("Date,Account,StockTicker,CUSIP,SecurityName,Shares,Price\n"
                "10/06/2026,SPUS,NVDA,67066G104,NVIDIA Corp,1,238.9\n"
                "10/06/2026,SPUS,BRK.B,0,Example B,1,1\n"
                "10/06/2026,SPUS,,0,Cash & Other,1,1\n"
                "10/06/2026,SPUS,CASH,0,Cash,1,1\n")
    expect("spus holdings parse, cash dropped",
           [r[0] for r in parse_spus(holdings)] == ["NVDA", "BRK-B"])

    width = max(len(n) for n, _ in checks)
    for name, passed in checks:
        print("%s  %s" % ("pass" if passed else "FAIL", name.ljust(width)))
    failed = [n for n, p in checks if not p]
    print("\n%d checks, %d failed" % (len(checks), len(failed)))
    return 1 if failed else 0


PROBES = (
    "https://www.sp-funds.com/spus/",
    "https://www.sp-funds.com/spte/",
    "https://www.wahed.com/hlal",
    "https://marketxls.com/etfs/hlal/holdings",
    "https://marketxls.com/etfs/spus/holdings",
    "https://www.zacks.com/funds/etf/SPUS/holding",
    "https://www.barchart.com/etfs-funds/quotes/SPUS/constituents",
    "https://www.etf.com/SPUS",
    "https://www.ishares.com/uk/individual/en/products/251393/ishares-msci-usa-islamic-ucits-etf",
)


def cmd_probe(args):
    """Which holdings sources answer from here, how big, and what they look like."""
    import re
    for url in args.urls or PROBES:
        try:
            text = pricefeed.http_get(url, {"Accept": "text/html,text/csv,*/*"})
        except Exception as error:
            print("%-75s FAILED %s" % (url[:75], error))
            continue
        links = sorted(set(re.findall(r'(https?://[^"\' <>]+?\.(?:csv|xlsx|xls|pdf)[^"\' <>]*)', text)))
        tickers = re.findall(r'\b[A-Z]{2,5}\b', re.sub(r"<[^>]+>", " ", text))
        print("%-75s %7d bytes, %4d capitalised words, %d file links"
              % (url[:75], len(text), len(set(tickers)), len(links)))
        if ".csv" in url.lower():
            for line in text.splitlines()[:6]:
                print("    | %s" % line[:200])
            print("    | ... %d lines in all" % len(text.splitlines()))
        for link in links[:8]:
            print("    %s" % link[:160])
    return 0


def cmd_gaps(args):
    """Which universe names lack a complete 13-month window at the last complete month, and
    what months Yahoo actually returned for them. Diagnostic, read-only."""
    universe, notes = fetch_universe()
    data = {}
    for number, (symbol, _, _) in enumerate(universe):
        if number:
            time.sleep(0.5)
        data[symbol] = fetch_monthly(symbol, span=args.span)
    print("span %s" % args.span)
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    months = sorted({m for bars in data.values() for m in bars if m < this_month})
    want = months[-LOOKBACK - 1:]
    print("universe %d, window %s to %s" % (len(universe), want[0], want[-1]))
    short = {s: [m for m in want if m not in bars] for s, bars in data.items()}
    short = {s: missing for s, missing in short.items() if missing}
    print("%d names with gaps" % len(short))
    from collections import Counter
    print("missing months: %s" % dict(Counter(m for v in short.values() for m in v)))
    for symbol in sorted(short)[:25]:
        recent = sorted(data[symbol])[-16:]
        print("  %-6s missing %s; has %s" % (symbol, ",".join(short[symbol]), ",".join(recent)))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("symbols", nargs="*")
    run.set_defaults(func=cmd_run)
    gaps = sub.add_parser("gaps")
    gaps.add_argument("span", nargs="?", default=SPAN)
    gaps.set_defaults(func=cmd_gaps)
    probe = sub.add_parser("probe")
    probe.add_argument("urls", nargs="*")
    probe.set_defaults(func=cmd_probe)
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
